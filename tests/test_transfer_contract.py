import json
import subprocess
from dataclasses import replace
from unittest.mock import Mock

import pytest

from sandbox_manager import programs
from sandbox_manager.errors import SandboxManagerError
from sandbox_manager.transfer_limits import check_service, validate_service_limits


def health(file=536870912, session=2147483648):
    return dict(protocol='minobox-management-v1', transfer_limits=dict(max_file_bytes=file, max_session_bytes=session))


@pytest.mark.parametrize('result', [None, {}, dict(transfer_limits=None), health(134217728, 536870912), health(536870912, 536870912),
                                    health('536870912'), health(True)])
def test_old_missing_or_partial_contract_rejected(result):
    with pytest.raises(ValueError, match='contract mismatch'):
        validate_service_limits(result)


def test_health_checks_live_worker_without_lease(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(dict(ok=True, result=health())).encode()))
    monkeypatch.setattr(subprocess, 'run', run)
    assert check_service('MinoVmService.exe') == health()['transfer_limits']
    request = json.loads(run.call_args.kwargs['input'])
    assert request['op'] == 'health'
    assert 'session' not in request and 'capability' not in request
    assert run.call_count == 1


def gcc(root, complete=True):
    (root/'bin').mkdir(parents=True)
    exe = root/'bin/gcc.exe'
    exe.write_bytes(b'fixture; never executed')
    if complete:
        (root/'include').mkdir()
        for name in ('winhttp.h', '_mingw.h'):
            (root/'include'/name).write_text('fixture')
        (root/'lib/gcc/x86_64-w64-mingw32/16.2.0').mkdir(parents=True)
    return str(exe)


def test_ucrt_precedes_legacy_path_without_host_execution(tmp_path, monkeypatch):
    old = gcc(tmp_path/'MinGW', False)
    modern = gcc(tmp_path/'ucrt64')
    monkeypatch.setitem(programs.PROGRAMS, 'gcc', replace(programs.PROGRAMS['gcc'], known=(modern,)))
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=AssertionError('must not execute Host compiler')))
    assert programs.find_program('gcc', which=lambda _: old).root == (tmp_path/'ucrt64').resolve()


def test_legacy_only_is_refused_with_actionable_reason(tmp_path, monkeypatch):
    old = gcc(tmp_path/'MinGW', False)
    monkeypatch.setitem(programs.PROGRAMS, 'gcc', replace(programs.PROGRAMS['gcc'], known=()))
    with pytest.raises(SandboxManagerError, match='legacy MinGW'):
        programs.find_program('gcc', which=lambda _: old)


def test_custom_mingw64_path_still_works(tmp_path, monkeypatch):
    modern = gcc(tmp_path/'custom')
    monkeypatch.setitem(programs.PROGRAMS, 'gcc', replace(programs.PROGRAMS['gcc'], known=()))
    assert programs.find_program('gcc', which=lambda _: modern).root == (tmp_path/'custom').resolve()
