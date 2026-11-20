#!/usr/bin/env bash
set -euo pipefail

LABEL="com.prompt-sidecar.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
echo "Prompt Sidecar stopped"
