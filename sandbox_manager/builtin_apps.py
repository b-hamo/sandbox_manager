"""Session copies of Windows apps absent from the micro_VM base image.

Like minoBox --app notepad, copy only the classic executable and its MUI
resources. Never launch on the Host or expose the Windows directory as a share.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import re
import shutil
from pathlib import Path

from .errors import RUNTIME_UNAVAILABLE, SandboxManagerError


def system_directory() -> Path:
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise SandboxManagerError(RUNTIME_UNAVAILABLE, "cannot locate Windows system directory for builtin apps")
    return Path(buffer.value)


def stage_notepad(package: Path, *, source: Path | None = None) -> list[dict]:
    """Prepare before publishing this session's read-only Runner package."""
    source = source if source is not None else system_directory()
    destination = package / "builtin-apps" / "notepad"
    try:
        files = [source / "notepad.exe"]
        if not files[0].is_file():
            raise OSError("classic Windows Notepad is not installed on this Host")
        for directory in sorted(source.iterdir()):
            if re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", directory.name):
                resource = directory / "notepad.exe.mui"
                if resource.is_file():
                    files.append(resource)
        # A failed/uncertain publication is not silently repeated over old files.
        destination.mkdir(parents=True, exist_ok=False)
        manifest = []
        for original in files:
            relative = original.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            expected = hashlib.sha256(original.read_bytes()).hexdigest()
            shutil.copyfile(original, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise OSError(f"builtin app copy hash mismatch: {relative}")
            manifest.append({"path": relative.as_posix(), "sha256": expected})
        (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest
    except OSError as exc:
        raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"builtin app preparation failed (notepad): {exc}") from exc


# Executed only inside the Guest, after the checked package share is attached.
# A preparation error prevents Runner startup; it is not an input rejection.
VERIFY_NOTEPAD_PS1 = r"""
$appRoot = 'C:\RunnerPackage\builtin-apps\notepad'
$appFiles = Get-Content -LiteralPath "$appRoot\manifest.json" -Raw | ConvertFrom-Json
if (-not ($appFiles | Where-Object { $_.path -eq 'notepad.exe' })) { throw 'Notepad manifest has no executable' }
foreach ($appFile in $appFiles) {
    if ($appFile.path -notmatch '^(notepad\.exe|[a-z]{2,3}(-[A-Za-z0-9]{2,8})*/notepad\.exe\.mui)$') { throw 'Invalid Notepad resource path' }
    $actual = (Get-FileHash -LiteralPath (Join-Path $appRoot $appFile.path) -Algorithm SHA256).Hash
    if ($actual -ne $appFile.sha256) { throw 'Notepad Guest copy hash mismatch' }
}
"""
