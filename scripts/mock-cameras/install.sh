#!/usr/bin/env bash
set -euo pipefail

PREFIX="${CAMWALL_MOCK_PREFIX:-/opt/camwall-mock-cameras}"
COUNT="${CAMWALL_MOCK_CAMERA_COUNT:-4}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo, for example: sudo CAMWALL_MOCK_CAMERA_COUNT=4 $0" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates docker.io ffmpeg python3
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin \
    || DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-v2 \
    || DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose
  systemctl enable --now docker
fi

install -d "$PREFIX"
install -m 0755 "$ROOT/start-streams.sh" "$PREFIX/start-streams.sh"
install -m 0755 "$ROOT/onvif_mock.py" "$PREFIX/onvif_mock.py"

cat >/etc/systemd/system/camwall-mock-mediamtx.service <<'UNIT'
[Unit]
Description=CamWall mock camera RTSP server
After=docker.service network-online.target
Wants=docker.service network-online.target

[Service]
Type=simple
ExecStartPre=-/usr/bin/docker rm -f camwall-mock-mediamtx
ExecStart=/usr/bin/docker run --rm --name camwall-mock-mediamtx --network host -e MTX_RTSPADDRESS=:554 bluenviron/mediamtx:latest
ExecStop=/usr/bin/docker stop camwall-mock-mediamtx
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/camwall-mock-streams.service <<UNIT
[Unit]
Description=CamWall mock camera video streams
After=camwall-mock-mediamtx.service network-online.target
Wants=camwall-mock-mediamtx.service network-online.target

[Service]
Type=simple
Environment=CAMWALL_MOCK_CAMERA_COUNT=$COUNT
WorkingDirectory=$PREFIX
ExecStart=$PREFIX/start-streams.sh
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/camwall-mock-onvif.service <<UNIT
[Unit]
Description=CamWall mock ONVIF PTZ responder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=CAMWALL_MOCK_ONVIF_PORT=2020
ExecStart=/usr/bin/python3 $PREFIX/onvif_mock.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now camwall-mock-mediamtx camwall-mock-streams camwall-mock-onvif

echo "Mock cameras running."
echo "RTSP paths:"
for i in $(seq 1 "$COUNT"); do
  key="$(printf 'mock%02d' "$i")"
  echo "  rtsp://<host>/$key/stream1"
  echo "  rtsp://<host>/$key/stream2"
done
