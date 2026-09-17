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
