#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"

if ! command -v gitleaks >/dev/null 2>&1; then
    echo "[secret-scan] gitleaks is required: brew install gitleaks" >&2
    exit 2
fi

echo "[secret-scan] scanning working tree"
gitleaks dir . --no-banner --redact --log-level warn

commit_count="$(git rev-list --all --count 2>/dev/null || printf '0')"
if [ "$commit_count" -gt 0 ]; then
    echo "[secret-scan] scanning $commit_count local commit(s)"
    gitleaks git . --no-banner --redact --log-level warn --log-opts="--all"
else
    echo "[secret-scan] local history has no commits; history scan skipped"
fi

if [ "${1:-}" = "--check-remote" ]; then
    remote_url="${2:-}"
    if [ -z "$remote_url" ]; then
        echo "[secret-scan] remote URL is required for --check-remote" >&2
        exit 2
    fi
    remote_refs="$(git ls-remote --heads "$remote_url")"
    if [ -n "$remote_refs" ]; then
        echo "[secret-scan] remote already has branches; inspect its old commits before pushing" >&2
        printf '%s\n' "$remote_refs" >&2
        echo "[secret-scan] override only after manual review: PROMPT_SIDECAR_ALLOW_REMOTE_HISTORY=1" >&2
        if [ "${PROMPT_SIDECAR_ALLOW_REMOTE_HISTORY:-}" != "1" ]; then
            exit 3
        fi
    else
        echo "[secret-scan] remote has no branches"
    fi
fi
