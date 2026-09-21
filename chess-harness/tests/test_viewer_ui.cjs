// Isolated controller regressions; no browser or external packages required.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function controller() {
  class Element {
    constructor() { this.dataset={}; this.children=[]; this.attributes={}; this.classList={toggle(){}}; this.imageRequests=0; }
    set src(value) { this.imageRequests++; this.source=value; }
    set innerHTML(value) { throw new Error('Viewer content must be assigned as text, never HTML'); }
    setAttribute(key,value) { this.attributes[key]=value; }
    append(...children) { this.children.push(...children); }
    add(child) { this.append(child); }
    replaceChildren(...children) { this.children=children; }
    querySelectorAll() { return this.children; }
  }
  const elements = new Map();
  const element = id => { if(!elements.has(id))elements.set(id,new Element()); return elements.get(id); };
  const timers = [];
  const state = {id:'sample',sequence:2,engine_name:'Stockfish',config:{llm_color:'white',prompt_version:'test',llm:{model:'LLM',think:false,seconds:60}},positions:[
    {fen:'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',san:'Start',uci:null},
    {fen:'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1',san:'e4',uci:'e2e4'}
  ],responses:[],summary:null,pending:null};
  let online=true;
  const sandbox = {
    document:{getElementById:element,createElement:()=>new Element()},
    Option:class extends Element {}, URLSearchParams, AbortSignal,
    setTimeout:fn=>timers.push(fn), setInterval(){},
    fetch:async url=>{
      if(!online)throw new Error('offline');
      return {ok:true,json:async()=>structuredClone(url==='/api/runs'?[{id:'sample',model:'LLM'}]:state)};
    }
  };
  vm.runInNewContext(readFileSync(path.join(__dirname,'../src/chess_harness/web/app.js'),'utf8'),sandbox);
  return {element,state,setOnline:value=>{online=value;},
    ready:()=>new Promise(resolve=>setImmediate(resolve)),
    poll:async()=>{assert.ok(timers.length);await timers.shift()();}};
}

test('retry a failed board after reconnect even without a new game event', async()=>{
  const app=controller();await app.ready();
  const board=app.element('board');
  assert.equal(board.imageRequests,1);
  await app.poll();assert.equal(board.imageRequests,1,'successful image is not repeatedly loaded');
  app.setOnline(false);await app.poll();
  assert.equal(app.element('connection').textContent,'Disconnected · retrying');
  app.element('first').onclick();board.onerror();
  const failedRequests=board.imageRequests;
  app.setOnline(true);await app.poll();
  assert.equal(app.element('connection').textContent,'Connected · local');
  assert.equal(board.imageRequests,failedRequests+1,'unchanged snapshot retries the failed SVG');
  assert.match(board.alt,/after 0 half-moves/,'reconnection preserves replay position');
});

test('new events preserve replay position until Live is selected',async()=>{
  const app=controller();await app.ready();
  app.element('first').onclick();
  app.state.positions.push({fen:'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',san:'e5',uci:'e7e5'});
  app.state.sequence++;
  await app.poll();
  assert.match(app.element('board').alt,/after 0 half-moves/);
  app.element('live').onclick();
  assert.match(app.element('board').alt,/after 2 half-moves/);
  assert.equal(app.element('live').attributes['aria-pressed'],'true');
});

test('mode labels distinguish assisted and unassisted games',async()=>{
  const app=controller();await app.ready();
  app.state.config.mode='legal-moves';app.state.config.prompt_version='legal-moves-v1';
  await app.poll();
  assert.equal(app.element('mode').textContent,'Legal-move assisted');
  assert.equal(app.element('prompt').textContent,'legal-moves-v1');
  app.state.config.mode='unassisted';app.state.config.prompt_version='unassisted-v2';
  await app.poll();
  assert.equal(app.element('mode').textContent,'Unassisted');
  assert.equal(app.element('prompt').textContent,'unassisted-v2');
  app.state.config.mode='constrained-legal';app.state.config.prompt_version='constrained-legal-v1';
  await app.poll();
  assert.equal(app.element('mode').textContent,'Schema-constrained legal');
  assert.equal(app.element('prompt').textContent,'constrained-legal-v1');
});

test('constrained moves display raw JSON separately from the chosen move',async()=>{
  const app=controller();await app.ready();
  app.state.responses=[{player:'LLM',ply:1,text:'e2e4',content:'{"move":"e2e4"}',seconds:1,applied:true}];
  app.state.sequence++;
  await app.poll();
  const rendered=JSON.stringify(app.element('responses').children);
  assert.match(rendered,/Raw model output/);
  assert.ok(rendered.includes(JSON.stringify('{"move":"e2e4"}')));
});

test('live rules exploration is separate from the real board and moves',async()=>{
  const app=controller();await app.ready();
  app.state.config.mode='rules-tools';
  app.state.tool_activity=[{type:'simulation_result',ply:3,call:1,result:{position:1,parent:0,move:'e2e4',side_to_move:'black',legal_moves:['e7e5']}}];
  app.state.sequence++;
  await app.poll();
  assert.equal(app.element('mode').textContent,'Rules-tool assisted');
  const rendered=JSON.stringify(app.element('responses').children);
  assert.match(rendered,/hypothetical positions/);
  assert.match(rendered,/simulation result/);
  assert.match(rendered,/e7e5/);
  assert.match(app.element('board').alt,/after 1 half-moves/);
  assert.equal(app.element('moves').children.length,1);
});

test('authored source and heuristics render as text, with facts and errors distinguished',async()=>{
  const app=controller();await app.ready();
  const malicious='</pre><script>throw new Error("must remain text")</script>';
  app.state.config.mode='authored-validator';
  app.state.validator_artifact={artifact_id:'frozen-a',source_sha256:'a'.repeat(64),source_text:malicious,
    setup_costs:{generation:{output_tokens:100}},manifest:{development:'dev-a'}};
  app.state.validator_costs={requests:2,results:2,successes:1,errors:1,unanswered:0,
    execution:{cpu_seconds:{samples:1,missing:1,total:0.1}},initialization:{invocations:1},model_inference:{responses:2}};
  app.state.tool_activity=[
    {type:'validator_preflight',ply:0,report:{status:'ok'}},
    {type:'validator_result',ply:3,call:1,candidate:'e2e4',input:{candidate:'e2e4'},execution:{status:'ok',reason:'completed',
      findings:{facts:[{kind:'capture_available',line:['e2e4','d7d5']}],heuristics:[{interpretation:malicious}]}}},
    {type:'validator_result',ply:3,call:2,candidate:'g1f3',input:{candidate:'g1f3'},execution:{status:'error',reason:'timeout',stderr:malicious}}
  ];
  app.state.sequence++;
  await app.poll();
  assert.equal(app.element('mode').textContent,'LLM-authored validator');
  const rendered=JSON.stringify(app.element('responses').children);
  assert.match(rendered,/Frozen authored-validator artifact/);
  assert.match(rendered,/Generation, development, and freeze costs/);
  assert.match(rendered,/Verified factual findings/);
  assert.match(rendered,/Heuristic interpretations \(not verified\)/);
  assert.match(rendered,/Status: error · Reason: timeout/);
  assert.match(rendered,/Missing measurements are unavailable/);
  assert.ok(rendered.includes(JSON.stringify(malicious)));
  assert.match(app.element('board').alt,/after 1 half-moves/);
  assert.equal(app.element('moves').children.length,1);
});
