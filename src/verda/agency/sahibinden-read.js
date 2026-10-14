/* Static DOM reader. No network, clicks, hidden fields, or model-written code. */
(() => {
  const visible = node => {
    if (!node || !node.getClientRects().length) return false;
    for (let p=node;p && p.nodeType===1;p=p.parentElement) {
      const s=getComputedStyle(p);
      if (s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0||p.hidden) return false;
    }
    return true;
  };
  const text = node => visible(node) ? node.innerText.trim() : '';
  const one = (selector, root=document) => [...root.querySelectorAll(selector)].find(visible);
  const number = value => {
    const match=value.match(/\d[\d.\s]*(?:,\d+)?/);
    return match ? Number(match[0].replace(/[.\s]/g,'').replace(',','.')) : null;
  };
  const body=text(document.body), lower=body.toLocaleLowerCase('tr');
  let blocker=null;
  if (one('iframe[src*="captcha"], .g-recaptcha, #challenge-running, #challenge-stage') || /robot olmadığınızı|insan olduğunuzu|verify you are human|güvenlik doğrulaması/.test(lower)) blocker='captcha';
  else if (/^\/cs\/[a-z]*loading/i.test(location.pathname) || /tarayıcınızı kontrol ediyoruz|bağlantınız kontrol ediliyor|olağan dışı (erişim|kullanım)/.test(lower)) blocker='unusual_access';
  else if (/doğrulama kodu/.test(lower)) blocker='otp';
  else if (/too many requests|çok fazla istek/.test(lower)) blocker='rate_limited';
  else if (/access denied|erişiminiz engellen|erişim engellendi/.test(lower)) blocker='access_denied';
  const rows=[...document.querySelectorAll('tr.searchResultsItem[data-id]')].filter(visible);
  const listings=rows.map(row=>{
    const link=one('.searchResultsTitleValue a[href*="/ilan/"]',row);
    return {listing_id:row.getAttribute('data-id'),title:text(link),url:link?.href,
      price_tl:number(text(one('.searchResultsPriceValue',row))),
      area_m2:number(text(one('.searchResultsAttributeValue',row))),
      neighborhood:text(one('.searchResultsLocationValue',row))||null,
      listed_at_text:text(one('.searchResultsDateValue',row))||null};
  });
  const resultText=text(one('.result-text')), totalMatch=resultText.match(/([\d.]+)\s+ilan\b/);
  const fields={};
  // Real pages: dl.classifiedInfoList > div.classifiedInfoItem > dt + dd. Older layout: li > strong + span.
  for (const row of document.querySelectorAll('.classifiedInfoList .classifiedInfoItem, .classifiedInfoList li')) {
    const label=text(one('dt, strong',row)).toLocaleLowerCase('tr');
    if(label)fields[label]=text(one('dd, span',row));
  }
  const detailId=fields['ilan no'];
  const attributes=Object.fromEntries(Object.entries(fields).slice(0,40).map(([k,v])=>[k.slice(0,80),v.slice(0,300)]));
  const photoOk=u=>/^https:\/\/([^/]*\.)?(shbdn\.com|sahibinden\.com)\//.test(u||'');
  const photos=[...new Set([...document.querySelectorAll('[class*=hoto] img, [class*=allery] img, [id*=hoto] img, .classifiedDetailPhotos img')]
    .map(i=>i.getAttribute('data-src')||i.currentSrc||i.src).filter(photoOk))].slice(0,30);
  // The place is in the breadcrumb (… Datça › Köyler › Yaka Mh.): the last item that names a neighbourhood.
  const crumbs=[...document.querySelectorAll('li.bc-item')].map(e=>text(e).split('\n')[0].trim()).filter(Boolean);
  const place=[...crumbs].reverse().find(t=>/\b(Mh\.?|Mahallesi)$/i.test(t))||null;
  const detail=detailId ? {listing_id:detailId,title:text(one('.classifiedDetailTitle h1')),url:location.href,neighborhood:place,
    price_tl:number(text(one('.classifiedInfo h3'))),area_m2:number(fields['m²']||fields['m2']||''),
    parcel_key:fields['ada no']&&fields['parsel no'] ? fields['ada no']+'/'+fields['parsel no'] : null,
    description:text(one('#classifiedDescription')).slice(0,16000)||null,attributes,page_text:body.slice(0,8000)||null,photos:photos.length?photos:null} : null;
  const removed=/görüntülemek istediğiniz ilan yayında değil/.test(lower)&&!detailId;
  return {url:location.href,blocker,removed,listings,detail,
    total_reported:totalMatch?Number(totalMatch[1].replace(/\./g,'')):null};
})()
