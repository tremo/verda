/* Pure projections of saved configuration and execution records. No invented edges. */
(function (root) {
  const WIDTH = 210, HEIGHT = 80;
  const node = (id, kind, key, label, x, y, extra = {}) => ({id, kind, key, label, x, y, width:WIDTH, height:HEIGHT, ...extra});
  function topology(data) {
    const nodes = [], edges = [], agents = new Map(data.agents.map(a => [a.key, a]));
    const incoming = new Set(data.agents.flatMap(a => a.delegates));
    const ranks = new Map();
    // Breadth-first ranks also handle cycles and disconnected extension agents.
    const visit = start => {
      ranks.set(start.key, 0); const queue = [start];
      for (let i = 0; i < queue.length; i++) for (const key of queue[i].delegates) {
        if (agents.has(key) && !ranks.has(key)) { ranks.set(key, ranks.get(queue[i].key) + 1); queue.push(agents.get(key)); }
      }
    };
    data.agents.filter(a => !incoming.has(a.key)).forEach(a => { if (!ranks.has(a.key)) visit(a); });
    data.agents.forEach(a => { if (!ranks.has(a.key)) visit(a); });
    const toolColumns = data.tools.length > 6 ? 2 : 1;
    const toolRows = Math.ceil(data.tools.length / toolColumns);
    const height = Math.max(600, toolRows * 108 + 110, data.triggers.length * 130 + 110);
    const maxRank = Math.max(0, ...ranks.values());
    for (let rank = 0; rank <= maxRank; rank++) {
      const group = data.agents.filter(a => ranks.get(a.key) === rank);
      group.forEach((a, i) => nodes.push(node('agent:' + a.key, 'agent', a.key, a.label, 320 + rank * 300, (i + 1) * height / (group.length + 1) - HEIGHT / 2)));
    }
    const toolX = 320 + (maxRank + 1) * 300;
    data.tools.forEach((t, i) => nodes.push(node('tool:' + t.key, 'tool', t.key, t.label, toolX + Math.floor(i / toolRows) * 270, 60 + (i % toolRows) * 108)));
    data.triggers.forEach((t, i) => {
      const target = nodes.find(n => n.id === 'agent:' + t.spec.agent);
      const y = data.triggers.length === 1 && target ? target.y : 60 + i * 130;
      nodes.push(node('trigger:' + t.key, 'trigger', t.key, t.key, 20, y));
      if (target) edges.push({from:'trigger:' + t.key, to:target.id, kind:'trigger', label:t.enabled ? 'Görev oluşturur' : 'Pasif tetikleyici', inactive:!t.enabled});
    });
    for (const a of data.agents) {
      for (const key of a.delegates) if (agents.has(key)) edges.push({from:'agent:' + a.key, to:'agent:' + key, kind:'delegate', label:'Görev verebilir'});
      for (const key of a.tools) if (data.tools.some(t => t.key === key)) edges.push({from:'agent:' + a.key, to:'tool:' + key, kind:'tool', label:'Aracı kullanabilir'});
    }
    const connections = (data.connections || []).filter(c => data.tools.some(t => t.resource === c.key));
    connections.forEach((c, i) => {
      nodes.push(node('connection:' + c.key, 'connection', c.key, c.label, toolX + toolColumns * 270 + 30, (i + 1) * height / (connections.length + 1) - HEIGHT / 2));
      data.tools.filter(t => t.resource === c.key).forEach(t => edges.push({from:'tool:' + t.key, to:'connection:' + c.key, kind:'resource', label:c.kind === 'browser' ? 'Browser oturumu' : 'Ortak kaynak'}));
    });
    return {nodes, edges};
  }
  function run(data, traceId) {
    const nodes = [], edges = [], tasks = data.tasks.filter(t => t.trace_id === traceId).sort((a,b) => a.created_at-b.created_at);
    const taskIds = new Set(tasks.map(t => t.id));
    tasks.forEach((t, i) => {
      const a = data.agents.find(a => a.key === t.agent);
      nodes.push(node('task:' + t.id, 'task', t.id, a?.label || t.agent, 340, 90 + i * 210));
      if (t.parent_id && taskIds.has(t.parent_id)) edges.push({from:'task:' + t.parent_id, to:'task:' + t.id, kind:'delegated', label:'Görev devredildi'});
      if (!t.parent_id) {
        nodes.push(node('source:' + t.id, 'source', t.id, t.source === 'user' ? 'Kullanıcı isteği' : t.source, 20, 90 + i * 210));
        edges.push({from:'source:' + t.id, to:'task:' + t.id, kind:'trigger', label:'Görev oluşturdu'});
      }
      const calls = data.events.filter(e => e.task_id === t.id && e.kind === 'tool_started');
      calls.forEach((event, j) => {
        const finish = data.events.find(e => e.task_id === t.id && e.kind === 'tool_finished' && e.data.call_id === event.data.call_id);
        nodes.push(node('call:' + event.id, 'call', event.id, data.tools.find(tool => tool.key === event.data.tool)?.label || event.data.tool, 670 + j * 260, 90 + i * 210, {event, finish}));
        edges.push({from:'task:' + t.id, to:'call:' + event.id, kind:'executed', label:'Araç çağrıldı'});
      });
      // A result return is evidence, not an inferred reverse delegation permission.
      const result = data.events.find(e => e.task_id === t.parent_id && e.kind === 'child_result_received' && e.data.child_id === t.id);
      if (result && taskIds.has(t.parent_id)) edges.push({from:'task:' + t.id, to:'task:' + t.parent_id, kind:'returned', label:'Sonuç döndü'});
    });
    return {nodes, edges};
  }
  function duration(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return 'Tanımlı değil';
    if (seconds % 3600 === 0) return seconds / 3600 + ' saat';
    if (seconds % 60 === 0) return seconds / 60 + ' dakika';
    return seconds + ' saniye';
  }
  function timerDetails(trigger, zone = 'Europe/Istanbul') {
    const at = trigger.next_at == null ? null : new Date(trigger.next_at * 1000);
    return {
      zone,
      clock:at ? at.toLocaleTimeString('tr-TR', {timeZone:zone, hour:'2-digit', minute:'2-digit', second:'2-digit'}) : 'Tanımlı değil',
      next:at ? at.toLocaleString('tr-TR', {timeZone:zone, dateStyle:'long', timeStyle:'medium'}) : 'Tanımlı değil',
      interval:duration(trigger.spec.interval_seconds),
      // The current scheduler has an interval, not a timezone cron or run-duration limit.
      enabled:!!trigger.enabled,
      hasDurationLimit:false,
    };
  }
  root.VerdaGraph = {topology, run, duration, timerDetails};
})(globalThis);
