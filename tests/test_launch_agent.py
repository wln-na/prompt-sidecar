from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LaunchAgentGeneratorTests(unittest.TestCase):
    def test_generates_user_specific_plist_outside_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            python_bin = temp / "bin" / "python3"
            codex_bin = temp / "bin" / "codex"
            output = temp / "LaunchAgents" / "com.prompt-sidecar.agent.plist"
            log_dir = temp / "Logs" / "PromptSidecar"
            python_bin.parent.mkdir(parents=True)
            python_bin.write_text("", encoding="utf-8")
            codex_bin.write_text("", encoding="utf-8")
            python_bin.chmod(0o755)
            codex_bin.chmod(0o755)

            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "generate_launch_agent.py"),
                    "--template",
                    str(ROOT / "launch-agent.plist.template"),
                    "--output",
                    str(output),
                    "--label",
                    "com.prompt-sidecar.agent",
                    "--python-bin",
                    str(python_bin),
                    "--codex-bin",
                    str(codex_bin),
                    "--project-dir",
                    str(ROOT),
                    "--log-dir",
                    str(log_dir),
                    "--port",
                    "8765",
                ],
                check=True,
                env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )

            plist = plistlib.loads(output.read_bytes())
            self.assertEqual(plist["Label"], "com.prompt-sidecar.agent")
            self.assertEqual(plist["ProgramArguments"][0], str(python_bin.resolve()))
            self.assertEqual(
                plist["ProgramArguments"][1],
                str((ROOT / "server.py").resolve()),
            )
            self.assertEqual(plist["WorkingDirectory"], str(ROOT.resolve()))
            self.assertTrue(plist["StandardOutPath"].endswith("prompt-sidecar.log"))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("__", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
