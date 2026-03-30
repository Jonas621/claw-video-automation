#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"

SERVICES=(
  "com.jonas.comfyui"
  "com.jonas.claw-mac-api"
  "com.jonas.claw-reverse-tunnel"
)

if [[ "${1:-}" == "--with-legacy-pipeline" ]]; then
  SERVICES+=("com.jonas.claw-video-pipeline")
fi

start_service() {
  local label="$1"
  local plist="${LAUNCH_AGENTS_DIR}/${label}.plist"

  if [[ ! -f "${plist}" ]]; then
    echo "[skip] ${label} (plist fehlt: ${plist})"
    return 0
  fi

  # Ensure loaded, then force restart process.
  launchctl bootstrap "${DOMAIN}" "${plist}" >/dev/null 2>&1 || true
  launchctl kickstart -k "${DOMAIN}/${label}" >/dev/null 2>&1 || true

  local line
  line="$(launchctl list | rg -N "${label}" || true)"
  if [[ -n "${line}" ]]; then
    echo "[ok]   ${label}"
  else
    echo "[warn] ${label} nicht in launchctl list sichtbar"
  fi
}

echo "== Restart local services (${DOMAIN}) =="
for svc in "${SERVICES[@]}"; do
  start_service "${svc}"
done

echo
echo "== Launchctl status =="
launchctl list | rg 'com\.jonas\.(comfyui|claw-mac-api|claw-reverse-tunnel|claw-video-pipeline)' || true

echo
echo "== Port check =="
lsof -iTCP -sTCP:LISTEN -nP | rg '127\.0\.0\.1:(8188|8787)' || true

echo
echo "Done."
