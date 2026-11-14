const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const graphSource = fs.readFileSync('intel/ui/graphs.js', 'utf8').replace(
  '  window.SiyabGraph =',
  '  window.graphTest = {recordingCoverage, graphAddress};\n  window.SiyabGraph ='
);
const window = {location: new URL('https://siyab.xyz/investigate.html?account=wrap.near')};
vm.runInNewContext(fs.readFileSync('intel/ui/number-format.js','utf8'),{window});
vm.runInNewContext(graphSource, {window, URL, document: {readyState: 'loading', addEventListener() {}}});
const live = {kind: 'flow', source: {recorded: false}, bounds: {requested_depth: 2, reached_depth: 1},
  coverage: {truncated: true, queried_accounts: ['wrap.near'], completeness: {reason: 'bounded live observation'}}};
assert.match(window.graphTest.recordingCoverage(live), /not complete account history/);
assert.doesNotMatch(window.graphTest.recordingCoverage(live), /acquisition complete|Recording:/);
assert.match(window.graphTest.recordingCoverage(live,true), /^Saved indexed observation/);
assert.equal(window.graphTest.graphAddress('/live/accounts/wrap.near/graph?depth=2').live, true);
assert.equal(window.graphTest.graphAddress('/live/account-data/' + 'b'.repeat(64) + '.json').snapshot, true);
assert.throws(() => window.graphTest.graphAddress('/live/account-data/' + 'b'.repeat(64) + '.json?account=x.near'), /address is invalid/);
assert.equal(window.graphTest.graphAddress('/graph-data/' + 'a'.repeat(64) + '.json').live, false);
for (const address of ['https://other.example/live/accounts/wrap.near/graph?depth=1',
  '/live/accounts/wrap.near/graph?depth=3', '/live/accounts/wrap.near/graph?depth=1&unexpected=x',
  '/live/accounts/wrap.near/graph?depth=1&depth=2', '/live/accounts/x.testnet/graph',
  '/live/accounts/a%2Fb.near/graph', '/graph-data/' + 'a'.repeat(64) + '.json?v=1']) {
  assert.throws(() => window.graphTest.graphAddress(address), /address is invalid/);
}

const uiSource = fs.readFileSync('intel/ui/investigate.js', 'utf8').replace(
  '  const root =',
  '  window.investigateTest = {accountId, baseUnits, exactTime, validPage, direction};\n  const root ='
);
vm.runInNewContext(uiSource, {window, URL, document: {getElementById() {return null;}}});
const {accountId, baseUnits, exactTime, validPage, direction} = window.investigateTest;
assert.equal(accountId('wrap.near'), true);
for (const account of ['x.testnet', 'testnet', 'a..near', 'bad!near', 'a'.repeat(65)]) assert.equal(accountId(account), false);
assert.equal(baseUnits('1000000000000000000000001', 24), '1.000000000000000000000001');
assert.equal(baseUnits('9007199254740993123456789', 6), '9007199254740993123.456789');
assert.equal(baseUnits(null, 24), 'Unavailable');
assert.equal(baseUnits('0', 24), '0');
assert.equal(baseUnits('100', null), '100 base units');
assert.equal(exactTime('1776237164627685607'), '2026-04-15 07:12:44.627685607 UTC');
assert.equal(exactTime(null), 'Not recorded');
assert.equal(direction({from:'wrap.near',to:'another.near'}, 'wrap.near'), 'Outgoing');
assert.equal(direction({from:null,to:'wrap.near'}, 'wrap.near'), 'Incoming');
assert.equal(validPage({account_id:'wrap.near',network:'mainnet',items:[],pagination:{},source:{},coverage:{}}, 'wrap.near'), true);
assert.equal(validPage({account_id:'other.near',network:'mainnet',items:[],pagination:{},source:{},coverage:{}}, 'wrap.near'), false);

