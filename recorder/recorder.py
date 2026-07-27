#!/usr/bin/env python3
import datetime
import ftplib
import json
import os
import shutil
import signal
import shlex
import subprocess
import threading
import time
import urllib.parse
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAMERAS_FILE = ROOT / "cameras.json"
NODES_FILE = ROOT / "nodes.json"
CONFIG_FILE = ROOT / "recording_config.json"
LOCKS_FILE = ROOT / "recording_locks.json"
ENV_FILE = ROOT / ".env"
RECORDINGS = ROOT / "recordings"
FFMPEG_RECORDINGS = Path(os.environ.get("CAMWALL_FFMPEG_RECORDINGS_ROOT", str(RECORDINGS)))
FFMPEG_CMD = shlex.split(os.environ.get("CAMWALL_FFMPEG_CMD", "ffmpeg"))
NODE_ID = os.environ.get("CAMWALL_NODE_ID", "")
PORT = int(os.environ.get("CAMWALL_RECORDER_PORT", "8091"))
START_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.mp4$")

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
DEFAULT_CONFIG = {
    "global": {
        "segment_minutes": 15,
        "retention_hours": 24,
        "max_mb": 10240,
        "archive": {"enabled": False, "type": "none", "location": ""},
    },
    "cameras": {},
}

lock = threading.RLock()
processes = {}
last_cleanup = 0


def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, value):
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, indent=2)
    os.replace(tmp, path)


def load_locks():
    value = read_json(LOCKS_FILE, {"locked": []})
    return set(value.get("locked") or [])


def save_locks(locked):
    write_json(LOCKS_FILE, {"locked": sorted(locked)})


def recording_path(rel):
    path = (RECORDINGS / rel).resolve()
    if not str(path).startswith(str(RECORDINGS.resolve())) or not path.is_file():
        return None
    return path


def load_cameras():
    cameras = read_json(CAMERAS_FILE, [])
    nodes = read_json(NODES_FILE, [])
    assigned = None
    for node in nodes:
        if node.get("id") == NODE_ID:
            assigned = set(node.get("cameras") or [])
            break
    if assigned is None:
        assigned = {cam["key"] for cam in cameras}
    return [cam for cam in cameras if cam.get("key") in assigned]


def load_config():
    cfg = read_json(CONFIG_FILE, DEFAULT_CONFIG)
    cfg.setdefault("global", {})
    cfg.setdefault("cameras", {})
    merged_global = dict(DEFAULT_CONFIG["global"])
    merged_global.update(cfg["global"])
    archive = dict(DEFAULT_CONFIG["global"]["archive"])
    archive.update(merged_global.get("archive") or {})
    merged_global["archive"] = archive
    cfg["global"] = merged_global
    return cfg


def camera_config(cfg, key):
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
    return out


def segment_path(cam):
    host_directory = RECORDINGS / cam["key"]
    host_directory.mkdir(parents=True, exist_ok=True)
    ffmpeg_directory = FFMPEG_RECORDINGS / cam["key"]
    return str(ffmpeg_directory / "%Y-%m-%d_%H-%M-%S.mp4")


def start_recording(cam, conf):
    key = cam["key"]
    if key in processes and processes[key].poll() is None:
        return
    source = cam.get("rtsp_main")
    if not source and (not ONVIF_USER or not ONVIF_PASS):
        raise RuntimeError("ONVIF_USER and ONVIF_PASS must be set")
    if not source:
        source = f"rtsp://{ONVIF_USER}:{ONVIF_PASS}@{cam['ip']}:554/stream1"
    cmd = FFMPEG_CMD + [
        "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", source,
        "-map", "0:v:0", "-c:v", "copy", "-an",
        "-f", "segment",
        "-segment_format", "mp4",
        "-segment_time", str(conf["segment_minutes"] * 60),
        "-reset_timestamps", "1",
        "-strftime", "1",
        segment_path(cam),
    ]
    log_path = RECORDINGS / f"{key}.ffmpeg.log"
    log = open(log_path, "ab", buffering=0)
    processes[key] = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL)


def stop_recording(key):
    proc = processes.get(key)
    if not proc or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def parse_start(path):
    match = START_RE.match(path.name)
    if not match:
        return None
    year, month, day, hour, minute, second = map(int, match.groups())
    # ffmpeg strftime uses the node's local timezone.
    local_dt = datetime.datetime(year, month, day, hour, minute, second).astimezone()
    return local_dt


def probe_duration(path):
    cmd = []
    if FFMPEG_CMD and FFMPEG_CMD[0] == "docker":
        rel = path.relative_to(RECORDINGS)
        cmd = FFMPEG_CMD[:-1] + ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(FFMPEG_RECORDINGS / rel)]
    else:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return max(0.0, float(result.stdout.strip()))
    except Exception:
        pass
    return None


def sync_processes():
    cams = load_cameras()
    cfg = load_config()
    keys = {cam["key"] for cam in cams}
    for key in list(processes):
        if key not in keys or processes[key].poll() is not None:
            processes.pop(key, None)
    for cam in cams:
        conf = camera_config(cfg, cam["key"])
        if conf["enabled"]:
            start_recording(cam, conf)
        else:
            stop_recording(cam["key"])
    return cams, cfg


