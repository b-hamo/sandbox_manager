"""Check that the Host firewall rules for the Sandbox switch are actually in force.

Why (poc/sandbox-launch README, 2026-09-29):
- Windows stores a rule's interface as the adapter GUID, not its name. A reboot recreates
  `vEthernet (Default Switch)` with a new GUID, so every rule silently stops applying:
  the Runner can no longer reach 17443/17444 AND the Host's other ports are exposed again.
- The adapter only exists after the first Sandbox start following a reboot, so this check
  runs after `wsb start`, never before.
- A block rule beats an allow rule, so the block range must leave 17443 and 17444 out.

Changing rules needs administrator rights; this module only reads and explains the fix.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from .network import SWITCH_ALIAS

RULE_PREFIX = "SCRP PoC"          # names used by the PoC setup (이어하기.md). Product installer may rename.
REQUIRED_PORTS = (17443, 17444)   # control WSS, screenshot HTTPS PUT
FIX_COMMAND = (f'Get-NetFirewallRule -DisplayName "{RULE_PREFIX}*" | '
               f'Set-NetFirewallRule -InterfaceAlias "{SWITCH_ALIAS}"')

_PS = r"""
$rules = @(Get-NetFirewallRule -DisplayName '%s*' -ErrorAction SilentlyContinue | ForEach-Object {
  $p = $_ | Get-NetFirewallPortFilter
  $i = $_ | Get-NetFirewallInterfaceFilter
  [pscustomobject]@{ name = $_.DisplayName; enabled = [string]$_.Enabled; direction = [string]$_.Direction;
    action = [string]$_.Action; protocol = [string]$p.Protocol; ports = @($p.LocalPort | ForEach-Object { [string]$_ });
    ifaces = @($i.InterfaceAlias | ForEach-Object { [string]$_ }) }
})
ConvertTo-Json -Compress -Depth 4 @{ rules = $rules }
"""


@dataclass
class Rule:
    name: str
    enabled: bool
    inbound: bool
    action: str
    protocol: str
    ports: list[str]
    ifaces: list[str]


def _covers(ports: list[str], port: int) -> bool:
    for part in ports:
        if part.lower() == "any":
            return True
        lo, _, hi = part.partition("-")
        try:
            if int(lo) <= port <= int(hi or lo):
                return True
        except ValueError:
            continue
    return False


def evaluate(rules: list[Rule]) -> list[str]:
    """Pure logic, testable without PowerShell. Empty list = OK."""
    problems: list[str] = []
    if not rules:
        return [f"no firewall rules named '{RULE_PREFIX}*' (see 이어하기.md for the admin setup)"]
    stale = [r.name for r in rules if SWITCH_ALIAS not in r.ifaces]
    if stale:
        problems.append(f"rules not bound to the current '{SWITCH_ALIAS}' adapter (reboot?): {stale}")
    live = [r for r in rules if r.enabled and r.inbound and SWITCH_ALIAS in r.ifaces and r.protocol.upper() == "TCP"]
    for port in REQUIRED_PORTS:
        if not any(r.action == "Allow" and _covers(r.ports, port) for r in live):
            problems.append(f"no active allow rule for TCP {port}")
        blockers = [r.name for r in live if r.action == "Block" and _covers(r.ports, port)]
        if blockers:
            problems.append(f"TCP {port} is inside a block rule (block beats allow): {blockers}")
    if not any(r.action == "Block" for r in live):
        problems.append("no active TCP block rule: other Host ports are reachable from the Sandbox")
    return problems


class FirewallCheck:
    def rules(self) -> list[Rule]:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _PS % RULE_PREFIX],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        data = json.loads(out or '{"rules": []}').get("rules") or []
        if isinstance(data, dict):
            data = [data]
        return [Rule(d["name"], d["enabled"] == "True", d["direction"] == "Inbound", d["action"],
                     d["protocol"], list(d.get("ports") or []), list(d.get("ifaces") or [])) for d in data]

    def problems(self) -> list[str]:
        try:
            return evaluate(self.rules())
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
            return [f"could not read firewall rules: {exc}"]
