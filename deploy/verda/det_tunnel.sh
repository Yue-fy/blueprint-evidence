#!/usr/bin/env bash
# V1 cycle 8: keep an SSH tunnel 127.0.0.1:PORT -> the Verda instance's 127.0.0.1:PORT (a GPU det_server twin) alive
# until DEADLINE, reconnecting after drops. A local det_server with DET_REMOTE_URL=http://127.0.0.1:PORT offloads its
# cache misses there and falls back to its own CPU when the tunnel / instance is gone (teardown).
#   IP=<ip> PORTS="8154 8155" DEADLINE="2026-11-15 02:05" setsid nohup bash deploy/verda/det_tunnel.sh > <log> 2>&1 &
set -u
IP=${IP:?}; PORTS=${PORTS:-${PORT:-8154}}; dl=$(date -d "${DEADLINE:?}" +%s)
FW=""; for p in $PORTS; do FW="$FW -L 127.0.0.1:$p:127.0.0.1:$p"; done
while (( $(date +%s) < dl )); do
  ssh -N -i $HOME/.ssh/verda_junction -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes $FW root@$IP
  echo "[$(date +%H:%M:%S)] tunnel exited ($?); retry in 10 s"; sleep 10
done
echo "[$(date +%H:%M:%S)] deadline $DEADLINE: tunnel stopped"
