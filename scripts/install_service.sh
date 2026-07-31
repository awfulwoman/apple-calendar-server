#!/bin/bash
set -euo pipefail

PLIST_LABEL="com.awfulwoman.apple-calendar-server"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

UV_PATH="$(which uv 2>/dev/null || true)"
if [ -z "$UV_PATH" ]; then
    echo "Error: uv not found on PATH. Install uv first."
    exit 1
fi

# 1. Build the venv (once, up front — NOT at launch). We deliberately do not
#    launch via `uv run`: that re-resolves the venv and can swap the interpreter
#    out from under us, changing its code identity and breaking the Calendar
#    TCC grant. See scripts/sign_runtime.sh for the full rationale.
"$UV_PATH" sync --project "$REPO_DIR"

# 2. Give the interpreter a stable code identity so TCC's Calendar grant
#    survives future rebuilds/upgrades (idempotent; safe to re-run).
"$REPO_DIR/scripts/setup_signing_cert.sh"
"$REPO_DIR/scripts/sign_runtime.sh"

VENV_PY="$REPO_DIR/.venv/bin/python3"

mkdir -p "$HOME/Library/LaunchAgents" "${REPO_DIR}/logs"
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_PY}</string>
        <string>-m</string>
        <string>apple_calendar_server.main</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${REPO_DIR}/logs/apple-calendar-server.log</string>
    <key>StandardErrorPath</key>
    <string>${REPO_DIR}/logs/apple-calendar-server.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
PLIST

# Must run as a gui/<uid> LaunchAgent (not a LaunchDaemon): EventKit's Calendar
# TCC permission is granted per-user, in a GUI session — a root-owned system
# daemon can never see the prompt or the grant.
launchctl bootout gui/"$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap gui/"$(id -u)" "$PLIST_PATH" 2>/dev/null || launchctl load "$PLIST_PATH"
echo "apple-calendar-server installed and started."
echo ""
echo "First launch shows a macOS permission dialog asking for Calendar access —"
echo "approve it once, in person, in this GUI session:"
echo "  System Settings > Privacy & Security > Calendars"
echo ""
echo "Thanks to the stable code signature, that grant now persists across"
echo "'uv sync', venv rebuilds and Python patch upgrades. Re-run"
echo "scripts/sign_runtime.sh after any manual rebuild to keep it."
