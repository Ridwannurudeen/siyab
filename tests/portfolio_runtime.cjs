const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const fixtures = JSON.parse(execFileSync(process.argv[2] || 'python', ['-c', `
import json
from intel.portfolio import PortfolioData
from tests.test_portfolio import Reader, Providers, NOW
reader, provider = Reader(), Providers()
reader.tokens[0]['metadata']['name'] = '<img src=x onerror=alert(1)>'
provider.prices.pop('near:wrap.near')
data = PortfolioData(reader, client_factory=provider.factory, clock=lambda: NOW)
current = data.inspect('alice.near')
for price in provider.prices.values(): price['timestamp'] = NOW-86400
archival = data.historical('alice.near', 50, ['wrap.near'])
large_reader = Reader()
large_reader.native['native']['amount_yocto'] = '65004280412848866147030863164068'
for price in provider.prices.values(): price['timestamp'] = NOW
large = PortfolioData(large_reader, client_factory=provider.factory, clock=lambda: NOW).inspect('alice.near')
print(json.dumps({'current': current, 'archival': archival, 'large': large}))
`], {encoding:'utf8'}));
class Element {
  constructor(tag, document) { this.tag=tag;this.document=document;this.children=[];this.listeners={};this.attributes={};this.style={};this.value='';this.disabled=false;this.ownText=''; }
  set textContent(value) {this.ownText=String(value);this.children=[];}
  get textContent() {return this.ownText+this.children.map(child=>child.textContent).join(' ');}
  set innerHTML(value) {throw new Error('HTML injection surface used');}
  get childNodes() {return this.children;}
  get firstChild() {return this.children[0]||null;}
  append(...nodes) {for(const item of nodes) {if(item.parent)item.parent.children=item.parent.children.filter(child=>child!==item);item.parent=this;this.children.push(item);}}
  replaceChildren(...nodes) {for(const child of this.children)child.parent=null;this.children=[];this.ownText='';this.append(...nodes);if(this.tag==='select')this.value=this.firstChild?.value||'';}
  setAttribute(key,value) {this.attributes[key]=String(value);}
  getAttribute(key) {return this.attributes[key]??null;}
  removeAttribute(key) {delete this.attributes[key];}
  addEventListener(name,callback) {this.listeners[name]=callback;}
  contains(target) {return this===target||this.children.some(child=>child.contains(target));}
  focus() {this.document.activeElement=this;}
  click() {if(!this.disabled)this.listeners.click?.({preventDefault(){}});}
}
const all = element => [element,...element.children.flatMap(all)];
const visibleText = element => element.ownText + (element.tag === 'details' ? element.children.filter(child=>child.tag==='summary') : element.children).map(visibleText).join(' ');
const tick = () => new Promise(resolve=>setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));
function mount() {
  const ids={},events={},windowEvents={},pending=[],timers=new Map();let timerId=0;
  const document={getElementById:id=>ids[id]||null,createElement:tag=>new Element(tag,document),addEventListener:(name,callback)=>events[name]=callback};
  const fragment=fs.readFileSync('intel/ui/portfolio-panel.html','utf8');
  for(const match of fragment.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g))ids[match[2]]=Object.assign(document.createElement(match[1]),{id:match[2]});
  ids['investigate-account']=document.createElement('input');ids['investigate-account'].value='alice.near';
  const window={Node:Element,addEventListener:(name,callback)=>windowEvents[name]=callback};
  const context={window,document,URLSearchParams,AbortController,
    setTimeout(callback){const id=++timerId;timers.set(id,callback);return id;},clearTimeout:id=>timers.delete(id),
    fetch(url,options){return new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));}};
  vm.runInNewContext(fs.readFileSync('intel/ui/number-format.js','utf8'),context);
  vm.runInNewContext(fs.readFileSync('intel/ui/portfolio.js','utf8'),context);
  return {ids,events,windowEvents,pending,timers,document};
}
const reply=(request,payload,status=200)=>request.resolve({ok:status===200,status,headers:{get:key=>key==='content-type'?'application/json':null},json:async()=>payload});
const submit=(app,id)=>app.ids[id].listeners.submit({preventDefault(){}});
const snapshot=(value,id='a'.repeat(64))=>({...clone(value),snapshot:{id,url:'/live/portfolio-data/'+id+'.json'}});
function point(value,id,scope) {return {id,url:'/live/portfolio-data/'+id+'.json',kind:value.kind,observed_at:value.observed_at,effective_at:value.effective_at,asset_scope_id:scope||value.asset_scope_id,assets_count:value.assets.length,...value.summary};}
const history=(items,asset=null)=>({account_id:'alice.near',network:'mainnet',items,pagination:{limit:100,next_before:null},coverage:{history_complete:false,asset_id:asset,stored_observations:items.length}});
async function behavior() {
  const axisApp=mount();axisApp.ids['portfolio-history-measure'].value='known';
  const archived=(value,id,time)=>({...point(fixtures.large,id),kind:'historical',assets_count:1,priced_assets:1,unpriced_assets:0,total_value_usd:null,known_value_usd:value,effective_at:time});
  const lower=archived('130763096.159999999999','1'.repeat(64),'2026-11-10T15:43:09Z');
  submit(axisApp,'portfolio-history-form');reply(axisApp.pending.at(-1),history([lower]));await tick();
  assert.match(axisApp.ids['portfolio-history-scope'].children[0].textContent,/1 read, 1 asset\)/);
  assert.match(visibleText(axisApp.ids['portfolio-history']),/1 priced, 0 unpriced of 1 asset\b/);
  const upper=archived('130763988.129999999999','2'.repeat(64),'2026-11-10T15:43:20Z');
  const middle=archived('130763400.55','3'.repeat(64),'2026-11-10T15:43:14Z');
  submit(axisApp,'portfolio-history-form');reply(axisApp.pending.at(-1),history([lower,middle,upper]));await tick();
  const chart=all(axisApp.ids['portfolio-history']).find(item=>item.className==='portfolio-chart');
  const axis=all(chart).find(item=>item.className==='portfolio-y-axis');
  assert.equal(axis.children[0].textContent,'130,763,988.13 USD');assert.equal(axis.children[1].textContent,'130,763,096.16 USD');
  assert.match(chart.attributes['aria-label'],/130763988\.129999999999/);assert.match(chart.attributes['aria-label'],/130763096\.159999999999/);
  assert.match(axisApp.ids['portfolio-history-scope'].children[0].textContent,/3 reads, 1 asset\)/);
  assert.ok(all(axisApp.ids['portfolio-history']).some(item=>item.attributes['data-export-value']==='130763988.129999999999 USD'));
  const extremeApp=mount(),maximum='340282366920938463463374607431768211455';
  extremeApp.ids['portfolio-history-asset'].value='native:near';extremeApp.ids['portfolio-history-asset'].listeners.change();
  const maximumPoint={...lower,id:'4'.repeat(64),url:'/live/portfolio-data/'+'4'.repeat(64)+'.json',asset_id:'native:near',balance:maximum,balance_base_units:maximum,decimals:0,asset_value_usd:null};
  submit(extremeApp,'portfolio-history-form');reply(extremeApp.pending.at(-1),history([maximumPoint],'native:near'));await tick();
  const maximumChart=all(extremeApp.ids['portfolio-history']).find(item=>item.className==='portfolio-chart');
  const maximumTick=all(maximumChart).find(item=>item.className==='portfolio-y-axis').children[0];
  assert.ok(maximumTick.textContent.length<=20,'U128 maximum with zero decimals must not create an unbounded axis');
  assert.match(maximumTick.textContent,/e\+38$/);assert.equal(maximumTick.title,maximum);assert.ok(maximumChart.attributes['aria-label'].includes(maximum));
  assert.ok(all(extremeApp.ids['portfolio-history']).some(item=>item.attributes['data-export-value']===maximum));
  const hugeUsd='1'+'0'.repeat(200);submit(axisApp,'portfolio-history-form');reply(axisApp.pending.at(-1),history([{...upper,known_value_usd:hugeUsd}]));await tick();
  const hugeChart=all(axisApp.ids['portfolio-history']).find(item=>item.className==='portfolio-chart');
  const hugeTick=all(hugeChart).find(item=>item.className==='portfolio-y-axis').children[0];
  assert.ok(hugeTick.textContent.length<=20,'Long finite USD values must fit the same axis budget');assert.match(hugeTick.textContent,/e\+200 USD$/);
  assert.equal(hugeTick.title,hugeUsd+' USD');assert.ok(hugeChart.attributes['aria-label'].includes(hugeUsd));
  const polished=mount();
  submit(polished,'portfolio-history-form');
  reply(polished.pending.at(-1),history([point(fixtures.large,'e'.repeat(64))]));await tick();
  assert.match(visibleText(polished.ids['portfolio-history']),/130,008,562\.83 USD/,'Partial history subtotal uses readable grouped rounding');
  assert.doesNotMatch(visibleText(polished.ids['portfolio-history']),/130008562\.825697/,'Long exact subtotal is disclosed rather than printed in the primary row');
  assert.ok(all(polished.ids['portfolio-history']).some(item=>item.tag==='details'&&item.textContent.includes(fixtures.large.summary.known_value_usd)),'History subtotal retains every source digit in a disclosure');
  assert.ok(all(polished.ids['portfolio-history']).some(item=>item.tag==='td'&&item.attributes['data-export-value']==='Not complete; known subtotal '+fixtures.large.summary.known_value_usd+' USD'),'CSV exports exact subtotal with partial coverage');
  const app=mount(),{ids,pending}=app;
  assert.equal(pending.length,0,'Opening the page never triggers portfolio reads');
  ids['portfolio-read'].click();assert.match(pending[0].url,/alice.near\/portfolio\?max_tokens=40$/);
  reply(pending[0],snapshot(fixtures.current));await tick();
  const text=ids['portfolio-current'].textContent;
  assert.match(text,/Known priced subtotal, not a complete total/);
  assert.match(text,/<img src=x onerror=alert\(1\)>/,'Provider names stay literal');
  assert.ok(!all(ids['portfolio-current']).some(item=>['img','script'].includes(item.tag)));
  assert.match(text,/6\.000000000000000000000002/,'Exact source value remains disclosed');
  assert.match(text,/1000000000000000000000000 base units/);
  assert.ok(all(ids['portfolio-current']).some(item=>item.className?.includes('portfolio-number')&&item.textContent.includes('6 USD')),'Primary figures use shared readable numbers');
  assert.match(text,/Not established/,'Unknown prices do not become zero');
  assert.ok(all(ids['portfolio-current']).some(item=>item.attributes['data-export-table']==='portfolio-assets-table'));
  ids['portfolio-read'].click();
  assert.doesNotMatch(ids['portfolio-current'].textContent,/6\.000000000000000000000002/,'Refreshing immediately clears old values');
  reply(pending.at(-1),{message:'Provider temporarily unavailable'},503);await tick();
  const retry=all(ids['portfolio-current']).find(item=>item.tag==='button'&&item.textContent==='Retry current valuation');
  assert.ok(retry);retry.click();reply(pending.at(-1),snapshot(fixtures.current));await tick();
  ids['portfolio-read'].click();const stale=pending.at(-1);
  app.events['siyab:account-selected']({detail:{account:'bob.near'}});
  assert.equal(stale.options.signal.aborted,true);
  reply(stale,snapshot(fixtures.current));await tick();assert.doesNotMatch(ids['portfolio-current'].textContent,/6\.000000/);
  assert.match(ids['portfolio-current'].textContent,/bob.near/);
  app.events['siyab:account-selected']({detail:{account:'alice.near'}});
  ids['portfolio-read'].click();const timed=pending.at(-1);app.timers.values().next().value();await tick();
  assert.equal(timed.options.signal.aborted,true);
  assert.match(ids['portfolio-current'].textContent,/25 seconds/,'A fetch that ignores abort still times out');
  assert.equal(ids['portfolio-read'].disabled,false);
  reply(timed,snapshot(fixtures.current));await tick();assert.match(ids['portfolio-current'].textContent,/25 seconds/,'Late timed-out completion cannot revive data');
  submit(app,'portfolio-history-form');
  const first=point(fixtures.current,'b'.repeat(64)), second=point(fixtures.archival,'c'.repeat(64),'d'.repeat(64));
  reply(pending.at(-1),history([first,second]));await tick();
  assert.equal(ids['portfolio-history-scope'].children.length,2,'Different inventories stay separate');
  assert.equal(all(ids['portfolio-history']).filter(item=>item.className?.includes('portfolio-mark')).length,0,'Incomplete total has no numeric chart mark');
  ids['portfolio-history-measure'].value='known';ids['portfolio-history-measure'].listeners.change();
  assert.equal(all(ids['portfolio-history']).filter(item=>item.className?.includes('portfolio-mark')).length,1);
  assert.ok(!all(ids['portfolio-history']).some(item=>['line','polyline','path'].includes(item.tag)),'Observations are never connected across gaps');
  assert.match(ids['portfolio-history'].textContent,/6\.000000000000000000000002/);
  ids['portfolio-history-asset'].value='native:near';ids['portfolio-history-asset'].listeners.change();submit(app,'portfolio-history-form');
  assert.equal(new URL(pending.at(-1).url,'https://siyab.xyz').searchParams.get('asset_id'),'native:near');
  const native={...first,asset_id:'native:near',balance:fixtures.current.assets[0].balance,balance_base_units:fixtures.current.assets[0].balance_base_units,decimals:24,asset_value_usd:fixtures.current.assets[0].value_usd};
  reply(pending.at(-1),history([native],'native:near'));await tick();
  assert.match(ids['portfolio-history'].textContent,/3000000000000000000000001 base units/);
  submit(app,'portfolio-history-form');reply(pending.at(-1),history([],'native:near'));await tick();assert.match(ids['portfolio-history'].textContent,/No valuation has been retained/);
  ids['portfolio-block-height'].value='18446744073709551616';submit(app,'portfolio-block-form');assert.match(ids['portfolio-block-height-error'].textContent,/positive whole/);
  ids['portfolio-block-height'].value='50';ids['portfolio-block-contracts'].value='wrap.near,wrap.near';submit(app,'portfolio-block-form');assert.match(ids['portfolio-block-contracts-error'].textContent,/once/);
  ids['portfolio-block-contracts'].value='wrap.near';submit(app,'portfolio-block-form');assert.match(pending.at(-1).url,/block_height=50&contracts=wrap.near$/);
  reply(pending.at(-1),snapshot(fixtures.archival));await tick();assert.match(ids['portfolio-block'].textContent,/Only native NEAR and the listed contracts/);
  const malicious=snapshot(fixtures.current);malicious.snapshot.url='https://attacker.example/data.json';ids['portfolio-read'].click();reply(pending.at(-1),malicious);await tick();assert.match(ids['portfolio-current'].textContent,/could not be read/);
  assert.ok(!all(ids['portfolio-current']).some(item=>item.href?.includes('attacker')));
  app.windowEvents.pagehide();assert.equal(app.timers.size,0);
  vm.runInNewContext(fs.readFileSync('intel/ui/portfolio.js','utf8'),{document:{getElementById:()=>null}});
  console.log('Portfolio runtime checks passed');
}
behavior().catch(error=>{console.error(error);process.exitCode=1;});
