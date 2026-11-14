const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element{constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this.attributes={};this.textContent='';this.disabled=false;}append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this.children=[];this.textContent='';this.append(...nodes);}addEventListener(name,fn){this.listeners[name]=fn;}setAttribute(name,value){this.attributes[name]=value;}set innerHTML(value){throw Error('HTML parsing prohibited');}}
const all=root=>[root,...root.children.flatMap(all)],text=root=>root.textContent+' '+root.children.map(text).join(' '),tick=()=>new Promise(resolve=>setImmediate(resolve));
const status=(extra={})=>({configured:true,bot_username:'siyabonnearbot',state:'disconnected',counts:{pending:1,sent:2,dead:1},capacity:{paused:false,queue_paused:false,attempts_paused:false,queue_rows_limit:10000,attempt_rows_limit:50000},last_poll_ms:Date.now(),last_send_ms:null,last_error:null,last_delivery_error:null,delivery_semantics:'at_least_once',...extra});
async function main(){
 const ids={},events={},pending=[],timers=new Map(),emitted=[];let timer=0;
 for(const id of ['team-delivery',...['context','status','warning','controls','refresh','log','newer','older','history'].map(id=>'delivery-'+id)])ids[id]=new Element();
 const document={hidden:false,getElementById:id=>ids[id],createElement:tag=>new Element(tag),addEventListener(name,fn){events['document:'+name]=fn;}};
 const window={addEventListener(name,fn){events[name]=fn;},dispatchEvent(event){emitted.push(event.type);}};
 vm.runInNewContext(fs.readFileSync('intel/ui/delivery.js','utf8'),{window,document,AbortController,CustomEvent:class{constructor(type){this.type=type;}},
  setTimeout(fn,delay){const id=++timer;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);},
  fetch(url,options){return new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));}});
 const reply=(request,data,code=200)=>request.resolve({ok:code===200,status:code,headers:{get:()=> 'application/json'},json:async()=>data});
 const context=(id='team-a',role='owner')=>({detail:{authenticated:true,userId:'user-a',team:{id,name:id,role},csrfToken:'forged-event-csrf'}});
 const session=(id='team-a',role='owner')=>({authenticated:true,user:{id:'user-a'},teams:[{id,name:id,role}],csrf_token:'actual-session-csrf'});
 const control=label=>all(ids['delivery-controls']).find(node=>node.tag==='button'&&node.textContent===label);
 async function load(data,log={items:[],next_before:null}){reply(pending.at(-1),data);await tick();reply(pending.at(-1),log);await tick();}
 assert.ok(emitted.includes('siyab:workspace-context-request'));assert.equal(pending.length,0);
 events['siyab:workspace-context'](context());assert.equal(pending.at(-1).url,'/workspace-api/session');reply(pending.at(-1),session());await tick();
 await load(status({configured:false}));assert.match(text(ids['delivery-controls']),/not configured/);assert.equal(control('Connect Telegram'),undefined);
 if(process.argv[2]==='group-warning'){
  const group={groups_enabled:true,kind:'group',verified:true};
  ids['delivery-refresh'].listeners.click();await load(status({...group,state:'active'}));
  assert.match(text(ids['delivery-controls']),/Everyone in this group/);
  control('Disconnect Telegram').listeners.click();assert.match(pending.at(-1).url,/\/revoke$/);
  reply(pending.at(-1),{});await tick();await load(status({...group,state:'disconnected'}));
  assert.doesNotMatch(text(ids['delivery-controls']),/Everyone in this group/);
  for(const extra of [{state:'expired'},{state:'pending',generation:4,expires_at:Date.now()-1},{state:'observed',generation:4,expires_at:Date.now()-1,observed_chat_label:'Expired group'}]){
   ids['delivery-refresh'].listeners.click();await load(status({...group,...extra}));
   assert.doesNotMatch(text(ids['delivery-controls']),/Everyone in this group/);
  }
  events.pagehide();assert.equal(timers.size,0);
  console.log('Group warning clears after disconnect and link expiry.');return;
 }
 if(process.argv[2]==='group-conflict'){
  const expires=Date.now()+60000,warning=ids['delivery-warning'];
  const observed=status({groups_enabled:true,kind:'group',state:'observed',generation:4,expires_at:expires,observed_chat_label:'Group "Treasury" (ID ending 1234), sent by @owner',verified:true});
  ids['delivery-refresh'].listeners.click();await load({...observed,link_conflict:false,link_conflict_chats:0});
  assert.equal(warning.textContent,'');assert.equal(control('Confirm observed destination').disabled,false);
  assert.match(text(ids['delivery-controls']),/A Telegram group was observed\. Confirm this destination/);assert.doesNotMatch(text(ids['delivery-controls']),/Telegram account was observed/);
  ids['delivery-refresh'].listeners.click();await load({...observed,kind:'private'});
  assert.match(text(ids['delivery-controls']),/A Telegram account was observed\. Confirm this destination/);assert.doesNotMatch(text(ids['delivery-controls']),/Telegram group was observed/);
  ids['delivery-refresh'].listeners.click();await load({...observed,link_conflict:true,link_conflict_chats:1});
  assert.equal(warning.textContent,'This link code was also used in another chat. If you did not post it there, someone else has it: cancel this link and create a new one.');
  assert.ok(warning.textContent.length>0);
  const confirm=control('Confirm observed destination');assert.ok(confirm);assert.equal(confirm.disabled,false);assert.ok(control('Cancel connection'));
  ids['delivery-refresh'].listeners.click();await load({...observed,link_conflict:true,link_conflict_chats:3});
  assert.match(warning.textContent,/^This link code was also used in 3 other chats\. If you did not post it there/);
  for(const extra of [{state:'active'},{state:'pending',observed_chat_label:null},{state:'observed',expires_at:Date.now()-1},{kind:'private'}]){
   ids['delivery-refresh'].listeners.click();await load({...observed,link_conflict:true,link_conflict_chats:1,...extra});
   assert.equal(warning.textContent,'',JSON.stringify(extra));
  }
  for(const bad of [{link_conflict:'true'},{link_conflict:1},{link_conflict:true,link_conflict_chats:-1},{link_conflict:true,link_conflict_chats:'1'}]){
   ids['delivery-refresh'].listeners.click();await load({...observed,link_conflict:true,link_conflict_chats:1});
   ids['delivery-refresh'].listeners.click();reply(pending.at(-1),{...observed,...bad});await tick();
   assert.match(ids['delivery-status'].textContent,/could not be validated/,JSON.stringify(bad));
  }
  ids['delivery-refresh'].listeners.click();await load(observed);assert.equal(warning.textContent,'');
  ids['delivery-refresh'].listeners.click();await load({...observed,link_conflict:true,link_conflict_chats:1});
  events['siyab:workspace-context'](context('team-b','viewer'));assert.equal(warning.textContent,'');
  reply(pending.at(-1),session('team-b','viewer'));await tick();await load({...observed,link_conflict:true,link_conflict_chats:1});
  assert.equal(warning.textContent,'');events.pagehide();assert.equal(timers.size,0);
  console.log('Group link conflict warning shows only for an observed group link and keeps confirm and cancel.');return;
 }
 if(process.argv[2]==='groups'){
  ids['delivery-refresh'].listeners.click();await load(status({groups_enabled:false}));assert.equal(control('Connect a Telegram group'),undefined);
  ids['delivery-refresh'].listeners.click();await load(status({groups_enabled:true,state:'active',kind:'private'}));
  assert.equal(control('Connect a Telegram group'),undefined);
  ids['delivery-refresh'].listeners.click();await load(status({groups_enabled:true}));
  control('Connect a Telegram group').listeners.click();assert.match(pending.at(-1).url,/\/link-group$/);
  reply(pending.at(-1),{link:'https://t.me/siyabonnearbot?start='+'a'.repeat(64),generation:3,expires_at:Date.now()+60000});await tick();
  assert.match(ids['delivery-status'].textContent,/could not be validated/);assert.equal(all(ids['delivery-controls']).some(node=>node.href),false);
  ids['delivery-refresh'].listeners.click();await load(status({groups_enabled:true}));control('Connect a Telegram group').listeners.click();
  const groupLink='https://t.me/siyabonnearbot?startgroup='+'a'.repeat(64),expires=Date.now()+60000;
  reply(pending.at(-1),{link:groupLink,generation:4,expires_at:expires});await tick();
  await load(status({groups_enabled:true,kind:'group',state:'pending',generation:4,expires_at:expires,verified:false}));
  assert.equal(all(ids['delivery-controls']).find(node=>node.tag==='a').href,groupLink);
  assert.ok(text(ids['delivery-controls']).includes("Everyone in this group will see this team's alerts."));
  assert.match(text(ids['delivery-controls']),/group admin/);
  const observed=status({groups_enabled:true,kind:'group',state:'observed',generation:4,expires_at:expires,observed_chat_label:'Group "Treasury" (ID ending 1234), sent by @owner',verified:false});
  ids['delivery-refresh'].listeners.click();await load(observed);assert.equal(control('Confirm observed destination').disabled,true);
  for(const verified of [undefined,'true',1]){
   ids['delivery-refresh'].listeners.click();reply(pending.at(-1),{...observed,verified});await tick();
   assert.equal(control('Confirm observed destination').disabled,true);assert.match(ids['delivery-status'].textContent,/could not be validated/);
  }
  ids['delivery-refresh'].listeners.click();await load({...observed,verified:true});assert.equal(control('Confirm observed destination').disabled,false);
  ids['delivery-refresh'].listeners.click();reply(pending.at(-1),{...observed,verified:'true'});await tick();assert.equal(control('Confirm observed destination').disabled,true);
  ids['delivery-refresh'].listeners.click();await load({...observed,verified:true});assert.equal(control('Confirm observed destination').disabled,false);
  assert.ok(text(ids['delivery-controls']).includes(observed.observed_chat_label));control('Confirm observed destination').listeners.click();
  assert.match(pending.at(-1).url,/\/confirm$/);assert.equal(JSON.parse(pending.at(-1).options.body).generation,4);
  reply(pending.at(-1),{});await tick();await load(status({groups_enabled:true,kind:'group',state:'active',verified:true}));
  assert.equal(control('Connect a Telegram group'),undefined);
  events['siyab:workspace-context'](context('team-b','viewer'));reply(pending.at(-1),session('team-b','viewer'));await tick();await load(status({groups_enabled:true,kind:'group',state:'active'}));
  assert.equal(control('Connect a Telegram group'),undefined);events.pagehide();assert.equal(timers.size,0);
  console.log('Group connection warning, link validation, owner permissions and verified confirmation passed.');return;
 }
 if(process.argv[2])for(const fixture of JSON.parse(fs.readFileSync(process.argv[2],'utf8'))){
  ids['delivery-refresh'].listeners.click();await load(fixture.status,fixture.log);
  assert.doesNotMatch(ids['delivery-status'].textContent,/could not be validated/);
  if(fixture.phase==='capacity'){
   assert.match(text(ids['delivery-controls']),/retention limit reached for this team/i);
   assert.match(text(ids['delivery-controls']),/until an owner disconnects or connects Telegram again/);
   assert.doesNotMatch(text(ids['delivery-controls']),/back up and review/);
   assert.equal(all(ids['delivery-log']).find(node=>node.textContent==='Retry this delivery').disabled,true);
  }else{
   const label={pending:'Queued',sending:'Sending',sent:'Accepted by Telegram',dead:'Stopped after retries',cancelled:'Cancelled'}[fixture.phase];
   assert.ok(text(ids['delivery-log']).includes(label),'Backend '+fixture.phase+' response was not rendered');
  }
 }
 ids['delivery-refresh'].listeners.click();await load(status());control('Connect Telegram').listeners.click();
 assert.equal(pending.at(-1).options.headers['X-CSRF-Token'],'actual-session-csrf');assert.equal(pending.at(-1).options.method,'POST');
 const link='https://t.me/siyabonnearbot?start='+'a'.repeat(64);reply(pending.at(-1),{link,generation:2,expires_at:Date.now()+60000});await tick();await load(status({state:'pending',generation:2,expires_at:Date.now()+60000,observed_chat_label:null}));
 const anchor=all(ids['delivery-controls']).find(node=>node.tag==='a');assert.equal(anchor.href,link);assert.equal(anchor.rel,'noopener noreferrer');
 ids['delivery-refresh'].listeners.click();await load(status({state:'observed',generation:2,expires_at:Date.now()+60000,observed_chat_label:'<img src=x onerror=bad()>'}));
 assert.match(text(ids['delivery-controls']),/<img src=x onerror=bad\(\)>/);control('Confirm observed destination').listeners.click();
 assert.equal(JSON.parse(pending.at(-1).options.body).generation,2);
 reply(pending.at(-1),status({state:'active'}));await tick();await load(status({state:'active',generation:2}),{items:[{id:4,event_id:'b'.repeat(64),state:'dead',attempts:5,updated_ms:Date.now(),uncertain_ack:1}],next_before:4});
 const retry=all(ids['delivery-log']).find(node=>node.textContent==='Retry this delivery');assert.ok(retry);assert.equal(retry.disabled,false);
 assert.match(text(ids['delivery-log']),/may already have been sent/);retry.listeners.click();assert.match(pending.at(-1).url,/\/4\/retry$/);
 reply(pending.at(-1),{error:'delivery_capacity',message:'Server text.'},503);await tick();
 assert.match(ids['delivery-status'].textContent,/nothing changed/);assert.doesNotMatch(ids['delivery-status'].textContent,/may have been saved|not confirmed|Server text/);
 assert.equal(control('Disconnect Telegram').disabled,false);let requests=pending.length;retry.listeners.click();assert.equal(pending.length,requests+1);assert.match(pending.at(-1).url,/\/4\/retry$/);
 reply(pending.at(-1),{error:'workspace_busy',message:'Server text.'},503);await tick();
 assert.match(ids['delivery-status'].textContent,/may have been saved/);assert.equal(control('Disconnect Telegram').disabled,true);requests=pending.length;retry.listeners.click();assert.equal(pending.length,requests);
 ids['delivery-refresh'].listeners.click();await load(status({state:'active',generation:2}));assert.equal(control('Disconnect Telegram').disabled,false);retry.listeners.click();assert.match(pending.at(-1).url,/\/4\/retry$/);
 reply(pending.at(-1),{retried:true});await tick();await load(status({state:'active'}));
 ids['delivery-refresh'].listeners.click();await load(status({state:'active'}),{items:Array.from({length:25},(_,i)=>({id:50-i,state:'sent',attempts:1,updated_ms:Date.now()})),next_before:26});
 assert.equal(ids['delivery-older'].disabled,false);ids['delivery-older'].listeners.click();assert.match(pending.at(-1).url,/before=26$/);
 reply(pending.at(-1),{items:[{id:25,state:'cancelled',attempts:0,updated_ms:Date.now()}],next_before:25});await tick();
 assert.equal(ids['delivery-newer'].disabled,false);ids['delivery-newer'].listeners.click();assert.doesNotMatch(pending.at(-1).url,/before=/);
 reply(pending.at(-1),{items:[],next_before:null});await tick();
 control('Disconnect Telegram').listeners.click();assert.match(pending.at(-1).url,/\/revoke$/);reply(pending.at(-1),status());await tick();await load(status());
 control('Connect Telegram').listeners.click();reply(pending.at(-1),{link:'https://evil.invalid/?start='+'a'.repeat(64),generation:3,expires_at:Date.now()+60000});await tick();
 assert.match(ids['delivery-status'].textContent,/could not be validated/);assert.equal(all(ids['delivery-controls']).some(node=>node.href?.startsWith('https://evil')),false);
 ids['delivery-refresh'].listeners.click();await load(status({state:'observed',generation:3,expires_at:Date.now()-1,observed_chat_label:'Expired account'}));
 assert.equal(control('Confirm observed destination'),undefined);assert.match(text(ids['delivery-controls']),/link expired/);
 control('Create a new link').listeners.click();pending.at(-1).reject(new TypeError('Failed to fetch'));await tick();
 assert.match(ids['delivery-status'].textContent,/change may have been saved/);assert.equal(control('Create a new link').disabled,true);
 events['siyab:workspace-context'](context('team-b','viewer'));reply(pending.at(-1),session('team-b','viewer'));await tick();await load(status({state:'active'}));
 assert.equal(control('Disconnect Telegram'),undefined);assert.match(text(ids['delivery-controls']),/Only a team owner/);
 ids['delivery-refresh'].listeners.click();const stale=pending.at(-1);events['siyab:workspace-context']({detail:{authenticated:false}});assert.ok(stale.options.signal.aborted);
 reply(stale,status({state:'observed',observed_chat_label:'PRIVATE OLD TEAM',generation:5}));await tick();assert.doesNotMatch(text(ids['delivery-controls']),/PRIVATE OLD TEAM/);assert.equal(ids['delivery-log'].children.length,1);assert.equal(ids['delivery-log'].children[0].tag,'p');assert.equal(ids['delivery-log'].children[0].textContent,'Sign in and select a team to see deliveries.');assert.doesNotMatch(text(ids['delivery-log']),/PRIVATE OLD TEAM/);
 events['siyab:workspace-context'](context());reply(pending.at(-1),session());await tick();reply(pending.at(-1),{},403);await tick();assert.match(ids['delivery-context'].textContent,/session changed/);assert.equal(ids['delivery-controls'].children.length,0);
 events.pagehide();assert.equal(timers.size,0);
 console.log('Delivery UI: configuration, session verification, CSRF, fixed bot link, explicit generation confirmation, retry, revoke, permissions, stale responses and private clearing passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
