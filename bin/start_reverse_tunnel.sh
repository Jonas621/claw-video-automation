#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV="$ROOT/config/.env"
if [[ ! -f "$ENV" ]]; then
  echo "Missing $ENV"
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV"
: "${VM_USER:?missing VM_USER}"
: "${VM_HOST:?missing VM_HOST}"
: "${VM_SSH_PORT:=22}"
: "${VM_SSH_PASSWORD:?missing VM_SSH_PASSWORD}"
: "${MAC_API_PORT:=8787}"
: "${VM_TUNNEL_PORT:=19090}"

while true; do
/usr/bin/expect <<EXP
set timeout -1
spawn -noecho ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -p $VM_SSH_PORT -R $VM_TUNNEL_PORT:127.0.0.1:$MAC_API_PORT $VM_USER@$VM_HOST
expect {
  -re {.*yes/no.*} { send "yes\r"; exp_continue }
  -re {.*[Pp]assword:.*} { send "$VM_SSH_PASSWORD\r"; exp_continue }
  eof
}
EXP
sleep 2
done
