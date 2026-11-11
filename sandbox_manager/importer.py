"""Copying a file the user already downloaded into the shared inbox (배주한 결정 2026-10-10).

    import_to_inbox("ZoomIt.zip", inbox, extract=True)     # from the user's Downloads folder
    # -> inbox\\ZoomIt\\... ; inbox.InboxWatcher lists it, the Guest copies it to Desktop\\Input

The caller (today the Agent through a Host MCP tool, later maybe the Host itself) names a file, not a
path: only a plain file directly in one source folder (by default the user's Downloads) can be taken.
That keeps an Agent that was tricked by a web page from handing other Host files (Documents, keys,
browser profiles) to the Sandbox, which has internet access.

What is checked here, whoever calls it:
- the name: no folder parts, no drive or stream (":"), no "..", no device names (CON, NUL, ...),
  no trailing dot or space (Windows would quietly open another name)
- the file: not a link or reparse point, not hard-linked (another name of some other Host file),
  finished (not a download temp name, nobody writing), within config.INPUT_FILE_MAX
- the bytes copied are the bytes hashed: copied once to a private staging folder while hashing, and
  refused if the source changed meanwhile; only then placed in the inbox under a new name
The original stays where it is (copy, not move). Nothing is ever run on the Host.

At the end of a session (배주한 결정 2026-10-11: default delete, keep = back to Downloads):

    finish_inbox(inbox, keep=["ZoomIt"])    # copy what the user wants to keep to Downloads, empty the inbox
    clear_inbox(inbox)                      # the default: empty the inbox, nothing kept

A kept file is copied (not moved: a moved file would carry the inbox's deny-execute ACL along) into
Downloads under a new name, marked as downloaded from the internet (Zone.Identifier, so Windows asks
before running it on the Host), and only then is the inbox emptied. If anything named cannot be kept,
nothing is deleted.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path, PureWindowsPath

from . import config
from .errors import INVALID_ARGUMENT, POLICY_DENIED, SandboxManagerError
from .inbox import check_inbox_root, no_writer
from .inputs import check_file

NAME_MAX = 255
_BAD_CHARS = set('\\/:*?"<>|')
_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


def downloads_folder() -> Path:
    """The user's Downloads folder, also when it was moved (Known Folder), else ~/Downloads."""
    if sys.platform == "win32":
        import ctypes
        import uuid
        from ctypes import wintypes
        guid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(FOLDERID_DOWNLOADS).bytes_le)
        out = ctypes.c_wchar_p()
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                                 ctypes.POINTER(ctypes.c_wchar_p)]
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            try:
                return Path(out.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(out)
    return Path.home() / "Downloads"


def check_name(name) -> str:
    if not isinstance(name, str) or not 1 <= len(name) <= NAME_MAX or not name.isprintable():
        raise SandboxManagerError(INVALID_ARGUMENT, "name must be a printable file name of 1-255 characters")
    if _BAD_CHARS & set(name) or name in (".", ".."):
        raise SandboxManagerError(POLICY_DENIED, f"only a file name is allowed, not a path: {name!r}")
    if name != name.strip(" ") or name.endswith("."):
        raise SandboxManagerError(INVALID_ARGUMENT, f"file name must not start or end with a space or end with a dot: {name!r}")
    if name.split(".")[0].upper() in _DEVICES:
        raise SandboxManagerError(POLICY_DENIED, f"device name: {name!r}")
    return name


def check_source_folder(path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        raise SandboxManagerError(INVALID_ARGUMENT, f"source folder must be an absolute path: {p}")
    try:
        st = os.lstat(p)
    except OSError:
        raise SandboxManagerError(INVALID_ARGUMENT, f"source folder does not exist: {p}") from None
    if os.path.islink(p) or getattr(st, "st_file_attributes", 0) & config.FILE_ATTRIBUTE_REPARSE_POINT \
            or not p.is_dir():
        raise SandboxManagerError(INVALID_ARGUMENT, f"source folder must be a plain folder: {p}")
    return p


def safe_name(name: str, fallback: str = "download.bin") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or fallback
    return name[:100]


def unique_path(folder: Path, name: str) -> Path:
    """folder/name, or name-2, name-3, ... : an existing file is never overwritten."""
    p = folder / name
    stem, suffix, n = p.stem, p.suffix, 1
    while os.path.lexists(p):
        n += 1
        p = folder / f"{stem}-{n}{suffix}"
    return p


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_zip(archive: Path, dest: Path) -> list[Path]:
    """Unpack into dest: no absolute paths, "..", drive or stream parts, links; count and size limits."""
    out = []
    try:
        z = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise SandboxManagerError(INVALID_ARGUMENT, f"not a valid zip file: {archive.name}") from None
    with z:
        members = [m for m in z.infolist() if not m.is_dir()]
        if len(members) > config.INBOX_FILES_MAX:
            raise SandboxManagerError(INVALID_ARGUMENT, f"zip has more than {config.INBOX_FILES_MAX} files")
        if sum(m.file_size for m in members) > config.INPUT_TOTAL_MAX:
            raise SandboxManagerError(INVALID_ARGUMENT, "zip contents are too large")
        for m in members:
            parts = PureWindowsPath(m.filename.replace("/", "\\")).parts
            if not parts or PureWindowsPath(m.filename).is_absolute() or any(p in ("..", "") or ":" in p for p in parts):
                raise SandboxManagerError(POLICY_DENIED, f"unsafe path in zip: {m.filename!r}")
            if (m.external_attr >> 16) & 0o170000 == 0o120000:
                raise SandboxManagerError(POLICY_DENIED, f"link in zip: {m.filename!r}")
            target = dest.joinpath(*[safe_name(p) for p in parts])
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(target)
    return out


def _stage(src: Path, staging: Path) -> tuple[Path, str]:
    """Copy src into staging while hashing it; refuse if src changed meanwhile."""
    before = check_file(src)
    if before.st_nlink > 1:
        raise SandboxManagerError(POLICY_DENIED, f"{src.name!r} is hard-linked to another file")
    if not no_writer(str(src)):
        raise SandboxManagerError(INVALID_ARGUMENT, f"{src.name!r} is still being written")
    out = staging / safe_name(src.name)
    h, total = hashlib.sha256(), 0
    with src.open("rb") as f, out.open("wb") as g:
        while chunk := f.read(1 << 20):
            total += len(chunk)
            if total > config.INPUT_FILE_MAX:
                raise SandboxManagerError(INVALID_ARGUMENT, f"{src.name!r} is over {config.INPUT_FILE_MAX} bytes")
            h.update(chunk)
            g.write(chunk)
    after = check_file(src)
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns) or total != before.st_size:
        raise SandboxManagerError(INVALID_ARGUMENT, f"{src.name!r} changed while it was copied; try again")
    return out, h.hexdigest()


def import_to_inbox(name: str, inbox, source_dir=None, extract: bool = False) -> dict:
    """Copy one finished file from source_dir (default: Downloads) into the inbox; a .zip may be unpacked."""
    inbox = check_inbox_root(inbox)
    source = check_source_folder(source_dir if source_dir is not None else downloads_folder())
    src = source / check_name(name)
    if not os.path.lexists(src):
        raise SandboxManagerError(INVALID_ARGUMENT, f"no file named {name!r} in {source}")
    with tempfile.TemporaryDirectory(prefix="securecua-import-") as tmp:
        staged, digest = _stage(src, Path(tmp))
        if extract:
            if staged.suffix.lower() != ".zip":
                raise SandboxManagerError(INVALID_ARGUMENT, "extract=true needs a .zip file")
            unpacked = Path(tmp) / "unpacked"
            files = extract_zip(staged, unpacked)
            root = unique_path(inbox, staged.stem)
            placed = []
            for f in files:
                dst = root / f.relative_to(unpacked)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dst)
                placed.append(dst)
        else:
            dst = unique_path(inbox, staged.name)
            shutil.copyfile(staged, dst)
            placed = [dst]
        files = [{"name": str(p.relative_to(inbox)), "size": p.stat().st_size, "sha256": sha256_file(p)}
                 for p in placed]
    return {"source": str(src), "source_sha256": digest, "files": files,
            "sandbox_paths": [f"Desktop\\Input\\{f['name']}" for f in files]}


