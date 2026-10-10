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

from sandbox_manager import SandboxManagerError, config, import_to_inbox  # noqa: E402
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
