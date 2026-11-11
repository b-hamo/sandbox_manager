"""import_to_inbox: a file the user already downloaded, named by file name only, copied into the inbox.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from sandbox_manager import SandboxManagerError, clear_inbox, config, finish_inbox, import_to_inbox  # noqa: E402
from sandbox_manager import importer  # noqa: E402


class ImportToInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.downloads = self.home / "Downloads"
        self.downloads.mkdir(parents=True)
        self.inbox = self.home / "SecureCUA" / "codex-work" / "inbox"
        self.inbox.mkdir(parents=True)
        patcher = mock.patch("sandbox_manager.inbox.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, name: str, data: bytes = b"MZ" + b"x" * 64) -> Path:
        p = self.downloads / name
        p.write_bytes(data)
        return p

    def run_import(self, name, **kw):
        return import_to_inbox(name, self.inbox, self.downloads, **kw)

    def assertRefused(self, code, name, **kw):
        with self.assertRaises(SandboxManagerError) as cm:
            self.run_import(name, **kw)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        self.assertEqual(list(self.inbox.iterdir()), [])          # nothing placed

    def test_copies_the_file_and_keeps_the_original(self):
        data = b"MZ" + os.urandom(1000)
        src = self.put("ZoomIt.exe", data)
        r = self.run_import("ZoomIt.exe")
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual((self.inbox / "ZoomIt.exe").read_bytes(), data)
        self.assertEqual(src.read_bytes(), data)                  # copy, not move
        self.assertEqual(r["files"], [{"name": "ZoomIt.exe", "size": len(data), "sha256": digest}])
        self.assertEqual(r["source_sha256"], digest)
        self.assertEqual(r["sandbox_paths"], ["Desktop\\Input\\ZoomIt.exe"])

    def test_name_is_case_insensitive_like_windows(self):
        self.put("Setup.msi")
        self.run_import("setup.msi")
        self.assertEqual(len(list(self.inbox.iterdir())), 1)

    def test_same_name_twice_never_overwrites(self):
        self.put("tool.exe", b"one")
        self.run_import("tool.exe")
        (self.downloads / "tool.exe").write_bytes(b"two")
        r = self.run_import("tool.exe")
        self.assertEqual(r["files"][0]["name"], "tool-2.exe")
        self.assertEqual((self.inbox / "tool.exe").read_bytes(), b"one")

    def test_zip_is_unpacked_into_its_own_folder(self):
        with zipfile.ZipFile(self.downloads / "ZoomIt.zip", "w") as z:
            z.writestr("ZoomIt.exe", b"MZ64")
            z.writestr("docs/Eula.txt", b"eula")
        r = self.run_import("ZoomIt.zip", extract=True)
        self.assertEqual(sorted(f["name"] for f in r["files"]), ["ZoomIt\\ZoomIt.exe", "ZoomIt\\docs\\Eula.txt"])
        self.assertEqual((self.inbox / "ZoomIt" / "ZoomIt.exe").read_bytes(), b"MZ64")

    def test_zip_with_a_path_out_is_refused(self):
        with zipfile.ZipFile(self.downloads / "evil.zip", "w") as z:
            z.writestr("ok.txt", b"ok")
            z.writestr("../../outside.txt", b"x")
        self.assertRefused("POLICY_DENIED", "evil.zip", extract=True)
        self.assertFalse((self.home / "SecureCUA" / "outside.txt").exists())

    def test_extract_needs_a_zip(self):
        self.put("tool.exe")
        self.assertRefused("INVALID_ARGUMENT", "tool.exe", extract=True)

    def test_paths_and_tricky_names_are_refused(self):
        secret = self.home / "Documents"
        secret.mkdir()
        (secret / "secret.docx").write_bytes(b"private")
        self.put("a.exe")
        for name in ("..\\Documents\\secret.docx", "../Documents/secret.docx", str(secret / "secret.docx"),
                     "C:secret.docx", "a.exe:hidden", "..", ".", "sub\\a.exe"):
            self.assertRefused("POLICY_DENIED", name)
        for name in ("CON", "nul.txt", "COM1.exe"):
            self.assertRefused("POLICY_DENIED", name)
        for name in ("a.exe ", " a.exe", "a.exe.", "", "a\x07.exe", "x" * 256, None, 5):
            self.assertRefused("INVALID_ARGUMENT", name)

    def test_missing_file_folder_and_unfinished_download_are_refused(self):
        (self.downloads / "folder").mkdir()
        self.put("ZoomIt.zip.crdownload")
        for name in ("nothing.exe", "folder", "ZoomIt.zip.crdownload"):
            self.assertRefused("INVALID_ARGUMENT", name)

    def test_too_big_is_refused(self):
        self.put("big.bin", b"x" * 20)
        with mock.patch.object(config, "INPUT_FILE_MAX", 10):
            self.assertRefused("INVALID_ARGUMENT", "big.bin")

    def test_hard_link_to_another_host_file_is_refused(self):
        secret = self.home / "secret.txt"
        secret.write_bytes(b"host secret")
        try:
            os.link(secret, self.downloads / "innocent.txt")
        except OSError:
            self.skipTest("hard links not supported here")
        self.assertRefused("POLICY_DENIED", "innocent.txt")
        self.assertEqual(secret.read_bytes(), b"host secret")

    @unittest.skipUnless(sys.platform == "win32", "junctions")
    def test_junction_in_downloads_is_refused(self):
        target = self.home / "private"
        target.mkdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.downloads / "j"), str(target)], check=True,
                       capture_output=True)
        self.assertRefused("INVALID_ARGUMENT", "j")

    def test_file_changed_while_copying_is_refused(self):
        src = self.put("tool.exe", b"first")
        real = importer.check_file
        calls = []

        def check_then_change(p):
            calls.append(p)
            if len(calls) == 2:                                   # after the copy, before the re-check
                src.write_bytes(b"replaced with something else")
            return real(p)

        with mock.patch.object(importer, "check_file", side_effect=check_then_change):
            self.assertRefused("INVALID_ARGUMENT", "tool.exe")

    def test_bad_inbox_or_source_folder_is_refused(self):
        self.put("a.exe")
        with self.assertRaises(SandboxManagerError):
            import_to_inbox("a.exe", self.downloads, self.downloads)    # inbox inside a user folder
        with self.assertRaises(SandboxManagerError):
            import_to_inbox("a.exe", self.inbox, Path("relative"))
        with self.assertRaises(SandboxManagerError):
            import_to_inbox("a.exe", self.inbox, self.home / "missing")

    @unittest.skipUnless(sys.platform == "win32", "Known Folder")
    def test_default_source_is_the_real_downloads_folder(self):
        self.assertTrue(importer.downloads_folder().is_dir())


class FinishInbox(unittest.TestCase):
    """Session end: default delete; kept items are copied to Downloads, marked as from the internet."""

    setUp, put = ImportToInbox.setUp, ImportToInbox.put

    def fill(self):
        (self.inbox / "tool.exe").write_bytes(b"MZtool")
        z = self.inbox / "ZoomIt"
        (z / "docs").mkdir(parents=True)
        (z / "ZoomIt64.exe").write_bytes(b"MZ64")
        (z / "docs" / "Eula.txt").write_bytes(b"eula")

    def zone(self, p: Path) -> str:
        try:
            with open(f"{p}:Zone.Identifier", encoding="ascii", newline="") as f:
                return f.read()
        except OSError:
            return ""

    def test_default_deletes_everything_and_keeps_the_folder(self):
        self.fill()
        r = finish_inbox(self.inbox)
        self.assertEqual(r, {"kept": [], "deleted_files": 3})
        self.assertTrue(self.inbox.is_dir())
        self.assertEqual(list(self.inbox.iterdir()), [])
        self.assertEqual(list(self.downloads.iterdir()), [])
        self.fill()
        self.assertEqual(clear_inbox(self.inbox), 3)

    def test_kept_file_goes_to_downloads_marked_and_the_rest_is_deleted(self):
        self.fill()
        r = finish_inbox(self.inbox, ["tool.exe"], self.downloads)
        self.assertEqual((self.downloads / "tool.exe").read_bytes(), b"MZtool")
        self.assertEqual(r["kept"][0]["to"], str(self.downloads / "tool.exe"))
        self.assertEqual(list(self.inbox.iterdir()), [])
        if sys.platform == "win32":
            self.assertEqual(self.zone(self.downloads / "tool.exe"), "[ZoneTransfer]\r\nZoneId=3\r\n")

    def test_kept_folder_is_copied_whole(self):
        self.fill()
        finish_inbox(self.inbox, ["ZoomIt"], self.downloads)
        self.assertEqual((self.downloads / "ZoomIt" / "ZoomIt64.exe").read_bytes(), b"MZ64")
        self.assertEqual((self.downloads / "ZoomIt" / "docs" / "Eula.txt").read_bytes(), b"eula")
        if sys.platform == "win32":
            self.assertIn("ZoneId=3", self.zone(self.downloads / "ZoomIt" / "ZoomIt64.exe"))

    def test_keep_by_reported_name_inside_a_folder(self):
        self.fill()
        finish_inbox(self.inbox, ["ZoomIt\\ZoomIt64.exe"], self.downloads)
        self.assertEqual((self.downloads / "ZoomIt64.exe").read_bytes(), b"MZ64")

    def test_never_overwrites_and_skips_what_is_already_there(self):
        self.put("tool.exe", b"other")                          # a different file with the same name
        self.put("same.exe", b"MZsame")                         # the original of an inbox_import copy
        (self.inbox / "tool.exe").write_bytes(b"MZtool")
        (self.inbox / "same.exe").write_bytes(b"MZsame")
        r = finish_inbox(self.inbox, ["tool.exe", "same.exe"], self.downloads)
        self.assertEqual((self.downloads / "tool.exe").read_bytes(), b"other")
        self.assertEqual((self.downloads / "tool-2.exe").read_bytes(), b"MZtool")
        self.assertEqual([k["already_there"] for k in r["kept"]], [False, True])
        self.assertEqual(sorted(p.name for p in self.downloads.iterdir()), ["same.exe", "tool-2.exe", "tool.exe"])

    def test_bad_keep_name_deletes_nothing(self):
        self.fill()
        (self.home / "Documents").mkdir()
        (self.home / "Documents" / "secret.docx").write_bytes(b"private")
        for bad, code in (("..\\Documents\\secret.docx", "POLICY_DENIED"),
                          (str(self.home / "Documents" / "secret.docx"), "POLICY_DENIED"),
                          ("missing.exe", "INVALID_ARGUMENT"), ("", "INVALID_ARGUMENT")):
            with self.assertRaises(SandboxManagerError) as cm:
                finish_inbox(self.inbox, ["tool.exe", bad], self.downloads)
            self.assertEqual(cm.exception.code, code, bad)
        self.assertTrue((self.inbox / "tool.exe").is_file())    # nothing deleted
        self.assertEqual(list(self.downloads.iterdir()), [])     # nothing kept

    @unittest.skipUnless(sys.platform == "win32", "junctions")
    def test_links_are_removed_not_followed_nor_kept(self):
        target = self.home / "private"
        target.mkdir()
        (target / "keep.txt").write_bytes(b"keep")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.inbox / "j"), str(target)], check=True,
                       capture_output=True)
        with self.assertRaises(SandboxManagerError):
            finish_inbox(self.inbox, ["j\\keep.txt"], self.downloads)
        finish_inbox(self.inbox)
        self.assertFalse(os.path.lexists(self.inbox / "j"))
        self.assertEqual((target / "keep.txt").read_bytes(), b"keep")


class HostWrapperCleanup(unittest.TestCase):
    """tools/host_with_inputs.py: when the Host ends a session (cleanup), the inbox is emptied by default."""

    setUp = ImportToInbox.setUp

    def test_cleanup_empties_the_inbox(self):
        from host_with_inputs import InputGate
        manager = mock.Mock()
        manager.cleanup.return_value = {"removed": True}
        gate = InputGate(manager, [], ask=None, inbox=self.inbox)
        (self.inbox / "left.exe").write_bytes(b"MZ")
        session = mock.Mock(session_id="SES-1")
        self.assertEqual(gate.cleanup(session), {"removed": True})
        manager.cleanup.assert_called_once_with(session)
        self.assertEqual(list(self.inbox.iterdir()), [])


class HostWrapperConfirm(unittest.TestCase):
    """What Codex did on its own (2026-10-12) needs a "예" on the Host screen now: keeping files on the PC,
    ending the Sandbox, and fetching another file from the web when a similar one is in Downloads."""

    setUp, put = ImportToInbox.setUp, ImportToInbox.put

    def wrap(self, answer):
        import json
        import types
        from host_with_inputs import _add_inbox_tools
        self.asked, self.host_calls = [], []

        def ask(text, title):
            self.asked.append(text)
            return answer
        host_calls = self.host_calls

        class Backend:
            def call(self, tool, arguments):
                host_calls.append(tool)
                return {"content": [{"type": "text", "text": json.dumps({"ok": True})}], "isError": False}
        server = types.SimpleNamespace(
            HostBackend=Backend,
            load_tool_list=lambda *a, **k: [{"name": "session_stop", "description": "End the session."}])
        _add_inbox_tools(server, self.inbox, self.downloads, ask)
        self.server = server
        return lambda tool, args: json.loads(Backend().call(tool, args)["content"][0]["text"])

    def test_keeping_files_needs_a_yes(self):
        call = self.wrap(False)
        (self.inbox / "setup.msi").write_bytes(b"x")
        body = call("inbox_finish", {"keep": ["setup.msi"]})
        self.assertEqual(body["error"], "POLICY_DENIED")
        self.assertEqual(list(self.downloads.iterdir()), [])            # nothing kept
        self.assertTrue((self.inbox / "setup.msi").is_file())           # nothing deleted either
        self.assertIn("setup.msi", self.asked[0])
        call = self.wrap(True)
        self.assertTrue(call("inbox_finish", {"keep": ["setup.msi"]})["ok"])
        self.assertTrue((self.downloads / "setup.msi").is_file())

    def test_keeping_nothing_asks_nothing(self):
        call = self.wrap(False)
        self.assertTrue(call("inbox_finish", {"keep": []})["ok"])
        self.assertEqual(self.asked, [])

    def test_ending_the_sandbox_needs_a_yes(self):
        call = self.wrap(False)
        self.assertEqual(call("session_stop", {"reason": "CANNOT_COMPLETE"})["error"], "POLICY_DENIED")
        self.assertEqual(self.host_calls, [])                           # the Host never saw it
        call = self.wrap(True)
        self.assertTrue(call("session_stop", {"reason": "TASK_COMPLETE"})["ok"])
        self.assertEqual(self.host_calls, ["session_stop"])

    def test_stop_rule_is_in_the_tool_description(self):
        self.wrap(True)
        stop = next(t for t in self.server.load_tool_list() if t["name"] == "session_stop")
        self.assertIn("끝내", stop["description"])
        names = [t["name"] for t in self.server.load_tool_list()]
        self.assertEqual(names, ["session_stop", "inbox_download", "inbox_import", "inbox_finish"])

    def test_web_download_of_something_already_in_downloads_needs_a_yes(self):
        self.put("tailscale-setup-1.102.4.exe")
        call = self.wrap(False)
        with mock.patch("inbox_download.download_into_inbox") as dl:
            body = call("inbox_download", {"url": "https://pkgs.example.com/tailscale-setup-1.102.4-amd64.msi"})
        self.assertEqual(body["error"], "POLICY_DENIED")
        self.assertIn("tailscale-setup-1.102.4.exe", body["message"])
        dl.assert_not_called()

    def test_similar_files(self):
        from inbox_download import similar_files
        for n in ("tailscale-setup-1.102.4.exe", "setup.exe", "ZoomIt.zip"):
            self.put(n)
        self.assertEqual(similar_files(self.downloads, "https://x.example/tailscale-setup-1.102.4-amd64.msi"),
                         ["tailscale-setup-1.102.4.exe"])
        self.assertEqual(similar_files(self.downloads, "https://x.example/setup-helper.msi"), [])  # too short a match
        self.assertEqual(similar_files(self.downloads, "https://x.example/other-tool.exe"), [])


class ImportTool(unittest.TestCase):
    """tools/inbox_download.py: the MCP tool shape around import_to_inbox."""

    setUp, put = ImportToInbox.setUp, ImportToInbox.put

    def test_tool_answer_and_error(self):
        from inbox_download import IMPORT_TOOL, DownloadError, import_into_inbox
        self.assertEqual(IMPORT_TOOL["inputSchema"]["required"], ["name"])
        self.put("a.exe")
        body = import_into_inbox("a.exe", self.inbox, False, self.downloads)
        self.assertIn("input-check.html", body["next"])
        with self.assertRaises(DownloadError) as cm:
            import_into_inbox("..\\a.exe", self.inbox, False, self.downloads)
        self.assertEqual(cm.exception.code, "POLICY_DENIED")


if __name__ == "__main__":
    unittest.main()


class HostOnePerPC(unittest.TestCase):
    """The Codex app keeps one Host per conversation running: only the one that starts working holds the ports."""

    def test_only_our_older_host_is_ended(self):
        from host_with_inputs import take_over_ports
        procs = {101: ("python.exe tools/host_with_inputs.py --inbox x", 100),
                 100: ("venv\Scripts\python.exe C:/x/tools/host_with_inputs.py --inbox x", 50),
                 202: ("C:\Program Files\Other\server.exe --port 17443", 1)}
        held = {17443: {101}, 17444: {101}}
        killed = []

        def kill(pid):
            killed.append(pid)
            for s in held.values():
                s.discard(pid)
        ended = take_over_ports(me=999, listening=lambda p: set(held[p]), info=lambda pid: procs.get(pid, ("", 0)),
                                kill=kill, wait_s=0.1)
        self.assertEqual(sorted(ended), [100, 101])                  # the Host and its venv launcher
        held = {17443: {202}, 17444: set()}
        killed.clear()
        self.assertEqual(take_over_ports(me=999, listening=lambda p: set(held[p]),
                                         info=lambda pid: procs.get(pid, ("", 0)), kill=kill, wait_s=0.1), [])
        self.assertEqual(killed, [])                                 # someone else's program is never touched

    def test_backend_starts_only_when_a_host_tool_is_called(self):
        import types
        from host_with_inputs import _start_when_needed
        started, calls = [], []

        class Backend:
            ident = None

            def start(self):
                started.append(True)

            def call(self, tool, arguments):
                calls.append(tool)
                return {}
        server = types.SimpleNamespace(HostBackend=Backend)
        with mock.patch("host_with_inputs.take_over_ports", return_value=[]) as take:
            _start_when_needed(server, None, frozenset({"inbox_import"}))
            b = Backend()
            b.start()                                                # "initialize": nothing happens yet
            self.assertEqual(started, [])
            b.call("inbox_import", {})                               # local tool: still no ports
            self.assertEqual(started, [])
            b.call("task_submit", {})                                # now the Host starts, after taking over
            self.assertEqual(started, [True])
            take.assert_called_once()
        self.assertEqual(calls, ["inbox_import", "task_submit"])
