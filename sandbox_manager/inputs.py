"""Registering a Host file for the Sandbox (반입), before any session exists.

    f = register_input(downloads / "ZoomIt.exe", source="https://download.sysinternals.com/files/ZoomIt.zip")
    # f.size, f.sha256 recorded now; the Host shows them in its approval prompt
    s = sm.prepare(..., input_files=[f])
    # prepare copies the file into the session workspace and refuses it if the bytes are no longer
    # the registered ones (changed or replaced after registration)

Only a finished, plain file is registered: no link or reparse point, no folder, no half-downloaded
file (browser temp names), within the size limit. The file is never opened for anything but reading,
and never run on the Host. Whether it may go into the Sandbox at all is the Host's decision
(approval); this module only records exactly which bytes were approved.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from . import config
from .errors import INVALID_ARGUMENT, SandboxManagerError

# Names browsers and download tools give a file that is still being written.
INCOMPLETE_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp", ".!ut")
SOURCE_MAX = 2048


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class InputFile:
    path: Path
    size: int
    sha256: str
    source: str | None = None        # where the Host got it (e.g. download URL); recorded, never fetched
    registered_at: str = ""


def check_file(p: Path) -> os.stat_result:
    """What may be handed to the Sandbox at all. Raises INVALID_ARGUMENT."""
    try:
        st = os.lstat(p)
    except OSError:
        raise SandboxManagerError(INVALID_ARGUMENT, f"input file not found: {p}") from None
    if p.is_symlink() or getattr(st, "st_file_attributes", 0) & config.FILE_ATTRIBUTE_REPARSE_POINT:
        raise SandboxManagerError(INVALID_ARGUMENT, f"input file is a link: {p}")
    if not p.is_file():
        raise SandboxManagerError(INVALID_ARGUMENT, f"input must be a regular file, not a folder: {p}")
    if p.name.lower().endswith(INCOMPLETE_SUFFIXES):
        raise SandboxManagerError(INVALID_ARGUMENT, f"input file is still downloading: {p.name}")
    if st.st_size > config.INPUT_FILE_MAX:
        raise SandboxManagerError(INVALID_ARGUMENT,
                                  f"input file {p.name!r} is {st.st_size} bytes (limit {config.INPUT_FILE_MAX})")
    return st


def _check_source(source: str | None) -> str | None:
    if source is None:
        return None
    if not isinstance(source, str) or len(source) > SOURCE_MAX or not source.isprintable():
        raise SandboxManagerError(INVALID_ARGUMENT, "source must be a printable string of at most 2048 characters")
    return source


def register_input(path, source: str | None = None) -> InputFile:
    """Record a finished Host file: size and SHA-256 now. The file must not change while it is hashed."""
    p = Path(path)
    before = check_file(p)
    digest = sha256_of(p)
    after = check_file(p)
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise SandboxManagerError(INVALID_ARGUMENT, f"input file {p.name!r} changed while it was registered")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return InputFile(p, before.st_size, digest, _check_source(source), now)
