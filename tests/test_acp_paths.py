from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server


class AcpPathTests(unittest.TestCase):
    def test_configured_remote_directory_is_used_for_read_and_send(self) -> None:
        config = {"acp_cwd": "/remote/project"}
        client = MagicMock()
        client.request.return_value = {}
        task = {"link": "https://example.test/acp", "token": "fixture-only"}
        with patch.object(server, "get_workbuddy_task", return_value=task):
            with patch.object(server, "WorkBuddyACPClient") as factory:
                factory.return_value.__enter__.return_value = client
                server.workbuddy_acp_load(config, "task-one")
                server.send_workbuddy_prompt(config, "task-one", "fixture prompt")
        loads = [call for call in client.request.call_args_list if call.args[0] == "session/load"]
        self.assertEqual(len(loads), 2)
        for call in loads:
            self.assertEqual(call.args[1]["cwd"], "/remote/project")

    def test_default_remains_compatible_and_relative_paths_are_rejected(self) -> None:
        self.assertEqual(server.workbuddy_acp_cwd({}), "/workspace")
        for cwd in ("relative", "~/project", "/bad\x00path"):
            with self.subTest(cwd=cwd), self.assertRaises(ValueError):
                server.workbuddy_acp_cwd({"acp_cwd": cwd})

    def test_remote_directory_survives_config_validation(self) -> None:
        cleaned = server._sanitize_bridges({"cloud": {"acp_cwd": "/remote/project"}})
        self.assertEqual(cleaned["cloud"]["acp_cwd"], "/remote/project")


class AcpConfigPathTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config_path = Path(temporary.name) / "sidecar.config.json"
        self.enterContext(patch.object(server, "CONFIG_PATH", self.config_path))

    def write_config(self, cwd: str) -> None:
        self.config_path.write_text(json.dumps({"bridges": {"cloud": {
            "adapter": "workbuddy", "surface": "cloud_task", "acp_cwd": cwd,
        }}}), encoding="utf-8")

    def test_load_rejects_invalid_remote_paths_before_local_expansion(self) -> None:
        for cwd in ("~/remote-project", "relative", "/bad\x00path"):
            with self.subTest(cwd=cwd):
                self.write_config(cwd)
                with self.assertRaisesRegex(ValueError, "absolute path on the remote host"):
                    server.load_sidecar_config()

    def test_absolute_remote_path_survives_load_save_and_reload(self) -> None:
        cwd = "/remote/project"
        self.write_config(cwd)
        self.assertEqual(server.load_sidecar_config()["bridges"]["cloud"]["acp_cwd"], cwd)
        updated = "/remote/another-project"
        saved = server.save_sidecar_config({"cloud": {"acp_cwd": updated}})
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))
        loaded = server.load_sidecar_config()
        for config in (saved, stored, loaded):
            self.assertEqual(config["bridges"]["cloud"]["acp_cwd"], updated)

    def test_invalid_save_leaves_existing_config_unchanged(self) -> None:
        self.write_config("/remote/project")
        original = self.config_path.read_bytes()
        for cwd in ("~/remote-project", "relative", "/bad\x00path"):
            with self.subTest(cwd=cwd):
                with self.assertRaisesRegex(ValueError, "absolute path on the remote host"):
                    server.save_sidecar_config({"cloud": {"acp_cwd": cwd}})
                self.assertEqual(self.config_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
