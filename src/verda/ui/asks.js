// The owner's word on each seller question: Sor (may be written to), Beklet (kept, not written to), Ele (dropped). Nothing is sent to a listing not marked Sor.
(() => {
  const {el, num, money, readableTitle, fetchJson, day} = Verda;
  const app = document.getElementById('app');
  const FILTERS = [['undecided', 'Karar bekleyen', r => !r.choice && r.text], ['ask', 'Sor', r => r.choice === 'ask'], ['hold', 'Beklet', r => r.choice === 'hold'],
    ['blocked', 'Şu an sorulamaz', r => !r.choice && !r.text], ['all', 'Hepsi', () => true]];
  const MODE = {off: 'Mesaj gönderme kapalı: onayladıkların gönderim açılınca sırayla sorulur.', dry_run: 'Deneme modu: onayladıkların için taslak hazırlanır, gönderilmez.', live: 'Canlı: onayladıkların 08:30–21:00 arasında, aralıklı olarak sorulur.'};
  const SENT = {confirmed: 'gönderildi', dry_run: 'deneme yapıldı', prepared: 'hazırlandı', sending: 'gönderiliyor', delivery_unknown: 'teslim belirsiz', cancelled: 'iptal'};
  let filter = 'undecided', data = null;

  const post = async (id, choice) => {
    const r = await fetch('/control/private/asks/' + id, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-Verda-CSRF': data.csrf_token}, body: JSON.stringify({choice})});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Kaydedilemedi (' + r.status + ')');
    return r.json();
  };

  const delta = (to, from) => { const d = to - from; return d === 0 ? '' : (d > 0 ? ' (+' : ' (−') + Math.abs(d) + ')'; };

  const detail = (r, box) => {
    if (r.text) { el('div', 'Gönderilecek mesaj:', 'muted', box); el('blockquote', r.text, null, box); }
    else el('p', 'Şu an sorulacak bir şey yok: ' + (r.why_not || '—'), 'muted', box);
    const s = r.if_answered;
    if (s && s.cases.length) {
      el('div', 'Cevap gelince puan (bugün ' + s.now + '):', 'muted', box);
      const cases = el('div', null, 'cases', box);
      s.cases.forEach(c => {
        const k = el('div', null, 'case', cases);
        el('div', c.label, null, k);
        const b = el('b', String(c.score), c.score > s.now ? 'up' : c.score < s.now ? 'down' : '', k);
        el('span', delta(c.score, s.now), 'd', k);
        el('div', c.detail, 'd', k);
        b.title = 'Aynı beklenen fiyat, arazi ve cezalarla yeniden hesaplanan fırsat puanı';
      });
      if (s.eliminates.length) el('p', 'Elenir: ' + s.eliminates.join(' · '), 'bad', box);
    } else el('p', 'Yol ve sit zaten biliniyor; cevap puanı değiştirmez.', 'muted', box);
    if (r.last_message) el('p', 'Son mesaj: ' + (SENT[r.last_message.state] || r.last_message.state) + ' · ' + day(r.last_message.sent_at || r.last_message.updated_at), 'note', box);
    const more = el('p', null, 'note', box); const a = el('a', 'İlan dosyasını aç', null, more); a.href = '/control/listing/' + r.listing_id;
  };

  const row = r => {
    const d = el('details', null, 'ask'); d.dataset.choice = r.choice || '';
    const s = el('div', null, 'arow', el('summary', null, null, d));
    const band = r.score >= 65 ? 'hi' : r.score >= 50 ? 'mid' : 'lo';
    const best = r.if_answered && r.if_answered.cases.length ? r.if_answered.cases[0].score : null;
    const sc = el('span', String(r.score), 'score ' + band + ' prov', s); sc.title = 'Bugünkü fırsat puanı' + (best != null ? ' · cevaplar iyi gelirse ' + best : '');
    const main = el('span', null, 'amain', s);
    el('span', readableTitle(r.title) || 'İlan ' + r.listing_id, 'atitle', main);
    const line = el('span', null, 'aline', main);
    el('span', '#' + r.listing_id, null, line);
    if (r.parcel) el('span', r.parcel, null, line);
    const tk = r.links.tkgm; if (tk) { const a = el('a', 'TKGM', null, line); a.href = tk; a.target = '_blank'; a.rel = 'noopener'; } else el('span', 'TKGM: parsel yok', 'muted', line);
    const sh = r.links.sahibinden; if (sh) { const a = el('a', 'Sahibinden', null, line); a.href = sh; a.target = '_blank'; a.rel = 'noopener'; }
    el('span', [r.neighborhood, r.area_m2 ? num(r.area_m2) + ' m²' : null, money(r.price_tl)].filter(Boolean).join(' · '), 'muted', line);
    el('span', r.asks_tr.length ? 'Sorulacak: ' + r.asks_tr.join(', ') + (best != null ? ' · iyi cevapla puan ' + best : '') : 'Sorulamaz: ' + (r.why_not || ''), 'asks', main);
    const ch = el('span', null, 'choice', s);
    [['ask', 'Sor'], ['hold', 'Beklet'], ['exclude', 'Ele']].forEach(([c, label]) => {
      const b = el('button', label, null, ch); b.type = 'button'; b.dataset.c = c; b.setAttribute('aria-pressed', String(r.choice === c));
      if (c === 'exclude') b.title = 'İlgilenmiyorum: ilan elenir, ona hiçbir iş yapılmaz';
      b.onclick = async e => {
        e.preventDefault(); e.stopPropagation();
        const next = r.choice === c ? 'clear' : c;
        if (c === 'exclude' && !confirm('#' + r.listing_id + ' elensin mi? (İlan sayfasından geri alınabilir.)')) return;
        ch.querySelectorAll('button').forEach(x => x.disabled = true);
        try {
          await post(r.listing_id, next);
          if (c === 'exclude') data.rows = data.rows.filter(x => x !== r); else r.choice = next === 'clear' ? null : next;
          draw();
        } catch (err) { alert(err.message); ch.querySelectorAll('button').forEach(x => x.disabled = false); }
      };
    });
    detail(r, el('div', null, 'detail', d));
    return d;
  };

  let box, filtersEl, countEl;
  const draw = () => {
    const rows = data.rows, view = rows.filter(FILTERS.find(f => f[0] === filter)[2]);
    filtersEl.querySelectorAll('button').forEach(b => { b.setAttribute('aria-pressed', String(b.dataset.f === filter)); b.querySelector('b').textContent = rows.filter(FILTERS.find(f => f[0] === b.dataset.f)[2]).length; });
    countEl.textContent = view.length + ' ilan';
    box.textContent = '';
    if (!view.length) { el('div', filter === 'undecided' ? 'Karar bekleyen ilan kalmadı.' : 'Bu süzgece uyan ilan yok.', 'empty', box); return; }
    view.forEach(r => box.appendChild(row(r)));
  };

  fetchJson('/control/private/asks').then(d => {
    data = d; app.textContent = '';
    el('h1', 'Soru onayı', null, app);
    el('p', 'Satıcıya yalnızca "Sor" dediğin ilanlara soru gider. "Beklet" ilanı tutar ama yazmaz; "Ele" ilanı çıkarır. Bir satıra tıklayınca gidecek mesajı ve cevaba göre puanın nereye çıkacağını görürsün. ' + (MODE[d.sender_mode] || ''), 'note', app);
    filtersEl = el('div', null, 'filters', app); filtersEl.setAttribute('role', 'group'); filtersEl.setAttribute('aria-label', 'Süzgeç');
    FILTERS.forEach(([k, label]) => { const b = el('button', label + ' ', null, filtersEl); b.type = 'button'; b.dataset.f = k; el('b', '', null, b); });
    filtersEl.onclick = e => { const b = e.target.closest('button'); if (b) { filter = b.dataset.f; draw(); } };
    countEl = el('p', '', 'rank-count', app);
    box = el('div', null, 'list', app);
    draw();
  }).catch(e => { app.textContent = ''; el('p', e.message, 'bad', el('section', null, null, app)); });
})();
