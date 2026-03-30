#!/bin/sh
set -a
. /config/.env
set +a

: "${VM_USER:?missing VM_USER}"
: "${VM_HOST:?missing VM_HOST}"
: "${VM_SSH_PORT:=22}"
: "${VM_SSH_PASSWORD:?missing VM_SSH_PASSWORD}"
: "${VM_TUNNEL_PORT:=19090}"
: "${MAC_API_PORT:=8787}"

apk add --no-cache openssh-client sshpass > /dev/null 2>&1

echo "Tunnel: ${VM_USER}@${VM_HOST}:${VM_SSH_PORT} -> remote:${VM_TUNNEL_PORT} -> mac_api:${MAC_API_PORT}"

while true; do
    sshpass -p "$VM_SSH_PASSWORD" ssh \
        -o StrictHostKeyChecking=no \
        -o ServerAliveInterval=30 \
        -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes \
        -N \
        -p "$VM_SSH_PORT" \
        -R "${VM_TUNNEL_PORT}:mac_api:${MAC_API_PORT}" \
        "${VM_USER}@${VM_HOST}"
    echo "SSH tunnel disconnected, retrying in 5s..."
    sleep 5
done
