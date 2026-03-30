#!/usr/bin/env bash
set -euo pipefail

COMFY_DIR="${COMFYUI_DIR:-$HOME/ComfyUI}"

if [[ ! -d "$COMFY_DIR" ]]; then
  git clone https://github.com/comfyanonymous/ComfyUI.git "$COMFY_DIR"
fi

cd "$COMFY_DIR"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "ComfyUI ready at: $COMFY_DIR"
echo "Start manually with: $COMFY_DIR/.venv/bin/python $COMFY_DIR/main.py --listen 127.0.0.1 --port 8188"
