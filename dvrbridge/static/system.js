(async () => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, attrs, text) => {
    const e = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v !== undefined && v !== null) e.setAttribute(k, v); });
    if (text) e.textContent = text;
    return e;
  };
  const SECTIONS = { dvr: 'DVR connection', playback: 'Playback', detection: 'Detections (Frigate)', monitoring: 'Monitoring',
    server: 'Server', auth: 'Logins' };
  const LABELS = {
    'dvr.host': 'DVR address', 'dvr.username': 'DVR username', 'dvr.password': 'DVR password',
    'dvr.http_port': 'Web API port', 'dvr.media_port': 'Client port (recordings)', 'dvr.rtsp_port': 'RTSP port (live)',
    'dvr.channels': 'Number of cameras', 'dvr.timezone': 'DVR time zone',
    'playback.max_concurrent': 'Clips at once', 'playback.max_range_hours': 'Longest clip (hours)',
    'monitoring.poll_interval': 'Status check interval (s)', 'monitoring.metrics_enabled': 'Prometheus metrics',
    'monitoring.metrics_token': 'Metrics token', 'server.public_url': 'Public URL',
    'auth.mode': 'Login method', 'auth.session_hours': 'Login lasts (hours)', 'auth.local_login': 'Password logins',
    'auth.totp': 'Authenticator codes', 'auth.oidc_issuer': 'Issuer URL', 'auth.oidc_client_id': 'Client ID',
    'auth.oidc_client_secret': 'Client secret', 'auth.oidc_scopes': 'Scopes', 'auth.oidc_username_claim': 'Username claim',
    'auth.oidc_groups_claim': 'Groups claim', 'auth.oidc_admin_group': 'Admin group', 'auth.oidc_allowed_group': 'Allowed group',
    'auth.oidc_button_label': 'Button text', 'auth.header_user': 'Username header', 'auth.header_groups': 'Groups header',
    'auth.header_admin_group': 'Admin group', 'auth.header_secret': 'Shared secret', 'auth.header_secret_name': 'Secret header',
    'auth.trusted_proxies': 'Trusted proxies',
    'detection.frigate_url': 'Frigate URL', 'detection.cameras': 'Camera mapping',
  };
  const me = await loadNav('settings');
  if (me.role !== 'admin') return;
  const r = await api('GET', '/api/settings/system');
  if (!r.ok) { $('saveMsg').textContent = r.data.detail || 'Could not load settings.'; return; }
  const wizard = !r.data.configured || new URLSearchParams(location.search).has('wizard');
  if (wizard) { $('wizard').hidden = false; if (!r.data.configured) $('tabs').hidden = true; }
  $('fileNote').textContent = 'Saved to ' + r.data.config_file + '. Settings marked "set by environment" can only be '
    + 'changed where DVR Bridge is started (e.g. compose.yaml).';

  const opts = r.data.options;
  const inputs = {};          // key -> {opt, input, row}
  const changed = new Map();  // key -> value (null = clear)

  function current(opt) {
    if (opt.secret) return '';
    return opt.value === null || opt.value === undefined ? '' : opt.value;
  }

  function control(opt) {
    let input;
    const id = 'o_' + opt.key.replace(/\W/g, '_');
    if (opt.kind === 'bool') {
      input = el('input', { type: 'checkbox', id });
      input.checked = !!opt.value;
    } else if (opt.choices.length) {
      input = el('select', { id });
      for (const c of opt.choices) { const o = el('option', { value: c }, c); input.appendChild(o); }
      input.value = String(opt.value);
    } else {
      input = el('input', { id, type: opt.secret ? 'password' : (opt.kind === 'text' ? 'text' : 'number'),
        autocomplete: opt.secret ? 'new-password' : 'off', spellcheck: 'false' });
      if (opt.kind === 'number' || opt.kind === 'int') input.step = opt.kind === 'int' ? '1' : 'any';
      input.value = current(opt);
      if (opt.secret) input.placeholder = opt.is_set ? 'set - leave blank to keep' : 'not set';
    }
    if (opt.locked) input.disabled = true;
    const mark = () => {
      const v = opt.kind === 'bool' ? input.checked : input.value;
      const same = opt.secret ? v === '' : (opt.kind === 'bool' ? v === !!opt.value : String(v) === String(current(opt)));
      if (same) changed.delete(opt.key); else changed.set(opt.key, v);
      row.classList.toggle('changed', !same);
      refresh();
    };
    input.addEventListener(opt.kind === 'bool' || opt.choices.length ? 'change' : 'input', mark);
    const row = el('div', { class: 'opt' });
    row.appendChild(el('label', { class: 'lbl', for: id }, LABELS[opt.key] || opt.key));
    const box = el('div');
    box.appendChild(input);
    if (opt.secret && opt.is_set && !opt.locked && opt.key !== 'dvr.password') {
      const clear = el('button', { type: 'button', class: 'link' }, 'Clear');
      clear.addEventListener('click', () => { input.value = ''; changed.set(opt.key, null); row.classList.add('changed'); refresh(); });
      box.appendChild(document.createTextNode(' ')); box.appendChild(clear);
    }
    row.appendChild(box);
    row.appendChild(el('div', { class: 'help' }, opt.help));
    if (opt.locked) row.appendChild(el('div', { class: 'lock' }, 'Set by environment variable ' + opt.env));
    inputs[opt.key] = { opt, input, row };
    return row;
  }

  for (const [sec, title] of Object.entries(SECTIONS)) {
    const list = opts.filter((o) => o.section === sec);
    if (!list.length) continue;
    const card = el('div', { class: 'card wide', id: 'sec_' + sec });
    card.appendChild(el('h3', {}, title));
    list.forEach((o) => card.appendChild(control(o)));
    if (wizard && sec !== 'dvr') card.hidden = true;
    $('sections').appendChild(card);
  }
  if (wizard) {
    const more = el('button', { type: 'button', class: 'link' }, 'Show all settings');
    more.addEventListener('click', () => { document.querySelectorAll('#sections .card').forEach((c) => { c.hidden = false; }); more.remove(); showAuthFields(); });
    $('sections').appendChild(more);
  }

  // Only show the OIDC / header fields for the login method that uses them.
  function showAuthFields() {
    const mode = inputs['auth.mode'] ? inputs['auth.mode'].input.value : 'local';
    for (const [key, { row }] of Object.entries(inputs)) {
      if (key.startsWith('auth.oidc_')) row.hidden = mode !== 'oidc';
      if (key.startsWith('auth.header_') || key === 'auth.trusted_proxies') row.hidden = mode !== 'header' && key !== 'auth.trusted_proxies';
    }
  }
  if (inputs['auth.mode']) inputs['auth.mode'].input.addEventListener('change', showAuthFields);
  showAuthFields();

  function refresh() {
    $('changes').textContent = changed.size ? changed.size + ' unsaved change' + (changed.size > 1 ? 's' : '') : '';
    $('authWarn').hidden = ![...changed.keys()].some((k) => k.startsWith('auth.'));
    $('save').textContent = wizard && !r.data.configured ? 'Save and connect' : 'Save';
  }
  refresh();

  // ---- connection test ----
  $('test').addEventListener('click', async () => {
    const v = (k) => (inputs[k] ? (inputs[k].opt.kind === 'bool' ? inputs[k].input.checked : inputs[k].input.value) : '');
    $('test').disabled = true; $('testState').textContent = 'Testing - this can take a few seconds...';
    const list = $('checks'); list.textContent = '';
    const t = await api('POST', '/api/settings/test-dvr', { host: v('dvr.host'), username: v('dvr.username'),
      password: v('dvr.password'), http_port: v('dvr.http_port'), media_port: v('dvr.media_port'), rtsp_port: v('dvr.rtsp_port') });
    $('test').disabled = false;
    if (!t.ok) { $('testState').textContent = t.data.detail || 'The test could not run.'; return; }
    for (const c of t.data.checks) list.appendChild(el('li', { class: c.ok ? 'ok' : 'err' }, c.check + ': ' + c.detail));
    $('testState').textContent = t.data.all_ok ? 'Everything works.' : t.data.ok ? 'Connected, but see the problems below.' : 'Not working yet.';
  });

  // ---- save ----
  $('save').addEventListener('click', async () => {
    if (!changed.size) { $('saveMsg').textContent = 'Nothing has changed.'; $('saveMsg').className = 'hint'; return; }
    const updates = {};
    for (const [k, v] of changed) updates[k] = v;
    $('save').disabled = true;
    const s = await api('PUT', '/api/settings/system', { updates });
    if (!s.ok) {
      $('save').disabled = false;
      $('saveMsg').className = 'err';
      $('saveMsg').textContent = (s.data.problems || [s.data.detail || 'Could not save.']).join(' ');
      return;
    }
    $('saveMsg').className = 'ok';
    $('saveMsg').textContent = s.data.detail;
    if (!s.data.reloading) { $('save').disabled = false; return; }
    // Wait for the new settings to be live, then continue.
    for (let i = 0; i < 40; i++) {
      await new Promise((res) => setTimeout(res, 750));
      try {
        const ping = await fetch('/api/me', { credentials: 'same-origin' });
        if (ping.status === 200 || ping.status === 401) {
          location.href = wizard && !r.data.configured ? '/' : '/settings/system';
          return;
        }
      } catch (e) { /* restarting */ }
    }
    $('saveMsg').className = 'err';
    $('saveMsg').textContent = 'DVR Bridge has not come back yet. Check the container log.';
  });
})();
