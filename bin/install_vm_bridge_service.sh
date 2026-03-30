#!/usr/bin/env bash
set -euo pipefail
BRIDGE_PY="$HOME/.openclaw/vm_bridge.py"
ENV_FILE="$HOME/.openclaw/vm_bridge.env"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/claw-vm-bridge.service"
mkdir -p "$SERVICE_DIR" "$HOME/.openclaw"
install -m 755 vm_bridge.py "$BRIDGE_PY"
if [[ ! -f "$ENV_FILE" ]]; then
  cp ../config/vm_bridge.env.example "$ENV_FILE"
  echo "Created $ENV_FILE - edit it before starting service"
fi
cat > "$SERVICE_FILE" <<SRV
[Unit]
Description=Claw VM Bridge (Discord GO/NO -> Mac API)
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/.openclaw
EnvironmentFile=%h/.openclaw/vm_bridge.env
ExecStart=/usr/bin/python3 %h/.openclaw/vm_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SRV
systemctl --user daemon-reload
systemctl --user enable --now claw-vm-bridge.service
echo "Installed and started claw-vm-bridge.service"
