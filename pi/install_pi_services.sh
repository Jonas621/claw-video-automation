#!/usr/bin/env bash
# Install all Pi-side services for claw-video-automation.
# Run this on the Raspberry Pi after cloning the repo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OPENCLAW_HOME="$HOME/.openclaw"
SERVICE_DIR="$HOME/.config/systemd/user"

mkdir -p "$OPENCLAW_HOME/logs" "$SERVICE_DIR"

echo "=== Copying vm_bridge.py ==="
install -m 755 "$SCRIPT_DIR/../bin/vm_bridge.py" "$OPENCLAW_HOME/vm_bridge.py"

echo "=== Copying loop scripts ==="
install -m 755 "$SCRIPT_DIR/vm_bridge_loop_en.sh" "$OPENCLAW_HOME/vm_bridge_loop_en.sh"
install -m 755 "$SCRIPT_DIR/vm_bridge_loop_de.sh" "$OPENCLAW_HOME/vm_bridge_loop_de.sh"

echo "=== Copying env examples (will NOT overwrite existing) ==="
for f in vm_bridge_en.env vm_bridge_de.env; do
  if [[ ! -f "$OPENCLAW_HOME/$f" ]]; then
    cp "$SCRIPT_DIR/${f}.example" "$OPENCLAW_HOME/$f"
    echo "  Created $OPENCLAW_HOME/$f — edit before starting!"
  else
    echo "  $OPENCLAW_HOME/$f already exists, skipping"
  fi
done

echo "=== Installing systemd services ==="
cp "$SCRIPT_DIR/openclaw-gateway.service" "$SERVICE_DIR/"
cp "$SCRIPT_DIR/claw-vm-bridge-en.service" "$SERVICE_DIR/"
cp "$SCRIPT_DIR/claw-vm-bridge-de.service" "$SERVICE_DIR/"

systemctl --user daemon-reload

echo "=== Enabling services ==="
systemctl --user enable openclaw-gateway.service
systemctl --user enable claw-vm-bridge-en.service
systemctl --user enable claw-vm-bridge-de.service

echo ""
echo "Done! Before starting, make sure to:"
echo "  1. Edit $OPENCLAW_HOME/vm_bridge_en.env and vm_bridge_de.env"
echo "  2. Set OPENCLAW_GATEWAY_TOKEN in $SERVICE_DIR/openclaw-gateway.service"
echo "  3. Run: openclaw onboard (if first time)"
echo "  4. Run: systemctl --user start openclaw-gateway claw-vm-bridge-en claw-vm-bridge-de"
echo "  5. Enable linger: sudo loginctl enable-linger \$USER"