def split_recording(key):
    cams = load_cameras()
    cam = next((item for item in cams if item.get("key") == key), None)
    if not cam:
        return {"ok": False, "error": "unknown camera"}
    cfg = load_config()
    conf = camera_config(cfg, key)
    if not conf["enabled"]:
        return {"ok": False, "error": "recording is not enabled"}
    with lock:
        stop_recording(key)
        start_recording(cam, conf)
    return {"ok": True, "camera": key}


def iter_files(key=None, include_duration=False):
    root = RECORDINGS
    if not root.exists():
        return []
    cfg = load_config()
    locked = load_locks()
    files = []
    bases = [root / key] if key else [p for p in root.iterdir() if p.is_dir()]
    now = time.time()
    for base in bases:
        if not base.exists():
            continue
        for path in base.glob("*.mp4"):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            started = parse_start(path)
            duration = None
            conf = camera_config(cfg, base.name)
            max_expected = max(3600, conf["segment_minutes"] * 60 * 2)
            # Duration probing is deliberately kept out of status/retention paths:
            # probing active or many files can block long enough for the master UI
            # to mark a node as down. Playback requests can afford the extra work.
            if include_duration and time.time() - stat.st_mtime > 10:
                duration = probe_duration(path)
            if duration is not None and duration > max_expected:
                duration = None
            fallback_duration = max(0, stat.st_mtime - started.timestamp()) if started else None
            if duration is None and fallback_duration is not None and fallback_duration <= max_expected:
                duration = fallback_duration
            display_start = started
            if duration is not None:
                display_start = datetime.datetime.fromtimestamp(stat.st_mtime - duration).astimezone()
            files.append({
                "camera": base.name,
                "path": str(path.relative_to(root)),
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "modified": datetime.datetime.utcfromtimestamp(stat.st_mtime).replace(microsecond=0).isoformat() + "Z",
                "started_at": display_start.isoformat() if display_start else None,
                "start_time_local": display_start.strftime("%Y-%m-%d %H:%M:%S") if display_start else path.stem,
                "duration_seconds": round(duration, 1) if duration is not None else None,
                "_started_ts": started.timestamp() if started else None,
                "_max_expected": max_expected,
                "locked": str(path.relative_to(root)) in locked,
            })
    latest_by_camera = {}
    for item in files:
        current = latest_by_camera.get(item["camera"])
        if current is None or item["mtime"] > current["mtime"]:
            latest_by_camera[item["camera"]] = item
    for item in files:
        active = (
            latest_by_camera.get(item["camera"]) is item
            and item["camera"] in processes
            and processes[item["camera"]].poll() is None
        )
        item["active"] = active
        if active and item.get("_started_ts"):
            duration = max(0, now - item["_started_ts"])
            if duration <= item["_max_expected"]:
                item["duration_seconds"] = round(duration, 1)
        item.pop("_started_ts", None)
        item.pop("_max_expected", None)
    files.sort(key=lambda item: item["mtime"], reverse=True)
    return files


def archive_file(path, archive):
    if not archive.get("enabled"):
        return
    atype = (archive.get("type") or "none").lower()
    location = archive.get("location") or ""
    if not location:
        return
    rel = path.relative_to(RECORDINGS)
    if atype in ("local", "samba"):
        dest = Path(location) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(path, dest)
    elif atype == "ftp":
        parsed = urllib.parse.urlparse(location)
        if not parsed.hostname:
            return
        user = urllib.parse.unquote(parsed.username or "anonymous")
        passwd = urllib.parse.unquote(parsed.password or "")
        base = parsed.path.strip("/")
        with ftplib.FTP(parsed.hostname, timeout=20) as ftp:
            ftp.login(user, passwd)
            parts = [p for p in (base + "/" + str(rel)).split("/") if p]
            for part in parts[:-1]:
                try:
                    ftp.mkd(part)
                except Exception:
                    pass
                ftp.cwd(part)
            with open(path, "rb") as f:
                ftp.storbinary("STOR " + parts[-1], f)
    elif atype == "s3":
        target = location.rstrip("/") + "/" + str(rel)
        subprocess.run(["aws", "s3", "cp", str(path), target], timeout=120, check=False)


def cleanup_retention():
    cfg = load_config()
    for cam in load_cameras():
        key = cam["key"]
        conf = camera_config(cfg, key)
        files = list(reversed(iter_files(key)))  # oldest first
        cutoff = time.time() - conf["retention_hours"] * 3600 if conf["retention_hours"] else None
        archive = conf.get("archive") or {}

        for item in list(files):
            if item.get("locked"):
                continue
            path = RECORDINGS / item["path"]
            if cutoff and item["mtime"] < cutoff:
                try:
                    archive_file(path, archive)
                except Exception:
                    pass
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

        if conf["max_mb"]:
            max_bytes = conf["max_mb"] * 1024 * 1024
            remaining = list(reversed(iter_files(key)))
            total = sum(item["size"] for item in remaining)
            for item in remaining:
                if total <= max_bytes:
                    break
                if item.get("locked"):
                    continue
                path = RECORDINGS / item["path"]
                try:
                    archive_file(path, archive)
                except Exception:
                    pass
                try:
                    path.unlink()
                    total -= item["size"]
                except FileNotFoundError:
                    pass


