/* Local management controls. Source text is always rendered as text, never HTML. */
function renderBrowser(root){
  const browser=data.browser, panel=card('Tarayıcı motoru');
  panel.append(e('p','Sahibinden operatörü aynı arama ve ilan okuma araçlarını kullanır. Bu seçim, yeni tarayıcı işlerini hangi motorun yürüteceğini belirler. Ana yönetici ve agent modelleri değişmez.'));
  const label=e('label','Yeni işler için motor'), select=e('select');select.setAttribute('aria-label','Tarayıcı motoru');
  for(const driver of browser.drivers)select.append(new Option(driver.label,driver.key));select.value=browser.selected_driver;label.append(select);
  const save=e('button','Motor seçimini kaydet','refresh'), status=e('p','','muted');status.setAttribute('role','status');
  save.onclick=async()=>{
    save.disabled=true;
    try{
      const response=await fetch('/control/private/browser',{method:'PUT',headers:{'Content-Type':'application/json','X-Verda-CSRF':data.csrf_token},body:JSON.stringify({driver:select.value})});
      const result=await response.json();if(!response.ok)throw Error(typeof result.detail==='string'?result.detail:'Motor seçilemedi.');
      data.browser=result;render();
    }catch(error){status.textContent=error.message;}finally{save.disabled=false;}
  };
  panel.append(label,save,status,e('p',browser.note,'muted'));root.append(panel);
  const jev=card('Browser Use · Jev Ultrafast'), info=browser.jev;
  const reasons={package_missing:'Paket kurulumu gerekiyor',typesafe_key_missing:'TypeSafe anahtarının yerel dosyadan bağlanması gerekiyor',chrome_connection_missing:'Browser Harness ile Chrome bağlantısı gerekiyor'};
  jev.append(chips([info.installed?'Paket kurulu':'Sabitlenmiş paket kurulumu gerekiyor',info.version?'Sürüm '+info.version:'Kurulu sürüm yok']),e('p',info.scope));
  if(info.last_check)jev.append(e('p','Son bağlantı kontrolü: '+(reasons[info.last_check.reason]||'Bağlantı doğrulandı')+' · '+stamp(info.last_check.checked_at)));
  jev.append(e('p','Jev kararları TypeSafe ile çalışır. Anahtarlar yerel dosyadan yalnız yürütücüye yüklenir. Codex hesabın ana yönetici ve operatör modelleri için kullanılmaya devam eder.'));
  jev.append(e('p','Tek tarayıcı işi · işler arasında en az 180 saniye · en fazla 12 Jev kararı / iş · CAPTCHA ve erişim engelinde durur. Kurulu olması bağlantının açık olduğu anlamına gelmez.','muted'));
  root.append(jev);
  const jobs=card('Tarayıcı işleri');
  for(const job of browser.jobs){const task=data.tasks.find(t=>t.id===job.task_id);const row=e('button',(job.driver==='jev'?'Jev Ultrafast':'Codex Chrome')+' · '+tool(job.tool)+' · '+(states[job.state]||job.state),'task-row');row.onclick=()=>{if(task)showTask(task);};jobs.append(row);}root.append(jobs);
}

function promptEditor(a) {
  const box=e('div',undefined,'prompt-editor');
  const descLabel=e('label','Görev açıklaması'), desc=e('textarea');
  desc.value=a.description;desc.rows=3;desc.maxLength=1000;desc.setAttribute('aria-label',a.label+' görev açıklaması');descLabel.append(desc);
  const promptLabel=e('label','Kalıcı yönerge / system prompt'), prompt=e('textarea');
  prompt.value=a.prompt;prompt.rows=14;prompt.maxLength=12000;prompt.setAttribute('aria-label',a.label+' system prompt');promptLabel.append(prompt);
  const result=e('p','','muted');result.setAttribute('role','status');
  const save=e('button','Değişiklikleri kaydet','refresh'), reset=e('button','Vazgeç','refresh');
  reset.onclick=()=>{desc.value=a.description;prompt.value=a.prompt;result.textContent='Kaydedilmiş yönerge geri yüklendi.';};
  save.onclick=async()=>{
    save.disabled=true;result.textContent='Kaydediliyor…';
    try {
      const response=await fetch('/control/private/agents/'+encodeURIComponent(a.key),{method:'PUT',headers:{'Content-Type':'application/json','X-Verda-CSRF':data.csrf_token},body:JSON.stringify({prompt:prompt.value,description:desc.value,expected_revision:a.edit_revision})});
      const changed=await response.json();if(!response.ok)throw Error(typeof changed.detail==='string'?changed.detail:'Değişiklik kaydedilemedi.');
      Object.assign(a,changed);result.textContent='Kaydedildi · Prompt v'+a.version+' · Yeni görevlerde kullanılacak.';
    }catch(error){result.textContent=error.message;}finally{save.disabled=false;}
  };
  const buttons=e('div',undefined,'editor-actions');buttons.append(save,reset);
  box.append(e('p','Değişiklikler bu Mac’te sürümlü saklanır ve yeni görevlere uygulanır. Başlamış ve bekleyen görevler atandıkları yönergeyi korur. Araç yetkileri bu alandan değişmez.','muted'),descLabel,promptLabel,buttons,result);
  return box;
}

