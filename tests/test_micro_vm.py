"""Ownership failures must never reach a VM mutation (no real VM in these tests)."""
import json
from pathlib import Path
from unittest.mock import Mock
from contextlib import contextmanager

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


@pytest.mark.parametrize("state,clean,denied", [("Running", False, True), ("Preparing", False, True),
    ("NeedsReconcile", False, True), ("Stopped", False, True), ("Failed", False, True),
    ("Stopped", True, False), ("Failed", True, False)])
def test_capacity_includes_pending_and_uncertain_cleanup(bound, state, clean, denied):
    manager, _, path, record = bound
    manager.max_active = 1
    record.update(state=state, cleanup_confirmed=clean)
    path.write_text(json.dumps(record), encoding="utf-8")
    if denied:
        with pytest.raises(SandboxManagerError, match="capacity exhausted"):
            manager._check_capacity()
    else:
        manager._check_capacity()
    manager._command.assert_not_called()


def test_capacity_does_not_admit_corrupt_record(bound):
    manager, _, path, _ = bound
    path.write_text("{partial", encoding="utf-8")
    with pytest.raises(SandboxManagerError, match="cannot be verified"):
        manager._check_capacity()
    manager._command.assert_not_called()


def test_start_checks_budget_under_lock_before_vm_command(bound, monkeypatch):
    manager, session, _, _ = bound
    session.state = "PREPARED"
    session.sandbox_id = None
    session.micro_vm = None
    manager.max_active = 1
    held = []
    @contextmanager
    def slot():
        held.append(True)
        try: yield
        finally: held.pop()
    monkeypatch.setattr("sandbox_manager.micro_vm.micro_vm_slot", slot)
    check_capacity = manager._check_capacity
    def checked():
        assert held
        check_capacity()
    monkeypatch.setattr(manager, "_check_capacity", checked)
    with pytest.raises(SandboxManagerError, match="capacity exhausted"):
        manager.start(session)
    assert not held
    assert session.sandbox_id is None and session.micro_vm is None
    manager._command.assert_not_called()


def uncreated_record(bound):
    manager, session, path, record = bound
    record.update(state="Failed", container_id=None, cleanup_confirmed=True,
                  runtime_creation="not_attempted", result=dict(cleaned_up=True,
                  runtime_creation="not_attempted", error=dict(code="host_contract_unverified")))
    path.write_text(json.dumps(record), encoding="utf-8")
    (manager.vm_root / "owner.json").write_text(json.dumps({"sid": record["owner_sid"]}))
    return manager, session, path, record


def test_preparation_failure_preserves_id_and_confirms_no_vm_without_stop(bound):
    manager, session, _, record = uncreated_record(bound)
    session.state, session.sandbox_id, session.micro_vm = "PREPARED", None, None
    manager._command = Mock(return_value={"id": record["id"]})
    with pytest.raises(SandboxManagerError, match="host_contract_unverified"):
        manager.start(session)
    assert session.sandbox_id == record["id"]
    assert session.micro_vm["container_id"] is None
    manager._command.reset_mock()
    manager.stop(session, "RUNTIME_ERROR", emergency=True)
    assert session.state == "TERMINATED"
    manager._command.assert_not_called()


def test_configured_start_budget_waits_for_slow_image_without_second_start(bound):
    manager, session, path, record = bound
    session.state, session.sandbox_id, session.micro_vm = "PREPARED", None, None
    manager.startup_timeout_s = 600
    now = [0]
    manager.clock = lambda: now[0]
    ready = dict(record)
    record.update(state="Preparing", container_id=None)
    path.write_text(json.dumps(record))
    def advance(_):
        now[0] += 181
        path.write_text(json.dumps(ready))
    manager.sleep = advance
    manager._command = Mock(return_value={"id": ready["id"]})
    manager._control = Mock(return_value={})
    assert manager.start(session) == "127.0.0.1"
    assert session.state == "STARTED" and session.sandbox_id == ready["id"]
    assert now[0] == 181
    manager._command.assert_called_once()


