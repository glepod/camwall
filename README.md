# CamWall

CamWall is a lightweight LAN camera wall for ONVIF/RTSP cameras. It serves a browser UI, proxies low-latency WebRTC streams through go2rtc, supports basic ONVIF PTZ, and records camera streams into segmented MP4 files.

The project is designed to run on one Linux host, with optional additional recorder/media worker nodes.

## Features

- Browser camera grid with fullscreen playback.
- go2rtc-backed WebRTC streams.
- ONVIF PTZ controls.
- Optional TP-Link Tapo OSD/talk helper support.
- Per-camera recording controls.
- Recording playback, locking, deletion, download, and "End file" split action.
- Multi-node recorder/media worker support.

## Requirements

- Linux host with systemd.
- Python 3.10+.
- Docker with Compose plugin.
- `ffmpeg` and `ffprobe`.
- Cameras exposing RTSP and ONVIF.
- LAN access from browser to the CamWall host and go2rtc WebRTC port.

On Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y python3 ffmpeg rsync docker.io docker-compose-plugin
```

## Quick Install

```bash
git clone git@github.com:glepod/camwall.git
cd camwall
sudo ./scripts/install.sh
```

Then edit the installed config:

```bash
sudoedit /opt/camwall/camwall.env
sudoedit /opt/camwall/cameras.json
sudoedit /opt/camwall/nodes.json
sudoedit /opt/camwall/recording_config.json
```

Render the go2rtc config and start services:

```bash
cd /opt/camwall
./scripts/render-go2rtc.sh
docker compose up -d
sudo systemctl restart camwall-backend camwall-recorder
```

Open:

```text
http://<camwall-host>:8090/
```

## Configuration

Start from the examples:

- `.env.example` -> `/opt/camwall/camwall.env`
- `cameras.json.example` -> `/opt/camwall/cameras.json`
- `nodes.json.example` -> `/opt/camwall/nodes.json`
- `recording_config.json.example` -> `/opt/camwall/recording_config.json`

### camwall.env

```bash
ONVIF_USER=camwall
ONVIF_PASS=change-me
TAPO_PASS=
CAMWALL_PUBLIC_URL=
```

`ONVIF_USER` and `ONVIF_PASS` are used for camera RTSP recording, go2rtc RTSP sources, and ONVIF PTZ.

`TAPO_PASS` is optional and only needed for Tapo-specific OSD/talk helpers.

`CAMWALL_PUBLIC_URL` is optional. If set, direct non-proxied backend requests redirect to that URL.

### cameras.json

```json
[
  { "key": "front_door", "name": "Front Door", "ip": "192.168.1.101" }
]
```

`key` should be stable and contain only simple identifier characters such as lowercase letters, numbers, and underscores.

### nodes.json

Single-node example:

```json
[
  {
    "id": "master",
    "name": "Master",
    "role": "master",
    "host": "192.168.1.10",
    "go2rtc_proxy": "/node/master/go2rtc",
    "recording_proxy": "/node/master/recording",
    "webrtc_candidate": "192.168.1.10:8555",
    "cameras": ["front_door"]
  }
]
```

For multiple nodes, install CamWall on each node, set `CAMWALL_NODE_ID` for each recorder, and assign cameras to the appropriate node in `nodes.json`.

## Services and Ports

- Backend UI/API: `8090`.
- Recorder API: `8091`.
- go2rtc API: `1984`.
- go2rtc WebRTC: `8555` TCP/UDP.

System services:

```bash
sudo systemctl status camwall-backend
sudo systemctl status camwall-recorder
```

Logs:

```bash
journalctl -u camwall-backend -f
journalctl -u camwall-recorder -f
```

## Rendering go2rtc.yaml

Render for the first node in `nodes.json`:

```bash
./scripts/render-go2rtc.sh
```

Render for a named node:

```bash
./scripts/render-go2rtc.sh master
```

The generated `go2rtc.yaml` is intentionally ignored by Git because it may contain camera credentials when rendered from environment values.

## Recording

Recordings are stored under:

```text
/opt/camwall/recordings/<camera-key>/
```

The UI can start/stop per-camera recording, lock recordings from deletion, delete unlocked files, download files, and finalize the active file with **End file**.

## Reverse Proxy

CamWall can run directly on `http://host:8090/`. For HTTPS, configure your reverse proxy to route:

- `/` -> backend `http://<master>:8090`
- `/node/<node>/go2rtc` -> worker go2rtc `http://<node>:1984`
- `/node/<node>/recording` -> worker recorder `http://<node>:8091`

Set `CAMWALL_PUBLIC_URL=https://your-host.example` if you want direct HTTP requests to redirect to the canonical URL.

## Security Notes

- Do not commit `.env`, `camwall.env`, `go2rtc.yaml`, or generated recording files.
- Keep CamWall on a trusted LAN or behind authentication at your reverse proxy.
- Camera credentials are powerful: use a dedicated ONVIF/RTSP camera account where possible.

## Development Checks

```bash
python3 -m py_compile backend/server.py recorder/recorder.py generate_go2rtc.py tapo-helper/osd.py
node --input-type=module --check < web/app.js
```
