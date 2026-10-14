(() => {
  const app = document.getElementById('app');
  const el = (tag, text, cls, parent) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; if (parent) parent.appendChild(e); return e; };
  const NS = 'http://www.w3.org/2000/svg';
  const icon = (paths) => { const s = document.createElementNS(NS, 'svg'); s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true'); paths.forEach(d => { const p = document.createElementNS(NS, 'path'); p.setAttribute('d', d); s.appendChild(p); }); return s; };
  const TRAY = ['M3 13l3-8h12l3 8', 'M3 13v6h18v-6', 'M3 13h5l1 2h6l1-2h5'];
  const ALERT = ['M12 3l10 18H2L12 3z', 'M12 10v5', 'M12 18v.5'];
  const STATE = {working: 'Çalışıyor', waiting: 'Bekliyor', idle: 'Boşta'};
  const ago = t => { const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? Math.round(s) + ' sn' : s < 5400 ? Math.round(s / 60) + ' dk' : Math.round(s / 3600) + ' sa'; };
  const open = new Set();
  const render = d => {
    app.textContent = '';
    const head = el('section', null, null, app);
    el('h1', 'Operatörler ne yapıyor', null, head);
    el('p', (d.workers.agent_worker_alive ? 'Görev işçisi canlı.' : 'Görev işçisi görünmüyor: işler çalışmaz.') + ' Sayılar o agent’ın kuyruğundaki iş sayısıdır; ayrıntı için kartı aç. Sayfa 5 sn’de bir yenilenir.', d.workers.agent_worker_alive ? 'muted' : 'bad', head);
    const grid = el('div', null, 'cards', app);
    d.agents.forEach(a => {
      const card = el('div', null, 'card ' + a.state, grid);
      el('h3', a.label, null, card);
      const st = el('div', null, 'status ' + a.state, card); el('span', null, 'dot', st); el('b', STATE[a.state], null, st); el('span', '· ' + a.note, 'muted', st);
      const counts = el('div', null, 'counts', card);
      const q = el('div', null, 'bucket', counts); q.title = 'Kuyruktaki iş'; q.appendChild(icon(TRAY)); el('b', String(a.queue), null, q); el('span', 'kuyrukta', 'muted', q);
      if (a.needs_you) { const y = el('div', null, 'bucket you', counts); y.title = 'Senden bir şey bekleyen ya da duran iş'; y.appendChild(icon(ALERT)); el('b', String(a.needs_you), null, y); el('span', 'ilgi bekliyor', null, y); }
      const det = el('details', null, null, card); det.open = open.has(a.key);
      det.addEventListener('toggle', () => { det.open ? open.add(a.key) : open.delete(a.key); });
      el('summary', a.tasks.length ? 'Kuyruğu göster (' + a.tasks.length + ')' : 'Kuyruk boş', null, det);
      a.tasks.forEach(t => {
        const row = el('div', null, 'task', det);
        el('div', t.objective, null, row);
        const meta = el('div', null, 'meta', row);
        meta.textContent = t.state_label + ' · ' + ago(t.since) + ' önce · kaynak: ' + (t.source || '—') + (t.listing ? ' · ilan ' + t.listing : '') + (t.turns ? ' · ' + t.turns + ' tur' : '') + (t.reason ? ' · ' + t.reason : '');
      });
    });
  };
  const load = () => fetch('/control/private/operators', {credentials: 'same-origin'}).then(r => {
    if (r.status === 401) throw new Error('Yerel görüntüleyici bağlantısını (verda serve çıktısı) aynı tarayıcıda aç.');
    if (!r.ok) throw new Error('Durum alınamadı.');
    return r.json();
  }).then(render).catch(e => { app.textContent = ''; el('p', e.message, 'bad', el('section', null, null, app)); });
  load(); setInterval(load, 5000);
})();
