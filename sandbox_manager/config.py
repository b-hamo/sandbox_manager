""".wsb configuration and the Guest start script.

Rules (CLAUDE.md "절대 하지 않는 것"):
- every mapped folder is read-only; there is no parameter to make one writable
- only folders inside this session's own workspace can be mapped (no user folders, browser
  profiles, Safe Results, or Host output)
- nothing inside a mapped folder may lead elsewhere on the Host: no junction, symlink or other
  reparse point, and no hard-linked file (tests/test_isolation.py)
- user files reach the Guest only as copies inside the workspace, never by mapping the user's folder
- Clipboard, printer, audio and video redirection are off
- Networking stays on for the control channel. This is NOT isolation (미결 16)
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from xml.sax.saxutils import escape

from .errors import INVALID_ARGUMENT, SandboxManagerError

GUEST_PACKAGE = r"C:\RunnerPackage"
GUEST_BOOTSTRAP = r"C:\RunnerBootstrap"
# User files the task needs, as read-only copies (prepare(input_files=...)). Mapped only when there are any.
GUEST_INPUT = r"C:\UserFiles"
# In the Guest, INPUT_WATCH_PS1 (started by the start script) copies each file listed in the manifest
# from there into Desktop\Input (writable, inside the Guest only), checks size and SHA-256 against the
# manifest before and after the copy and appends the outcome to Desktop\input-check.txt. A copy that does
# not match is deleted. Files added while the Sandbox runs (add_input) show up the same way: the mapped
# folders are live. Nothing is run: running is the Agent's GUI action through SCRP.
INPUT_MANIFEST = "inputs.json"                # in the package folder, never among the user's files
INPUT_WATCH_SCRIPT = "input_watch.ps1"
# Optional shared inbox (prepare(inbox=...)): one fixed Host folder the Agent downloads into, mapped
# read-only and live. Not a copy: the Host side (inbox.InboxWatcher) lists only finished files in the
# manifest, with their SHA-256, and deletes any link that appears in it. Files there are never run on
# the Host (the folder carries a deny-execute ACE: tools/setup_inbox.ps1).
GUEST_INBOX = r"C:\Inbox"
INBOX_FILES_MAX = 200

# Started by START_PS1 when there is a manifest; runs for the life of the Sandbox (one instance: mutex).
# The manifest is read again every second because files can be added while the Sandbox runs.
# Paths come only from the two fixed Guest folders above plus a relative name checked here.
INPUT_WATCH_PS1 = r"""$mutex = New-Object System.Threading.Mutex($false, 'Global\SecureCuaInputWatch')
if (-not $mutex.WaitOne(0)) { exit 0 }
$manifest = 'C:\RunnerPackage\inputs.json'
$dest = Join-Path $env:USERPROFILE 'Desktop\Input'
$check = Join-Path $env:USERPROFILE 'Desktop\input-check.txt'
# The Sandbox has no Notepad (Windows 11's is a Store app), so the same lines also go to an .html that Edge opens.
$checkHtml = Join-Path $env:USERPROFILE 'Desktop\input-check.html'
function Add-Check([string]$line) {
  $line | Out-File $check -Append -Encoding utf8
  $body = [System.Net.WebUtility]::HtmlEncode((Get-Content $check -Raw -Encoding utf8))
  "<!doctype html><meta charset='utf-8'><title>input-check</title><pre style='font:20px Consolas,monospace'>$body</pre>" |
    Out-File $checkHtml -Encoding utf8
}
New-Item -ItemType Directory -Force $dest | Out-Null
# Exists from the start, so the Agent can open it before the first file arrives and refresh it (F5).
if (-not (Test-Path $checkHtml)) {
  "<!doctype html><meta charset='utf-8'><title>input-check</title><pre style='font:20px Consolas,monospace'>" +
    "(no file yet - refresh with F5)</pre>" | Out-File $checkHtml -Encoding utf8
}
$done = @{}
$tries = @{}
while ($true) {
  $entries = @()
  # PowerShell 5.1 ConvertFrom-Json returns a JSON array as ONE object; piping it on unrolls it into entries.
  try { $parsed = Get-Content $manifest -Raw -ErrorAction Stop | ConvertFrom-Json; $entries = @($parsed | ForEach-Object { $_ }) } catch { }
  foreach ($f in $entries) {
    $name = [string]$f.name
    $key = "$name|$($f.sha256)"
    if ($done.ContainsKey($key)) { continue }
    $root = if ($f.from -eq 'inbox') { 'C:\Inbox' } else { 'C:\UserFiles' }
    if ($name -match '(^|\\)\.\.(\\|$)' -or $name.Contains(':') -or $name.StartsWith('\') -or $name.Contains('/')) {
      Add-Check "FAIL $name bad name (skipped)"; $done[$key] = 1; continue
    }
    $src = Join-Path $root $name
    $dst = Join-Path $dest $name
    if (-not (Test-Path -LiteralPath $src)) {
      $tries[$key] = 1 + [int]$tries[$key]
      if ($tries[$key] -ge 30) { Add-Check "FAIL $name not visible in $root"; $done[$key] = 1 }
      continue
    }
    try {
      $srcHash = (Get-FileHash -LiteralPath $src -Algorithm SHA256 -ErrorAction Stop).Hash.ToLower()
      New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
      Copy-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
      $len = (Get-Item -LiteralPath $dst).Length
      $dstHash = (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash.ToLower()
      if ($len -eq [int64]$f.size -and $srcHash -eq $f.sha256 -and $dstHash -eq $f.sha256) {
        $line = "OK   $name size=$len sha256=$dstHash"
      } else {
        Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue
        $line = "FAIL $name size=$len sha256=$dstHash expected size=$($f.size) sha256=$($f.sha256) (copy deleted)"
      }
    } catch {
      Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue
      $line = "FAIL $name copy failed: $($_.Exception.Message)"
    }
    Add-Check $line
    $done[$key] = 1
  }
  Start-Sleep -Seconds 1
}
"""
INPUT_FILE_MAX = 50 * 1024 * 1024       # same limits as Artifacts (SCRP): 50 MiB per file,
INPUT_TOTAL_MAX = 200 * 1024 * 1024     # 200 MiB per session
RUNNER_NAME = "sandbox_runner.exe"
START_SCRIPT = "start.ps1"
RESTART_SCRIPT = "restart.ps1"
# In the bootstrap folder, written after the Sandbox started. READY_NAME is always written last.
BOOTSTRAP_NAME = "bootstrap.json"
CERT_NAME = "host-cert.cer"
ADDRESS_NAME = "host_address.txt"
READY_NAME = "bootstrap.ready"
READY_WAIT_S = 120

# Runs in the Guest as its LogonCommand. The Host address exists only after this Sandbox started
# (after a reboot the Default Switch is created by the first start, with a new subnet), so the
# script waits for the ready marker, then trusts the Host certificate (Guest account is an
# administrator, no prompt: tools/cert_trust_probe.py) and starts the Runner.
# The log stays inside the Guest and disappears with it; nothing is written back to the Host.
START_PS1 = r"""$log = Join-Path $env:USERPROFILE 'Desktop\runner-start.log'
$watch = 'C:\RunnerPackage\input_watch.ps1'
if (Test-Path $watch) {
  Start-Process -FilePath powershell.exe -WindowStyle Hidden `
    -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',$watch
}
$marker = 'C:\RunnerBootstrap\bootstrap.ready'
$t0 = Get-Date
while (-not (Test-Path $marker) -and ((Get-Date) - $t0).TotalSeconds -lt %(wait)d) { Start-Sleep -Milliseconds 500 }
if (-not (Test-Path $marker)) { "no ready marker" | Out-File $log -Encoding utf8; exit 1 }
$HostIp = (Get-Content 'C:\RunnerBootstrap\host_address.txt' -Raw).Trim()
$parsed = $null
if (-not [System.Net.IPAddress]::TryParse($HostIp, [ref]$parsed) -or $parsed.AddressFamily -ne 'InterNetwork') {
  "bad host address" | Out-File $log -Encoding utf8; exit 1
}
& certutil.exe -addstore -f Root 'C:\RunnerBootstrap\host-cert.cer' | Out-Null
"certutil exit=$LASTEXITCODE" | Out-File $log -Encoding utf8
if ($LASTEXITCODE -ne 0) { exit 1 }""" % {"wait": READY_WAIT_S} + r"""
try {
  $p = Start-Process -FilePath 'C:\RunnerPackage\sandbox_runner.exe' -PassThru -NoNewWindow -ErrorAction Stop `
    -ArgumentList "--host-bootstrap C:\RunnerBootstrap\bootstrap.json --host-address $HostIp" `
    -RedirectStandardOutput (Join-Path $env:USERPROFILE 'Desktop\runner.out.txt') `
    -RedirectStandardError (Join-Path $env:USERPROFILE 'Desktop\runner.err.txt')
  $p.WaitForExit()
  "runner exit=$($p.ExitCode)" | Out-File $log -Append -Encoding utf8
} catch {
  "runner start failed: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
  exit 1
}
"""

# Run by SandboxManager.restart_runner() through `wsb exec` (Host side, fixed text, no arguments).
# Ends the old Runner, then starts start.ps1 again detached: it waits for the ready marker of the
# NEW bootstrap that the Host writes next. Exit 3: the old Runner would not die.
RESTART_PS1 = r"""Stop-Process -Name sandbox_runner -Force -ErrorAction SilentlyContinue
$t0 = Get-Date
while ((Get-Process -Name sandbox_runner -ErrorAction SilentlyContinue) -and ((Get-Date) - $t0).TotalSeconds -lt 10) {
  Start-Sleep -Milliseconds 200
}
if (Get-Process -Name sandbox_runner -ErrorAction SilentlyContinue) { exit 3 }
Start-Process -FilePath powershell.exe -WindowStyle Hidden `
  -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\RunnerPackage\start.ps1'
exit 0
"""


@dataclass(frozen=True)
class Mapping:
    host: Path
    guest: str


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def check_contents(folder: Path) -> None:
    """Refuse a mapped folder holding anything that points outside it.

    The mapping root is checked by build_wsb(); this walks what is inside. A junction or symlink
    would show the Guest a Host folder, and a hard link shares the bytes of a Host file.
    """
    pending = [Path(folder)]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for e in entries:
                st = e.stat(follow_symlinks=False)
                if e.is_symlink() or getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                    raise SandboxManagerError(INVALID_ARGUMENT, f"link inside a mapped folder: {e.path}")
                if e.is_dir(follow_symlinks=False):
                    pending.append(Path(e.path))
                elif os.lstat(e.path).st_nlink > 1:            # DirEntry.stat() leaves st_nlink unset on Windows
                    raise SandboxManagerError(INVALID_ARGUMENT, f"hard-linked file inside a mapped folder: {e.path}")


def logon_command() -> str:
    """Fixed text: nothing session-specific goes on the command line (the address arrives as a file)."""
    return f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File {GUEST_PACKAGE}\\{START_SCRIPT}"


def restart_command() -> str:
    """Fixed text, like logon_command(). The only command Sandbox Manager ever runs through `wsb exec`."""
    return f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File {GUEST_PACKAGE}\\{RESTART_SCRIPT}"


def check_ipv4(value: str) -> str:
    return str(ipaddress.IPv4Address(value))   # raises ValueError on anything but a plain IPv4 literal


def build_wsb(mappings: list[Mapping], command: str, workspace: Path, inbox: Mapping | None = None) -> str:
    """inbox: the one folder allowed outside the workspace (checked by inbox.check_inbox_root), read-only too."""
    for m in mappings:
        if not _inside(m.host, workspace):
            raise SandboxManagerError(INVALID_ARGUMENT, f"refusing to map a folder outside the session workspace: {m.host}")
        if not m.host.is_dir():
            raise SandboxManagerError(INVALID_ARGUMENT, f"mapped folder does not exist: {m.host}")
        if not PureWindowsPath(m.guest).is_absolute():
            raise SandboxManagerError(INVALID_ARGUMENT, f"guest path must be absolute: {m.guest}")
        check_contents(m.host)
    if inbox is not None:
        if not inbox.host.is_dir():
            raise SandboxManagerError(INVALID_ARGUMENT, f"inbox folder does not exist: {inbox.host}")
        if _inside(inbox.host, workspace) or _inside(workspace, inbox.host):
            raise SandboxManagerError(INVALID_ARGUMENT, f"inbox must be separate from the session workspace: {inbox.host}")
        check_contents(inbox.host)
    folders = "".join(
        "<MappedFolder>"
        f"<HostFolder>{escape(str(m.host.resolve()))}</HostFolder>"
        f"<SandboxFolder>{escape(m.guest)}</SandboxFolder>"
        "<ReadOnly>true</ReadOnly>"
        "</MappedFolder>"
        for m in mappings + ([inbox] if inbox is not None else []))
    return (
        "<Configuration>"
        "<Networking>Default</Networking>"
        "<ClipboardRedirection>Disable</ClipboardRedirection>"
        "<PrinterRedirection>Disable</PrinterRedirection>"
        "<AudioInput>Disable</AudioInput>"
        "<VideoInput>Disable</VideoInput>"
        "<ProtectedClient>Enable</ProtectedClient>"
        f"<MappedFolders>{folders}</MappedFolders>"
        f"<LogonCommand><Command>{escape(command)}</Command></LogonCommand>"
        "</Configuration>"
    )
