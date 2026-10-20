"""Verify the actual Windows child has no console, while output remains readable."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandbox_manager.wsb import WsbCli


@unittest.skipUnless(sys.platform == "win32", "Windows console API")
class WsbConsole(unittest.TestCase):
    def test_cli_child_has_no_console_and_returns_output(self):
        code = (
            "import ctypes,json,sys;"
            "ctypes.windll.kernel32.GetConsoleWindow.restype=ctypes.c_void_p;"
            "print(json.dumps({'console':ctypes.windll.kernel32.GetConsoleWindow()}));"
            "print('stderr captured',file=sys.stderr)"
        )
        data, text = WsbCli(exe=sys.executable)._run("-c", code, timeout=10)
        self.assertIsNone(data["console"])
        self.assertIn("stderr captured", text)


if __name__ == "__main__":
    unittest.main()
