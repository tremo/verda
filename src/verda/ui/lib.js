// Shared helpers for the listing pages: DOM building, number formats, readable titles.
window.Verda = (() => {
  const el = (tag, text, cls, parent) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; if (parent) parent.appendChild(e); return e; };
  const num = n => n == null ? '—' : Math.round(n).toLocaleString('tr-TR');
  const dec = (n, d = 1) => n == null ? '—' : Number(n).toLocaleString('tr-TR', {maximumFractionDigits: d});
  // 8.400.000 → "8,4 M ₺": the list is read by magnitude, the exact figure goes in the tooltip.
  const money = n => n == null ? '—' : n >= 1e6 ? dec(n / 1e6, 2) + ' M ₺' : num(n) + ' ₺';
  const moneyFull = n => n == null ? '' : num(n) + ' ₺';

  // Titles typed in capitals are shown in sentence case; place names keep their capital (same rule as the old dashboard).
  const PLACES = ['datça', 'datca', 'palamutbükü', 'knidos', 'yaka', 'sındı', 'cumalı', 'hızırşah', 'mesudiye', 'karaköy', 'kızlan', 'emecik', 'reşadiye', 'iskele', 'yazı',
    'ovabükü', 'hayıtbükü', 'kargı', 'gebekum', 'eksera', 'mersincik', 'bencik', 'aktur', 'ılıca', 'kurubük', 'değirmenbükü', 'marmaris', 'muğla', 'bozburun', 'pınarönü',
    'körmen', 'pelit', 'zeytincik', 'ege', 'yunan', 'sahibinden'];
  const WORD_FIXES = [['tıny', 'tiny'], ['knıdos', 'knidos'], ['mesudıye', 'mesudiye'], ['ımar', 'imar'], ['ılan', 'ilan'], ['acıl', 'acil'], ['ıdeal', 'ideal']];
  const FRONT = 'eiöü', VOWELS = 'eiöüaıou';
  // An ASCII capital I in a Turkish word is i or ı depending on the neighbouring vowels.
  const lowerWord = w => {
    const chars = [...w]; let out = '';
    for (let i = 0; i < chars.length; i++) {
      const c = chars[i];
      if (c !== 'I') { out += c.toLocaleLowerCase('tr'); continue; }
      let v = [...out].reverse().find(x => VOWELS.includes(x));
      if (!v) v = chars.slice(i + 1).map(x => x.toLocaleLowerCase('tr')).find(x => VOWELS.includes(x) && x !== 'ı');
      out += v && FRONT.includes(v) ? 'i' : 'ı';
    }
    return out;
  };
  const readableTitle = t => {
    let s = String(t ?? '').replace(/\s+/g, ' ').trim();
    if (!s) return '';
    const letters = s.replace(/[^\p{L}]/gu, ''), upper = letters.replace(/[^A-ZÇĞİÖŞÜ]/g, '');
    if (letters.length > 8 && upper.length / letters.length > .6) {
      s = s.replace(/\p{L}+/gu, w => {
        let low = lowerWord(w);
        for (const [from, to] of WORD_FIXES) if (low.startsWith(from)) low = to + low.slice(from.length);
        const place = PLACES.find(p => low.startsWith(p) && low.length - p.length <= 4);
        return place ? low[0].toLocaleUpperCase('tr') + low.slice(1) : low;
      }).replace(/^\P{L}*\p{L}/u, m => m.toLocaleUpperCase('tr')).replace(/([!?.]\s+)(\p{L})/gu, (m, a, b) => a + b.toLocaleUpperCase('tr'));
    }
    return s.replace(/(^|\s)(\p{L}+)(\s+\2)+(?=\s|$|[!,.])/giu, '$1$2').replace(/m2(?=$|[^\p{L}\d])/gu, 'm²').replace(/\.{3,}/g, '…').replace(/([!?])\1+/g, '$1');
  };

  // The order of the list the user last looked at, so an open listing can step to the next one.
  const ORDER_KEY = 'verda.order';
  const saveOrder = ids => { try { sessionStorage.setItem(ORDER_KEY, JSON.stringify(ids)); } catch { /* storage may be unavailable */ } };
  const loadOrder = () => { try { return JSON.parse(sessionStorage.getItem(ORDER_KEY) || '[]'); } catch { return []; } };

  // "21 Eyl", with the year when it is not this year
  const day = t => { if (t == null) return ''; const d = new Date(t * 1000); return d.toLocaleDateString('tr-TR', d.getFullYear() === new Date().getFullYear() ? {day: 'numeric', month: 'short'} : {day: 'numeric', month: 'short', year: 'numeric'}); };
  const fetchJson = async url => {
    const r = await fetch(url, {credentials: 'same-origin'});
    if (r.status === 401) throw new Error('Yerel görüntüleyici bağlantısını (verda serve çıktısı) aynı tarayıcıda aç.');
    if (!r.ok) throw new Error('Veri alınamadı (' + r.status + ').');
    return r.json();
  };
  return {el, num, dec, money, moneyFull, readableTitle, saveOrder, loadOrder, fetchJson, day};
})();