# ---- end of a session: keep (to Downloads) or delete ------------------------------------------------

KEEP_MAX = 20
ZONE_INTERNET = "[ZoneTransfer]\r\nZoneId=3\r\n"


def _is_link(path: Path, st: os.stat_result) -> bool:
    return os.path.islink(path) or bool(getattr(st, "st_file_attributes", 0) & config.FILE_ATTRIBUTE_REPARSE_POINT)


def _keep_target(inbox: Path, name) -> Path:
    """inbox-relative name (as the tools reported it, e.g. "ZoomIt\\ZoomIt64.exe") -> a plain path inside."""
    if not isinstance(name, str) or not name:
        raise SandboxManagerError(INVALID_ARGUMENT, "keep names must be non-empty strings")
    parts = re.split(r"[\\/]", name)
    if PureWindowsPath(name).is_absolute() or PureWindowsPath(name).drive:
        raise SandboxManagerError(POLICY_DENIED, f"only names inside the inbox: {name!r}")
    p = inbox
    for part in parts:
        check_name(part)
        p = p / part
        try:
            st = os.lstat(p)
        except OSError:
            raise SandboxManagerError(INVALID_ARGUMENT, f"not in the inbox: {name!r}") from None
        if _is_link(p, st):
            raise SandboxManagerError(POLICY_DENIED, f"link in the inbox: {name!r}")
    if p.is_file() and os.lstat(p).st_nlink > 1:
        raise SandboxManagerError(POLICY_DENIED, f"hard-linked file in the inbox: {name!r}")
    return p


