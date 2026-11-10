const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag='div') { this.tag=tag;this.children=[];this.listeners={};this.attributes={};this.dataset={};this.value='';this.textContent='';this.disabled=false;this.hidden=false; }
  append(...items) {this.children.push(...items);}
  replaceChildren(...items) {this.children=[];this.textContent='';this.append(...items);}
  addEventListener(name, fn) {this.listeners[name]=fn;}
  setAttribute(name,value) {this.attributes[name]=value;}
  set innerHTML(value) {throw new Error('Untrusted HTML must never be parsed: '+value);}
}
const all = item => [item,...item.children.flatMap(all)];
const content = item => item.textContent+' '+item.children.map(content).join(' ');
const tick = () => new Promise(resolve=>setImmediate(resolve));
const entry = (account='wrap.near', changes={}) => ({entity_id:'wrapped-near',name:'Wrapped NEAR',aliases:['wNEAR'],account_id:account,
  network:'mainnet',category:'token',role:'Wrapped token',lifecycle:'documented',status:'documented',check_status:'bundled_snapshot',
  provenance:'official_source_documented',last_checked_at:null,last_document_sha256:'a'.repeat(64),
  evidence:{url:'https://example.com/source',publisher:'Official publisher',retrieved_at:'2026-11-10T00:00:00Z',
    first_retrieved_at:'2026-11-10T00:00:00Z',content_sha256:'a'.repeat(64),excerpt:'Documented exact account: '+account},...changes});

