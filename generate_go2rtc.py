#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAMERAS = json.loads((ROOT / "cameras.json").read_text())
NODES = json.loads((ROOT / "nodes.json").read_text())
CAM_BY_KEY = {cam["key"]: cam for cam in CAMERAS}


def onvif_user():
    return os.environ.get("ONVIF_USER", "${ONVIF_USER}")


def onvif_pass():
    return os.environ.get("ONVIF_PASS", "${ONVIF_PASS}")


def stream_block(cam):
    key = cam["key"]
    ip = cam["ip"]
    user = onvif_user()
    passwd = onvif_pass()
    main = cam.get("rtsp_main") or f"rtsp://{user}:{passwd}@{ip}:554/stream1"
    sub = cam.get("rtsp_sub") or cam.get("rtsp_main") or f"rtsp://{user}:{passwd}@{ip}:554/stream2"
    return [
        f"  {key}:",
        f"    - {main}#backchannel=0#timeout=30",
        f"  {key}_sub:",
        f"    - {sub}#media=video#backchannel=0#timeout=30",
        f"  {key}_grid: exec:ffmpeg -hide_banner -fflags nobuffer -rtsp_transport tcp -i {sub} -an -vf scale=640:360 -c:v libx264 -preset ultrafast -tune zerolatency -b:v 250k -maxrate 300k -bufsize 300k -g 15 -profile:v baseline -level 3.1 -f rtsp {{output}}",
        f"  {key}_gridts: exec:ffmpeg -hide_banner -fflags nobuffer -rtsp_transport tcp -i {sub} -an -vf scale=640:360,drawtext=fontfile=/usr/share/fonts/droid/DroidSansMono.ttf:text=%{{localtime}}:x=10:y=h-th-8:fontsize=16:fontcolor=white:box=1:boxcolor=black@0.5 -c:v libx264 -preset ultrafast -tune zerolatency -b:v 250k -maxrate 300k -bufsize 300k -g 15 -profile:v baseline -level 3.1 -f rtsp {{output}}",
        f"  {key}_maints: exec:ffmpeg -hide_banner -fflags nobuffer -rtsp_transport tcp -i {main} -vf drawtext=fontfile=/usr/share/fonts/droid/DroidSansMono.ttf:text=%{{localtime}}:x=10:y=h-th-8:fontsize=32:fontcolor=white:box=1:boxcolor=black@0.5 -c:v libx264 -preset ultrafast -tune zerolatency -b:v 2000k -maxrate 2500k -bufsize 2500k -g 30 -c:a copy -f rtsp {{output}}",
        f"  {key}_talk: tapo://${{TAPO_PASS}}@{ip}?subtype=1",
    ]


def generate(node):
    lines = [
        f"# go2rtc config for CamWall node {node['id']} - generated",
        "api:",
        '  listen: ":1984"',
        '  origin: "*"',
        "",
        "webrtc:",
        '  listen: ":8555"',
        "  candidates:",
        f"    - {node['webrtc_candidate']}",
        "",
        "log:",
        "  level: info",
        "",
        "streams:",
    ]
    for key in node["cameras"]:
        if key not in CAM_BY_KEY:
            raise SystemExit(f"node {node['id']} references unknown camera {key}")
        lines.extend(stream_block(CAM_BY_KEY[key]))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="write go2rtc-<node>.yaml for every node")
    parser.add_argument("--node", help="write one node config to stdout")
    args = parser.parse_args()

    if args.node:
        node = next((n for n in NODES if n["id"] == args.node), None)
        if not node:
            raise SystemExit(f"unknown node {args.node}")
        print(generate(node), end="")
        return

    if not args.all:
        parser.error("use --all or --node NODE")

    for node in NODES:
        path = ROOT / f"go2rtc-{node['id']}.yaml"
        path.write_text(generate(node))
        print(path)


if __name__ == "__main__":
    main()
