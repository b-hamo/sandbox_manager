"""Shared folder per session (shared.py): mapped writable, nothing runs on the Host, scanned before it leaves.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox_manager import SandboxManagerError, config, finish_shared, make_share  # noqa: E402
from sandbox_manager import shared  # noqa: E402
from test_manager import Base  # noqa: E402


def clean(_folder):
    return "clean", "no threats"


class Share(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        (self.home / "Downloads").mkdir(parents=True)
        self.root = self.home / "SecureCUA" / "codex-work" / "shared"
        self.root.mkdir(parents=True)
        self.dest = self.home / "Downloads" / "SandboxShared"
        for p in (mock.patch("sandbox_manager.inbox.Path.home", return_value=self.home),
                  mock.patch("sandbox_manager.shared.check_no_execute")):
            p.start()
            self.addCleanup(p.stop)

    def share_with(self, files: dict[str, bytes]) -> Path:
        share = make_share(self.root, "SES-1")
        for name, data in files.items():
            (share / name).parent.mkdir(parents=True, exist_ok=True)
            (share / name).write_bytes(data)
        return share

    def test_make_share_creates_a_new_empty_folder_per_session(self):
        share = make_share(self.root, "SES-1")
        self.assertEqual(share, self.root.resolve() / "SES-1")
        self.assertEqual(list(share.iterdir()), [])
        with self.assertRaises(SandboxManagerError):
            make_share(self.root, "SES-1")                 # never reuses an old session's files
        for bad in ("..", "a\\b", ""):
            with self.assertRaises(SandboxManagerError):
                make_share(self.root, bad)

    def test_user_folders_are_refused_as_root(self):
        for bad in (self.home / "Downloads", self.home, Path("relative")):
            with self.assertRaises(SandboxManagerError):
                make_share(bad, "SES-1")

    def test_clean_folder_is_copied_to_downloads_marked_and_the_share_removed(self):
        share = self.share_with({"App\\app.exe": b"MZ" + b"a" * 100, "note.pdf": b"%PDF-1.4"})
        r = finish_shared(share, dest=self.dest, scan=clean)
        self.assertEqual(r["status"], "EXPORTED")
        out = Path(r["dest"])
        self.assertEqual(out, self.dest / "SES-1")
        self.assertEqual((out / "App" / "app.exe").read_bytes(), b"MZ" + b"a" * 100)
        self.assertEqual((out / "note.pdf").read_bytes(), b"%PDF-1.4")
        self.assertFalse(share.exists())
        self.assertEqual([f["name"] for f in r["files"]], ["App\\app.exe", "note.pdf"])
        self.assertEqual(r["files"][1]["sha256"], hashlib.sha256(b"%PDF-1.4").hexdigest())
        if sys.platform == "win32":
            self.assertIn("ZoneId=3", Path(f"{out / 'note.pdf'}:Zone.Identifier").read_text())

    def test_threat_or_scan_error_leaves_everything_in_place(self):
        for verdict, status in (("threat", "BLOCKED"), ("error", "QUARANTINED")):
            share = self.share_with({"x.exe": b"MZ"})
            r = finish_shared(share, dest=self.dest, scan=lambda _f, v=verdict: (v, "detail"))
            self.assertEqual(r["status"], status)
            self.assertIsNone(r["dest"])
            self.assertTrue((share / "x.exe").is_file())  # still where nothing can run
            self.assertFalse(self.dest.exists())
            (share / "x.exe").unlink()
            share.rmdir()

    def test_too_big_is_not_scanned_nor_exported(self):
        share = self.share_with({"a.bin": b"x" * 10})
        with mock.patch.object(config, "SHARED_TOTAL_MAX", 5):
            r = finish_shared(share, dest=self.dest, scan=lambda _f: self.fail("scanned"))
        self.assertEqual(r["status"], "BLOCKED")
        self.assertTrue((share / "a.bin").is_file())

    def test_empty_share_is_just_removed(self):
        share = self.share_with({})
        self.assertEqual(finish_shared(share, dest=self.dest, scan=clean)["status"], "EMPTY")
        self.assertFalse(share.exists())
        self.assertEqual(finish_shared(share, dest=self.dest, scan=clean)["status"], "EMPTY")

    def test_never_overwrites_an_earlier_export(self):
        (self.dest / "SES-1").mkdir(parents=True)
        share = self.share_with({"a.txt": b"new"})
        r = finish_shared(share, dest=self.dest, scan=clean)
        self.assertEqual(Path(r["dest"]).name, "SES-1-2")

    @unittest.skipUnless(sys.platform == "win32", "junctions and hard links")
    def test_links_are_removed_never_followed_nor_exported(self):
        secret = Path(self.tmp.name) / "secret"
        secret.mkdir()
        (secret / "key.txt").write_text("secret")
        share = self.share_with({"a.txt": b"a"})
        out = subprocess.run(["cmd", "/c", "mklink", "/J", str(share / "j"), str(secret)], capture_output=True)
        if out.returncode:
            self.skipTest("cannot create a junction here")
        os.link(secret / "key.txt", share / "hard.txt")
        events = shared.sweep_share(share)
        self.assertEqual(sorted(k for k, _ in events), ["SHARED_HARDLINK_REMOVED", "SHARED_LINK_REMOVED"])
        self.assertEqual((secret / "key.txt").read_text(), "secret")   # the target is untouched
        r = finish_shared(share, dest=self.dest, scan=clean)
        self.assertEqual([f["name"] for f in r["files"]], ["a.txt"])


@unittest.skipUnless(sys.platform == "win32", "ACLs")
class NoExecuteProbe(unittest.TestCase):
    def test_a_folder_where_programs_still_run_is_refused_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sandbox_manager.inbox.Path.home",
                                                             return_value=Path(tmp) / "home"):
            root = Path(tmp) / "work" / "shared"
            root.mkdir(parents=True)
            with self.assertRaises(SandboxManagerError) as cm:
                make_share(root, "SES-1")
            self.assertEqual(cm.exception.code, "POLICY_DENIED")
            self.assertFalse((root / "SES-1").exists())

    def test_deny_execute_ace_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sandbox_manager.inbox.Path.home",
                                                             return_value=Path(tmp) / "home"):
            root = Path(tmp) / "work" / "shared"
            root.mkdir(parents=True)
            subprocess.run(["icacls", str(root), "/deny", "*S-1-1-0:(OI)(IO)(X)"], check=True, capture_output=True)
            share = make_share(root, "SES-1")
            self.assertEqual(list(share.iterdir()), [])
            subprocess.run(["icacls", str(root), "/remove:d", "*S-1-1-0"], capture_output=True)


class ManagerShared(Base):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "home"
        (self.home / "Documents").mkdir(parents=True)
        self.share = self.home / "SecureCUA" / "codex-work" / "shared" / "SES-1"
        self.share.mkdir(parents=True)
        patcher = mock.patch("sandbox_manager.inbox.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shared_is_the_only_writable_mapping_and_the_mirror_is_installed(self):
        m = self.manager()
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner, shared=self.share)
        self.assertTrue((s.package_dir / config.SHARED_MIRROR_SCRIPT).is_file())
        m.start(s)
        xml = self.wsb.started_xml[0]
        self.assertIn(f"<HostFolder>{self.share.resolve()}</HostFolder><SandboxFolder>{config.GUEST_SHARED}"
                      "</SandboxFolder><ReadOnly>false</ReadOnly>", xml)
        self.assertEqual(xml.count("<ReadOnly>false</ReadOnly>"), 1)
        m.stop(s, "TASK_COMPLETE")
        m.cleanup(s)
        self.assertTrue(self.share.is_dir())                 # finish_shared decides, not cleanup

    def test_shared_must_be_separate_from_inbox_and_workspace(self):
        m = self.manager()
        for bad in dict(shared=m.root / "x"), dict(shared=self.share, inbox=self.share):
            (m.root / "x").mkdir(parents=True, exist_ok=True)
            with self.assertRaises(SandboxManagerError):
                m.prepare("SES-1", "RT-SBX-001", 1, self.runner, **bad)

    def test_sweep_runs_only_while_live(self):
        m = self.manager()
        s = m.prepare("SES-1", "RT-SBX-001", 1, self.runner, shared=self.share)
        self.assertEqual(m.sweep_shared(s), [])
        m.start(s)
        m.stop(s, "TASK_COMPLETE")
        with mock.patch("sandbox_manager.manager.sweep_share", side_effect=AssertionError):
            self.assertEqual(m.sweep_shared(s), [])


class GuestScripts(unittest.TestCase):
    def test_mirror_copies_only_never_purges_and_skips_junctions(self):
        ps = config.SHARED_MIRROR_PS1
        self.assertIn("robocopy.exe", ps)
        self.assertIn("/XJ", ps)
        self.assertNotIn("/MIR", ps)
        self.assertNotIn("/PURGE", ps)
        self.assertIn("shared_mirror.ps1", config.START_PS1)


if __name__ == "__main__":
    unittest.main()
