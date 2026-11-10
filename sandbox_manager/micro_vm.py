"""Explicit micro_VM app-window backend; existing SCRP authentication is unchanged.

The broker owns VM creation/cleanup. This adapter never chooses an existing VM,
never falls back to WSB, and never interprets a closed viewer as VM termination.
"""
from __future__ import annotations

import base64
import json
import math
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from . import config
from .allocation import micro_vm_slot
from .builtin_apps import stage_notepad, VERIFY_NOTEPAD_PS1
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

    def __init__(self, root: Path, *, package: Path, vm_root: Path, relay: Path, max_active=8,
                 startup_timeout_s=180, **kwargs):
        super().__init__(root, **kwargs)
        if type(max_active) is not int or not 1 <= max_active <= 64:
            raise SandboxManagerError(INVALID_ARGUMENT, "micro_VM max_active must be 1..64")
        self.max_active = max_active
        if (type(startup_timeout_s) not in (int, float) or not math.isfinite(startup_timeout_s) or
                not 1 <= startup_timeout_s <= 3600):
            raise SandboxManagerError(INVALID_ARGUMENT, "micro_VM startup_timeout_s must be 1..3600 seconds")
        self.startup_timeout_s = startup_timeout_s
        self.package, self.vm_root, self.relay = package.resolve(), vm_root.resolve(), relay.resolve()
        self.exe = self.package / "dbictl.exe"
        if not self.exe.is_file() or not self.relay.is_file():
            raise SandboxManagerError(INVALID_ARGUMENT, "micro_VM package or ScrpRelay executable is missing")
        self._relays: dict[str, list[subprocess.Popen]] = {}
        self.management = None
        if (self.package / "management.json").is_file():
            from .management_service import ManagementService
            self.management = ManagementService(self.package, self.vm_root, self.root)

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
        record = self._read_record(s, workspace)
        for key in ("id", "container_id", "owner_sid", "label"):
            expected = workspace if key == "id" else s.micro_vm[key]
            if record.get(key) != expected:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"VM ownership mismatch: {key}")
        return record

    def _read_record(self, s: SandboxSession, workspace: str) -> dict:
        if self.management:
            # The High writer may be replacing/locking its state file. Medium
            # clients use the serialized, lease-authorized snapshot, never open
            # a privileged implementation file or relax its ACL to poll it.
            record = self.management.rpc(s.session_id, "status")
            if record.get("id") != workspace or record.get("label") != s.session_id:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, "management status returned a different session/VM")
            return record
        return json.loads((self.vm_root / "workspaces" / (workspace + ".json")).read_text(encoding="utf-8-sig"))

    def _control(self, s: SandboxSession, action: dict) -> dict:
        self._record(s)
        if self.management:
            result = self.management.rpc(s.session_id, "control", {"action": action})
            if result.get("acknowledged") is not True:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"VM control was not acknowledged: {result}")
            return result["reply"]
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
        # All cooperating Hosts serialize admission through the broker's durable
        # creation record. Uncertain/failed cleanup consumes capacity; no old VM
        # is reclaimed. This is a per-broker-root admission budget, not an OS
        # memory guarantee or a limit on manual native CLI users.
        if self.management:
            # The service serializes admission with creation and includes every
            # unresolved record; no cross-integrity Host mutex is necessary.
            result = self.management.start(s.session_id)
        else:
            with micro_vm_slot():
                self._check_capacity()
                result = self._command("start", "--memory-mb", "4096", "--viewer", "--confirm-start",
                                       "--trial-unverified-build", "--label", s.session_id)
        workspace = guid(result["id"])
        s.sandbox_id = workspace
        self._save(s)
        deadline = self.clock() + self.startup_timeout_s
        while self.clock() < deadline:
            record = self._read_record(s, workspace)
            if record.get("container_id") and s.micro_vm is None:
                s.micro_vm = {"root": str(self.vm_root), "workspace_id": workspace,
                              "container_id": guid(record["container_id"]), "owner_sid": record["owner_sid"],
                              "label": s.session_id, "channels": []}
                self._save(s)
            if record["state"] == "Running":
                self._record(s)
                try:
                    info = self._control(s, {"cmd": "info"})
                except SandboxManagerError as error:
                    # Read-only readiness probe, never an application/input replay.
                    if "no matching guest connection within timeout" not in str(error):
                        raise
                    self.sleep(1)
                    continue
                if (info.get("run_process_status") is True and type(info.get("pid")) is int and info["pid"] > 0 and
                        isinstance(info.get("agent_creation_time"), str) and info["agent_creation_time"].isdigit()):
                    s.micro_vm.update(agent_pid=info["pid"], process_status_v1=True,
                                      agent_creation_time=info["agent_creation_time"],
                                      output_storage_generation=s.generation)
                s.host_address = "127.0.0.1"
                self._state(s, STARTED)
                return s.host_address
            if record["state"] in ("Failed", "Stopped", "NeedsReconcile"):
                if s.micro_vm is None and self._creation_not_attempted(s, record):
                    # Keep the exact failed creation identity without inventing a
                    # container binding or issuing a stop against another VM.
                    s.micro_vm = {"root": str(self.vm_root), "workspace_id": workspace,
                                  "container_id": None, "owner_sid": record["owner_sid"],
                                  "label": s.session_id, "channels": []}
                    self._save(s)
                error = record.get("result", {}).get("error", {})
                raise SandboxManagerError(RUNTIME_UNAVAILABLE,
                    f"micro_VM startup ended in {record['state']}: {error}")
            self.sleep(1)
        raise SandboxManagerError(RUNTIME_UNAVAILABLE, "micro_VM startup timeout; owned binding retained")

    def _creation_not_attempted(self, s, record):
        owner = ({"sid": self.management.owner_sid} if self.management else
                 json.loads((self.vm_root / "owner.json").read_text(encoding="utf-8-sig")))
        result = record.get("result", {})
        return (record.get("id") == s.sandbox_id and record.get("label") == s.session_id and
                bool(owner.get("sid")) and record.get("owner_sid") == owner["sid"] and
                record.get("container_id") is None and record.get("state") == "Failed" and
                record.get("runtime_creation") == "not_attempted" and
                record.get("cleanup_confirmed") is True and result.get("cleaned_up") is True and
                result.get("runtime_creation") == "not_attempted")

    def _check_capacity(self):
        active = 0
        for path in (self.vm_root / "workspaces").glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
                if guid(record["id"]) != path.stem:
                    raise ValueError("record filename mismatch")
                state = record["state"]
                if not isinstance(state, str):
                    raise ValueError("invalid state")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE,
                                          "micro_VM capacity cannot be verified; no VM started") from exc
            if state not in ("Stopped", "Failed") or record.get("cleanup_confirmed") is not True:
                active += 1
        if active >= self.max_active:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE,
                f"micro_VM capacity exhausted: {active}/{self.max_active}; existing VMs preserved")

    def _share(self, s: SandboxSession, source: Path) -> str:
        config.check_contents(source)
        if self.management:
            bucket = ("package" if source == s.package_dir else "bootstrap" if source == s.bootstrap_dir else
                      "input" if source == s.input_dir else "inbox" if s.inbox and source == Path(s.inbox) else None)
            if bucket is None:
                raise SandboxManagerError(INVALID_ARGUMENT, "unknown management projection source")
            self._record(s)
            result = self.management.publish(s.session_id, source, bucket)
        else:
            result = self._share_native(s, source)
        return self._wait_share(s, result)

    def _share_native(self, s: SandboxSession, source: Path) -> dict:
        # Grant VM workers read access to this checked session copy only.
        acl = subprocess.run(["icacls.exe", str(source), "/grant", "*S-1-5-83-0:(OI)(CI)RX"],
                             capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if acl.returncode:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "cannot grant VM read access to session mapping")
        self._record(s)
        return self._command("share", "--id", s.sandbox_id, "--path", str(source), "--confirm-share")

    def _wait_share(self, s: SandboxSession, result: dict) -> str:
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
        if s.micro_vm.get("recovery_pending"):
            # Same checked read-only shares and relays, fresh bootstrap/token.
            context_path = s.package_dir / "guest-context.json"
            context = json.loads(context_path.read_text(encoding="utf-8"))
            context["generation"] = s.generation
            context_path.write_text(json.dumps(context), encoding="utf-8")
            super().publish_bootstrap(s, host_cert_pem)
            self._launch_runner(s)
            s.micro_vm.pop("recovery_pending")
            self._save(s)
            return
        bootstrap = json.loads(s.bootstrap_path.read_text(encoding="utf-8"))
        ports = {bootstrap["port"], bootstrap["observation_upload"]["port"]}
        if "artifact_upload" in bootstrap:
            ports.add(bootstrap["artifact_upload"]["port"])
        if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
            raise SandboxManagerError(INVALID_ARGUMENT, "invalid SCRP relay ports")
        shutil.copyfile(self.relay, s.package_dir / "ScrpRelay.exe")
        app_files = stage_notepad(s.package_dir)
        self._event(s, "MICRO_VM_BUILTIN_APP_STAGED", app="notepad", files=app_files)
        context = {"contract": "scrp-micro-vm-v1", "session_id": s.session_id,
                   "runtime_id": s.runtime_id, "generation": s.generation,
                   "workspace_id": s.micro_vm["workspace_id"], "container_id": s.micro_vm["container_id"],
                   "runner_sha256": s.runner_sha256,
                   "output_path": f"C:\\RunnerWorkspace\\Sessions\\session-{s.session_id}\\runtime-{s.runtime_id}\\generation-{s.micro_vm.get('output_storage_generation', s.generation)}\\output"}
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
        script += VERIFY_NOTEPAD_PS1
        for channel in channels:
            args = ["guest", "{" + s.micro_vm["container_id"] + "}", "{" + channel["service"] + "}",
                    str(channel["port"])]
            parent = str(s.micro_vm["agent_pid"]) if s.micro_vm.get("process_status_v1") else "$PID"
            script += "$a=@(" + ",".join(ps_quote(arg) for arg in args) + ", [string]" + parent + ", " + ps_quote(
                "C:\\RunnerLogs\\relay-" + str(channel["port"]) + ".ready") + ")\n"
            script += "$p=Start-Process C:\\RunnerPackage\\ScrpRelay.exe -ArgumentList $a -PassThru -WindowStyle Hidden\n"
        script += "& certutil.exe -addstore -f Root C:\\RunnerBootstrap\\host-cert.cer | Out-Null\nif($LASTEXITCODE){throw 'Guest certificate import failed'}\n"
        script += "if(Test-Path C:\\RunnerPackage\\input_watch.ps1){Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File C:\\RunnerPackage\\input_watch.ps1'}\n"
        if not s.micro_vm.get("process_status_v1"):
            script += "$runner=Start-Process C:\\RunnerPackage\\sandbox_runner.exe -PassThru -ArgumentList '--host-bootstrap C:\\RunnerBootstrap\\bootstrap.json --host-address 127.0.0.1 --surface app-window' -RedirectStandardOutput C:\\RunnerLogs\\runner.out.txt -RedirectStandardError C:\\RunnerLogs\\runner.err.txt\n$runner.WaitForExit()\n"
        script += "} catch { $_ | Out-String | Write-Output; exit 1 } finally { Stop-Transcript }\n"
        # This fixed launcher executes in the VM through its authenticated control
        # channel. It is not a Router workload or an application success receipt.
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded
        if s.micro_vm.get("process_status_v1"):
            preparation = self._run_tracked(s, command)
            row = self._wait_run(s, preparation, exit_required=True)
            if row["process_status"]["exit_code"] != 0:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, "Guest Runner preparation failed; no Runner launched")
            reply = self._launch_runner(s)
        else:
            reply = self._control(s, {"cmd": "run", "exe": command})
        self._event(s, "MICRO_VM_RUNNER_LAUNCH_REQUESTED", reply=reply, relay_ports=sorted(ports))
        if self.management:
            self.management.watch()

    def _run_tracked(self, s, command):
        reply = self._control(s, {"cmd": "run", "exe": command})
        if (reply.get("agent_pid") != s.micro_vm.get("agent_pid") or
                reply.get("agent_creation_time") != s.micro_vm.get("agent_creation_time") or
                type(reply.get("run_id")) is not int or reply["run_id"] < 1):
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "Guest launch outcome unknown; never replayed")
        return reply

    def _sync_app_package(self, s):
        super()._sync_app_package(s)
        self._record(s)
        if self.management:
            with self.management.lock:
                self.management.mappings[(s.session_id, "package")] = s.package_dir
                self.management.sync(s.session_id)

    def _app_guest_exit(self, s, script):
        self._record(s)
        if not s.micro_vm.get("process_status_v1"):
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "app preparation requires verified Guest process receipts")
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        launched = self._run_tracked(s, "powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded)
        row = self._wait_run(s, launched, exit_required=True)
        return row["process_status"]["exit_code"]

    def share_program(self, s, name):
        result = self.prepare_apps(s, [name])
        if not result["prepared"]:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"application preparation incomplete: {result['apps']}")
        return result["apps"][0]

    def _run_record(self, s, run_id):
        info = self._control(s, {"cmd": "info"})
        if (info.get("pid") != s.micro_vm.get("agent_pid") or info.get("run_process_status") is not True or
                info.get("agent_creation_time") != s.micro_vm.get("agent_creation_time")):
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "Guest Agent identity changed; process outcome unknown")
        rows = [row for row in info.get("runs", []) if row.get("run_id") == run_id]
        if len(rows) != 1:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "original Guest process record unavailable; VM preserved")
        return rows[0]

    def _wait_run(self, s, launched, *, exit_required=False):
        deadline = self.clock() + 30
        while self.clock() < deadline:
            row = self._run_record(s, launched["run_id"])
            status = row.get("process_status", {})
            if row.get("error"):
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, "Guest process creation failed; not retried")
            if not row.get("pending", True):
                if (type(row.get("pid")) is not int or row["pid"] <= 0 or
                        status.get("state") not in ("RUNNING", "EXITED") or
                        not isinstance(status.get("creation_time"), str) or not status["creation_time"].isdigit()):
                    raise SandboxManagerError(RUNTIME_UNAVAILABLE, "original Guest process handle could not be verified")
                if not exit_required or status["state"] == "EXITED":
                    return dict(row, agent_pid=s.micro_vm["agent_pid"])
            self.sleep(.1)
        raise SandboxManagerError(RUNTIME_UNAVAILABLE, "Guest process outcome timeout; launch not replayed")

    def _launch_runner(self, s):
        if self.management:
            self.management.sync(s.session_id)
        if s.micro_vm.get("runner_launch_requested"):
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "Runner launch already requested; refusing duplicate")
        s.micro_vm["runner_launch_requested"] = True
        self._save(s)  # Persist intent before sending; a lost reply never causes a duplicate.
        launched = self._run_tracked(s, r'C:\RunnerPackage\sandbox_runner.exe --host-bootstrap C:\RunnerBootstrap\bootstrap.json --host-address 127.0.0.1 --surface app-window')
        runner = self._wait_run(s, launched)
        s.micro_vm["runner_process"] = runner
        self._save(s)
        return runner

    @_locked
    def runner_exited(self, s):
        """Positive original-handle proof only; never kill/reopen a PID here."""
        if not s.micro_vm or not s.micro_vm.get("process_status_v1"):
            return False
        if self._record(s)["state"] != "Running":
            return False
        original = s.micro_vm.get("runner_process")
        if not original:
            return False
        row = self._run_record(s, original["run_id"])
        status = row.get("process_status", {})
        return (row.get("pid") == original["pid"] and not row.get("pending", True) and
                status.get("creation_time") == original["process_status"]["creation_time"] and
                status.get("state") == "EXITED" and type(status.get("exit_code")) is int)

    @_locked
    def is_running(self, s: SandboxSession) -> bool:
        return bool(s.micro_vm) and self._record(s)["state"] == "Running"

    def window_state(self, s: SandboxSession) -> str:
        return "UNAVAILABLE"

    @_locked
    def restart_runner(self, s: SandboxSession, generation: int) -> None:
        self._begin_recovery(s, generation, RUNNING)
        if not self.runner_exited(s):
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "micro_VM Runner recovery requires confirmed old process termination; VM preserved")
        s.ready_path.unlink(missing_ok=True)
        s.bootstrap_path.unlink(missing_ok=True)
        old = s.generation
        s.micro_vm.setdefault("retired_runners", []).append(s.micro_vm.pop("runner_process"))
        s.micro_vm.pop("runner_launch_requested", None)
        s.micro_vm["recovery_pending"] = True
        s.generation = generation
        s.restarts += 1
        self._state(s, STARTED, recovery="runner", generation_from=old, generation=generation)

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
            if s.micro_vm.get("container_id") is None:
                if not self._creation_not_attempted(s, record):
                    raise SandboxManagerError(RUNTIME_UNAVAILABLE, "VM creation/cleanup uncertain; refusing blind stop")
            elif record["state"] != "Stopped":
                if self.management:
                    self.management.rpc(s.session_id, "stop")
                else:
                    self._command("stop", "--id", s.sandbox_id, "--confirm-stop")
            deadline = self.clock() + 60
            while self.clock() < deadline:
                record = self._record(s)
                if s.micro_vm.get("container_id") is None and self._creation_not_attempted(s, record): break
                if record["state"] == "Stopped" and record.get("cleanup_confirmed"): break
                self.sleep(.5)
            else: raise SandboxManagerError(RUNTIME_UNAVAILABLE, "owned micro_VM cleanup not confirmed")
        elif s.sandbox_id:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "VM ownership binding incomplete; refusing blind stop")
        if self.management:
            self.management.stop_watching(s.session_id)
        for process in self._relays.pop(s.session_id, []):
            if process.poll() is None: process.terminate()
            process.wait(timeout=5)
        s.ready_path.unlink(missing_ok=True)
        s.bootstrap_path.unlink(missing_ok=True)
        s.termination_reason = reason
        self._state(s, TERMINATED, reason=reason, verified_by="exact workspace/container ownership + cleanup_confirmed")
        return s
