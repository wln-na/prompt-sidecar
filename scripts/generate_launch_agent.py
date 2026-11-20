#!/usr/bin/env python3
"""Render the local LaunchAgent plist without committing machine-specific paths."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
from pathlib import Path


def executable_path(value: str, label: str) -> str:
    path = Path(value).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"{label} is not executable: {path}")
    return str(path)


def generate_launch_agent(
    *,
    template_path: Path,
    output_path: Path,
    label: str,
    python_bin: str,
    codex_bin: str,
    project_dir: Path,
    log_dir: Path,
    port: int,
) -> dict:
    template = plistlib.loads(template_path.read_bytes())
    python_path = executable_path(python_bin, "Python")
    codex_path = executable_path(codex_bin, "Codex")
    project_path = project_dir.expanduser().resolve()
    server_script = project_path / "backend" / "server.py"
    if not server_script.is_file():
        raise ValueError(f"server.py not found: {server_script}")

    log_path = log_dir.expanduser().resolve()
    log_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_log = log_path / "prompt-sidecar.log"
    error_log = log_path / "prompt-sidecar.err.log"
    path_entries = [
        str(Path(codex_path).parent),
        str(Path(shutil.which("node") or "").parent) if shutil.which("node") else "",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    path_entries = list(dict.fromkeys(entry for entry in path_entries if entry))

    template["Label"] = label
    template["EnvironmentVariables"] = {
        "CODEX_BIN": codex_path,
        "PATH": ":".join(path_entries),
        "PYTHONUNBUFFERED": "1",
    }
    for name in ("CODEX_HOME",):
        value = os.environ.get(name, "").strip()
        if value:
            template["EnvironmentVariables"][name] = str(Path(value).expanduser().resolve())
    template["ProgramArguments"] = [
        python_path,
        str(server_script),
        "--port",
        str(port),
    ]
    template["StandardOutPath"] = str(output_log)
    template["StandardErrorPath"] = str(error_log)
    template["WorkingDirectory"] = str(project_path)

    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_path.write_bytes(plistlib.dumps(template, sort_keys=False))
    output_path.chmod(0o600)
    return template


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Prompt Sidecar LaunchAgent")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--python-bin", required=True)
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9876)
    args = parser.parse_args()
    generate_launch_agent(
        template_path=args.template.resolve(),
        output_path=args.output.expanduser().resolve(),
        label=args.label,
        python_bin=args.python_bin,
        codex_bin=args.codex_bin,
        project_dir=args.project_dir,
        log_dir=args.log_dir,
        port=args.port,
    )


if __name__ == "__main__":
    main()
