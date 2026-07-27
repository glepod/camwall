#!/usr/bin/env python3
"""
CamWall backend.
 - Serves the static web UI (/opt/camwall/web).
 - GET /api/cameras           -> JSON list of cameras (key, name, ip).
 - GET /api/ptz?cam=KEY&x=..&y=..   -> ONVIF ContinuousMove (pan=x, tilt=y).
 - GET /api/ptz?cam=KEY&stop=1      -> ONVIF Stop.
Video/audio streaming is handled by go2rtc (port 1984); this server only
handles the UI and PTZ control. Stdlib only, no third-party deps.
"""
import json, os, base64, hashlib, secrets, datetime, urllib.request, urllib.error
import socket, time
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import subprocess
BASE = os.path.dirname(os.path.abspath(__file__))
WEB  = os.path.join(os.path.dirname(BASE), "web")
CAMS = os.path.join(os.path.dirname(BASE), "cameras.json")
NODES_FILE = os.path.join(os.path.dirname(BASE), "nodes.json")
CFG_FILE = os.path.join(os.path.dirname(BASE), "cameras_config.json")
REC_CFG_FILE = os.path.join(os.path.dirname(BASE), "recording_config.json")
ENV_FILE = os.path.join(os.path.dirname(BASE), ".env")

def load_env_file(path):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass

load_env_file(ENV_FILE)

ONVIF_USER = os.environ.get("ONVIF_USER", "")
ONVIF_PASS = os.environ.get("ONVIF_PASS", "")
ONVIF_PORT = 2020
PROFILE    = "profile_1"     # Tapo main-stream profile (verified)
TS_MODES   = ("off", "osd", "ffmpeg")
ARCHIVE_TYPES = ("none", "local", "samba", "s3", "ftp")
REC_DEFAULT = {
    "global": {
        "segment_minutes": 15,
        "retention_hours": 24,
        "max_mb": 10240,
        "archive": {"enabled": False, "type": "none", "location": ""},
    },
    "cameras": {},
}

with open(CAMS) as f:
    CAMERAS = json.load(f)          # base: key, name (default), ip
try:
    with open(NODES_FILE) as f:
        NODES = json.load(f)
except Exception:
    NODES = []
CAM_BY_KEY = {}
NODE_BY_ID = {}
CAM_NODE = {}
CONFIG_LOCK = threading.RLock()
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

def rebuild_topology():
    global CAM_BY_KEY, NODE_BY_ID, CAM_NODE
    CAM_BY_KEY = {c["key"]: c for c in CAMERAS}
    NODE_BY_ID = {n["id"]: n for n in NODES}
    CAM_NODE = {}
    for node in NODES:
        for key in node.get("cameras", []):
            CAM_NODE[key] = node["id"]

rebuild_topology()

