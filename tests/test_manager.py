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

from sandbox_manager import (FAILED, PREPARED, RUNNING, STARTED, TERMINATED, SandboxManager,  # noqa: E402
                             SandboxManagerError)
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

    def start(self, xml):
        self.started_xml.append(xml)
        self.live.add(SBX)
        return SBX

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


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.runner = Path(self.tmp.name) / "sandbox_runner.exe"
        self.runner.write_bytes(b"MZ fake runner")
        self.clock = FakeClock()
        self.wsb = FakeWsb()
        self.env = mock.patch.dict(os.environ, {"OneDrive": str(Path(self.tmp.name) / "OneDrive")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def manager(self, net=None, firewall=None):
        return SandboxManager(self.root, wsb=self.wsb, network=net or FakeNet(), firewall=firewall or FakeFirewall(),
                              clock=self.clock, sleep=self.clock.sleep, ip_wait_s=10, stop_verify_s=5)

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
