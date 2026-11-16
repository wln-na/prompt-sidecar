#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.prompt-sidecar.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/PromptSidecar"
PORT="${PORT:-8765}"

find_python() {
    local candidate
    for candidate in \
        "$(command -v python3.14 2>/dev/null || true)" \
        "$(command -v python3 2>/dev/null || true)" \
        "$HOME/miniconda3/bin/python3"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            if "$candidate" -c 'import compression.zstd' >/dev/null 2>&1 || \
                "$candidate" -c 'import zstandard' >/dev/null 2>&1; then
                printf '%s\n' "$candidate"
                return 0
            fi
        fi
    done
    for candidate in \
        "$(command -v python3.14 2>/dev/null || true)" \
        "$(command -v python3 2>/dev/null || true)" \
        "$HOME/miniconda3/bin/python3"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

if [ -z "${PYTHON_BIN:-}" ]; then
    PYTHON_BIN="$(find_python)" || {
        echo "No usable Python 3 executable found. Set PYTHON_BIN explicitly." >&2
        exit 1
    }
fi
if [ -z "${CODEX_BIN:-}" ]; then
    CODEX_BIN="$(command -v codex 2>/dev/null || true)"
fi
if [ -z "$CODEX_BIN" ] || [ ! -x "$CODEX_BIN" ]; then
    echo "Codex executable not found. Set CODEX_BIN explicitly." >&2
    exit 1
fi

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && \
    curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "Prompt Sidecar is already running: http://127.0.0.1:$PORT"
    exit 0
fi
if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "Port $PORT is already serving Prompt Sidecar from another LaunchAgent; stop it first." >&2
    exit 1
fi

umask 077
mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
touch "$LOG_DIR/prompt-sidecar.log" "$LOG_DIR/prompt-sidecar.err.log"
chmod 600 "$LOG_DIR/prompt-sidecar.log" "$LOG_DIR/prompt-sidecar.err.log"

"$PYTHON_BIN" "$ROOT_DIR/scripts/generate_launch_agent.py" \
    --template "$ROOT_DIR/launch-agent.plist.template" \
    --output "$PLIST" \
    --label "$LABEL" \
    --python-bin "$PYTHON_BIN" \
    --codex-bin "$CODEX_BIN" \
    --project-dir "$ROOT_DIR" \
    --log-dir "$LOG_DIR" \
    --port "$PORT"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
        echo "Prompt Sidecar started: http://127.0.0.1:$PORT"
        echo "Logs: $LOG_DIR"
        exit 0
    fi
    sleep 1
done

echo "Prompt Sidecar failed to start; recent stderr:" >&2
tail -n 40 "$LOG_DIR/prompt-sidecar.err.log" >&2 || true
exit 1
