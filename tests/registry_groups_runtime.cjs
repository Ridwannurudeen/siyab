const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Node{
 constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.listeners={};this.attributes={};this.textContent='';this.value='';this.hidden=false;}
 append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;this.textContent='';}setAttribute(key,value){this.attributes[key]=value;}getAttribute(key){return this.attributes[key]??null;}addEventListener(name,fn){this.listeners[name]=fn;}emit(name){this.listeners[name]({preventDefault(){}});}focus(){}
}
const a='a'.repeat(64),b='b'.repeat(64),tick=()=>new Promise(resolve=>setImmediate(resolve));
function response(rows,{size=null,chunk=null}={}){
 const bytes=chunk||new TextEncoder().encode(JSON.stringify({rows}));let sent=false;
 return {ok:true,headers:{get:()=>size},body:{getReader:()=>({async read(){if(sent)return {done:true};sent=true;return {done:false,value:bytes};},releaseLock(){}})}};
}
function setup(query=''){
 const nodes=Object.fromEntries(['.entity-table-toolbar','[data-table-search]','[data-table-chain]','[data-table-sort]','[data-table-group]','.entity-table-status','.entity-table-pagination','[data-table-retry]','[data-table-empty]'].map(key=>[key,new Node()]));
 nodes['[data-table-sort]'].value='recorded';nodes['[data-table-group]'].options=[{value:''},{value:a},{value:b}];
 const panel=new Node(),body=new Node(),section=new Node(),count=new Node(),disclosure=new Node();body.append(new Node('original-row'));
 panel.dataset={source:'organisations.table.json',page:'1'};panel.querySelector=key=>nodes[key];panel.querySelectorAll=()=>[section];
 section.dataset.entityKind='stated';section.querySelector=key=>key==='tbody'?body:count;
 section.querySelectorAll=()=>['Subject','Chain','Label','Category','Provenance','Note'].map(text=>Object.assign(new Node('th'),{textContent:text}));
 const links=[a,b].map(id=>Object.assign(new Node('a'),{dataset:{registryGroup:id}})),requests=[];
 const document={activeElement:null,querySelectorAll:key=>key==='[data-entity-table]'?[panel]:links,getElementById:()=>disclosure,createElement:tag=>new Node(tag),createTextNode:text=>Object.assign(new Node('text'),{textContent:text})};
 const window={location:new URL('https://siyab.xyz/entities/organisations.html'+query),history:{replaceState(state,title,url){window.location=new URL(url);}}};
 vm.runInNewContext(fs.readFileSync('intel/ui/tables.js','utf8'),{document,window,URL,AbortController,TextDecoder,setTimeout,clearTimeout,
  fetch(url,options){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}});
 return {nodes,body,requests,links,window,disclosure};
}
const rows=Array.from({length:70},(_,i)=>({id:(i+1).toString(16).padStart(64,'0'),kind:'stated',source_group:i<60?a:b,subject:'account-'+(i+1)+'.near',chain:i%2?'near':'eth',label:'Recorded factory association',note:'Registry "source", exact identifier '+(i+1),search:'account-'+(i+1)+'.near registry',cells:[[{text:'account-'+(i+1)+'.near',quoted:true}], [{text:i%2?'near':'eth'}],[{text:'Recorded factory association'}],[{text:'organisation'}],[{text:'onchain'}],[{text:'Recorded source, exact identifier '+(i+1)}]]}));
async function main(){
 const env=setup();assert.equal(env.requests.length,0,'Overview never loads the full dataset initially');
 env.links[0].emit('click');assert.equal(env.requests.length,1);assert.equal(env.disclosure.open,true);
 assert.equal(env.window.location.searchParams.get('source'),a);env.requests[0].resolve(response(rows));await tick();
 assert.equal(env.body.children.length,50);assert.ok(env.body.children.every(row=>rows.find(item=>item.id===row.dataset.assertionId).source_group===a));
 assert.equal(env.body.children[0].children[5].attributes['data-export-value'],rows[0].note,'Shown-row CSV retains exact original quotation punctuation');
 env.nodes['.entity-table-pagination'].children[2].emit('click');assert.equal(env.body.children.length,10);
 const workspace=fs.readFileSync('intel/ui/workspace.js','utf8'),extracted=workspace.slice(workspace.indexOf('  const csv ='),workspace.indexOf('  function viewUrl()'));let exported='';
 const exportTable={querySelectorAll:()=>env.body.children.map(row=>{row.querySelectorAll=()=>row.children;return row;})};
 const exporter={document:{getElementById:()=>exportTable},download:(name,value)=>{exported=value;},message(){}};
 vm.runInNewContext(extracted+';globalThis.exportPage=tableExport;',exporter);exporter.exportPage('entity-collection-stated');
 assert.equal(exported.trim().split('\r\n').length,10,'CSV contains only the current filtered page');assert.match(exported,/account-51\.near/);assert.doesNotMatch(exported,/account-1\.near/);
 env.body.children[0].children[0].attributes['data-export-value']='=SUM(1,2)';exporter.exportPage('entity-collection-stated');assert.match(exported,/"'=SUM\(1,2\)"/,'CSV formula protection remains active for exact source values');
 env.nodes['[data-table-chain]'].value='eth';env.nodes['[data-table-chain]'].emit('change');await tick();assert.equal(env.body.children.length,30);
 env.nodes['[data-table-search]'].value='account-3';env.nodes['[data-table-search]'].emit('input');await tick();assert.equal(env.body.children.length,6);
 env.nodes['[data-table-search]'].value='';env.nodes['[data-table-search]'].emit('input');await tick();env.links[1].emit('click');await tick();assert.equal(env.body.children.length,5);
 env.nodes['[data-table-group]'].value='';env.nodes['[data-table-group]'].emit('change');await tick();assert.equal(env.body.children.length,35);assert.equal(env.window.location.searchParams.has('source'),false);
 const linked=setup('?source='+b+'#individual-assertions');assert.equal(linked.requests.length,1);linked.requests[0].resolve(response(rows));await tick();assert.equal(linked.body.children.length,10);
 for(const value of ['not-a-source','c'.repeat(64)]){const invalid=setup('?source='+value+'&chain=near');assert.equal(invalid.requests.length,0);assert.match(invalid.nodes['.entity-table-status'].textContent,/not in this recorded collection/);assert.equal(invalid.nodes['[data-table-group]'].value,'');assert.equal(invalid.window.location.searchParams.has('source'),false);assert.equal(invalid.window.location.searchParams.get('chain'),'near');assert.equal(invalid.body.children[0].tag,'original-row');}
 for(const answer of [response(rows,{size:String(4*1024*1024+1)}),response(rows,{chunk:new Uint8Array(4*1024*1024+1)}),response(Array(10001).fill(null)),response([{...rows[0],source_group:'c'.repeat(64)}])]){
  const bad=setup();bad.links[0].emit('click');bad.requests[0].resolve(answer);await tick();
  assert.equal(bad.body.children[0].tag,'original-row','A bounded or invalid response retains static records');assert.equal(bad.nodes['[data-table-retry]'].hidden,false);assert.equal(bad.requests[0].options.signal.aborted,true);
 }
 console.log('Registry overview runtime: lazy exact-source permalinks, intersected filters, 50-row views, bounded response/row counts and retained static fallback passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
