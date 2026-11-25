(async () => {
  const ch = parseInt(location.pathname.split('/').pop(), 10);
  const info = await loadHeader('live');
  const c = info.channels.find((x) => x.channel === ch);
  document.getElementById('title').textContent = ch + ' · ' + (c ? c.name : 'Channel ' + ch) + ' (main stream)';
  document.title = 'Camera ' + ch + ' - DVR Bridge';
  const st = document.getElementById('state');
  startMse(document.getElementById('v'), '/ws/live/' + ch + '/main', (s) => { st.textContent = s; st.hidden = s === 'live'; });
})();
