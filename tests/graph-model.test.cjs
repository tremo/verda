const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
require('../src/verda/ui/graph-model.js');
const {topology, run, timerDetails} = globalThis.VerdaGraph;
const config = JSON.parse(readFileSync(new URL('../src/verda/agency/default.json', 'file://' + __filename)));

test('configuration graph shares tools and only draws granted directional links', () => {
  const data = {...config, triggers:[{key:'morning',kind:'timer',enabled:0,spec:{agent:'sahibinden'}}], connections:[{key:'sahibinden',kind:'browser',label:'Browser'}]};
  const graph = topology(data);
  assert.equal(graph.nodes.filter(n=>n.id==='tool:policy.screen').length,1);
  assert.deepEqual(graph.edges.filter(e=>e.to==='tool:policy.screen').map(e=>e.from).sort(),['agent:manager','agent:research']);
  assert(graph.edges.some(e=>e.from==='trigger:morning' && e.to==='agent:sahibinden' && e.inactive));
  assert(!graph.edges.some(e=>e.from==='agent:sahibinden' && e.to==='agent:manager'));
  assert.equal(graph.edges.filter(e=>e.to==='connection:sahibinden').length,4);
  const ids=new Set(graph.nodes.map(n=>n.id));
  assert(graph.edges.every(e=>ids.has(e.from)&&ids.has(e.to)));
});

test('extension agents with cyclic delegation retain distinct finite positions', () => {
  const graph=topology({agents:[{key:'a',label:'A',delegates:['b'],tools:[]},{key:'b',label:'B',delegates:['a'],tools:[]}],tools:[],triggers:[],connections:[]});
  assert.equal(graph.nodes.length,2);
  assert.equal(graph.edges.length,2);
  assert(graph.nodes.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)));
  assert.notDeepEqual([graph.nodes[0].x,graph.nodes[0].y],[graph.nodes[1].x,graph.nodes[1].y]);
});

test('timer interval is not a duration limit and zero timestamps are valid', () => {
  const timer={enabled:0,next_at:0,spec:{interval_seconds:86400}};
  const details=timerDetails(timer,'UTC');
  assert.equal(details.clock,'00:00:00');
  assert.equal(details.interval,'24 saat');
  assert.equal(details.enabled,false);
  assert.equal(details.hasDurationLimit,false);
  assert.match(details.next,/1970/);
});

test('capability grouping never expands an agent grant or claims a browser session is ready', () => {
  const data={...config,connections:[{key:'sahibinden',kind:'browser',label:'Browser',session_status:'unconnected'}]};
  const browser=globalThis.VerdaGraph.capabilities(data,'sahibinden');
  assert.equal(browser.length,1);
  assert.equal(browser[0].icon,'browser');
  assert.deepEqual(browser[0].tools.map(t=>t.key),config.agents.find(a=>a.key==='sahibinden').tools);
  assert(!browser[0].tools.some(t=>t.available===true));
  const limited={...data,agents:data.agents.map(a=>a.key==='sahibinden'?{...a,tools:['sahibinden.read_listing']}:a)};
  assert.equal(globalThis.VerdaGraph.capabilities(limited,'sahibinden')[0].tools.length,1);
  assert.equal(globalThis.VerdaGraph.capabilities(data,'manager')[0].icon,'code');
});

test('agent overview keeps trigger and delegation edges while nesting granted tools in larger cards', () => {
  const data={...config,triggers:[{key:'morning',kind:'timer',enabled:0,spec:{agent:'sahibinden'}}],connections:[{key:'sahibinden',kind:'browser',label:'Browser'}]};
  const graph=globalThis.VerdaGraph.agentNetwork(data);
  assert.equal(graph.nodes.length,5);
  assert.equal(graph.edges.length,4);
  assert(graph.nodes.filter(n=>n.kind==='agent').every(n=>n.width>210&&n.height>80));
  const operator=graph.nodes.find(n=>n.key==='sahibinden');
  assert.equal(operator.capabilities[0].tools.length,4);
  const ids=new Set(graph.nodes.map(n=>n.id));
  assert(graph.edges.every(e=>ids.has(e.from)&&ids.has(e.to)));
});

test('execution graph isolates a trace and only shows evidenced calls and returns', () => {
  const data={...config,tasks:[
    {id:'parent',agent:'manager',trace_id:'one',created_at:1,source:'user'},
    {id:'child',agent:'sahibinden',trace_id:'one',created_at:2,parent_id:'parent'},
    {id:'other',agent:'research',trace_id:'two',created_at:3,source:'user'}
  ],events:[
    {id:1,task_id:'child',kind:'tool_started',data:{call_id:'call',tool:'sahibinden.read_listing'}},
    {id:2,task_id:'child',kind:'tool_finished',data:{call_id:'call',state:'complete',output:{area:2000}}},
    {id:3,task_id:'parent',kind:'child_result_received',data:{child_id:'child'}}
  ]};
  const graph=run(data,'one');
  assert(!graph.nodes.some(n=>n.key==='other'));
  assert.equal(graph.nodes.filter(n=>n.kind==='call').length,1);
  assert.deepEqual(graph.nodes.find(n=>n.kind==='call').finish.data.output,{area:2000});
  assert(graph.edges.some(e=>e.kind==='returned'&&e.from==='task:child'&&e.to==='task:parent'));
  assert(!run({...data,events:data.events.slice(0,2)},'one').edges.some(e=>e.kind==='returned'));
});
