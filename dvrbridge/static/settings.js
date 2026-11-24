(async () => {
  await loadHeader('settings');
  const list = document.getElementById('list'), msg = document.getElementById('msg');
  const NAME_RE = /^[A-Za-z0-9 \-_.'()&]+$/;
  const note = (t) => { msg.hidden = !t; msg.textContent = t || ''; };
  const why = { dvr_refused: 'The DVR refused the change', dvr_unavailable: 'The DVR did not answer', dvr_login_refused: 'The DVR refused DVR Bridge’s login' };

  function check(name, max) {
    const n = name.trim();
    if (!n) return 'The name cannot be empty.';
    if (n.length > max) return 'At most ' + max + ' characters.';
    if (!NAME_RE.test(n)) return 'Only letters, digits, spaces and - _ . \' ( ) & are allowed.';
    return '';
  }

  function addRow(c) {
    const form = document.createElement('form'); form.className = 'srow'; form.dataset.channel = c.channel;
    const lab = document.createElement('label'); lab.textContent = 'Channel ' + c.channel;
    const id = 'name' + c.channel; lab.htmlFor = id;
    const inp = document.createElement('input'); inp.id = id; inp.type = 'text'; inp.value = c.name;
    inp.maxLength = c.max_length; inp.autocomplete = 'off'; inp.spellcheck = false; inp.setAttribute('autocapitalize', 'off');
    const btn = document.createElement('button'); btn.type = 'submit'; btn.textContent = 'Save'; btn.disabled = true;
    const st = document.createElement('span'); st.className = 'sstate'; st.setAttribute('role', 'status');
    let saved = c.name;
    const status = (t, cls) => { st.textContent = t; st.className = 'sstate ' + (cls || ''); };
    inp.addEventListener('input', () => { btn.disabled = inp.value.trim() === saved; status(''); });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const bad = check(inp.value, c.max_length);
      if (bad) { status(bad, 'bad'); return; }
      btn.disabled = true; inp.disabled = true; status('Saving…');
      try {
        const r = await fetch('/api/settings/channels/' + c.channel, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: inp.value.trim() }) });
        let j = {};
        try { j = await r.json(); } catch (e) {}
        if (r.ok) { saved = j.name; inp.value = j.name; status('Saved', 'ok'); }
        else {
          status((j.detail || why[j.error] || 'Failed (HTTP ' + r.status + ')'), 'bad');
        }
      } catch (e) { status('Could not reach DVR Bridge', 'bad'); }
      inp.disabled = false; btn.disabled = inp.value.trim() === saved;
    });
    form.append(lab, inp, btn, st);
    list.append(form);
  }

  try {
    const r = await fetch('/api/settings/channels');
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { list.textContent = ''; note((j.detail || 'Could not read the camera names') + ' (HTTP ' + r.status + ')'); msg.classList.add('err'); return; }
    list.textContent = '';
    document.getElementById('maxlen').textContent = 'up to ' + j.max_length;
    for (const c of j.channels) addRow(c);
  } catch (e) { list.textContent = ''; note('Could not reach DVR Bridge'); }
})();