def _tapo_pass():
    try:
        for line in open(ENV_FILE):
            if line.startswith("TAPO_PASS="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get("TAPO_PASS", "")

def _public_url():
    return os.environ.get("CAMWALL_PUBLIC_URL", "").rstrip("/")

def use_proxy_routes():
    return os.environ.get("CAMWALL_USE_PROXY_ROUTES", "0") == "1"

def node_go2rtc_ws_base(node):
    if node.get("go2rtc_ws"):
        return node["go2rtc_ws"]
    if use_proxy_routes():
        return node.get("go2rtc_proxy", f"/node/{node.get('id')}/go2rtc") + "/api/ws?src="
    return f"ws://{node.get('host')}:1984/api/ws?src="

def node_recording_file_base(node):
    if node.get("recording_url"):
        return node["recording_url"].rstrip("/")
    if use_proxy_routes():
        return node.get("recording_proxy", f"/node/{node.get('id')}/recording")
    return f"http://{node.get('host')}:8091"

# Per-camera overrides: {key: {"name": str, "ts": "off|osd|ffmpeg"}}
def load_cfg():
    try:
        with open(CFG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_cfg(cfg):
    tmp = CFG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, CFG_FILE)

CONFIG = load_cfg()

def write_json_atomic(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)

def normalize_camera(raw):
    if not isinstance(raw, dict):
        raise ValueError("camera must be an object")
    key = str(raw.get("key", "")).strip().lower().replace("-", "_")
    if not KEY_RE.match(key):
        raise ValueError("camera key must start with a letter and contain lowercase letters, numbers, and underscores")
    name = str(raw.get("name", "")).strip()[:80]
    ip = str(raw.get("ip", "")).strip()
    if not name:
        raise ValueError(f"camera {key} requires a name")
    if not ip:
        raise ValueError(f"camera {key} requires an ip or hostname")
    out = {"key": key, "name": name, "ip": ip}
    for field in ("rtsp_main", "rtsp_sub"):
        value = str(raw.get(field, "")).strip()
        if value:
            if not value.startswith("rtsp://"):
                raise ValueError(f"{field} for {key} must start with rtsp://")
            out[field] = value
    for field in ("rtsp_port", "onvif_port"):
        if raw.get(field) not in (None, ""):
            out[field] = max(1, min(65535, int(raw.get(field))))
    return out

def normalize_node(raw, valid_camera_keys):
    if not isinstance(raw, dict):
        raise ValueError("node must be an object")
    node_id = str(raw.get("id", "")).strip().lower().replace("-", "_")
    if not KEY_RE.match(node_id):
        raise ValueError("node id must start with a letter and contain lowercase letters, numbers, and underscores")
    host = str(raw.get("host", "")).strip()
    if not host:
        raise ValueError(f"node {node_id} requires a host")
    cameras = []
    for key in raw.get("cameras") or []:
        key = str(key).strip()
        if key not in valid_camera_keys:
            raise ValueError(f"node {node_id} references unknown camera {key}")
        if key not in cameras:
            cameras.append(key)
    out = {
        "id": node_id,
        "name": str(raw.get("name") or node_id).strip()[:80],
        "role": str(raw.get("role") or ("master" if node_id == "master" else "worker")).strip()[:24],
        "host": host,
        "go2rtc_proxy": str(raw.get("go2rtc_proxy") or f"/node/{node_id}/go2rtc").strip(),
        "recording_proxy": str(raw.get("recording_proxy") or f"/node/{node_id}/recording").strip(),
        "webrtc_candidate": str(raw.get("webrtc_candidate") or f"{host}:8555").strip(),
        "cameras": cameras,
    }
    for field in ("go2rtc_ws", "recording_url"):
        value = str(raw.get(field, "")).strip()
        if value:
            out[field] = value
    return out

def config_payload():
    return {
        "generated_at": now_iso(),
        "cameras": CAMERAS,
        "nodes": NODES,
        "camera_overrides": CONFIG,
    }

def save_topology(cameras, nodes):
    global CAMERAS, NODES, CONFIG
    normalized_cameras = [normalize_camera(c) for c in cameras]
    keys = [c["key"] for c in normalized_cameras]
    if len(keys) != len(set(keys)):
        raise ValueError("camera keys must be unique")
    valid_keys = set(keys)
    normalized_nodes = [normalize_node(n, valid_keys) for n in nodes]
    if not normalized_nodes:
        host = socket.gethostbyname(socket.gethostname())
        normalized_nodes = [normalize_node({
            "id": "master",
            "name": "Master",
            "role": "master",
            "host": host,
            "cameras": keys,
        }, valid_keys)]
    node_ids = [n["id"] for n in normalized_nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("node ids must be unique")
    assigned = {key for node in normalized_nodes for key in node.get("cameras", [])}
    missing = [key for key in keys if key not in assigned]
    if missing:
        normalized_nodes[0]["cameras"].extend(missing)

    with CONFIG_LOCK:
        write_json_atomic(CAMS, normalized_cameras)
        write_json_atomic(NODES_FILE, normalized_nodes)
        CONFIG = {key: value for key, value in CONFIG.items() if key in valid_keys}
        save_cfg(CONFIG)
        CAMERAS = normalized_cameras
        NODES = normalized_nodes
        rebuild_topology()
        refresh_recordings_cache_soon()
    return config_payload()

def apply_runtime_config():
    out = {"ok": True, "steps": []}
    try:
        rendered = subprocess.run(
            [os.path.join(os.path.dirname(BASE), "scripts", "render-go2rtc.sh")],
            cwd=os.path.dirname(BASE), capture_output=True, text=True, timeout=30)
        out["steps"].append({
            "name": "render-go2rtc",
            "ok": rendered.returncode == 0,
            "stdout": rendered.stdout[-400:],
            "stderr": rendered.stderr[-400:],
        })
        if rendered.returncode != 0:
            out["ok"] = False
            return out
    except Exception as e:
        out["ok"] = False
        out["steps"].append({"name": "render-go2rtc", "ok": False, "error": str(e)})
        return out

    try:
        restarted = subprocess.run(
            ["docker", "compose", "up", "-d", "--force-recreate", "go2rtc"],
            cwd=os.path.dirname(BASE), capture_output=True, text=True, timeout=60)
        out["steps"].append({
            "name": "restart-go2rtc",
            "ok": restarted.returncode == 0,
            "stdout": restarted.stdout[-400:],
            "stderr": restarted.stderr[-400:],
        })
        if restarted.returncode != 0:
            out["ok"] = False
    except Exception as e:
        out["ok"] = False
        out["steps"].append({"name": "restart-go2rtc", "ok": False, "error": str(e)})
    out["reload_recorders"] = reload_recorders(load_recording_config())
    return out

def load_recording_config():
    try:
        with open(REC_CFG_FILE) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault("global", {})
    cfg.setdefault("cameras", {})
    merged_global = dict(REC_DEFAULT["global"])
    merged_global.update(cfg["global"])
    archive = dict(REC_DEFAULT["global"]["archive"])
    archive.update(merged_global.get("archive") or {})
    if archive.get("type") not in ARCHIVE_TYPES:
        archive["type"] = "none"
    merged_global["archive"] = archive
    cfg["global"] = merged_global
    return cfg

def save_recording_config(cfg):
    tmp = REC_CFG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, REC_CFG_FILE)

def merged_recording_camera_config(cfg, key):
    out = dict(cfg["global"])
    out["archive"] = dict(cfg["global"].get("archive") or {})
    cam = cfg.get("cameras", {}).get(key, {})
    for field in ("enabled", "segment_minutes", "retention_hours", "max_mb"):
        if field in cam:
            out[field] = cam[field]
    if "archive" in cam:
        out["archive"].update(cam.get("archive") or {})
    out["enabled"] = bool(out.get("enabled", False))
    out["segment_minutes"] = max(1, int(out.get("segment_minutes") or 15))
    out["retention_hours"] = max(0, int(out.get("retention_hours") or 0))
    out["max_mb"] = max(0, int(out.get("max_mb") or 0))
    if out["archive"].get("type") not in ARCHIVE_TYPES:
        out["archive"]["type"] = "none"
    out["archive"]["enabled"] = bool(out["archive"].get("enabled", False))
    out["archive"]["location"] = out["archive"].get("location") or ""
    return out

def recording_config_payload():
    cfg = load_recording_config()
    return {
        "global": cfg["global"],
        "cameras": [{
            "key": cam["key"],
            "name": CONFIG.get(cam["key"], {}).get("name", cam["name"]),
            "node": CAM_NODE.get(cam["key"]),
            "node_name": NODE_BY_ID.get(CAM_NODE.get(cam["key"]), {}).get("name"),
            "config": merged_recording_camera_config(cfg, cam["key"]),
            "override": cfg.get("cameras", {}).get(cam["key"], {}),
        } for cam in CAMERAS],
    }

def recorder_url(node, path):
    return f"http://{node.get('host')}:8091{path}"

def reload_recorders(cfg=None):
    out = []
    for node in NODES:
        pushed = post_json(recorder_url(node, "/api/config"), cfg, timeout=3) if cfg is not None else None
        out.append({
            "node": node.get("id"),
            "config": {k: pushed.get(k) for k in ("ok", "status", "latency_ms", "error", "message")} if pushed else None,
            **{k: v for k, v in fetch_json(recorder_url(node, "/api/reload"), timeout=2.5).items() if k != "data"},
        })
    return out

def recording_status_payload():
    cfg = load_recording_config()
    node_status = []
    cameras = []
    by_key = {}
    for node in NODES:
        status = fetch_json(recorder_url(node, "/api/status"), timeout=2.5)
        item = {
            "id": node.get("id"),
            "name": node.get("name"),
            "host": node.get("host"),
            "role": node.get("role"),
            "proxy": node.get("recording_proxy", f"/node/{node.get('id')}/recording"),
            "api": {k: status.get(k) for k in ("ok", "status", "latency_ms", "error", "message")},
            "disk": (status.get("data") or {}).get("disk"),
            "root": (status.get("data") or {}).get("root"),
        }
        node_status.append(item)
        for cam in (status.get("data") or {}).get("cameras", []):
            by_key[cam.get("key")] = {**cam, "node": node.get("id"), "node_name": node.get("name")}
    for cam in CAMERAS:
        key = cam["key"]
        rec = by_key.get(key, {})
        cameras.append({
            "key": key,
            "name": CONFIG.get(key, {}).get("name", cam["name"]),
            "ip": cam["ip"],
            "node": CAM_NODE.get(key),
            "node_name": NODE_BY_ID.get(CAM_NODE.get(key), {}).get("name"),
            "config": merged_recording_camera_config(cfg, key),
            "recording": bool(rec.get("recording")),
            "pid": rec.get("pid"),
            "stats": rec.get("stats", {"files": 0, "bytes": 0, "latest": None}),
        })
    return {
        "generated_at": now_iso(),
        "global": cfg["global"],
        "nodes": node_status,
        "cameras": cameras,
        "totals": {
            "recording": len([c for c in cameras if c["recording"]]),
            "enabled": len([c for c in cameras if c["config"].get("enabled")]),
            "files": sum(int((c.get("stats") or {}).get("files") or 0) for c in cameras),
            "bytes": sum(int((c.get("stats") or {}).get("bytes") or 0) for c in cameras),
        },
    }

def recordings_payload(key=None):
    cache = refresh_recordings_cache()
    out = cache.get("recordings", [])
    if key:
        out = [item for item in out if item.get("camera") == key]
    return {
        "ok": True,
        "generated_at": cache.get("generated_at"),
        "refreshing": cache.get("refreshing", False),
        "stale": cache.get("stale", False),
        "nodes": cache.get("nodes", []),
        "recordings": out,
    }

REC_CACHE_TTL = 15
REC_CACHE = {"recordings": [], "nodes": [], "generated_at": None, "last_refresh": 0, "refreshing": False, "stale": True}
_rec_cache_lock = threading.Lock()

def _read_recordings_node(node):
    resp = fetch_json(recorder_url(node, "/api/recordings"), timeout=5)
    item = {
        "id": node.get("id"),
        "name": node.get("name"),
        "ok": resp.get("ok", False),
        "latency_ms": resp.get("latency_ms"),
        "error": resp.get("error"),
        "message": resp.get("message"),
    }
    out = []
    if not resp.get("ok"):
        return item, out
    proxy = node_recording_file_base(node)
    for rec in (resp.get("data") or {}).get("recordings", []):
        cam = CAM_BY_KEY.get(rec.get("camera"), {})
        out.append({
            **rec,
            "node": node.get("id"),
            "node_name": node.get("name"),
            "camera_name": CONFIG.get(rec.get("camera"), {}).get("name", cam.get("name", rec.get("camera"))),
            "url": proxy + "/api/file?path=" + quote(rec.get("path", "")),
        })
    item["files"] = len(out)
    return item, out

def refresh_recordings_cache(force=False):
    now = time.time()
    with _rec_cache_lock:
        if not force and REC_CACHE["recordings"] and now - REC_CACHE["last_refresh"] < REC_CACHE_TTL:
            return dict(REC_CACHE)
        if REC_CACHE["refreshing"] and REC_CACHE["recordings"]:
            return dict(REC_CACHE)
        REC_CACHE["refreshing"] = True

    nodes, recordings = [], []
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(NODES))) as pool:
            for future in as_completed([pool.submit(_read_recordings_node, node) for node in NODES]):
                node_item, node_recordings = future.result()
                nodes.append(node_item)
                recordings.extend(node_recordings)
        recordings.sort(key=lambda item: item.get("mtime", 0), reverse=True)
        with _rec_cache_lock:
            REC_CACHE.update({
                "recordings": recordings,
                "nodes": nodes,
                "generated_at": now_iso(),
                "last_refresh": time.time(),
                "refreshing": False,
                "stale": False,
            })
            return dict(REC_CACHE)
    except Exception as e:
        with _rec_cache_lock:
            REC_CACHE.update({"refreshing": False, "stale": True, "error": str(e)[:160]})
            return dict(REC_CACHE)

