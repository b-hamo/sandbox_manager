"""Shared inbox: one fixed Host folder the Agent downloads into, mapped read-only into the Sandbox.

    sm.prepare(..., inbox=r"C:\\Users\\me\\SecureCUA\\codex-work\\inbox")   # 배주한 결정 2026-10-08
    watcher = InboxWatcher(sm, s); watcher.start()                        # Host side, every second
    # the Guest (config.INPUT_WATCH_PS1) copies each listed file to Desktop\\Input and checks its SHA-256

Unlike prepare(input_files=...), the folder itself is mapped, so the Agent's downloads appear in the
running Sandbox without a copy step. What keeps that from opening the Host:
- the folder is read-only in the Guest, and nothing in it may run on the Host (deny-execute ACE,
  tools/setup_inbox.ps1); it must not be a user folder, OneDrive, or hold the session workspace
- every scan deletes any junction, symlink or other reparse point, and any hard-linked file, found
  anywhere in it (a link would show the Guest some other Host file); the start refuses one outright
- only finished files are listed in the manifest: not a download temp name, nobody writing to it,
  size and time unchanged for STABLE_S, within the size limits. Their SHA-256 is taken then; a file
  that changes later is listed again with its new hash, and the change is recorded
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
from pathlib import Path

from . import config
from .errors import INVALID_ARGUMENT, SandboxManagerError
from .inputs import INCOMPLETE_SUFFIXES, sha256_of

STABLE_S = 2.0
MAX_DEPTH = 4
# Never an inbox, nor inside one of these (CLAUDE.md: no user folders, browser profiles, OneDrive).
USER_FOLDERS = ("Documents", "Desktop", "Downloads", "Pictures", "Videos", "Music", "Favorites", "Contacts",
                "AppData", "OneDrive", ".ssh", ".codex", ".claude", ".config")

log = logging.getLogger("sandbox-inbox")


def _is_link(path: str, st: os.stat_result) -> bool:
    return os.path.islink(path) or bool(getattr(st, "st_file_attributes", 0) & config.FILE_ATTRIBUTE_REPARSE_POINT)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def check_inbox_root(path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        raise SandboxManagerError(INVALID_ARGUMENT, f"inbox must be an absolute path: {p}")
    try:
        st = os.lstat(p)
    except OSError:
        raise SandboxManagerError(INVALID_ARGUMENT, f"inbox folder does not exist: {p}") from None
    if _is_link(str(p), st) or not p.is_dir():
        raise SandboxManagerError(INVALID_ARGUMENT, f"inbox must be a plain folder, not a link or file: {p}")
    p = p.resolve()
    home = Path.home().resolve()
    if p == Path(p.anchor) or _within(home, p):
        raise SandboxManagerError(INVALID_ARGUMENT, f"inbox must not be a drive root or contain the user profile: {p}")
    for name in USER_FOLDERS:
        if _within(p, home / name) or p.name.lower().startswith("onedrive"):
            raise SandboxManagerError(INVALID_ARGUMENT, f"inbox must not be inside a user folder ({name}): {p}")
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        if os.environ.get(var) and _within(p, Path(os.environ[var]).resolve()):
            raise SandboxManagerError(INVALID_ARGUMENT, f"inbox must not be inside OneDrive: {p}")
    return p


def no_writer(path: str) -> bool:
    """True when nobody has the file open for writing (a download still in progress usually has)."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 0x1, 3
    h = k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        return False                                   # sharing violation: someone is still writing
    k32.CloseHandle(h)
    return True


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class InboxScanner:
    """One scan of the inbox. Keeps what it saw last time so it can tell when a file stopped changing."""

    def __init__(self, root: Path, clock):
        self.root = Path(root)
        self.clock = clock
        self.seen: dict[str, tuple[int, int, float]] = {}     # rel -> (size, mtime_ns, unchanged since)
        self.listed: dict[str, dict] = {}                      # rel -> manifest entry (+ mtime_ns)
        self.warned: set[str] = set()

    def scan(self) -> tuple[bool, list[tuple[str, dict]]]:
        """Returns (manifest changed, events)."""
        events: list[tuple[str, dict]] = []
        files: dict[str, tuple[str, os.stat_result]] = {}
        self._walk(str(self.root), "", 0, files, events)
        changed = False
        now = self.clock()
        for rel in [r for r in self.listed if r not in files]:
            del self.listed[rel]
            changed = True
            events.append(("INBOX_FILE_GONE", {"name": rel}))
        total = sum(e["size"] for e in self.listed.values())
        for rel, (path, st) in sorted(files.items()):
            key = (st.st_size, st.st_mtime_ns)
            prev = self.seen.get(rel)
            if prev is None or prev[:2] != key:
                self.seen[rel] = (*key, now)
                continue
            listed = self.listed.get(rel)
            if listed and (listed["size"], listed["mtime_ns"]) == key:
                continue
            if now - prev[2] < STABLE_S or not no_writer(path):
                continue
            problem = None
            if st.st_size > config.INPUT_FILE_MAX:
                problem = f"{st.st_size} bytes (limit {config.INPUT_FILE_MAX})"
            elif not listed and len(self.listed) >= config.INBOX_FILES_MAX:
                problem = f"more than {config.INBOX_FILES_MAX} files"
            elif total - (listed["size"] if listed else 0) + st.st_size > config.INPUT_TOTAL_MAX:
                problem = f"inbox over {config.INPUT_TOTAL_MAX} bytes"
            if problem:
                if rel not in self.warned:
                    self.warned.add(rel)
                    events.append(("INBOX_FILE_SKIPPED", {"name": rel, "reason": problem}))
                continue
            digest = sha256_of(Path(path))
            try:
                after = os.lstat(path)
            except OSError:
                continue
            if (after.st_size, after.st_mtime_ns) != key:      # changed while hashing: next scan
                self.seen[rel] = (after.st_size, after.st_mtime_ns, now)
                continue
            entry = {"name": rel, "size": st.st_size, "sha256": digest, "from": "inbox",
                     "registered_at": _now(), "mtime_ns": st.st_mtime_ns}
            events.append(("INBOX_FILE_CHANGED" if listed else "INBOX_FILE_REGISTERED",
                           {"name": rel, "size": st.st_size, "sha256": digest}))
            total += st.st_size - (listed["size"] if listed else 0)
            self.listed[rel] = entry
            changed = True
        return changed, events

    def _walk(self, folder: str, prefix: str, depth: int, files: dict, events: list) -> None:
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
            if _is_link(e.path, st):
                self._remove(e.path, st, rel, "INBOX_LINK_REMOVED", events)
            elif e.is_dir(follow_symlinks=False):
                if depth < MAX_DEPTH:
                    self._walk(e.path, rel + "\\", depth + 1, files, events)
            elif st.st_nlink > 1:
                self._remove(e.path, st, rel, "INBOX_HARDLINK_REMOVED", events)
            elif not e.name.lower().endswith(INCOMPLETE_SUFFIXES):
                files[rel] = (e.path, st)

    @staticmethod
    def _remove(path: str, st: os.stat_result, rel: str, kind: str, events: list) -> None:
        """Remove the link itself, never what it points to."""
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                os.rmdir(path)                                 # a junction: removes the junction only
            else:
                os.unlink(path)                                # symlink or one name of a hard link
            events.append((kind, {"name": rel}))
        except OSError as exc:
            events.append((kind + "_FAILED", {"name": rel, "error": str(exc)}))


class InboxWatcher(threading.Thread):
    """Calls manager.scan_inbox(s) every `interval` seconds until the session is over."""

    def __init__(self, manager, session, interval: float = 1.0):
        super().__init__(name=f"inbox-{session.session_id}", daemon=True)
        self.manager, self.session, self.interval = manager, session, interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        from .manager import FAILED, TERMINATED
        while not self._stop_event.wait(self.interval):
            if self.session.state in (TERMINATED, FAILED):
                return
            try:
                self.manager.scan_inbox(self.session)
            except Exception as exc:  # noqa: BLE001 - keep watching; the next scan may succeed
                log.warning("inbox scan failed for %s: %s", self.session.session_id, exc)

    def stop(self) -> None:
        self._stop_event.set()
