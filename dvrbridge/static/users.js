(async () => {
  const $ = (id) => document.getElementById(id);
  const say = (text, ok) => { $('msg').textContent = text || ''; $('msg').className = ok ? 'ok' : 'err'; };
  const when = (ts) => ts ? new Date(ts * 1000).toLocaleString() : '-';
  const el = (tag, text, cls) => { const e = document.createElement(tag); if (text) e.textContent = text; if (cls) e.className = cls; return e; };
  const me = await loadNav('users');
  if (me.role !== 'admin') return;

  async function patch(u, body, done) {
    const r = await api('PATCH', '/api/users/' + u.id, body);
    say(r.ok ? done : (r.data.detail || 'That did not work.'), r.ok);
    await load();
  }

  // Where people are added when logins come from a proxy or an identity provider.
  function externalHint(d) {
    const where = d.auth_mode === 'header' ? 'your reverse proxy' : 'your identity provider';
    const group = d.role_groups[d.auth_mode];
    let text = '';
    if (!d.local_login) text += 'Local accounts are off, so people are added in ' + where + ', not here: give them access to this app there, and they appear in this list after their first visit. ';
    else text += 'People who sign in through ' + where + ' appear in this list after their first visit. ';
    text += group ? 'Members of the group "' + group + '" are admins and everyone else is a viewer; change roles by changing their groups there.'
                  : 'Set their role here once they have signed in.';
    return text;
  }

  function row(u, localLogin, roleGroups) {
    const tr = document.createElement('tr');
    const name = el('td', u.username);
    if (u.source !== 'local') name.appendChild(el('span', u.source === 'oidc' ? 'SSO' : 'proxy', 'tag'));
    if (u.disabled) name.appendChild(el('span', 'disabled', 'tag'));
    if (u.username === me.username) name.appendChild(el('span', 'you', 'tag'));
    tr.appendChild(name);

    const roleTd = document.createElement('td');
    const sel = document.createElement('select');
    for (const r of ['viewer', 'admin']) { const o = el('option', r[0].toUpperCase() + r.slice(1)); o.value = r; sel.appendChild(o); }
    sel.value = u.role;
    const group = roleGroups[u.source];
    if (group) { sel.disabled = true; sel.title = 'Set by membership of "' + group + '" at the identity provider'; }
    sel.addEventListener('change', () => patch(u, { role: sel.value }, u.username + ' is now ' + sel.value + '.'));
    roleTd.appendChild(sel); tr.appendChild(roleTd);

    tr.appendChild(el('td', u.source === 'local' ? (u.totp_enabled ? 'On' : 'Off') : '-'));
    tr.appendChild(el('td', when(u.last_login_at)));

    const act = document.createElement('td');
    const btn = (label, fn) => { const b = el('button', label); b.type = 'button'; b.addEventListener('click', fn); act.appendChild(b); act.appendChild(document.createTextNode(' ')); };
    btn(u.disabled ? 'Enable' : 'Disable', () => patch(u, { disabled: !u.disabled }, u.username + (u.disabled ? ' enabled.' : ' disabled and signed out.')));
    if (u.source === 'local' && localLogin) {
      btn('Set password', () => {
        const pw = prompt('New password for ' + u.username + ' (at least 10 characters):');
        if (pw) patch(u, { password: pw }, 'Password changed; ' + u.username + ' was signed out.');
      });
      if (u.totp_enabled) btn('Reset two-factor', () => {
        if (confirm('Remove ' + u.username + "'s authenticator? They can set up a new one after logging in.")) {
          patch(u, { reset_totp: true }, 'Two-factor reset for ' + u.username + '.');
        }
      });
    }
    if (u.username !== me.username) btn('Delete', async () => {
      if (!confirm('Delete ' + u.username + '?')) return;
      const r = await api('DELETE', '/api/users/' + u.id);
      say(r.ok ? u.username + ' deleted.' : (r.data.detail || 'Could not delete.'), r.ok);
      await load();
    });
    tr.appendChild(act);
    return tr;
  }

  async function load() {
    const r = await api('GET', '/api/users');
    if (!r.ok) { say(r.data.detail || 'Could not load users.'); return; }
    $('addSection').hidden = !r.data.local_login;
    const external = r.data.auth_mode === 'header' || r.data.auth_mode === 'oidc';
    $('extHint').hidden = !external;
    if (external) $('extHint').textContent = externalHint(r.data);
    const body = $('users'); body.textContent = '';
    for (const u of r.data.users) body.appendChild(row(u, r.data.local_login, r.data.role_groups || {}));
    const a = await api('GET', '/api/audit?limit=100');
    const ab = $('audit'); ab.textContent = '';
    for (const e of (a.data.events || [])) {
      const tr = document.createElement('tr');
      for (const v of [when(e.ts), e.username || '-', e.action.replace(/_/g, ' '), e.detail || '', e.ip || '']) tr.appendChild(el('td', v));
      ab.appendChild(tr);
    }
  }

  $('addForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const r = await api('POST', '/api/users', { username: $('newName').value, password: $('newPw').value, role: $('newRole').value });
    say(r.ok ? r.data.username + ' added.' : (r.data.detail || 'Could not add the user.'), r.ok);
    if (r.ok) $('addForm').reset();
    await load();
  });
  await load();
})();
