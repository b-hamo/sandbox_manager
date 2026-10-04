"""SandboxManager without a real Sandbox: fake `wsb`, network, firewall and clock.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox_manager import (FAILED, PREPARED, RESTARTING, RUNNING, STARTED, TERMINATED,  # noqa: E402
                             SandboxManager, SandboxManagerError)
from sandbox_manager import config  # noqa: E402
from sandbox_manager.firewall import Rule, evaluate  # noqa: E402

CERT = "-----BEGIN CERTIFICATE-----\nMIIBdGVzdA==\n-----END CERTIFICATE-----\n"
TOKEN = "tok-" + "x" * 40
SBX = "11111111-2222-3333-4444-555555555555"
SWITCH = "vEthernet (Default Switch)"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeWsb:
    def __init__(self, guest_ip="192.168.212.199", ip_after=1, stop_works=True):
        self.guest_ip, self.ip_after, self.stop_works = guest_ip, ip_after, stop_works
        self.live: set[str] = set()
        self.started_xml: list[str] = []
        self.connected: list[str] = []
        self.ip_calls = 0
        self.exec_calls: list[tuple[str, str]] = []
        self.exec_code = 0

    def start(self, xml):
        self.started_xml.append(xml)
        n = len(self.started_xml)
        sid = SBX if n == 1 else f"{n:08d}-2222-3333-4444-555555555555"   # a reset gets a new Sandbox
        self.live.add(sid)
        return sid

    def exec(self, sid, command):
        self.exec_calls.append((sid, command))
        return self.exec_code

    def running(self):
        return set(self.live)

    def ip(self, sid):
        self.ip_calls += 1
        return self.guest_ip if self.ip_calls >= self.ip_after else None

    def connect(self, sid):
        self.connected.append(sid)

    def stop(self, sid):
        if self.stop_works:
            self.live.discard(sid)


class FakeNet:
    def __init__(self, switch="192.168.208.1", toward=None):
        self.switch, self._toward = switch, toward or switch

    def switch_ipv4(self):
        return self.switch

    def toward(self, guest_ip):
        return self._toward


class FakeFirewall:
    def __init__(self, problems=()):
        self._p = list(problems)

    def problems(self):
        return list(self._p)


class FakeProcesses:
    """pid -> creation time of live processes. A pid in `hidden` exists but cannot be inspected."""

    def __init__(self):
        self.table = {os.getpid(): 111}
        self.hidden: set[int] = set()

    def __call__(self, pid):
        if pid in self.hidden:
            raise OSError(5, "access denied")
        return self.table.get(pid)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.runner = Path(self.tmp.name) / "sandbox_runner.exe"
        self.runner.write_bytes(b"MZ fake runner")
        self.clock = FakeClock()
        self.wsb = FakeWsb()
        self.procs = FakeProcesses()
        self.env = mock.patch.dict(os.environ, {"OneDrive": str(Path(self.tmp.name) / "OneDrive")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def manager(self, net=None, firewall=None):
        return SandboxManager(self.root, wsb=self.wsb, network=net or FakeNet(), firewall=firewall or FakeFirewall(),
                              clock=self.clock, sleep=self.clock.sleep, ip_wait_s=10, stop_verify_s=5,
                              process_started=self.procs)

    def started(self, m=None):
        m = m or self.manager()
        s = m.prepare("SES-20260929-001", "RT-SBX-001", 1, self.runner)
        m.start(s)
        return m, s

    def host_writes_bootstrap(self, s):
        s.bootstrap_path.write_text(json.dumps({"token": TOKEN, "host": None}), encoding="utf-8")

    def assertCode(self, code, fn, *a, **kw):
        with self.assertRaises(SandboxManagerError) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.code, code)
        return cm.exception


class HappyPath(Base):
    def test_full_lifecycle(self):
        m = self.manager()
        s = m.prepare("SES-20260929-001", "RT-SBX-001", 1, self.runner)
        self.assertEqual((s.state, s.host_address), (PREPARED, None))    # address unknown before start
        for name in ("sandbox_runner.exe", "start.ps1"):
            self.assertTrue((s.package_dir / name).is_file(), name)
        self.assertRegex(s.runner_sha256, r"^[0-9a-f]{64}$")

        address = m.start(s)
        self.assertEqual((s.state, address, s.host_address), (STARTED, "192.168.208.1", "192.168.208.1"))
        self.assertEqual(self.wsb.connected, [SBX])            # window opened so LogonCommand runs
        self.assertFalse(s.ready_path.exists())                # Guest keeps waiting

        self.host_writes_bootstrap(s)
        m.publish_bootstrap(s, CERT)
        self.assertEqual(s.state, RUNNING)
        self.assertEqual((s.bootstrap_dir / config.ADDRESS_NAME).read_text(), "192.168.208.1")
        self.assertTrue((s.bootstrap_dir / config.CERT_NAME).is_file())
        self.assertTrue(s.ready_path.exists())

        m.mark_ready(s)
        self.assertFalse(s.bootstrap_path.exists())            # spent token not left for the Guest
        self.assertFalse(s.ready_path.exists())

        m.stop(s, "TASK_COMPLETE")
        self.assertEqual(s.state, TERMINATED)
        self.assertNotIn(SBX, self.wsb.live)
        self.assertEqual(s.events[-1]["verified_by"], "wsb list")

        result = m.cleanup(s)
        self.assertEqual(result["failed"], [])
        self.assertFalse(s.package_dir.exists())
        self.assertFalse(s.wsb_path.exists())
        self.assertTrue((s.dir / "state.json").exists())       # audit record stays

    def test_ready_marker_is_written_last(self):
        m, s = self.started()
        self.host_writes_bootstrap(s)
        order = []
        real_write_bytes, real_write_text = Path.write_bytes, Path.write_text

        def wb(p, *a, **k):
            order.append(p.name)
            return real_write_bytes(p, *a, **k)

        def wt(p, *a, **k):
            order.append(p.name)
            return real_write_text(p, *a, **k)

        with mock.patch.object(Path, "write_bytes", wb), mock.patch.object(Path, "write_text", wt):
            m.publish_bootstrap(s, CERT)
        order = [n for n in order if not n.startswith("state.json")]   # Host-only audit record, not seen by the Guest
        self.assertEqual(order[-1], config.READY_NAME)
        self.assertLess(order.index(config.CERT_NAME), order.index(config.READY_NAME))
        m.stop(s, "TASK_COMPLETE")

    def test_state_file_never_contains_token(self):
        m, s = self.started()
        self.host_writes_bootstrap(s)
        m.publish_bootstrap(s, CERT)
        m.stop(s, "USER_STOP")
        self.assertNotIn(TOKEN, (s.dir / "state.json").read_text(encoding="utf-8"))

    def test_load_round_trip(self):
        m, s = self.started()
        again = m.load(s.session_id)
        self.assertEqual((again.state, again.sandbox_id, again.host_address), (STARTED, SBX, "192.168.208.1"))
        m.stop(again, "TASK_COMPLETE")

    def test_waits_for_guest_ip(self):
        self.wsb.ip_after = 3
        m, s = self.started()
        self.assertEqual(self.wsb.ip_calls, 3)
        m.stop(s, "TASK_COMPLETE")

    def test_stop_is_idempotent(self):
        m, s = self.started()
        m.stop(s, "TASK_COMPLETE")
        m.stop(s, "USER_STOP")
        self.assertEqual(s.termination_reason, "TASK_COMPLETE")


class WsbConfig(Base):
    def test_only_two_read_only_mappings_inside_workspace(self):
        m, s = self.started()
        xml = self.wsb.started_xml[0]
        hosts = re.findall(r"<HostFolder>(.*?)</HostFolder>", xml)
        self.assertEqual(sorted(Path(h) for h in hosts), sorted([s.package_dir.resolve(), s.bootstrap_dir.resolve()]))
        self.assertEqual(xml.count("<ReadOnly>true</ReadOnly>"), 2)
        self.assertNotIn("<ReadOnly>false", xml)
        for off in ("ClipboardRedirection", "PrinterRedirection", "AudioInput", "VideoInput"):
            self.assertIn(f"<{off}>Disable</{off}>", xml)
        self.assertNotIn("HostIp", xml)                        # nothing session-specific on the command line
        m.stop(s, "TASK_COMPLETE")

    def test_refuses_mapping_outside_workspace(self):
        ws = self.root / "sessions" / "X"
        ws.mkdir(parents=True)
        self.assertCode("INVALID_ARGUMENT", config.build_wsb, [config.Mapping(Path.home(), r"C:\x")], "cmd", ws)

    def test_check_ipv4(self):
        self.assertEqual(config.check_ipv4("192.168.208.1"), "192.168.208.1")
        for bad in ("192.168.208.1 -Evil 1", "1.2.3.4;calc", "host.local", ""):
            with self.assertRaises(ValueError):
                config.check_ipv4(bad)

    def test_guest_script_waits_for_marker_and_validates_address(self):
        self.assertIn("bootstrap.ready", config.START_PS1)
        self.assertIn("TryParse", config.START_PS1)
        self.assertLess(config.START_PS1.index("bootstrap.ready"), config.START_PS1.index("sandbox_runner.exe"))


class Refusals(Base):
    def test_bad_arguments(self):
        m = self.manager()
        self.assertCode("INVALID_ARGUMENT", m.prepare, "../evil", "RT-SBX-001", 1, self.runner)
        self.assertCode("INVALID_ARGUMENT", m.prepare, "SES-1", "RT SBX", 1, self.runner)
        self.assertCode("INVALID_ARGUMENT", m.prepare, "SES-1", "RT-SBX-001", 0, self.runner)
        self.assertCode("INVALID_ARGUMENT", m.prepare, "SES-1", "RT-SBX-001", True, self.runner)
        self.assertCode("INVALID_ARGUMENT", m.prepare, "SES-1", "RT-SBX-001", 1, self.runner.with_name("no.exe"))

    def test_bad_certificates(self):
        m, s = self.started()
        self.host_writes_bootstrap(s)
        self.assertCode("INVALID_ARGUMENT", m.publish_bootstrap, s, "not a pem")
        leaked = CERT + "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
        self.assertCode("INVALID_ARGUMENT", m.publish_bootstrap, s, leaked)
        self.assertFalse(s.ready_path.exists())
        m.stop(s, "TASK_COMPLETE")

    def test_publish_needs_complete_bootstrap(self):
        m, s = self.started()
        self.assertCode("INVALID_ARGUMENT", m.publish_bootstrap, s, CERT)          # not written yet
        s.bootstrap_path.write_text('{"token": "half', encoding="utf-8")
        self.assertCode("INVALID_ARGUMENT", m.publish_bootstrap, s, CERT)          # half written
        self.assertFalse(s.ready_path.exists())
        m.stop(s, "TASK_COMPLETE")

    def test_duplicate_session(self):
        m = self.manager()
        m.prepare("SES-1", "RT-SBX-001", 1, self.runner)
        self.assertCode("INVALID_ARGUMENT", m.prepare, "SES-1", "RT-SBX-002", 1, self.runner)

    def test_one_sandbox_per_pc(self):
        self.wsb.live.add("99999999-2222-3333-4444-555555555555")
        m = self.manager()
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner)
        self.assertCode("RUNTIME_UNAVAILABLE", m.start, s)
        self.assertEqual(s.state, PREPARED)

    def test_workspace_in_onedrive_refused(self):
        with mock.patch.dict(os.environ, {"OneDrive": str(self.root.parent)}):
            self.assertCode("INVALID_ARGUMENT", SandboxManager, self.root)

    def test_order_is_enforced(self):
        m = self.manager()
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner)
        self.assertCode("INVALID_ARGUMENT", m.publish_bootstrap, s, CERT)
        self.assertCode("INVALID_ARGUMENT", m.mark_ready, s)
        m.start(s)
        self.assertCode("INVALID_ARGUMENT", m.start, s)
        self.assertCode("INVALID_ARGUMENT", m.mark_ready, s)
        self.assertCode("INVALID_ARGUMENT", m.cleanup, s)
        m.stop(s, "TASK_COMPLETE")

    def test_bad_stop_reason(self):
        m, s = self.started()
        self.assertCode("INVALID_ARGUMENT", m.stop, s, "BECAUSE")
        m.stop(s, "TASK_COMPLETE")


class StartFailures(Base):
    def assertStoppedFailed(self, m, code):
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner)
        err = self.assertCode(code, m.start, s)
        self.assertEqual(s.state, FAILED)
        self.assertEqual(self.wsb.live, set())                 # never left running
        m.cleanup(s)
        return err

    def test_lan_route_refused(self):
        # Reached through the LAN address instead of the switch: the firewall rules would not cover it.
        self.assertStoppedFailed(self.manager(net=FakeNet(switch="192.168.208.1", toward="192.168.0.3")),
                                 "RUNTIME_START_FAILED")

    def test_no_switch_address_after_start(self):
        self.assertStoppedFailed(self.manager(net=FakeNet(switch=None, toward="192.168.208.1")),
                                 "RUNTIME_START_FAILED")

    def test_no_guest_ip(self):
        self.wsb.ip_after = 10_000
        self.assertStoppedFailed(self.manager(), "RUNTIME_START_FAILED")

    def test_firewall_not_ready(self):
        err = self.assertStoppedFailed(self.manager(firewall=FakeFirewall(["rules not bound"])), "RUNTIME_UNAVAILABLE")
        self.assertIn("Set-NetFirewallRule", err.message)       # tells the user how to fix it

    def test_firewall_fixed_by_watcher_while_waiting(self):
        class LateFirewall:                                     # watcher rebinds after a few checks
            calls = 0

            def problems(self):
                self.calls += 1
                return [] if self.calls >= 4 else ["rules not bound"]
        fw = LateFirewall()
        m = self.manager(firewall=fw)
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner)
        self.assertEqual(m.start(s), "192.168.208.1")
        self.assertEqual((s.state, fw.calls), (STARTED, 4))
        m.stop(s, "TASK_COMPLETE")

    def test_stop_not_confirmed(self):
        self.wsb.stop_works = False
        m, s = self.started()
        self.assertCode("RUNTIME_UNAVAILABLE", m.stop, s, "SECURITY_VIOLATION", emergency=True)
        self.assertNotEqual(s.state, TERMINATED)               # never claimed without Host-side proof
        self.assertTrue(any(e["kind"] == "STOP_NOT_CONFIRMED" for e in s.events))


class Concurrency(Base):
    """Issue #3: the Host may call mark_ready() and stop() from two threads right after READY."""

    def running_session(self):
        m, s = self.started()
        self.host_writes_bootstrap(s)
        m.publish_bootstrap(s, CERT)
        return m, s

    def test_mark_ready_and_stop_together(self):
        import threading
        m, s = self.running_session()
        inside, peak, guard = [0], [0], threading.Lock()
        real_save = m._save

        def slow_save(sess):                                    # widen the window a race would need
            with guard:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            try:
                import time as _t
                _t.sleep(0.01)
                real_save(sess)
            finally:
                with guard:
                    inside[0] -= 1
        m._save = slow_save
        errors = []

        def run(fn, *a, **k):
            try:
                fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=run, args=(m.mark_ready, s)),
                   threading.Thread(target=run, args=(m.stop, s, "TASK_COMPLETE"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(peak[0], 1)                            # never two state.json writers at once
        self.assertEqual(s.state, TERMINATED)
        self.assertFalse(s.bootstrap_path.exists())
        self.assertEqual(list(s.dir.glob("state.json.*.tmp")), [])   # no temp file left behind

    def test_mark_ready_after_stop_is_harmless(self):
        m, s = self.running_session()
        m.stop(s, "TASK_COMPLETE")
        m.mark_ready(s)                                         # stop() won the race: nothing to do
        self.assertEqual(s.state, TERMINATED)

    def test_failed_start_still_stops_inside_lock(self):
        # start() calls stop() itself on failure; the lock must be reentrant for that.
        m = self.manager(firewall=FakeFirewall(["rules not bound"]))
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner)
        with self.assertRaises(SandboxManagerError):
            m.start(s)
        self.assertEqual((s.state, self.wsb.live), (FAILED, set()))


class Orphans(Base):
    """The Host died (Codex closed or killed) before stop(): the next start() must not fail forever."""

    DEAD_PID = 424242

    def left_running(self, owner=None):
        """A RUNNING session whose Host process is gone: bootstrap token still in the workspace."""
        m, s = self.started()
        self.host_writes_bootstrap(s)
        m.publish_bootstrap(s, CERT)
        d = json.loads((s.dir / "state.json").read_text(encoding="utf-8"))
        d["owner"] = owner if owner is not None else {"pid": self.DEAD_PID, "started": 5}
        if owner == {}:
            del d["owner"]                                      # record from before owners existed
        (s.dir / "state.json").write_text(json.dumps(d), encoding="utf-8")
        return s

    def next_start(self):
        m = self.manager()                                      # the new Host process
        s = m.prepare("SES-20260929-002", "RT-SBX-001", 1, self.runner)
        return m, s

    def test_owner_recorded(self):
        m, s = self.started()
        self.assertEqual(s.owner, {"pid": os.getpid(), "started": 111})
        m.stop(s, "TASK_COMPLETE")

    def test_orphan_reclaimed_then_start_succeeds(self):
        old = self.left_running()
        m, s = self.next_start()
        self.assertEqual(m.start(s), "192.168.208.1")
        again = m.load(old.session_id)
        self.assertEqual((again.state, again.termination_reason), (TERMINATED, "RUNTIME_ERROR"))
        self.assertIn("ORPHAN_RECLAIMED", [e["kind"] for e in again.events])
        self.assertFalse(old.bootstrap_path.exists())           # the leftover token is gone too
        self.assertFalse(old.package_dir.exists())
        self.assertEqual(len(self.wsb.started_xml), 2)
        m.stop(s, "TASK_COMPLETE")

    def test_live_owner_is_left_alone(self):
        m1, first = self.started()                              # its Host (this process) is alive
        m, s = self.next_start()
        self.assertCode("RUNTIME_UNAVAILABLE", m.start, s)
        self.assertEqual(m.load(first.session_id).state, STARTED)
        self.assertIn(SBX, self.wsb.live)
        m1.stop(first, "TASK_COMPLETE")

    def test_pid_reused_by_another_process(self):
        self.left_running(owner={"pid": os.getpid(), "started": 99})   # same PID, different creation time
        m, s = self.next_start()
        m.start(s)
        self.assertEqual(s.state, STARTED)
        m.stop(s, "TASK_COMPLETE")

    def test_owner_that_cannot_be_inspected_counts_as_alive(self):
        old = self.left_running()
        self.procs.hidden.add(self.DEAD_PID)
        m, s = self.next_start()
        self.assertCode("RUNTIME_UNAVAILABLE", m.start, s)
        self.assertEqual(m.load(old.session_id).state, RUNNING)

    def test_record_without_owner_is_reclaimed(self):
        old = self.left_running(owner={})
        m, s = self.next_start()
        m.start(s)
        self.assertEqual(m.load(old.session_id).state, TERMINATED)
        m.stop(s, "TASK_COMPLETE")

    def test_sandbox_already_closed_by_hand(self):
        old = self.left_running()
        self.wsb.live.clear()                                   # user closed the window; record still says RUNNING
        m = self.manager()
        self.assertEqual(m.reclaim_orphans(), [old.session_id])
        self.assertEqual(m.load(old.session_id).state, TERMINATED)
        self.assertFalse(old.bootstrap_path.exists())

    def test_orphan_that_will_not_stop_blocks_start(self):
        old = self.left_running()
        self.wsb.stop_works = False
        m, s = self.next_start()
        self.assertCode("RUNTIME_UNAVAILABLE", m.start, s)
        self.assertNotEqual(m.load(old.session_id).state, TERMINATED)   # never claimed without proof
        self.assertEqual(s.state, PREPARED)

    def test_unrecorded_sandbox_is_refused_not_stopped(self):
        stranger = "99999999-2222-3333-4444-555555555555"
        self.wsb.live.add(stranger)
        m, s = self.next_start()
        err = self.assertCode("RUNTIME_UNAVAILABLE", m.start, s)
        self.assertIn(stranger, self.wsb.live)
        self.assertIn("close it first", err.message)

    def test_finished_and_broken_records_are_ignored(self):
        m1, done = self.started()
        m1.stop(done, "TASK_COMPLETE")
        broken = self.root / "sessions" / "SES-BROKEN"
        broken.mkdir(parents=True)
        (broken / "state.json").write_text("{not json", encoding="utf-8")
        m = self.manager()
        self.assertEqual(m.reclaim_orphans(), [])
        self.assertEqual(m.load(done.session_id).events[-1]["kind"], "STATE")   # nothing appended


class Recovery(Base):
    """restart_runner(): the Runner died, the Sandbox is fine. reset_sandbox(): the Sandbox is unresponsive."""

    def running_session(self, m=None):
        m, s = self.started(m)
        self.host_writes_bootstrap(s)
        m.publish_bootstrap(s, CERT)
        m.mark_ready(s)
        return m, s

    def test_restart_runner_round_trip(self):
        m, s = self.running_session()
        self.host_writes_bootstrap(s)                           # stale file a Guest could still read
        m.restart_runner(s, 2)
        self.assertEqual((s.state, s.generation, s.restarts), (RESTARTING, 2, 1))
        self.assertEqual(self.wsb.exec_calls, [(SBX, config.restart_command())])   # the one fixed command
        self.assertFalse(s.bootstrap_path.exists())             # Guest waits for the NEW bootstrap
        self.assertFalse(s.ready_path.exists())
        self.assertIn(SBX, self.wsb.live)                       # same Sandbox
        self.host_writes_bootstrap(s)                           # Host: new token, generation 2
        m.publish_bootstrap(s, CERT)
        self.assertEqual(s.state, RUNNING)
        self.assertTrue(s.ready_path.exists())
        m.mark_ready(s)
        self.assertEqual(m.load(s.session_id).restarts, 1)
        m.stop(s, "TASK_COMPLETE")

    def test_restart_script_is_fixed_and_in_the_package(self):
        m, s = self.running_session()
        script = (s.package_dir / config.RESTART_SCRIPT).read_text(encoding="utf-8-sig")
        self.assertIn("Stop-Process -Name sandbox_runner", script)
        self.assertIn(r"C:\RunnerPackage\start.ps1", script)
        self.assertEqual(config.restart_command(),
                         r"powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\RunnerPackage\restart.ps1")
        m.stop(s, "TASK_COMPLETE")

    def test_generation_must_go_up(self):
        m, s = self.running_session()
        for bad in (1, 0, True, "2", None):
            self.assertCode("INVALID_ARGUMENT", m.restart_runner, s, bad)
            self.assertCode("INVALID_ARGUMENT", m.reset_sandbox, s, bad)
        self.assertEqual((s.state, s.restarts), (RUNNING, 0))
        m.stop(s, "TASK_COMPLETE")

    def test_restart_limit_is_shared(self):
        m, s = self.running_session()
        m.restart_runner(s, 2)
        m.reset_sandbox(s, 3)                                   # from RESTARTING: escalate to a new Sandbox
        self.host_writes_bootstrap(s)
        m.publish_bootstrap(s, CERT)
        m.restart_runner(s, 4)
        self.assertEqual(s.restarts, 3)
        err = self.assertCode("RUNTIME_UNAVAILABLE", m.restart_runner, s, 5)
        self.assertIn("restart limit", err.message)
        self.assertCode("RUNTIME_UNAVAILABLE", m.reset_sandbox, s, 5)
        self.assertEqual(s.generation, 4)
        m.stop(s, "RUNTIME_ERROR", emergency=True)

    def test_limit_is_configurable(self):
        m = SandboxManager(self.root, wsb=self.wsb, network=FakeNet(), firewall=FakeFirewall(), clock=self.clock,
                           sleep=self.clock.sleep, ip_wait_s=10, stop_verify_s=5, process_started=self.procs,
                           max_restarts=0)
        m, s = self.running_session(m)
        self.assertCode("RUNTIME_UNAVAILABLE", m.restart_runner, s, 2)
        m.stop(s, "TASK_COMPLETE")

    def test_restart_when_sandbox_is_gone(self):
        m, s = self.running_session()
        self.wsb.live.clear()
        self.assertCode("RUNTIME_UNAVAILABLE", m.restart_runner, s, 2)
        self.assertEqual((s.state, s.generation, s.restarts, self.wsb.exec_calls), (RUNNING, 1, 0, []))

    def test_restart_script_fails_then_stop_or_reset(self):
        m, s = self.running_session()
        self.wsb.exec_code = 3                                  # old Runner would not end
        self.assertCode("RUNTIME_START_FAILED", m.restart_runner, s, 2)
        self.assertEqual(s.state, RESTARTING)
        self.assertTrue(any(e["kind"] == "RUNNER_RESTART_FAILED" for e in s.events))
        self.assertEqual(m.reset_sandbox(s, 3), "192.168.208.1")   # escalate
        self.assertEqual((s.state, s.restarts), (STARTED, 2))
        m.stop(s, "RUNTIME_ERROR", emergency=True)
        self.assertEqual(s.state, TERMINATED)

    def test_reset_sandbox(self):
        m, s = self.running_session()
        address = m.reset_sandbox(s, 2)
        self.assertEqual(address, "192.168.208.1")
        self.assertNotIn(SBX, self.wsb.live)                    # old Sandbox confirmed gone
        self.assertEqual(len(self.wsb.live), 1)
        self.assertNotEqual(s.sandbox_id, SBX)
        self.assertEqual((s.state, s.generation, s.restarts), (STARTED, 2, 1))
        self.assertTrue(any(e.get("recovery") == "sandbox" and e.get("old_sandbox_id") == SBX for e in s.events))
        self.host_writes_bootstrap(s)
        m.publish_bootstrap(s, CERT)
        m.mark_ready(s)
        m.stop(s, "TASK_COMPLETE")
        self.assertEqual(self.wsb.live, set())

    def test_reset_when_old_sandbox_will_not_stop(self):
        m, s = self.running_session()
        self.wsb.stop_works = False
        self.assertCode("RUNTIME_UNAVAILABLE", m.reset_sandbox, s, 2)
        self.assertEqual((s.state, s.generation, s.restarts, len(self.wsb.started_xml)), (RUNNING, 1, 0, 1))

    def test_recovery_refused_outside_a_live_session(self):
        m = self.manager()
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner)
        self.assertCode("INVALID_ARGUMENT", m.restart_runner, s, 2)
        self.assertCode("INVALID_ARGUMENT", m.reset_sandbox, s, 2)
        m.start(s)
        m.stop(s, "TASK_COMPLETE")
        self.assertCode("INVALID_ARGUMENT", m.restart_runner, s, 2)
        self.assertCode("INVALID_ARGUMENT", m.reset_sandbox, s, 2)

    def test_restarting_is_live_for_orphan_reclaim(self):
        m, s = self.running_session()
        m.restart_runner(s, 2)
        self.assertEqual(self.manager().reclaim_orphans(), [])  # its Host is alive: not an orphan
        d = json.loads((s.dir / "state.json").read_text(encoding="utf-8"))
        d["owner"] = {"pid": 424242, "started": 5}              # now its Host died mid-restart
        (s.dir / "state.json").write_text(json.dumps(d), encoding="utf-8")
        self.assertEqual(self.manager().reclaim_orphans(), [s.session_id])
        self.assertNotIn(SBX, self.wsb.live)


