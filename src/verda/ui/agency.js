const $ = id => document.getElementById(id);
const e = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
let data, view = 'studio', selected = 'sahibinden', tab = 'overview', taskId = null, toolId = null, listing = '', queueFilter = 'open';
const terminal = new Set(['complete', 'cancelled', 'failed']);
const states = {queued:'Sırada', running:'Çalışıyor', complete:'Tamamlandı', blocked:'Engel / bağlantı bekliyor', waiting_children:'Alt görev sonucu bekliyor', waiting_user:'Kullanıcı girdisi bekliyor', uncertain:'Sonuç belirsiz · tekrar yok', failed:'Başarısız', cancelled:'İptal'};
const kinds = {task_submitted:'Görev kuyruğa alındı', turn_started:'Agent görevi aldı', decision:'Bir sonraki işlem seçildi', delegated:'Başka agent’a görev verildi', tool_started:'Araç çağrıldı', tool_finished:'Araç sonucu alındı', tool_deferred:'Kaynak sırası bekleniyor', task_complete:'Görev tamamlandı', child_result_received:'Alt görev sonucu geldi', child_needs_attention:'Alt görevde engel var', task_blocked:'Görev durdu', task_waiting_user:'Kullanıcı girdisi gerekiyor', resumed:'Kullanıcı girdisiyle devam etti', worker_lost:'Çalışan kesildi'};
Object.assign(kinds, {observation_recorded:'Bulgu ortak kayda yazıldı',outcome_recorded:'Sonuç ortak kayda yazıldı',record_delivered:'Sonuç kuyruğa teslim edildi',record_read:'Kaydedilmiş kanıt okundu'});
const transports = {python:'Statik kod · Python', script:'Statik script', mcp:'MCP aracı', unconnected:'Kaynak adaptörü'};
const agent = key => data.agents.find(a => a.key === key)?.label || key;
const tool = key => data.tools.find(t => t.key === key)?.label || key;
const stamp = x => new Date(x * 1000).toLocaleString('tr-TR');
const json = x => e('pre', JSON.stringify(x, null, 2));
const source = s => s.startsWith('record:') ? 'Ortak kayıt servisi' : s.startsWith('agent:') ? 'Agent: ' + agent(s.slice(6)) : s.startsWith('timer:') ? 'Zamanlayıcı: ' + s.slice(6) : s.startsWith('event:') ? 'Olay: ' + s.slice(6) : 'Kullanıcı';
const count = (key, predicate) => data.task_counts.filter(c => (!key || c.agent === key) && predicate(c)).reduce((n, c) => n + c.count, 0);
function card(title) {
  const n = e('section', undefined, 'agency-card');
  if (title) n.append(e('h2', title));
  return n;
}
function chips(items) {
  const n = e('div', undefined, 'chips');
  items.forEach(x => n.append(e('span', x, 'chip')));
  return n;
}
async function load() {
  try {
    const r = await fetch('/control/private/agency');
    if (!r.ok) throw Error(r.status === 401 ? 'Özel agent merkezi için yerel inceleme bağlantısını açman gerekiyor.' : 'Agent kayıtları yüklenemedi.');
    data = await r.json();
    if (!data.agents.some(a => a.key === selected)) selected = data.agents[0]?.key;
    render();
    $('updated-at').textContent = 'Son okuma: ' + new Date().toLocaleTimeString('tr-TR');
  } catch (err) {
    $('updated-at').textContent='Kayıtlar yenilenemedi · gösterilen veri eski olabilir';
    if(!data)$('content').replaceChildren(e('p', err.message, 'empty'));
    checkHealth();
  }
}
function runtimeStatus() {
  const bar = $('runtime-status');
  bar.replaceChildren();
  const local = data.workers.filter(w => w.mode === 'local' && w.active);
  const synthetic = data.workers.filter(w => w.mode === 'synthetic' && w.active);
  const items = [['Yerel panel', 'Açık', true], ['Görev yürütücüsü', local.length ? 'Aktif bildirim var' : 'Aktif bildirim yok', !!local.length], ['Browser', 'Henüz bağlı değil', false], ['Kayıtlı görevler', data.total_tasks + ' · ' + count(null, c => !terminal.has(c.state)) + ' açık', true]];
  items.forEach(([label, value, ready]) => {
    const n = e('div', undefined, 'runtime-item ' + (ready ? 'ready' : 'pending'));
    n.append(e('small', label), e('strong', value)); bar.append(n);
  });
  if (synthetic.length) bar.append(e('p', 'Örnek veri yürütücüsü aktif · canlı kaynak işi değil.', 'muted'));
}
function taskButton(t) {
  const b = e('button', undefined, 'task-row' + (t.id === taskId ? ' selected' : ''));
  b.append(e('strong', t.objective), e('small', (states[t.state] || t.state) + ' · ' + source(t.source)), e('small', (t.listing_ref || 'İlan bağı yok') + ' · Öncelik ' + t.priority + ' · ' + (t.mode === 'synthetic' ? 'ÖRNEK VERİ' : 'Yerel görev')));
  if (t.executor_tool) b.append(e('small', 'Model kullanmaz → ' + tool(t.executor_tool)));
  b.onclick = () => { taskId = t.id; render(); };
  return b;
}
function taskDetail(t) {
  const n = card('Seçili görev');
  if (!t) { n.append(e('p', 'Ayrıntısını görmek için bir görev seç.', 'muted')); return n; }
  n.append(e('p', t.objective), chips([agent(t.agent), states[t.state] || t.state, source(t.source), t.executor_tool ? 'Statik görev' : 'Agent kararı']), e('p', 'İlan: ' + (t.listing_ref || 'Belirtilmedi') + ' · ' + stamp(t.created_at), 'muted'));
  if(t.retry)n.append(e('p','Geçici okuma hatası · '+t.retry.attempt+'/'+t.retry.max_attempts+' deneme · Sonraki deneme: '+stamp(t.retry.next_at),'inspector-note'));
  if (t.reason) n.append(e('p', 'Bekleme / durma nedeni: ' + t.reason, 'empty'));
  if (t.state === 'queued' && !data.workers.some(w => w.active && w.mode === t.mode)) n.append(e('p', 'Bu çalışma türü için aktif yürütücü bildirimi yok. Görev kuyrukta saklanıyor.', 'empty'));
  if (t.parent_id) {
    const parent = data.tasks.find(x => x.id === t.parent_id);
    n.append(e('p', 'Görevi veren: ' + (parent ? agent(parent.agent) + ' — ' + parent.objective : t.parent_id), 'muted'));
  }
  taskRecordLinks(n, t);
  n.append(e('h3', 'Atanan girdi'), json(t.inputs));
  if (t.result) n.append(e('h3', 'Kaydedilen sonuç'), json(t.result));
  n.append(e('h3', 'İşlem izi'));
  data.events.filter(x => x.task_id === t.id).forEach(x => {
    const row = e('details', undefined, 'event');
    row.append(e('summary', (kinds[x.kind] || x.kind) + ' · ' + stamp(x.at)));
    if (x.kind === 'decision') row.append(e('p', x.data.decision.summary), chips([x.data.generation.provider || 'Normal kod', x.data.generation.executor === 'code' ? 'Model çağrısı yok' : x.data.generation.model_requested || 'Sağlayıcı varsayılanı', 'Prompt v' + x.data.prompt_version]));
    if (x.kind === 'tool_started') row.append(e('p', tool(x.data.tool) + ' · ' + (transports[x.data.transport] || x.data.transport)));
    row.append(json(x.data)); n.append(row);
  });
  if (data.events_truncated) n.append(e('p', 'En son 1.000 olay gösteriliyor; eski olaylar veritabanında korunuyor.', 'muted'));
  return n;
}
function toolDetail(t) {
  const n = card(t.label);
  n.append(chips([transports[t.transport], t.status_label, t.effect === 'write' ? 'Yazma işlemi' : 'Okuma / hesaplama']), e('p', t.description));
  n.append(e('p', 'Yetkili agent’lar: ' + data.agents.filter(a => a.tools.includes(t.key)).map(a => a.label).join(', '), 'muted'));
  if (t.implementation.entrypoint) n.append(e('p', 'Çalışan kod: ' + t.implementation.entrypoint, 'implementation'));
  if (t.implementation.script_name) n.append(e('p', 'Script: ' + t.implementation.script_name, 'implementation'));
  if (t.implementation.remote_name) n.append(e('p', 'MCP araç adı: ' + t.implementation.remote_name, 'implementation'));
  if (t.resource) {
    const resource = data.resources.find(r => r.key === t.resource);
    n.append(e('p', 'Ortak kaynak: ' + t.resource + ' · En az ' + t.min_interval_seconds + ' saniye aralık', 'muted'));
    if (resource?.halted) n.append(e('p', 'Kaynak durduruldu: ' + resource.halted, 'empty'));
    if (resource?.next_at > Date.now() / 1000) n.append(e('p', 'Sonraki izin zamanı: ' + stamp(resource.next_at), 'muted'));
  }
  const d = e('details', undefined, 'event'); d.append(e('summary', 'Aracın aldığı veri'), json(t.input_schema)); n.append(d);
  return n;
}
function connectionMap(a) {
  const n = card('Agent ve kullanabildiği araçlar');
  n.append(e('p', 'Çizgiler araç kullanma yetkisini gösterir. Bir araca tıklayarak uygulamasını ve bağlantı durumunu incele.', 'muted'));
  const map = e('div', undefined, 'agent-map'), actor = e('div', undefined, 'map-actor'), targets = e('div', undefined, 'map-targets');
  actor.append(e('small', 'AGENT'), e('strong', a.label), e('span', a.model.provider + ' · ' + (a.model.model || 'Varsayılan model')), e('span', count(a.key, c => !terminal.has(c.state)) + ' açık görev'));
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('aria-hidden', 'true');
  map.append(svg, actor, targets);
  a.tools.forEach(key => {
    const t = data.tools.find(t => t.key === key), b = e('button', undefined, 'map-tool ' + t.status + (toolId === key ? ' selected' : ''));
    b.append(e('strong', t.label), e('small', transports[t.transport]), e('span', t.status_label));
    b.onclick = () => { toolId = key; taskId = null; render(); }; targets.append(b);
  });
  if (!a.tools.length) targets.append(e('p', 'Atanmış araç yok.', 'empty'));
  n.append(map);
  const resources = new Set(a.tools.map(k => data.tools.find(t => t.key === k).resource));
  data.connections.filter(c => resources.has(c.key) && c.kind === 'browser').forEach(c => {
    const box = e('div', undefined, 'browser-connection'); box.append(e('strong', c.label), e('p', c.note), e('small', 'Araçların kaydı bu browser bağlantısını kendiliğinden açmaz.')); n.append(box);
  });
  const draw = () => {
    if (!map.isConnected) return;
    svg.replaceChildren();
    const root = map.getBoundingClientRect(), from = actor.getBoundingClientRect();
    targets.querySelectorAll('.map-tool').forEach(target => {
      const to = target.getBoundingClientRect(), path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      const x1 = from.right - root.left, y1 = from.top + from.height / 2 - root.top, x2 = to.left - root.left, y2 = to.top + to.height / 2 - root.top, mid = (x1 + x2) / 2;
      path.setAttribute('d', `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`); svg.append(path);
    });
  };
  requestAnimationFrame(draw); window.onresize = draw;
  return n;
}
function taskQueue(a) {
  const n = card('Görev kuyruğu'), filters = e('div', undefined, 'agent-tabs');
  [['open','Bekleyen / çalışan'], ['history','Tamamlanan / kapanan'], ['all','Hepsi']].forEach(([key,label]) => {
    const b = e('button', label, queueFilter === key ? 'active' : ''); b.onclick = () => { queueFilter = key; taskId = null; render(); }; filters.append(b);
  });
  n.append(filters, e('p', 'Öncelik sırasına göre gösterilir. Örnek görevler ayrıca işaretlidir.', 'muted'));
  const tasks = data.tasks.filter(t => t.agent === a.key && (queueFilter === 'all' || terminal.has(t.state) === (queueFilter === 'history'))).sort((x,y) => y.priority - x.priority || x.created_at - y.created_at);
  tasks.forEach(t => n.append(taskButton(t)));
  if (!tasks.length) n.append(e('p', queueFilter === 'open' ? 'Bu agent’ın görüntülenen kayıtlarında bekleyen görev yok.' : 'Bu görünümde görev yok.', 'empty'));
  return n;
}
function renderAgents(root) {
  const grid = e('div', undefined, 'agency-grid'), side = e('aside', undefined, 'agency-side'), body = e('div');
  data.agents.forEach(a => {
    const b = e('button', undefined, a.key === selected ? 'active' : '');
    b.append(e('strong', a.label), e('small', count(a.key, c => !terminal.has(c.state)) + ' açık iş · ' + a.tools.length + ' araç'));
    b.onclick = () => { selected = a.key; taskId = null; toolId = null; tab = 'overview'; render(); }; side.append(b);
  });
  const a = data.agents.find(a => a.key === selected), head = card(a.label);
  head.append(e('p', a.description), chips([a.model.provider, a.model.model || 'Varsayılan model', 'Prompt v' + a.version, a.max_turns + ' karar turu sınırı']));
  const tabs = e('div', undefined, 'agent-tabs');
  [['overview','Araçlar ve görevler'], ['prompt','Yönerge / prompt'], ['delegates','Görev verebildiği agent’lar']].forEach(([key,label]) => {
    const b = e('button', label, tab === key ? 'active' : ''); b.onclick = () => { tab = key; render(); }; tabs.append(b);
  }); head.append(tabs); body.append(head);
  if (tab === 'prompt') { const p = card('Çalışma yönergesi'); p.append(e('p', 'Agent yönergesi ve ortak çalışma protokolü. Her görev tanımın sürümlü kopyasını saklar.', 'muted'), e('pre', a.prompt + '\n\n' + data.protocol_prompt)); body.append(p); }
  else if (tab === 'delegates') {
    const p = card('Görev devri');
    a.delegates.forEach(key => { const b = e('button', agent(key), 'task-row'); b.onclick = () => { selected = key; tab = 'overview'; toolId = null; taskId = null; render(); }; p.append(b); });
    if (!a.delegates.length) p.append(e('p', 'Bu agent kendi araçlarıyla çalışır; başka agent’a görev verme yetkisi yok.', 'muted')); body.append(p);
  } else {
    body.append(connectionMap(a));
    if (toolId && a.tools.includes(toolId)) body.append(toolDetail(data.tools.find(t => t.key === toolId)));
    body.append(taskQueue(a)); if (taskId) body.append(taskDetail(data.tasks.find(t => t.id === taskId)));
  }
  grid.append(side, body); root.append(grid);
}
function renderTrace(root) {
  const heading = card(), bar = e('div', undefined, 'trace-heading'); bar.append(e('h2', 'İlan / görev izi'));
  const select = e('select'); select.setAttribute('aria-label', 'İlan filtresi'); select.append(new Option('Bütün görevler', ''));
  [...new Set(data.tasks.map(t => t.listing_ref).filter(Boolean))].forEach(k => select.append(new Option(k,k))); select.value = listing;
  select.onchange = () => { listing = select.value; taskId = null; render(); }; bar.append(select);
  heading.append(bar, e('p', 'Her satır bir görevdir. Sorumlu agent ve altında çağrılan araçlar ayrı kaydedilir.', 'muted')); root.append(heading);
  const layout = e('div', undefined, 'trace-layout'), list = card('Görev devri ve sonuç dönüşleri');
  const tasks = data.tasks.filter(t => !listing || t.listing_ref === listing).sort((a,b) => a.created_at - b.created_at);
  tasks.forEach(t => {
    const row = e('div', undefined, 'lineage'), parent = data.tasks.find(p => p.id === t.parent_id);
    row.append(e('small', (t.parent_id ? (parent ? agent(parent.agent) : 'Üst görev') : source(t.source)) + ' → ' + agent(t.agent)), taskButton(t)); list.append(row);
  });
  if (!tasks.length) list.append(e('p', 'Bu ilan için yeni agent kaydı yok.', 'empty'));
  layout.append(list, taskDetail(data.tasks.find(t => t.id === taskId))); root.append(layout);
}
function render() {
  studioCleanup();
  document.body.classList.toggle('studio-view', view === 'studio');
  runtimeStatus(); const nav = $('views'); nav.replaceChildren();
  [['studio','Akış tuvali'], ['agents','Agent listesi'], ['trace','Görev kayıtları'], ['records','Ortak kayıtlar'], ['events','Bildirimler ve hatalar'], ['tools','Tüm araçlar ve scriptler'], ['triggers','Tetikleyiciler']].forEach(([key,label]) => {
    const b = e('button', label, 'tab' + (view === key ? ' selected' : '')); b.onclick = () => { view = key; taskId = null; render(); }; nav.append(b);
  });
  const root = $('content'); root.replaceChildren();
  if (data.tasks_truncated) root.append(e('p', 'Son 200 görev gösteriliyor. Agent sayaçları tüm kayıtları kapsar; eski görevler veritabanında korunuyor.', 'empty'));
  if (view === 'studio') renderStudio(root);
  if (view === 'agents') renderAgents(root);
  if (view === 'trace') renderTrace(root);
  if (view === 'records') renderRecords(root);
  if (view === 'events') renderEventCenter(root);
  if (view === 'tools') {
    const scripts = data.tools.filter(t => t.transport === 'script');
    root.append(e('p', scripts.length + ' bağımsız script · ' + data.tools.filter(t => t.transport === 'python').length + ' statik Python aracı · ' + data.tools.filter(t => t.transport === 'mcp').length + ' MCP aracı tanımlı.', 'statusline'));
    const grid = e('div', undefined, 'tool-grid'); data.tools.forEach(t => grid.append(toolDetail(t))); root.append(grid);
  }
  if (view === 'triggers') {
    renderTriggerCatalog(root);
  }
}
const deliveryStates={pending:'Teslim bekliyor',delivered:'Kuyruğa teslim edildi',blocked:'Teslim engellendi'};
function recordButton(kind, record, label) {
  const b=e('button',label,'task-row');
  b.onclick=()=>{view='records';recordSelection={kind,id:record.id};render();};return b;
}
let recordSelection=null;
function taskRecordLinks(n,t){
  if(t.caused_by_task_id)n.append(e('p','Bu değerlendirme, ortak kayıt servisinin teslim ettiği bir sonuçtan doğdu.','muted'));
  const observations=(data.records?.observations||[]).filter(o=>o.task_id===t.id);
  const outcomes=(data.records?.outcomes||[]).filter(o=>o.task_id===t.id);
  if(observations.length||outcomes.length)n.append(e('h3','Ortak kayıtlar'));
  observations.forEach(o=>n.append(recordButton('observation',o,'Bulgu · '+tool(o.tool))));
  outcomes.forEach(o=>n.append(recordButton('outcome',o,'Sonuç v'+o.version+' · '+(states[o.state]||o.state))));
}
async function recordInspector(n,kind,id){
  const request=kind+':'+id;n.dataset.recordRequest=request;
  n.append(e('p','Kayıt yükleniyor…','muted'));
  try{
    const response=await fetch('/control/private/records/'+encodeURIComponent(kind)+'/'+encodeURIComponent(id));
    if(!response.ok)throw Error('Kayıt okunamadı; yerel oturumunu kontrol et.');
    const r=await response.json();if(!n.isConnected||n.dataset.recordRequest!==request)return;n.replaceChildren();
    n.append(e('h2',kind==='observation'?'Kaynak bulgusu':'Kaydedilen görev sonucu'),chips([agent(r.agent),r.mode==='synthetic'?'ÖRNEK VERİ':'Yerel kayıt',r.listing_ref||'İlan bağı yok']),e('p','Kayıt zamanı: '+stamp(r.captured_at||r.recorded_at),'muted'));
    if(kind==='observation'){
      n.append(e('p','Bu kayıt aracın bildirdiği veridir; bağımsız doğrulama veya ilan kararı değildir. Eski bulguların üzerine yazılmaz.','inspector-note'),e('h3',tool(r.tool)),json(r.fields),e('h3','Kaynak çıktısı'),json(r.payload));
    }else{
      n.append(e('p',states[r.state]||r.state));if(r.reason)n.append(e('p',r.reason));
      n.append(deliveryList(r.deliveries||[]));
      n.append(e('h3','Sonuç / yorum'),json(r.result),e('h3','İlişkili kaynak bulguları · '+r.observations.length));
      r.observations.forEach(o=>{const d=e('details',undefined,'event');d.append(e('summary',tool(o.tool)+' · '+stamp(o.captured_at)),e('p','Kaynak bildirimi; bağımsız doğrulanmış bilgi değil.','muted'),json(o.payload));n.append(d);});
    }
    if(kind==='observation')n.append(deliveryList(r.deliveries||[]));
    const raw=e('details',undefined,'event');raw.append(e('summary','Kayıt kimliği ve bütün ayrıntılar'),json(r));n.append(raw);
  }catch(err){if(n.isConnected&&n.dataset.recordRequest===request)n.replaceChildren(e('p',err.message,'empty'));}
}
function renderRecords(root){
  const records=data.records, heading=card('Ortak kayıt servisi');
  heading.append(e('p','Araç bulgusu → kalıcı kayıt → görev sonucu → yönetici kuyruğu. Yazmayı normal kod yapar; değerlendirmeyi agent yapar.'),chips([records.observation_count+' bulgu',records.outcome_count+' sonuç',records.pending_count+' teslim bekliyor',records.blocked_count+' teslim engeli']));root.append(heading);
  const layout=e('div',undefined,'trace-layout'), list=card('Sonuçlar ve bulgular'), detail=card();
  records.outcomes.forEach(r=>list.append(recordButton('outcome',r,(r.listing_ref||agent(r.agent))+' · '+(r.kind==='manager_decision'?'Yönetici kararı':'Görev sonucu')+' · '+(states[r.state]||r.state)+(r.mode==='synthetic'?' · ÖRNEK':''))));
  records.observations.forEach(r=>list.append(recordButton('observation',r,(r.listing_ref||agent(r.agent))+' · '+tool(r.tool)+(r.mode==='synthetic'?' · ÖRNEK':''))));
  list.append(e('p','Görüntülenen görevlerin en son 200 bulgusu ve 200 sonucu. Önceki sürümde bitmiş görevler geriye dönük işletilmez.','muted'));
  layout.append(list,detail);root.append(layout);
  if(recordSelection)recordInspector(detail,recordSelection.kind,recordSelection.id);else detail.append(e('p','Kaynak çıktısını ve yöneticiye teslimini görmek için bir kayıt seç.'));
}
Object.assign(kinds,{task_retry_scheduled:'Okuma yeniden denenecek',source_resumed:'Kaynak erişimi düzeltildi'});
const processingStates={waiting:'Değerlendirme bekliyor',processing:'İşlemeye başladı',processed:'Değerlendirdi',attention:'Değerlendirme müdahale bekliyor',observed:'Bilgi olarak alındı · model uyandırmaz',legacy:'Eski teslim · işleme onayı tutulmamış'};
function showTask(t){view='studio';studioMode='run';studioTrace=t.trace_id;studioSelection='task:'+t.id;studioCamera=null;render();}
function deliveryList(deliveries){
  const n=e('section',undefined,'inspector-section');n.append(e('h3','Alıcılar ve teslim durumu'));
  if(!deliveries.length)n.append(e('p','Abone alıcı yok. Kayıt işlem izinde korunuyor.','muted'));
  deliveries.forEach(d=>{
    const row=e('div',undefined,'lineage');row.append(e('strong',agent(d.target_agent)),e('p',(deliveryStates[d.state]||d.state)+' · '+(processingStates[d.processing_state]||'')),e('small',d.wake?'Değerlendirme tetikler':'Bilgi bildirimi'));
    if(d.error)row.append(e('p','Teslim hatası: '+d.error+' · '+d.attempts+' deneme','empty'));
    if(d.state==='pending'&&d.attempts)row.append(e('p','Sonraki teslim denemesi: '+stamp(d.available_at),'muted'));
    if(d.state==='blocked')row.append(e('p','Kayıt korunuyor. Neden giderildikten sonra yerel teslim tekrar komutuyla devam ettirilebilir.','muted'));
    [['Kaydedildi',d.created_at],['Teslim edildi',d.delivered_at],['İşlemeye başladı',d.started_at],['Değerlendirdi',d.processed_at]].forEach(([label,value])=>{if(value!=null)row.append(e('small',label+': '+stamp(value)));});
    if(d.target_task_id){const t=data.tasks.find(t=>t.id===d.target_task_id);if(t){const b=e('button','Alıcının görevini aç','task-row');b.onclick=()=>showTask(t);row.append(b);}}
    n.append(row);
  });return n;
}
function subscriptionList(target){
  const n=e('section',undefined,'inspector-section');n.append(e('h3','Olay abonelikleri'));
  (data.subscriptions||[]).filter(s=>!target||s.target_agent===target).forEach(s=>{
    const row=e('details',undefined,'event');row.append(e('summary',(s.builtin?'Yönetici bildirimleri':s.key)+' → '+agent(s.target_agent)+(s.enabled?' · Etkin':' · Pasif')),e('p',s.wake==='outcomes_and_incidents'?'Bulguları bilgi olarak alır; görev sonuçları ve ilk kaynak engeli değerlendirme başlatır. Kendi çıktısıyla yeniden uyanmaz.':s.wake?'Eşleşen olay değerlendirme başlatır.':'Eşleşen olay bilgi olarak kaydedilir.'),e('p','Mod: '+s.mode+' · Kaynak agent: '+(s.source_agents.length?s.source_agents.map(agent).join(', '):'Tümü')),e('p',s.event_types.join(', ')));n.append(row);
  });return n;
}
const eventLabels={'observation.recorded':'Bulgu kaydedildi','task.complete':'Görev tamamlandı','task.blocked':'Görev engellendi','task.uncertain':'İşlem sonucu belirsiz','task.failed':'Görev başarısız','task.cancelled':'Görev iptal edildi','task.waiting_user':'Kullanıcı girdisi gerekiyor','task.retry_scheduled':'Okuma yeniden denenecek','source.blocked':'Kaynak erişimi durdu','source.recovered':'Kaynak erişimi düzeltildi'};
function renderEventCenter(root){
  const head=card('Bildirimler ve hata yönetimi');head.append(e('p','Her alıcının teslimi ayrı izlenir. Bilgi bildirimi model çağrısı oluşturmaz; değerlendirme görevleri kendi işlem durumunu taşır.'),subscriptionList());root.append(head);
  const policy=card('Uygulanan tekrar kuralları');policy.append(e('p','Geçici okumalarda toplam '+data.error_policy.read_attempts+' deneme. Beklemeler: '+data.error_policy.read_delays.join(' / ')+' saniye. Kaynak aralığı veya adaptörün bekleme süresi daha uzunsa o uygulanır.'),e('p','Geçici teslim hatalarında toplam '+data.error_policy.delivery_attempts+' deneme. Beklemeler: '+data.error_policy.delivery_delays.join(' / ')+' saniye. Eksik alıcı veya yapılandırma hatası doğrudan incelemeye ayrılır.'),e('p','Belirsiz dış yazma işlemi otomatik tekrarlanmaz. Kaynak engeli erişim düzeltildiği doğrulanana kadar bekler.'));root.append(policy);
  const incidents=card('Kaynak olayları');
  (data.incidents||[]).forEach(i=>{
    const row=e('details',undefined,'event');row.append(e('summary',i.resource+' · '+(i.state==='open'?'Engel açık':'Düzeltildi')+' · '+i.tasks.length+' görev'+(i.mode==='synthetic'?' · ÖRNEK':'')),e('p','Neden: '+i.reason),e('p','Başlangıç: '+stamp(i.created_at)));
    if(i.resolution)row.append(e('p','Düzeltme kaydı: '+i.resolution));
    i.tasks.forEach(id=>{const t=data.tasks.find(t=>t.id===id);if(t){const b=e('button',agent(t.agent)+' · '+(states[t.state]||t.state)+' · '+(t.listing_ref||t.objective),'task-row');b.onclick=()=>showTask(t);row.append(b);}});incidents.append(row);
  });if(!(data.incidents||[]).length)incidents.append(e('p','Kayıtlı kaynak engeli yok.','muted'));root.append(incidents);
  const events=card('Olaylar ve alıcıları · son 200 kayıt');
  (data.records.events||[]).forEach(event=>{
    const row=e('details',undefined,'event');row.append(e('summary',(eventLabels[event.type]||event.type)+' · '+agent(event.agent)+' · '+(event.listing_ref||'İlan bağı yok')+(event.mode==='synthetic'?' · ÖRNEK':'')),e('p',stamp(event.created_at)),json(event.data),deliveryList(data.records.deliveries.filter(d=>d.event_id===event.id)));
    if(['observation','outcome'].includes(event.record_kind))row.append(recordButton(event.record_kind,{id:event.record_id},'İlgili kaydı aç'));
    events.append(row);
  });root.append(events);
}
async function checkHealth(){
  const n=$('system-health');
  try{
    const response=await fetch('/control/private/health');
    if(response.status===401){n.replaceChildren(e('p','Yerel görüntüleme oturumu kapalı veya süresi dolmuş.','empty'));return;}
    if(!response.ok)throw Error('health');
    const health=await response.json();
    const failures=health.workers.filter(w=>w.state==='storage_error');
    n.replaceChildren();
    if(!health.storage_accessible)n.append(e('p','Kayıt deposuna erişilemiyor. Görev yürütme duraklatıldı; mevcut ekran eski kayıtları gösteriyor olabilir.','empty'));
    else if(failures.length)n.append(e('p','Depoya erişilebiliyor. Bir yürütücü kayıt hatası bildirmiş; yarım kalan işler ve son durum kontrol edilmeli.','empty'));
    else n.append(e('small','Sağlık kontrolü: kayıt deposuna erişilebiliyor · '+stamp(health.checked_at),'muted'));
  }catch(err){n.replaceChildren(e('p','Yerel sunucuya ulaşılamıyor. Ekrandaki kayıtlar güncel olmayabilir.','empty'));}
}
$('refresh').onclick=()=>{load();checkHealth();};load();checkHealth();
setInterval(()=>{if(!document.hidden)checkHealth();},15000);
