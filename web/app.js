import { VideoRTC } from './video-rtc.js';
customElements.define('video-rtc', VideoRTC);

// go2rtc signaling WebSocket.
//  • Over HTTPS (via Traefik): use secure same-origin wss through the /go2rtc proxy
//    (a plain ws:// would be blocked as mixed content).
//  • Over plain HTTP (direct to the Aero): talk to go2rtc on :1984 directly.
const DEFAULT_WS_BASE = (location.protocol === 'https:')
  ? `wss://${location.host}/go2rtc/api/ws?src=`
  : `ws://${location.hostname}:1984/api/ws?src=`;

function cameraWsBase(cam) {
  if (!cam || !cam.ws_base) return DEFAULT_WS_BASE;
  if (cam.ws_base.startsWith('ws://') || cam.ws_base.startsWith('wss://')) return cam.ws_base;
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}${cam.ws_base}`;
}

const grid   = document.getElementById('grid');
const viewer = document.getElementById('viewer');
const stage  = document.getElementById('viewer-stage');
const vTitle = document.getElementById('v-title');

let cameras = [];
let current = null;           // currently open camera in viewer
let viewerPlayer = null;      // <video-rtc> in the viewer

// Per-camera power state is SERVER-SIDE (global, shared by all viewers). A
// disabled camera gets no players from any client, so go2rtc's on-demand pull
// stops -> the camera is no longer processed or requested.
const offCams = new Set();
const tiles = new Map();      // key -> { tile, cam, player }

// Apply a fresh disabled-set from the server, re-rendering only what changed.
function applyState(disabled) {
  const next = new Set(disabled || []);
  const changed = [];
  cameras.forEach(c => { if (next.has(c.key) !== offCams.has(c.key)) changed.push(c.key); });
  offCams.clear(); next.forEach(k => offCams.add(k));
  changed.forEach(k => { if (tiles.has(k)) renderTile(k); });
  if (current && changed.includes(current.key)) renderViewer();
}

function toggleCam(key) {
  const turnOn = offCams.has(key);            // currently off -> request on
  if (turnOn) offCams.delete(key); else offCams.add(key);   // optimistic
  if (tiles.has(key)) renderTile(key);
  if (current && current.key === key) renderViewer();
  fetch(`/api/power?cam=${encodeURIComponent(key)}&on=${turnOn ? 1 : 0}`)
    .then(r => r.json()).then(d => applyState(d.disabled))
    .catch(() => {});
}

// Keep all viewers in sync: poll the shared state periodically.
setInterval(() => {
  fetch('/api/state').then(r => r.json()).then(d => applyState(d.disabled)).catch(() => {});
}, 5000);

/* ------------------------------------------------------------------ *
 *  Build a VideoRTC player element
 * ------------------------------------------------------------------ */
function makePlayer(streamName, parent, { muted = true }) {
  const el = document.createElement('video-rtc');
  el.background = false;        // disconnect when off-screen (saves cameras/CPU)
  // Cameras use H264 video + PCMA (G.711) audio. MSE can't carry PCMA, so we
  // use WebRTC exclusively — it handles both H264 and PCMA natively, lowest latency.
  el.mode = 'webrtc';
  el.style.width = '100%'; el.style.height = '100%';
  parent.appendChild(el);       // parent must already be in the DOM -> creates el.video
  // configure underlying <video>
  if (el.video) {
    el.video.controls = false;
    el.video.muted = muted;
    el.video.playsInline = true;
  }
  el.src = streamName;
  return el;
}

// Stream names depend on the camera's timestamp mode:
//  - ffmpeg mode uses transcoded streams with a burned-in timestamp
//  - off/osd modes use the plain streams (osd = camera burns it in at source),
//    so fullscreen stays a direct copy (no transcode = lowest latency)
function gridStream(cam) { return cam.ts === 'ffmpeg' ? cam.key + '_gridts' : cam.key + '_grid'; }
function mainStream(cam) { return cam.ts === 'ffmpeg' ? cam.key + '_maints' : cam.key; }

/* ------------------------------------------------------------------ *
 *  Grid
 * ------------------------------------------------------------------ */
function buildGrid() {
  grid.innerHTML = '';
  tiles.clear();
  cameras.forEach(cam => {
    const tile = document.createElement('div');
    tile.className = 'tile offline';
    tile.dataset.key = cam.key;
    tile.addEventListener('click', () => openViewer(cam));
    grid.appendChild(tile);      // in DOM first so the player can init
    tiles.set(cam.key, { tile, cam, player: null });
    renderTile(cam.key);
  });
}

// (Re)build one tile's contents based on its power state.
function renderTile(key) {
  const t = tiles.get(key); if (!t) return;
  const { tile, cam } = t;
  if (t.player) { try { t.player.remove(); } catch (e) {} t.player = null; }
  tile.innerHTML = '';
  const off = offCams.has(key);
  tile.classList.toggle('powered-off', off);
  tile.classList.remove('live'); tile.classList.add('offline');

  if (!off) {
    const player = makePlayer(cameraWsBase(cam) + encodeURIComponent(gridStream(cam)), tile, { muted: true });
    t.player = player;
    const v = player.video;
    if (v) {
      const markLive = () => { tile.classList.add('live'); tile.classList.remove('offline'); };
      const markDown = () => { tile.classList.remove('live'); tile.classList.add('offline'); };
      v.addEventListener('playing', markLive);
      v.addEventListener('loadeddata', markLive);
      v.addEventListener('emptied', markDown);
      v.addEventListener('stalled', markDown);
    }
  }

  const label = document.createElement('div');
  label.className = 'label'; label.textContent = cam.name;
  const dot = document.createElement('div'); dot.className = 'dot';
  const badge = document.createElement('div'); badge.className = 'badge';
  badge.textContent = off ? cam.name + ' — off' : cam.name + ' — connecting…';

  const pw = document.createElement('button');
  pw.className = 'powerbtn'; pw.title = 'Turn feed on/off'; pw.textContent = '⏻';
  pw.setAttribute('aria-label', 'Turn ' + cam.name + ' on/off');
  pw.addEventListener('click', (e) => { e.stopPropagation(); toggleCam(key); });
  pw.addEventListener('pointerdown', (e) => e.stopPropagation());

  const cfg = document.createElement('button');
  cfg.className = 'cfgbtn'; cfg.title = 'Camera settings'; cfg.textContent = '⚙';
  cfg.setAttribute('aria-label', 'Settings for ' + cam.name);
  cfg.addEventListener('click', (e) => { e.stopPropagation(); openConfig(cam.key); });
  cfg.addEventListener('pointerdown', (e) => e.stopPropagation());

  tile.append(label, dot, badge, pw, cfg);
}

/* ------------------------------------------------------------------ *
 *  Fullscreen viewer (main stream)
 * ------------------------------------------------------------------ */
function openViewer(cam) {
  current = cam;
  vTitle.textContent = cam.name;
  resetZoom();
  viewer.classList.remove('hidden');
  grid.style.display = 'none';   // pauses grid sub-streams (off-screen)
  renderViewer();
  showHint();
}

// (Re)build the viewer stage based on the current camera's power state.
function renderViewer() {
  if (viewerPlayer) { try { viewerPlayer.remove(); } catch (e) {} viewerPlayer = null; }
  const ph = stage.querySelector('.viewer-off'); if (ph) ph.remove();
  const off = current && offCams.has(current.key);
  if (current && !off) {
    viewerPlayer = makePlayer(cameraWsBase(current) + encodeURIComponent(mainStream(current)), stage, { muted: false });   // main stream, audio on
    if (viewerPlayer.video) {
      viewerPlayer.video.muted = false;
      viewerPlayer.video.volume = parseFloat(vVol.value);
    }
    applyTransform();
  } else if (off) {
    const d = document.createElement('div');
    d.className = 'viewer-off';
    d.innerHTML = '<div>⏻<div class="off-label">Feed off — tap power to turn on</div></div>';
    stage.appendChild(d);
  }
  updateMuteIcon();
  updatePowerIcon();
}

function closeViewer() {
  ptzStop();
  stopTalk();
  if (viewerPlayer) { try { viewerPlayer.remove(); } catch (e) {} viewerPlayer = null; }
  const ph = stage.querySelector('.viewer-off'); if (ph) ph.remove();
  viewer.classList.add('hidden');
  grid.style.display = '';       // resumes grid
  current = null;
  resetZoom();
}

document.getElementById('v-back').addEventListener('click', closeViewer);

const vPower = document.getElementById('v-power');
function updatePowerIcon() {
  if (!vPower) return;
  const off = current && offCams.has(current.key);
  vPower.classList.toggle('is-off', !!off);
  vPower.title = off ? 'Turn feed on' : 'Turn feed off';
}
if (vPower) vPower.addEventListener('click', () => { if (current) toggleCam(current.key); });

/* ------------------------------------------------------------------ *
 *  Volume / mute
 * ------------------------------------------------------------------ */
const vVol  = document.getElementById('v-vol');
const vMute = document.getElementById('v-mute');

function curVideo() { return viewerPlayer && viewerPlayer.video; }
function updateMuteIcon() {
  const v = curVideo();
  vMute.textContent = (!v || v.muted || v.volume === 0) ? '🔇' : '🔊';
}
vVol.addEventListener('input', () => {
  const v = curVideo(); if (!v) return;
  v.volume = parseFloat(vVol.value);
  v.muted = v.volume === 0;
  updateMuteIcon();
});
vMute.addEventListener('click', () => {
  const v = curVideo(); if (!v) return;
  v.muted = !v.muted;
  if (!v.muted && v.volume === 0) { v.volume = 1; vVol.value = 1; }
  updateMuteIcon();
});

document.getElementById('btn-mute-all').addEventListener('click', (e) => {
  // grid tiles are always muted; this mutes the viewer if open
  const v = curVideo(); if (v) { v.muted = true; updateMuteIcon(); }
  e.currentTarget.classList.toggle('active');
});

/* ------------------------------------------------------------------ *
 *  Two-way audio (talk to camera speaker via Tapo backchannel)
 * ------------------------------------------------------------------ */
const vTalk = document.getElementById('v-talk');
let talkPC = null, talkStream = null, talkMuteWas = null, talkCtx = null, talkWS = null;
const TALK_GAIN = 4.0;   // mic boost for the camera speaker (raise if still quiet)

function setTalkUI(state) {   // 'off' | 'connecting' | 'talking'
  if (!vTalk) return;
  vTalk.classList.toggle('connecting', state === 'connecting');
  vTalk.classList.toggle('talking', state === 'talking');
  vTalk.title = state === 'talking' ? 'Stop talking' : 'Talk to camera';
}

async function startTalk() {
  if (!current || talkPC) return;
  setTalkUI('connecting');
  try {
    talkStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false }
    });
  } catch (e) {
    setTalkUI('off');
    alert('Microphone unavailable: ' + (e && e.message ? e.message : e));
    return;
  }
  // Half-duplex: mute the incoming feed while talking to avoid echo/feedback.
  const v = curVideo();
  if (v) { talkMuteWas = v.muted; v.muted = true; updateMuteIcon(); }

  // Boost mic level via WebAudio (AGC off above so this gain is predictable).
  let sendTrack = talkStream.getAudioTracks()[0];
  try {
    talkCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = talkCtx.createMediaStreamSource(talkStream);
    const gain = talkCtx.createGain(); gain.gain.value = TALK_GAIN;
    const dest = talkCtx.createMediaStreamDestination();
    src.connect(gain).connect(dest);
    sendTrack = dest.stream.getAudioTracks()[0];
  } catch (e) { /* fall back to raw mic track */ }

  const pc = new RTCPeerConnection({ bundlePolicy: 'max-bundle' });
  talkPC = pc;
  pc.addEventListener('connectionstatechange', () => {
    if (['failed', 'disconnected', 'closed'].includes(pc.connectionState)) stopTalk();
    else if (pc.connectionState === 'connected') setTalkUI('talking');
  });
  const tx = pc.addTransceiver(sendTrack, { direction: 'sendonly' });
  // Force G.711 (PCMA/PCMU) — the Tapo backchannel needs it; the browser would
  // otherwise send Opus, which the camera can't play (result: silence).
  try {
    const caps = RTCRtpSender.getCapabilities('audio');
    const g711 = caps.codecs.filter(c => /pcma|pcmu/i.test(c.mimeType));
    const others = caps.codecs.filter(c => !/pcma|pcmu/i.test(c.mimeType));
    if (g711.length && tx.setCodecPreferences) tx.setCodecPreferences(g711.concat(others));
  } catch (e) { /* setCodecPreferences unsupported -> leave default */ }

  // Signal via go2rtc's WebSocket API with trickle ICE — this is go2rtc's own
  // two-way-audio path (the WHEP POST doesn't register the backchannel consumer).
  const ws = new WebSocket(cameraWsBase(current) + encodeURIComponent(current.key + '_talk'));
  talkWS = ws;
  ws.addEventListener('open', async () => {
    pc.addEventListener('icecandidate', ev => {
      if (ev.candidate) ws.send(JSON.stringify({ type: 'webrtc/candidate', value: ev.candidate.candidate }));
    });
    try {
      await pc.setLocalDescription(await pc.createOffer());
      ws.send(JSON.stringify({ type: 'webrtc/offer', value: pc.localDescription.sdp }));
    } catch (e) { stopTalk(); }
  });
  ws.addEventListener('message', ev => {
    let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === 'webrtc/candidate') {
      try { pc.addIceCandidate({ candidate: msg.value, sdpMid: '0' }); } catch (e) {}
    } else if (msg.type === 'webrtc/answer') {
      pc.setRemoteDescription({ type: 'answer', sdp: msg.value }).catch(() => {});
    } else if (msg.type === 'error') {
      alert('Talk failed: ' + msg.value); stopTalk();
    }
  });
  ws.addEventListener('error', () => { if (talkPC && talkPC.connectionState !== 'connected') { alert('Talk connection failed'); stopTalk(); } });
}

function stopTalk() {
  if (talkWS) { try { talkWS.close(); } catch (e) {} talkWS = null; }
  if (talkPC) { try { talkPC.close(); } catch (e) {} talkPC = null; }
  if (talkCtx) { try { talkCtx.close(); } catch (e) {} talkCtx = null; }
  if (talkStream) { talkStream.getTracks().forEach(t => t.stop()); talkStream = null; }
  const v = curVideo();
  if (v && talkMuteWas !== null) { v.muted = talkMuteWas; updateMuteIcon(); }
  talkMuteWas = null;
  setTalkUI('off');
}

function toggleTalk() { if (talkPC) stopTalk(); else startTalk(); }
if (vTalk) vTalk.addEventListener('click', toggleTalk);

/* ------------------------------------------------------------------ *
 *  Page fullscreen
 * ------------------------------------------------------------------ */
function toggleFs(elem) {
  if (!document.fullscreenElement) (elem.requestFullscreen || elem.webkitRequestFullscreen || (()=>{})).call(elem);
  else document.exitFullscreen();
}
document.getElementById('v-fs').addEventListener('click', () => toggleFs(viewer));
document.getElementById('btn-fullscreen-page').addEventListener('click', () => toggleFs(document.documentElement));

/* ------------------------------------------------------------------ *
 *  PTZ (physical pan/tilt via ONVIF backend)
 * ------------------------------------------------------------------ */
// Two modes:
//   • Short press  -> RelativeMove (a fixed, self-terminating nudge)
//   • Hold         -> ContinuousMove (smooth pan) + Stop on release
// ContinuousMove carries a 1s camera-side Timeout, so even a lost/racing Stop
// can only overshoot ~1s — never "run to the limit".
const NUDGE = 0.06;          // relative step per short press
const CONT_SPEED = 0.5;      // continuous velocity while held
const HOLD_MS = 250;         // press longer than this becomes continuous
const CONT_REFRESH_MS = 700; // re-issue continuous move while held (Timeout=1s)

// Module-level PTZ state so a single window-level release handler can always
// end a move — even if a button's own pointerup is missed (pointer capture,
// pointer moved off the button, touch quirks). Combined with the camera-side
// 1s Timeout on ContinuousMove, a move can never get "stuck".
let ptzHoldTimer = null;   // fires when a press becomes a hold
let ptzRepeat = null;      // re-issues ContinuousMove while held
let ptzHolding = false;    // currently in continuous mode
let ptzDir = null;         // {sx,sy} of the active button

function ptzReq(params) {
  if (!current) return;
  fetch(`/api/ptz?cam=${encodeURIComponent(current.key)}&${params}`).catch(()=>{});
}
function ptzPressStart(sx, sy) {
  ptzEnd(true);                                   // clear anything prior, no request
  ptzDir = { sx, sy };
  ptzHolding = false;
  ptzHoldTimer = setTimeout(() => {               // held long enough -> continuous
    ptzHolding = true;
    ptzReq(`x=${sx*CONT_SPEED}&y=${sy*CONT_SPEED}`);
    ptzRepeat = setInterval(() => ptzReq(`x=${sx*CONT_SPEED}&y=${sy*CONT_SPEED}`), CONT_REFRESH_MS);
  }, HOLD_MS);
}
function ptzEnd(silent) {
  if (ptzHoldTimer) { clearTimeout(ptzHoldTimer); ptzHoldTimer = null; }
  if (ptzRepeat) { clearInterval(ptzRepeat); ptzRepeat = null; }
  if (silent) { ptzHolding = false; ptzDir = null; return; }
  if (ptzHolding) { ptzReq('stop=1'); }                             // hold -> stop
  else if (ptzDir) { ptzReq(`dx=${ptzDir.sx*NUDGE}&dy=${ptzDir.sy*NUDGE}`); } // tap -> nudge
  ptzHolding = false; ptzDir = null;
}
document.querySelectorAll('.ptz-btn').forEach(btn => {
  if (btn.dataset.home) {
    btn.addEventListener('click', resetZoom);
    return;
  }
  const sx = Math.sign(parseFloat(btn.dataset.x));
  const sy = Math.sign(parseFloat(btn.dataset.y));
  btn.addEventListener('pointerdown', (e) => { e.preventDefault(); ptzPressStart(sx, sy); });
});
// Global release safety net — any pointer release/cancel ends an active PTZ move.
window.addEventListener('pointerup',     () => { if (ptzDir || ptzHolding) ptzEnd(false); });
window.addEventListener('pointercancel', () => { if (ptzDir || ptzHolding) ptzEnd(false); });
function ptzStop() { ptzEnd(true); if (current) ptzReq('stop=1'); }

/* ------------------------------------------------------------------ *
 *  Digital zoom + pan (touch pinch / drag / wheel / buttons)
 * ------------------------------------------------------------------ */
let scale = 1, tx = 0, ty = 0;
const MIN = 1, MAX = 6;

function applyTransform() {
  if (!viewerPlayer) return;
  // clamp pan so the image edges never leave the stage
  const rect = stage.getBoundingClientRect();
  const maxX = rect.width  * (scale - 1);
  const maxY = rect.height * (scale - 1);
  tx = Math.min(0, Math.max(-maxX, tx));
  ty = Math.min(0, Math.max(-maxY, ty));
  viewerPlayer.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
}
function resetZoom() { scale = 1; tx = 0; ty = 0; applyTransform(); }

function zoomAt(cx, cy, factor) {
  const rect = stage.getBoundingClientRect();
  const px = cx - rect.left, py = cy - rect.top;
  const newScale = Math.min(MAX, Math.max(MIN, scale * factor));
  const k = newScale / scale;
  // keep the point under the cursor fixed
  tx = px - k * (px - tx);
  ty = py - k * (py - ty);
  scale = newScale;
  if (scale === 1) { tx = 0; ty = 0; }
  applyTransform();
}

document.getElementById('z-in').addEventListener('click', () => { const r = stage.getBoundingClientRect(); zoomAt(r.left + r.width/2, r.top + r.height/2, 1.3); });
document.getElementById('z-out').addEventListener('click', () => { const r = stage.getBoundingClientRect(); zoomAt(r.left + r.width/2, r.top + r.height/2, 1/1.3); });
document.getElementById('z-reset').addEventListener('click', resetZoom);

stage.addEventListener('wheel', (e) => {
  e.preventDefault();
  zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.15 : 1/1.15);
}, { passive: false });

// Pointer-based drag (pan) and pinch (zoom)
const pointers = new Map();
let lastDist = 0, lastMid = null;

stage.addEventListener('pointerdown', (e) => {
  if (e.target.closest('.ptz') || e.target.closest('.zoomctl') || e.target.closest('.viewer-top')) return;
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  stage.setPointerCapture(e.pointerId);
});
stage.addEventListener('pointermove', (e) => {
  if (!pointers.has(e.pointerId)) return;
  const prev = pointers.get(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

  if (pointers.size === 1) {
    if (scale > 1) { tx += e.clientX - prev.x; ty += e.clientY - prev.y; applyTransform(); }
  } else if (pointers.size === 2) {
    const pts = [...pointers.values()];
    const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
    const mid  = { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 };
    if (lastDist) {
      zoomAt(mid.x, mid.y, dist / lastDist);
      if (lastMid) { tx += mid.x - lastMid.x; ty += mid.y - lastMid.y; applyTransform(); }
    }
    lastDist = dist; lastMid = mid;
  }
});
function endPointer(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) { lastDist = 0; lastMid = null; }
}
stage.addEventListener('pointerup', endPointer);
stage.addEventListener('pointercancel', endPointer);
stage.addEventListener('pointerleave', endPointer);

// double-tap / double-click to toggle zoom
let lastTap = 0;
stage.addEventListener('click', (e) => {
  if (e.target.closest('.ptz') || e.target.closest('.zoomctl') || e.target.closest('.viewer-top')) return;
  const now = Date.now();
  if (now - lastTap < 300) { scale > 1 ? resetZoom() : zoomAt(e.clientX, e.clientY, 2.2); }
  lastTap = now;
});

window.addEventListener('resize', applyTransform);
window.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !viewer.classList.contains('hidden')) closeViewer(); });

/* ------------------------------------------------------------------ *
 *  Hint auto-hide
 * ------------------------------------------------------------------ */
const hint = document.getElementById('hint');
function showHint() { hint.classList.remove('fade'); clearTimeout(showHint._t); showHint._t = setTimeout(() => hint.classList.add('fade'), 3500); }

/* ------------------------------------------------------------------ *
 *  Camera settings (rename + timestamp mode)
 * ------------------------------------------------------------------ */
const cfgModal = document.getElementById('cfg');
const cfgName  = document.getElementById('cfg-name');
const cfgTs    = document.getElementById('cfg-ts');
const cfgSave  = document.getElementById('cfg-save');
let cfgKey = null;

function camByKey(key) { return cameras.find(c => c.key === key); }

function openConfig(key) {
  const cam = camByKey(key); if (!cam || !cfgModal) return;
  cfgKey = key;
  cfgName.value = cam.name;
  cfgTs.value = cam.ts || 'off';
  cfgModal.classList.remove('hidden');
  cfgName.focus();
}
function closeConfig() { if (cfgModal) cfgModal.classList.add('hidden'); cfgKey = null; }

async function saveConfig() {
  if (!cfgKey) return;
  const key = cfgKey, name = cfgName.value.trim(), ts = cfgTs.value;
  cfgSave.disabled = true; cfgSave.textContent = 'Saving…';
  try {
    const url = `/api/config?cam=${encodeURIComponent(key)}&name=${encodeURIComponent(name)}&ts=${ts}`;
    const d = await fetch(url).then(r => r.json());
    if (d.cameras) {
      cameras = d.cameras;
      // refresh cached cam refs + re-render affected views
      const nc = camByKey(key);
      if (tiles.has(key)) { tiles.get(key).cam = nc; renderTile(key); }
      if (current && current.key === key) { current = nc; vTitle.textContent = nc.name; renderViewer(); }
    }
    if (d.osd_error) alert('Saved, but camera OSD change failed: ' + d.osd_error);
    closeConfig();
  } catch (e) {
    alert('Save failed: ' + (e && e.message ? e.message : e));
  } finally {
    cfgSave.disabled = false; cfgSave.textContent = 'Save';
  }
}

if (cfgModal) {
  cfgSave.addEventListener('click', saveConfig);
  document.getElementById('cfg-cancel').addEventListener('click', closeConfig);
  cfgModal.addEventListener('click', (e) => { if (e.target === cfgModal) closeConfig(); });
}
const vCfg = document.getElementById('v-cfg');
if (vCfg) vCfg.addEventListener('click', () => { if (current) openConfig(current.key); });

/* ------------------------------------------------------------------ *
 *  System status
 * ------------------------------------------------------------------ */
const sysModal = document.getElementById('sys');
const sysContent = document.getElementById('sys-content');
const sysSubtitle = document.getElementById('sys-subtitle');
let sysData = null;
let sysTab = 'overview';

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtBytes(n) {
  n = Number(n || 0);
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return 'unknown';
  seconds = Math.max(0, Math.round(Number(seconds)));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function okPill(ok, text) {
  return `<span class="pill ${ok ? 'ok' : 'bad'}">${esc(text || (ok ? 'OK' : 'Down'))}</span>`;
}

function card(title, body) {
  return `<section class="sys-card"><h3>${esc(title)}</h3>${body}</section>`;
}

function kv(rows) {
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>`;
}

