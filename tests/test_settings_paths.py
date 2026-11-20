from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server


class SettingsPathTests(unittest.TestCase):
    ADAPTERS = (
        ("dsh", "dsh"),
        ("claude", "claude"),
        ("workbuddy_local", "workbuddy"),
        ("gemini", "gemini"),
        ("custom-agent", "dsh"),
    )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / "sidecar.config.json"
        self.enterContext(patch.object(server, "CONFIG_PATH", self.config_path))

    def write_bridge(self, source: str, bridge: dict[str, Any]) -> None:
        self.config_path.write_text(
            json.dumps({"bridges": {source: bridge}}), encoding="utf-8"
        )

    def submit_form(self, source: str, fields: dict[str, str]) -> dict[str, Any]:
        fixture = {
            "source": source,
            "bridges": server.public_bridge_configs(),
            "fields": fields,
        }
        script = Path(__file__).with_suffix(".js")
        result = subprocess.run(
            ["node", str(script), "--payload"],
            input=json.dumps(fixture), text=True, capture_output=True, check=True, timeout=10,
        )
        payload = json.loads(result.stdout)
        server.save_sidecar_config(payload["bridges"])
        return server.load_sidecar_config()["bridges"][source]

    def test_repeated_home_edits_through_form_keep_implicit_paths_following_home(self) -> None:
        for source, adapter in self.ADAPTERS:
            with self.subTest(source=source):
                self.write_bridge(source, {"adapter": adapter, "home": str(self.root / "old")})
                children = server._bridge_child_paths(adapter)
                for name in ("first", "second"):
                    editable = server.public_bridge_configs()[source]
                    self.assertTrue(all(editable[field] == "" for field in children))
                    home = str(self.root / name)
                    fields = {"home": home, **{field: editable[field] for field in children}}
                    loaded = self.submit_form(source, fields)
                    for field, relative in children.items():
                        self.assertEqual(loaded[field], str(Path(home) / relative))

    def test_explicit_tilde_paths_survive_form_save_then_clear_to_follow_home(self) -> None:
        for source, adapter in self.ADAPTERS:
            with self.subTest(source=source):
                children = server._bridge_child_paths(adapter)
                explicit = {field: f"~/.fixture/{field}" for field in children}
                self.write_bridge(source, {
                    "adapter": adapter, "home": "~/.fixture/home", **explicit,
                })
                editable = server.public_bridge_configs()[source]
                self.assertEqual(editable["home"], "~/.fixture/home")
                for field, value in explicit.items():
                    self.assertEqual(editable[field], value)
                loaded = self.submit_form(source, {"home": "/fixture/moved", **explicit})
                stored = json.loads(self.config_path.read_text())["bridges"][source]
                for field, value in explicit.items():
                    self.assertEqual(loaded[field], str(Path(value).expanduser()))
                    self.assertEqual(stored[field], value)
                loaded = self.submit_form(source, {
                    "home": "/fixture/moved", **dict.fromkeys(children, ""),
                })
                for field, relative in children.items():
                    self.assertEqual(loaded[field], str(Path("/fixture/moved") / relative))

    def test_saving_other_bridge_does_not_freeze_implicit_paths(self) -> None:
        self.write_bridge("dsh", {"home": "/fixture/old"})
        self.submit_form("codex", {"home": "/fixture/codex"})
        saved = server.save_sidecar_config({"dsh": {"home": "/fixture/new"}})
        self.assertEqual(saved["bridges"]["dsh"]["sessions_dir"], "/fixture/new/sessions")


if __name__ == "__main__":
    unittest.main()
