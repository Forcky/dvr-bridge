// Minimal MSE-over-WebSocket client for go2rtc (via the app's /ws/live proxy).
// startMse(video, url, onState) returns {stop()}. onState gets a short status string.
function startMse(video, url, onState) {
  const MS = window.ManagedMediaSource || window.MediaSource;
  const say = (t) => { try { onState && onState(t); } catch (e) {} };
  if (!MS) { say('no MSE support'); return { stop() {} }; }
  const wanted = ['avc1.640029', 'avc1.64002A', 'avc1.640033', 'avc1.4D401F', 'avc1.42E01F', 'hvc1.1.6.L153.B0'];
  const codecs = wanted.filter((c) => MS.isTypeSupported('video/mp4; codecs="' + c + '"')).join(',');
  let ws = null, ms = null, sb = null, queue = [], stopped = false, timer = null, watchdog = null, retry = 0, lastData = 0;

  function cleanup() {
    clearInterval(timer); clearInterval(watchdog);
    if (ws) { ws.onclose = ws.onmessage = ws.onerror = null; try { ws.close(); } catch (e) {} }
    ws = null; sb = null; queue = [];
    if (video.src && video.src.startsWith('blob:')) { try { URL.revokeObjectURL(video.src); } catch (e) {} }
    video.removeAttribute('src'); try { video.load(); } catch (e) {}
  }

  function pump() {
    if (!sb || sb.updating || !queue.length) return;
    try { sb.appendBuffer(queue.shift()); } catch (e) {
      if (e.name === 'QuotaExceededError') trim(true); else { say('append error'); reconnect(); }
    }
  }
  function trim(force) {
    if (!sb || sb.updating || !video.buffered.length) return;
    const start = video.buffered.start(0), end = video.buffered.end(video.buffered.length - 1);
    if (force || end - start > 20) { try { sb.remove(start, Math.max(start, end - 8)); } catch (e) {} }
  }

  function connect() {
    if (stopped) return;
    say('connecting');
    ms = new MS();
    video.src = URL.createObjectURL(ms);
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(proto + '//' + location.host + url);
    ws.binaryType = 'arraybuffer';
    const opened = new Promise((r) => ms.addEventListener('sourceopen', r, { once: true }));
    ws.onopen = async () => { await opened; ws && ws.send(JSON.stringify({ type: 'mse', value: codecs })); };
    ws.onmessage = (ev) => {
      lastData = Date.now();
      if (typeof ev.data === 'string') {
        const m = JSON.parse(ev.data);
        if (m.type === 'mse') {
          try {
            sb = ms.addSourceBuffer(m.value);
            sb.mode = 'segments';
            sb.addEventListener('updateend', () => { trim(false); pump(); });
          } catch (e) { say('codec unsupported'); return; }
        } else if (m.type === 'error') { say('stream error'); }
        return;
      }
      queue.push(ev.data); pump();
      if (video.paused) video.play().catch(() => {});
      say('live'); retry = 0;
    };
    ws.onclose = ws.onerror = () => reconnect();
    timer = setInterval(() => { // stay near the live edge
      if (!video.buffered.length) return;
      const end = video.buffered.end(video.buffered.length - 1);
      if (end - video.currentTime > 3) video.currentTime = end - 0.4;
    }, 1000);
    lastData = Date.now();
    watchdog = setInterval(() => { if (Date.now() - lastData > 10000) reconnect(); }, 2000);
  }

  let rc = null;
  function reconnect() {
    if (stopped || rc) return;
    cleanup(); say('reconnecting');
    retry = Math.min(retry + 1, 6);
    rc = setTimeout(() => { rc = null; connect(); }, 1000 * retry);
  }
  video.muted = true; video.playsInline = true;
  connect();
  return { stop() { stopped = true; clearTimeout(rc); cleanup(); } };
}

// Shared header + DVR status line.
async function loadHeader(active) {
  const nav = document.getElementById('nav');
  if (nav) nav.innerHTML = '<h1>DVR Bridge</h1><a href="/" class="' + (active === 'live' ? 'on' : '') + '">Live</a><a href="/recordings" class="' + (active === 'rec' ? 'on' : '') + '">Recordings</a><a href="/settings" class="' + (active === 'settings' ? 'on' : '') + '">Settings</a><span class="spacer"></span><span id="status"></span>';
  try {
    const r = await (await fetch('/api/channels')).json();
    const st = document.getElementById('status');
    if (st) st.textContent = r.dvr_up ? '' : 'DVR API unreachable';
    return r;
  } catch (e) { return { channels: [], today: null }; }
}
