#!/usr/bin/env bash
set -euo pipefail

COUNT="${CAMWALL_MOCK_CAMERA_COUNT:-4}"
RTSP_BASE="${CAMWALL_MOCK_RTSP_BASE:-rtsp://127.0.0.1:554}"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

start_stream() {
  local name="$1"
  local size="$2"
  local rate="$3"
  local url="$4"
  ffmpeg -hide_banner -loglevel warning -re \
    -f lavfi -i "testsrc2=size=${size}:rate=${rate}" \
    -f lavfi -i "sine=frequency=900:sample_rate=8000" \
    -vf "drawtext=text='${name}  %{localtime}':x=18:y=18:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.55" \
    -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -g "$rate" \
    -c:a aac -b:a 64k \
    -f rtsp -rtsp_transport tcp "$url" &
  PIDS+=("$!")
}

sleep 2
for i in $(seq 1 "$COUNT"); do
  key="$(printf 'mock%02d' "$i")"
  start_stream "$key main" "1280x720" 15 "${RTSP_BASE}/${key}/stream1"
  start_stream "$key sub" "640x360" 10 "${RTSP_BASE}/${key}/stream2"
done

wait
