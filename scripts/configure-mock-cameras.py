#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/opt/camwall")
    parser.add_argument("--mock-host", required=True)
    parser.add_argument("--master-host", required=True)
    parser.add_argument("--public-host", default="")
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--node-id", default="master")
    parser.add_argument("--node-name", default="Master")
    args = parser.parse_args()

    root = Path(args.root)
    cameras = []
    keys = []
    for index in range(1, args.count + 1):
        key = f"mock{index:02d}"
        keys.append(key)
        cameras.append({
            "key": key,
            "name": f"Mock Camera {index:02d}",
            "ip": args.mock_host,
            "rtsp_main": f"rtsp://{args.mock_host}:554/{key}/stream1",
            "rtsp_sub": f"rtsp://{args.mock_host}:554/{key}/stream2",
            "onvif_port": 2020,
        })

    public_host = args.public_host or args.master_host
    nodes = [{
        "id": args.node_id,
        "name": args.node_name,
        "role": "master" if args.node_id == "master" else "worker",
        "host": args.master_host,
        "go2rtc_proxy": f"/node/{args.node_id}/go2rtc",
        "recording_proxy": f"/node/{args.node_id}/recording",
        "webrtc_candidate": f"{public_host}:8555",
        "go2rtc_ws": f"ws://{public_host}:1984/api/ws?src=",
        "recording_url": f"http://{public_host}:8091",
        "cameras": keys,
    }]

    (root / "cameras.json").write_text(json.dumps(cameras, indent=2) + "\n")
    (root / "nodes.json").write_text(json.dumps(nodes, indent=2) + "\n")


if __name__ == "__main__":
    main()
