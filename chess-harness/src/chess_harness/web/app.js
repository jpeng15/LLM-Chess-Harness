const $ = id => document.getElementById(id);
let data=null, index=0, follow=true, flipped=false, playing=false, selection='latest', lastKey='', boardKey='';
function text(id, value){$(id).textContent=value;}
function stopReplay(){playing=false;text('play','Play replay');}
function seek(value){if(!data)return;follow=false;stopReplay();index=Math.max(0,Math.min(value,data.positions.length-1));renderPosition();}
function renderPosition(){
  if(!data)return;
  const pos=data.positions[index], final=data.positions.length-1;
  const query=new URLSearchParams({fen:pos.fen,last:pos.uci||'',flip:flipped?'1':'0'}).toString();
  if(query!==boardKey){$('board').src='/api/board?'+query;boardKey=query;}
  $('board').alt=`Chessboard after ${index} half-moves. ${pos.san}. ${pos.fen.split(' ')[1]==='w'?'White':'Black'} to move.`;
  text('fen',pos.fen);$('position').max=final;$('position').value=index;
  text('position-label',`${index===0?'Starting position':`Ply ${index} · ${pos.san}`} · ${follow?'Following game':'Replay'}`);
  $('live').setAttribute('aria-pressed',String(follow));
  $('first').disabled=$('prev').disabled=index===0;
  $('next').disabled=$('end').disabled=index===final;
  $('play').disabled=final===0;
  for(const button of $('moves').querySelectorAll('button')){
    const active=Number(button.dataset.ply)===index;button.classList.toggle('selected',active);
    button.setAttribute('aria-current',active?'step':'false');
  }
  const names={white:data.config.llm_color==='white'?data.config.llm.model:data.engine_name,black:data.config.llm_color==='black'?data.config.llm.model:data.engine_name};
  text('top-player',names[flipped?'white':'black']);text('bottom-player',names[flipped?'black':'white']);
  text('top-color',flipped?'White':'Black');text('bottom-color',flipped?'Black':'White');
  text('top-piece',flipped?'○':'●');text('bottom-piece',flipped?'●':'○');
}
function renderData(){
  if(!data)return;
  const summary=data.summary;
  text('mode',({'legal-moves':'Legal-move assisted','constrained-legal':'Schema-constrained legal','rules-tools':'Rules-tool assisted','unassisted':'Unassisted'})[data.config.mode]||data.config.mode||'Mode unavailable');
  text('status',summary?summary.status.replaceAll('_',' '):(data.pending?'Thinking':data.sequence===0?'Preparing game':'Game in progress'));
  text('result',summary?.result||'');
  text('detail',summary?(summary.error||summary.reason.replaceAll('_',' ')):(data.pending?`${data.pending.player} is choosing a move.`:'Waiting for the next event.'));
  text('prompt',data.config.prompt_version);text('thinking',data.config.llm.think?'Thinking enabled':'Thinking disabled');
  $('moves').replaceChildren();
  if(data.positions.length===1){const p=document.createElement('p');p.className='sub';p.textContent='No moves yet.';$('moves').append(p);}
  data.positions.slice(1).forEach((pos,i)=>{
    const prior=data.positions[i].fen.split(' '), button=document.createElement('button');
    button.textContent=`${prior[5]}${prior[1]==='w'?'.':'…'} ${pos.san}`;button.dataset.ply=i+1;
    button.onclick=()=>seek(i+1);$('moves').append(button);
  });
  $('responses').replaceChildren();text('response-count',String(data.responses.length));
  if(data.tool_activity?.length){
    const details=document.createElement('details'),title=document.createElement('summary');
    title.textContent='Rules-tool exploration (hypothetical positions)';details.append(title);
    for(const event of data.tool_activity){
      const entry=document.createElement('details'),label=document.createElement('summary'),body=document.createElement('pre');
      label.textContent=`Ply ${event.ply} · call ${event.call} · ${event.type.replaceAll('_',' ')}`;
      body.textContent=JSON.stringify(event.result||event.raw||event.request,null,2);
      entry.append(label,body);details.append(entry);
    }
    $('responses').append(details);
  }
  for(const response of [...data.responses].reverse()){
    const row=document.createElement('article');row.className='response'+(response.applied?'':' pending');
    const meta=document.createElement('div');meta.className='response-meta';
    const who=document.createElement('span');who.textContent=`${response.player} · ply ${response.ply}`;
    const elapsed=document.createElement('span');elapsed.textContent=Number.isFinite(response.seconds)?`${response.seconds.toFixed(2)}s`:'Timing unavailable';
    meta.append(who,elapsed);const output=document.createElement('pre');output.textContent=response.text||'(empty response)';row.append(meta,output);
    if(!response.applied){const label=document.createElement('div');label.className='sub';label.textContent=summary?'Not applied to board':'Awaiting validation';row.append(label);}
    if(response.failure_reason){const reason=document.createElement('div');reason.className='sub';reason.textContent=response.failure_reason.replaceAll('_',' ');row.append(reason);}
    if(response.error){const error=document.createElement('pre');error.textContent=response.error;row.append(error);}
    if(response.content&&response.content!==response.text){const details=document.createElement('details'),title=document.createElement('summary'),raw=document.createElement('pre');title.textContent='Raw model output';raw.textContent=response.content;details.append(title,raw);row.append(details);}
    if(response.thinking){const details=document.createElement('details'),title=document.createElement('summary'),reasoning=document.createElement('pre');title.textContent='Thinking output';reasoning.textContent=response.thinking;details.append(title,reasoning);row.append(details);}
    $('responses').append(row);
  }
  if(!data.responses.length){const p=document.createElement('p');p.className='sub';p.textContent='Responses appear after each turn.';$('responses').append(p);}
  renderPosition();
}
async function get(url){const res=await fetch(url,{signal:AbortSignal.timeout(5000)});if(!res.ok)throw new Error(`Viewer request failed (${res.status})`);return res.json();}
async function poll(){
  const selected=selection;
  try{
    const runs=await get('/api/runs');
    if(selected!==selection)return;
    const options=runs.map(r=>r.id).join('|');
    if($('games').dataset.options!==options){
      $('games').replaceChildren(new Option('Follow newest game','latest'));
      for(const run of runs)$('games').add(new Option(`${run.id} · ${run.model}`,run.id));
      $('games').value=selected;$('games').dataset.options=options;
    }
    const id=selected==='latest'?runs[0]?.id:selected;
    text('connection','Connected · local');
    if(!id){text('notice','No saved games yet. Start the game runner in another terminal.');return;}
    const next=await get('/api/runs/'+encodeURIComponent(id));
    if(selected!==selection)return;
    const changed=data?.id!==next.id;
    if(changed){follow=true;stopReplay();}
    data=next;if(follow)index=data.positions.length-1;else index=Math.min(index,data.positions.length-1);
    text('notice',data.id+' · Board controls affect replay only; the game keeps running.');
    const key=JSON.stringify([data.id,data.sequence,data.summary,data.engine_name,data.config.mode,data.config.prompt_version]);
    if(key!==lastKey){renderData();lastKey=key;}
    else if(!boardKey){renderPosition();}
  }catch(error){text('connection','Disconnected · retrying');text('notice',error.message+'; showing last received position.');}
  finally{setTimeout(poll,700);}
}
// A failed SVG request must be retried even if no new game event arrives.
$('board').onerror=()=>{boardKey='';};
$('games').onchange=()=>{selection=$('games').value;lastKey='';};
$('flip').onclick=()=>{flipped=!flipped;renderPosition();};
$('first').onclick=()=>seek(0);$('prev').onclick=()=>seek(index-1);$('next').onclick=()=>seek(index+1);$('end').onclick=()=>seek(data?.positions.length-1);
$('position').oninput=()=>seek(Number($('position').value));
$('live').onclick=()=>{follow=true;stopReplay();if(data){index=data.positions.length-1;renderPosition();}};
$('play').onclick=()=>{if(!data)return;if(playing){stopReplay();return;}follow=false;if(index===data.positions.length-1)index=0;playing=true;text('play','Pause replay');renderPosition();};
setInterval(()=>{if(playing&&data){if(index<data.positions.length-1){index++;renderPosition();}else stopReplay();}},800);
setInterval(()=>{
  if(!data)return;
  const age=data.last_event?Math.max(0,Math.floor((Date.now()-Date.parse(data.last_event))/1000)):null;
  if(!data.summary&&data.pending){const seconds=Math.max(0,Math.floor((Date.now()-Date.parse(data.pending.time))/1000));text('detail',`${data.pending.player} · ${seconds}s elapsed`);}
  text('freshness',data.summary?'Saved result':age===null?'Waiting for initialization…':`Last event ${age}s ago${age>Math.max(data.config.llm.seconds,180)+15?' · runner may have stopped':''}`);
},500);
poll();

// Optional page tools use the same replay state; they cannot control the players.
if(document.modelContext?.registerTool){
  const lifecycle=new AbortController();
  window.addEventListener('pagehide',()=>lifecycle.abort(),{once:true});
  const tools=[
    {name:'read_chess_view',description:'Read the selected game and displayed replay position.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},execute(){return {game:data?.id??null,ply:index,fen:data?.positions[index]?.fen??null,following:follow,result:data?.summary??null};}},
    {name:'seek_chess_replay',description:'Display a saved half-move in the selected game. Does not affect game execution.',inputSchema:{type:'object',properties:{ply:{type:'integer',minimum:0}},required:['ply'],additionalProperties:false},annotations:{readOnlyHint:false,untrustedContentHint:false},execute(input){if(!data||!Number.isInteger(input?.ply)||input.ply<0||input.ply>=data.positions.length)throw new Error('Choose a recorded ply in the selected game.');seek(input.ply);return {game:data.id,ply:index,fen:data.positions[index].fen};}}
  ];
  for(const tool of tools){try{Promise.resolve(document.modelContext.registerTool(tool,{signal:lifecycle.signal})).catch(()=>{});}catch{}}
}
