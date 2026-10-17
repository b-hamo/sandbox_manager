"""Isolation checks (progress.md 3.1): the Guest must never see Host user folders, browser profiles,
Safe Results or a writable Host output. Each test tries a way around the mapping rules.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_manager import CERT, SBX, Base  # noqa: E402

from sandbox_manager import config  # noqa: E402


def junction(link: Path, target: Path) -> None:
    """Directory junction: no admin rights needed, followed like a real folder."""
    out = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if out.returncode != 0:
        raise unittest.SkipTest(f"cannot create a junction here: {out.stdout}{out.stderr}")


@unittest.skipUnless(os.name == "nt", "junctions are Windows only")
class Junctions(Base):
    def setUp(self):
        super().setUp()
        self.user = Path(self.tmp.name) / "Users" / "victim"       # stands in for a Host user folder
        (self.user / "Documents").mkdir(parents=True)
        (self.user / "Documents" / "secret.txt").write_text("host data", encoding="utf-8")

    def test_package_swapped_for_junction_before_start(self):
        m = self.manager()
        s = m.prepare("SES-20260929-001", "RT-SBX-001", 1, self.runner)
        for p in s.package_dir.iterdir():
            p.unlink()
        s.package_dir.rmdir()
        junction(s.package_dir, self.user)
        self.assertCode("INVALID_ARGUMENT", m.start, s)
        self.assertEqual(self.wsb.started_xml, [])                 # no Sandbox was started

    def test_junction_inside_package(self):
        m = self.manager()
        s = m.prepare("SES-20260929-001", "RT-SBX-001", 1, self.runner)
        junction(s.package_dir / "docs", self.user / "Documents")
        self.assertCode("INVALID_ARGUMENT", m.start, s)
        self.assertEqual(self.wsb.started_xml, [])

    def test_junction_deep_inside_bootstrap(self):
        m = self.manager()
        s = m.prepare("SES-20260929-001", "RT-SBX-001", 1, self.runner)
        (s.bootstrap_dir / "a" / "b").mkdir(parents=True)
        junction(s.bootstrap_dir / "a" / "b" / "c", self.user)
        self.assertCode("INVALID_ARGUMENT", m.start, s)
        self.assertEqual(self.wsb.started_xml, [])


    def test_junction_added_after_start_blocks_the_ready_marker(self):
        """bootstrap/ keeps receiving files after start(); the Guest must not be told to go on."""
        m, s = self.started()
        self.host_writes_bootstrap(s)
        junction(s.bootstrap_dir / "late", self.user)
        self.assertCode("INVALID_ARGUMENT", m.publish_bootstrap, s, CERT)
        self.assertFalse(s.ready_path.exists())                    # Guest script keeps waiting, Runner never starts
        m.stop(s, "SECURITY_VIOLATION")
        self.assertNotIn(SBX, self.wsb.live)


class HardLinks(Base):
    def test_hard_linked_file_in_package(self):
        secret = Path(self.tmp.name) / "host-secret.txt"
        secret.write_text("host data", encoding="utf-8")
        m = self.manager()
        s = m.prepare("SES-20260929-001", "RT-SBX-001", 1, self.runner)
        os.link(secret, s.package_dir / "notes.txt")
        self.assertCode("INVALID_ARGUMENT", m.start, s)
        self.assertEqual(self.wsb.started_xml, [])

    def test_hard_linked_bootstrap_after_start(self):
        m, s = self.started()
        secret = Path(self.tmp.name) / "host-secret.txt"
        secret.write_text("{}", encoding="utf-8")
        os.link(secret, s.bootstrap_path)                           # valid JSON, but it is a Host file
        self.assertCode("INVALID_ARGUMENT", m.publish_bootstrap, s, CERT)
        self.assertFalse(s.ready_path.exists())
        m.stop(s, "SECURITY_VIOLATION")

    def test_normal_session_passes_both_checks(self):
        m, s = self.started()
        self.host_writes_bootstrap(s)
        m.publish_bootstrap(s, CERT)
        self.assertTrue(s.ready_path.exists())
        m.stop(s, "TASK_COMPLETE")


class SensitiveFolders(Base):
    def test_user_folders_refused_even_when_named_directly(self):
        ws = self.root / "sessions" / "X"
        ws.mkdir(parents=True)
        home = Path.home()
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidates = [home, home / "Desktop", home / "Documents", home / "Downloads",
                      local / "Google" / "Chrome" / "User Data", local / "Microsoft" / "Edge" / "User Data",
                      Path(os.environ.get("APPDATA", home)) / "Mozilla" / "Firefox" / "Profiles"]
        for host in candidates:
            if not host.is_dir():
                continue
            with self.subTest(host=str(host)):
                self.assertCode("INVALID_ARGUMENT", config.build_wsb, [config.Mapping(host, r"C:\x")], "cmd", ws)

    def test_workspace_parent_refused(self):
        """A folder that contains the workspace (and so other sessions' files) is outside it too."""
        ws = self.root / "sessions" / "X"
        (ws / "package").mkdir(parents=True)
        for host in (self.root, self.root / "sessions"):
            with self.subTest(host=str(host)):
                self.assertCode("INVALID_ARGUMENT", config.build_wsb, [config.Mapping(host, r"C:\x")], "cmd", ws)

    def test_dotdot_escape_refused(self):
        ws = self.root / "sessions" / "X"
        (ws / "package").mkdir(parents=True)
        (self.root / "sessions" / "Y").mkdir()
        sneaky = ws / "package" / ".." / ".." / "Y"
        self.assertCode("INVALID_ARGUMENT", config.build_wsb, [config.Mapping(sneaky, r"C:\x")], "cmd", ws)


class GeneratedConfig(Base):
    def test_wsb_names_no_host_path_outside_the_session(self):
        m, s = self.started()
        xml = self.wsb.started_xml[0]
        session = str(s.dir.resolve()).lower()
        for host in re.findall(r"<HostFolder>(.*?)</HostFolder>", xml):
            self.assertTrue(host.lower().startswith(session + "\\"), host)
        outside = xml
        for host in re.findall(r"<HostFolder>.*?</HostFolder>", xml):
            outside = outside.replace(host, "")
        self.assertNotIn(str(Path.home()).lower(), outside.lower())   # not in the command, guest paths, etc.
        self.assertEqual(len(re.findall(r"<MappedFolder>", xml)), 2)
        self.assertEqual(sorted(re.findall(r"<SandboxFolder>(.*?)</SandboxFolder>", xml)),
                         sorted([config.GUEST_BOOTSTRAP, config.GUEST_PACKAGE]))
        m.stop(s, "TASK_COMPLETE")

    def test_guest_script_writes_only_inside_the_guest(self):
        """The start script logs to the Guest desktop; nothing goes back through a mapped folder."""
        writes = re.findall(r"Out-File\s+(\S+)|-Redirect\w+\s+\(?([^)\s]+)", config.START_PS1)
        targets = [a or b for a, b in writes]
        self.assertTrue(targets)
        for t in targets:
            self.assertNotIn("RunnerPackage", t)
            self.assertNotIn("RunnerBootstrap", t)

    def test_sandbox_id_only_after_checks(self):
        """start() writes and starts the .wsb only after the mapping checks passed."""
        m, s = self.started()
        self.assertEqual(s.sandbox_id, SBX)
        self.assertTrue(s.wsb_path.is_file())
        m.stop(s, "TASK_COMPLETE")


if __name__ == "__main__":
    unittest.main()
