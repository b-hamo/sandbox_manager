import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sandbox_manager.management_service import ManagementService
from sandbox_manager.errors import SandboxManagerError


class ViewerRegistrationTests(unittest.TestCase):
    def test_locator_binding_and_gui_journal_preserved(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"LOCALAPPDATA": temp}):
            base = Path(temp)
            package = base / "package"
            package.mkdir()
            root = base / "runtime"
            (package / "management.json").write_text(json.dumps({"schema_version": 1, "root": str(root), "owner_sid": "owner"}))
            (package / "MinoVmService.exe").touch()
            client = ManagementService(package, root, base / "state")
            session = "SES-" + "1" * 32
            record = {"id": "11111111-1111-4111-8111-111111111111", "container_id": "22222222-2222-4222-8222-222222222222", "owner_sid": "owner", "label": session}
            client._register_viewer(session, record)
            path = base / "MinoBox" / "management-viewer" / (session + ".json")
            saved = json.loads(path.read_text())
            self.assertNotIn("capability", saved)
            self.assertEqual(saved["container_id"], record["container_id"])
            self.assertEqual(saved["lease_path"], str(client.state / (session + ".json")))
            saved["stop_request_id"] = "33333333-3333-4333-8333-333333333333"
            path.write_text(json.dumps(saved))
            before = path.read_bytes()
            client._register_viewer(session, record)
            self.assertEqual(path.read_bytes(), before)
            for field in ("id", "container_id", "owner_sid", "label"):
                changed = dict(record, **{field: "44444444-4444-4444-8444-444444444444"})
                with self.assertRaises(SandboxManagerError):
                    client._register_viewer(session, changed)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
