"""Thin wrapper over the Windows Sandbox CLI (`wsb`).

PoC findings (poc/sandbox-launch README, 2026-09-27):
- start takes the .wsb XML as a string and returns the new id
- only one Sandbox runs per PC (second start fails with CO_E_APPSINGLEUSE)
- LogonCommand runs only after `wsb connect` opens the window
- `wsb exec` returns no output, so it is never used on the product path
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess

from .errors import RUNTIME_START_FAILED, RUNTIME_UNAVAILABLE, SandboxManagerError

GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class WsbCli:
    """The real CLI. Tests replace this with a fake that has the same five methods."""

    def __init__(self, exe: str | None = None):
        self.exe = exe or shutil.which("wsb")

    def _run(self, *args: str, timeout: float = 60) -> tuple[dict | None, str]:
        if not self.exe:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "wsb CLI not found; Windows Sandbox is required")
        try:
            proc = subprocess.run([self.exe, *args, "--raw"], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"wsb {args[0]} timed out after {timeout}s") from None
        out = (_decode(proc.stdout) + _decode(proc.stderr)).strip()
        if proc.returncode != 0:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"wsb {args[0]} exit {proc.returncode}: {out}")
        try:
            return json.loads(_decode(proc.stdout)), out
        except json.JSONDecodeError:
            return None, out

    def start(self, config_xml: str) -> str:
        data, text = self._run("start", "--config", config_xml, timeout=180)
        found = GUID_RE.findall(json.dumps(data) if data else text)
        if not found:
            raise SandboxManagerError(RUNTIME_START_FAILED, f"could not read sandbox id from: {text}")
        return found[0].lower()

    def running(self) -> set[str]:
        data, text = self._run("list")
        ids: set[str] = set()
        envs = (data or {}).get("WindowsSandboxEnvironments", [])
        for env in envs:
            if isinstance(env, str):
                ids.add(env.lower())
            elif isinstance(env, dict):
                ids.update(v.lower() for k, v in env.items() if k.lower() == "id" and isinstance(v, str))
        if not ids and envs:
            ids.update(g.lower() for g in GUID_RE.findall(text))
        return ids

    def ip(self, sandbox_id: str) -> str | None:
        try:
            data, text = self._run("ip", "--id", sandbox_id)
        except SandboxManagerError:
            return None
        for candidate in IPV4_RE.findall(json.dumps(data) if data else text):
            try:
                addr = ipaddress.IPv4Address(candidate)
            except ValueError:
                continue
            if not (addr.is_loopback or addr.is_link_local or addr.is_unspecified):
                return str(addr)
        return None

    def connect(self, sandbox_id: str) -> None:
        """Open the Sandbox window without waiting; LogonCommand needs the interactive logon."""
        if not self.exe:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "wsb CLI not found")
        subprocess.Popen([self.exe, "connect", "--id", sandbox_id],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self, sandbox_id: str) -> None:
        self._run("stop", "--id", sandbox_id, timeout=60)