def refresh_recordings_cache_soon():
    threading.Thread(target=lambda: refresh_recordings_cache(True), daemon=True).start()

def recording_cache_loop():
    while True:
        refresh_recordings_cache(True)
        time.sleep(REC_CACHE_TTL)

def recording_node_action(node_id, path, action, extra=None):
    node = NODE_BY_ID.get(node_id)
    if not node:
        return {"ok": False, "error": "unknown node"}
    params = {"path": path}
    params.update(extra or {})
    query = "&".join(f"{quote(str(k))}={quote(str(v), safe='')}" for k, v in params.items())
    resp = fetch_json(recorder_url(node, f"/api/{action}?{query}"), timeout=5)
    data = resp.get("data") if resp.get("ok") else None
    if isinstance(data, dict):
        ok = bool(data.get("ok"))
        out = {**data, "node": node_id}
    else:
        ok = False
        out = {"ok": False, "node": node_id, "error": resp.get("message") or resp.get("error") or "request failed"}
    if ok:
        with _rec_cache_lock:
            if action == "delete":
                REC_CACHE["recordings"] = [
                    item for item in REC_CACHE["recordings"]
                    if not (item.get("node") == node_id and item.get("path") == path)
                ]
            elif action == "lock":
                locked = (extra or {}).get("locked") == "1"
                for item in REC_CACHE["recordings"]:
                    if item.get("node") == node_id and item.get("path") == path:
                        item["locked"] = locked
        refresh_recordings_cache_soon()
    return out

