"""Prepare declared task dependencies in the bound Guest before workload input.

No workload is launched here. Copies live in the session's existing read-only
package mapping; missing installers are handed back to the inbox/Guest flow.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import uuid
import zipfile
from pathlib import Path

from . import config
from .builtin_apps import stage_notepad
from .errors import SandboxManagerError
from .programs import PROGRAMS, FoundProgram, check_program_root, find_program

ALIASES = {"메모장": "notepad", "notepad.exe": "notepad", "python3": "python",
           "python.exe": "python", "node.exe": "node", "powershell.exe": "powershell", "cmd.exe": "cmd"}
SYSTEM_APPS = {"notepad": r"C:\Windows\System32\notepad.exe",
               "cmd": r"C:\Windows\System32\cmd.exe",
               "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"}
MAX_BYTES = 512 * 1024 * 1024


def installed_candidates(name):
    candidates = []
    if name == "python":
        candidates.append(str(Path(sys.base_prefix) / "python.exe"))
    if sys.platform == "win32":
        import winreg
        key = r"Software\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + PROGRAMS[name].exe
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as registered:
                        value, kind = winreg.QueryValueEx(registered, "")
                        if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                            candidates.append(os.path.expandvars(value).strip('"'))
                except FileNotFoundError:
                    pass
    return candidates


def quote(text):
    return "'" + text.replace("'", "''") + "'"


def probe(path):
    return f"$ErrorActionPreference='Stop'; if(Test-Path -LiteralPath {quote(path)} -PathType Leaf){{exit 0}}; exit 3"


def copy_program(source: Path, target: Path):
    """Copy bytes, not links, with limits and a manifest for Guest verification."""
    target.mkdir(parents=True, exist_ok=False)
    rows, total = [], 0
    for directory, dirs, files in os.walk(source):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction():
                raise OSError("program changed to contain a link")
        for name in files:
            path = Path(directory) / name
            before = path.stat()
            total += before.st_size
            if len(rows) >= 60000 or total > MAX_BYTES or before.st_size > 128 * 1024 * 1024:
                raise OSError("application copy exceeds session package limits")
            relative = path.relative_to(source)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            after = path.stat()
            if ((before.st_size, before.st_mtime_ns, before.st_ino) !=
                    (after.st_size, after.st_mtime_ns, after.st_ino) or
                    hashlib.sha256(destination.read_bytes()).hexdigest() != expected):
                raise OSError("application changed while copying")
            rows.append({"path": relative.as_posix(), "sha256": expected})
    config.check_contents(target)
    (target / "app-files.json").write_text(json.dumps(rows), encoding="utf-8")
    return rows


def verify_and_link(source, target, executable, manifest="app-files.json"):
    # JSON array assignment deliberately avoids @(... ConvertFrom-Json): PS 5.1
    # otherwise hands the entire manifest to foreach as a single entry.
    return f"""$ErrorActionPreference='Stop'
$source={quote(source)}; $target={quote(target)}
$rows=Get-Content -LiteralPath (Join-Path $source {quote(manifest)}) -Raw | ConvertFrom-Json
if(-not $rows){{throw 'Empty app manifest'}}
foreach($row in $rows){{
 $relative=[string]$row.path
 if([IO.Path]::IsPathRooted($relative) -or $relative.Contains(':') -or ($relative -split '[\\/]' | Where-Object {{$_ -eq '..' -or $_ -eq '.'}})){{throw 'Invalid app manifest path'}}
 if((Get-FileHash -LiteralPath (Join-Path $source $relative) -Algorithm SHA256).Hash -ne $row.sha256){{throw 'App hash mismatch'}}
}}
if(-not (Test-Path -LiteralPath {quote(executable)} -PathType Leaf)){{
 if(Test-Path -LiteralPath $target){{throw 'Guest application target already exists'}}
 New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
 New-Item -ItemType SymbolicLink -Path $target -Target $source | Out-Null
}}
if(-not (Test-Path -LiteralPath {quote(executable)} -PathType Leaf)){{throw 'Guest executable unavailable'}}
exit 0
"""


def bundle_program(source, staging, archive):
    """Build outside the watched share, then publish one immutable archive."""
    copy_program(source, staging)
    temporary = staging.parent / (staging.name + ".zip")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                package.write(path, path.relative_to(staging).as_posix())
    if temporary.stat().st_size > 128 * 1024 * 1024:
        raise OSError("application archive exceeds management transfer limit")
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        raise OSError("application archive already exists; publication not replayed")
    temporary.replace(archive)
    return digest


def unpack_and_link(archive, digest, source, target, executable):
    # Validate entry destinations before any extraction. Only this request's
    # fresh Guest directory is used; no existing user files are overwritten.
    return f"""$ErrorActionPreference='Stop'
