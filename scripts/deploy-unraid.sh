#!/usr/bin/env bash
# Deploy light-relay to an Unraid box for testing.
#
#   scripts/deploy-unraid.sh root@<host> [allowed peers]
#
# Copies the source over SSH and runs scripts/up.sh there, with its state in
# /mnt/user/appdata/light-relay. See up.sh for what that does.

set -euo pipefail

TARGET=${1:?usage: deploy-unraid.sh root@<host> [allowed peers]}
PEERS=${2:-}
APPDATA=/mnt/user/appdata/light-relay

cd "$(dirname "$0")/.."

# One connection for everything: one password prompt.
SOCK=$(mktemp -u "${TMPDIR:-/tmp}/light-relay-ssh.XXXXXX")
ssh -o ControlMaster=yes -o ControlPath="$SOCK" -o ControlPersist=120 -fN "$TARGET"
trap 'ssh -o ControlPath="$SOCK" -O exit "$TARGET" 2>/dev/null' EXIT
run() { ssh -o ControlPath="$SOCK" "$TARGET" "$@"; }

echo "==> copying source to $TARGET:$APPDATA/src"
tar czf - Dockerfile lightrelay scripts/up.sh | run "rm -rf $APPDATA/src && mkdir -p $APPDATA/src && tar xzf - -C $APPDATA/src"
run "bash $APPDATA/src/scripts/up.sh $APPDATA '$PEERS'"