function renderOverview(d) {
  const t = d.totals || {};
  return `
    <div class="metric-grid">
      ${card('Cameras', `<div class="metric">${t.powered_on || 0}/${t.cameras || 0}</div><div class="muted">powered on</div>`)}
      ${card('Nodes', `<div class="metric">${t.nodes || 0}</div><div class="muted">${(d.nodes || []).filter(n => n.api?.ok).length} online</div>`)}
      ${card('Active streams', `<div class="metric">${t.active_streams || 0}</div><div class="muted">${t.producers || 0} producer entries, ${t.consumers || 0} consumers</div>`)}
      ${card('Power state', `<div class="metric">${t.powered_off || 0}</div><div class="muted">feeds disabled</div>`)}
    </div>
    <div class="sys-grid two">
      ${card('Master', kv([
        ['Host', esc(d.master?.host)],
        ['Port', esc(d.master?.port)],
        ['UI', esc(d.routes?.ui)],
        ['Node route', esc(d.routes?.node_go2rtc_pattern)],
      ]))}
      ${card('Nodes', `<table><thead><tr><th>Node</th><th>Host</th><th>Role</th><th>API</th><th>Streams</th></tr></thead><tbody>${(d.nodes || []).map(n => `
        <tr><td>${esc(n.name || n.id)}</td><td>${esc(n.host)}</td><td>${esc(n.role)}</td><td>${okPill(n.api?.ok, n.api?.ok ? `${n.api.latency_ms} ms` : n.api?.error)}</td><td>${esc(n.streams?.active_streams || 0)} active / ${esc(n.streams?.streams || 0)}</td></tr>`).join('')}</tbody></table>`)}
    </div>`;
}

