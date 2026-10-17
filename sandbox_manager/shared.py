"""Shared folder per session: what the Agent saves in the Sandbox also appears on the Host (배주한 요청 2026-10-15).

    share = make_share(r"C:\\Users\\me\\SecureCUA\\codex-work\\shared", session_id)
    sm.prepare(..., shared=share)          # mapped WRITABLE at config.GUEST_SHARED
    ...                                    # the Guest mirrors Desktop\\Shared into it every few seconds
    finish_shared(share)                   # session end: scan, then copy to Downloads\\SandboxShared\\<session>

For portable programs and plain files (PDF, zip, ...). An installed program (service, driver, registry)
does not come along: only files do.

This is the one writable mapping, an exception to "no writable Host output mapping" (CLAUDE.md), so:
- one new, empty folder per session, under a root that is not a user folder or OneDrive and where
  nothing runs on the Host: every file created there inherits a deny-execute ACE (tools/setup_inbox.ps1
  on the codex-work folder). make_share() proves it by trying to run a harmless copy of whoami.exe.
  Probe 2026-10-15: the Guest cannot remove that ACE (its icacls changes do not reach the Host) and
  cannot create a junction or symlink in the mapped folder ("Access is denied")
- the deny also applies inside the Guest, so the Guest works in its own Desktop\\Shared (runnable there)
  and config.SHARED_MIRROR_PS1 copies it into C:\\Shared; the Host side never runs anything from it
- every second the Host deletes any link or hard-linked file that appears anyway (sweep_share)
- at session end nothing is trusted as it is: links are removed, the size is checked, Microsoft
  Defender scans the folder, the person chooses keep / delete / hold (finish_shared(decide=)), and only a
  clean folder they keep is COPIED (new files, Downloads' own permissions,
  marked as from the internet so Windows asks before running them) to Downloads\\SandboxShared\\<session>;
  then the share is deleted. A threat, a scan error or Defender missing leaves the files where they are,
  still not runnable (QUARANTINED / BLOCKED, like an Artifact)
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import config
from .errors import INVALID_ARGUMENT, POLICY_DENIED, SandboxManagerError
from .importer import _clear, _copy_out, _is_link, downloads_folder, sha256_file, unique_path
from .inbox import check_inbox_root

DEST_FOLDER = "SandboxShared"
MPCMDRUN = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"
SCAN_TIMEOUT_S = 600

log = logging.getLogger("sandbox-shared")


def make_share(root, session_id: str) -> Path:
    """Create root\\<session_id> (new, empty) and prove nothing in it can run on the Host."""
    root = check_inbox_root(root)                      # same rules as the inbox: no user folder, no OneDrive
    if not session_id or not all(c.isalnum() or c in "-_" for c in session_id):
        raise SandboxManagerError(INVALID_ARGUMENT, f"bad session id {session_id!r}")
    share = root / session_id
    try:
        share.mkdir()
    except FileExistsError:
        raise SandboxManagerError(INVALID_ARGUMENT, f"shared folder already exists: {share}") from None
    try:
        check_no_execute(share)
    except SandboxManagerError:
        shutil.rmtree(share, ignore_errors=True)
        raise
    return share


def check_no_execute(folder: Path) -> None:
    """A copy of whoami.exe placed in folder must refuse to start on the Host."""
    if sys.platform != "win32":
        return
    probe = folder / "share_probe_whoami.exe"
    shutil.copyfile(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe", probe)
    try:
        subprocess.run([str(probe)], capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ran = True
    except OSError:
        ran = False
    finally:
        probe.unlink(missing_ok=True)
    if ran:
        raise SandboxManagerError(POLICY_DENIED, f"programs still run from {folder} on the Host; run "
                                  "tools/setup_inbox.ps1 on its parent folder first (deny-execute ACE)")


def sweep_share(share) -> list[tuple[str, dict]]:
    """Delete every link and hard-linked file inside (the link itself, never its target). Returns events."""
    events: list[tuple[str, dict]] = []
    share = Path(share)

    def walk(folder: Path, prefix: str) -> None:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for e in entries:
            rel = prefix + e.name
            try:
                st = os.lstat(e.path)
            except OSError:
                continue
            if _is_link(Path(e.path), st):
                kind = "SHARED_LINK_REMOVED"
            elif e.is_dir(follow_symlinks=False):
                walk(Path(e.path), rel + "\\")
                continue
            elif st.st_nlink > 1:
                kind = "SHARED_HARDLINK_REMOVED"
            else:
                continue
            try:
                if os.path.isdir(e.path) and not os.path.islink(e.path):
                    os.rmdir(e.path)                   # a junction: removes the junction only
                else:
                    os.unlink(e.path)
                events.append((kind, {"name": rel}))
            except OSError as exc:
                events.append((kind + "_FAILED", {"name": rel, "error": str(exc)}))

    walk(share, "")
    return events


def list_files(share: Path) -> list[dict]:
    out = []
    for folder, dirs, files in os.walk(share):
        for name in files:
            p = Path(folder) / name
            st = os.lstat(p)
            if _is_link(p, st) or st.st_nlink > 1:
                continue
            out.append({"name": str(p.relative_to(share)), "size": st.st_size})
    return sorted(out, key=lambda f: f["name"].lower())


def defender_scan(folder: Path) -> tuple[str, str]:
    """("clean" | "threat" | "error", detail). Nothing is removed by Defender here (-DisableRemediation)."""
    if not MPCMDRUN.is_file():
        return "error", "Microsoft Defender (MpCmdRun.exe) not found"
    try:
        r = subprocess.run([str(MPCMDRUN), "-Scan", "-ScanType", "3", "-File", str(folder), "-DisableRemediation"],
                           capture_output=True, text=True, errors="replace", timeout=SCAN_TIMEOUT_S,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error", f"scan did not finish: {exc}"
    tail = " ".join((r.stdout or "").split())[-300:]
    if r.returncode == 0:
        return "clean", tail
    if r.returncode == 2:
        return "threat", tail
    return "error", f"MpCmdRun exit {r.returncode}: {tail}"


KEEP, DELETE, HOLD = "keep", "delete", "hold"


def finish_shared(share, dest=None, scan=defender_scan, decide=None) -> dict:
    """Session end. Only a clean folder is offered; decide(result) -> KEEP | DELETE | HOLD says what then
    (배주한 결정 2026-10-15: the Host asks the person; no answer = HOLD). Without decide: KEEP.

    KEEP   -> copied to dest\\<session>, the share deleted              status EXPORTED
    DELETE -> the share deleted                                         status DELETED
    HOLD   -> left as it is, where nothing runs (the person decides later) status HELD
    Not clean: left in place, status BLOCKED or QUARANTINED. Nothing in it: removed, status EMPTY.
    Returns {"status", "share", "dest", "files", "scan", "events"}.
    """
    share = Path(share)
    if not share.is_dir():
        return {"status": "EMPTY", "share": str(share), "dest": None, "files": [], "scan": None, "events": []}
    events = sweep_share(share)
    files = list_files(share)
    result = {"share": str(share), "dest": None, "files": files, "scan": None,
              "events": [{"kind": k, **f} for k, f in events]}
    if not files:
        _remove(share)
        return result | {"status": "EMPTY"}
    total = sum(f["size"] for f in files)
    if len(files) > config.SHARED_FILES_MAX or total > config.SHARED_TOTAL_MAX:
        return result | {"status": "BLOCKED", "scan": f"too big: {len(files)} files, {total} bytes "
                         f"(limit {config.SHARED_FILES_MAX} files, {config.SHARED_TOTAL_MAX} bytes)"}
    verdict, detail = scan(share)
    result["scan"] = f"{verdict}: {detail}".strip()
    if verdict == "threat":
        return result | {"status": "BLOCKED"}
    if verdict != "clean":
        return result | {"status": "QUARANTINED"}
    for f in files:
        f["sha256"] = sha256_file(share / f["name"])
    try:
        choice = decide(result | {"status": "CLEAN"}) if decide is not None else KEEP
    except Exception:  # noqa: BLE001 - no clear answer: nothing is copied or deleted
        choice = HOLD
    if choice == DELETE:
        _remove(share)
        return result | {"status": "DELETED"}
    if choice != KEEP:
        return result | {"status": "HELD"}
    base = Path(dest) if dest is not None else downloads_folder() / DEST_FOLDER
    base.mkdir(parents=True, exist_ok=True)
    target = unique_path(base, share.name)
    _copy_out(share, target)                           # new files: no deny ACE, marked as from the internet
    _remove(share)
    return result | {"status": "EXPORTED", "dest": str(target)}


def _remove(share: Path) -> None:
    """Links are removed, never followed; read-only files too."""
    try:
        _clear(share)
        share.rmdir()
    except OSError:
        pass                                           # left behind, still not runnable; reported by the caller's next look


class SharedWatcher(threading.Thread):
    """Calls manager.sweep_shared(s) every `interval` seconds until the session is over."""

    def __init__(self, manager, session, interval: float = 1.0):
        super().__init__(name=f"shared-{session.session_id}", daemon=True)
        self.manager, self.session, self.interval = manager, session, interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        from .manager import FAILED, TERMINATED
        while not self._stop_event.wait(self.interval):
            if self.session.state in (TERMINATED, FAILED):
                return
            try:
                self.manager.sweep_shared(self.session)
            except Exception as exc:  # noqa: BLE001 - keep watching; the next sweep may succeed
                log.warning("shared sweep failed for %s: %s", self.session.session_id, exc)

    def stop(self) -> None:
        self._stop_event.set()
