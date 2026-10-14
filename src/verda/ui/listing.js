(() => {
  const {el, num, dec, money, readableTitle, loadOrder, fetchJson, day} = Verda;
  const id = location.pathname.split('/').pop();
  const app = document.getElementById('app');
  const FIELD = {price_tl: 'Fiyat', sale_area_m2: 'Alan', sale_scope_resolved: 'Satış kapsamı', parcel_key: 'Ada/Parsel', property_type: 'Nitelik', shared_title: 'Hisseli mi',
    archaeological_sit: 'Arkeolojik sit', natural_sit: 'Doğal sit', access: 'Kadastral yol', route_minutes: 'Mertur süresi', destination_verified: 'Hedef onaylı',
    route: 'Mertur rotası', sale_area: 'Alan', price: 'Fiyat', legacy_excluded: 'eski sistem eledi', natural_sit_second_degree: 'doğal sit 2. derece', natural_sit_third_degree: 'doğal sit 3. derece', access_existing_easement: 'geçiş hakkı'};
  const GRADE = {kullanici: ['Senin kararın', 'acc'], resmi: ['Resmî kayıt', 'ok'], bagimsiz: ['Hesap', 'acc'], beyan: ['Beyan', 'warn'], rapor: ['Varsayım', '']};
  const STATE = {excluded: ['Elendi', 'bad'], outside_search: ['45 dk dışında', 'warn'], research_required: ['Araştırma sürüyor', 'warn'], conditional: ['Koşullu', 'warn'], eligible: ['Ön eleme tamam', 'ok']};
  const ACTION = {'sahibinden.read_listing': 'İlan ayrıntısını oku', 'sahibinden.read_thread': 'Satıcı konuşmasını oku', 'tkgm.lookup': 'Resmî parsel kaydını sorgula', 'geo.route': 'Mertur rotasını hesapla',
    'ask:sit': 'Satıcıya sit durumunu sor', 'ask:access': 'Satıcıya kadastral yol cephesini sor', 'ask:parcel_identity': 'Satıcıya ada/parseli sor',
    human_review: 'Senin incelemen gerekiyor', user_review: 'Gözden geçir: ön eleme tamam', none: 'Yapılacak bir şey yok'};
  const SRC = {parcel_key: 'TKGM kaydı', official_area_m2: 'TKGM resmî alan', official_area_matches_listing: 'TKGM parseli bulundu → tek parsel satışı', official_parcel_identified: 'TKGM parseli bulundu → tek parsel satışı',
    property_type: 'TKGM nitelik', price_tl: 'ilandaki fiyat', area_m2: 'ilandaki alan', 'engine:osrm': 'OSRM', 'engine:valhalla': 'Valhalla',
    'absence:no_shared_title_words': 'ilanda hisse sözcüğü yok → hisseli değil varsayıldı', 'attributes.imar durumu': 'ilandaki İmar Durumu', 'attributes.tapu durumu': 'ilandaki Tapu Durumu'};
  const VALUE = {none: 'yok', first_degree: '1. derece', second_degree: '2. derece', third_degree: '3. derece', archival_exception: 'arşiv istisnası', direct: 'doğrudan kadastral cephe',
    existing_easement: 'mevcut geçiş hakkı', corridor_conditional: 'koridor (koşullu)', true: 'evet', false: 'hayır'};
  const OPTIONS = {archaeological_sit: [['false', 'hayır'], ['true', 'evet · eler']], shared_title: [['false', 'hayır'], ['true', 'evet']],
    natural_sit: [['none', 'yok'], ['first_degree', '1. derece'], ['second_degree', '2. derece · −30'], ['third_degree', '3. derece · −30']],
    access: [['direct', 'doğrudan kadastral cephe'], ['existing_easement', 'mevcut geçiş hakkı · −10'], ['corridor_conditional', 'koridor (koşullu)'], ['none', 'yok']]};
  const CRITERIA = ['parcel_key', 'property_type', 'shared_title', 'archaeological_sit', 'natural_sit', 'access'];
  const EDITABLE = ['parcel_key', 'shared_title', 'archaeological_sit', 'natural_sit', 'access'];
  const show = v => Array.isArray(v) ? v.map(show).join(' · ') : (VALUE[v] ?? (typeof v === 'number' ? v.toLocaleString('tr-TR') : String(v)));
  const tone = (field, v) => field === 'archaeological_sit' ? (v === true ? 'bad' : 'ok') : field === 'shared_title' ? (v === true ? 'bad' : 'ok')
    : field === 'natural_sit' ? (v === 'none' ? 'ok' : v === 'first_degree' ? 'bad' : v === 'archival_exception' ? 'warn' : 'warn')
    : field === 'access' ? (v === 'direct' ? 'ok' : v === 'none' ? 'bad' : 'warn') : 'ok';
  const claimsOf = (facts, f) => facts[f] == null ? [] : Array.isArray(facts[f]) ? facts[f] : [facts[f]];
  const sourceOf = c => c.quote || SRC[c.source_ref] || c.source_ref || '';

  fetchJson('/control/private/listing/' + encodeURIComponent(id)).then(d => {
    app.textContent = '';
    const st = d.status, page = d.page, facts = st.facts || {};
    const title = readableTitle(page?.title) || 'İlan ' + id;
    document.title = title + ' · Verda';
    const flags = Object.fromEntries((d.clarifications || []).map(c => [c.field, c]));
    const post = async (url, body) => {
      const r = await fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-Verda-CSRF': d.csrf_token}, body: JSON.stringify(body)});
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'İşlem yapılamadı.');
      return r.json();
    };

    // navigation along the list the user came from
    const order = loadOrder(), at = order.indexOf(id);
    const crumbs = el('div', null, 'crumbs', app);
    const back = el('a', '← İlanlar', null, crumbs); back.href = '/control/ranking';
    el('span', null, 'sp', crumbs);
    if (at >= 0) {
      const step = el('span', null, 'step', crumbs);
      const link = (i, t) => { if (order[i]) { const a = el('a', t, null, step); a.href = '/control/listing/' + order[i]; a.title = t === '‹' ? 'Önceki ilan' : 'Sonraki ilan'; } else el('span', t, 'off', step); };
      link(at - 1, '‹'); el('span', (at + 1) + ' / ' + order.length, null, step).style.padding = '0 6px'; link(at + 1, '›');
      document.addEventListener('keydown', e => { if (e.target.closest('input,textarea,select') || e.metaKey || e.ctrlKey) return; const to = e.key === 'ArrowLeft' ? order[at - 1] : e.key === 'ArrowRight' ? order[at + 1] : null; if (to) location.href = '/control/listing/' + to; });
    }

    // head
    const head = el('section', null, 'head', app);
    el('h1', title, null, head);
    const meta = el('div', null, 'meta', head);
    const op = st.official_parcel;
    const where = [op?.found ? op.neighborhood : null, op?.found ? op.type : null].filter(Boolean).join(' · ');
    const whereEl = el('span', where || 'Mahalle ve nitelik henüz bilinmiyor', null, meta);
    el('span', '#' + id, null, meta);
    const dt = d.dates || {};
    el('span', 'yayın ' + (day(dt.published_at) || '—'), null, meta).title = 'İlanın Sahibinden\'de yayınlandığı gün';
    el('span', 'keşif ' + (day(dt.discovered_at) || '—'), null, meta).title = 'Sistemin ilanı ilk gördüğü gün' + (dt.discovered_by ? ' · kaynak: ' + dt.discovered_by : '');
    el('span', page ? 'okundu ' + new Date(page.captured_at * 1000).toLocaleString('tr-TR', {dateStyle: 'short', timeStyle: 'short'}) : 'ayrıntısı henüz okunmadı', null, meta);
    const lk = (href, text) => { if (!href) return; const a = el('a', text + ' ↗', 'btn', meta); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; };
    lk(st.links.sahibinden, 'Sahibinden'); lk(st.links.tkgm, 'TKGM');

    // the figures, once
    const first = f => claimsOf(facts, f)[0]?.value;
    const price = first('price_tl'), areaV = first('sale_area_m2'), minutes = claimsOf(facts, 'route_minutes').map(c => c.value).filter(v => typeof v === 'number');
    const tiles = el('div', null, 'tiles', app);
    const tile = (label, value, sub, cls) => { const t = el('div', null, 'tile', tiles); el('span', label, null, t); const b = el('b', value, cls || null, t); el('small', sub || '', null, t); return {t, b, sub: t.querySelector('small')}; };
    const tScore = tile('Puan', '…', '');
    tile('Fiyat', price == null ? '—' : money(price), price == null ? '' : price.toLocaleString('tr-TR') + ' ₺').b.title = price == null ? '' : price.toLocaleString('tr-TR') + ' ₺';
    tile('Alan', areaV == null ? '—' : num(areaV) + ' m²', claimsOf(facts, 'sale_area_m2')[0] ? (GRADE[claimsOf(facts, 'sale_area_m2')[0].grade] || [''])[0] : '');
    const tUnit = tile('₺/m²', price && areaV ? num(price / areaV) : '—', 'satış alanına göre');
    const rng = minutes.length ? (Math.min(...minutes) === Math.max(...minutes) ? dec(Math.max(...minutes)) : dec(Math.min(...minutes)) + '–' + dec(Math.max(...minutes))) + ' dk' : '—';
    const km = (d.route?.engines || []).filter(e => e.ok).map(e => e.km); const tRoute = tile('Mertur', rng, km.length ? dec(Math.max(...km)) + ' km' + (d.route?.engines?.length > 1 ? ' · ' + d.route.engines.length + ' motor' : '') : (d.route?.reason ? 'hesaplanamadı' : ''));
    const tCmp = tile('Emsal', '…', '');

    // verdict, once
    const vb = el('section', null, 'verdict-bar', app);
    if (st.decision) {
      const [label, t] = STATE[st.decision.state] || [st.decision.state, ''];
      el('span', label, 'verdict ' + t, vb);
      const why = st.decision.reasons.filter(r => r !== 'screening_complete');
      if (st.decision.state !== 'research_required' && why.length) el('span', why.filter(r => r !== 'legacy_excluded').map(r => FIELD[r] || r).join(', '), 'next', vb);
      if (why.includes('legacy_excluded')) { const n = el('span', null, 'next', vb); el('b', 'Eski sistem eledi: ', null, n); n.append(d.legacy_reason || 'neden kayıtlı değil'); }
      Object.entries(st.decision.penalties || {}).forEach(([k, v]) => { el('span', '−' + v + ' ' + (FIELD[k] || k), 'chip bad', vb).title = 'Puandan düşen ceza'; });
      if (st.decision.conditions.length) el('span', 'Koşul: ' + st.decision.conditions.join(', '), 'next', vb);
    } else el('span', 'Henüz karar için yeterli olgu yok.', 'next', vb);
    if ((d.clarifications || []).length) el('span', d.clarifications.length + ' alan senin netleştirmeni bekliyor', 'chip warn', vb);
    const na = st.next_action;
    if (na && na.action !== 'user_review') {
      const n = el('span', null, 'next', vb); el('b', 'Sıradaki: ', null, n); n.append((ACTION[na.action] || na.action) + (na.also?.length ? ' → sonra ' + na.also.map(x => ACTION[x] || x).join(', ') : ''));
    }

    // criteria: each with its proof, a pencil, and the seller's own words when they did not settle it
    const sec = el('section', null, null, app); el('h2', 'Kriterler ve kanıtları', null, sec);
    const rows = el('div', null, 'rows', sec);
    const restPost = (field, value, note) => post('/control/private/listing/' + encodeURIComponent(id) + '/decide', {field, value, note});
    const editor = (field, row, flag) => {
      const form = el('form', null, 'edit', row); form.noValidate = true;
      let input;
      if (OPTIONS[field]) {
        const cur = claimsOf(facts, field).find(c => c.grade === 'kullanici')?.value;
        const opts = el('div', null, 'opts', form);
        OPTIONS[field].forEach(([v, t]) => { const l = el('label', null, 'opt', opts); const r = el('input', null, null, l); r.type = 'radio'; r.name = 'v-' + field; r.value = v; r.checked = String(cur) === v; el('span', t, null, l); });
        input = () => opts.querySelector('input:checked')?.value;
      } else {
        const i = el('input', null, null, form); i.type = 'text'; i.placeholder = 'ada/parsel, örn. 118/24'; i.setAttribute('aria-label', 'Ada/Parsel'); input = () => i.value.trim();
      }
      const lab = el('label', 'Dayanağın', 'field', form);
      const note = el('textarea', null, null, lab); note.rows = 2; note.maxLength = 300; note.placeholder = 'Neden böyle? Örn. satıcıyla telefonda konuştum; e-Devlet’te baktım…'; el('small', 'Satırda senin notun olarak görünür', null, lab);
      const actions = el('div', null, 'actions', form);
      const save = el('button', 'Değeri kaydet', 'primary', actions); save.type = 'button';
      const state = el('span', '', 'muted', actions);
      if (flag) { const close = el('button', 'Netleştirdim, işareti kaldır', null, actions); close.type = 'button';
        close.onclick = async () => { close.disabled = true; try { await post('/control/private/flags/' + flag.id + '/close', {note: note.value.trim()}); location.reload(); } catch (e) { state.textContent = e.message; state.className = 'bad'; close.disabled = false; } }; }
      save.onclick = async () => {
        const raw = input();
        if (!raw) { state.textContent = 'Bir seçenek seç.'; state.className = 'bad'; return; }
        if (note.value.trim().length < 3) { state.textContent = 'Dayanağını kısaca yaz.'; state.className = 'bad'; return; }
        save.disabled = true; state.textContent = 'Kaydediliyor…'; state.className = 'muted';
        try { await restPost(field, raw === 'true' ? true : raw === 'false' ? false : raw, note.value.trim()); location.reload(); }
        catch (e) { state.textContent = e.message; state.className = 'bad'; save.disabled = false; }
      };
      return form;
    };
    // what the system did to learn an unknown field, from the records: asked / waiting / replied / halted, with the conversation link
    const learn = d.learning || {fields: {}, events: [], conversation: null};
    const when = t => t ? new Date(t * 1000).toLocaleString('tr-TR', {dateStyle: 'short', timeStyle: t % 86400 === 0 ? undefined : 'short'}) : '';
    const learnLine = (field, parent) => {
      const f = learn.fields[field]; if (!f) { el('div', 'Henüz kanıt yok', 'csrc', parent); return; }
      const line = el('div', null, 'learn ' + f.state, parent);
      el('span', null, 'ldot', line);
      el('span', f.label + (f.at ? ' · ' + when(f.at) : ''), null, line);
      if (f.url) { const a = el('a', 'Konuşmayı aç ↗', null, line); a.href = f.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; }
    };
    const crow = (field, claim) => {
      const flag = flags[field];
      const row = el('div', null, 'crow' + (flag ? ' flagged' : ''), rows);
      const v = claim ? claim.value : null;
      el('span', null, 'dot ' + (claim ? tone(field, v) : 'miss'), row);
      const main = el('div', null, 'cmain', row);
      const line = el('div', null, 'cline', main);
      el('span', FIELD[field] || field, 'k', line);
      el('b', claim ? show(v) : 'eksik', claim ? 'v' : 'v miss', line);
      if (claim) { const [g, gt] = GRADE[claim.grade] || [claim.grade, '']; el('span', g, 'chip ' + gt, line); }
      const src = claim ? sourceOf(claim) : '';
      if (src || claim?.proposer?.startsWith('llm:')) el('div', src + (claim?.proposer?.startsWith('llm:') ? (src ? ' · ' : '') + 'model önerdi, alıntı doğrulandı' : ''), 'csrc', main);
      if (!claim) learnLine(field, main);
      if (flag) { const b = el('blockquote', null, null, main); el('b', 'Satıcı: ', null, b); b.append('“' + flag.quote + '”'); el('div', flag.note, 'muted', b); }
      if (EDITABLE.includes(field)) {
        const pen = el('button', '✎', 'pencil', row); pen.type = 'button'; pen.setAttribute('aria-expanded', 'false'); pen.setAttribute('aria-label', (FIELD[field] || field) + ' bilgisini düzelt ya da not ekle'); pen.title = 'Düzelt ya da not ekle';
        pen.onclick = e => { const open = row.querySelector('form.edit'); if (open) { open.remove(); pen.setAttribute('aria-expanded', 'false'); } else { editor(field, row, flag); pen.setAttribute('aria-expanded', 'true'); if (e.isTrusted) { row.querySelector('form.edit input, form.edit textarea')?.focus({preventScroll: true}); row.scrollIntoView({block: 'nearest'}); } } };
        if (flag) pen.click();
      }
    };
    const seen = new Set();
    CRITERIA.forEach(f => { seen.add(f); const cl = claimsOf(facts, f); if (cl.length) cl.forEach(c => crow(f, c)); else crow(f, null); });
    if (minutes.length || d.route) {
      const row = el('div', null, 'crow', rows); const missing = !minutes.length;
      el('span', null, 'dot ' + (missing ? 'miss' : 'ok'), row);
      const main = el('div', null, 'cmain', row), line = el('div', null, 'cline', main);
      el('span', 'Mertur rotası', 'k', line); el('b', missing ? (d.route?.reason ? 'hesaplanamadı' : 'eksik') : 'hesaplandı', 'v' + (missing ? ' miss' : ''), line);
      if (!missing) el('span', 'Hesap', 'chip acc', line);
      const parts = (d.route?.engines || []).map(e => e.engine.toUpperCase() + ' ' + (e.ok ? dec(e.minutes) + ' dk · ' + dec(e.km) + ' km' + (e.origin_snap_m != null ? ' · yola izdüşüm ' + e.origin_snap_m + ' m' : '') + (e.fallback ? ' (en yakın yoldan)' : '') : 'hata: ' + e.error));
      if (d.route?.reason) parts.push('Rota hesaplanamadı: ' + d.route.reason);
      if (d.route?.straight_line_km != null) parts.push('kuş uçuşu ' + d.route.straight_line_km + ' km · ' + (d.route.destination?.name || ''));
      if (parts.length) el('div', parts.join(' · '), 'csrc', main);
      if (d.route?.note) el('div', d.route.note, 'csrc fine', main);
    }
    if (!minutes.length && !d.route) {
      const row = el('div', null, 'crow', rows); el('span', null, 'dot miss', row);
      const main = el('div', null, 'cmain', row), line = el('div', null, 'cline', main);
      el('span', 'Mertur rotası', 'k', line); el('b', 'eksik', 'v miss', line); learnLine('route_minutes', main);
    }
    const extras = Object.keys(facts).filter(f => !seen.has(f) && !['price_tl', 'sale_area_m2', 'route_minutes', 'official_area_m2', 'area_m2'].includes(f));
    const more = extras.map(f => (FIELD[f] || f) + ': ' + claimsOf(facts, f).map(c => show(c.value)).join(' / ')).join(' · ');
    if (more) el('p', more, 'note', sec);
    if (st.listing_errors?.length) { const w = el('p', null, 'note', sec); el('b', 'İlandaki hatalar (resmî kayıt geçerli): ', 'warn', w); w.append(st.listing_errors.map(e => (FIELD[e.field] || e.field) + ': ilan ' + show(e.other.value) + ' → resmî ' + show(e.chosen.value)).join('; ')); }
    if (st.review?.length) { const w = el('p', null, 'note', sec); el('b', 'Senin kararını bekleyen: ', 'warn', w); w.append(st.review.map(x => (FIELD[x.field] || x.field) + ' = ' + show(x.value) + (x.quote ? ' ("' + x.quote + '")' : '')).join('; ')); }
    if (st.contradictions?.length) { const w = el('p', null, 'note', sec); el('b', 'Senin kararın ve resmî sonuç ayrışıyor: ', 'warn', w); w.append(st.contradictions.map(c => (FIELD[c.field] || c.field) + ': senin ' + show(c.chosen.value) + ' · resmî ' + show(c.other.value)).join('; ')); }
    if (st.to_verify_officially?.length) el('p', 'Alma öncesi resmî doğrulanacak: ' + st.to_verify_officially.map(f => FIELD[f] || f).join(', '), 'note', sec);



    // the seller message: the mode, the text the code would send now, and every attempt with what it proved
    const snd = d.sender;
    if (snd) {
      const box = el('section', null, null, app); el('h2', 'Satıcıya mesaj', null, box);
      const MODE = {off: ['Kapalı', 'bad'], dry_run: ['Deneme · göndermez', 'warn'], live: ['Canlı · gerçekten gönderir', 'ok']};
      const bar = el('div', null, 'row', box);
      el('span', (MODE[snd.mode] || [snd.mode, ''])[0], 'chip ' + (MODE[snd.mode] || ['', ''])[1], bar);
      const settings = el('a', 'Ayarlar ↗', 'muted', bar); settings.href = '/control/settings';
      const NAMES = {parcel_key: 'ada/parsel', access: 'kadastral yol', natural_sit: 'doğal sit', archaeological_sit: 'arkeolojik sit'};
      if (snd.preview.ok) {
        el('p', 'Sıradaki mesaj (metni kod yazar, model değil):', 'note', box);
        el('blockquote', snd.preview.text, 'msg', box);
        el('p', 'Sorulanlar: ' + snd.preview.fields.map(f => NAMES[f] || f).join(', ') + ' · ' + (snd.preview.dry_run ? 'Deneme modunda form doldurulur ve doğrulanır, mesaj gönderilmez.' : 'Canlı modda sıradaki çalışmada tek mesaj olarak gönderilir.'), 'note', box);
      } else el('p', 'Şu an mesaj hazırlanmıyor: ' + snd.preview.message, 'note', box);
      const STATE = {prepared: 'Hazırlandı, henüz gönderilmedi', sending: 'Gönderiliyor', dry_run: 'Deneme: yazıldı ve doğrulandı, GÖNDERİLMEDİ', confirmed: 'Gönderildi, konuşmada aynen görüldü', delivery_unknown: 'Gittiği doğrulanamadı', cancelled: 'İptal edildi'};
      snd.attempts.forEach(a => {
        const item = el('div', null, 'attempt ' + a.state, box);
        const line = el('div', null, 'row', item);
        el('b', STATE[a.state] || a.state, null, line);
        el('span', when(a.updated_at), 'muted', line);
        if (a.thread_url) { const l = el('a', 'Konuşmayı aç ↗', null, line); l.href = a.thread_url; l.target = '_blank'; l.rel = 'noopener noreferrer'; }
        el('blockquote', a.text, 'msg', item);
        const ev = a.evidence || {};
        const facts = [ev.typed_exact ? 'metin birebir yazıldı' : null, ev.send_enabled ? 'Gönder düğmesi etkin' : null, ev.draft_cleared ? 'taslak silindi' : null,
          ev.phone_banner === 'standard' ? 'Sahibinden’in standart karşılıklı telefon paylaşımı uyarısı var' : null, ev.exact_text_in_thread ? 'metin konuşmada aynen görüldü' : null, ['dry_run', 'confirmed'].includes(a.state) ? null : a.reason].filter(Boolean);
        if (facts.length) el('div', facts.join(' · '), 'note', item);
        if (a.state === 'delivery_unknown') {
          const form = el('div', null, 'actions', item);
          const note = el('input', null, null, form); note.type = 'text'; note.placeholder = 'Nasıl kontrol ettin? (örn. konuşmada gördüm)'; note.setAttribute('aria-label', 'Not');
          [['Gitti', true], ['Gitmedi', false]].forEach(([t, v]) => { const b = el('button', t, null, form); b.type = 'button'; b.onclick = async () => {
            try { await post('/control/private/outbox/' + a.id + '/resolve', {delivered: v, note: note.value.trim() || 'Kullanıcı konuşmayı kontrol etti'}); location.reload(); } catch (e) { alert(e.message); } }; });
        }
      });
    }

    // what was done for this listing, newest first
    if (learn.events.length || learn.conversation) {
      const act = el('section', null, null, app); el('h2', 'Ne yapıldı?', null, act);
      const c = learn.conversation;
      const sum = el('p', null, 'note', act);
      sum.style.marginTop = '0';
      if (!c) sum.textContent = 'Satıcı konuşması henüz kontrol edilmedi.';
      else if (!c.found) sum.textContent = 'Satıcıyla bu ilan için bir konuşma yok (son kontrol ' + when(c.checked_at) + ').';
      else {
        sum.append('Sahibinden konuşması: sen ' + c.sent + ', satıcı ' + c.received + ' mesaj yazdı · son kontrol ' + when(c.checked_at) + ' · ');
        const a = el('a', 'Konuşmayı aç ↗', null, sum); a.href = c.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      }
      const list = el('ul', null, 'acts', act);
      const ICON = {check: '✓', sent: '✉', reply: '↩', task: '⏳', halt: '⛔', draft: '✎', unknown: '⚠'};
      learn.events.forEach(e => {
        const li = el('li', null, e.kind, list);
        el('i', ICON[e.kind] || '·', null, li);
        const body = el('span', null, null, li); el('b', e.label, null, body);
        if (e.at) el('span', ' · ' + when(e.at), 'muted', body);
        if (e.url) { body.append(' '); const a = el('a', 'aç ↗', null, body); a.href = e.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; }
        if (e.text) el('div', '“' + e.text.slice(0, 260) + (e.text.length > 260 ? '…' : '') + '”', 'muted quote', body);
      });
    }

    // the listing text and its attributes
    if (page) {
      const desc = el('section', null, null, app); el('h2', 'İlan açıklaması', null, desc);
      el('div', page.description || '(açıklama yok)', 'desc', desc);
      const attrs = Object.entries(page.attributes || {}); if (attrs.length) { const a = el('div', null, 'attrs', desc); attrs.forEach(([k, v]) => el('span', k + ': ' + v, 'chip line', a)); }
    }

    // photos
    if (d.assets.length) {
      const ph = el('section', null, null, app); el('h2', 'Fotoğraflar · ' + d.assets.length + (page?.photos_listed ? ' / ilanda ' + page.photos_listed : ''), null, ph);
      const strip = el('div', null, 'strip', ph); const dlg = el('dialog', null, 'viewer', app); const big = el('img', null, null, dlg); dlg.onclick = () => dlg.close();
      d.assets.forEach(x => { const i = el('img', null, null, strip); i.src = '/control/private/assets/' + x.id; i.loading = 'lazy'; i.alt = 'İlan fotoğrafı'; i.onclick = () => { big.src = i.src; dlg.showModal(); }; });
    } else if (page?.photos_listed) el('p', 'Fotoğraf: ilanda ' + page.photos_listed + ' var, henüz kaydedilmedi.', 'note', app);

    // the figures that need the whole ranking arrive afterwards
    fetchJson('/control/private/ranking?limit=200').then(r => {
      const row = r.ranked.find(x => x.listing_id === id);
      if (!row) { tScore.b.textContent = '—'; tScore.b.className = 'dim'; tScore.sub.textContent = st.decision?.state === 'excluded' ? 'sıralama dışı' : ''; tCmp.b.textContent = '—'; return; }
      tScore.b.textContent = String(row.score); tScore.b.className = row.score >= 75 ? 'hi' : '';
      tScore.sub.textContent = row.provisional ? 'geçici · doğrulama eksik' : 'fiyat + Mertur';
      if (!whereEl.dataset.set && !where && row.neighborhood) whereEl.textContent = [row.neighborhood, row.type].filter(Boolean).join(' · ');
      if (row.discount_pct == null) { tCmp.b.textContent = 'emsal yok'; tCmp.b.className = 'dim'; tCmp.sub.textContent = 'puan en çok 49'; return; }
      const dp = row.discount_pct; tCmp.b.textContent = Math.abs(dp) < 5 ? 'emsale yakın' : dp > 0 ? '%' + dec(dp) + ' ucuz' : '%' + dec(-dp) + ' pahalı';
      tCmp.b.className = dp >= 5 ? 'hi' : dp <= -5 ? 'bad' : '';
      tCmp.sub.textContent = [row.benchmark_scope, row.comparables + ' emsal', row.confidence ? row.confidence + ' güven' : null].filter(Boolean).join(' · ');
      if (row.benchmark_unit_price) tUnit.sub.textContent = 'emsal ' + num(row.benchmark_unit_price) + ' ₺/m²';
    }).catch(() => { tScore.b.textContent = '—'; tCmp.b.textContent = '—'; });
  }).catch(e => { app.textContent = ''; el('p', e.message, 'bad', el('section', null, null, app)); });
})();
