import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sandbox_manager.app_preparation import prepare_apps, copy_program, verify_and_link, bundle_program, unpack_and_link
from sandbox_manager.errors import SandboxManagerError
from sandbox_manager.manager import SandboxSession
from sandbox_manager.programs import FoundProgram


@pytest.fixture
def setup(tmp_path):
    session = SandboxSession("SES-apps", "RT-apps", 1, tmp_path / "session", state="RUNNING")
    session.package_dir.mkdir(parents=True)
    manager = SimpleNamespace(_app_guest_exit=Mock(return_value=0), _sync_app_package=Mock(), _event=Mock())
    return manager, session


def test_guest_app_is_used_without_host_copy(setup):
    manager, session = setup
    result = prepare_apps(manager, session, ["메모장", "powershell"])
    assert result["prepared"] and not result["workload_started"]
    assert all(row["source"] == "guest" for row in result["apps"])
    manager._sync_app_package.assert_not_called()
    assert not list(session.package_dir.iterdir())


def test_language_copy_verified_before_available_and_reused(setup, tmp_path, monkeypatch):
    manager, session = setup
    root = tmp_path / "Python"
    root.mkdir()
    (root / "python.exe").write_bytes(b"fixture, never executed")
    (root / "python313.dll").write_bytes(b"dependency")
    monkeypatch.setattr("sandbox_manager.app_preparation.find_program",
                        lambda *args, **kwargs: FoundProgram("python", root, "python.exe"))
    manager._app_guest_exit.side_effect = [3, 0, 0]
    result = prepare_apps(manager, session, ["python"])
    assert result["prepared"] and result["apps"][0]["exe"] == r"C:\Tools\python\python.exe"
    with zipfile.ZipFile(session.package_dir / "prepared-apps/python.zip") as archive:
        assert len(json.loads(archive.read("app-files.json"))) == 2
    assert "Get-FileHash" in manager._app_guest_exit.call_args_list[1].args[1]
    assert prepare_apps(manager, session, ["python"])["prepared"]
    manager._sync_app_package.assert_called_once()
    assert (root / "python.exe").read_bytes() == b"fixture, never executed"


def test_absent_host_program_requests_guest_install_before_workload(setup, monkeypatch):
    manager, session = setup
    manager._app_guest_exit.return_value = 3
    def missing(*args, **kwargs):
        raise SandboxManagerError("INVALID_ARGUMENT", "python is not installed on this PC")
    monkeypatch.setattr("sandbox_manager.app_preparation.find_program", missing)
    result = prepare_apps(manager, session, ["python"])
    assert not result["prepared"]
    assert result["apps"][0]["status"] == "NEEDS_INSTALL"
    assert not result["workload_started"]
    manager._sync_app_package.assert_not_called()


def test_lost_guest_reply_is_not_replayed(setup):
    manager, session = setup
    manager._app_guest_exit.side_effect = SandboxManagerError("RUNTIME_UNAVAILABLE", "reply lost")
    first = prepare_apps(manager, session, ["notepad"])
    second = prepare_apps(manager, session, ["notepad"])
    assert first["apps"][0]["status"] == second["apps"][0]["status"] == "UNKNOWN"
    manager._app_guest_exit.assert_called_once()


def test_persisted_pending_attempt_is_not_restarted(setup):
    manager, session = setup
    session.app_preparations["notepad"] = {"app": "notepad", "status": "PREPARING", "prepared": False}
    assert prepare_apps(manager, session, ["notepad"])["apps"][0]["status"] == "UNKNOWN"
    manager._app_guest_exit.assert_not_called()


def test_host_path_is_not_an_application_recipe(setup):
    manager, session = setup
    result = prepare_apps(manager, session, [r"C:\Users\private.exe"])
    assert result["apps"][0]["status"] == "UNSUPPORTED"
    manager._app_guest_exit.assert_not_called()


@pytest.mark.skipif(not shutil.which("powershell.exe"), reason="Windows PowerShell 5.1")
@pytest.mark.parametrize("tamper", [False, True])
def test_copied_dependency_manifest_is_checked_in_powershell(tmp_path, tamper):
    source, target = tmp_path / "source", tmp_path / "target"
    (source / "Lib").mkdir(parents=True)
    (source / "python.exe").write_bytes(b"fixture, not executed")
    (source / "Lib/stdlib.py").write_bytes(b"dependency")
    copy_program(source, target)
    if tamper:
        (target / "Lib/stdlib.py").write_bytes(b"modified")
    script = tmp_path / "verify.ps1"
    # Already exists: the test never creates a Host symlink or runs an app.
    script.write_text(verify_and_link(str(target), str(target), str(target / "python.exe")), encoding="utf-8-sig")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            capture_output=True, timeout=20)
    assert (result.returncode != 0) == tamper, result.stderr.decode(errors="replace")
    if tamper:
        assert b"App hash mismatch" in result.stderr


@pytest.mark.skipif(not shutil.which("powershell.exe"), reason="Windows PowerShell 5.1")
@pytest.mark.parametrize("escape", [False, True])
def test_bundle_extracts_verified_files_and_refuses_zip_traversal(tmp_path, escape):
    import hashlib
    source = tmp_path / "source"
    source.mkdir()
    (source / "python.exe").write_bytes(b"fixture, never executed")
    archive = tmp_path / "package" / "python.zip"
    digest = bundle_program(source, tmp_path / "staging", archive)
    if escape:
        with zipfile.ZipFile(archive, "a") as package:
            package.writestr("../escaped.txt", "must not extract")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    destination = tmp_path / "extracted"
    script = tmp_path / "unpack.ps1"
    script.write_text(unpack_and_link(str(archive), digest, str(destination), str(destination),
                                     str(destination / "python.exe")), encoding="utf-8-sig")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            capture_output=True, timeout=20)
    assert (result.returncode != 0) == escape, result.stderr.decode(errors="replace")
    assert not (tmp_path / "escaped.txt").exists()
    if escape:
        assert not destination.exists()
    else:
        assert (destination / "python.exe").read_bytes() == b"fixture, never executed"
