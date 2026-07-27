#!/usr/bin/env bash
set -euo pipefail

PREFIX="${CAMWALL_PREFIX:-/opt/camwall}"
SERVICE_USER="${CAMWALL_USER:-${SUDO_USER:-$USER}}"
RECORDER_MODE="${CAMWALL_RECORDER_MODE:-system}"
ROLE="${CAMWALL_ROLE:-master}"
NODE_ID="${CAMWALL_NODE_ID:-master}"
INSTALL_DEPS="${CAMWALL_INSTALL_DEPS:-1}"
START_SERVICES="${CAMWALL_START_SERVICES:-1}"
HOST_IP="${CAMWALL_HOST:-}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo, for example: sudo ./scripts/install.sh" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "$HOST_IP" ]]; then
  HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
if [[ -z "$HOST_IP" ]]; then
  HOST_IP="127.0.0.1"
fi

if [[ "$INSTALL_DEPS" == "1" ]]; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      ca-certificates curl ffmpeg python3 rsync docker.io
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin \
      || DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-v2 \
      || DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose
    systemctl enable --now docker
  else
    echo "Skipping dependency installation: apt-get not found." >&2
  fi
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$PREFIX"
rsync -a --delete \
  --exclude '.git' \
  --exclude '.env' \
  --exclude 'camwall.env' \
  --exclude 'recorder.env' \
  --exclude 'recordings' \
  "$ROOT/" "$PREFIX/"

for example in cameras.json nodes.json recording_config.json; do
  if [[ ! -f "$PREFIX/$example" && -f "$PREFIX/$example.example" ]]; then
    cp "$PREFIX/$example.example" "$PREFIX/$example"
    chown "$SERVICE_USER:$SERVICE_USER" "$PREFIX/$example"
  fi
done

if [[ "$ROLE" == "master" && -f "$PREFIX/nodes.json" ]]; then
  python3 - "$PREFIX/nodes.json" "$HOST_IP" <<'PY'
import json, sys
path, host = sys.argv[1], sys.argv[2]
data = json.load(open(path))
if data and data[0].get("id") == "master" and data[0].get("host") in {"192.168.1.10", "127.0.0.1", ""}:
    data[0]["host"] = host
    data[0]["webrtc_candidate"] = f"{host}:8555"
    json.dump(data, open(path, "w"), indent=2)
    open(path, "a").write("\n")
PY
fi

if [[ ! -f "$PREFIX/camwall.env" ]]; then
  cp "$PREFIX/.env.example" "$PREFIX/camwall.env"
  chmod 600 "$PREFIX/camwall.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$PREFIX/camwall.env"
fi

if [[ ! -f "$PREFIX/.env" ]]; then
  ln -s camwall.env "$PREFIX/.env" 2>/dev/null || cp "$PREFIX/camwall.env" "$PREFIX/.env"
  chmod 600 "$PREFIX/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$PREFIX/.env"
fi

if ! grep -q '^CAMWALL_NODE_ID=' "$PREFIX/camwall.env"; then
  printf '\nCAMWALL_NODE_ID=%s\n' "$NODE_ID" >> "$PREFIX/camwall.env"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$PREFIX/recordings"
chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"

install -m 0644 "$PREFIX/camwall-backend.service" /etc/systemd/system/camwall-backend.service
sed -i "s/^User=.*/User=$SERVICE_USER/" /etc/systemd/system/camwall-backend.service

if [[ "$RECORDER_MODE" == "user" ]]; then
  user_home="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$user_home/.config/systemd/user"
  install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$PREFIX/camwall-recorder.user.service" "$user_home/.config/systemd/user/camwall-recorder.service"
  sudo -u "$SERVICE_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$SERVICE_USER")" systemctl --user daemon-reload || true
  echo "Installed user recorder service. Enable linger and start it with:"
  echo "  sudo loginctl enable-linger $SERVICE_USER"
  echo "  sudo -u $SERVICE_USER systemctl --user enable --now camwall-recorder.service"
else
  install -m 0644 "$PREFIX/camwall-recorder.service" /etc/systemd/system/camwall-recorder.service
  sed -i "s/^User=.*/User=$SERVICE_USER/" /etc/systemd/system/camwall-recorder.service
fi

systemctl daemon-reload
if [[ "$ROLE" == "master" ]]; then
  systemctl enable camwall-backend.service
fi
if [[ "$RECORDER_MODE" != "user" ]]; then
  systemctl enable camwall-recorder.service
fi

if id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx docker; then
  :
else
  usermod -aG docker "$SERVICE_USER" || true
fi

if [[ "$ROLE" == "master" || "$ROLE" == "worker" ]]; then
  sudo -u "$SERVICE_USER" bash -lc "cd '$PREFIX' && ./scripts/render-go2rtc.sh '$NODE_ID'" || true
  sg docker -c "cd '$PREFIX' && docker compose up -d --force-recreate go2rtc" || \
    sudo -u "$SERVICE_USER" bash -lc "cd '$PREFIX' && docker compose up -d --force-recreate go2rtc" || true
fi

if [[ "$START_SERVICES" == "1" ]]; then
  if [[ "$ROLE" == "master" ]]; then
    systemctl restart camwall-backend.service
  fi
  if [[ "$RECORDER_MODE" != "user" ]]; then
    systemctl restart camwall-recorder.service
  fi
fi

echo "Installed CamWall to $PREFIX."
echo "Role: $ROLE"
echo "Node: $NODE_ID"
echo "Open: http://$HOST_IP:8090/"
echo "Edit camera inventory in the app under System -> Config, or edit $PREFIX/cameras.json and $PREFIX/nodes.json."