class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.dataset = {}; this.value = ''; this.textContent = ''; const classes = new Set(); this.classList = {add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name)}; }
  append(...nodes) { for (const item of nodes) { item.parent = this; this.children.push(item); } }
  get childNodes() { return this.children; }
  replaceChildren(...nodes) { this.children = []; this.textContent = ''; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  contains(target) { return this === target || this.children.some(child => child.contains(target)); }
  querySelector(selector) { return this.children.find(child => selector === 'h2' ? child.tag === 'h2' : child.attributes['data-live-content'] !== undefined) || null; }
  focus() { this.document.activeElement = this; }
}
const contentText = item => item.textContent + item.children.map(contentText).join(' ');
const tick = () => new Promise(resolve => setImmediate(resolve));
async function appBehavior() {
  const ids = {};
  const doc = {getElementById: id => ids[id], createElement: tag => Object.assign(new Element(tag), {document: doc}), dispatchEvent() {}};
  for (const id of ['account-investigation','investigate-form','investigate-account','investigate-status','investigate-title','investigate-watch','investigate-save','investigate-graph','investigate-load-graph','investigate-depth','investigate-refresh','investigate-read-identity','investigate-history-load','investigate-history-kind','investigate-backfill','investigate-backfill-pages','investigate-backfill-status']) ids[id] = doc.createElement('div');
  ids['investigate-depth'].value = '1'; ids['investigate-history-kind'].value='transfers';ids['investigate-backfill-pages'].value='1';
  for (const kind of ['profile','holdings','transfers','transactions','identity','history']) {
    const section = doc.createElement('section'), content = doc.createElement('div'); content.setAttribute('data-live-content','');
    section.append(doc.createElement('h2'), content); ids['investigate-'+kind] = section;
  }
  const events = {}, pending = [], timers = new Map(); let timerIndex = 0, destroyed = 0;
  const win = {Node: Element, location: new URL('https://siyab.xyz/investigate.html'),
    history: {pushState(state, text, url) {win.location = new URL(url);},replaceState(state,text,url) {win.location = new URL(url);}},
    addEventListener(name, callback) {events[name] = callback;},
    SiyabGraph: {mount: async () => {}, destroy: () => destroyed++}};
  vm.runInNewContext(fs.readFileSync('intel/ui/number-format.js','utf8'),{window:win});
  vm.runInNewContext(fs.readFileSync('intel/ui/investigate.js','utf8'), {window:win,document:doc,URL,URLSearchParams,AbortController,
    CustomEvent: class {constructor(type, options) {this.type=type; this.detail=options.detail;}},
    setTimeout(callback, delay) {const id=++timerIndex;callback.delay=delay;timers.set(id,callback);return id;},clearTimeout(id){timers.delete(id);},
    fetch(url,options) {return new Promise((resolve,reject) => {pending.push({url,options,resolve,reject});});}});
  const select = subject => {ids['investigate-account'].value=subject;ids['investigate-form'].listeners.submit({preventDefault(){}});};
  const reply = (request, payload) => request.resolve({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>payload});
  const page = subject => ({account_id:subject,network:'mainnet',items:[],pagination:{next_offset:null,next_cursor:null},source:{provider:'Provider'},coverage:{scope:'Indexed observation'}});
  select('wrap.near'); assert.equal(pending.length,4);
  const old = pending.slice(); select('other.near'); assert.equal(pending.length,8);
  assert.ok(old.every(request => request.options.signal.aborted), 'Changing accounts aborts every old read');
  reply(old[0],{account_id:'wrap.near',network:'mainnet',exists:false,labels:[],source:{},coverage:{}});
  for (const request of old.slice(1)) reply(request,page('wrap.near'));
  await tick(); assert.equal(ids['investigate-title'].textContent,'other.near'); assert.equal(ids['investigate-title'].classList.contains('investigate-subject'),true,'A loaded subject marks the title');
  assert.doesNotMatch(contentText(ids['investigate-profile']),/does not currently exist/,'A late old response must not overwrite the active account');
  reply(pending[4],{account_id:'other.near',network:'mainnet',exists:true,native:{amount_yocto:'1000000000000000000000001',locked_yocto:'0',block_height:'9007199254740993',storage_usage:12},labels:[],source:{provider:'RPC'},coverage:{scope:'Includes storage backing'}});
  const holdings=page('other.near');holdings.items=[{contract_id:'wrap.near',balance:'9007199254740993123456789',metadata:{name:'<img src=x onerror=bad()>',symbol:'TEST',decimals:6},balance_status:'indexed',last_update_block_height:'1'}];holdings.pagination.next_offset=10;
  reply(pending[5],holdings);reply(pending[6],page('other.near'));reply(pending[7],page('other.near'));await tick();
  assert.match(contentText(ids['investigate-profile']),/Account balance 1/);
  assert.match(contentText(ids['investigate-profile']),/1000000000000000000000001/,'Exact finalized JSON retains every base unit');
  assert.match(contentText(ids['investigate-holdings']),/9007199254740993123\.456789/);
  assert.match(contentText(ids['investigate-holdings']),/<img src=x onerror=bad\(\)>/,'Provider text is rendered as literal text');
  assert.equal(ids['investigate-holdings'].attributes['aria-busy'],'false');
  const allNodes = root => [root,...root.children.flatMap(allNodes)];
  let next=allNodes(ids['investigate-holdings']).find(item=>item.tag==='button'&&item.textContent==='Next page');
  next.listeners.click();assert.match(pending.at(-1).url,/holdings\?offset=10&limit=10$/);
  reply(pending.at(-1),page('other.near'));await tick();
  const previous=allNodes(ids['investigate-holdings']).find(item=>item.tag==='button'&&item.textContent==='Previous page');
  previous.listeners.click();assert.match(pending.at(-1).url,/holdings\?offset=0&limit=10$/);
  reply(pending.at(-1),page('other.near'));await tick();
  const transferPage=page('other.near');transferPage.pagination.next_cursor='opaque/+?cursor==';
  transferPage.items = [
    {amount: '100000000000000000000000', amount_decimals: 24, human_amount: '0.09999999999999999'},
    {amount: '1', amount_decimals: 24, human_amount: '1e-24'},
    {amount: '123', amount_decimals: null, human_amount: '1.23'},
  ].map(item => ({from: 'other.near', to: 'bob.near', asset_id: 'nep141:token.near', usd_amount: '0.09999999999999999', transaction_hash: 'tx', block_timestamp: '1776237164627685607', ...item}));
  ids['investigate-refresh'].listeners.click();const refreshed=pending.slice(-4);
  reply(refreshed[0],{account_id:'other.near',network:'mainnet',exists:false,labels:[],source:{},coverage:{}});
  reply(refreshed[1],page('other.near'));reply(refreshed[2],transferPage);reply(refreshed[3],page('other.near'));await tick();
  assert.equal(allNodes(ids['investigate-transfers']).find(item => item.tag === 'small').textContent, '0.1 USD (provider value)');
  const transferAmounts = allNodes(ids['investigate-transfers']).filter(item => item.tag === 'span' && item.className === 'mono').map(item => item.textContent);
  assert.ok(transferAmounts.includes('0.1'), 'Transfer values use exact base units instead of provider floats');
  assert.ok(transferAmounts.includes('<0.0001'), 'Transfer dust keeps the display floor');
  assert.ok(transferAmounts.includes('123 base units'), 'Unknown decimals stay base units despite a human amount');
  assert.match(contentText(ids['investigate-transfers']), /100000000000000000000000/, 'Transfer source keeps exact base units');
  next=allNodes(ids['investigate-transfers']).find(item=>item.tag==='button'&&item.textContent==='Next page');next.listeners.click();
  assert.equal(new URL(pending.at(-1).url,win.location).searchParams.get('cursor'),'opaque/+?cursor==','Opaque cursor must pass through unchanged');
  const timed=pending.at(-1);timers.values().next().value();timed.reject(new Error('aborted'));await tick();
  assert.match(contentText(ids['investigate-transfers']),/timed out/);
  assert.ok(allNodes(ids['investigate-transfers']).some(item=>item.textContent==='Retry transfers'));
  ids['investigate-read-identity'].listeners.click();assert.match(pending.at(-1).url,/\/identity$/);
  reply(pending.at(-1),{account_id:'other.near',network:'mainnet',observed_at:'2026-11-10T00:00:00Z',contract:{status:'deployed',code_hash:'ExactCodeHash',block_height:'123',source:{provider:'RPC'}},validator:{status:'current',stake_yocto:'1000000000000000000000001',epoch_start_height:'120',epoch_height:'8',source:{provider:'RPC'}},token_metadata:{name:'<script>metadata</script>',symbol:'TOK',decimals:24,spec:'ft-1.0.0',source:{method:'ft_metadata'}},contract_source:{version:'v1',link:'https://example.com/source',standards:[],source:{method:'contract_source_metadata'}},registry_entities:[{name:'Recorded DAO',observed_at:'2026-11-01T00:00:00Z',source:{block:1}}],assertions:[],sources:[],coverage:{scope:'Reported metadata; ownership not verified'}});await tick();
  assert.match(contentText(ids['investigate-identity']),/1\.000000000000000000000001/);
  assert.match(contentText(ids['investigate-identity']),/<script>metadata<\/script>/);
  assert.match(contentText(ids['investigate-identity']),/2026-11-01T00:00:00Z/);
  assert.match(contentText(ids['investigate-identity']),/do not establish verified brand identity/);
  const archived=kind=>({account_id:'other.near',network:'mainnet',kind,items:[],pagination:{next_before:null,limit:25},observations:[{page_id:'a'.repeat(64),source:{provider:'Original provider'},observed_at:'2026-11-09T00:00:00Z'}],coverage:{stored_rows:3,history_complete:false,scope:'Retained observations only'}});
  ids['investigate-history-load'].listeners.click();assert.match(pending.at(-1).url,/history\?kind=transfers&before=0&limit=25$/);
  const retained=archived('transfers');retained.pagination.next_before=77;reply(pending.at(-1),retained);await tick();
  assert.match(contentText(ids['investigate-history']),/not complete chain history/);
  next=allNodes(ids['investigate-history']).find(item=>item.tag==='button'&&item.textContent==='Next page');next.listeners.click();assert.match(pending.at(-1).url,/before=77&limit=25$/);
  reply(pending.at(-1),archived('transfers'));await tick();
  ids['investigate-backfill-pages'].value='2';ids['investigate-backfill'].listeners.click();
  assert.match(pending.at(-1).url,/backfill\?kind=transfers&max_pages=2$/);
  assert.equal(ids['investigate-backfill'].disabled,true,'A pending bounded backfill must not be duplicated by repeated clicks');
  reply(pending.at(-1),{pages:2,inserted:3,next_cursor:'resume/+?token==',coverage:{provider_pages_exhausted:false,history_complete:false}});await tick();
  assert.equal(ids['investigate-backfill'].textContent,'Continue backfill');
  assert.equal(ids['investigate-history'].attributes['aria-busy'],'true','Archive refresh retains its busy state after backfill completes');
  reply(pending.at(-1),archived('transfers'));await tick();
  ids['investigate-backfill'].listeners.click();assert.equal(new URL(pending.at(-1).url,win.location).searchParams.get('cursor'),'resume/+?token==');
  const interruptedBackfill=pending.at(-1);timers.values().next().value();interruptedBackfill.reject(new Error('aborted'));await tick();
  assert.equal(ids['investigate-backfill'].textContent,'Resume backfill');
  const resumeRetry=allNodes(ids['investigate-backfill-status']).find(item=>item.tag==='button'&&item.textContent==='Retry backfill');resumeRetry.listeners.click();
  assert.equal(new URL(pending.at(-1).url,win.location).searchParams.has('cursor'),false,'Retry must resume the server checkpoint after a partially retained bounded backfill');
  reply(pending.at(-1),{pages:1,inserted:0,next_cursor:null,coverage:{provider_pages_exhausted:true,history_complete:false}});await tick();
  assert.equal(ids['investigate-backfill'].disabled,true);reply(pending.at(-1),archived('transfers'));await tick();
  ids['investigate-history-kind'].value='transactions';ids['investigate-history-kind'].listeners.change();
  assert.equal(ids['investigate-backfill'].disabled,false);ids['investigate-backfill'].listeners.click();const abandoned=pending.at(-1);
  ids['investigate-history-kind'].value='transfers';ids['investigate-history-kind'].listeners.change();assert.equal(abandoned.options.signal.aborted,true);
  reply(abandoned,{pages:1,inserted:999,next_cursor:null,coverage:{provider_pages_exhausted:true,history_complete:false}});await tick();
  assert.doesNotMatch(contentText(ids['investigate-backfill-status']),/999/,'Changing record type must ignore old backfill completion');

  const refuse = (request, status, after) => request.resolve({ok:false,status,headers:{get:name=>name==='Retry-After'?after:'application/json'},json:async()=>({message:'Read is busy.'})});
  const waitRetry = () => {
    const entry=[...timers.entries()].find(([,callback])=>callback.delay!==25000);
    assert.ok(entry,'A bounded busy response schedules a retry wait');
    timers.delete(entry[0]);entry[1]();
  };
  ids['investigate-refresh'].listeners.click();const busyReads=pending.slice(-4);
  for (const request of busyReads.slice(1)) reply(request,page('other.near'));
  refuse(busyReads[0],429,'1');await tick();
  assert.equal([...timers.values()].filter(callback=>callback.delay===25000).length,1,'The original deadline covers retry waits');
  let beforeRetry=pending.length;waitRetry();await tick();assert.equal(pending.length,beforeRetry+1);
  refuse(pending.at(-1),503,'5');await tick();waitRetry();await tick();
  refuse(pending.at(-1),503,'1');await tick();
  assert.equal(pending.length,beforeRetry+2,'A read automatically retries at most twice');
  assert.match(contentText(ids['investigate-profile']),/Read is busy/);
  assert.ok(allNodes(ids['investigate-profile']).some(item=>item.textContent==='Retry profile'));
  assert.equal(timers.size,0);
  for (const [status, after] of [[429,'60'],[503,'0'],[503,'6'],[503,'1.0'],[503,'-1'],[503,null],[500,'1']]) {
    ids['investigate-read-identity'].listeners.click();const count=pending.length;
    refuse(pending.at(-1),status,after);await tick();
    assert.equal(pending.length,count);assert.equal(timers.size,0,'Unbounded or invalid waits must remain explicit retries');
    assert.ok(allNodes(ids['investigate-identity']).some(item=>item.textContent==='Retry identity'));
  }
  for (const [status, error] of [[429,'rate_limited'],[503,'provider_budget']]) {
    ids['investigate-refresh'].listeners.click();const limited=pending.slice(-4);
    for(const request of limited.slice(1)) reply(request,page('other.near'));
    limited[0].resolve({ok:false,status,headers:{get:name=>name==='Retry-After'?'1':'application/json'},json:async()=>({error,message:'Read limit reached.'})});await tick();
    assert.equal(timers.size,0,'Rate and provider budget refusals must not silently retry initial reads');
    assert.ok(allNodes(ids['investigate-profile']).some(item=>item.textContent==='Retry profile'));
    ids['investigate-read-identity'].listeners.click();
    pending.at(-1).resolve({ok:false,status,headers:{get:name=>name==='Retry-After'?'5':'application/json'},json:async()=>({error,message:'Read limit reached.'})});await tick();
    assert.equal(timers.size,0,'Rate and provider budget refusals must not silently retry extra reads');
    assert.ok(allNodes(ids['investigate-identity']).some(item=>item.textContent==='Retry identity'));
  }
  ids['investigate-history-load'].listeners.click();refuse(pending.at(-1),503,'2');await tick();
  waitRetry();await tick();reply(pending.at(-1),archived('transfers'));await tick();
  assert.match(contentText(ids['investigate-history']),/not complete chain history/);
  ids['investigate-history-load'].listeners.click();refuse(pending.at(-1),503,'1');await tick();
  [...timers.values()].find(callback=>callback.delay===25000)();waitRetry();await tick();
  assert.match(contentText(ids['investigate-history']),/timed out/,'The original deadline also ends a retry wait');
  assert.ok(allNodes(ids['investigate-history']).some(item=>item.textContent==='Retry archive'));
  ids['investigate-read-identity'].listeners.click();refuse(pending.at(-1),429,'1');await tick();
  const staleWait=[...timers.entries()].find(([,callback])=>callback.delay!==25000);
  select('other.near');const activeCount=pending.length;
  timers.delete(staleWait[0]);staleWait[1]();await tick();
  assert.equal(pending.length,activeCount,'A stale extra read must not retry after an account change');
  for(const request of pending.slice(-4)) reply(request,request.url.includes('/profile')?{account_id:'other.near',network:'mainnet',exists:false,labels:[],source:{},coverage:{}}:page('other.near'));
  await tick();
  ids['investigate-refresh'].listeners.click();const staleReads=pending.slice(-4);
  for(const request of staleReads.slice(1)) reply(request,page('other.near'));
  refuse(staleReads[0],503,'1');await tick();
  const staleLoadWait=[...timers.entries()].find(([,callback])=>callback.delay!==25000);
  select('other.near');const currentCount=pending.length;
  timers.delete(staleLoadWait[0]);staleLoadWait[1]();await tick();
  assert.equal(pending.length,currentCount,'A stale initial read must not retry after an account change');
  for(const request of pending.slice(-4)) reply(request,request.url.includes('/profile')?{account_id:'other.near',network:'mainnet',exists:false,labels:[],source:{},coverage:{}}:page('other.near'));
  await tick();

  ids['investigate-depth'].value='2'; await ids['investigate-load-graph'].listeners.click();
  assert.equal(win.location.searchParams.get('depth'),'2');
  assert.equal(ids['investigate-graph'].children[0].attributes['data-graph-src'],'/live/accounts/other.near/graph?depth=2');
  events.pagehide();assert.equal(destroyed,1);
  win.location=new URL('https://siyab.xyz/investigate.html?account=other.near&depth=2&observation='+'b'.repeat(64));events.popstate();await tick();
  assert.equal(ids['investigate-graph'].children[0].attributes['data-graph-src'],'/live/account-data/'+'b'.repeat(64)+'.json');
  assert.equal(ids['investigate-graph'].children[0].attributes['data-graph-account'],'other.near');
  for(const request of pending.slice(-4)) reply(request, request.url.includes('/profile') ? {account_id:'other.near',network:'mainnet',exists:false,labels:[],source:{},coverage:{}} : page('other.near'));
  await tick();
  win.location=new URL('https://siyab.xyz/investigate.html');events.popstate();assert.equal(ids['investigate-title'].textContent,'Investigate account');assert.equal(ids['investigate-title'].classList.contains('investigate-subject'),false,'Reset clears the subject mark');assert.equal(ids['investigate-load-graph'].disabled,true);
  assert.equal(timers.size,0,'Every completed request releases its deadline');
  select('bad.testnet');assert.match(ids['investigate-status'].textContent,/valid NEAR mainnet/);
}
appBehavior().catch(error=>{console.error(error);process.exitCode=1;});

