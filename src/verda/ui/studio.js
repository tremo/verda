/* Interactive inspection canvas. Moving nodes only changes this browser view. */
let studioMode = 'topology', studioSelection = 'agent:sahibinden', studioTrace = '', studioCamera = null;
const studioPositions = new Map();
let studioCleanup = () => {};

function studioButton(label, action, cls = 'studio-link') {
  const b = e('button', label, cls); b.type = 'button'; b.onclick = action; return b;
}
function studioFields(items) {
  const list = e('dl', undefined, 'studio-fields');
  items.forEach(([label, value]) => list.append(e('dt', label), e('dd', value))); return list;
}
function studioSection(title, text) {
  const n = e('section', undefined, 'inspector-section'); n.append(e('h3', title));
  if (text) n.append(e('p', text)); return n;
}
function openStudio(id) {
  studioMode = 'topology'; studioSelection = id; view = 'studio'; render();
}
function triggerInspector(t, select) {
  const n = e('div'), a = data.agents.find(a => a.key === t.spec.agent);
  n.append(chips([t.enabled ? 'Etkin' : 'Pasif', t.spec.mode === 'synthetic' ? 'ÖRNEK VERİ' : 'Yerel görev']));
  n.append(studioButton('Hedef agent → ' + agent(t.spec.agent), () => select('agent:' + t.spec.agent)));
  const prompt = studioSection('Agent’a gönderilen görev / prompt');
  prompt.append(e('pre', t.spec.objective, 'inspector-prompt')); n.append(prompt);
  if (t.kind === 'timer') {
    const schedule = VerdaGraph.timerDetails(t);
    n.append(studioFields([
      ['Kayıtlı tetikleme saati', schedule.clock + ' · İstanbul'],
      ['Tekrar aralığı', 'Her ' + schedule.interval],
      [t.enabled ? 'Sıradaki kayıtlı zaman' : 'Pasif kayıttaki zaman', schedule.next],
      ['Saat gösterimi', schedule.zone + ' (UTC+03:00)'],
      ['Çalışma süresi', 'Toplam süre sınırı tanımlı değil'],
    ]));
    n.append(e('p', t.enabled ? 'Zamanı gelince görev kuyruğa eklenir. İşin başlaması yürütücüye ve agent’ın müsaitliğine bağlıdır.' : 'Bu zamanlayıcı pasif. Saat geldiğinde görev oluşturmaz.', 'inspector-note'));
    n.append(e('p', 'Bu kayıt sabit aralıkla tekrar eder; saat dilimine göre cron takvimi değildir. Kaçırılan aralıklar tek görevde birleştirilir.', 'muted'));
  } else {
    n.append(studioFields([['Dinlenen olay', t.spec.event_type], ['Görev girdisi', 'Gelen olayın veri gövdesi'], ['Çalışma süresi', 'Toplam süre sınırı tanımlı değil']]));
  }
  if (t.spec.executor_tool) {
    n.append(e('p', 'Bu tetikleyici doğrudan statik araç işi oluşturur. Görev metni model prompt’u olarak çalıştırılmaz.', 'inspector-note'));
    n.append(studioButton('Doğrudan araç → ' + tool(t.spec.executor_tool), () => select('tool:' + t.spec.executor_tool)));
  } else if (a) {
    n.append(e('p', 'Bu görev metni agent’ın kalıcı yönergesi ve çalışma protokolüyle birlikte kullanılır.', 'muted'));
  }
  n.append(studioFields([['Öncelik', String(t.spec.priority ?? (t.kind === 'timer' ? 20 : 50))],
    ['Karar sınırı', t.spec.executor_tool ? 'Model çağrısı yok' : (a?.max_turns ?? '—') + ' tur / görev'],
    ['Tek model çağrısının süresi', t.spec.executor_tool ? 'Uygulanmaz' : VerdaGraph.duration(a?.model.timeout_seconds)]]));
  const input = studioSection(t.kind === 'timer' ? 'Görevle gönderilen girdi' : 'Olaydan gelen girdi');
  if (t.kind === 'timer') input.append(json(t.spec.inputs || {}));
  else input.append(e('p', 'Her olayın veri gövdesi çalışma anında görev girdisi olur; sabit örnek veri kullanılmaz.'));
  n.append(input);
  const recent = data.tasks.filter(task => task.source === (t.kind === 'timer' ? 'timer:' + t.key : 'event:' + t.spec.event_type));
  const history = studioSection(t.kind === 'timer' ? 'Bu zamanlayıcının oluşturduğu görevler' : 'Aynı olay türünden gelen görevler');
  recent.forEach(task => history.append(studioButton((states[task.state] || task.state) + ' · ' + task.objective, () => select('task:' + task.id))));
  if (!recent.length) history.append(e('p', 'Görüntülenen kayıtlarda görev yok.', 'muted'));
  n.append(history); return n;
}

