(async () => {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const next = params.get('next') || '/';
  const showError = (text) => { $('error').textContent = text; $('error').hidden = !text; };
  if (params.get('error')) showError(params.get('error'));

  const post = async (url, body) => {
    const r = await fetch(url, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let data = {};
    try { data = await r.json(); } catch (e) { /* no body */ }
    return { ok: r.ok, data };
  };

  let cfg = {};
  try { cfg = await (await fetch('/api/auth/config')).json(); } catch (e) { showError('The server did not answer.'); }
  if (cfg.setup_required) $('setup').hidden = false;
  if (cfg.local_login) $('pw').hidden = false;
  if (cfg.oidc) {
    $('sso').hidden = false;
    $('or').hidden = !cfg.local_login;
    $('ssoButton').textContent = cfg.oidc;
    $('ssoButton').href = '/auth/oidc/start?next=' + encodeURIComponent(next);
  }
  if (cfg.mode === 'header' && !cfg.local_login) {
    showError('This server signs you in through its reverse proxy. Open it through the proxy\'s address.');
  }
  $('username').focus();

  $('pw').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    showError('');
    const r = await post('/api/auth/login', { username: $('username').value, password: $('password').value, next });
    if (!r.ok) { showError(r.data.detail || 'Login failed.'); $('password').select(); return; }
    if (r.data.mfa) { $('pw').hidden = true; $('sso').hidden = true; $('mfa').hidden = false; $('code').focus(); return; }
    location.href = r.data.next || '/';
  });

  $('mfa').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    showError('');
    const r = await post('/api/auth/totp', { code: $('code').value, next });
    if (!r.ok) { showError(r.data.detail || 'That code did not work.'); $('code').select(); return; }
    location.href = r.data.next || '/';
  });
})();
