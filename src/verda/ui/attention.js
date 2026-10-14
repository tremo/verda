(() => {
  const app = document.getElementById('app');
  const el = (tag, text, cls, parent) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; if (parent) parent.appendChild(e); return e; };
  const GROUPS = [['action', 'İlgi bekleyenler', 'bad'], ['waiting', 'Beklemede', 'warn'], ['info', 'Bilgi', 'muted']];
  let csrf = '';
  const ack = async key => {
    await fetch('/control/private/attention/ack', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Verda-CSRF': csrf}, body: JSON.stringify({key})});
    load(true);
  };
  const render = d => {
    app.textContent = '';
    csrf = d.csrf_token;
    const head = el('section', null, null, app);
    el('h1', d.count ? d.count + ' konu ilgi bekliyor' : 'İlgi bekleyen konu yok', null, head);
    el('p', 'Durmuş görevler, tekrar tekrar başarısız olan adımlar, kaynak engelleri ve cevap bekleyen sorular burada toplanır. Sebep ortadan kalkınca konu kendiliğinden kaybolur; "Gördüm" bir konuyu, sebebi değişene kadar gizler.', 'muted', head);
    GROUPS.forEach(([sev, title, tone]) => {
      const items = d.items.filter(i => i.severity === sev);
      if (!items.length) return;
      const box = el('section', null, null, app);
      el('h2', title + ' · ' + items.length, null, box);
      items.forEach(i => {
        const row = el('div', null, 'row', box); row.style.cssText = 'display:block;border-top:1px solid var(--line);padding:10px 0';
        el('div', i.title, tone === 'muted' ? '' : tone, row).style.fontWeight = '600';
        el('div', i.detail, 'muted', row);
        const links = el('div', null, 'links', row);
        (i.listings || []).slice(0, 12).forEach(id => { const a = el('a', id, null, links); a.href = '/control/listing/' + encodeURIComponent(id); });
        if ((i.listings || []).length > 12) el('span', '+' + (i.listings.length - 12) + ' ilan daha', 'muted', links);
        if (i.ackable) { const b = el('button', 'Gördüm', null, row); b.onclick = () => ack(i.key); b.style.cssText = 'margin-top:6px'; }
        el('div', new Date(i.since * 1000).toLocaleString('tr-TR'), 'muted', row).style.fontSize = '12px';
      });
    });
  };
  const load = fresh => fetch('/control/private/attention' + (fresh ? '?fresh=true' : ''), {credentials: 'same-origin'}).then(r => {
    if (r.status === 401) throw new Error('Yerel görüntüleyici bağlantısını (verda serve çıktısı) aynı tarayıcıda aç.');
    if (!r.ok) throw new Error('Bekleyen konular alınamadı.');
    return r.json();
  }).then(render).catch(e => { app.textContent = ''; el('p', e.message, 'bad', el('section', null, null, app)); });
  load(false);
})();
