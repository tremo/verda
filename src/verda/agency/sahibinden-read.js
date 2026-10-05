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
  else if (/too many requests|çok fazla istek|olağan dışı erişim/.test(lower)) blocker='rate_limited';
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
  for (const row of document.querySelectorAll('.classifiedInfoList li')) {
    const label=text(one('strong',row)).toLocaleLowerCase('tr');
    if(label)fields[label]=text(one('span',row));
  }
  const detailId=fields['ilan no'];
  const detail=detailId ? {listing_id:detailId,title:text(one('.classifiedDetailTitle h1')),url:location.href,
    price_tl:number(text(one('.classifiedInfo h3'))),area_m2:number(fields['m²']||fields['m2']||''),
    parcel_key:fields['ada no']&&fields['parsel no'] ? fields['ada no']+'/'+fields['parsel no'] : null,
    description:text(one('#classifiedDescription')).slice(0,16000)||null} : null;
  return {url:location.href,blocker,listings,detail,
    total_reported:totalMatch?Number(totalMatch[1].replace(/\./g,'')):null};
})()
