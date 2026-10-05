#!/usr/bin/env bash
# Build the image and (re)start both containers on this host.
#
#   scripts/up.sh [state dir] [allowed peers]
#
# Both containers use the host network and bind 127.0.0.1 only, so nothing is
# reachable until it's published with `tailscale serve` (relay) and
# `tailscale funnel` (inbox). Settings live in <state dir>/{relay,inbox}.env;
# they are created on the first run and never overwritten, so re-running
# only updates the code.

set -euo pipefail

STATE=${1:-$HOME/.local/share/light-relay}
PEERS=${2:-"100.68.208.23,$(tailscale ip -4 2>/dev/null | head -1)"}  # muse, plus this machine
SRC=$(cd "$(dirname "$0")/.." && pwd)

mkdir -p "$STATE/relay" "$STATE/inbox"
if [ "$(id -u)" = 0 ]; then
  # Unraid: appdata belongs to nobody:users, which the image already runs as.
  chown 99:100 "$STATE/relay" "$STATE/inbox"
  RUN_AS=()
else
  RUN_AS=(--user "$(id -u):$(id -g)")
fi

echo "==> building light-relay:local"
docker build -q -t light-relay:local "$SRC" >/dev/null

if [ ! -f "$STATE/relay.env" ]; then
  printf 'ALLOWED_PEERS=%s\nPUSH_ENDPOINT=\nPUSH_KEY=\n' "$PEERS" > "$STATE/relay.env"
  echo "==> created $STATE/relay.env (ALLOWED_PEERS=$PEERS; not paired yet)"
fi
if [ ! -f "$STATE/inbox.env" ]; then
  # A stand-in until the phone is paired; replace it with the tool's REPLY_TOKEN.
  printf 'REPLY_TOKEN=%s\n' "$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')" > "$STATE/inbox.env"
  echo "==> created $STATE/inbox.env with a temporary REPLY_TOKEN"
fi
chmod 600 "$STATE"/*.env

echo "==> starting containers"
docker rm -f light-relay light-relay-inbox >/dev/null 2>&1 || true
docker run -d --name light-relay-inbox --restart unless-stopped --network host "${RUN_AS[@]}" \
  --env-file "$STATE/inbox.env" -e ROLE=inbox -e HOST=127.0.0.1 -e PORT=18081 \
  -v "$STATE/inbox:/data" -v "$STATE/relay:/outbox:ro" light-relay:local >/dev/null
docker run -d --name light-relay --restart unless-stopped --network host "${RUN_AS[@]}" \
  --env-file "$STATE/relay.env" -e ROLE=relay -e HOST=127.0.0.1 -e PORT=18080 \
  -v "$STATE/relay:/data" -v "$STATE/inbox:/inbox:ro" light-relay:local >/dev/null

sleep 2
echo "==> relay health: $(curl -s http://127.0.0.1:18080/healthz || echo 'NOT RESPONDING')"
docker logs --tail 5 light-relay 2>&1 | sed 's/^/    relay: /'
docker logs --tail 5 light-relay-inbox 2>&1 | sed 's/^/    inbox: /'

if command -v tailscale >/dev/null; then
  NAME=$(tailscale status --json | grep -m1 '"DNSName"' | sed 's/.*: "\(.*\)\.",*/\1/')
  cat <<EOF

To publish, if not already (check with: tailscale serve status):
    tailscale serve  --bg --https=443  http://127.0.0.1:18080   # relay: tailnet only
    tailscale funnel --bg --https=8443 http://127.0.0.1:18081   # inbox: public

    relay  https://$NAME
    inbox  https://$NAME:8443/replies
EOF
fi
