#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV="$ROOT/config/mac_api.env"

COMFY_DIR="$HOME/ComfyUI"
COMFY_PORT="8188"

if [[ -f "$ENV" ]]; then
  dir_line="$(grep -E '^COMFYUI_DIR=' "$ENV" | tail -n1 || true)"
  port_line="$(grep -E '^COMFYUI_PORT=' "$ENV" | tail -n1 || true)"
  if [[ -n "$dir_line" ]]; then
    COMFY_DIR="${dir_line#COMFYUI_DIR=}"
  fi
  if [[ -n "$port_line" ]]; then
    COMFY_PORT="${port_line#COMFYUI_PORT=}"
  fi
fi

if [[ ! -f "$COMFY_DIR/main.py" ]]; then
  echo "ComfyUI main.py not found in $COMFY_DIR"
  echo "Run: bash $ROOT/bin/setup_comfyui.sh"
  exit 1
fi

if [[ -f "$COMFY_DIR/.venv/bin/python" ]]; then
  exec caffeinate -i "$COMFY_DIR/.venv/bin/python" "$COMFY_DIR/main.py" --listen 127.0.0.1 --port "$COMFY_PORT"
else
  exec caffeinate -i python3 "$COMFY_DIR/main.py" --listen 127.0.0.1 --port "$COMFY_PORT"
fi