async function main() {
  const ids={},events={},windowEvents={},pending=[],timers=new Map();let timerId=0;
  const document={getElementById:id=>ids[id],createElement:tag=>new Element(tag),addEventListener:(name,fn)=>{events[name]=fn;}};
  for(const id of ['entity-enrichment','investigate-account',...['account','associations','retry','directory','search','query','category','results','directory-status','previous','next','page-count'].map(id=>'enrichment-'+id)]) ids[id]=new Element();
  ids['investigate-account'].value='wrap.near';
  const window={location:new URL('https://siyab.xyz/investigate.html?account=wrap.near'),addEventListener:(name,fn)=>{windowEvents[name]=fn;}};
  vm.runInNewContext(fs.readFileSync('intel/ui/enrichment.js','utf8'),{window,document,URL,URLSearchParams,AbortController,
    setTimeout(fn){const id=++timerId;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
    fetch(url,options){return new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));}});
  const reply=(request,data)=>request.resolve({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>data});
  const account=(subject,entities)=>({network:'mainnet',account_id:subject,entities,coverage:{}});
  const page=(items,offset=0,total=items.length,next=null)=>({network:'mainnet',items,categories:['token','dex'],pagination:{offset,limit:25,total,next_offset:next},coverage:{}});
  assert.equal(pending[0].url,'/live/accounts/wrap.near/enrichment','Initial account gets its sourced association');
  const old=pending[0];events['siyab:account-selected']({detail:{account:'other.near'}});
  assert.ok(old.options.signal.aborted);
  reply(old,account('wrap.near',[entry()]));
  reply(pending[1],account('other.near',[]));await tick();
  assert.match(content(ids['enrichment-associations']),/Ownership remains unknown/);
  assert.equal(ids['enrichment-account'].textContent,'other.near');
  assert.doesNotMatch(content(ids['enrichment-associations']),/Wrapped NEAR/,'Late old account must not replace selected account');

  events['siyab:account-selected']({detail:{account:'wrap.near'}});
  reply(pending.at(-1),account('wrap.near',[entry('wrap.near',{name:'<img src=x onerror=alert(1)>',status:'source_withdrawn',lifecycle:'deprecated',check_status:'unavailable',
    evidence:{...entry().evidence,excerpt:'<script>not executable</script>'}})]));await tick();
  const shown=content(ids['enrichment-associations']);
  assert.match(shown,/<img src=x onerror=alert\(1\)>/);assert.match(shown,/<script>not executable<\/script>/);
  assert.match(shown,/Account removed from source/);assert.match(shown,/Deprecated contract/);assert.match(shown,/Dated evidence retained/);
  const sourceLink=all(ids['enrichment-associations']).find(node=>node.target==='_blank');
  assert.equal(sourceLink.rel,'noopener noreferrer');
  assert.ok(all(ids['enrichment-associations']).some(node=>node.tag==='details'));

  events['siyab:account-selected']({detail:{account:'wrap.near'}});
  reply(pending.at(-1),account('wrap.near',[entry('wrap.near',{evidence:{...entry().evidence,url:'javascript:alert(1)'}})]));await tick();
  assert.match(content(ids['enrichment-associations']),/unavailable/);
  assert.equal(ids['enrichment-retry'].hidden,false);
  assert.equal(ids['enrichment-associations'].attributes['aria-busy'],'false');
  ids['enrichment-retry'].listeners.click();reply(pending.at(-1),account('different.near',[entry('different.near')]));await tick();
  assert.match(content(ids['enrichment-associations']),/unavailable/,'Wrong-account response is refused');
  const count=pending.length;events['siyab:account-selected']({detail:{account:'bad.testnet'}});
  assert.equal(pending.length,count,'Testnet and invalid account selections do not fetch');

  ids['enrichment-directory'].open=true;ids['enrichment-directory'].listeners.toggle();
  assert.equal(new URL(pending.at(-1).url,window.location).searchParams.get('limit'),'25');
  const first=pending.at(-1);ids['enrichment-query'].value='Ref Finance';
  ids['enrichment-search'].listeners.submit({preventDefault(){}});const searched=pending.at(-1);
  assert.ok(first.options.signal.aborted);
  assert.equal(new URL(searched.url,window.location).searchParams.get('query'),'Ref Finance');
  reply(first,page([entry()]));reply(searched,page([entry('v2.ref-finance.near',{name:'RHEA Finance'})],0,33,25));await tick();
  assert.equal(ids['enrichment-next'].disabled,false);
  assert.match(content(ids['enrichment-results']),/RHEA Finance/);
  ids['enrichment-next'].listeners.click();assert.equal(new URL(pending.at(-1).url,window.location).searchParams.get('offset'),'25');
  reply(pending.at(-1),page([entry()],25,33));await tick();
  assert.equal(ids['enrichment-previous'].disabled,false);assert.equal(ids['enrichment-next'].disabled,true);
  ids['enrichment-previous'].listeners.click();assert.equal(new URL(pending.at(-1).url,window.location).searchParams.get('offset'),'0');
  reply(pending.at(-1),page([]));await tick();assert.match(content(ids['enrichment-results']),/No documented accounts match/);
  ids['enrichment-category'].value='token';ids['enrichment-category'].listeners.change();
  assert.equal(new URL(pending.at(-1).url,window.location).searchParams.get('category'),'token');
  pending.at(-1).resolve({ok:false,status:429});await tick();
  assert.match(ids['enrichment-directory-status'].textContent,/unavailable/);assert.equal(ids['enrichment-results'].attributes['aria-busy'],'false');
  ids['enrichment-search'].listeners.submit({preventDefault(){}});const timed=pending.at(-1);
  [...timers.values()].at(-1)();timed.reject(new Error('network aborted'));await tick();
  assert.match(ids['enrichment-directory-status'].textContent,/Search again to retry/);

  ids['enrichment-search'].listeners.submit({preventDefault(){}});const leaving=pending.at(-1);
  windowEvents.pagehide();assert.ok(leaving.options.signal.aborted);
  reply(leaving,page([entry()]));await tick();assert.doesNotMatch(content(ids['enrichment-results']),/Wrapped NEAR/);
  console.log('Entity enrichment runtime: source safety, unknowns, lifecycle, races, retry, filter and pagination passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