function renderCameras(d) {
  return card('Camera inventory', `<table><thead><tr><th>Camera</th><th>IP</th><th>Node</th><th>Power</th><th>Timestamp</th><th>RTSP</th><th>ONVIF</th><th>Active</th></tr></thead><tbody>${(d.cameras || []).map(c => {
    const active = Object.values(c.streams || {}).some(s => s && s.active);
    return `<tr>
      <td><strong>${esc(c.name)}</strong><div class="muted">${esc(c.key)}</div></td>
      <td>${esc(c.ip)}</td>
      <td>${esc(c.node_name || c.node)}</td>
      <td>${okPill(c.power === 'on', c.power)}</td>
      <td>${esc(c.timestamp_mode)}</td>
      <td>${okPill(c.checks?.rtsp_554?.ok, c.checks?.rtsp_554?.ok ? `${c.checks.rtsp_554.latency_ms} ms` : c.checks?.rtsp_554?.error)}</td>
      <td>${okPill(c.checks?.onvif_2020?.ok, c.checks?.onvif_2020?.ok ? `${c.checks.onvif_2020.latency_ms} ms` : c.checks?.onvif_2020?.error)}</td>
      <td>${okPill(active, active ? 'yes' : 'idle')}</td>
    </tr>`;
  }).join('')}</tbody></table>`);
}

