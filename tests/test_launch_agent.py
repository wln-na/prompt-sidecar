from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LaunchAgentStartupTests(unittest.TestCase):
    def run_start(
        self, *, enable_fails: bool = False
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        """Run the actual script with launchd disabled, without touching user services/files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            commands = {
                "launchctl": '''#!/bin/bash
echo "$*" >> "$START_TEST_DIR/calls"
case "$1" in
    print|bootout) exit 1 ;;
    enable)
        if [ "$START_TEST_ENABLE_FAILS" = 1 ]; then exit 1; fi
        /usr/bin/touch "$START_TEST_DIR/enabled" ;;
    bootstrap)
        if [ ! -f "$START_TEST_DIR/enabled" ]; then
            echo "Bootstrap failed: 5: Input/output error" >&2
            exit 5
        fi
        /usr/bin/touch "$START_TEST_DIR/running" ;;
    *) exit 2 ;;
esac
''',
                "curl": '#!/bin/bash\n[ -f "$START_TEST_DIR/running" ]\n',
                "mkdir": "#!/bin/bash\nexit 0\n",
                "touch": "#!/bin/bash\nexit 0\n",
                "chmod": "#!/bin/bash\nexit 0\n",
            }
            for name, source in commands.items():
                command = temp / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "scripts" / "start.sh")],
                capture_output=True,
                text=True,
                timeout=5,
                env={
                    **os.environ,
                    "PATH": f"{temp}:/usr/bin:/bin",
                    "PYTHON_BIN": "/usr/bin/true",
                    "CODEX_BIN": "/usr/bin/true",
                    "START_TEST_DIR": str(temp),
                    "START_TEST_ENABLE_FAILS": "1" if enable_fails else "0",
                },
            )
            return result, (temp / "calls").read_text(encoding="utf-8").splitlines()

    def test_start_enables_disabled_service_before_bootstrap(self) -> None:
        result, calls = self.run_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = f"gui/{os.getuid()}/com.prompt-sidecar.agent"
        enable_index = calls.index(f"enable {target}")
        bootstrap_index = next(i for i, call in enumerate(calls) if call.startswith("bootstrap "))
        self.assertLess(enable_index, bootstrap_index)
        self.assertIn("Prompt Sidecar started:", result.stdout)

    def test_enable_failure_does_not_bootstrap_or_report_success(self) -> None:
        result, calls = self.run_start(enable_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(call.startswith("enable ") for call in calls))
        self.assertFalse(any(call.startswith("bootstrap ") for call in calls))
        self.assertNotIn("Prompt Sidecar started:", result.stdout)


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
                    str(ROOT / "config" / "launch-agent.plist.template"),
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
                    "9876",
                ],
                check=True,
                env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )

            plist = plistlib.loads(output.read_bytes())
            self.assertEqual(plist["Label"], "com.prompt-sidecar.agent")
            self.assertEqual(plist["ProgramArguments"][0], str(python_bin.resolve()))
            self.assertEqual(
                plist["ProgramArguments"][1],
                str((ROOT / "backend" / "server.py").resolve()),
            )
            self.assertEqual(plist["WorkingDirectory"], str(ROOT.resolve()))
            self.assertTrue(plist["StandardOutPath"].endswith("prompt-sidecar.log"))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("__", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