async function graphBehavior() {
  const timers=new Map(), pending=[];let timerId=0;
  const doc={readyState:'loading',addEventListener(){},createElement:tag=>new Element(tag)};
  const win={location:new URL('https://siyab.xyz/investigate.html?account=wrap.near')};
  vm.runInNewContext(fs.readFileSync('intel/ui/number-format.js','utf8'),{window:win});
  vm.runInNewContext(fs.readFileSync('intel/ui/graphs.js','utf8'),{window:win,document:doc,URL,AbortController,
    setTimeout(callback,delay){const id=++timerId;timers.set(id,{callback,delay});return id;},clearTimeout(id){timers.delete(id);},
    fetch(url,options){return new Promise((resolve,reject)=>{pending.push({url,options,resolve,reject});options.signal.addEventListener('abort',()=>reject(new Error('aborted')));});}});
  const graph=new Element();graph.setAttribute('data-graph-src','/live/accounts/wrap.near/graph?depth=2');
  const loading=win.SiyabGraph.mount(graph);assert.equal([...timers.values()][0].delay,25000,'Live graph has enough time for the bounded service deadline');
  assert.match(contentText(graph),/Live indexed neighborhood/);
  win.SiyabGraph.destroy(graph);await loading;
  assert.equal(pending[0].options.signal.aborted,true);assert.equal(timers.size,0);
  assert.doesNotMatch(contentText(graph),/unavailable/,'Destroyed graph must not mutate its detached DOM after abort');
  assert.equal(win.SiyabGraph.getPayload(graph),null);
  const snapshot=new Element();snapshot.setAttribute('data-graph-src','/live/account-data/'+'a'.repeat(64)+'.json');snapshot.setAttribute('data-graph-account','wrap.near');
  const restoring=win.SiyabGraph.mount(snapshot);
  pending[1].resolve({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({kind:'flow',root:'wrong.near',nodes:[{id:'wrong.near',label:'wrong.near'}],records:[],edges:[],total_nodes:1,total_records:0,source:{recorded:false}})});
  await restoring;assert.match(contentText(snapshot),/does not match the requested account/);assert.equal(win.SiyabGraph.getPayload(snapshot),null);
  assert.equal(timers.size,0);
}
graphBehavior().catch(error=>{console.error(error);process.exitCode=1;});