def _mark_internet(path: Path) -> None:
    """Mark of the Web: Windows (SmartScreen) asks before the Host runs it."""
    try:
        with open(f"{path}:Zone.Identifier", "w", encoding="ascii", newline="") as f:
            f.write(ZONE_INTERNET)
    except OSError:
        pass                                           # not NTFS: nothing to mark


def _copy_out(src: Path, dst: Path) -> list[Path]:
    """Copy a file or folder tree out of the inbox without following links; returns the files written."""
    if src.is_file():
        shutil.copyfile(src, dst)                      # new file: Downloads' own permissions, not the inbox's
        _mark_internet(dst)
        return [dst]
    dst.mkdir()
    out = []
    for e in os.scandir(src):
        st = os.lstat(e.path)
        if _is_link(Path(e.path), st) or (not e.is_dir(follow_symlinks=False) and st.st_nlink > 1):
            continue                                   # links never leave the inbox
        out += _copy_out(Path(e.path), dst / e.name)
    return out


def clear_inbox(inbox) -> int:
    """Empty the inbox (the folder itself stays). Links are removed, never followed. Returns files removed."""
    inbox = check_inbox_root(inbox)
    return _clear(inbox)


def _clear(folder: Path) -> int:
    n = 0
    for e in os.scandir(folder):
        path = Path(e.path)
        st = os.lstat(path)
        if _is_link(path, st):
            os.rmdir(path) if e.is_dir() and not os.path.islink(path) else os.unlink(path)
        elif e.is_dir(follow_symlinks=False):
            n += _clear(path)
            os.rmdir(path)
        else:
            if not st.st_mode & 0o200:
                os.chmod(path, 0o666)                  # read-only attribute
            os.unlink(path)
            n += 1
    return n


def finish_inbox(inbox, keep=(), dest=None) -> dict:
    """Session end: copy each kept item to Downloads (marked as from the internet), then empty the inbox."""
    inbox = check_inbox_root(inbox)
    keep = list(keep or [])
    if len(keep) > KEEP_MAX:
        raise SandboxManagerError(INVALID_ARGUMENT, f"at most {KEEP_MAX} items can be kept")
    targets = [(name, _keep_target(inbox, name)) for name in keep]       # all checked before anything moves
    kept = []
    if targets:
        out_dir = check_source_folder(dest if dest is not None else downloads_folder())
        for name, src in targets:
            if src.is_file():
                same = out_dir / src.name
                if same.is_file() and same.stat().st_size == src.stat().st_size \
                        and sha256_file(same) == sha256_file(src):
                    kept.append({"name": name, "to": str(same), "files": 1, "already_there": True})
                    continue
            dst = unique_path(out_dir, src.name)
            files = _copy_out(src, dst)
            kept.append({"name": name, "to": str(dst), "files": len(files), "already_there": False})
    deleted = _clear(inbox)
    return {"kept": kept, "deleted_files": deleted}
