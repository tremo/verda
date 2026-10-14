(() => {
  const app = document.getElementById('app');
  const el = (tag, text, cls, parent) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; if (parent) parent.appendChild(e); return e; };
  let csrf = '';
  const dirty = new Map();
  const send = async (url, method, body) => {
    const r = await fetch(url, {method, credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-Verda-CSRF': csrf}, body: body ? JSON.stringify(body) : undefined});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || 'İşlem yapılamadı.');
    return data;
  };
  const when = t => new Date(t * 1000).toLocaleString('tr-TR');
  const render = d => {
    app.textContent = ''; csrf = d.csrf_token; dirty.clear();
    const head = el('section', null, null, app);
    el('h1', 'Ayarlar', null, head);
    el('p', 'Burada değiştirdiğin sayılar servisleri yeniden başlatmadan geçerli olur. Agent sınırları yalnız yeni başlayan görevlere uygulanır. Her değişiklik altta kayıt altında tutulur.', 'muted', head);
    const msg = el('p', '', 'muted', head);
    const bar = el('div', null, 'row', head);
    const save = el('button', 'Değişiklikleri kaydet', null, bar); save.disabled = true;
    const mark = () => { save.disabled = dirty.size === 0; save.textContent = dirty.size ? 'Değişiklikleri kaydet (' + dirty.size + ')' : 'Değişiklikleri kaydet'; };
    save.onclick = async () => {
      try { await send('/control/private/settings', 'PUT', {changes: [...dirty].map(([key, value]) => ({key, value}))}); location.reload(); }
      catch (e) { msg.textContent = e.message; msg.className = 'bad'; }
    };
    // timers
    const timers = el('section', null, null, app); el('h2', 'Zamanlayıcılar', null, timers);
    d.triggers.filter(t => !t.sample).forEach(t => {
      const row = el('div', null, 'task', timers);
      el('b', t.key, null, row); el('span', ' → ' + t.agent + ' · ' + t.objective, 'muted', row);
      const line = el('div', null, 'row', row); line.style.margin = '6px 0';
      el('span', 'Her', null, line);
      const mins = el('input', null, null, line); mins.type = 'number'; mins.min = 1; mins.max = 10080; mins.step = 'any'; mins.value = t.interval_minutes; mins.style.width = '90px';
      mins.oninput = () => { const v = parseFloat(mins.value); if (v === t.interval_minutes) dirty.delete('trigger.' + t.key + '.interval_minutes'); else dirty.set('trigger.' + t.key + '.interval_minutes', v); mark(); };
      el('span', 'dakikada bir', null, line);
      const lab = el('label', null, null, line); const on = el('input', null, null, lab); on.type = 'checkbox'; on.checked = t.enabled; el('span', ' Etkin', null, lab);
      on.onchange = () => { if (on.checked === t.enabled) dirty.delete('trigger.' + t.key + '.enabled'); else dirty.set('trigger.' + t.key + '.enabled', on.checked); mark(); };
      const run = el('button', 'Şimdi çalıştır', null, line);
      run.onclick = async () => { try { await send('/control/private/triggers/' + encodeURIComponent(t.key) + '/run', 'POST'); msg.textContent = t.key + ' başlatıldı. "Çalışma durumu" sayfasından izleyebilirsin.'; msg.className = 'ok'; } catch (e) { msg.textContent = e.message; msg.className = 'bad'; } };
      el('span', 'Sonraki çalışma: ' + when(t.next_at), 'muted', line);
    });
    // numbers
    const groups = [...new Set(d.entries.map(e => e.group))];
    groups.forEach(g => {
      const box = el('section', null, null, app); el('h2', g, null, box);
      d.entries.filter(e => e.group === g).forEach(e => {
        const row = el('div', null, 'task', box);
        el('div', e.label, null, row);
        const line = el('div', null, 'row', row); line.style.margin = '4px 0';
        if (e.type === 'choice') {
          const select = el('select', null, null, line); select.setAttribute('aria-label', e.label);
          e.choices.forEach(([v, text]) => { const o = el('option', text, null, select); o.value = v; o.selected = v === e.value; });
          el('span', '(varsayılan: ' + (e.choices.find(c => c[0] === e.default) || [, e.default])[1] + ')', 'muted', line);
          select.onchange = () => {
            if (select.value === 'live' && !confirm('Canlı mod: sistem satıcılara GERÇEK mesaj gönderir (08:30–21:00, her çalışmada en çok bir). Önce Deneme modunda taslakları incelediysen devam et. Canlıya alınsın mı?')) { select.value = e.value; dirty.delete(e.key); mark(); return; }
            if (select.value === e.value) dirty.delete(e.key); else dirty.set(e.key, select.value); mark();
          };
          el('div', e.help, 'muted', row).style.fontSize = '12px';
          return;
        }
        const input = el('input', null, null, line); input.type = 'number'; input.min = e.min; input.max = e.max; input.step = e.type === 'int' ? 1 : 'any'; input.value = e.value; input.style.width = '110px';
        el('span', e.unit, null, line);
        el('span', '(varsayılan ' + e.default + ', aralık ' + e.min + '–' + e.max + ')', 'muted', line);
        input.oninput = () => { const v = parseFloat(input.value); if (Number.isNaN(v) || v === e.value) dirty.delete(e.key); else dirty.set(e.key, v); mark(); };
        if (e.changed) { const reset = el('button', 'Varsayılana dön', null, line); reset.onclick = () => { dirty.set(e.key, null); save.click(); }; }
        el('div', e.help, 'muted', row).style.fontSize = '12px';
      });
    });
    const log = el('section', null, null, app); el('h2', 'Son değişiklikler', null, log);
    if (!d.log.length) el('p', 'Henüz değişiklik yapılmadı.', 'muted', log);
    d.log.forEach(l => el('div', when(l.at) + ' · ' + l.key + ' : ' + (l.old ?? '—') + ' → ' + (l.new ?? 'varsayılan'), 'muted', log).style.fontSize = '13px');
  };
  fetch('/control/private/settings', {credentials: 'same-origin'}).then(r => {
    if (r.status === 401) throw new Error('Yerel görüntüleyici bağlantısını (verda serve çıktısı) aynı tarayıcıda aç.');
    if (!r.ok) throw new Error('Ayarlar alınamadı.');
    return r.json();
  }).then(render).catch(e => { app.textContent = ''; el('p', e.message, 'bad', el('section', null, null, app)); });
})();
