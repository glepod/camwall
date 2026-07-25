#!/usr/bin/env bash
set -euo pipefail

PREFIX="${CAMWALL_PREFIX:-/opt/camwall}"
SERVICE_USER="${CAMWALL_USER:-${SUDO_USER:-$USER}}"
RECORDER_MODE="${CAMWALL_RECORDER_MODE:-system}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo, for example: sudo ./scripts/install.sh" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

if [[ ! -f "$PREFIX/camwall.env" ]]; then
  cp "$PREFIX/.env.example" "$PREFIX/camwall.env"
  chmod 600 "$PREFIX/camwall.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$PREFIX/camwall.env"
fi

if [[ ! -f "$PREFIX/.env" ]]; then
  cp "$PREFIX/camwall.env" "$PREFIX/.env"
  chmod 600 "$PREFIX/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$PREFIX/.env"
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
systemctl enable camwall-backend.service
if [[ "$RECORDER_MODE" != "user" ]]; then
  systemctl enable camwall-recorder.service
fi

echo "Installed CamWall to $PREFIX."
echo "Next:"
echo "  1. Edit $PREFIX/cameras.json, $PREFIX/nodes.json, and $PREFIX/camwall.env"
echo "  2. Run: cd $PREFIX && ./scripts/render-go2rtc.sh"
echo "  3. Start go2rtc: cd $PREFIX && docker compose up -d"
echo "  4. Start services: sudo systemctl restart camwall-backend camwall-recorder"