function renderNodes(d) {
  return `<div class="sys-grid">${(d.nodes || []).map(n => card(n.name || n.id, `
    ${kv([
      ['ID', esc(n.id)],
      ['Role', esc(n.role)],
      ['Host', esc(n.host)],
      ['API', okPill(n.api?.ok, n.api?.ok ? `${n.api.latency_ms} ms` : n.api?.error)],
      ['ICE', esc(n.webrtc_candidate)],
      ['Proxy', esc(n.go2rtc_proxy)],
      ['Streams', `${esc(n.streams?.active_streams || 0)} active / ${esc(n.streams?.streams || 0)}`],
      ['Producers', esc(n.streams?.producers || 0)],
      ['Consumers', esc(n.streams?.consumers || 0)],
      ['Received', esc(fmtBytes(n.streams?.bytes_recv))],
      ['Sent', esc(fmtBytes(n.streams?.bytes_send))],
    ])}
    <div class="mini-list">${(n.assigned_cameras || []).map(k => `<span>${esc(k)}</span>`).join('')}</div>
  `)).join('')}</div>`;
}

function renderStreams(d) {
  const rows = [];
  (d.cameras || []).forEach(c => {
    Object.entries(c.streams || {}).forEach(([name, s]) => rows.push({ camera: c.name, node: c.node, name, ...s }));
  });
  return card('go2rtc streams', `<table><thead><tr><th>Stream</th><th>Camera</th><th>Node</th><th>Producers</th><th>Consumers</th><th>Received</th><th>Sent</th></tr></thead><tbody>${rows.map(r => `
    <tr><td>${esc(r.name)}</td><td>${esc(r.camera)}</td><td>${esc(r.node)}</td><td>${esc(r.producers)}</td><td>${esc(r.consumers)}</td><td>${esc(fmtBytes(r.bytes_recv))}</td><td>${esc(fmtBytes(r.bytes_send))}</td></tr>
  `).join('')}</tbody></table>`);
}