function renderStudio(root) {
  studioCleanup();
  const shell = e('section', undefined, 'studio-shell');
  const toolbar = e('div', undefined, 'studio-toolbar');
  const modes = e('div', undefined, 'studio-modes');
  [['topology','Bağlantı haritası'], ['run','Çalışma izi']].forEach(([key, label]) => modes.append(studioButton(label, () => {
    studioMode = key; studioSelection = ''; studioCamera = null; render();
  }, studioMode === key ? 'active' : '')));
  toolbar.append(modes);
  if (studioMode === 'run') {
    const picker = e('select'); picker.setAttribute('aria-label', 'İzlenecek çalışma');
    const traces = [...new Set(data.tasks.map(t => t.trace_id))];
    if (!traces.includes(studioTrace)) studioTrace = traces.find(id => data.tasks.filter(t => t.trace_id === id).length > 1) || traces[0] || '';
    traces.forEach(id => {
      const tasks = data.tasks.filter(t => t.trace_id === id), first = tasks.find(t => !t.parent_id) || tasks[0];
      picker.append(new Option((first.listing_ref || first.objective.slice(0,40)) + ' · ' + tasks.length + ' görev' + (first.mode === 'synthetic' ? ' · ÖRNEK' : '') + ' · ' + stamp(first.created_at), id));
    });
    picker.value = studioTrace;
    picker.onchange = () => { studioTrace = picker.value; studioSelection = ''; studioCamera = null; render(); };
    toolbar.append(picker);
  } else toolbar.append(e('span', 'Kayıtlı tetikleyiciler → agent’lar → araçlar → kaynaklar', 'studio-caption'));
  const controls = e('div', undefined, 'studio-controls'); toolbar.append(controls);
  const layout = e('div', undefined, 'studio-layout');
  const viewport = e('div', undefined, 'studio-viewport'); viewport.tabIndex = 0; viewport.setAttribute('aria-label', 'Agent bağlantı tuvali');
  const world = e('div', undefined, 'studio-world');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.classList.add('studio-edges'); svg.setAttribute('aria-hidden', 'true');
  world.append(svg); viewport.append(world);
  const panel = e('aside', undefined, 'studio-inspector'); panel.setAttribute('aria-label', 'Seçili düğüm ayrıntıları');
  const legend = e('div', undefined, 'studio-legend');
  const legendItems = studioMode === 'topology' ? [['trigger','Görev oluşturur'],['delegate','Görev verebilir'],['tool','Araç yetkisi'],['resource','Ortak kaynak']] : [['trigger','Görev oluştu'],['delegated','Devredildi'],['executed','Araç çağrıldı'],['returned','Sonuç döndü']];
  legendItems.forEach(([kind,label]) => legend.append(e('span', label, 'legend-' + kind)));
  const note = e('p', studioMode === 'topology' ? 'Bağlar tanımlı yetki ve hedefleri gösterir. Düğüme tıkla; boşluğu sürükleyerek gezin. Düğümleri taşıyabilirsin.' : 'Bağlar kaydedilmiş işlemleri gösterir. Göreve tıklayarak devredilen girdiyi ve sonucu incele.' + (data.events_truncated ? ' Son 1.000 olay gösterildiği için eski çağrı ve dönüşler eksik olabilir.' : ''), 'studio-help');
  layout.append(viewport, panel); shell.append(toolbar, legend, layout, note); root.append(shell);
  const graph = studioMode === 'topology' ? VerdaGraph.topology(data) : VerdaGraph.run(data, studioTrace);
  const graphKey = studioMode + ':' + studioTrace;
  graph.nodes.forEach(n => { const pos = studioPositions.get(graphKey + ':' + n.id); if (pos) Object.assign(n, pos); });
  const nodes = new Map(graph.nodes.map(n => [n.id, n]));
  const buttons = new Map(); let gesture = null, suppressClick = false, disposed = false;
  const camera = studioCamera?.key === graphKey ? studioCamera : {key:graphKey, x:25, y:25, scale:1};
  studioCamera = camera;
  function transform() { world.style.transform = `translate(${camera.x}px, ${camera.y}px) scale(${camera.scale})`; zoomLabel.textContent = Math.round(camera.scale * 100) + '%'; }
  function zoom(scale) {
    const next = Math.max(.2, Math.min(1.8, scale)), x = viewport.clientWidth / 2, y = viewport.clientHeight / 2;
    camera.x = x - (x - camera.x) * next / camera.scale; camera.y = y - (y - camera.y) * next / camera.scale; camera.scale = next; transform();
  }
  function fit() {
    if (!graph.nodes.length || disposed) return;
    const minX = Math.min(...graph.nodes.map(n => n.x)) - 35, minY = Math.min(...graph.nodes.map(n => n.y)) - 45;
    const width = Math.max(...graph.nodes.map(n => n.x + n.width)) - minX + 35;
    const height = Math.max(...graph.nodes.map(n => n.y + n.height)) - minY + 45;
    camera.scale = Math.max(.2, Math.min(1, (viewport.clientWidth - 40) / width, (viewport.clientHeight - 40) / height));
    camera.x = (viewport.clientWidth - width * camera.scale) / 2 - minX * camera.scale;
    camera.y = (viewport.clientHeight - height * camera.scale) / 2 - minY * camera.scale; transform();
  }
  const zoomLabel = e('span', undefined, 'zoom-label');
  controls.append(studioButton('−', () => zoom(camera.scale / 1.2), 'zoom-button'), zoomLabel,
    studioButton('+', () => zoom(camera.scale * 1.2), 'zoom-button'), studioButton('Sığdır', fit, 'zoom-button'),
    studioButton('Yerleşimi sıfırla', () => { for (const key of studioPositions.keys()) if (key.startsWith(graphKey + ':')) studioPositions.delete(key); studioCamera = null; render(); }, 'zoom-button'));
  function drawEdges() {
    svg.replaceChildren();
    const defs = document.createElementNS(svg.namespaceURI, 'defs');
    for (const kind of ['trigger','delegate','tool','resource','delegated','executed','returned']) {
      const marker = document.createElementNS(svg.namespaceURI, 'marker'); marker.id = 'arrow-' + kind;
      for (const [key,value] of Object.entries({viewBox:'0 0 10 10',refX:'9',refY:'5',markerWidth:'7',markerHeight:'7',orient:'auto-start-reverse'})) marker.setAttribute(key,value);
      const triangle = document.createElementNS(svg.namespaceURI, 'path'); triangle.setAttribute('d','M 0 0 L 10 5 L 0 10 z'); triangle.classList.add('arrow', 'edge-' + kind); marker.append(triangle); defs.append(marker);
    }
    svg.append(defs);
    graph.edges.forEach(edge => {
      const from = nodes.get(edge.from), to = nodes.get(edge.to); if (!from || !to) return;
      const group = document.createElementNS(svg.namespaceURI, 'g'); group.classList.add('edge-group');
      const highlighted = edge.from === studioSelection || edge.to === studioSelection;
      if (highlighted) group.classList.add('highlighted');
      if (edge.inactive) group.classList.add('inactive');
      const path = document.createElementNS(svg.namespaceURI, 'path'); path.classList.add('edge-path','edge-' + edge.kind);
      let x1 = from.x + from.width, y1 = from.y + from.height / 2, x2 = to.x, y2 = to.y + to.height / 2;
      let d, lx, ly;
      if (from.id === to.id) {
        x2 = from.x + from.width / 2; y2 = from.y;
        d = `M ${x1} ${y1} C ${x1+90} ${y1}, ${x2} ${y2-90}, ${x2} ${y2}`; lx=x1+20; ly=y2-28;
      } else if (Math.abs(from.x-to.x)<30 || edge.kind === 'returned') {
        const right = edge.kind !== 'returned'; x1 = from.x + (right ? from.width : 0); x2 = to.x + (right ? to.width : 0);
        const bend = right ? Math.max(x1,x2)+100 : Math.min(x1,x2)-90;
        d = `M ${x1} ${y1} C ${bend} ${y1}, ${bend} ${y2}, ${x2} ${y2}`; lx=bend; ly=(y1+y2)/2;
      } else {
        const mid = (x1+x2)/2;
        if (x2-x1 > 200) {
          // Route across empty rows so a long edge cannot look like a connection
          // to an unrelated node in the intervening column.
          const left=x1+22, right=x2-22;
          const obstacles=graph.nodes.filter(n=>n.id!==from.id && n.id!==to.id && n.x+n.width>left && n.x<right);
          const candidates=[(y1+y2)/2,...obstacles.flatMap(n=>[n.y-12,n.y+n.height+12])];
          const lanes=candidates.filter(y=>obstacles.every(n=>y<n.y-6 || y>n.y+n.height+6));
          const lane=lanes.sort((a,b)=>Math.abs(a-(y1+y2)/2)-Math.abs(b-(y1+y2)/2))[0] ?? -25;
          d=`M ${x1} ${y1} L ${left} ${y1} L ${left} ${lane} L ${right} ${lane} L ${right} ${y2} L ${x2} ${y2}`;lx=mid;ly=lane-8;
        } else {d = `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;lx=mid;ly=(y1+y2)/2-8;}
      }
      path.setAttribute('d',d); path.setAttribute('marker-end','url(#arrow-' + edge.kind + ')');
      const title = document.createElementNS(svg.namespaceURI,'title'); title.textContent = from.label + ' → ' + to.label + ': ' + edge.label; path.append(title); group.append(path);
      if (highlighted && (edge.kind !== 'tool' || graph.edges.filter(x => x.from === edge.from && x.kind === 'tool').length < 3)) {
        const text = document.createElementNS(svg.namespaceURI,'text'); text.setAttribute('x',lx); text.setAttribute('y',ly); text.setAttribute('text-anchor','middle'); text.textContent=edge.label; group.append(text);
      }
      svg.append(group);
    });
  }
  function inspect(id) {
    studioSelection = id;
    buttons.forEach((b, key) => { b.classList.toggle('selected', key === id); b.setAttribute('aria-pressed', String(key === id)); });
    drawEdges(); panel.replaceChildren(); panel.scrollTop = 0;
    const entry = nodes.get(id);
    if (id.startsWith('task:')) {
      const t = data.tasks.find(t => 'task:' + t.id === id);
      if (t) {
        panel.append(studioButton('← ' + agent(t.agent), () => { if (studioMode === 'run') openStudio('agent:' + t.agent); else inspect('agent:' + t.agent); }));
        panel.append(taskDetail(t)); return;
      }
    }
    const [kind, ...parts] = id.split(':'), key = parts.join(':');
    if (kind === 'agent') {
      const a = data.agents.find(a => a.key === key); if (!a) return;
      panel.append(e('p','AGENT','eyebrow'),e('h2',a.label),e('p',a.description),chips([a.model.provider,a.model.model || 'Varsayılan model','Prompt v' + a.version]));
      const prompt = studioSection('Kalıcı yönerge / system prompt'); prompt.append(e('pre',a.prompt,'inspector-prompt')); panel.append(prompt);
      const tools = studioSection('Kullanabildiği araçlar · ' + a.tools.length);
      a.tools.forEach(key => { const t = data.tools.find(t => t.key === key); tools.append(studioButton(t.label + ' · ' + t.status_label, () => inspect('tool:' + key))); });
      if (!a.tools.length) tools.append(e('p','Atanmış araç yok.','muted')); panel.append(tools);
      const triggers = studioSection('Bu agent’ı tetikleyen kayıtlar');
      data.triggers.filter(t => t.spec.agent === key).forEach(t => triggers.append(studioButton((t.enabled ? 'Etkin' : 'Pasif') + ' · ' + t.key, () => inspect('trigger:' + t.key))));
      if (triggers.children.length === 1) triggers.append(e('p','Kayıtlı tetikleyici yok.','muted')); panel.append(triggers);
      const incoming = data.agents.filter(a => a.delegates.includes(key)), links = studioSection('Agent’lar arası görev yetkisi');
      incoming.forEach(a => links.append(studioButton(a.label + ' → bu agent’a görev verebilir', () => inspect('agent:' + a.key))));
      a.delegates.forEach(k => links.append(studioButton('Görev verebilir → ' + agent(k), () => inspect('agent:' + k))));
      if (links.children.length === 1) links.append(e('p','Tanımlı görev devri bağlantısı yok.','muted')); panel.append(links);
      panel.append(studioFields([['Karar sınırı',a.max_turns + ' tur / görev'],['Tek model çağrısı sınırı',VerdaGraph.duration(a.model.timeout_seconds)],['Toplam görev süresi','Sabit süre tanımlı değil']]));
      const queue = studioSection('Gelen işler · ' + count(a.key, c => !terminal.has(c.state)) + ' açık');
      data.tasks.filter(t => t.agent === a.key).sort((a,b)=>Number(terminal.has(a.state))-Number(terminal.has(b.state)) || b.priority-a.priority || a.created_at-b.created_at).forEach(t => queue.append(studioButton((states[t.state] || t.state) + ' · ' + t.objective + (t.mode === 'synthetic' ? ' · ÖRNEK' : ''), () => inspect('task:' + t.id))));
      if (queue.children.length === 1) queue.append(e('p','Görüntülenen görev yok.','muted')); panel.append(queue);
      const protocol=e('details',undefined,'event'); protocol.append(e('summary','Ortak çalışma protokolü'),e('pre',data.protocol_prompt,'inspector-prompt')); panel.append(protocol);
    } else if (kind === 'trigger') {
      const t = data.triggers.find(t => t.key === key); if (!t) return;
      panel.append(e('p',t.kind === 'timer' ? 'ZAMANLAYICI' : 'OLAY TETİKLEYİCİSİ','eyebrow'),e('h2',t.key),triggerInspector(t,inspect));
    } else if (kind === 'tool') {
      const t = data.tools.find(t => t.key === key); if (!t) return;
      panel.append(e('p','ARAÇ','eyebrow'),toolDetail(t));
      panel.append(studioFields([['Çalıştırıcı',transports[t.transport]],['Süre sınırı', ['script','mcp'].includes(t.transport) ? VerdaGraph.duration(t.timeout_seconds) : 'Bu adaptörde uygulanmış toplam süre sınırı yok']]));
      const usedBy=studioSection('Bu aracı kullanabilen agent’lar');
      data.agents.filter(a=>a.tools.includes(key)).forEach(a=>usedBy.append(studioButton(a.label,()=>inspect('agent:'+a.key)))); panel.append(usedBy);
      if(t.resource) panel.append(studioButton('Kaynak bağlantısı → '+t.resource,()=>inspect('connection:'+t.resource)));
    } else if (kind === 'connection') {
      const c=data.connections.find(c=>c.key===key); if(!c)return;
      const state=data.resources.find(r=>r.key===key);
      panel.append(e('p',c.kind==='browser'?'BROWSER BAĞLANTISI':'ORTAK KAYNAK','eyebrow'),e('h2',c.label),e('p',c.note));
      panel.append(studioFields([['Oturum',c.session_status==='unconnected'?'Henüz bağlı değil':'Canlı bağlantı doğrulanmadı'],['Kaynak engeli',state?.halted || 'Kayıtlı engel yok'],['Sonraki kaynak izni',state?.next_at ? stamp(state.next_at) : 'Henüz kullanım yok']]));
      const related=studioSection('Bu kaynağı paylaşan araçlar'); data.tools.filter(t=>t.resource===key).forEach(t=>related.append(studioButton(t.label,()=>inspect('tool:'+t.key)))); panel.append(related);
    } else if (kind === 'call' && entry) {
      panel.append(e('p','KAYDEDİLMİŞ ARAÇ ÇAĞRISI','eyebrow'),e('h2',entry.label),e('p',stamp(entry.event.at)),e('h3','Araca verilen girdi'),json(entry.event.data.arguments));
      panel.append(e('h3','Kaydedilen sonuç'),entry.finish ? json(entry.finish.data) : e('p','Görüntülenen kayıtlarda sonuç yok.'));
      panel.append(e('p','Bu çağrı çalışma anındaki kayıttır. Bugünkü araç bağlantısından farklı olabilir.','muted'));
    } else if (kind === 'source') {
      const t=data.tasks.find(t=>t.id===key); if(!t)return;
      panel.append(e('p','GÖREV KAYNAĞI','eyebrow'),e('h2',source(t.source)),e('h3','Gönderilen görev'),e('pre',t.objective,'inspector-prompt'),e('h3','Gönderilen veri'),json(t.inputs));
    } else panel.append(e('h2','Bir düğüm seç'),e('p','Agent’ın prompt ve araçlarını, zamanlayıcının saat ve görevini sağ panelden incele.'));
  }
  for (const n of graph.nodes) {
    const b=e('button',undefined,'studio-node node-'+n.kind); b.type='button'; b.dataset.nodeId=n.id;
    let type='', subtitle='', status='';
    if(n.kind==='agent'){const a=data.agents.find(a=>a.key===n.key);type='AGENT';subtitle=a.model.provider+' · '+a.tools.length+' araç';status=count(a.key,c=>!terminal.has(c.state))+' açık görev';}
    if(n.kind==='trigger'){const t=data.triggers.find(t=>t.key===n.key);type=t.kind==='timer'?'◷ ZAMANLAYICI':'↯ OLAY';subtitle=t.kind==='timer'?'Her '+VerdaGraph.timerDetails(t).interval+' · '+VerdaGraph.timerDetails(t).clock:t.spec.event_type;status=(t.enabled?'Etkin':'Pasif')+(t.spec.mode==='synthetic'?' · ÖRNEK':'');}
    if(n.kind==='tool'){const t=data.tools.find(t=>t.key===n.key);type=transports[t.transport];subtitle=t.status_label;}
    if(n.kind==='connection'){const c=data.connections.find(c=>c.key===n.key);type=c.kind==='browser'?'BROWSER':'KAYNAK';subtitle=c.kind==='browser'?'Henüz bağlı değil':'Paylaşılan kaynak kuyruğu';}
    if(n.kind==='task'){const t=data.tasks.find(t=>t.id===n.key);type=t.executor_tool?'STATİK GÖREV':'AGENT GÖREVİ';subtitle=states[t.state]||t.state;status=(t.listing_ref||'İlan bağı yok')+(t.mode==='synthetic'?' · ÖRNEK':'');}
    if(n.kind==='call'){type='ARAÇ ÇAĞRISI';subtitle=n.finish ? (states[n.finish.data.state]||n.finish.data.state) : 'Sonuç kaydı yok';}
    if(n.kind==='source'){type='TETİKLEME';subtitle='Kaydedilmiş görev girdisi';}
    b.append(e('small',type),e('strong',n.label),e('span',subtitle)); if(status)b.append(e('span',status,'node-status'));
    b.setAttribute('aria-label',n.label+' · '+type+' · '+subtitle+(status?' · '+status:''));
    b.style.left=n.x+'px';b.style.top=n.y+'px';b.style.width=n.width+'px';b.style.height=n.height+'px';
    b.onclick=()=>{if(suppressClick){suppressClick=false;return;}inspect(n.id);};world.append(b);buttons.set(n.id,b);
  }
  viewport.addEventListener('pointerdown',event=>{
    if(event.button!==0)return; const target=event.target.closest('.studio-node');
    gesture={x:event.clientX,y:event.clientY,cx:camera.x,cy:camera.y,node:target?nodes.get(target.dataset.nodeId):null,moved:false};
    if(gesture.node){gesture.nx=gesture.node.x;gesture.ny=gesture.node.y;}
    viewport.setPointerCapture(event.pointerId);
  });
  viewport.addEventListener('pointermove',event=>{
    if(!gesture)return; const dx=event.clientX-gesture.x,dy=event.clientY-gesture.y;
    if(Math.abs(dx)+Math.abs(dy)<5&&!gesture.moved)return;gesture.moved=true;
    if(gesture.node){const n=gesture.node;n.x=gesture.nx+dx/camera.scale;n.y=gesture.ny+dy/camera.scale;const b=buttons.get(n.id);b.style.left=n.x+'px';b.style.top=n.y+'px';studioPositions.set(graphKey+':'+n.id,{x:n.x,y:n.y});drawEdges();}
    else{camera.x=gesture.cx+dx;camera.y=gesture.cy+dy;transform();}
  });
  viewport.addEventListener('pointerup',event=>{if(gesture?.moved)suppressClick=true;else if(gesture?.node)inspect(gesture.node.id);gesture=null;if(viewport.hasPointerCapture(event.pointerId))viewport.releasePointerCapture(event.pointerId);});
  viewport.addEventListener('pointercancel',()=>{gesture=null;});
  viewport.addEventListener('wheel',event=>{if(event.ctrlKey||event.metaKey){event.preventDefault();zoom(camera.scale*Math.exp(-event.deltaY*.008));}},{passive:false});
  viewport.addEventListener('keydown',event=>{if(event.target!==viewport)return;const delta={ArrowLeft:[40,0],ArrowRight:[-40,0],ArrowUp:[0,40],ArrowDown:[0,-40]}[event.key];if(delta){event.preventDefault();camera.x+=delta[0];camera.y+=delta[1];transform();}if(event.key==='0')fit();});
  inspect(studioSelection || graph.nodes[0]?.id || '');
  requestAnimationFrame(()=>{if(disposed)return;if(!camera.fitted){fit();camera.fitted=true;}else transform();});
  const observer = new ResizeObserver(()=>{if(!disposed && camera.fitted) transform();}); observer.observe(viewport);
  studioCleanup=()=>{disposed=true;observer.disconnect();};
}

function renderTriggerCatalog(root) {
  const n=card('Tetikleyiciler');n.append(e('p','Bir kaydı açarak zamanını, hedefini, görev metnini ve girdisini bağlantı haritasında incele.'));
  data.triggers.forEach(t=>{
    const row=e('div',undefined,'lineage');row.append(e('h3',t.key),chips([t.enabled?'Etkin':'Pasif',t.spec.mode==='synthetic'?'ÖRNEK VERİ':'Yerel']),e('p',(t.kind==='timer'?'Her '+VerdaGraph.timerDetails(t).interval+' · '+VerdaGraph.timerDetails(t).clock+' İstanbul':t.spec.event_type)+' → '+agent(t.spec.agent)),studioButton('Zamanlama ve prompt’u aç',()=>openStudio('trigger:'+t.key)));n.append(row);
  });
  if(!data.triggers.length)n.append(e('p','Henüz kayıtlı tetikleyici yok.','empty'));root.append(n);
}
