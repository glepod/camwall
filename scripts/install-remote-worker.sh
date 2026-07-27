#!/usr/bin/env bash
set -euo pipefail

WORKER_HOST="${CAMWALL_WORKER_HOST:-${1:-}}"
WORKER_USER="${CAMWALL_WORKER_USER:-${2:-ubuntu}}"
WORKER_NODE_ID="${CAMWALL_WORKER_NODE_ID:-worker}"
PREFIX="${CAMWALL_PREFIX:-/opt/camwall}"

if [[ -z "$WORKER_HOST" ]]; then
  echo "Usage: CAMWALL_WORKER_HOST=192.168.1.50 CAMWALL_WORKER_USER=ubuntu $0" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="/tmp/camwall-install"
SSH=(ssh -o StrictHostKeyChecking=accept-new "$WORKER_USER@$WORKER_HOST")
RSYNC=(rsync -az --delete -e "ssh -o StrictHostKeyChecking=accept-new")

if [[ -n "${CAMWALL_WORKER_SSH_PASS:-}" ]]; then
  if ! command -v sshpass >/dev/null 2>&1; then
    echo "CAMWALL_WORKER_SSH_PASS is set, but sshpass is not installed locally." >&2
    exit 1
  fi
  SSH=(sshpass -e ssh -o StrictHostKeyChecking=accept-new "$WORKER_USER@$WORKER_HOST")
  RSYNC=(sshpass -e rsync -az --delete -e "ssh -o StrictHostKeyChecking=accept-new")
  export SSHPASS="$CAMWALL_WORKER_SSH_PASS"
fi

"${SSH[@]}" "rm -rf '$REMOTE' && mkdir -p '$REMOTE'"
"${RSYNC[@]}" \
  --exclude '.git' \
  --exclude '.env' \
  --exclude 'camwall.env' \
  --exclude 'recordings' \
  "$ROOT/" "$WORKER_USER@$WORKER_HOST:$REMOTE/"

"${SSH[@]}" "cd '$REMOTE' && sudo CAMWALL_ROLE=worker CAMWALL_NODE_ID='$WORKER_NODE_ID' CAMWALL_PREFIX='$PREFIX' ./scripts/install.sh"

echo "Installed CamWall worker $WORKER_NODE_ID on $WORKER_HOST."
