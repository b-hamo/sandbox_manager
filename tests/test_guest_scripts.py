"""The Guest input watcher (config.INPUT_WATCH_PS1), run for real by this PC's Windows PowerShell 5.1.

Its fixed Guest folders are pointed at temp folders; the rest is the shipped text. Regression: with more
than one manifest entry, PowerShell 5.1 handed the whole array over as one entry (2026-10-08).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox_manager import config  # noqa: E402


@unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell.exe"), "Windows PowerShell")
class InputWatcher(unittest.TestCase):
    def test_copies_and_checks_each_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            inbox, user, pkg, home = t / "Inbox", t / "UserFiles", t / "RunnerPackage", t / "home"
            for d in (inbox / "ZoomIt", user, pkg, home / "Desktop"):
                d.mkdir(parents=True)
            files = {("inbox", r"ZoomIt\a.exe"): b"AAAA", ("inbox", "b.txt"): b"BB",
                     ("inbox", r"ZoomIt\bad.bin"): b"real", ("input", "memo.txt"): b"memo"}
            manifest = []
            for (src, name), data in files.items():
                ((inbox if src == "inbox" else user) / name).write_bytes(data)
                manifest.append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "from": src})
            manifest[2]["sha256"] = "0" * 64                   # not what the Host listed: must FAIL
            manifest.append({"name": r"..\escape.txt", "size": 1, "sha256": "0" * 64, "from": "inbox"})
            (pkg / config.INPUT_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
            script = (config.INPUT_WATCH_PS1.replace(r"C:\RunnerPackage", str(pkg))
                      .replace(r"'C:\Inbox'", f"'{inbox}'").replace(r"'C:\UserFiles'", f"'{user}'")
                      .replace(r"Global\SecureCuaInputWatch", rf"Local\SecureCuaTest{os.getpid()}"))
            (t / "w.ps1").write_text(script, encoding="utf-8-sig")
            p = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(t / "w.ps1")],
                                 env={**os.environ, "USERPROFILE": str(home)})
            check_file = home / "Desktop" / "input-check.txt"
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if check_file.exists() and check_file.read_text(encoding="utf-8-sig").count("\n") >= 5:
                        break
                    time.sleep(0.5)
            finally:
                p.kill()
                p.wait()
            check = check_file.read_text(encoding="utf-8-sig")
            self.assertEqual(check.count("OK   "), 3, check)
            self.assertIn(r"FAIL ZoomIt\bad.bin", check)
            self.assertIn(r"FAIL ..\escape.txt bad name", check)
            copied = sorted(str(x.relative_to(home / "Desktop" / "Input"))
                            for x in (home / "Desktop" / "Input").rglob("*") if x.is_file())
            self.assertEqual(copied, [r"ZoomIt\a.exe", "b.txt", "memo.txt"])
            self.assertTrue((home / "Desktop" / "input-check.html").is_file())


if __name__ == "__main__":
    unittest.main()