class InputFiles(Base):
    """prepare(input_files=...): user files reach the Guest only as read-only copies in the workspace."""

    def make(self, name, data=b"hello"):
        p = Path(self.tmp.name) / "user" / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(data)
        return p

    def test_copies_are_mapped_read_only(self):
        a, b = self.make("견적서.xlsx", b"x" * 1000), self.make("memo.txt")
        m = self.manager()
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner, input_files=[a, str(b)])
        self.assertEqual([f["name"] for f in s.input_files], ["견적서.xlsx", "memo.txt"])
        self.assertEqual(s.input_files[0]["size"], 1000)
        self.assertRegex(s.input_files[0]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual((s.input_dir / "memo.txt").read_bytes(), b"hello")
        self.assertEqual(s.guest_input_paths, [config.GUEST_INPUT + "\\견적서.xlsx", config.GUEST_INPUT + "\\memo.txt"])
        m.start(s)
        xml = self.wsb.started_xml[0]
        hosts = re.findall(r"<HostFolder>(.*?)</HostFolder>", xml)
        self.assertIn(str(s.input_dir.resolve()), hosts)
        self.assertNotIn(str(a.parent), hosts)                    # the user's own folder is never mapped
        self.assertEqual(xml.count("<ReadOnly>true</ReadOnly>"), 3)
        self.assertNotIn("<ReadOnly>false", xml)
        self.assertIn(f"<SandboxFolder>{config.GUEST_INPUT}</SandboxFolder>", xml)
        m.stop(s, "TASK_COMPLETE")

    def test_original_is_untouched_by_changes_to_the_copy(self):
        a = self.make("a.txt", b"original")
        m = self.manager()
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner, input_files=[a])
        (s.input_dir / "a.txt").write_bytes(b"changed")
        self.assertEqual(a.read_bytes(), b"original")
        self.assertEqual(os.stat(s.input_dir / "a.txt").st_nlink, 1)   # a real copy, not a hard link

    def test_no_inputs_means_no_extra_mapping(self):
        m, s = self.started()
        self.assertEqual(self.wsb.started_xml[0].count("<MappedFolder>"), 2)
        self.assertFalse(s.input_dir.exists())
        self.assertEqual(s.guest_input_paths, [])
        m.stop(s, "TASK_COMPLETE")

    def test_refusals(self):
        m = self.manager()
        folder = Path(self.tmp.name) / "afolder"
        folder.mkdir()
        dup1, dup2 = self.make("Same.txt"), Path(self.tmp.name) / "other" / "same.TXT"
        dup2.parent.mkdir()
        dup2.write_bytes(b"x")
        for bad in ([Path(self.tmp.name) / "missing.txt"], [folder], [dup1, dup2]):
            self.assertCode("INVALID_ARGUMENT", m.prepare, "SES-1", "RT-SBX-001", 1, self.runner, input_files=bad)
        self.assertFalse((self.root / "sessions" / "SES-1").exists())   # refused before anything was created

    def test_size_limits(self):
        big = self.make("big.bin", b"x" * 11)
        with mock.patch.object(config, "INPUT_FILE_MAX", 10):
            self.assertCode("INVALID_ARGUMENT", self.manager().prepare, "SES-1", "RT-SBX-001", 1, self.runner,
                            input_files=[big])
        files = [self.make(f"f{i}.bin", b"x" * 6) for i in range(3)]
        with mock.patch.object(config, "INPUT_TOTAL_MAX", 15):
            self.assertCode("INVALID_ARGUMENT", self.manager().prepare, "SES-1", "RT-SBX-001", 1, self.runner,
                            input_files=files)

    @unittest.skipUnless(sys.platform == "win32", "symlink creation")
    def test_link_is_refused(self):
        target = self.make("secret.txt")
        link = Path(self.tmp.name) / "user" / "link.txt"
        try:
            os.symlink(target, link)
        except OSError:
            self.skipTest("symlinks need Developer Mode or admin")
        self.assertCode("INVALID_ARGUMENT", self.manager().prepare, "SES-1", "RT-SBX-001", 1, self.runner,
                        input_files=[link])

    def test_state_keeps_no_source_path_and_cleanup_removes_copies(self):
        a = self.make("private-name.txt")
        m = self.manager()
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner, input_files=[a])
        m.start(s)
        m.stop(s, "TASK_COMPLETE")
        state = (s.dir / "state.json").read_text(encoding="utf-8")
        self.assertNotIn(str(a.parent), state)
        self.assertIn("private-name.txt", state)
        self.assertIn("input", m.cleanup(s)["removed"])
        self.assertFalse(s.input_dir.exists())
        self.assertEqual(m.load(s.session_id).input_files[0]["name"], "private-name.txt")

    def test_reset_sandbox_keeps_the_inputs(self):
        a = self.make("a.txt")
        m = self.manager()
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner, input_files=[a])
        m.start(s)
        self.host_writes_bootstrap(s)
        m.publish_bootstrap(s, CERT)
        m.reset_sandbox(s, 2)
        self.assertIn(f"<SandboxFolder>{config.GUEST_INPUT}</SandboxFolder>", self.wsb.started_xml[1])
        self.assertTrue((s.input_dir / "a.txt").is_file())
        m.stop(s, "TASK_COMPLETE")