const missingLabels={price_tl:'Fiyat',area_m2:'Alan',parcel_key:'Ada/parsel',natural_sit:'Doğal sit yanıtı',archaeological_sit:'Arkeolojik sit yanıtı',route_minutes:'Mertur’a araç süresi',access:'Yol erişimi'};
let researchOffset=0;
async function requestReview(listingId,button,status){
  button.disabled=true;status.textContent='Yöneticiye iletiliyor…';
  try{
    const response=await fetch('/control/private/research/review',{method:'POST',headers:{'Content-Type':'application/json','X-Verda-CSRF':data.csrf_token},body:JSON.stringify({request_key:crypto.randomUUID(),listing_id:listingId||null})});
    const payload=await response.json();if(!response.ok)throw Error(typeof payload.detail==='string'?payload.detail:'Görev oluşturulamadı.');
    status.textContent='Yönetici kuyruğuna alındı. Kayıtları okuyup bu incelemede en fazla bir ilan için sonraki işi belirleyecek.';
  }catch(error){status.textContent=error.message;button.disabled=false;}
}
async function renderResearch(root){
  const intro=card('İlanlar, eksikler ve hata takibi');
  intro.append(e('p','Ana yönetici kayıtlı bilgiyi ve açık işleri okur, eksik kalan adımı uygun operatöre verir, sonucu değerlendirir. Eski yazışmalardaki cevapları korur; erişim engellerini ve belirsiz gönderimleri otomatik tekrarlamaz.'));
  const review=e('button','Yöneticiye eksikleri incelet','refresh'), status=e('p','','muted');review.onclick=()=>requestReview(null,review,status);intro.append(review,status);root.append(intro);
  const list=card('Kayıtlar yükleniyor…');root.append(list);
  try{
    const response=await fetch('/control/private/research?offset='+researchOffset+'&limit=20');if(!response.ok)throw Error('İlan kayıtları okunamadı.');
    const report=await response.json();if(!list.isConnected)return;list.replaceChildren(e('h2',report.total+' kayıtlı ilan'),e('p',report.note,'muted'));
    for(const item of report.listings){
      const row=e('div',undefined,'lineage');row.append(e('h3',item.title||item.listing_id),e('p','İlan '+item.listing_id,'muted'));
      if(item.url){const link=e('a','İlanı Sahibinden’de aç');link.href=item.url;link.target='_blank';link.rel='noopener';row.append(link);}
      row.append(chips(Object.entries(item.observed).filter(([k])=>['price_tl','area_m2','parcel_key'].includes(k)).map(([k,v])=>(missingLabels[k]||k)+': '+v.toLocaleString('tr-TR'))));
      row.append(e('p',item.archive_review_required?'Eski arşiv mevcut · Yeniden sorgulamadan önce kayıtlı kontrol ve yazışmalar okunacak.':item.missing.length?'Kayıtlı kaynak alanlarında eksik: '+item.missing.map(k=>missingLabels[k]||k).join(', '):'Kaynak alanları mevcut; yönetici değerlendirmesi ayrıca izlenir.','muted'));
      if(item.open_tasks.length)row.append(e('p',item.open_tasks.length+' açık görev var. Aynı iş yeniden açılmaz.','inspector-note'));
      if(item.record_ids.length)row.append(recordButton('observation',{id:item.record_ids[item.record_ids.length-1]},'Kaynak kaydını aç'));
      if(item.trace_id){const trace=e('button','Bu ilanın akışını aç','refresh');trace.onclick=()=>{studioMode='run';studioTrace=item.trace_id;studioSelection='task:'+item.source_task_id;studioCamera=null;view='studio';render();};row.append(trace);}
      const button=e('button','Bu ilanı yöneticiye incelet','refresh'), progress=e('p','','muted');button.onclick=()=>requestReview(item.listing_id,button,progress);
      if(item.lifecycle==='excluded'){button.disabled=true;progress.textContent='Daha önce elenmiş · Yeniden değerlendirme kararı olmadan görev açılmaz.';}
      row.append(button,progress);list.append(row);
    }
    if(report.errors.length){list.append(e('h3','Müdahale bekleyen işler'));for(const item of report.errors)list.append(e('p',agent(item.agent)+' · '+(item.listing_ref||'Genel görev')+' · '+(states[item.state]||item.state)+' · '+(item.reason||'')));}
    const nav=e('div',undefined,'editor-actions');
    if(researchOffset){const prev=e('button','Önceki','refresh');prev.onclick=()=>{researchOffset=Math.max(0,researchOffset-20);render();};nav.append(prev);}
    if(report.has_more){const next=e('button','Sonraki','refresh');next.onclick=()=>{researchOffset+=20;render();};nav.append(next);}list.append(nav);
  }catch(error){list.replaceChildren(e('p',error.message,'empty'));}
}
