(async () => {
  const $ = (id) => document.getElementById(id);
  const showError = (text) => { $('error').textContent = text; $('error').hidden = !text; };
  try {
    const cfg = await (await fetch('/api/auth/config')).json();
    if (!cfg.setup_required) { location.href = '/login'; return; }
  } catch (e) { showError('The server did not answer.'); }
  $('code').focus();

  $('form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    showError('');
    if ($('password').value !== $('password2').value) { showError('The two passwords do not match.'); return; }
    const r = await fetch('/api/auth/setup', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ setup_code: $('code').value, username: $('username').value, password: $('password').value }) });
    let data = {};
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (!r.ok) { showError(data.detail || 'Setup failed.'); return; }
    location.href = data.next || '/';
  });
})();