def recording_node_split(node_id, cam):
    node = NODE_BY_ID.get(node_id)
    if not node:
        return {"ok": False, "error": "unknown node"}
    if cam not in CAM_BY_KEY:
        return {"ok": False, "error": "unknown camera"}
    resp = fetch_json(recorder_url(node, f"/api/split?cam={quote(cam, safe='')}"), timeout=10)
    data = resp.get("data") if resp.get("ok") else None
    if isinstance(data, dict):
        out = {**data, "node": node_id}
    else:
        out = {"ok": False, "node": node_id, "error": resp.get("message") or resp.get("error") or "request failed"}
    if out.get("ok"):
        refresh_recordings_cache(True)
    return out

def merged_cameras():
    out = []
    for c in CAMERAS:
        o = CONFIG.get(c["key"], {})
        node_id = CAM_NODE.get(c["key"])
        node = NODE_BY_ID.get(node_id, {})
        out.append({"key": c["key"], "ip": c["ip"],
                    "name": o.get("name", c["name"]),
                    "ts": o.get("ts", "off"),
                    "node": node_id,
                    "node_name": node.get("name", node_id),
                    "ws_base": node_go2rtc_ws_base(node)})
    return out

def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def tcp_check(host, port, timeout=1.2):
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "latency_ms": round((time.time() - started) * 1000)}
    except Exception as e:
        return {"ok": False, "error": e.__class__.__name__}

