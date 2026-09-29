""".wsb configuration and the Guest start script.

Rules (CLAUDE.md "절대 하지 않는 것"):
- every mapped folder is read-only; there is no parameter to make one writable
- only folders inside this session's own workspace can be mapped (no user folders, browser
  profiles, Safe Results, or Host output)
- Clipboard, printer, audio and video redirection are off
- Networking stays on for the control channel. This is NOT isolation (미결 16)
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from xml.sax.saxutils import escape

from .errors import INVALID_ARGUMENT, SandboxManagerError

GUEST_PACKAGE = r"C:\RunnerPackage"
GUEST_BOOTSTRAP = r"C:\RunnerBootstrap"
RUNNER_NAME = "sandbox_runner.exe"
START_SCRIPT = "start.ps1"
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


def logon_command() -> str:
    """Fixed text: nothing session-specific goes on the command line (the address arrives as a file)."""
    return f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File {GUEST_PACKAGE}\\{START_SCRIPT}"


def check_ipv4(value: str) -> str:
    return str(ipaddress.IPv4Address(value))   # raises ValueError on anything but a plain IPv4 literal


def build_wsb(mappings: list[Mapping], command: str, workspace: Path) -> str:
    for m in mappings:
        if not _inside(m.host, workspace):
            raise SandboxManagerError(INVALID_ARGUMENT, f"refusing to map a folder outside the session workspace: {m.host}")
        if not m.host.is_dir():
            raise SandboxManagerError(INVALID_ARGUMENT, f"mapped folder does not exist: {m.host}")
        if not PureWindowsPath(m.guest).is_absolute():
            raise SandboxManagerError(INVALID_ARGUMENT, f"guest path must be absolute: {m.guest}")
    folders = "".join(
        "<MappedFolder>"
        f"<HostFolder>{escape(str(m.host.resolve()))}</HostFolder>"
        f"<SandboxFolder>{escape(m.guest)}</SandboxFolder>"
        "<ReadOnly>true</ReadOnly>"
        "</MappedFolder>"
        for m in mappings)
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