@pytest.mark.parametrize("timeout", [0, True, float("nan"), float("inf"), 3601])
def test_invalid_start_budget_rejected_before_start(bound, timeout):
    manager, _, _, _ = bound
    with pytest.raises(SandboxManagerError, match="startup_timeout_s"):
        MicroVmManager(manager.root, package=manager.package, vm_root=manager.vm_root,
                       relay=manager.relay, startup_timeout_s=timeout)


@pytest.mark.parametrize("change", [{"cleanup_confirmed": False}, {"runtime_creation": "attempted"},
    {"state": "NeedsReconcile"}, {"result": {}}, {"owner_sid": "S-1-5-21-other"}])
def test_uncreated_binding_requires_complete_cleanup_proof(bound, change):
    manager, session, path, record = uncreated_record(bound)
    session.micro_vm["container_id"] = None
    record.update(change)
    path.write_text(json.dumps(record))
    with pytest.raises(SandboxManagerError):
        manager.stop(session, "RUNTIME_ERROR", emergency=True)
    manager._command.assert_not_called()


def tracked_runner(bound):
    manager, session, _, _ = bound
    original = dict(run_id=2, pid=42, pending=False, agent_pid=10,
                    process_status=dict(state="RUNNING", creation_time="123456"))
    session.micro_vm.update(process_status_v1=True, agent_pid=10, agent_creation_time="111",
                            runner_process=original, runner_launch_requested=True, output_storage_generation=1)
    row = dict(original, process_status=dict(state="EXITED", creation_time="123456", exit_code=259))
    info = dict(pid=10, agent_creation_time="111", run_process_status=True, runs=[row])
    manager._control = Mock(return_value=info)
    return manager, session, row, info


@pytest.mark.parametrize("change", [{"state": "RUNNING"}, {"state": "UNKNOWN"}, {"creation_time": "reused"}])
def test_recovery_needs_original_runner_exit(bound, change):
    manager, session, row, _ = tracked_runner(bound)
    row["process_status"].update(change)
    with pytest.raises(SandboxManagerError, match="confirmed old process termination"):
        manager.restart_runner(session, 2)
    assert session.generation == 1 and session.micro_vm["runner_launch_requested"]
    manager._command.assert_not_called()


@pytest.mark.parametrize("change", [{"pid": 11}, {"agent_creation_time": "222"}, {"runs": []}])
def test_missing_or_reused_agent_never_proves_exit(bound, change):
    manager, session, _, info = tracked_runner(bound)
    info.update(change)
    with pytest.raises(SandboxManagerError):
        manager.restart_runner(session, 2)
    assert session.generation == 1
    manager._command.assert_not_called()


def test_confirmed_exit_advances_only_runner_generation(bound):
    manager, session, _, _ = tracked_runner(bound)
    vm = (session.sandbox_id, session.micro_vm["container_id"])
    manager.restart_runner(session, 2)
    assert (session.sandbox_id, session.micro_vm["container_id"]) == vm
    assert session.generation == 2 and session.state == "STARTED"
    assert session.micro_vm["output_storage_generation"] == 1
    assert session.micro_vm["recovery_pending"]
    assert "runner_launch_requested" not in session.micro_vm
    assert session.micro_vm["retired_runners"][0]["pid"] == 42
    manager._command.assert_not_called()


def test_runner_launch_lost_reply_is_never_replayed(bound):
    manager, session, _, _ = tracked_runner(bound)
    session.micro_vm.pop("runner_launch_requested")
    manager._control.side_effect = TimeoutError("lost reply")
    with pytest.raises(TimeoutError): manager._launch_runner(session)
    with pytest.raises(SandboxManagerError, match="refusing duplicate"): manager._launch_runner(session)
    assert manager._control.call_count == 1