def fetch_json(url, timeout=1.5):
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return {
                "ok": True,
                "status": r.status,
                "latency_ms": round((time.time() - started) * 1000),
                "data": json.loads(r.read().decode()),
            }
    except Exception as e:
        return {
            "ok": False,
            "latency_ms": round((time.time() - started) * 1000),
            "error": e.__class__.__name__,
            "message": str(e)[:160],
        }

def post_json(url, payload, timeout=2.5):
    started = time.time()
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {
                "ok": True,
                "status": r.status,
                "latency_ms": round((time.time() - started) * 1000),
                "data": json.loads(r.read().decode()),
            }
    except Exception as e:
        return {
            "ok": False,
            "latency_ms": round((time.time() - started) * 1000),
            "error": e.__class__.__name__,
            "message": str(e)[:160],
        }

def stream_summary(streams):
    out = {}
    totals = {"streams": 0, "active_streams": 0, "producers": 0, "consumers": 0,
              "bytes_recv": 0, "bytes_send": 0}
    if not isinstance(streams, dict):
        return out, totals
    for name, info in streams.items():
        producers = info.get("producers") or []
        consumers = info.get("consumers") or []
        recv = sum(int(p.get("bytes_recv") or 0) for p in producers)
        sent = 0
        for consumer in consumers:
            for sender in consumer.get("senders") or []:
                sent += int(sender.get("bytes") or 0)
        active = bool(consumers)
        out[name] = {
            "producers": len(producers),
            "consumers": len(consumers),
            "bytes_recv": recv,
            "bytes_send": sent,
            "active": active,
        }
        totals["streams"] += 1
        totals["active_streams"] += 1 if active else 0
        totals["producers"] += len(producers)
        totals["consumers"] += len(consumers)
        totals["bytes_recv"] += recv
        totals["bytes_send"] += sent
    return out, totals

def build_system_status():
    cameras = merged_cameras()
    disabled = sorted(DISABLED)
    node_status = []
    node_streams = {}

    for node in NODES:
        host = node.get("host")
        api_url = f"http://{host}:1984/api/streams"
        api = fetch_json(api_url)
        streams, totals = stream_summary(api.get("data"))
        node_streams[node["id"]] = streams
        ports = {
            "go2rtc_api_1984": tcp_check(host, 1984),
            "go2rtc_webrtc_8555": tcp_check(host, 8555),
            "go2rtc_rtsp_8554": tcp_check(host, 8554),
            "recorder_8091": tcp_check(host, 8091),
        }
        recorder = fetch_json(f"http://{host}:8091/api/status", timeout=1.5)
        node_status.append({
            "id": node.get("id"),
            "name": node.get("name"),
            "role": node.get("role"),
            "host": host,
            "go2rtc_proxy": node.get("go2rtc_proxy"),
            "recording_proxy": node.get("recording_proxy"),
            "webrtc_candidate": node.get("webrtc_candidate"),
            "assigned_cameras": node.get("cameras", []),
            "api": {k: api.get(k) for k in ("ok", "status", "latency_ms", "error", "message")},
            "recorder": {
                **{k: recorder.get(k) for k in ("ok", "status", "latency_ms", "error", "message")},
                "disk": (recorder.get("data") or {}).get("disk"),
                "root": (recorder.get("data") or {}).get("root"),
            },
            "ports": ports,
            "streams": totals,
        })

    camera_checks = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        future_map = {}
        for cam in cameras:
            rtsp_port = int(CAM_BY_KEY.get(cam["key"], {}).get("rtsp_port") or 554)
            onvif_port = int(CAM_BY_KEY.get(cam["key"], {}).get("onvif_port") or ONVIF_PORT)
            for label, port in (("rtsp_554", rtsp_port), ("onvif_2020", onvif_port)):
                future_map[pool.submit(tcp_check, cam["ip"], port)] = (cam["key"], label)
        for future in as_completed(future_map):
            key, label = future_map[future]
            camera_checks.setdefault(key, {})[label] = future.result()

    camera_status = []
    for cam in cameras:
        key = cam["key"]
        node_id = cam.get("node")
        streams = node_streams.get(node_id, {})
        related = {name: streams.get(name) for name in (
            key, key + "_grid", key + "_gridts", key + "_maints", key + "_talk"
        ) if name in streams}
        camera_status.append({
            "key": key,
            "name": cam["name"],
            "ip": cam["ip"],
            "node": node_id,
            "node_name": cam.get("node_name"),
            "timestamp_mode": cam.get("ts", "off"),
            "power": "off" if key in DISABLED else "on",
            "ws_base": cam.get("ws_base"),
            "checks": camera_checks.get(key, {}),
            "streams": related,
        })

    totals = {
        "cameras": len(cameras),
        "nodes": len(NODES),
        "powered_on": len([c for c in cameras if c["key"] not in DISABLED]),
        "powered_off": len(disabled),
        "active_streams": sum(n["streams"]["active_streams"] for n in node_status),
        "producers": sum(n["streams"]["producers"] for n in node_status),
        "consumers": sum(n["streams"]["consumers"] for n in node_status),
    }
    return {
        "generated_at": now_iso(),
        "master": {
            "host": socket.gethostname(),
            "port": int(os.environ.get("CAMWALL_PORT", "8090")),
            "web_root": WEB,
            "config_files": {
                "cameras": CAMS,
                "nodes": NODES_FILE,
                "camera_overrides": CFG_FILE,
                "power_state": POWER_FILE,
                "recording": REC_CFG_FILE,
            },
        },
        "totals": totals,
        "routes": {
            "ui": (_public_url() + "/") if _public_url() else "/",
            "legacy_go2rtc": "/go2rtc",
            "node_go2rtc_pattern": "/node/<node-id>/go2rtc",
            "node_recording_pattern": "/node/<node-id>/recording",
        },
        "nodes": node_status,
        "cameras": camera_status,
        "power": {"disabled": disabled},
    }