$archive={quote(archive)}; $source={quote(source)}
if((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne {quote(digest)}){{throw 'Application archive hash mismatch'}}
if(Test-Path -LiteralPath $source){{throw 'Application extraction directory already exists'}}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip=[IO.Compression.ZipFile]::OpenRead($archive)
try{{
 $size=0L; $count=0
 foreach($entry in $zip.Entries){{
  $count++; $size+=$entry.Length
  if($count -gt 60001 -or $size -gt {MAX_BYTES + 16777216}){{throw 'Application archive exceeds limits'}}
  if($entry.FullName.Contains(':')){{throw 'Invalid application archive entry'}}
  $resolved=[IO.Path]::GetFullPath((Join-Path $source $entry.FullName))
  if(-not $resolved.StartsWith($source+'\',[StringComparison]::OrdinalIgnoreCase)){{throw 'Application archive path escapes destination'}}
 }}
}}finally{{$zip.Dispose()}}
[IO.Compression.ZipFile]::ExtractToDirectory($archive,$source)
""" + verify_and_link(source, target, executable)


def prepare_one(manager, session, requested):
    name = ALIASES.get(requested.strip().lower(), requested.strip().lower())
    if name not in SYSTEM_APPS and name not in PROGRAMS:
        return {"app": name, "status": "UNSUPPORTED", "prepared": False,
                "next_step": "use_official_installer_via_inbox_in_same_guest"}
    previous = session.app_preparations.get(name)
    if previous:
        # Pending intent after a crash is uncertain, not permission to replay.
        if previous["status"] == "PREPARING":
            return dict(previous, status="UNKNOWN", prepared=False, next_step="inspect_preparation_no_replay")
        if previous["status"] != "NEEDS_INSTALL":
            if previous.get("prepared") and manager._app_guest_exit(session, probe(previous["exe"])) != 0:
                return dict(previous, status="FAILED", prepared=False, detail="prepared Guest executable disappeared")
            if previous.get("prepared") and previous.get("generation") != session.generation:
                previous["generation"] = session.generation
                manager._event(session, "APP_REVALIDATED", app=name, generation=session.generation)
            return previous
    row = {"app": name, "status": "PREPARING", "prepared": False}
    session.app_preparations[name] = row
    manager._event(session, "APP_PREPARING", app=name)
    try:
        existing = (FoundProgram(name, Path(name), PROGRAMS[name].marker).for_agent()
                    if name in PROGRAMS and not PROGRAMS[name].keep_name else None)
        candidate = SYSTEM_APPS.get(name) or (existing["exe"] if existing else None)
        if candidate:
            code = manager._app_guest_exit(session, probe(candidate))
            if code == 0:
                row.update(exe=candidate, source="guest", path=[], env={}, cmd_setup="", cmd_setup_all="")
                if existing:
                    row.update(existing, source="guest", cmd_setup_all=existing["cmd_setup"])
            elif code != 3:
                raise OSError(f"Guest app probe failed: exit {code}")
        if "exe" not in row:
            if name in ("cmd", "powershell"):
                raise OSError("required Guest system application is unavailable")
            if name == "notepad":
                root = session.package_dir / "builtin-apps" / "notepad"
                if not root.exists():
                    stage_notepad(session.package_dir)
                guest_source = r"C:\RunnerPackage\builtin-apps\notepad"
                target = r"C:\Tools\notepad"
                answer = {"name": name, "guest_root": target, "exe": target + r"\notepad.exe",
                          "path": [target], "env": {}, "cmd_setup": f'set "PATH={target};%PATH%"'}
                manifest = "manifest.json"
                script = verify_and_link(guest_source, target, answer["exe"], manifest)
            else:
                try:
                    found = find_program(name, check=check_program_root, candidates=installed_candidates(name))
                except SandboxManagerError as exc:
                    if "is not installed on this PC" not in exc.message:
                        raise
                    row.update(status="NEEDS_INSTALL", detail=exc.message,
                               next_step="use_official_installer_via_inbox_in_same_guest")
                    manager._event(session, "APP_NEEDS_INSTALL", app=name)
                    return row
                answer = found.for_agent()
                staging = session.dir / "app-staging" / name
                archive = session.package_dir / "prepared-apps" / (name + ".zip")
                digest = bundle_program(found.root, staging, archive)
                if not staging.resolve().is_relative_to(session.dir.resolve() / "app-staging"):
                    raise OSError("application staging escaped session directory")
                shutil.rmtree(staging)
                guest_archive = "C:\\RunnerPackage\\prepared-apps\\" + name + ".zip"
                guest_source = "C:\\RunnerApps\\" + name + "-" + uuid.uuid4().hex
                script = unpack_and_link(guest_archive, digest, guest_source, answer["guest_root"], answer["exe"])
            manager._sync_app_package(session)
            code = manager._app_guest_exit(session, script)
            if code != 0:
                raise OSError(f"Guest application preparation failed: exit {code}")
            row.update(answer, source="host_session_copy", host_read_only=True, read_only=name == "notepad")
            row["cmd_setup_all"] = row["cmd_setup"]
        row.update(status="PREPARED", prepared=True, proof="guest_file_verified", workload_started=False,
                   session_id=session.session_id, runtime_id=session.runtime_id, generation=session.generation)
        manager._event(session, "APP_PREPARED", app=name, exe=row["exe"], source=row["source"])
        return row
    except Exception as exc:
        # A lost Guest command response may have created the link. Keep intent
        # and never repeat it automatically under another request ID.
        row.update(status="UNKNOWN" if isinstance(exc, SandboxManagerError) else "FAILED",
                   detail=str(exc), next_step="inspect_preparation_no_replay")
        manager._event(session, "APP_PREPARATION_FAILED", app=name, detail=str(exc))
        return row


def prepare_apps(manager, session, apps):
    rows = [prepare_one(manager, session, name) for name in dict.fromkeys(apps)]
    return {"prepared": all(row.get("prepared") for row in rows), "apps": rows,
            "session_id": session.session_id, "runtime_id": session.runtime_id,
            "generation": session.generation, "workload_started": False}