function renderNetwork(d) {
  return `<div class="sys-grid two">
    ${card('Routes', kv([
      ['UI', esc(d.routes?.ui)],
      ['Legacy go2rtc', esc(d.routes?.legacy_go2rtc)],
      ['Node go2rtc', esc(d.routes?.node_go2rtc_pattern)],
    ]))}
    ${card('Config files', kv(Object.entries(d.master?.config_files || {}).map(([k, v]) => [k, esc(v)])))}
    ${card('Node ports', `<table><thead><tr><th>Node</th><th>1984</th><th>8555</th><th>8554</th></tr></thead><tbody>${(d.nodes || []).map(n => `
      <tr><td>${esc(n.name || n.id)}</td><td>${okPill(n.ports?.go2rtc_api_1984?.ok, n.ports?.go2rtc_api_1984?.ok ? `${n.ports.go2rtc_api_1984.latency_ms} ms` : n.ports?.go2rtc_api_1984?.error)}</td><td>${okPill(n.ports?.go2rtc_webrtc_8555?.ok, n.ports?.go2rtc_webrtc_8555?.ok ? `${n.ports.go2rtc_webrtc_8555.latency_ms} ms` : n.ports?.go2rtc_webrtc_8555?.error)}</td><td>${okPill(n.ports?.go2rtc_rtsp_8554?.ok, n.ports?.go2rtc_rtsp_8554?.ok ? `${n.ports.go2rtc_rtsp_8554.latency_ms} ms` : n.ports?.go2rtc_rtsp_8554?.error)}</td></tr>
    `).join('')}</tbody></table>`)}
  </div>`;
}

