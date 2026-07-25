#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE="${1:-${CAMWALL_NODE_ID:-}}"
cd "$ROOT"

if [[ -f "$ROOT/camwall.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/camwall.env"
  set +a
elif [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

if [[ -z "$NODE" ]]; then
  NODE="$(python3 - <<'PY'
import json
from pathlib import Path
nodes = json.loads(Path("nodes.json").read_text())
print(nodes[0]["id"] if nodes else "")
PY
)"
fi

if [[ -z "$NODE" ]]; then
  echo "No node selected. Pass a node id, or set CAMWALL_NODE_ID." >&2
  exit 1
fi

python3 generate_go2rtc.py --node "$NODE" > go2rtc.yaml
echo "Wrote $ROOT/go2rtc.yaml for node $NODE"
