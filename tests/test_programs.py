"""Installed programs shared read-only into a running Sandbox (programs.py, SandboxManager.share_program).

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox_manager import SandboxManagerError  # noqa: E402
from sandbox_manager import programs  # noqa: E402
from sandbox_manager.programs import FoundProgram, check_program_root, find_program  # noqa: E402
from sandbox_manager.wsb import WsbCli  # noqa: E402
from test_manager import Base, FakeWsb  # noqa: E402


def touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"MZ")
    return p


class Find(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_program_is_refused(self):
        with self.assertRaises(SandboxManagerError) as cm:
            find_program("photoshop", which=lambda n: None)
        self.assertEqual(cm.exception.code, "INVALID_ARGUMENT")

    def test_not_installed(self):
        with mock.patch.object(programs, "PROGRAMS", {"gcc": programs.Program("gcc.exe", r"bin\gcc.exe", ("bin",), ())}):
            with self.assertRaises(SandboxManagerError):
                find_program("gcc", which=lambda n: None)

    def test_python_install_folder(self):
        exe = touch(self.t / "Python313" / "python.exe")
        found = find_program("python", which=lambda n: str(exe))
        self.assertEqual(found.root, exe.parent.resolve())
        self.assertEqual(found.exe, "python.exe")

    def test_venv_python_shares_its_base_install(self):
        base = touch(self.t / "Python313" / "python.exe")
        venv = self.t / "proj" / ".venv"
        touch(venv / "Scripts" / "python.exe")
        (venv / "pyvenv.cfg").write_text(f"home = {base.parent}\nversion = 3.13.15\n", encoding="utf-8")
        found = find_program("python", which=lambda n: str(venv / "Scripts" / "python.exe"))
        self.assertEqual(found.root, base.parent.resolve())

    def test_store_alias_is_skipped(self):
        alias = touch(self.t / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "python.exe")
        with self.assertRaises(SandboxManagerError):
            find_program("python", which=lambda n: str(alias))

    def test_git_root_is_the_folder_with_cmd_git(self):
        root = self.t / "Git"
        touch(root / "cmd" / "git.exe")
        inner = touch(root / "mingw64" / "bin" / "git.exe")
        found = find_program("git", which=lambda n: str(inner))
        self.assertEqual(found.root, root.resolve())
        self.assertEqual(found.exe, r"cmd\git.exe")

    def test_for_agent_is_read_only_and_points_caches_at_the_guest(self):
        info = FoundProgram("python", self.t, "python.exe").for_agent()
        self.assertTrue(info["read_only"])
        self.assertEqual(info["exe"], r"C:\Tools\python\python.exe")
        self.assertEqual(info["path"], [r"C:\Tools\python", r"C:\Tools\python\Scripts"])
        self.assertIn(r"%TEMP%", info["env"]["PYTHONPYCACHEPREFIX"])
        self.assertTrue(info["cmd_setup"].startswith(r'set "PATH=C:\Tools\python;C:\Tools\python\Scripts;%PATH%"'))

    def test_gcc_keeps_its_folder_name(self):
        # MSYS2 gcc looks for headers at bin\..\..\ucrt64 (real Sandbox, 2026-10-27: stdio.h not found otherwise)
        info = FoundProgram("gcc", self.t / "msys64" / "ucrt64", r"bin\gcc.exe").for_agent()
        self.assertEqual(info["guest_root"], r"C:\Tools\gcc\ucrt64")
        self.assertEqual(info["path"], [r"C:\Tools\gcc\ucrt64\bin"])

    def test_one_setup_line_for_several_programs(self):
        line = programs.cmd_setup([FoundProgram("python", self.t, "python.exe"),
                                   FoundProgram("git", self.t, r"cmd\git.exe")])
        self.assertEqual(line.count("set \"PATH="), 1)
        self.assertIn(r"C:\Tools\python;C:\Tools\python\Scripts;C:\Tools\git\cmd;%PATH%", line)


class RootChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name).resolve()
        self.home = self.t / "Users" / "JH"
        self.home.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, root: Path):
        return check_program_root(FoundProgram("python", root, "python.exe"), home=self.home)

    def refused(self, root: Path):
        with self.assertRaises(SandboxManagerError) as cm:
            self.check(root)
        self.assertEqual(cm.exception.code, "INVALID_ARGUMENT")

    def test_program_folder_is_accepted(self):
        root = self.t / "Program Files" / "Python"
        touch(root / "python.exe")
        self.assertEqual(self.check(root).root, root)

    def test_per_user_install_under_programs_is_accepted(self):
        root = self.home / "AppData" / "Local" / "Programs" / "Python" / "Python313"
        touch(root / "python.exe")
        self.check(root)

    def test_profile_and_its_parents_are_refused(self):
        self.refused(self.home)
        self.refused(self.home.parent)

    def test_other_profile_folders_are_refused(self):
        for sub in ("Documents", "Desktop", "Downloads", r"AppData\Roaming\App", r"AppData\Local\Google"):
            root = self.home / sub
            touch(root / "python.exe")
            self.refused(root)

    def test_drive_root_is_refused(self):
        self.refused(Path(self.t.anchor))

    def test_junction_inside_is_refused(self):
        root = self.t / "Tool"
        touch(root / "python.exe")
        target = self.home / "Documents"
        target.mkdir(parents=True)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "docs"), str(target)], capture_output=True)
        if r.returncode != 0:
            self.skipTest("cannot create a junction here")
        self.refused(root)

    def test_hard_links_are_counted(self):
        root = self.t / "Git"
        a = touch(root / "bin" / "git.exe")
        try:
            os.link(a, root / "bin" / "git-add.exe")
        except OSError:
            self.skipTest("no hard links on this file system")
        self.assertEqual(self.check(root).hard_links, 2)

    def test_too_many_files_is_refused(self):
        root = self.t / "Big"
        for i in range(5):
            touch(root / f"f{i}")
        with mock.patch.object(programs, "SCAN_MAX_FILES", 3):
            self.refused(root)


class ShareFakeWsb(FakeWsb):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.shares: list[tuple[str, str, str]] = []

    def share(self, sid, host_path, guest_path):
        self.shares.append((sid, host_path, guest_path))


class ShareProgram(Base):
    def setUp(self):
        super().setUp()
        self.wsb = ShareFakeWsb()
        self.prog = Path(self.tmp.name).resolve() / "Program Files" / "Python313"
        touch(self.prog / "python.exe")
        self.find = mock.patch("sandbox_manager.manager.find_program",
                               lambda name, check: check(FoundProgram(name, self.prog, "python.exe")))
        self.find.start()
        home = Path(self.tmp.name).resolve() / "Users" / "JH"        # the temp folder is inside the real profile
        self.check = mock.patch("sandbox_manager.manager.check_program_root",
                                lambda found: check_program_root(found, home=home))
        self.check.start()

    def tearDown(self):
        self.check.stop()
        self.find.stop()
        super().tearDown()

    def test_shares_read_only_into_the_running_sandbox(self):
        m, s = self.started()
        info = m.share_program(s, "python")
        self.assertEqual(self.wsb.shares, [(s.sandbox_id, str(self.prog), r"C:\Tools\python")])
        self.assertEqual(info["exe"], r"C:\Tools\python\python.exe")
        self.assertEqual([p["name"] for p in s.programs], ["python"])
        ev = [e for e in s.events if e["kind"] == "PROGRAM_SHARED"]
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["read_only"])

    def test_answer_has_a_setup_line_for_everything_shared(self):
        m, s = self.started()
        m.share_program(s, "python")
        info = m.share_program(s, "git")
        self.assertIn(r"C:\Tools\python;", info["cmd_setup_all"])
        self.assertIn(r"C:\Tools\git\cmd;", info["cmd_setup_all"])

    def test_sharing_again_does_not_share_twice(self):
        m, s = self.started()
        first = m.share_program(s, "python")
        self.assertEqual(m.share_program(s, "python"), first)
        self.assertEqual(len(self.wsb.shares), 1)

    def test_not_before_start(self):
        m = self.manager()
        s = m.prepare("SES-20260929-001", "RT-SBX-001", 1, self.runner)
        self.assertCode("INVALID_ARGUMENT", m.share_program, s, "python")
        self.assertEqual(self.wsb.shares, [])

    def test_not_when_the_sandbox_is_gone(self):
        m, s = self.started()
        self.wsb.live.clear()
        self.assertCode("RUNTIME_UNAVAILABLE", m.share_program, s, "python")

    def test_a_reset_sandbox_gets_the_programs_again(self):
        m, s = self.started()
        self.host_writes_bootstrap(s)
        m.publish_bootstrap(s, "-----BEGIN CERTIFICATE-----\nMIIBdGVzdA==\n-----END CERTIFICATE-----\n")
        m.mark_ready(s)
        m.share_program(s, "python")
        m.reset_sandbox(s, 2)
        self.assertEqual(len(self.wsb.shares), 2)
        self.assertNotEqual(self.wsb.shares[0][0], self.wsb.shares[1][0])     # the new Sandbox
        self.assertEqual(self.wsb.shares[1][0], s.sandbox_id)


class NotInstalled(unittest.TestCase):
    """No portable zip is fetched any more (배주한 2026-11-07): an installed copy or "install it"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_installed_tells_the_user_to_install_it(self):
        with mock.patch.object(programs, "PROGRAMS", {"node": programs.Program("node.exe", "node.exe", ("",), ())}):
            with self.assertRaises(SandboxManagerError) as cm:
                find_program("node", which=lambda n: None)
        self.assertEqual(cm.exception.code, "INVALID_ARGUMENT")
        self.assertIn("not installed on this PC", cm.exception.message)
        self.assertIn("install it", cm.exception.message)

    def test_nothing_is_fetched(self):
        for name in ("PORTABLE", "ensure_portable", "tools_root", "_download"):
            self.assertFalse(hasattr(programs, name), name)

    def test_a_refused_copy_falls_to_the_next_installed_one(self):
        # 2026-11-03: Codex's own node.exe (~/.cache/codex-runtimes/...) was first on the Host's PATH
        home = self.t / "Users" / "JH"
        codex = touch(home / ".cache" / "codex-runtimes" / "node" / "bin" / "node.exe")
        installed = touch(self.t / "Program Files" / "nodejs" / "node.exe")
        spec = programs.Program("node.exe", "node.exe", ("",), (), (str(installed),))
        with mock.patch.dict(programs.PROGRAMS, {"node": spec}):
            found = find_program("node", which=lambda n: str(codex), check=lambda f: check_program_root(f, home=home))
        self.assertEqual(found.root, installed.parent)
        self.assertEqual(found.source, "installed")

    def test_only_refused_copies_report_the_refusal(self):
        home = self.t / "Users" / "JH"
        exe = touch(home / "tools" / "bin" / "gcc.exe")
        with mock.patch.dict(programs.PROGRAMS, {"gcc": programs.Program("gcc.exe", r"bin\gcc.exe", ("bin",), ())}):
            with self.assertRaises(SandboxManagerError) as cm:
                find_program("gcc", which=lambda n: str(exe), check=lambda f: check_program_root(f, home=home))
        self.assertIn("inside the user's profile", cm.exception.message)

    def test_git_answer_names_its_shell(self):
        root = self.t / "MinGit"
        touch(root / "cmd" / "git.exe")
        touch(root / "ucrt64" / "bin" / "ash.exe")
        self.assertEqual(FoundProgram("git", root, r"cmd\git.exe").for_agent()["shell"], r"C:\Tools\git\ucrt64\bin\ash.exe")
        full = self.t / "Git"
        touch(full / "cmd" / "git.exe")
        touch(full / "bin" / "bash.exe")
        self.assertEqual(FoundProgram("git", full, r"cmd\git.exe").for_agent()["shell"], r"C:\Tools\git\bin\bash.exe")


class CliHasNoWriteOption(unittest.TestCase):
    def test_share_never_asks_for_write(self):
        cli = WsbCli(exe="wsb.exe")
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("subprocess.run", return_value=done) as run:
            cli.share("sbx", r"C:\Program Files\Git", r"C:\Tools\git")
        args = run.call_args[0][0]
        self.assertEqual(args[:2], ["wsb.exe", "share"])
        self.assertNotIn("-w", args)
        self.assertNotIn("--allow-write", args)


if __name__ == "__main__":
    unittest.main()
