from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server


class PathConfigTests(unittest.TestCase):
    CHILD_PATHS = {
        "dsh": {
            "sessions_dir": "sessions",
            "workspace_storage": "storages/workspace.json",
            "token_file": "prompt-sidecar-bridge.token",
        },
        "workbuddy_local": {
            "db_path": "workbuddy.db",
            "projects_dir": "projects",
        },
        "gemini": {"sessions_dir": "tmp"},
        "claude": {"projects_dir": "projects"},
    }

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / "sidecar.config.json"
        self.enterContext(patch.object(server, "CONFIG_PATH", self.config_path))

    def write_config(self, config: dict[str, Any]) -> None:
        self.config_path.write_text(json.dumps(config), encoding="utf-8")

    def test_home_override_derives_missing_paths_for_each_adapter(self) -> None:
        for source, expected_paths in self.CHILD_PATHS.items():
            with self.subTest(source=source):
                home = self.root / source
                self.write_config({"bridges": {source: {"home": str(home)}}})
                bridge = server.load_sidecar_config()["bridges"][source]
                for field, relative in expected_paths.items():
                    with self.subTest(field=field):
                        self.assertEqual(bridge[field], str(home / relative))

    def test_repeated_home_only_saves_keep_implicit_child_paths_following_home(self) -> None:
        for source, expected_paths in self.CHILD_PATHS.items():
            with self.subTest(source=source):
                self.write_config({"bridges": {source: {"home": str(self.root / "old")}}})
                for name in ("first", "second"):
                    home = self.root / name
                    saved = server.save_sidecar_config({source: {"home": str(home)}})
                    loaded = server.load_sidecar_config()
                    for field, relative in expected_paths.items():
                        with self.subTest(save=name, field=field):
                            self.assertEqual(saved["bridges"][source][field], str(home / relative))
                            self.assertEqual(loaded["bridges"][source][field], str(home / relative))

    def test_explicit_child_paths_win_even_when_equal_to_previous_defaults(self) -> None:
        for source, paths in self.CHILD_PATHS.items():
            for field in paths:
                with self.subTest(source=source, field=field):
                    explicit = server.DEFAULT_SIDECAR_CONFIG["bridges"][source][field]
                    home = self.root / "initial"
                    self.write_config({"bridges": {source: {
                        "home": str(home), field: explicit,
                    }}})
                    self.assertEqual(
                        server.load_sidecar_config()["bridges"][source][field], explicit
                    )
                    home = self.root / "moved"
                    saved = server.save_sidecar_config({source: {"home": str(home)}})
                    loaded = server.load_sidecar_config()
                    for key, relative in paths.items():
                        expected = explicit if key == field else str(home / relative)
                        self.assertEqual(saved["bridges"][source][key], expected)
                        self.assertEqual(loaded["bridges"][source][key], expected)

    def test_new_explicit_child_override_survives_later_home_only_save(self) -> None:
        for source, paths in self.CHILD_PATHS.items():
            with self.subTest(source=source):
                self.write_config({})
                explicit = {field: str(self.root / field) for field in paths}
                server.save_sidecar_config({source: {
                    "home": str(self.root / "first"), **explicit,
                }})
                server.save_sidecar_config({source: {"home": str(self.root / "second")}})
                loaded = server.load_sidecar_config()["bridges"][source]
                for field, expected in explicit.items():
                    self.assertEqual(loaded[field], expected)

    def test_legacy_child_only_config_keeps_paths_and_default_home(self) -> None:
        for source, paths in self.CHILD_PATHS.items():
            with self.subTest(source=source):
                explicit = {field: str(self.root / field) for field in paths}
                self.write_config({"bridges": {source: explicit}})
                server.save_sidecar_config({source: {"enabled": False}})
                loaded = server.load_sidecar_config()["bridges"][source]
                self.assertEqual(
                    loaded["home"], server.DEFAULT_SIDECAR_CONFIG["bridges"][source]["home"]
                )
                for field, expected in explicit.items():
                    self.assertEqual(loaded[field], expected)

    def test_alias_sources_use_adapter_path_rules(self) -> None:
        for adapter, field, relative in (
            ("dsh", "sessions_dir", "sessions"),
            ("workbuddy", "db_path", "workbuddy.db"),
            ("gemini", "sessions_dir", "tmp"),
            ("claude", "projects_dir", "projects"),
        ):
            with self.subTest(adapter=adapter):
                source = "custom-agent"
                self.write_config({"bridges": {source: {
                    "adapter": adapter, "home": str(self.root / "initial"),
                }}})
                home = self.root / "moved"
                server.save_sidecar_config({source: {"home": str(home)}})
                bridge = server.resolve_bridge(source)
                self.assertEqual(bridge.config[field], str(home / relative))

    def test_home_and_explicit_child_tildes_expand_without_freezing_on_save(self) -> None:
        self.write_config({"bridges": {"dsh": {
            "home": "~/.test-dsh", "token_file": "~/.test-token",
        }}})
        saved = server.save_sidecar_config({"dsh": {"enabled": False}})
        self.assertEqual(saved["bridges"]["dsh"]["sessions_dir"],
                         str(Path.home() / ".test-dsh" / "sessions"))
        self.assertEqual(saved["bridges"]["dsh"]["token_file"], str(Path.home() / ".test-token"))
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))["bridges"]["dsh"]
        self.assertEqual(stored["home"], "~/.test-dsh")
        self.assertEqual(stored["token_file"], "~/.test-token")
        self.assertNotIn("sessions_dir", stored)

    def test_unrelated_save_does_not_freeze_default_paths_or_drop_other_settings(self) -> None:
        self.write_config({"extra": {"keep": True}})
        server.save_sidecar_config({"codex": {"enabled": False}})
        home = self.root / "dsh"
        server.save_sidecar_config({"dsh": {"home": str(home)}})
        loaded = server.load_sidecar_config()
        self.assertEqual(loaded["bridges"]["dsh"]["sessions_dir"], str(home / "sessions"))
        self.assertEqual(loaded["extra"], {"keep": True})
        self.assertFalse(loaded["bridges"]["codex"]["enabled"])

    def test_empty_file_child_paths_derive_from_home(self) -> None:
        for empty in ("", None):
            for source, paths in self.CHILD_PATHS.items():
                with self.subTest(source=source, empty=empty):
                    home = self.root / source
                    self.write_config({"bridges": {source: {
                        "home": str(home), **dict.fromkeys(paths, empty),
                    }}})
                    loaded = server.load_sidecar_config()["bridges"][source]
                    for field, relative in paths.items():
                        self.assertEqual(loaded[field], str(home / relative))

    def test_clearing_explicit_child_path_restores_home_derivation(self) -> None:
        home = self.root / "new-home"
        self.write_config({"bridges": {"dsh": {"sessions_dir": str(self.root / "explicit")}}})
        saved = server.save_sidecar_config({"dsh": {
            "home": str(home), "sessions_dir": "",
        }})
        self.assertEqual(saved["bridges"]["dsh"]["sessions_dir"], str(home / "sessions"))
        self.assertEqual(server.load_sidecar_config()["bridges"]["dsh"]["sessions_dir"],
                         str(home / "sessions"))


if __name__ == "__main__":
    unittest.main()