class WsbExec(unittest.TestCase):
    def test_exit_code_from_text(self):
        from sandbox_manager.wsb import WsbCli
        w = WsbCli(exe="wsb")
        for text, code in (("프로세스가 종료되었습니다(코드: 0).", 0), ("프로세스가 종료되었습니다(코드: 7).", 7),
                           ("Process exited with code -1.", -1)):
            with mock.patch.object(w, "_run", return_value=(None, text)):
                self.assertEqual(w.exec("x", "cmd"), code)
        with mock.patch.object(w, "_run", return_value=({"ExitCode": 3}, "{}")):
            self.assertEqual(w.exec("x", "cmd"), 3)

    def test_waits_for_the_logon_session(self):
        from sandbox_manager import wsb as wsbmod
        w = wsbmod.WsbCli(exe="wsb")
        calls = []

        def fake_run(*a, **k):
            calls.append(a)
            if len(calls) < 3:
                raise SandboxManagerError("RUNTIME_UNAVAILABLE", "wsb exec exit 1: 지정한 로그온 세션이 없습니다. (0x80070520)")
            return None, "프로세스가 종료되었습니다(코드: 0)."
        with mock.patch.object(w, "_run", side_effect=fake_run), mock.patch.object(wsbmod.time, "sleep"):
            self.assertEqual(w.exec("x", "cmd"), 0)
        self.assertEqual(len(calls), 3)
        self.assertIn("ExistingLogin", calls[0])

    def test_other_errors_are_not_retried(self):
        from sandbox_manager.wsb import WsbCli
        w = WsbCli(exe="wsb")
        err = SandboxManagerError("RUNTIME_UNAVAILABLE", "wsb exec exit 1: 지정된 파일을 찾을 수 없습니다. (0x80070002)")
        with mock.patch.object(w, "_run", side_effect=err) as run:
            with self.assertRaises(SandboxManagerError):
                w.exec("x", "cmd")
        self.assertEqual(run.call_count, 1)


