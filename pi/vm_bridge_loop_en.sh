#!/usr/bin/env bash
set -euo pipefail
while true; do
  VM_BRIDGE_ENV_FILE="$HOME/.openclaw/vm_bridge_en.env"  VM_BRIDGE_STATE_FILE="$HOME/.openclaw/vm_bridge_en_state.json"  VM_BRIDGE_LOG_FILE="$HOME/.openclaw/logs/vm_bridge_en.log"  /usr/bin/python3 "$HOME/.openclaw/vm_bridge.py" || true
  sleep 5
done
