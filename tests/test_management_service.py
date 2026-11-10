"""Client byte-transfer/no-replay contracts; these do not start a real service."""
import base64
import hashlib
import json
import subprocess
from unittest.mock import Mock

import pytest

from sandbox_manager.management_service import ManagementService
from sandbox_manager.errors import SandboxManagerError


@pytest.fixture
def client(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "MinoVmService.exe").write_bytes(b"not executed")
    vm = tmp_path / "vm"
    (package / "management.json").write_text(json.dumps({"schema_version": 1, "root": str(vm)}))
    return ManagementService(package, vm.resolve(), tmp_path / "state")


SESSION = "SES-" + "1" * 32


def test_start_uses_persisted_request_and_capability(client, monkeypatch):
    sent = []
    def run(args, **kwargs):
        sent.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(args, 0, json.dumps({"ok": True, "result": {"id": "bound"}}).encode())
    monkeypatch.setattr(subprocess, "run", run)
    client.start(SESSION)
    client.leases.clear()
    client.start(SESSION)
    assert sent[0] == sent[1]
    assert len(sent[0]["capability"]) == 64
    assert sent[0]["op"] == "start" and sent[0]["payload"] == {}


def test_timeout_never_reissues_request(client, monkeypatch):
    run = Mock(side_effect=subprocess.TimeoutExpired("management-client", 45))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SandboxManagerError, match="not replayed"):
        client.start(SESSION)
    assert run.call_count == 1


def test_publication_sends_bytes_not_host_paths(client, tmp_path):
    source = tmp_path / "host-source"
    source.mkdir()
    content = b"ordinary caller bytes" * 6000
    (source / "same.txt").write_bytes(content)
    received = bytearray()
    calls = []
    def rpc(session, operation, payload=None, **unused):
        assert session == SESSION
        calls.append((operation, payload))
        assert "path" not in payload and str(source) not in json.dumps(payload)
        if operation == "put_begin":
            assert payload["size"] == len(content)
            return {"upload_id": "upload", "offset": 0}
        if operation == "put_chunk":
            assert payload["offset"] == len(received)
            received.extend(base64.b64decode(payload["data"]))
            return {"offset": len(received)}
        if operation == "put_commit":
            return {"published": True, "size": len(received), "sha256": hashlib.sha256(received).hexdigest()}
        if operation == "share":
            return {"share_id": "share"}
        if operation == "put_remove":
            return {"removed": True}
        raise AssertionError(operation)
    client.rpc = rpc
    assert client.publish(SESSION, source, "inbox") == {"share_id": "share"}
    assert received == content
    count = len(calls)
    client.sync(SESSION)
    assert len(calls) == count
    (source / "same.txt").unlink()
    client.sync(SESSION)
    assert calls[-1] == ("put_remove", {"bucket": "inbox", "name": "same.txt"})


def test_wrong_commit_hash_is_not_accepted(client):
    client.rpc = Mock(side_effect=[{"upload_id": "u"}, {"offset": 3},
        {"published": True, "size": 3, "sha256": "0" * 64}])
    with pytest.raises(SandboxManagerError, match="hash/size mismatch"):
        client._put(SESSION, "inbox", "same.txt", b"abc")
    assert client.hashes == {}
    assert client.rpc.call_count == 3


def test_projection_error_stops_instead_of_replaying(client):
    client.failure = "uncertain chunk"
    client.rpc = Mock()
    with pytest.raises(SandboxManagerError, match="uncertain update"):
        client.sync(SESSION)
    client.rpc.assert_not_called()


def test_medium_status_uses_authorized_rpc_not_privileged_state_files(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from sandbox_manager.micro_vm import MicroVmManager
    manager = object.__new__(MicroVmManager)
    manager.vm_root = tmp_path / "protected"
    workspace = "12345678-1234-1234-1234-123456789abc"
    record = dict(id=workspace, label=SESSION, container_id="22345678-1234-1234-1234-123456789abc", owner_sid="owner", state="Running")
    manager.management = SimpleNamespace(rpc=Mock(return_value=record))
    session = SimpleNamespace(session_id=SESSION, sandbox_id=workspace,
        micro_vm=dict(root=str(manager.vm_root), workspace_id=workspace, **{k: record[k] for k in ('label', 'container_id', 'owner_sid')}))
    monkeypatch.setattr(Path, 'read_text', Mock(side_effect=PermissionError('protected state')))
    assert manager._record(session) == record
    manager.management.rpc.assert_called_once_with(SESSION, "status")
    manager.management.rpc.return_value = dict(record, id="other-workspace")
    with pytest.raises(SandboxManagerError, match="different session/VM"):
        manager._record(session)
    manager.management.rpc.return_value = dict(record, container_id="other-container")
    with pytest.raises(SandboxManagerError, match="ownership mismatch"):
        manager._record(session)
