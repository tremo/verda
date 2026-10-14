(() => {
  const {el, num, money, moneyFull, readableTitle, saveOrder, fetchJson, day} = Verda;
  const app = document.getElementById('app');
  const MISSING = {sale_area: 'alan', sale_area_m2: 'alan', route: 'rota', route_minutes: 'rota', archaeological_sit: 'arkeolojik sit', natural_sit: 'doğal sit', access: 'yol',
    price: 'fiyat', price_tl: 'fiyat', parcel_key: 'ada/parsel', property_type: 'nitelik', shared_title: 'hisse', sale_scope_resolved: 'satış kapsamı'};
  const PENALTY = {natural_sit_second_degree: 'doğal sit 2. derece', natural_sit_third_degree: 'doğal sit 3. derece', access_existing_easement: 'geçiş hakkı'};
  const SORTS = {
    score: ['Puan', null],
    price: ['Fiyat · düşükten', (a, b) => (a.price_tl ?? Infinity) - (b.price_tl ?? Infinity)],
    area: ['Alan · büyükten', (a, b) => (b.area_m2 ?? -Infinity) - (a.area_m2 ?? -Infinity)],
    unit: ['m² fiyatı · düşükten', (a, b) => (unit(a) ?? Infinity) - (unit(b) ?? Infinity)],
    route: ['Mertur · yakından', (a, b) => (a.route_minutes_max ?? Infinity) - (b.route_minutes_max ?? Infinity)],
    discount: ['Emsale göre ucuz', (a, b) => (b.discount_pct ?? -Infinity) - (a.discount_pct ?? -Infinity)],
    published: ['Yayın · yeni önce', (a, b) => (b.published_at ?? -Infinity) - (a.published_at ?? -Infinity)],
    discovered: ['Keşif · yeni önce', (a, b) => (b.discovered_at ?? -Infinity) - (a.discovered_at ?? -Infinity)]};
  const FILTERS = [['all', 'Hepsi', () => true], ['settled', 'Ön eleme tamam', r => !r.provisional], ['research', 'Araştırılıyor', r => r.provisional], ['clarify', 'Netleştirme', r => r.needs_clarification]];
  const unit = r => r.price_tl && r.area_m2 ? r.price_tl / r.area_m2 : null;
  const STORE = 'verda.rank';
  let ui = {q: '', filter: 'all', sort: 'score'};
  try { ui = {...ui, ...JSON.parse(sessionStorage.getItem(STORE) || '{}')}; } catch { /* a fresh state is fine */ }
  const remember = () => { try { sessionStorage.setItem(STORE, JSON.stringify(ui)); } catch { /* storage may be unavailable */ } };
  const fold = s => String(s || '').toLocaleLowerCase('tr');

  const chips = r => {
    const out = [];
    if (r.discount_pct != null) out.push([Math.abs(r.discount_pct) < 5 ? 'emsale yakın' : r.discount_pct > 0 ? '%' + num(r.discount_pct) + ' ucuz' : '%' + num(-r.discount_pct) + ' pahalı',
      r.discount_pct >= 5 ? 'ok' : r.discount_pct <= -5 ? 'bad' : '', r.comparables + ' emsal']);
    else out.push(['emsal yok', 'line', 'Yeterli emsal yok; puan en çok 49']);
    out.push(r.provisional ? [(r.missing || []).length > 2 ? (r.missing.length + ' eksik') : (r.missing || []).length ? 'eksik: ' + r.missing.map(x => MISSING[x] || x).join(', ') : 'araştırılıyor', 'warn', (r.missing || []).map(x => MISSING[x] || x).join(', ')]
      : ['ön eleme tamam', 'ok', 'Resmî uygunluk değildir']);
    for (const [k, v] of Object.entries(r.penalties || {})) out.push(['−' + v + ' ' + (PENALTY[k] || k), 'bad', 'Puandan düşen ceza']);
    if (r.needs_clarification) out.push(['netleştirme', 'warn', 'Satıcı cevabı bir alanı netleştirmiyor']);
    return out;
  };

  const row = r => {
    const a = el('a', null, 'lrow' + (r.needs_clarification ? ' clar' : ''));
    a.href = '/control/listing/' + r.listing_id;
    const band = r.score >= 75 ? 'hi' : r.score >= 50 ? 'mid' : 'lo';
    const s = el('span', String(r.score), 'score ' + band + (r.provisional ? ' prov' : ''), a);
    s.title = r.provisional ? 'Geçici puan: doğrulama eksik' : 'Puan';
    const main = el('span', null, 'lmain', a);
    const title = readableTitle(r.title) || 'İlan ' + r.listing_id;
    el('span', title, 'ltitle', main).title = title;
    el('span', [r.neighborhood, r.type && r.type !== 'Diğer / belirsiz' ? r.type : null, '#' + r.listing_id].filter(Boolean).join(' · '), 'lmeta', main);
    const dates = el('span', null, 'lmeta ldates', main);
    const pub = el('span', 'yayın ' + (day(r.published_at) || '—'), null, dates); pub.title = 'İlanın Sahibinden\'de yayınlandığı gün';
    const disc = el('span', 'keşif ' + (day(r.discovered_at) || '—'), null, dates); disc.title = r.discovered_by ? 'Sistemin ilanı ilk gördüğü gün · kaynak: ' + r.discovered_by : 'Sistemin ilanı ilk gördüğü gün';
    if (r.action) { const act = el('span', r.action.label + (r.action.count > 1 ? ' (' + r.action.count + ' alan)' : ''), 'lact ' + r.action.state, dates); act.title = 'Eksik bilgileri tamamlamak için alınan aksiyon'; }
    const nums = el('span', null, 'lnums', a);
    const price = el('span', money(r.price_tl), 'num r', nums); price.title = moneyFull(r.price_tl);
    el('span', r.area_m2 == null ? '—' : num(r.area_m2) + ' m²', 'num r', nums);
    el('span', unit(r) == null ? '—' : num(unit(r)) + ' ₺/m²', 'num r', nums);
    el('span', r.route_minutes_max == null ? '—' : num(r.route_minutes_max) + ' dk', 'num r', nums);
    const flags = el('span', null, 'lflags', a);
    chips(r).forEach(([text, tone, tip]) => { const c = el('span', text, 'chip ' + tone, flags); if (tip) c.title = tip; });
    return a;
  };

  const draw = (data, box, countEl, filtersEl) => {
    const rows = data.ranked;
    const view = rows.filter(FILTERS.find(f => f[0] === ui.filter)[2]).filter(r => !ui.q || fold([r.title, r.neighborhood, r.type, r.listing_id].join(' ')).includes(fold(ui.q)));
    const sorter = SORTS[ui.sort][1];
    if (sorter) view.sort(sorter);
    saveOrder(view.map(r => r.listing_id));
    filtersEl.querySelectorAll('button').forEach(b => { b.setAttribute('aria-pressed', String(b.dataset.f === ui.filter)); b.querySelector('b').textContent = rows.filter(FILTERS.find(f => f[0] === b.dataset.f)[2]).length; });
    countEl.textContent = view.length + ' ilan' + (view.length !== rows.length ? ' · ' + rows.length + ' içinden' : '');
    box.textContent = '';
    if (!view.length) { el('div', 'Bu süzgece uyan ilan yok.', 'empty', box); return; }
    const head = el('div', null, 'lhead', box);
    ['Puan', 'İlan', 'Fiyat', 'Alan', '₺/m²', 'Mertur', 'Durum'].forEach((t, i) => el('span', t, [2, 3, 4, 5].includes(i) ? 'r' : '', head));
    view.forEach(r => box.appendChild(row(r)));
  };

  fetchJson('/control/private/ranking?limit=200').then(d => {
    app.textContent = '';
    const bar = el('div', null, 'toolbar', app);
    const q = el('input', null, null, bar); q.type = 'search'; q.placeholder = 'Başlık, mahalle, ilan no…'; q.value = ui.q; q.setAttribute('aria-label', 'İlan ara');
    const sort = el('select', null, null, bar); sort.setAttribute('aria-label', 'Sırala');
    Object.entries(SORTS).forEach(([k, [label]]) => { const o = el('option', label, null, sort); o.value = k; o.selected = k === ui.sort; });
    const filters = el('div', null, 'filters', app); filters.setAttribute('role', 'group'); filters.setAttribute('aria-label', 'Süzgeç');
    FILTERS.forEach(([k, label]) => { if (k === 'clarify' && !d.ranked.some(r => r.needs_clarification)) return; const b = el('button', label + ' ', null, filters); b.type = 'button'; b.dataset.f = k; el('b', '', null, b); });
    const count = el('p', '', 'rank-count', app); count.title = d.note;
    const box = el('div', null, 'list', app);
    const redraw = () => { remember(); draw(d, box, count, filters); };
    q.oninput = () => { ui.q = q.value; redraw(); };
    sort.onchange = () => { ui.sort = sort.value; redraw(); };
    filters.onclick = e => { const b = e.target.closest('button'); if (b) { ui.filter = b.dataset.f; redraw(); } };
    redraw();
  }).catch(e => { app.textContent = ''; el('p', e.message, 'bad', el('section', null, null, app)); });
})();
