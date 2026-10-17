#!/usr/bin/env bash
# Build the image and (re)start both containers on this host.
#
#   scripts/up.sh [state dir] [allowed peers]
#
# Both containers use the host network and bind 127.0.0.1 only. They are then
# published with `tailscale serve` (relay, tailnet only) and `tailscale funnel`
# (inbox, public). Settings live in <state dir>/{relay,inbox}.env. They are
# created on the first run, which needs the allowed peers (comma-separated
# tailnet IPs), and never overwritten, so re-running only updates the code.
#
# Short-lived sessions: put SESSION_HOST=<this host's LAN IP> in inbox.env. This
# then makes a self-signed certificate for that address in <state dir>/session
# (once), mounts it into the inbox only, and prints the pin for the phone build.

set -euo pipefail

STATE=${1:-$HOME/.local/share/light-relay}
PEERS=${2:-}
SRC=$(cd "$(dirname "$0")/.." && pwd)

if [ ! -f "$STATE/relay.env" ] && [ -z "$PEERS" ]; then
  echo "First run: name the tailnet IPs allowed to call the relay, comma-separated:" >&2
  echo "    $0 $STATE <agent IP>[,<your laptop IP>]    (tailscale ip -4 <device>)" >&2
  exit 1
fi

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
elif [ -n "$PEERS" ]; then
  echo "==> $STATE/relay.env exists, so '$PEERS' is ignored; edit ALLOWED_PEERS there"
fi
if [ ! -f "$STATE/inbox.env" ]; then
  # A stand-in until the phone is paired; replace it with the tool's REPLY_TOKEN.
  printf 'REPLY_TOKEN=%s\n' "$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')" > "$STATE/inbox.env"
  echo "==> created $STATE/inbox.env with a temporary REPLY_TOKEN"
fi
chmod 600 "$STATE"/*.env

SESSION_HOST=$(sed -n 's/^SESSION_HOST=//p' "$STATE/inbox.env")
SESSION_PORT=$(sed -n 's/^SESSION_PORT=//p' "$STATE/inbox.env")
SESSION_MOUNT=()
if [ -n "$SESSION_HOST" ]; then
  mkdir -p "$STATE/session"
  if [ ! -f "$STATE/session/cert.pem" ]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
      -keyout "$STATE/session/key.pem" -out "$STATE/session/cert.pem" \
      -subj "/CN=light-relay sessions" -addext "subjectAltName=IP:$SESSION_HOST" 2>/dev/null
    chmod 600 "$STATE/session/key.pem"
    echo "==> made a certificate for $SESSION_HOST in $STATE/session"
  fi
  [ "$(id -u)" = 0 ] && chown -R 99:100 "$STATE/session"
  SESSION_MOUNT=(-v "$STATE/session:/session:ro")
fi

# Optional: WEBHOOK_URL, WEBHOOK_KEY (and WEBHOOK_AGENT, an agent's tailnet IP
# whose ack decides re-sends) in <state dir>/webhook.env wake an agent on each reply.
WEBHOOK_ENV=()
if [ -f "$STATE/webhook.env" ]; then
  chmod 600 "$STATE/webhook.env"
  WEBHOOK_ENV=(--env-file "$STATE/webhook.env")
fi
# Optional: named agents (see docs/agents.md). <state dir>/agents.env holds AGENTS
# and each agent's AGENT_<NAME>_TOKEN / AGENT_<NAME>_IP, for both containers;
# AGENT_<NAME>_WEBHOOK_URL / _KEY go in webhook.env, which only the inbox reads.
AGENTS_ENV=()
if [ -f "$STATE/agents.env" ]; then
  chmod 600 "$STATE/agents.env"
  AGENTS_ENV=(--env-file "$STATE/agents.env")
fi

echo "==> starting containers"
docker rm -f light-relay light-relay-inbox >/dev/null 2>&1 || true
docker run -d --name light-relay-inbox --restart unless-stopped --network host "${RUN_AS[@]}" \
  --env-file "$STATE/inbox.env" "${AGENTS_ENV[@]}" "${WEBHOOK_ENV[@]}" -e ROLE=inbox -e HOST=127.0.0.1 -e PORT=18081 \
  -v "$STATE/inbox:/data" -v "$STATE/relay:/outbox:ro" "${SESSION_MOUNT[@]}" light-relay:local >/dev/null
docker run -d --name light-relay --restart unless-stopped --network host "${RUN_AS[@]}" \
  --env-file "$STATE/relay.env" "${AGENTS_ENV[@]}" -e ROLE=relay -e HOST=127.0.0.1 -e PORT=18080 \
  -v "$STATE/relay:/data" -v "$STATE/inbox:/inbox:ro" light-relay:local >/dev/null

sleep 2
echo "==> relay health: $(curl -s http://127.0.0.1:18080/healthz || echo 'NOT RESPONDING')"
docker logs --tail 5 light-relay 2>&1 | sed 's/^/    relay: /'
docker logs --tail 5 light-relay-inbox 2>&1 | sed 's/^/    inbox: /'

if ! command -v tailscale >/dev/null; then
  echo "==> no tailscale here: publish 127.0.0.1:18080 (tailnet only) and 127.0.0.1:18081 (public) yourself"
  exit 0
fi

# Both are idempotent, so re-running leaves an existing setup as it was.
echo "==> publishing"
tailscale serve --bg --https=443 http://127.0.0.1:18080 >/dev/null \
  || echo "    tailscale serve failed: run as root, or once: sudo tailscale set --operator=\$USER"
tailscale funnel --bg --https=8443 http://127.0.0.1:18081 >/dev/null \
  || echo "    tailscale funnel failed: is Funnel allowed for this node in the tailnet policy?"

NAME=$(tailscale status --json | grep -m1 '"DNSName"' | sed 's/.*: "\(.*\)\.",*/\1/')
echo "    relay  https://$NAME              (tailnet only: the agent's RELAY_URL)"
echo "    inbox  https://$NAME:8443/replies   (public: the tool's relay.inboxUrl)"
if [ -n "$SESSION_HOST" ]; then
  PIN=$(openssl x509 -in "$STATE/session/cert.pem" -pubkey -noout | openssl pkey -pubin -outform der \
    | openssl dgst -sha256 -binary | base64)
  echo "    sessions https://$SESSION_HOST:${SESSION_PORT:-18443}   (LAN only: the tool's relay.sessionUrl)"
  echo "    relay.sessionPin=$PIN"
fi
