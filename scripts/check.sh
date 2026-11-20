#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON_BIN="${PYTHON_BIN:-python3}"

for tool in "$PYTHON_BIN" node gitleaks; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "Required validation tool not found: $tool" >&2
        exit 2
    fi
done

"$PYTHON_BIN" -c '
try:
    from compression import zstd
except ImportError:
    try:
        import zstandard
    except ImportError:
        raise SystemExit("DSH tests require zstandard: install requirements/test.txt")
'

PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -m unittest discover -s tests -v
for test_file in tests/test_*.js; do
    node "$test_file"
done
for source_file in frontend/app.js dsh-plugin/prompt-sidecar-bridge/index.js; do
    node --check "$source_file"
done
for source_file in scripts/start.sh scripts/stop.sh scripts/check.sh scripts/secret-scan.sh \
    .githooks/pre-push; do
    bash -n "$source_file"
done
git diff --check
bash scripts/secret-scan.sh
