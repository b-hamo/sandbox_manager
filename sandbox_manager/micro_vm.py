"""Explicit micro_VM app-window backend; existing SCRP authentication is unchanged.

The broker owns VM creation/cleanup. This adapter never chooses an existing VM,
never falls back to WSB, and never interprets a closed viewer as VM termination.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from . import config
from .errors import INVALID_ARGUMENT, RUNTIME_UNAVAILABLE, SandboxManagerError
from .manager import (SandboxManager, SandboxSession, _locked, PREPARED, STARTING,
                      STARTED, RUNNING, RESTARTING, TERMINATED, FAILED, TERMINATION_REASONS)


def guid(value: str) -> str:
    parsed = str(uuid.UUID(value))
    if parsed != value or parsed == str(uuid.UUID(int=0)):
        raise ValueError("a full canonical nonzero VM/service GUID is required")
    return parsed


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class MicroVmManager(SandboxManager):
    fixed_vm_binding = True
    guest_input_root = r"C:\RunnerInput"

    def __init__(self, root: Path, *, package: Path, vm_root: Path, relay: Path, **kwargs):
        super().__init__(root, **kwargs)
        self.package, self.vm_root, self.relay = package.resolve(), vm_root.resolve(), relay.resolve()
        self.exe = self.package / "dbictl.exe"
        if not self.exe.is_file() or not self.relay.is_file():
            raise SandboxManagerError(INVALID_ARGUMENT, "micro_VM package or ScrpRelay executable is missing")
        self._relays: dict[str, list[subprocess.Popen]] = {}

    def _command(self, *arguments: str) -> dict:
        process = subprocess.run([str(self.exe), "workspace", *arguments, "--root", str(self.vm_root)],
                                 capture_output=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            result = json.loads(process.stdout.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "micro_VM command returned no JSON; not retried") from exc
        if process.returncode or not result.get("ok"):
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"micro_VM {arguments[0]} failed: {result}")
        return result["result"]

    def _record(self, s: SandboxSession) -> dict:
        if not s.micro_vm or s.micro_vm["root"] != str(self.vm_root):
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "session has no matching VM binding")
        workspace = guid(s.micro_vm["workspace_id"])
        if s.sandbox_id != workspace:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "session/workspace binding mismatch")
        record = json.loads((self.vm_root / "workspaces" / (workspace + ".json")).read_text(encoding="utf-8-sig"))
        for key in ("id", "container_id", "owner_sid", "label"):
            expected = workspace if key == "id" else s.micro_vm[key]
            if record.get(key) != expected:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"VM ownership mismatch: {key}")
        return record

    def _control(self, s: SandboxSession, action: dict) -> dict:
        self._record(s)
        path = s.dir / ("control-" + uuid.uuid4().hex + ".json")
        path.write_text(json.dumps(action), encoding="utf-8")
        try:
            result = self._command("control", "--id", s.sandbox_id, "--action", str(path))
        finally:
            path.unlink(missing_ok=True)
        if not result.get("acknowledged"):
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"VM control was not acknowledged: {result}")
        return result["reply"]

    @_locked
    def start(self, s: SandboxSession) -> str:
        self._need(s, PREPARED)
        s.runtime_type = "MICRO_VM_APP_WINDOW"
        self._state(s, STARTING)
        # The authenticated broker serializes creation; --viewer enables its GUI
        # agent but does not log on a full desktop or open an RDP viewer.
        result = self._command("start", "--memory-mb", "4096", "--viewer", "--confirm-start",
                               "--trial-unverified-build", "--label", s.session_id)
        workspace = guid(result["id"])
        s.sandbox_id = workspace
        deadline = self.clock() + 180
        while self.clock() < deadline:
            record = json.loads((self.vm_root / "workspaces" / (workspace + ".json")).read_text(encoding="utf-8-sig"))
            if record.get("container_id") and s.micro_vm is None:
                s.micro_vm = {"root": str(self.vm_root), "workspace_id": workspace,
                              "container_id": guid(record["container_id"]), "owner_sid": record["owner_sid"],
                              "label": s.session_id, "channels": []}
                self._save(s)
            if record["state"] == "Running":
                self._record(s)
                try:
                    self._control(s, {"cmd": "info"})
                except SandboxManagerError as error:
                    # Read-only readiness probe, never an application/input replay.
                    if "no matching guest connection within timeout" not in str(error):
                        raise
                    self.sleep(1)
                    continue
                s.host_address = "127.0.0.1"
                self._state(s, STARTED)
                return s.host_address
            if record["state"] in ("Failed", "Stopped", "NeedsReconcile"):
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"micro_VM startup ended in {record['state']}")
            self.sleep(1)
        raise SandboxManagerError(RUNTIME_UNAVAILABLE, "micro_VM startup timeout; owned binding retained")

    def _share(self, s: SandboxSession, source: Path) -> str:
        config.check_contents(source)
        # Grant VM workers read access to this checked session copy only.
        acl = subprocess.run(["icacls.exe", str(source), "/grant", "*S-1-5-83-0:(OI)(CI)RX"],
                             capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if acl.returncode:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "cannot grant VM read access to session mapping")
        self._record(s)
        result = self._command("share", "--id", s.sandbox_id, "--path", str(source), "--confirm-share")
        share = guid(result["share_id"])
        deadline = self.clock() + 30
        while self.clock() < deadline:
            shares = self._record(s).get("host_shares", [])
            row = next((item for item in shares if item["id"] == share), None)
            if row and row["state"] == "Attached":
                # PowerShell 5.1's FileSystem provider cannot resolve the raw
                # VMSMB device path. The existing guest mount implementation
                # defines a checked DOS drive for this exact share instead.
                mounted = self._control(s, {"cmd": "mount", "share": share, "name": "SCRP-" + share})
                drive = mounted.get("drive")
                if not isinstance(drive, str) or len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
                    raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"VM share has no usable DOS drive: {mounted}")
                return drive + "\\"
            if row and row["state"] == "Failed":
                break
            self.sleep(.2)
        raise SandboxManagerError(RUNTIME_UNAVAILABLE, "read-only VM share not attached")

    @_locked
    def publish_bootstrap(self, s: SandboxSession, host_cert_pem: str) -> None:
        self._need(s, STARTED)
        bootstrap = json.loads(s.bootstrap_path.read_text(encoding="utf-8"))
        ports = {bootstrap["port"], bootstrap["observation_upload"]["port"]}
        if "artifact_upload" in bootstrap:
            ports.add(bootstrap["artifact_upload"]["port"])
        if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
            raise SandboxManagerError(INVALID_ARGUMENT, "invalid SCRP relay ports")
        shutil.copyfile(self.relay, s.package_dir / "ScrpRelay.exe")
        context = {"contract": "scrp-micro-vm-v1", "session_id": s.session_id,
                   "runtime_id": s.runtime_id, "generation": s.generation,
                   "workspace_id": s.micro_vm["workspace_id"], "container_id": s.micro_vm["container_id"],
                   "runner_sha256": s.runner_sha256,
                   "output_path": f"C:\\RunnerWorkspace\\Sessions\\session-{s.session_id}\\runtime-{s.runtime_id}\\generation-{s.generation}\\output"}
        (s.package_dir / "guest-context.json").write_text(json.dumps(context), encoding="utf-8")
        # Keep the checked inbox copying implementation; replace only its local
        # destination, so no Guest username is assumed by Router.
        watcher = s.package_dir / config.INPUT_WATCH_SCRIPT
        if watcher.exists():
            script = watcher.read_text(encoding="utf-8-sig").replace(
                "$dest = Join-Path $env:USERPROFILE 'Desktop\\Input'", "$dest = 'C:\\RunnerInput'")
            script = script.replace("Join-Path $env:USERPROFILE 'Desktop\\input-check.txt'", "'C:\\RunnerLogs\\input-check.txt'")
            script = script.replace("Join-Path $env:USERPROFILE 'Desktop\\input-check.html'", "'C:\\RunnerLogs\\input-check.html'")
            watcher.write_text(script, encoding="utf-8-sig")
        channels = [{"port": port, "service": str(uuid.uuid4())} for port in sorted(ports)]
        s.micro_vm["channels"] = channels
        self._save(s)
        super().publish_bootstrap(s, host_cert_pem)
        # All mappings remain read-only; file export uses the existing HTTPS
        # artifact service and scanner, never a writable Host mount.
        mappings = [(s.package_dir, config.GUEST_PACKAGE), (s.bootstrap_dir, config.GUEST_BOOTSTRAP)]
        if s.inbox: mappings.append((Path(s.inbox), config.GUEST_INBOX))
        if s.input_files: mappings.append((s.input_dir, config.GUEST_INPUT))
        links = [(self._share(s, source), target) for source, target in mappings]
        processes = self._relays.setdefault(s.session_id, [])
        for channel in channels:
            ready = s.dir / ("relay-" + channel["service"] + ".ready")
            log = open(s.dir / ("relay-" + channel["service"] + ".log"), "wb")
            try:
                process = subprocess.Popen([str(self.relay), "host", "{" + s.micro_vm["container_id"] + "}",
                    "{" + channel["service"] + "}", str(channel["port"]), str(os.getpid()), str(ready)],
                    stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
            finally:
                log.close()
            processes.append(process)
            deadline = self.clock() + 5
            while not ready.exists() and process.poll() is None and self.clock() < deadline: self.sleep(.05)
            if not ready.exists() or process.poll() is not None:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, "VM-bound SCRP relay did not listen")
        script = "$ErrorActionPreference='Stop'\nNew-Item -ItemType Directory -Force C:\\RunnerLogs,C:\\RunnerInput | Out-Null\nStart-Transcript C:\\RunnerLogs\\launch.txt\ntry {\n"
        for source, target in links:
            script += f"New-Item -ItemType SymbolicLink -Path {ps_quote(target)} -Target {ps_quote(source)} | Out-Null\n"
        for channel in channels:
            args = ["guest", "{" + s.micro_vm["container_id"] + "}", "{" + channel["service"] + "}",
                    str(channel["port"])]
            script += "$a=@(" + ",".join(ps_quote(arg) for arg in args) + ", [string]$PID, " + ps_quote(
                "C:\\RunnerLogs\\relay-" + str(channel["port"]) + ".ready") + ")\n"
            script += "$p=Start-Process C:\\RunnerPackage\\ScrpRelay.exe -ArgumentList $a -PassThru -WindowStyle Hidden\n"
        script += "& certutil.exe -addstore -f Root C:\\RunnerBootstrap\\host-cert.cer | Out-Null\nif($LASTEXITCODE){throw 'Guest certificate import failed'}\n"
        script += "if(Test-Path C:\\RunnerPackage\\input_watch.ps1){Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File C:\\RunnerPackage\\input_watch.ps1'}\n"
        script += "$runner=Start-Process C:\\RunnerPackage\\sandbox_runner.exe -PassThru -ArgumentList '--host-bootstrap C:\\RunnerBootstrap\\bootstrap.json --host-address 127.0.0.1 --surface app-window' -RedirectStandardOutput C:\\RunnerLogs\\runner.out.txt -RedirectStandardError C:\\RunnerLogs\\runner.err.txt\n$runner.WaitForExit()\n"
        script += "} catch { $_ | Out-String | Write-Output } finally { Stop-Transcript }\n"
        # This fixed launcher executes in the VM through its authenticated control
        # channel. It is not a Router workload or an application success receipt.
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        reply = self._control(s, {"cmd": "run", "exe": "powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded})
        self._event(s, "MICRO_VM_RUNNER_LAUNCH_REQUESTED", reply=reply, relay_ports=sorted(ports))

    @_locked
    def is_running(self, s: SandboxSession) -> bool:
        return bool(s.micro_vm) and self._record(s)["state"] == "Running"

    def window_state(self, s: SandboxSession) -> str:
        return "UNAVAILABLE"

    @_locked
    def restart_runner(self, s: SandboxSession, generation: int) -> None:
        # Never replay an uncertain launch or move the session to another VM.
        raise SandboxManagerError(RUNTIME_UNAVAILABLE, "micro_VM Runner recovery requires confirmed old process termination; VM preserved")

    @_locked
    def reset_sandbox(self, s: SandboxSession, generation: int) -> str:
        raise SandboxManagerError(RUNTIME_UNAVAILABLE, "micro_VM binding is fixed until explicit session termination")

    @_locked
    def stop(self, s: SandboxSession, reason: str, *, emergency: bool = False) -> SandboxSession:
        if reason not in TERMINATION_REASONS:
            raise SandboxManagerError(INVALID_ARGUMENT, "invalid termination reason")
        if s.state in (TERMINATED, FAILED): return s
        if s.micro_vm:
            record = self._record(s)
            if record["state"] != "Stopped":
                self._command("stop", "--id", s.sandbox_id, "--confirm-stop")
            deadline = self.clock() + 60
            while self.clock() < deadline:
                record = self._record(s)
                if record["state"] == "Stopped" and record.get("cleanup_confirmed"): break
                self.sleep(.5)
            else: raise SandboxManagerError(RUNTIME_UNAVAILABLE, "owned micro_VM cleanup not confirmed")
        elif s.sandbox_id:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "VM ownership binding incomplete; refusing blind stop")
        for process in self._relays.pop(s.session_id, []):
            if process.poll() is None: process.terminate()
            process.wait(timeout=5)
        s.ready_path.unlink(missing_ok=True)
        s.bootstrap_path.unlink(missing_ok=True)
        s.termination_reason = reason
        self._state(s, TERMINATED, reason=reason, verified_by="exact workspace/container ownership + cleanup_confirmed")
        return s
