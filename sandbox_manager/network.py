"""Which Host address the Runner must dial.

Only the `vEthernet (Default Switch)` address is used. The Host LAN address also reaches the Host,
but through NAT the source becomes the Host itself and the Host firewall cannot filter it
(poc/sandbox-launch README "격리 점검"). The address is known before the Sandbox starts
(tools/cert_trust_probe.py, 2026-09-29) and re-checked after start because it has changed within a day before.
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess

SWITCH_ALIAS = "vEthernet (Default Switch)"


class HostNetwork:
    def switch_ipv4(self) -> str | None:
        cmd = (f"(Get-NetIPAddress -InterfaceAlias '{SWITCH_ALIAS}' -AddressFamily IPv4 "
               "-ErrorAction SilentlyContinue).IPAddress")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in out.splitlines():
            try:
                return str(ipaddress.IPv4Address(line.strip()))
            except ValueError:
                continue
        return None

    def toward(self, guest_ip: str) -> str:
        """Local address the OS would use to reach the guest (UDP connect sends no packet)."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((guest_ip, 9))
            return s.getsockname()[0]
