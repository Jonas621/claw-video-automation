#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.jonas.claw-mac-api.plist"
PY_BIN="$(command -v python3)"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.jonas.claw-mac-api</string>
<key>ProgramArguments</key><array>
<string>$PY_BIN</string>
<string>$ROOT/bin/mac_api.py</string>
</array>
<key>WorkingDirectory</key><string>$ROOT</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$ROOT/logs/mac_api.out.log</string>
<key>StandardErrorPath</key><string>$ROOT/logs/mac_api.err.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"
launchctl kickstart -k "gui/$(id -u)/com.jonas.claw-mac-api" >/dev/null 2>&1 || true
echo "Installed: com.jonas.claw-mac-api"
