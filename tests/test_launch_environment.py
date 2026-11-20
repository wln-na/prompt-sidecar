from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.generate_launch_agent import generate_launch_agent


class LaunchEnvironmentTests(unittest.TestCase):
    def test_preserves_path_overrides_without_copying_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            overrides = {
                "CODEX_HOME": str(root / "custom-codex"),
                "OPENAI_API_KEY": "fixture-only",
                "PRIVATE_API_TOKEN": "fixture-only",
            }
            with patch.dict(os.environ, overrides):
                plist = generate_launch_agent(
                    template_path=ROOT / "config" / "launch-agent.plist.template",
                    output_path=root / "agent.plist",
                    label="test.prompt-sidecar",
                    python_bin=sys.executable,
                    codex_bin=sys.executable,
                    project_dir=ROOT,
                    log_dir=root / "logs",
                    port=9876,
                )
            environment = plist["EnvironmentVariables"]
            self.assertEqual(environment["CODEX_HOME"], overrides["CODEX_HOME"])
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("PRIVATE_API_TOKEN", environment)
            self.assertNotIn("fixture-only", (root / "agent.plist").read_text())
            self.assertEqual(plist["ProgramArguments"][-1], "9876")


if __name__ == "__main__":
    unittest.main()
