"""Allocation decisions with fake VM backends; no real VM is started."""
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from sandbox_manager.automatic import AutomaticVmManager
from sandbox_manager.errors import SandboxManagerError
from sandbox_manager.manager import SandboxSession, PREPARED


@pytest.fixture
def allocation(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    (package / "dbictl.exe").write_bytes(b"not executable")
    relay = package / "ScrpRelay.exe"
    relay.write_bytes(b"not executable")
    wsb = Mock()
    wsb.running.return_value = set()
    manager = AutomaticVmManager(tmp_path / "manager", package=package, vm_root=tmp_path / "vm",
                                 relay=relay, wsb=wsb, allow_app_window=True)
    session = SandboxSession("SES-test", "RT-test", 1, tmp_path / "session")
    session.dir.mkdir()
    held = []
    @contextmanager
    def slot():
        held.append(True)
        try: yield
        finally: held.pop()
    monkeypatch.setattr("sandbox_manager.automatic.windows_sandbox_slot", slot)
    def start_wsb(s):
        assert held
        s.sandbox_id = "owned-wsb"
        return "172.16.0.1"
    manager._start_reserved = Mock(side_effect=start_wsb)
    manager.micro.start = Mock(return_value="127.0.0.1")
    return manager, session, wsb


def test_free_wsb_is_reserved_through_start(allocation):
    manager, session, _ = allocation
    assert manager.start(session) == "172.16.0.1"
    assert session.runtime_type == "WINDOWS_SANDBOX"
    manager.micro.start.assert_not_called()


def test_busy_wsb_selects_micro_without_mutating_existing_vm(allocation):
    manager, session, wsb = allocation
    wsb.running.return_value = {"other-wsb"}
    assert manager.start(session) == "127.0.0.1"
    assert session.runtime_type == "MICRO_VM_APP_WINDOW"
    assert manager.guest_input_root == r"C:\RunnerInput"
    manager._start_reserved.assert_not_called()
    assert wsb.method_calls == [("running", (), {})]
    wsb.running.return_value = set()
    with pytest.raises(SandboxManagerError, match="reassignment"):
        manager.start(session)
    manager.micro.start.assert_called_once_with(session)


@pytest.mark.parametrize("where", ["query", "start"])
def test_failure_never_switches_backends(allocation, where):
    manager, session, wsb = allocation
    if where == "query": wsb.running.side_effect = OSError("unknown occupancy")
    else: manager._start_reserved.side_effect = OSError("resource exhaustion")
    with pytest.raises(OSError): manager.start(session)
    manager.micro.start.assert_not_called()
    assert session.state == PREPARED
    assert session.sandbox_id is None


def test_micro_failure_and_reset_cannot_reassign(allocation):
    manager, session, wsb = allocation
    wsb.running.return_value = {"other-wsb"}
    manager.micro.start.side_effect = OSError("resource exhaustion")
    with pytest.raises(OSError): manager.start(session)
    with pytest.raises(SandboxManagerError, match="fixed"):
        manager.reset_sandbox(session, 2)
    manager._start_reserved.assert_not_called()


def test_app_window_permission_required(tmp_path):
    with pytest.raises(SandboxManagerError, match="explicit app-window"):
        AutomaticVmManager(tmp_path, package=tmp_path, vm_root=tmp_path, relay=tmp_path)