def set_camera_osd(ip, enable):
    """Toggle the camera's built-in date/time OSD via the Tapo helper container."""
    pw = _tapo_pass()
    if not pw:
        raise RuntimeError("TAPO_PASS unavailable")
    r = subprocess.run(
        ["docker", "run", "--rm", "--network", "host", "-e", "TAPO_PASS=" + pw,
         "camwall-tapo", ip, "1" if enable else "0"],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200])

MIME = {".html":"text/html", ".js":"text/javascript", ".css":"text/css",
        ".json":"application/json", ".png":"image/png", ".jpg":"image/jpeg",
        ".svg":"image/svg+xml", ".ico":"image/x-icon", ".woff2":"font/woff2"}

# ---- Global power state (server-side, shared by all viewers) ----------------
# A disabled camera gets no players from any client, so go2rtc (on-demand) stops
# its transcode and RTSP pull — the camera is no longer processed or requested.
POWER_FILE = os.path.join(os.path.dirname(BASE), "power.json")
_power_lock = threading.Lock()

def load_disabled():
    try:
        with open(POWER_FILE) as f:
            return set(json.load(f).get("disabled", []))
    except Exception:
        return set()

def save_disabled(s):
    tmp = POWER_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"disabled": sorted(s)}, f)
    os.replace(tmp, POWER_FILE)

DISABLED = load_disabled()


def wsse():
    if not ONVIF_USER or not ONVIF_PASS:
        raise RuntimeError("ONVIF_USER and ONVIF_PASS must be set")
    nonce = secrets.token_bytes(16)
    created = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + ONVIF_PASS.encode()).digest()).decode()
    n64 = base64.b64encode(nonce).decode()
    return (f'<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            f'<UsernameToken><Username>{ONVIF_USER}</Username>'
            f'<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{n64}</Nonce>'
            f'<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
            f'</UsernameToken></Security>')