@unittest.skipUnless(sys.platform == "win32", "Windows process API")
class RealProcess(unittest.TestCase):
    def test_own_process_and_exited_process(self):
        import subprocess
        from sandbox_manager.process import process_started
        self.assertIsInstance(process_started(os.getpid()), int)
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertIsNone(process_started(p.pid))


class Firewall(unittest.TestCase):
    def rules(self, iface=SWITCH, block_ports=("1-17442", "17445-65535")):
        return [Rule("SCRP PoC 17443 from Sandbox", True, True, "Allow", "TCP", ["17443"], [iface]),
                Rule("SCRP PoC 17444 from Sandbox", True, True, "Allow", "TCP", ["17444"], [iface]),
                Rule("SCRP PoC block Sandbox to Host TCP", True, True, "Block", "TCP", list(block_ports), [iface])]

    def test_good(self):
        self.assertEqual(evaluate(self.rules()), [])

    def test_stale_after_reboot(self):
        problems = evaluate(self.rules(iface="c5b606ab-986a-4159-abc2-faae196d5f40"))
        self.assertTrue(any("not bound" in p for p in problems))
        self.assertTrue(any("allow rule for TCP 17443" in p for p in problems))

    def test_block_covers_upload_port(self):
        problems = evaluate(self.rules(block_ports=("1-17442", "17444-65535")))
        self.assertTrue(any("17444 is inside a block rule" in p for p in problems))

    def test_missing(self):
        self.assertTrue(evaluate([]))
        self.assertTrue(any("no active TCP block rule" in p for p in evaluate(self.rules()[:2])))


if __name__ == "__main__":
    unittest.main()