function renderSystem() {
  if (!sysData) {
    sysContent.innerHTML = '<div class="sys-loading">Loading…</div>';
    return;
  }
  sysSubtitle.textContent = `Generated ${sysData.generated_at}`;
  document.querySelectorAll('.sys-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === sysTab));
  const renderers = { overview: renderOverview, cameras: renderCameras, nodes: renderNodes, streams: renderStreams, network: renderNetwork };
  sysContent.innerHTML = sysTab === 'raw'
    ? `<pre class="raw">${esc(JSON.stringify(sysData, null, 2))}</pre>`
    : renderers[sysTab](sysData);
}

async function loadSystem() {
  sysSubtitle.textContent = 'Loading…';
  sysContent.innerHTML = '<div class="sys-loading">Loading…</div>';
  try {
    sysData = await fetch('/api/system').then(r => r.json());
    renderSystem();
  } catch (e) {
    sysContent.innerHTML = `<div class="sys-error">Failed to load system status: ${esc(e && e.message ? e.message : e)}</div>`;
  }
}

function openSystem() {
  if (!sysModal) return;
  sysModal.classList.remove('hidden');
  loadSystem();
}
function closeSystem() { if (sysModal) sysModal.classList.add('hidden'); }

document.getElementById('btn-system')?.addEventListener('click', openSystem);
document.getElementById('sys-close')?.addEventListener('click', closeSystem);
document.getElementById('sys-refresh')?.addEventListener('click', loadSystem);
sysModal?.addEventListener('click', (e) => { if (e.target === sysModal) closeSystem(); });
document.querySelectorAll('.sys-tab').forEach(btn => btn.addEventListener('click', () => {
  sysTab = btn.dataset.tab;
  renderSystem();
}));

/* ------------------------------------------------------------------ *
 *  Recording and playback
 * ------------------------------------------------------------------ */
const recModal = document.getElementById('rec');
const recContent = document.getElementById('rec-content');
const recSubtitle = document.getElementById('rec-subtitle');
let recData = null;
let recAllFiles = [];
let recFiles = [];
let recFilesLoading = false;
let recSelected = new Set();
let recPlayerUrl = '';
let recCurrentId = '';
let recTab = 'playback';
let recPlaybackCam = '';
let recTickTimer = null;
let recRefreshTimer = null;
let recRefreshingInBackground = false;
let recNoticeTimer = null;
let recProcessing = false;

function archiveText(conf) {
  const a = conf?.archive || {};
  if (!a.enabled) return 'off';
  return `${a.type || 'none'}${a.location ? ' · ' + a.location : ''}`;
}

function camRecConfig(key) {
  return (recData?.cameras || []).find(c => c.key === key)?.config || {};
}

async function recSet(params) {
  const url = '/api/recording/set?' + new URLSearchParams(params).toString();
  const d = await fetch(url).then(r => r.json());
  if (!d.ok) throw new Error(d.error || 'recording update failed');
  await loadRecording(false);
}

async function setAllRecording(enabled) {
  await recSet({ all: '1', enabled: enabled ? '1' : '0' });
}

async function setCameraRecording(key, enabled) {
  await recSet({ cam: key, enabled: enabled ? '1' : '0' });
}

function renderRecControls(d) {
  const t = d.totals || {};
  return `
    <div class="metric-grid">
      ${card('Recording', `<div class="metric">${t.recording || 0}</div><div class="muted">${t.enabled || 0} enabled</div>`)}
      ${card('Stored', `<div class="metric">${t.files || 0}</div><div class="muted">${fmtBytes(t.bytes || 0)}</div>`)}
      ${card('Segment size', `<div class="metric">${esc(d.global?.segment_minutes || 15)}m</div><div class="muted">global default</div>`)}
      ${card('Retention', `<div class="metric">${esc(d.global?.retention_hours || 0)}h</div><div class="muted">${fmtBytes((d.global?.max_mb || 0) * 1024 * 1024)} cap</div>`)}
    </div>
    ${card('Camera recording', `<table><thead><tr><th>Camera</th><th>Node</th><th>Status</th><th>Files</th><th>Latest</th><th>Segment</th><th>Retention</th><th></th></tr></thead><tbody>${(d.cameras || []).map(c => `
      <tr>
        <td><strong>${esc(c.name)}</strong><div class="muted">${esc(c.key)}</div></td>
        <td>${esc(c.node_name || c.node)}</td>
        <td>${okPill(c.recording, c.recording ? 'recording' : (c.config.enabled ? 'starting' : 'off'))}</td>
        <td>${esc(c.stats?.files || 0)}<div class="muted">${fmtBytes(c.stats?.bytes || 0)}</div></td>
        <td>${esc(c.stats?.latest || 'none')}</td>
        <td>${esc(c.config.segment_minutes)} min</td>
        <td>${esc(c.config.retention_hours)} h / ${fmtBytes((c.config.max_mb || 0) * 1024 * 1024)}</td>
        <td><button class="tbtn ${c.config.enabled ? '' : 'active'}" data-rec-action="toggle" data-key="${esc(c.key)}" data-on="${c.config.enabled ? '0' : '1'}">${c.config.enabled ? 'Stop' : 'Start'}</button></td>
      </tr>`).join('')}</tbody></table>`)}
    <div class="sys-grid two">
      ${card('Recorder nodes', `<table><thead><tr><th>Node</th><th>API</th><th>Root</th><th>Disk free</th></tr></thead><tbody>${(d.nodes || []).map(n => `
        <tr><td>${esc(n.name || n.id)}</td><td>${okPill(n.api?.ok, n.api?.ok ? `${n.api.latency_ms} ms` : n.api?.error)}</td><td>${esc(n.root || '')}</td><td>${fmtBytes(n.disk?.free || 0)}</td></tr>
      `).join('')}</tbody></table>`)}
    </div>`;
}

function renderRetention(d) {
  return card('Per-camera policy', `<table class="rec-policy"><thead><tr><th>Camera</th><th>Segment</th><th>Hours</th><th>Max MB</th><th>Archive</th><th>Type</th><th>Location</th><th></th></tr></thead><tbody>${(d.cameras || []).map(c => {
    const a = c.config.archive || {};
    return `<tr data-policy-row="${esc(c.key)}">
      <td><strong>${esc(c.name)}</strong><div class="muted">${esc(c.node_name || c.node)}</div></td>
      <td><input class="rec-input" data-field="segment_minutes" type="number" min="1" step="1" value="${esc(c.config.segment_minutes)}"></td>
      <td><input class="rec-input" data-field="retention_hours" type="number" min="0" step="1" value="${esc(c.config.retention_hours)}"></td>
      <td><input class="rec-input" data-field="max_mb" type="number" min="0" step="100" value="${esc(c.config.max_mb)}"></td>
      <td><input data-field="archive_enabled" type="checkbox" ${a.enabled ? 'checked' : ''}></td>
      <td><select class="rec-input" data-field="archive_type">
        ${['none', 'local', 'samba', 's3', 'ftp'].map(v => `<option value="${v}" ${a.type === v ? 'selected' : ''}>${v}</option>`).join('')}
      </select></td>
      <td><input class="rec-input wide" data-field="archive_location" type="text" value="${esc(a.location || '')}" placeholder="/mnt/archive or s3://bucket/path"></td>
      <td><button class="tbtn active" data-rec-action="save-policy" data-key="${esc(c.key)}">Save</button> <button class="tbtn" data-rec-action="copy-policy" data-key="${esc(c.key)}">Copy to all</button></td>
    </tr>`;
  }).join('')}</tbody></table>`);
}

function renderPlayback(d) {
  recFiles = recPlaybackCam ? recAllFiles.filter(f => f.camera === recPlaybackCam) : recAllFiles.slice();
  const opts = ['<option value="">All cameras</option>'].concat((d.cameras || []).map(c => `<option value="${esc(c.key)}" ${recPlaybackCam === c.key ? 'selected' : ''}>${esc(c.name)}</option>`)).join('');
  const selectedCount = recFiles.filter(f => recSelected.has(`${f.node}:${f.path}`)).length;
  const current = recAllFiles.find(f => `${f.node}:${f.path}` === recCurrentId);
  return `
    <div class="rec-layout">
    <section class="sys-card rec-player-card"><h3>Playback</h3>
      <div class="rec-filter">
        <select id="rec-playback-cam" class="rec-input">${opts}</select>
        <button id="rec-playback-refresh" class="tbtn">Refresh</button>
        <span id="rec-file-count" class="muted">${recFilesLoading ? 'Loading…' : `${recFiles.length} recordings`}</span>
        <div class="spacer"></div>
        <button class="tbtn" data-rec-bulk="lock" ${selectedCount ? '' : 'disabled'}>Lock</button>
        <button class="tbtn" data-rec-bulk="unlock" ${selectedCount ? '' : 'disabled'}>Unlock</button>
        <button class="tbtn" data-rec-bulk="download" ${selectedCount ? '' : 'disabled'}>Download</button>
        <button class="tbtn" data-rec-bulk="delete" ${selectedCount ? '' : 'disabled'}>Delete</button>
      </div>
      <div class="rec-player-wrap">
        <video id="rec-player" class="rec-player" playsinline ${recPlayerUrl ? `src="${esc(recPlayerUrl)}"` : ''}></video>
        <div id="rec-processing" class="rec-processing ${recProcessing ? '' : 'hidden'}">Processing…</div>
      </div>
      <div class="rec-progress">
        <div class="rec-progress-meta">
          <span id="rec-clock">${current ? esc(current.start_time_local || current.modified) : 'No recording selected'}</span>
          <span id="rec-elapsed">${current ? '0:00' : ''}</span>
        </div>
        <input id="rec-progress" type="range" min="0" max="0" value="0" step="0.1">
        <div class="rec-controls">
          <button id="rec-play-toggle" class="tbtn" type="button">Play</button>
          <button id="rec-mute-toggle" class="tbtn" type="button">Mute</button>
          <button id="rec-fullscreen" class="tbtn" type="button">Fullscreen</button>
          <button id="rec-menu" class="tbtn" type="button">Menu</button>
        </div>
      </div>
      <div id="rec-notice" class="rec-notice hidden"></div>
    </section>
    <section class="sys-card rec-segments-card"><h3>Segments</h3><div class="rec-table-scroll">${renderRecTable()}</div></section>
    </div>
  `;
}

function renderRecTable() {
  return `<table class="rec-table"><thead><tr><th><input id="rec-select-all" type="checkbox" ${recFiles.length && recFiles.every(f => recSelected.has(`${f.node}:${f.path}`)) ? 'checked' : ''}></th><th>Start time</th><th>Duration</th><th>Camera</th><th>Node</th><th>Size</th><th>Actions</th></tr></thead><tbody>${renderRecRows()}</tbody></table>`;
}

function renderRecRows() {
  if (recFilesLoading) return '<tr><td colspan="7" class="muted">Loading…</td></tr>';
  if (!recFiles.length) return '<tr><td colspan="7" class="muted">No recordings found</td></tr>';
  return recFiles.map(f => {
    const id = `${f.node}:${f.path}`;
    return `<tr class="rec-row ${id === recCurrentId ? 'active' : ''} ${f.active ? 'recording' : ''}" data-rec-action="play" data-id="${esc(id)}" data-url="${esc(f.url)}">
      <td><input class="rec-select" type="checkbox" data-id="${esc(id)}" ${recSelected.has(id) ? 'checked' : ''}></td>
      <td>${esc(f.start_time_local || f.modified)}</td>
      <td><span class="rec-duration" data-id="${esc(id)}">${esc(fmtDuration(f.duration_seconds))}</span>${f.active ? '<div class="muted">recording</div>' : ''}</td>
      <td>${esc(f.camera_name || f.camera)}</td>
      <td>${esc(f.node_name || f.node)}</td>
      <td>${fmtBytes(f.size)}</td>
      <td class="rec-actions">
        <button class="tbtn" data-rec-action="lock" data-node="${esc(f.node)}" data-path="${esc(f.path)}" data-locked="${f.locked ? '0' : '1'}">${f.locked ? 'Unlock' : 'Lock'}</button>
        ${f.active ? `<button class="tbtn active" data-rec-action="split" data-node="${esc(f.node)}" data-cam="${esc(f.camera)}">End file</button>` : ''}
        <button class="tbtn" data-rec-action="download" data-url="${esc(f.url)}" data-name="${esc(f.name || 'recording.mp4')}">Download</button>
        <button class="tbtn" data-rec-action="delete" data-node="${esc(f.node)}" data-path="${esc(f.path)}">Delete</button>
      </td>
    </tr>`;
  }).join('');
}

function renderArchive(d) {
  const a = d.global?.archive || {};
  return `<div class="sys-grid two">
    ${card('Global defaults', `
      <div class="rec-form" data-global-archive>
        <label><span>Segment minutes</span><input class="rec-input" data-field="segment_minutes" type="number" min="1" value="${esc(d.global?.segment_minutes || 15)}"></label>
        <label><span>Retention hours</span><input class="rec-input" data-field="retention_hours" type="number" min="0" value="${esc(d.global?.retention_hours || 0)}"></label>
        <label><span>Max MB</span><input class="rec-input" data-field="max_mb" type="number" min="0" value="${esc(d.global?.max_mb || 0)}"></label>
        <label class="check"><input data-field="archive_enabled" type="checkbox" ${a.enabled ? 'checked' : ''}> Archive after retention</label>
        <label><span>Archive type</span><select class="rec-input" data-field="archive_type">${['none', 'local', 'samba', 's3', 'ftp'].map(v => `<option value="${v}" ${a.type === v ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
        <label><span>Archive location</span><input class="rec-input" data-field="archive_location" type="text" value="${esc(a.location || '')}"></label>
        <button class="tbtn active" data-rec-action="save-global">Save global defaults</button>
      </div>
    `)}
    ${card('Effective archive policy', `<table><thead><tr><th>Camera</th><th>Archive</th></tr></thead><tbody>${(d.cameras || []).map(c => `
      <tr><td>${esc(c.name)}</td><td>${esc(archiveText(c.config))}</td></tr>
    `).join('')}</tbody></table>`)}
  </div>`;
}

function renderRecording() {
  if (!recData) {
    recContent.innerHTML = '<div class="sys-loading">Loading…</div>';
    return;
  }
  recSubtitle.textContent = `Generated ${recData.generated_at}`;
  document.querySelectorAll('.rec-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === recTab));
  recContent.classList.toggle('rec-playback-content', recTab === 'playback');
  updateRecordingBodyMode();
  const renderers = { controls: renderRecControls, retention: renderRetention, playback: renderPlayback, archive: renderArchive };
  recContent.innerHTML = renderers[recTab](recData);
  if (recTab === 'playback') wireRecPlayer();
}

async function loadRecordings(force = false) {
  const d = await fetch('/api/recordings' + (force ? '?refresh=1' : '')).then(r => r.json());
  recAllFiles = d.recordings || [];
  recSelected = new Set([...recSelected].filter(id => recAllFiles.some(f => `${f.node}:${f.path}` === id)));
}

async function loadRecording(includeFiles = true) {
  recSubtitle.textContent = 'Loading…';
  recContent.innerHTML = '<div class="sys-loading">Loading…</div>';
  try {
    recData = await fetch('/api/recording/status').then(r => r.json());
    if (includeFiles || recTab === 'playback') await loadRecordings();
    renderRecording();
  } catch (e) {
    recContent.innerHTML = `<div class="sys-error">Failed to load recordings: ${esc(e && e.message ? e.message : e)}</div>`;
  }
}

function recDateAt(file, seconds) {
  const source = file?.start_time_local || file?.started_at || file?.modified;
  if (!source) return '';
  const stamp = source.includes('T') ? source : source.replace(' ', 'T');
  const start = new Date(stamp);
  if (Number.isNaN(start.getTime())) return source;
  return new Date(start.getTime() + Math.max(0, seconds || 0) * 1000).toLocaleString();
}

function recStartedMs(file) {
  const source = file?.start_time_local || file?.started_at || file?.modified;
  if (!source) return null;
  const start = new Date(source.includes('T') ? source : source.replace(' ', 'T'));
  return Number.isNaN(start.getTime()) ? null : start.getTime();
}

function localActiveDuration(file) {
  const startMs = recStartedMs(file);
  if (!file?.active || startMs === null) return file?.duration_seconds;
  return Math.max(Number(file.duration_seconds || 0), (Date.now() - startMs) / 1000);
}

function tickActiveRecordingDurations() {
  if (!recModal || recModal.classList.contains('hidden') || recTab !== 'playback') return;
  for (const file of recAllFiles) {
    if (!file.active) continue;
    const id = `${file.node}:${file.path}`;
    const seconds = localActiveDuration(file);
    const cell = recContent.querySelector(`.rec-duration[data-id="${CSS.escape(id)}"]`);
    if (cell) cell.textContent = fmtDuration(seconds);
  }
}

function updateRecordingListOnly() {
  recFiles = recPlaybackCam ? recAllFiles.filter(f => f.camera === recPlaybackCam) : recAllFiles.slice();
  const count = document.getElementById('rec-file-count');
  if (count) count.textContent = recFilesLoading ? 'Loading…' : `${recFiles.length} recordings`;
  const scroll = recContent.querySelector('.rec-table-scroll');
  if (scroll) scroll.innerHTML = renderRecTable();
  tickActiveRecordingDurations();
  updateRecPlayerDisplay();
}

async function backgroundRefreshRecordings() {
  if (!recModal || recModal.classList.contains('hidden') || recTab !== 'playback' || recRefreshingInBackground) return;
  recRefreshingInBackground = true;
  try {
    await loadRecordings(true);
    updateRecordingListOnly();
  } catch (e) {
    console.warn('recording refresh failed', e);
  } finally {
    recRefreshingInBackground = false;
  }
}

function startRecordingTimers() {
  if (!recTickTimer) recTickTimer = setInterval(tickActiveRecordingDurations, 1000);
  if (!recRefreshTimer) recRefreshTimer = setInterval(backgroundRefreshRecordings, 10000);
}

function stopRecordingTimers() {
  if (recTickTimer) clearInterval(recTickTimer);
  if (recRefreshTimer) clearInterval(recRefreshTimer);
  recTickTimer = null;
  recRefreshTimer = null;
}

function updateRecPlayerDisplay() {
  const player = document.getElementById('rec-player');
  const progress = document.getElementById('rec-progress');
  const clock = document.getElementById('rec-clock');
  const elapsed = document.getElementById('rec-elapsed');
  const play = document.getElementById('rec-play-toggle');
  const mute = document.getElementById('rec-mute-toggle');
  const file = recAllFiles.find(f => `${f.node}:${f.path}` === recCurrentId);
  if (!player || !progress) return;
  const duration = Number.isFinite(player.duration) ? player.duration : 0;
  const current = Number.isFinite(player.currentTime) ? player.currentTime : 0;
  progress.max = duration ? String(duration) : '0';
  progress.value = String(current);
  if (clock) clock.textContent = file ? recDateAt(file, current) : 'No recording selected';
  if (elapsed) elapsed.textContent = file ? `${fmtDuration(current)} / ${duration ? fmtDuration(duration) : 'unknown'}` : '';
  if (play) play.textContent = player.paused ? 'Play' : 'Pause';
  if (mute) mute.textContent = player.muted || player.volume === 0 ? 'Unmute' : 'Mute';
}

function wireRecPlayer() {
  const player = document.getElementById('rec-player');
  const progress = document.getElementById('rec-progress');
  const play = document.getElementById('rec-play-toggle');
  const mute = document.getElementById('rec-mute-toggle');
  const fullscreen = document.getElementById('rec-fullscreen');
  const menu = document.getElementById('rec-menu');
  if (!player || !progress) return;
  player.addEventListener('loadedmetadata', updateRecPlayerDisplay);
  player.addEventListener('timeupdate', updateRecPlayerDisplay);
  player.addEventListener('durationchange', updateRecPlayerDisplay);
  player.addEventListener('play', updateRecPlayerDisplay);
  player.addEventListener('pause', updateRecPlayerDisplay);
  player.addEventListener('volumechange', updateRecPlayerDisplay);
  progress.addEventListener('input', () => {
    player.currentTime = Number(progress.value || 0);
    updateRecPlayerDisplay();
  });
  play?.addEventListener('click', () => {
    if (!player.src) return;
    if (player.paused) player.play().catch(() => {});
    else player.pause();
    updateRecPlayerDisplay();
  });
  mute?.addEventListener('click', () => {
    player.muted = !player.muted;
    updateRecPlayerDisplay();
  });
  fullscreen?.addEventListener('click', () => {
    const target = document.querySelector('.rec-player-wrap') || player;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else target.requestFullscreen?.().catch(() => {});
  });
  menu?.addEventListener('click', () => {
    showRecNotice('Playback controls are shown below the video.');
  });
  updateRecPlayerDisplay();
}

function showRecNotice(message) {
  const notice = document.getElementById('rec-notice');
  if (!notice) return;
  notice.textContent = message;
  notice.classList.remove('hidden');
  if (recNoticeTimer) clearTimeout(recNoticeTimer);
  recNoticeTimer = setTimeout(() => notice.classList.add('hidden'), 3500);
}

function setRecProcessing(on) {
  recProcessing = on;
  const overlay = document.getElementById('rec-processing');
  if (overlay) overlay.classList.toggle('hidden', !on);
}

async function recFileAction(action, node, path, params = {}, refresh = true) {
  const qs = new URLSearchParams({ node, path, ...params });
  const d = await fetch(`/api/recordings/${action}?` + qs.toString()).then(r => r.json());
  if (!d.ok) throw new Error(d.error || `${action} failed`);
  if (refresh) {
    await loadRecordings();
    renderRecording();
  }
}

async function recSplitAction(node, cam) {
  const before = recAllFiles.find(f => f.node === node && f.camera === cam && f.active)?.path || '';
  const qs = new URLSearchParams({ node, cam });
  setRecProcessing(true);
  const d = await fetch('/api/recordings/split?' + qs.toString()).then(r => r.json());
  if (!d.ok) {
    setRecProcessing(false);
    throw new Error(d.error || 'split failed');
  }
  for (let i = 0; i < 8; i += 1) {
    await loadRecordings(true);
    const current = recAllFiles.find(f => f.node === node && f.camera === cam && f.active);
    if (current && current.path !== before) break;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  setRecProcessing(false);
  updateRecordingListOnly();
}

function selectedRecordings() {
  const byId = new Map(recAllFiles.map(f => [`${f.node}:${f.path}`, f]));
  return [...recSelected].map(id => byId.get(id)).filter(Boolean);
}

async function runRecBulk(action) {
  const items = selectedRecordings();
  if (!items.length) return;
  if (action === 'delete' && !confirm(`Delete ${items.length} recordings?`)) return;
  if (action === 'download') {
    items.forEach(f => {
      const a = document.createElement('a');
      a.href = f.url;
      a.download = f.name || '';
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
    return;
  }
  for (const f of items) {
    if (action === 'delete') {
      await recFileAction('delete', f.node, f.path, {}, false);
    } else {
      await recFileAction('lock', f.node, f.path, { locked: action === 'lock' ? '1' : '0' }, false);
    }
  }
  recSelected.clear();
  await loadRecordings();
  renderRecording();
}

function rowParams(row, key) {
  const params = { cam: key };
  row.querySelectorAll('[data-field]').forEach(el => {
    const field = el.dataset.field;
    params[field] = el.type === 'checkbox' ? (el.checked ? '1' : '0') : el.value;
  });
  return params;
}

async function savePolicy(key, copyToAll) {
  const row = recContent.querySelector(`[data-policy-row="${CSS.escape(key)}"]`);
  if (!row) return;
  const params = rowParams(row, key);
  if (copyToAll) params.copy_to_all = '1';
  await recSet(params);
}

async function saveGlobalArchive() {
  const box = recContent.querySelector('[data-global-archive]');
  if (!box) return;
  const params = { scope: 'global' };
  box.querySelectorAll('[data-field]').forEach(el => {
    const field = el.dataset.field;
    params[field] = el.type === 'checkbox' ? (el.checked ? '1' : '0') : el.value;
  });
  await recSet(params);
}

function openRecording() {
  if (!recModal) return;
  recModal.classList.remove('hidden');
  recTab = 'playback';
  updateRecordingBodyMode();
  startRecordingTimers();
  loadRecording(true);
}
function closeRecording() {
  if (recModal) recModal.classList.add('hidden');
  updateRecordingBodyMode();
  stopRecordingTimers();
}

function updateRecordingBodyMode() {
  document.body.classList.toggle(
    'rec-playback-open',
    !!recModal && !recModal.classList.contains('hidden') && recTab === 'playback',
  );
}

document.getElementById('btn-recording')?.addEventListener('click', openRecording);
document.getElementById('rec-close')?.addEventListener('click', closeRecording);
document.getElementById('rec-refresh')?.addEventListener('click', () => loadRecording(true));
document.getElementById('rec-all-on')?.addEventListener('click', () => setAllRecording(true).catch(e => alert(e.message || e)));
document.getElementById('rec-all-off')?.addEventListener('click', () => setAllRecording(false).catch(e => alert(e.message || e)));
recModal?.addEventListener('click', (e) => { if (e.target === recModal) closeRecording(); });
document.querySelectorAll('.rec-tab').forEach(btn => btn.addEventListener('click', async () => {
  recTab = btn.dataset.tab;
  updateRecordingBodyMode();
  if (recTab === 'playback' && !recAllFiles.length) await loadRecordings().catch(() => {});
  if (recTab === 'playback') startRecordingTimers();
  renderRecording();
}));
recContent?.addEventListener('click', async (e) => {
  if (e.target.closest('input, select, a')) return;
  const btn = e.target.closest('[data-rec-action]');
  if (!btn) return;
  const action = btn.dataset.recAction;
  try {
    if (action === 'toggle') await setCameraRecording(btn.dataset.key, btn.dataset.on === '1');
    if (action === 'save-policy') await savePolicy(btn.dataset.key, false);
    if (action === 'copy-policy') await savePolicy(btn.dataset.key, true);
    if (action === 'save-global') await saveGlobalArchive();
    if (action === 'play') {
      const player = document.getElementById('rec-player');
      const nextId = btn.dataset.id || '';
      const file = recAllFiles.find(f => `${f.node}:${f.path}` === nextId);
      if (file?.active) {
        showRecNotice('This file is still being recorded. Use End file before playback.');
        return;
      }
      recPlayerUrl = btn.dataset.url;
      recCurrentId = nextId;
      recContent.querySelectorAll('.rec-row.active').forEach(row => row.classList.remove('active'));
      btn.classList.add('active');
      if (player) { player.src = recPlayerUrl; player.play().catch(() => {}); }
      updateRecPlayerDisplay();
    }
    if (action === 'download') {
      const a = document.createElement('a');
      a.href = btn.dataset.url;
      a.download = btn.dataset.name || '';
      document.body.appendChild(a);
      a.click();
      a.remove();
    }
    if (action === 'lock') {
      await recFileAction('lock', btn.dataset.node, btn.dataset.path, { locked: btn.dataset.locked });
    }
    if (action === 'split') {
      await recSplitAction(btn.dataset.node, btn.dataset.cam);
    }
    if (action === 'delete') {
      if (confirm('Delete this recording?')) await recFileAction('delete', btn.dataset.node, btn.dataset.path);
    }
  } catch (err) {
    alert(err && err.message ? err.message : err);
  }
});
recContent?.addEventListener('change', async (e) => {
  if (e.target.id === 'rec-playback-cam') {
    recPlaybackCam = e.target.value;
    recFilesLoading = true;
    renderRecording();
    await loadRecordings(true);
    recFilesLoading = false;
    renderRecording();
  }
  if (e.target.id === 'rec-select-all') {
    recFiles.forEach(f => {
      const id = `${f.node}:${f.path}`;
      if (e.target.checked) recSelected.add(id); else recSelected.delete(id);
    });
    renderRecording();
  }
  if (e.target.classList.contains('rec-select')) {
    if (e.target.checked) recSelected.add(e.target.dataset.id); else recSelected.delete(e.target.dataset.id);
    renderRecording();
  }
});
recContent?.addEventListener('click', async (e) => {
  if (e.target.id === 'rec-playback-refresh') {
    recFilesLoading = true;
    renderRecording();
    await loadRecordings();
    recFilesLoading = false;
    renderRecording();
  }
  const bulk = e.target.closest('[data-rec-bulk]');
  if (bulk) await runRecBulk(bulk.dataset.recBulk).catch(err => alert(err && err.message ? err.message : err));
});

/* ------------------------------------------------------------------ *
 *  Boot
 * ------------------------------------------------------------------ */
Promise.all([
  fetch('/api/cameras').then(r => r.json()),
  fetch('/api/state').then(r => r.json()).catch(() => ({ disabled: [] })),
])
  .then(([list, state]) => {
    cameras = list;
    (state.disabled || []).forEach(k => offCams.add(k));
    buildGrid();
  })
  .catch(err => { grid.innerHTML = '<div style="padding:20px;color:#f85149">Failed to load cameras: ' + err + '</div>'; });