def status_payload():
    cams, cfg = sync_processes()
    disk = shutil.disk_usage(RECORDINGS)
    files = iter_files()
    by_cam = {}
    for item in files:
        b = by_cam.setdefault(item["camera"], {"files": 0, "bytes": 0, "latest": None})
        b["files"] += 1
        b["bytes"] += item["size"]
        if not b["latest"] or item["mtime"] > b["_mtime"]:
            b["latest"] = item["modified"]
            b["_mtime"] = item["mtime"]
    for value in by_cam.values():
        value.pop("_mtime", None)
    return {
        "ok": True,
        "node": NODE_ID,
        "generated_at": now_iso(),
        "root": str(RECORDINGS),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "cameras": [{
            "key": cam["key"],
            "name": cam.get("name", cam["key"]),
            "ip": cam["ip"],
            "config": camera_config(cfg, cam["key"]),
            "recording": cam["key"] in processes and processes[cam["key"]].poll() is None,
            "pid": processes[cam["key"]].pid if cam["key"] in processes and processes[cam["key"]].poll() is None else None,
            "stats": by_cam.get(cam["key"], {"files": 0, "bytes": 0, "latest": None}),
        } for cam in cams],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_body(self, code, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        global last_cleanup
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if time.time() - last_cleanup > 300:
            last_cleanup = time.time()
            threading.Thread(target=cleanup_retention, daemon=True).start()
        if parsed.path == "/api/status":
            return self.send_body(200, status_payload())
        if parsed.path == "/api/reload":
            with lock:
                sync_processes()
            return self.send_body(200, {"ok": True})
        if parsed.path == "/api/recordings":
            key = (qs.get("cam") or [""])[0] or None
            include_duration = (qs.get("duration") or ["0"])[0] == "1"
            return self.send_body(200, {"ok": True, "node": NODE_ID, "recordings": iter_files(key, include_duration=include_duration)})
        if parsed.path == "/api/file":
            rel = (qs.get("path") or [""])[0]
            path = (RECORDINGS / rel).resolve()
            if not str(path).startswith(str(RECORDINGS.resolve())) or not path.is_file():
                return self.send_body(404, "not found", "text/plain")
            size = path.stat().st_size
            start, end = 0, size - 1
            code = 200
            range_header = self.headers.get("Range")
            if range_header and range_header.startswith("bytes="):
                spec = range_header.split("=", 1)[1].split(",", 1)[0]
                a, _, b = spec.partition("-")
                try:
                    if not a and b:
                        start = max(0, size - int(b))
                    elif a:
                        start = int(a)
                        if b:
                            end = int(b)
                    start = max(0, min(start, size - 1))
                    end = max(start, min(end, size - 1))
                    code = 206
                except Exception:
                    start, end, code = 0, size - 1, 200
            length = end - start + 1
            self.send_response(code)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            return
        if parsed.path == "/api/delete":
            rel = (qs.get("path") or [""])[0]
            path = recording_path(rel)
            if not path:
                return self.send_body(404, {"ok": False, "error": "not found"})
            locked = load_locks()
            if rel in locked:
                return self.send_body(409, {"ok": False, "error": "recording is locked"})
            try:
                path.unlink()
                return self.send_body(200, {"ok": True, "path": rel})
            except Exception as e:
                return self.send_body(500, {"ok": False, "error": str(e)})
        if parsed.path == "/api/lock":
            rel = (qs.get("path") or [""])[0]
            if not recording_path(rel):
                return self.send_body(404, {"ok": False, "error": "not found"})
            locked = load_locks()
            if (qs.get("locked") or ["1"])[0] == "1":
                locked.add(rel)
            else:
                locked.discard(rel)
            save_locks(locked)
            return self.send_body(200, {"ok": True, "path": rel, "locked": rel in locked})
        if parsed.path == "/api/split":
            key = (qs.get("cam") or [""])[0]
            return self.send_body(200, split_recording(key))
        return self.send_body(404, "not found", "text/plain")

    do_HEAD = do_GET

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length)
                cfg = json.loads(body.decode())
                if not isinstance(cfg, dict) or "global" not in cfg or "cameras" not in cfg:
                    return self.send_body(400, {"error": "bad config"})
                write_json(CONFIG_FILE, cfg)
                with lock:
                    sync_processes()
                return self.send_body(200, {"ok": True})
            except Exception as e:
                return self.send_body(400, {"error": str(e)})
        return self.send_body(404, "not found", "text/plain")


def supervisor():
    while True:
        with lock:
            sync_processes()
        time.sleep(20)


if __name__ == "__main__":
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=supervisor, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"CamWall recorder node={NODE_ID or 'all'} on :{PORT}")
    server.serve_forever()
