"""Ownership failures must never reach a VM mutation (no real VM in these tests)."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from sandbox_manager.manager import RUNNING, SandboxSession
from sandbox_manager.micro_vm import MicroVmManager, guid
from sandbox_manager.errors import SandboxManagerError


@pytest.fixture
def bound(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "dbictl.exe").write_bytes(b"not executed")
    relay = package / "ScrpRelay.exe"
    relay.write_bytes(b"not executed")
    vm_root = tmp_path / "vm"
    (vm_root / "workspaces").mkdir(parents=True)
    manager = MicroVmManager(tmp_path / "manager", package=package, vm_root=vm_root, relay=relay)
    manager._command = Mock(side_effect=AssertionError("VM command must not be sent"))
    workspace = "139fb9f9-7dfb-4085-bc74-cec301255d60"
    container = "6cecc5f1-19c7-4ae1-91d0-ff18950e5270"
    session = SandboxSession("SES-owned", "RT-SBX-001", 1, tmp_path / "session", state=RUNNING,
        sandbox_id=workspace, micro_vm={"root": str(vm_root), "workspace_id": workspace,
        "container_id": container, "owner_sid": "S-1-5-21-1", "label": "SES-owned"})
    session.dir.mkdir()
    record = {"id": workspace, "container_id": container, "owner_sid": "S-1-5-21-1",
              "label": "SES-owned", "state": "Running"}
    path = vm_root / "workspaces" / (workspace + ".json")
    path.write_text(json.dumps(record), encoding="utf-8")
    return manager, session, path, record


@pytest.mark.parametrize("field,value", [("container_id", "5d48dced-5d00-4614-aa8c-1ca8ee2c2282"),
    ("owner_sid", "S-1-5-21-2"), ("label", "SES-other"), ("id", "139fb9f9-7dfb-4085-bc74-cec301255d61")])
def test_stop_refuses_changed_ownership(bound, field, value):
    manager, session, path, record = bound
    record[field] = value
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(SandboxManagerError, match="ownership mismatch"):
        manager.stop(session, "USER_STOP")
    manager._command.assert_not_called()


def test_viewer_disappearance_is_not_vm_termination(bound):
    manager, session, _, _ = bound
    assert manager.window_state(session) == "UNAVAILABLE"
    assert manager.is_running(session)
    manager._command.assert_not_called()


def test_reset_never_reassigns_owned_session(bound):
    manager, session, _, _ = bound
    identity = dict(session.micro_vm)
    with pytest.raises(SandboxManagerError, match="fixed until explicit"):
        manager.reset_sandbox(session, 2)
    assert session.micro_vm == identity
    assert session.generation == 1
    manager._command.assert_not_called()


@pytest.mark.parametrize("value", ["139fb9f9", "{139fb9f9-7dfb-4085-bc74-cec301255d60}",
                                  "00000000-0000-0000-0000-000000000000"])
def test_abbreviated_or_zero_identity_is_rejected(value):
    with pytest.raises(ValueError): guid(value)
