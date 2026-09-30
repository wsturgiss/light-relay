#!/usr/bin/env bash
# Deploy light-relay to an Unraid box for testing.
#
#   scripts/deploy-unraid.sh [root@host] [allowed peers]
#
# Copies the source over SSH, builds the image on the box, and (re)starts both
# containers on the host network, bound to 127.0.0.1 only, so nothing is
# reachable until you publish it with `tailscale serve` / `tailscale funnel`.
# Settings live in /mnt/user/appdata/light-relay/{relay,inbox}.env; they are
# created on the first run and never overwritten, so re-running only updates code.

set -euo pipefail

TARGET=${1:-root@baconrepo}
# muse, plus this machine for testing
PEERS=${2:-"100.68.208.23,$(tailscale ip -4 2>/dev/null | head -1)"}
APPDATA=/mnt/user/appdata/light-relay

cd "$(dirname "$0")/.."

# One connection for everything: one password prompt.
SOCK=$(mktemp -u "${TMPDIR:-/tmp}/light-relay-ssh.XXXXXX")
ssh -o ControlMaster=yes -o ControlPath="$SOCK" -o ControlPersist=120 -fN "$TARGET"
trap 'ssh -o ControlPath="$SOCK" -O exit "$TARGET" 2>/dev/null' EXIT
run() { ssh -o ControlPath="$SOCK" "$TARGET" "$@"; }

echo "==> copying source to $TARGET:$APPDATA/src"
tar czf - Dockerfile lightrelay | run "rm -rf $APPDATA/src && mkdir -p $APPDATA/src && tar xzf - -C $APPDATA/src"

run bash -s -- "$APPDATA" "$PEERS" <<'REMOTE'
set -euo pipefail
A=$1 PEERS=$2
mkdir -p "$A/relay" "$A/inbox"
chown 99:100 "$A/relay" "$A/inbox"

echo "==> building light-relay:local"
docker build -q -t light-relay:local "$A/src" >/dev/null

if [ ! -f "$A/relay.env" ]; then
  printf 'ALLOWED_PEERS=%s\nPUSH_ENDPOINT=\nPUSH_KEY=\n' "$PEERS" > "$A/relay.env"
  echo "==> created $A/relay.env (ALLOWED_PEERS=$PEERS; not paired yet)"
fi
if [ ! -f "$A/inbox.env" ]; then
  # A stand-in until the phone is paired; replace it with the tool's REPLY_TOKEN.
  printf 'REPLY_TOKEN=%s\n' "$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')" > "$A/inbox.env"
  echo "==> created $A/inbox.env with a temporary REPLY_TOKEN"
fi
chmod 600 "$A"/*.env

echo "==> starting containers"
docker rm -f light-relay light-relay-inbox >/dev/null 2>&1 || true
docker run -d --name light-relay-inbox --restart unless-stopped --network host \
  --env-file "$A/inbox.env" -e ROLE=inbox -e HOST=127.0.0.1 -e PORT=18081 \
  -v "$A/inbox:/data" light-relay:local >/dev/null
docker run -d --name light-relay --restart unless-stopped --network host \
  --env-file "$A/relay.env" -e ROLE=relay -e HOST=127.0.0.1 -e PORT=18080 \
  -v "$A/relay:/data" -v "$A/inbox:/inbox:ro" light-relay:local >/dev/null

sleep 2
echo "==> relay health: $(curl -s http://127.0.0.1:18080/healthz || echo 'NOT RESPONDING')"
docker logs --tail 5 light-relay 2>&1 | sed 's/^/    relay: /'
docker logs --tail 5 light-relay-inbox 2>&1 | sed 's/^/    inbox: /'

if command -v tailscale >/dev/null; then
  NAME=$(tailscale status --json | grep -m1 '"DNSName"' | sed 's/.*: "\(.*\)\.",*/\1/')
  echo
  echo "==> current tailscale serve config on this box:"
  tailscale serve status 2>&1 | sed 's/^/    /'
  cat <<EOF

To publish (run on this box; ports chosen to leave 443 alone):
    tailscale serve  --bg --https=4443 http://127.0.0.1:18080   # relay: tailnet only
    tailscale funnel --bg --https=8443 http://127.0.0.1:18081   # inbox: public

Then:
    relay  https://$NAME:4443
    inbox  https://$NAME:8443/replies
EOF
else
  echo "==> no tailscale CLI on the host; publish 127.0.0.1:18080 (Serve) and :18081 (Funnel) another way"
fi
REMOTE
