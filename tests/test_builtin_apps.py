import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sandbox_manager.builtin_apps import stage_notepad, VERIFY_NOTEPAD_PS1
from sandbox_manager.errors import SandboxManagerError


def test_session_copies_only_notepad_and_languages(tmp_path):
    system = tmp_path / "System32"
    (system / "ko-KR").mkdir(parents=True)
    (system / "unrelated").mkdir()
    (system / "notepad.exe").write_bytes(b"fixture exe; never executed")
    (system / "ko-KR" / "notepad.exe.mui").write_bytes(b"Korean resources")
    (system / "unrelated" / "notepad.exe.mui").write_bytes(b"do not expose")
    (system / "private.txt").write_bytes(b"do not expose")
    snapshots = []
    for session in ("B", "C"):
        package = tmp_path / session
        manifest = stage_notepad(package, source=system)
        root = package / "builtin-apps" / "notepad"
        assert {row["path"] for row in manifest} == {"notepad.exe", "ko-KR/notepad.exe.mui"}
        assert json.loads((root / "manifest.json").read_text()) == manifest
        for row in manifest:
            assert hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() == row["sha256"]
        snapshots.append(root)
    (snapshots[0] / "notepad.exe").write_bytes(b"changed B")
    assert (snapshots[1] / "notepad.exe").read_bytes() == (system / "notepad.exe").read_bytes()


def test_missing_host_app_is_preparation_error_without_partial_package(tmp_path):
    with pytest.raises(SandboxManagerError, match="builtin app preparation failed.*notepad"):
        stage_notepad(tmp_path / "package", source=tmp_path)
    assert not (tmp_path / "package").exists()


def test_changed_copy_is_not_published(tmp_path, monkeypatch):
    (tmp_path / "notepad.exe").write_bytes(b"original")
    monkeypatch.setattr("sandbox_manager.builtin_apps.shutil.copyfile",
                        lambda source, target: Path(target).write_bytes(b"different"))
    with pytest.raises(SandboxManagerError, match="hash mismatch"):
        stage_notepad(tmp_path / "package", source=tmp_path)
    assert not (tmp_path / "package/builtin-apps/notepad/manifest.json").exists()


def test_existing_session_copy_is_not_overwritten(tmp_path):
    (tmp_path / "notepad.exe").write_bytes(b"original")
    package = tmp_path / "package"
    stage_notepad(package, source=tmp_path)
    (tmp_path / "notepad.exe").write_bytes(b"updated Host")
    with pytest.raises(SandboxManagerError, match="builtin app preparation failed"):
        stage_notepad(package, source=tmp_path)
    assert (package / "builtin-apps/notepad/notepad.exe").read_bytes() == b"original"


@pytest.mark.skipif(not shutil.which("powershell.exe"), reason="Windows PowerShell 5.1")
@pytest.mark.parametrize("tamper", [False, True])
def test_guest_verifier_reads_every_manifest_entry(tmp_path, tamper):
    system = tmp_path / "System32"
    (system / "ko-KR").mkdir(parents=True)
    (system / "notepad.exe").write_bytes(b"not executable")
    (system / "ko-KR/notepad.exe.mui").write_bytes(b"resource")
    package = tmp_path / "package"
    stage_notepad(package, source=system)
    root = package / "builtin-apps/notepad"
    if tamper:
        (root / "ko-KR/notepad.exe.mui").write_bytes(b"changed")
    script = tmp_path / "verify.ps1"
    script.write_text("$ErrorActionPreference='Stop'\n" + VERIFY_NOTEPAD_PS1.replace(
        r"C:\RunnerPackage\builtin-apps\notepad", str(root).replace("'", "''")), encoding="utf-8-sig")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                            capture_output=True, timeout=20)
    assert (result.returncode != 0) == tamper, result.stderr.decode(errors="replace")
    if tamper:
        assert b"Notepad Guest copy hash mismatch" in result.stderr
