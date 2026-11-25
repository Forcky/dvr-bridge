(async () => {
  const info = await loadHeader('live');
  const grid = document.getElementById('grid');
  for (const c of info.channels) {
    const t = document.createElement('div'); t.className = 'tile'; t.dataset.channel = String(Number(c.channel));
    t.innerHTML = '<video muted playsinline autoplay></video><div class="label"><span></span><span class="state">connecting</span></div><a class="cover" href="/camera/' + Number(c.channel) + '" title="Open main stream"></a>';
    t.querySelector('.label span').textContent = c.channel + ' · ' + c.name;
    grid.appendChild(t);
    const st = t.querySelector('.state');
    startMse(t.querySelector('video'), '/ws/live/' + c.channel + '/sub', (s) => { st.textContent = s === 'live' ? '' : s; });
  }
})();
