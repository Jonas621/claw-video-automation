#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.jonas.claw-video-pipeline.plist"
PY_BIN="$(command -v python3)"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.jonas.claw-video-pipeline</string>
    <key>ProgramArguments</key>
    <array>
      <string>$PY_BIN</string>
      <string>$ROOT/bin/run_pipeline.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>$ROOT/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/logs/launchd.err.log</string>
  </dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
if ! launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
  # Fallback for shells/environments where bootstrap to gui domain fails.
  launchctl unload "$PLIST" >/dev/null 2>&1 || true
  launchctl load "$PLIST"
fi
launchctl kickstart -k "gui/$(id -u)/com.jonas.claw-video-pipeline" >/dev/null 2>&1 || true

echo "Installed + started: com.jonas.claw-video-pipeline"
echo "Plist: $PLIST"
