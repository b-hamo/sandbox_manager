"""Real Windows kernel coordination in the test VM; no real WSB is started."""
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from sandbox_manager.allocation import windows_sandbox_slot
from sandbox_manager.errors import SandboxManagerError


def test_another_process_cannot_allocate_until_owner_releases():
    root = str(Path(__file__).resolve().parents[1])
    code = (
        f"import sys; sys.path.insert(0, {root!r})\n"
        "from sandbox_manager.allocation import windows_sandbox_slot\n"
        "from sandbox_manager.errors import SandboxManagerError\n"
        "try:\n"
        " with windows_sandbox_slot(): print('acquired')\n"
        "except SandboxManagerError as exc:\n"
        " print(exc.code); sys.exit(23)\n"
    )
    with windows_sandbox_slot():
        contender = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
        assert contender.returncode == 23, contender.stderr
        assert contender.stdout.strip() == "RUNTIME_UNAVAILABLE"
    successor = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert successor.returncode == 0, successor.stderr
    assert successor.stdout.strip() == "acquired"


def test_another_thread_is_rejected_and_failure_releases_lock():
    errors = []
    def contender():
        try:
            with windows_sandbox_slot():
                errors.append("unexpected allocation")
        except SandboxManagerError as exc:
            errors.append(exc.code)
    with pytest.raises(RuntimeError, match="injected"):
        with windows_sandbox_slot():
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
            assert errors == ["RUNTIME_UNAVAILABLE"]
            raise RuntimeError("injected start failure")
    with windows_sandbox_slot():
        pass
