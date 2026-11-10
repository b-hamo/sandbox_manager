"""Medium client of the installed micro_VM management worker.

Only bytes cross the privilege boundary for file projection. Neither local
source paths nor native CLI arguments are sent to the privileged service.
The protected native client authenticates the same-SID High server executable
before sending a capability. Mutating RPCs are never automatically replayed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import threading
import uuid
from pathlib import Path

from . import config
from .errors import INVALID_ARGUMENT, RUNTIME_UNAVAILABLE, SandboxManagerError


class ManagementService:
    def __init__(self, package: Path, vm_root: Path, state: Path):
        self.exe = package / "MinoVmService.exe"
        installed = json.loads((package / "management.json").read_text(encoding="utf-8-sig"))
        if installed.get("schema_version") != 1 or Path(installed.get("root", "")).resolve() != vm_root:
            raise SandboxManagerError(INVALID_ARGUMENT, "installed management root does not match this Host")
        if not self.exe.is_file():
            raise SandboxManagerError(INVALID_ARGUMENT, "installed VM management client is missing")
        self.owner_sid = installed.get("owner_sid")
        self.vm_root = vm_root
        self.state = state / "service-leases"
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.leases = {}
        self.mappings = {}
        self.hashes = {}
        self.stopping = threading.Event()
        self.thread = None
        self.failure = None
        self.viewer_registration_error = None

    def _lease(self, session):
        if len(session) != 36 or not session.startswith("SES-") or any(c not in "0123456789abcdef" for c in session[4:]):
            raise SandboxManagerError(INVALID_ARGUMENT, "service requires a canonical SCRP session ID")
        if session not in self.leases:
            path = self.state / (session + ".json")
            config.check_contents(self.state)
            if path.exists():
                lease = json.loads(path.read_text(encoding="utf-8"))
            else:
                lease = {"session": session, "capability": secrets.token_hex(32), "start_request_id": str(uuid.uuid4())}
                with path.open("x", encoding="utf-8") as output:
                    json.dump(lease, output)
            if lease.get("session") != session:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, "local management lease mismatch")
            self.leases[session] = lease
        return self.leases[session]

    def rpc(self, session, operation, payload=None, *, request_id=None):
        with self.lock:
            lease = self._lease(session)
            request = {"protocol": "minobox-management-v1", "request_id": request_id or str(uuid.uuid4()),
                       "op": operation, "session": session, "capability": lease["capability"],
                       "payload": payload or {}}
            try:
                process = subprocess.run([str(self.exe), "--client"], input=json.dumps(request).encode("utf-8"),
                    capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
                reply = json.loads(process.stdout.decode("utf-8-sig"))
            except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError) as error:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE,
                    f"management {operation} outcome unknown; request {request['request_id']} not replayed") from error
            if process.returncode or reply.get("ok") is not True:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"management {operation} refused: {reply.get('error', reply)}")
            result = reply["result"]
            if operation in ("start", "status") and isinstance(result, dict) and result.get("id"):
                try:
                    self._register_viewer(session, result)
                    self.viewer_registration_error = None
                except (OSError, ValueError, SandboxManagerError):
                    # Optional UI discovery must not turn an acknowledged VM
                    # start/status into a failed operation or trigger recovery.
                    self.viewer_registration_error = "viewer registration failed; existing VM preserved"
            return result

    def _register_viewer(self, session, record):
        """Publish a locator, never a capability, for the same-user native viewer."""
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return
        if record.get("owner_sid") != self.owner_sid or record.get("label") != session:
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "viewer registration ownership mismatch")
        workspace = str(uuid.UUID(record["id"]))
        container = record.get("container_id")
        if container is not None:
            container = str(uuid.UUID(container))
        folder = Path(local) / "MinoBox" / "management-viewer"
        folder.mkdir(parents=True, exist_ok=True)
        config.check_contents(folder)
        target = folder / (session + ".json")
        value = {"schema_version": 1, "session": session, "root": str(self.vm_root),
                 "owner_sid": self.owner_sid, "workspace_id": workspace,
                 "container_id": container, "lease_path": str(self.state / (session + ".json"))}
        if target.exists():
            previous = json.loads(target.read_text(encoding="utf-8"))
            if any(previous.get(k) != value[k] for k in ("session", "root", "owner_sid", "workspace_id", "lease_path")):
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, "viewer registration binding changed")
            if previous.get("container_id") is not None and container is not None and previous["container_id"] != container:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, "viewer container binding changed")
            # The GUI owns mutation journals and pins the first container itself.
            # Never rewrite its file while it may be committing a stop intent.
            return
        temp = folder / (str(uuid.uuid4()) + ".tmp")
        with temp.open("x", encoding="utf-8") as output:
            json.dump(value, output)
        os.replace(temp, target)

    def start(self, session):
        with self.lock:
            lease = self._lease(session)
            return self.rpc(session, "start", request_id=lease["start_request_id"])

    def publish(self, session, source: Path, bucket: str):
        with self.lock:
            self.mappings[(session, bucket)] = source
            self.sync(session)
            return self.rpc(session, "share", {"bucket": bucket})

    def _put(self, session, bucket, name, data):
        digest = hashlib.sha256(data).hexdigest()
        upload = self.rpc(session, "put_begin", {"bucket": bucket, "name": name, "size": len(data), "sha256": digest})
        for offset in range(0, len(data), 65536):
            acknowledged = self.rpc(session, "put_chunk", {"bucket": bucket, "upload_id": upload["upload_id"],
                "offset": offset, "data": base64.b64encode(data[offset:offset + 65536]).decode("ascii")})
            if acknowledged.get("offset") != min(len(data), offset + 65536):
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, "management chunk acknowledgement mismatch; not replayed")
        committed = self.rpc(session, "put_commit", {"bucket": bucket, "upload_id": upload["upload_id"]})
        if committed.get("published") is not True or committed.get("sha256") != digest or committed.get("size") != len(data):
            raise SandboxManagerError(RUNTIME_UNAVAILABLE, "management publication hash/size mismatch")
        self.hashes[(session, bucket, name)] = digest

    def sync(self, session):
        with self.lock:
            if self.failure:
                raise SandboxManagerError(RUNTIME_UNAVAILABLE, f"management projection stopped after an uncertain update: {self.failure}")
            for (owner, bucket), source in tuple(self.mappings.items()):
                if owner != session:
                    continue
                config.check_contents(source)
                names = set()
                # Publish manifests last so Guest verification sees complete bytes.
                files = sorted((p for p in source.rglob("*") if p.is_file()),
                               key=lambda p: (p.name in {"inputs.json", "bootstrap.json"}, str(p)))
                for path in files:
                    name = path.relative_to(source).as_posix()
                    names.add(name)
                    before = path.stat()
                    if before.st_size > 128 * 1024 * 1024:
                        raise SandboxManagerError(INVALID_ARGUMENT, "service projection file exceeds 128 MiB")
                    # These reads execute in the ordinary Host, never as the High
                    # service. A concurrent replacement cannot expose privileged
                    # files that this caller could not already read.
                    data = path.read_bytes()
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or len(data) != after.st_size:
                        raise SandboxManagerError(RUNTIME_UNAVAILABLE, "source file changed during management publication")
                    digest = hashlib.sha256(data).hexdigest()
                    if self.hashes.get((session, bucket, name)) != digest:
                        self._put(session, bucket, name, data)
                for key in list(self.hashes):
                    if key[:2] == (session, bucket) and key[2] not in names:
                        self.rpc(session, "put_remove", {"bucket": bucket, "name": key[2]})
                        del self.hashes[key]

    def watch(self):
        with self.lock:
            if self.thread:
                return

            def work():
                while not self.stopping.wait(1):
                    try:
                        for session in {key[0] for key in tuple(self.mappings)}:
                            self.sync(session)
                    except Exception as error:
                        # No retry after a transport outcome becomes uncertain.
                        self.failure = str(error)
                        return

            self.thread = threading.Thread(target=work, name="micro-vm-projection", daemon=True)
            self.thread.start()

    def stop_watching(self, session):
        with self.lock:
            for key in list(self.mappings):
                if key[0] == session:
                    del self.mappings[key]
            if not self.mappings:
                self.stopping.set()
