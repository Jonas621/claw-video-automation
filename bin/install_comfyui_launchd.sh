#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.jonas.comfyui.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"

cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.jonas.comfyui</string>
<key>ProgramArguments</key><array>
<string>$ROOT/bin/start_comfyui.sh</string>
</array>
<key>WorkingDirectory</key><string>$ROOT</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$ROOT/logs/comfyui.out.log</string>
<key>StandardErrorPath</key><string>$ROOT/logs/comfyui.err.log</string>
</dict></plist>
PL

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load "$PLIST"
launchctl kickstart -k "gui/$(id -u)/com.jonas.comfyui" >/dev/null 2>&1 || true

echo "Installed + started: com.jonas.comfyui"
echo "Logs: $ROOT/logs/comfyui.out.log / comfyui.err.log"