def onvif_call(ip, body, port=ONVIF_PORT, timeout=5):
    env = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           f'<s:Header>{wsse()}</s:Header>'
           f'<s:Body xmlns:tt="http://www.onvif.org/ver10/schema">{body}</s:Body></s:Envelope>')
    req = urllib.request.Request(f"http://{ip}:{port}/onvif/ptz",
                                 data=env.encode(),
                                 headers={"Content-Type": "application/soap+xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def ptz_move(cam, x, y):
    # Continuous move, with a Timeout safety net so the camera auto-stops
    # even if the Stop command is lost or arrives out of order.
    x = max(-1.0, min(1.0, x)); y = max(-1.0, min(1.0, y))
    body = (f'<ContinuousMove xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
            f'<ProfileToken>{PROFILE}</ProfileToken>'
            f'<Velocity><PanTilt x="{x}" y="{y}" xmlns="http://www.onvif.org/ver10/schema"/></Velocity>'
            f'<Timeout>PT1S</Timeout>'
            f'</ContinuousMove>')
    return onvif_call(cam["ip"], body, int(cam.get("onvif_port") or ONVIF_PORT))


def ptz_relative(cam, dx, dy):
    # Relative move — a fixed, self-terminating increment. No Stop needed, so
    # there is no move/stop race and the camera can never "run to the limit".
    dx = max(-1.0, min(1.0, dx)); dy = max(-1.0, min(1.0, dy))
    body = (f'<RelativeMove xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
            f'<ProfileToken>{PROFILE}</ProfileToken>'
            f'<Translation><PanTilt x="{dx}" y="{dy}" xmlns="http://www.onvif.org/ver10/schema"/></Translation>'
            f'</RelativeMove>')
    return onvif_call(cam["ip"], body, int(cam.get("onvif_port") or ONVIF_PORT))


def ptz_stop(cam):
    body = (f'<Stop xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
            f'<ProfileToken>{PROFILE}</ProfileToken><PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>')
    return onvif_call(cam["ip"], body, int(cam.get("onvif_port") or ONVIF_PORT))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body=b"", ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length > 1024 * 1024:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode() or "{}")

    def do_GET(self):
        # Redirect direct plain-HTTP access (e.g. http://server1.lan:8090) to the
        # trusted HTTPS endpoint. Traefik-proxied requests carry X-Forwarded-Proto
        # and are served normally, so this never loops.
        public_url = _public_url()
        if public_url and self.headers.get("X-Forwarded-Proto") is None:
            self.send_response(302)
            self.send_header("Location", public_url + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        u = urlparse(self.path)
        path = u.path
        if path == "/api/cameras":
            return self._send(200, json.dumps(merged_cameras()), "application/json")
        if path == "/api/admin/config":
            return self._send(200, json.dumps(config_payload()), "application/json")
        if path == "/api/system":
            return self._send(200, json.dumps(build_system_status()), "application/json")
        if path == "/api/recording/config":
            return self._send(200, json.dumps(recording_config_payload()), "application/json")
        if path == "/api/recording/status":
            return self._send(200, json.dumps(recording_status_payload()), "application/json")
        if path == "/api/recordings":
            q = parse_qs(u.query)
            key = (q.get("cam") or [""])[0]
            if key and key not in CAM_BY_KEY:
                return self._send(404, json.dumps({"error": "unknown camera"}))
            if (q.get("refresh") or ["0"])[0] == "1":
                refresh_recordings_cache(True)
            return self._send(200, json.dumps(recordings_payload(key or None)), "application/json")
        if path == "/api/recordings/delete":
            q = parse_qs(u.query)
            node = (q.get("node") or [""])[0]
            rec_path = (q.get("path") or [""])[0]
            return self._send(200, json.dumps(recording_node_action(node, rec_path, "delete")), "application/json")
        if path == "/api/recordings/lock":
            q = parse_qs(u.query)
            node = (q.get("node") or [""])[0]
            rec_path = (q.get("path") or [""])[0]
            locked = (q.get("locked") or ["1"])[0]
            return self._send(200, json.dumps(recording_node_action(node, rec_path, "lock", {"locked": locked})), "application/json")
        if path == "/api/recordings/split":
            q = parse_qs(u.query)
            node = (q.get("node") or [""])[0]
            cam = (q.get("cam") or [""])[0]
            return self._send(200, json.dumps(recording_node_split(node, cam)), "application/json")
        if path == "/api/recording/reload":
            return self._send(200, json.dumps({"ok": True, "nodes": reload_recorders(load_recording_config())}), "application/json")
        if path == "/api/recording/set":
            q = parse_qs(u.query)
            key = (q.get("cam") or [""])[0]
            apply_all = (q.get("all") or q.get("copy_to_all") or ["0"])[0] == "1"
            scope_global = (q.get("scope") or [""])[0] == "global"
            if key and key not in CAM_BY_KEY:
                return self._send(404, json.dumps({"error": "unknown camera"}))
            if not key and not apply_all and not scope_global:
                return self._send(400, json.dumps({"error": "camera, all=1, or scope=global required"}))

            with _power_lock:
                cfg = load_recording_config()
                targets = ["global"] if scope_global else ([c["key"] for c in CAMERAS] if apply_all else [key])
                for target in targets:
                    entry = cfg["global"] if target == "global" else cfg.setdefault("cameras", {}).setdefault(target, {})
                    archive = dict(entry.get("archive") or {})
                    if "enabled" in q:
                        entry["enabled"] = (q["enabled"][0] == "1")
                    if "segment_minutes" in q:
                        entry["segment_minutes"] = max(1, int(float(q["segment_minutes"][0])))
                    if "retention_hours" in q:
                        entry["retention_hours"] = max(0, int(float(q["retention_hours"][0])))
                    if "max_mb" in q:
                        entry["max_mb"] = max(0, int(float(q["max_mb"][0])))
                    if "archive_enabled" in q:
                        archive["enabled"] = (q["archive_enabled"][0] == "1")
                    if "archive_type" in q:
                        atype = q["archive_type"][0]
                        if atype not in ARCHIVE_TYPES:
                            return self._send(400, json.dumps({"error": "bad archive_type"}))
                        archive["type"] = atype
                    if "archive_location" in q:
                        archive["location"] = q["archive_location"][0].strip()
                    if any(k in q for k in ("archive_enabled", "archive_type", "archive_location")):
                        entry["archive"] = archive
                save_recording_config(cfg)
            reloads = reload_recorders(cfg)
            return self._send(200, json.dumps({"ok": True, "config": recording_config_payload(), "reload": reloads}))
        if path == "/api/config":
            q = parse_qs(u.query)
            key = (q.get("cam") or [""])[0]
            if key not in CAM_BY_KEY:
                return self._send(404, json.dumps({"error": "unknown camera"}))
            entry = dict(CONFIG.get(key, {}))
            old_ts = entry.get("ts", "off")
            if "name" in q:
                nm = q["name"][0].strip()[:40]
                if nm:
                    entry["name"] = nm
            osd_err = None
            if "ts" in q:
                ts = q["ts"][0]
                if ts not in TS_MODES:
                    return self._send(400, json.dumps({"error": "bad ts mode"}))
                entry["ts"] = ts
                # Only touch the camera OSD when its on/off state actually changes
                # (the helper call is slow) — so a plain rename stays instant.
                if (ts == "osd") != (old_ts == "osd"):
                    try:
                        set_camera_osd(CAM_BY_KEY[key]["ip"], ts == "osd")
                    except Exception as e:
                        osd_err = str(e)
            with _power_lock:
                CONFIG[key] = entry
                save_cfg(CONFIG)
            resp = {"ok": osd_err is None, "cameras": merged_cameras()}
            if osd_err:
                resp["osd_error"] = osd_err
            return self._send(200, json.dumps(resp))
        if path == "/api/state":
            return self._send(200, json.dumps({"disabled": sorted(DISABLED)}))
        if path == "/api/power":
            q = parse_qs(u.query)
            key = (q.get("cam") or [""])[0]
            if key not in CAM_BY_KEY:
                return self._send(404, json.dumps({"error": "unknown camera"}))
            on = (q.get("on") or ["1"])[0] == "1"
            with _power_lock:
                if on:
                    DISABLED.discard(key)
                else:
                    DISABLED.add(key)
                save_disabled(DISABLED)
            return self._send(200, json.dumps({"ok": True, "disabled": sorted(DISABLED)}))
        if path == "/api/ptz":
            q = parse_qs(u.query)
            key = (q.get("cam") or [""])[0]
            cam = CAM_BY_KEY.get(key)
            if not cam:
                return self._send(404, json.dumps({"error": "unknown camera"}))
            try:
                if (q.get("stop") or ["0"])[0] == "1":
                    ptz_stop(cam)
                elif "dx" in q or "dy" in q:
                    dx = float((q.get("dx") or ["0"])[0])
                    dy = float((q.get("dy") or ["0"])[0])
                    ptz_relative(cam, dx, dy)
                else:
                    x = float((q.get("x") or ["0"])[0])
                    y = float((q.get("y") or ["0"])[0])
                    ptz_move(cam, x, y)
                return self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                return self._send(502, json.dumps({"error": str(e)}))
        # static files
        return self._static(path)

    do_HEAD = do_GET

    def do_POST(self):
        public_url = _public_url()
        if public_url and self.headers.get("X-Forwarded-Proto") is None:
            self.send_response(302)
            self.send_header("Location", public_url + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        u = urlparse(self.path)
        try:
            if u.path == "/api/admin/config":
                payload = self._json_body()
                saved = save_topology(payload.get("cameras") or [], payload.get("nodes") or [])
                return self._send(200, json.dumps({"ok": True, **saved}))
            if u.path == "/api/admin/apply":
                return self._send(200, json.dumps(apply_runtime_config()))
            return self._send(404, json.dumps({"error": "not found"}))
        except ValueError as e:
            return self._send(400, json.dumps({"ok": False, "error": str(e)}))
        except Exception as e:
            return self._send(500, json.dumps({"ok": False, "error": str(e)}))

    def _static(self, path):
        if path == "/" or path == "":
            path = "/index.html"
        path = path.split("?")[0]
        fp = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not fp.startswith(WEB) or not os.path.isfile(fp):
            return self._send(404, "not found", "text/plain")
        ext = os.path.splitext(fp)[1].lower()
        with open(fp, "rb") as f:
            data = f.read()
        return self._send(200, data, MIME.get(ext, "application/octet-stream"))


if __name__ == "__main__":
    port = int(os.environ.get("CAMWALL_PORT", "8090"))
    threading.Thread(target=recording_cache_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"CamWall backend on :{port}, {len(CAMERAS)} cameras")
    srv.serve_forever()
