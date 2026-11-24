(async () => {
  const info = await loadHeader('rec');
  const $ = (id) => document.getElementById(id);
  const dayEl = $('day'), rows = $('rows'), msg = $('msg'), summary = $('summary');
  const tlwrap = $('tlwrap'), axisTop = $('axisTop'), axisBot = $('axisBot'), clipsBox = $('clips'), tip = $('tip');
  const pad = (n) => String(n).padStart(2, '0');
  const iso = (d) => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  const fmtSize = (b) => b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB';
  const hms = (s) => s.slice(11, 19);
  const secs = (s) => { const t = s.slice(11, 19).split(':'); return (+t[0]) * 3600 + (+t[1]) * 60 + (+t[2]); };
  const note = (t, err) => { msg.hidden = !t; msg.textContent = t || ''; msg.className = 'notice' + (err ? ' err' : ''); };
  const clock = (t, withSec) => { t = Math.round(t); const h = Math.floor(t / 3600) % 24 + (t >= 86400 ? 24 : 0); return pad(h) + ':' + pad(Math.floor(t / 60) % 60) + (withSec ? ':' + pad(t % 60) : ''); };
  const lenText = (s) => s >= 3600 ? (s / 3600).toFixed(s % 3600 ? 1 : 0) + ' h' : s >= 60 ? (s / 60).toFixed(s % 60 ? 1 : 0) + ' min' : Math.round(s) + ' s';

  // ---- player ---------------------------------------------------------------
  const player = $('player'), pv = $('pv'), dl = $('dl');
  const ERR = { busy: 'The gateway is already playing the maximum number of clips. Try again in a moment.',
    dvr_refused: 'The DVR refused to play that clip.', no_footage: 'The DVR has no video for that clip.',
    dvr_unavailable: 'The DVR media port did not answer.', dvr_login_refused: 'The DVR refused the gateway’s media login.',
    dvr_stalled: 'The DVR stopped sending video part-way through.' };
  let current = null;
  const clearOn = () => document.querySelectorAll('.on:not(.zb)').forEach(e => e.classList.remove('on'));
  function stopPlayer() { pv.pause(); pv.removeAttribute('src'); pv.load(); }
  $('pclose').onclick = () => { stopPlayer(); player.hidden = true; current = null; clearOn(); };
  pv.addEventListener('error', async () => {
    if (!current || !pv.getAttribute('src')) return;
    let why = 'The stream failed.';
    try { const j = await (await fetch('/api/playback/status')).json(); if (j.last_error && ERR[j.last_error]) why = ERR[j.last_error]; } catch (e) {}
    note('Channel ' + current.ch + ' ' + hms(current.clip.start) + '–' + hms(current.clip.end) + ': ' + why, true);
  });

  // rec = { ch, name, clip, s, e, els: [] }  (s/e = seconds into the day; e is 86400 for a clip that runs past midnight)
  function playClip(rec) {
    const { ch, name, clip } = rec;
    note('');
    stopPlayer();
    current = { ch, clip };
    clearOn();
    rec.els.forEach(e => e.classList.add('on'));
    const q = new URLSearchParams({ channel: ch, start: clip.start, end: clip.end });
    $('ptitle').textContent = ch + ' · ' + name + ' · ' + clip.start.slice(0, 10) + ' ' + hms(clip.start) + '–' + hms(clip.end);
    dl.href = '/api/export?' + q;
    player.hidden = false;
    pv.src = '/api/playback?' + q;
    pv.play().catch(() => {});
    player.scrollIntoView({ block: 'nearest' });
  }

  // ---- shared time axis: view = [a, b] seconds into the day ------------------------
  const DAY = 86400, MIN_SPAN = 20;
  const PRESETS = [['24 h', DAY], ['6 h', 21600], ['1 h', 3600], ['15 min', 900]];
  const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];
  let view = { a: 0, b: DAY };
  let recs = [];      // every clip element's record, for relayout
  let tls = [];       // the per-channel timeline elements
  let sels = [];      // their selection overlays
  let raf = 0;
  const span = () => view.b - view.a;

  function setView(a, b) {
    let sp = Math.min(DAY, Math.max(MIN_SPAN, b - a));
    a = Math.min(Math.max(0, a), DAY - sp);
    view = { a, b: a + sp };
    schedule();
  }
  function zoomAt(frac, factor) {
    const sp = span(), t = view.a + frac * sp;
    const ns = Math.min(DAY, Math.max(MIN_SPAN, sp * factor));
    setView(t - frac * ns, t - frac * ns + ns);
  }
  const setSpan = (sp) => { const c = (view.a + view.b) / 2; setView(c - sp / 2, c + sp / 2); };
  function ensureVisible(s, e) {
    const sp = span();
    if (s >= view.a && e <= view.b) return;
    const c = (s + e) / 2;
    setView(c - Math.max(sp, e - s) / 2, c + Math.max(sp, e - s) / 2);
  }
  // data-pending on #view is 1 from the moment a view change is requested until it has been drawn (also used by the UI tests)
  function schedule() { $('view').dataset.pending = '1'; if (!raf) raf = requestAnimationFrame(() => { raf = 0; layout(); }); }

  function tickStep(width) { for (const s of STEPS) if (s * width / span() >= 72) return s; return STEPS[STEPS.length - 1]; }
  function drawAxis(el, step) {
    el.textContent = '';
    const sp = span();
    for (let t = Math.ceil(view.a / step) * step; t <= view.b + 1e-6; t += step) {
      const f = (t - view.a) / sp;
      const lab = document.createElement('span');
      lab.textContent = clock(t, step < 60);
      lab.style.left = (f * 100) + '%';
      if (f < 0.04) lab.style.transform = 'translateX(0)'; else if (f > 0.96) lab.style.transform = 'translateX(-100%)';
      el.append(lab);
    }
  }

  function layout() {
    const sp = span();
    const first = tls[0];
    const w = first ? first.clientWidth : (axisTop.clientWidth || 1);
    const step = tickStep(w);
    drawAxis(axisTop, step); drawAxis(axisBot, step);
    const stepPx = step * w / sp, off = (Math.ceil(view.a / step) * step - view.a) * w / sp;
    for (const tl of tls) {
      tl.style.backgroundSize = stepPx + 'px 100%';
      tl.style.backgroundPosition = off + 'px 0';
    }
    for (const r of recs) {
      const el = r.el;
      if (r.e < view.a || r.s > view.b) { el.style.display = 'none'; continue; }
      el.style.display = '';
      el.style.left = ((r.s - view.a) / sp * 100) + '%';
      el.style.width = ((r.e - r.s) / sp * 100) + '%';
    }
    $('view').textContent = sp >= DAY ? 'Whole day' : clock(view.a, true) + ' – ' + clock(view.b, true) + ' (' + lenText(sp) + ')';
    for (const [btn, p] of presetBtns) btn.classList.toggle('on', Math.abs(sp - p) < 0.5);
    $('view').dataset.pending = raf ? '1' : '0';
    $('zout').disabled = sp >= DAY; $('reset').disabled = sp >= DAY; $('zin').disabled = sp <= MIN_SPAN;
  }

  // toolbar
  const presetBtns = [];
  const zbar = $('zbar');
  const mk = (id, text, title, fn) => { const b = document.createElement('button'); b.type = 'button'; if (id) b.id = id; b.textContent = text; b.title = title; b.setAttribute('aria-label', title); b.onclick = fn; b.classList.add('zb'); return b; };
  const grp = (...els) => { const g = document.createElement('span'); g.className = 'zgrp'; g.append(...els); return g; };
  const presetsGrp = [];
  for (const [label, sp] of PRESETS) { const b = mk('', label, 'Show ' + label, () => setSpan(sp)); presetBtns.push([b, sp]); presetsGrp.push(b); }
  let forceSelect = false;
  const selBtn = mk('selmode', 'Select', 'Drag on the timeline to select a range to zoom into', () => { forceSelect = !forceSelect; selBtn.classList.toggle('on', forceSelect); selBtn.setAttribute('aria-pressed', forceSelect); });
  selBtn.setAttribute('aria-pressed', 'false');
  zbar.prepend(
    grp(mk('zin', '+', 'Zoom in', () => zoomAt(0.5, 0.5)), mk('zout', '−', 'Zoom out', () => zoomAt(0.5, 2))),
    grp(mk('pl', '◀', 'Pan earlier', () => setView(view.a - span() / 2, view.b - span() / 2)), mk('pr', '▶', 'Pan later', () => setView(view.a + span() / 2, view.b + span() / 2))),
    grp(mk('reset', 'Reset', 'Show the whole day', () => setView(0, DAY))),
    grp(...presetsGrp),
    grp(selBtn));

  // ---- pointer / wheel handling (mouse, touch and pen via Pointer Events) ---------------------
  const pts = new Map();
  let g = null, suppress = false, tipTimer = 0;
  const tlBox = () => { const t = tls[0] || axisTop; const r = t.getBoundingClientRect(); return { left: r.left + t.clientLeft, width: t.clientWidth || 1 }; };
  const fracAt = (x) => { const b = tlBox(); return Math.min(1, Math.max(0, (x - b.left) / b.width)); };
  const timeAt = (x) => view.a + fracAt(x) * span();
  const inTl = (t) => t instanceof Element && t.closest('.tl, .axis');
  const dist = () => { const [p, q] = [...pts.values()]; return Math.hypot(p.x - q.x, p.y - q.y) || 1; };
  const midX = () => { const [p, q] = [...pts.values()]; return (p.x + q.x) / 2; };

  function drawSel(x0, x1) {
    const b = tlBox(), l = Math.min(x0, x1) - b.left, w = Math.abs(x1 - x0);
    for (const s of sels) { s.hidden = false; s.style.left = Math.max(0, l) + 'px'; s.style.width = Math.min(w, b.width - Math.max(0, l)) + 'px'; }
  }
  const hideSel = () => sels.forEach(s => { s.hidden = true; });

  tlwrap.addEventListener('pointerdown', (e) => {
    if (!inTl(e.target) || (e.pointerType === 'mouse' && e.button !== 0)) return;
    hideTip();
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 1) {
      suppress = false;
      g = { kind: 'pending', x0: e.clientX, y0: e.clientY, shift: e.shiftKey, a: view.a, b: view.b, touch: e.pointerType !== 'mouse', id: e.pointerId };
    } else if (pts.size === 2) {
      suppress = true; hideSel();
      const mx = midX();
      g = { kind: 'pinch', d0: dist(), a: view.a, b: view.b, f0: fracAt(mx), t0: timeAt(mx) };
      try { tlwrap.setPointerCapture(e.pointerId); } catch (_) {}
    }
  });
  tlwrap.addEventListener('pointermove', (e) => {
    if (pts.has(e.pointerId)) pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (!g) return;
    if (g.kind === 'pending' && e.pointerId === g.id) {
      const dx = e.clientX - g.x0;
      if (Math.abs(dx) < (g.touch ? 8 : 5) && Math.abs(e.clientY - g.y0) < 12) return;
      const zoomed = span() < DAY - 0.5;
      g.kind = (g.shift || forceSelect || !zoomed) ? 'select' : 'pan';
      suppress = true; hideTip();
      try { tlwrap.setPointerCapture(e.pointerId); } catch (_) {}
    }
    if (g.kind === 'pan' && e.pointerId === g.id) {
      const dt = -(e.clientX - g.x0) / tlBox().width * (g.b - g.a);
      const sp = g.b - g.a, a = Math.min(Math.max(0, g.a + dt), DAY - sp);
      view = { a, b: a + sp }; schedule();
    } else if (g.kind === 'select' && e.pointerId === g.id) {
      drawSel(g.x0, e.clientX);
    } else if (g.kind === 'pinch' && pts.size >= 2) {
      const sp0 = g.b - g.a, sp = Math.min(DAY, Math.max(MIN_SPAN, sp0 * g.d0 / dist()));
      const f = fracAt(midX()), a = Math.min(Math.max(0, g.t0 - f * sp), DAY - sp);
      view = { a, b: a + sp }; schedule();
    }
  });
  function endPointer(e, cancelled) {
    if (!pts.has(e.pointerId)) return;
    const wasTouch = g && g.touch;
    if (g && g.kind === 'select' && e.pointerId === g.id && !cancelled && Math.abs(e.clientX - g.x0) >= 8) {
      const t1 = timeAt(g.x0), t2 = timeAt(e.clientX);
      setView(Math.min(t1, t2), Math.max(t1, t2));
    }
    hideSel();
    pts.delete(e.pointerId);
    try { tlwrap.releasePointerCapture(e.pointerId); } catch (_) {}
    if (pts.size === 0) {
      // a plain tap on a touch screen: show the clip tooltip (the click that follows still plays it)
      if (g && g.kind === 'pending' && wasTouch && !cancelled) { const c = e.target instanceof Element && e.target.closest('.clip'); if (c) showTip(c, e.clientX, e.clientY, true); }
      g = null;
    } else {
      g = { kind: 'none' };  // a finger is still down after a pinch: ignore it until it lifts
    }
  }
  tlwrap.addEventListener('pointerup', (e) => endPointer(e, false));
  tlwrap.addEventListener('pointercancel', (e) => endPointer(e, true));
  tlwrap.addEventListener('wheel', (e) => {
    if (!inTl(e.target)) return;
    e.preventDefault();
    const k = e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 400 : 1;
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      const d = (e.deltaX || e.deltaY) * k / tlBox().width * span();
      setView(view.a + d, view.b + d);
    } else {
      zoomAt(fracAt(e.clientX), Math.exp(e.deltaY * k * 0.0015));
    }
  }, { passive: false });
  // a drag that moved must not also play the clip it started on
  tlwrap.addEventListener('click', (e) => { if (suppress) { e.stopPropagation(); e.preventDefault(); suppress = false; } }, true);

  // ---- tooltip (textContent only: clip data comes from the DVR) ----------------------
  function tipText(c) {
    const r = c._rec, clip = r.clip;
    return r.ch + ' · ' + r.name + '\n' + hms(clip.start) + ' – ' + hms(clip.end) + ' · ' + clip.duration_s + ' s · ' + fmtSize(clip.size);
  }
  function showTip(c, x, y, autoHide) {
    clearTimeout(tipTimer);
    tip.textContent = tipText(c); tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(4, Math.min(x + 12, innerWidth - w - 4)) + 'px';
    tip.style.top = (y + 18 + h > innerHeight ? Math.max(4, y - h - 12) : y + 18) + 'px';
    if (autoHide) tipTimer = setTimeout(hideTip, 2500);
  }
  function hideTip() { clearTimeout(tipTimer); tip.hidden = true; }
  tlwrap.addEventListener('pointerover', (e) => { if (e.pointerType === 'mouse' && !g) { const c = e.target instanceof Element && e.target.closest('.clip'); if (c) showTip(c, e.clientX, e.clientY); } });
  tlwrap.addEventListener('pointermove', (e) => { if (e.pointerType === 'mouse' && !tip.hidden && !g) { const c = e.target instanceof Element && e.target.closest('.clip'); if (c) showTip(c, e.clientX, e.clientY); else hideTip(); } });
  tlwrap.addEventListener('pointerout', (e) => { if (e.pointerType === 'mouse') hideTip(); });
  tlwrap.addEventListener('focusin', (e) => { const c = e.target instanceof Element && e.target.closest('.clip'); if (c) { const r = c.getBoundingClientRect(); showTip(c, r.left, r.bottom - 12); } });
  tlwrap.addEventListener('focusout', hideTip);
  new ResizeObserver(() => schedule()).observe(tlwrap);

  // ---- load a day -----------------------------------------------------------------------
  async function load() {
    note(''); rows.textContent = 'Loading…'; summary.textContent = ''; clipsBox.textContent = '';
    recs = []; tls = []; sels = []; hideTip();
    let doc;
    try {
      const r = await fetch('/api/recordings?date=' + dayEl.value);
      if (!r.ok) { rows.textContent = ''; note(r.status === 502 ? 'The DVR did not answer the search.' : 'Search failed (HTTP ' + r.status + ')', true); return; }
      doc = await r.json();
    } catch (e) { rows.textContent = ''; note('Search failed', true); return; }
    rows.textContent = '';
    let total = 0;
    const all = [];
    for (const c of doc.channels) {
      total += c.clips.length;
      const row = document.createElement('div'); row.className = 'row'; row.dataset.channel = c.channel;
      const h = document.createElement('h3');
      const a = document.createElement('a'); a.href = '/camera/' + Number(c.channel); a.style.color = 'inherit'; a.textContent = c.channel + ' · ' + c.name;
      const cnt = document.createElement('span'); cnt.textContent = c.clips.length + ' clip' + (c.clips.length === 1 ? '' : 's');
      h.append(a, cnt); row.append(h);
      const tl = document.createElement('div'); tl.className = 'tl';
      for (const clip of c.clips) {
        const s = secs(clip.start);
        const e = clip.end.slice(0, 10) > clip.start.slice(0, 10) ? DAY : Math.max(secs(clip.end), s + 1);
        const el = document.createElement('div'); el.className = 'clip';
        el.tabIndex = 0; el.setAttribute('role', 'button');
        el.setAttribute('aria-label', 'Channel ' + c.channel + ' ' + hms(clip.start) + ' to ' + hms(clip.end));
        const rec = { ch: c.channel, name: c.name, clip, s, e, el, els: [el] };
        el._rec = rec;
        el.onclick = () => playClip(rec);
        el.onkeydown = (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); playClip(rec); } };
        recs.push(rec); all.push(rec);
        tl.append(el);
      }
      const sel = document.createElement('div'); sel.className = 'sel'; sel.hidden = true; tl.append(sel);
      tls.push(tl); sels.push(sel);
      row.append(tl);
      rows.append(row);
    }
    summary.textContent = total + ' clips';
    layout();
    if (total) {
      const det = document.createElement('details'); det.open = true;
      const sm = document.createElement('summary'); sm.textContent = 'All clips'; det.append(sm);
      const tb = document.createElement('table');
      const head = document.createElement('thead'), hr = document.createElement('tr');
      for (const [t, cls] of [['Channel', ''], ['Start', ''], ['End', ''], ['Length', 'n'], ['Size', 'n']]) { const th = document.createElement('th'); th.textContent = t; if (cls) th.className = cls; hr.append(th); }
      head.append(hr); tb.append(head);
      const body = document.createElement('tbody');
      all.sort((x, y) => (x.clip.start < y.clip.start ? -1 : 1));
      for (const rec of all) {
        const { clip } = rec;
        const tr = document.createElement('tr'); tr.className = 'clipRow';
        for (const [txt, cls] of [[rec.ch + ' ' + rec.name, ''], [hms(clip.start), ''], [hms(clip.end), ''], [clip.duration_s + ' s', 'n'], [fmtSize(clip.size), 'n']]) {
          const td = document.createElement('td'); td.textContent = txt; if (cls) td.className = cls; tr.append(td);
        }
        rec.els.push(tr);
        tr.onclick = () => { ensureVisible(rec.s, rec.e); playClip(rec); };
        body.append(tr);
      }
      tb.append(body); det.append(tb); clipsBox.append(det);
    }
  }

  const today = info.today || iso(new Date());
  dayEl.value = today; dayEl.max = today;
  const shift = (n) => { const d = new Date(dayEl.value + 'T12:00:00'); d.setDate(d.getDate() + n); const v = iso(d); if (v <= dayEl.max) { dayEl.value = v; load(); } };
  $('prev').onclick = () => shift(-1);
  $('next').onclick = () => shift(1);
  dayEl.onchange = load;
  load();
})();
