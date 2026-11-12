const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const flush = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.attributes = {}; this.textContent = ''; this.value = ''; this.disabled = false; this.hidden = false; this.id = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.textContent = ''; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  dispatchEvent(event) { for (const fn of this.listeners[event.type] || []) fn(event); return true; }
  setAttribute(key, value) { this.attributes[key] = value; }
  focus() { if (this.document) this.document.activeElement = this; }
  click() { this.dispatchEvent({type: 'click'}); }
  remove() { this.removed = true; }
}
const text = node => [node.textContent, ...node.children.map(text)].join(' ');
function environment(ignoreAbort = false) {
  const nodes = {}, requests = [], timers = new Map(), downloads = [], writes = []; let next = 0;
  const defaults = {account: '', kind: 'transfers', from: '', until: '', pages: '20', rows: '1000', depth: '3', accounts: '20', 'account-pages': '2'};
  const document = new Element('document'); document.visibilityState = 'visible'; document.body = new Element('body'); document.activeElement = document.body;
  document.createElement = tag => { const node = new Element(tag); node.document = document; return node; };
  const find = (node, id) => node.id === id ? node : node.children.map(child => find(child, id)).find(Boolean);
  document.getElementById = id => nodes[id] || find(document.body, id);
  for (const id of ['investigation-jobs','jobs-form','jobs-list','jobs-status','jobs-context','jobs-results','jobs-result-body','jobs-refresh','jobs-submit','jobs-results-title','jobs-graph-fields']) { nodes[id] = document.createElement('div'); nodes[id].id = id; document.body.append(nodes[id]); }
  for (const [name, value] of Object.entries(defaults)) { const node = document.createElement('input'); node.id = 'jobs-' + name; node.value = value; nodes[node.id] = node; document.body.append(node); }
  nodes['jobs-form'].reset = () => { for (const [name, value] of Object.entries(defaults)) nodes['jobs-' + name].value = value; };
  const window = new Element('window');
  class Event { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } }
  const context = {document, window, CustomEvent: Event, URL: class extends URL {static createObjectURL(blob) {downloads.push(blob); return 'blob:download';} static revokeObjectURL() {}}, Blob, AbortController, Date,
    localStorage: {setItem(...args) {writes.push(args);}, getItem() {return null;}},
    setTimeout: (callback, delay) => { const id = ++next; timers.set(id, {callback, delay}); return id; }, clearTimeout: id => timers.delete(id),
    fetch: (path, options) => new Promise((resolve, reject) => { const request = {path, options, resolve, reject}; requests.push(request); if (!ignoreAbort) options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name:'AbortError'}))); })};
  vm.runInNewContext(fs.readFileSync('intel/ui/number-format.js', 'utf8'), context);
  const source = fs.readFileSync('intel/ui/investigation-jobs.js', 'utf8'); const marker = source.lastIndexOf('})();');
  vm.runInNewContext(source.slice(0, marker) + 'globalThis.jobsTest={state,contextChanged,loadResults,request,loadList,mutate,report};\n' + source.slice(marker), context);
  return {nodes, document, window, requests, timers, downloads, writes, api: context.jobsTest, emit: detail => window.dispatchEvent(new Event('siyab:workspace-context', {detail}))};
}
async function reply(request, data, status = 200) { request.resolve({ok: status < 400, status, headers: {get: () => 'application/json'}, json: async () => JSON.parse(JSON.stringify(data))}); await flush(); }
function detail(team = fixture.team) { return {authenticated: true, userId: fixture.session.user.id, team, csrfToken: fixture.session.csrf_token}; }
async function signed(env, jobs = [fixture.queued], role = 'owner') {
  const team = {...fixture.team, role}; env.emit(detail(team));
  await reply(env.requests.at(-1), {...fixture.session, teams: [team]});
  await reply(env.requests.at(-1), {items: jobs});
}
async function main() {
  const scenario = process.argv[2];
  if (scenario === 'auth-team-race') {
    const env = environment(true); env.emit(detail()); await reply(env.requests[0], fixture.session);
    const old = env.requests[1]; const other = {...fixture.team2, role: 'owner'};
    env.emit(detail(other)); await reply(env.requests[2], {...fixture.session, teams: [other]}); await reply(env.requests[3], {items: [fixture.other]});
    await reply(old, {items: [fixture.queued]}); assert.match(text(env.nodes['jobs-list']), /bob.near/); assert.doesNotMatch(text(env.nodes['jobs-list']), /alice.near/);
    env.emit({authenticated: false}); assert.equal(text(env.nodes['jobs-list']).trim(), ''); assert.equal(env.nodes['jobs-form'].hidden, true);
    assert.equal([...env.timers.values()].filter(timer => timer.delay === 20000).length, 0); assert.equal(env.writes.length, 0);
  } else if (scenario === 'viewer-and-visibility') {
    const env = environment(); await signed(env, [fixture.queued], 'viewer'); assert.equal(env.nodes['jobs-form'].hidden, true);
    const cancel = env.document.getElementById('jobs-cancel-' + fixture.queued.id); assert.equal(cancel.disabled, true);
    const count = env.requests.length; await env.api.mutate(fixture.queued.id, 'cancel'); assert.equal(env.requests.length, count);
    assert.equal([...env.timers.values()].filter(timer => timer.delay === 20000).length, 1);
    env.document.visibilityState = 'hidden'; env.document.dispatchEvent({type:'visibilitychange'}); assert.equal([...env.timers.values()].filter(timer => timer.delay === 20000).length, 0);
    env.document.visibilityState = 'visible'; env.document.dispatchEvent({type:'visibilitychange'}); assert.equal(env.requests.length, count + 1);
  } else if (scenario === 'timeout-and-permission') {
    const env = environment(); await signed(env); env.nodes['jobs-account'].value = 'alice.near';
    const submit = env.nodes['jobs-form'].listeners.submit[0]({preventDefault(){}}); await flush(); const post = env.requests.at(-1);
    assert.equal(post.options.method, 'POST'); assert.equal(post.options.headers['X-CSRF-Token'], fixture.session.csrf_token);
    assert.deepEqual(JSON.parse(post.options.body), {account:'alice.near',kind:'transfers',max_pages:20,max_rows:1000});
    [...env.timers.values()].find(timer => timer.delay === 12000).callback(); await submit;
    assert.match(text(env.nodes['jobs-status']), /change may have been saved/); assert.equal(env.nodes['jobs-submit'].disabled, true);
    const reload = env.api.loadList(); await reply(env.requests.at(-1), {items:[fixture.queued]}); await reload; assert.equal(env.nodes['jobs-submit'].disabled, false);
    const mutation = env.api.mutate(fixture.queued.id, 'cancel'); await reply(env.requests.at(-1), {message:'Your team role does not permit this action.'}, 403); await mutation;
    assert.equal(env.nodes['jobs-form'].hidden, true); assert.equal(text(env.nodes['jobs-list']).trim(), '');
  } else if (scenario === 'results-and-stale-auth') {
    const env = environment(true); await signed(env, [fixture.completed]);
    const result = env.api.loadResults(fixture.completed.id); await reply(env.requests.at(-1), fixture.result); await result;
    assert.match(text(env.nodes['jobs-result-body']), /Unknown sender/); assert.match(text(env.nodes['jobs-result-body']), /1000000000000000000000001/);
    assert.match(text(env.nodes['jobs-result-body']), /Complete chain history is not established/);
    const stale = env.api.loadResults(fixture.completed.id).catch(error => env.api.report(error)); const request = env.requests.at(-1); env.emit({authenticated:false}); await reply(request, fixture.result); await stale;
    assert.equal(text(env.nodes['jobs-result-body']).trim(), ''); assert.equal(env.nodes['jobs-results'].hidden, true);
  } else if (scenario === 'result-selection-race') {
    const env = environment(true); await signed(env, [fixture.completed]);
    const first = env.api.loadResults(fixture.completed.id).catch(error => env.api.report(error)), old = env.requests.at(-1);
    const second = env.api.loadResults(fixture.completed.id), fresh = env.requests.at(-1);
    await reply(fresh, {...fixture.result,items:[{...fixture.result.items[0],receipt_id:'latest-receipt'}]}); await second;
    await reply(old, {...fixture.result,items:[{...fixture.result.items[0],receipt_id:'stale-receipt'}]}); await first;
    assert.match(text(env.nodes['jobs-result-body']), /latest-receipt/); assert.doesNotMatch(text(env.nodes['jobs-result-body']), /stale-receipt/);
  } else if (scenario === 'unknown-and-bounds') {
    const env = environment(); await signed(env);
    const utcJob = {...fixture.queued,last_error:'watch_priority',bounds:{...fixture.queued.bounds,from_timestamp_ms:1794325863000,to_timestamp_ms:1794325923000}};
    const listing = env.api.loadList(); await reply(env.requests.at(-1), {items:[utcJob]}); await listing;
    assert.match(text(env.nodes['jobs-list']), /2026-11-10 15:51:03 UTC/);
    assert.match(text(env.nodes['jobs-list']), /Queued\. .*Paused while due watched accounts are polled\. After three short pauses in a row it reads its next page anyway\./s);
    assert.doesNotMatch(text(env.nodes['jobs-list']), /archive capacity/);
    const failed = env.api.loadList(); await reply(env.requests.at(-1), {items:[{...fixture.queued,state:'failed',last_error:'job_capacity',attempts:0}]}); await failed;
    assert.match(text(env.nodes['jobs-list']), /Collection stopped at the job storage limit/);
    assert.match(text(env.nodes['jobs-list']), /Job storage capacity was reached\. Retained results remain available/);
    assert.doesNotMatch(text(env.nodes['jobs-list']), /repeated errors|could not read its source/);
    const waiting = env.api.loadList(); await reply(env.requests.at(-1), {items:[{...fixture.queued,last_error:'history_capacity',attempts:0}]}); await waiting;
    assert.match(text(env.nodes['jobs-list']), /Waiting for read or storage capacity/);
    env.nodes['jobs-status'].textContent = 'Investigation queued. Progress follows the worker and source budgets shown below.';
    const completed = env.api.loadList(); await reply(env.requests.at(-1), {items:[fixture.completed]}); await completed;
    assert.match(text(env.nodes['jobs-list']), /Finished within bounds/); assert.doesNotMatch(text(env.nodes['jobs-status']), /queued/i);
    const native = {...fixture.result,items:[{...fixture.result.items[0],amount:'1250000000000000000000',asset_id:'native:near',timestamp_ms:1794325863000}]};
    const result = env.api.loadResults(fixture.completed.id); await reply(env.requests.at(-1), native); await result;
    const descendants = node => [node,...node.children.flatMap(descendants)];
    const amount = descendants(env.nodes['jobs-result-body']).find(node => node.tag === 'td' && node.dataset.label === 'Amount');
    assert.match(text(amount), /0\.0013 NEAR/); assert.equal(amount.dataset.exportValue, '1250000000000000000000');
    assert.match(text(amount), /1250000000000000000000/); assert.match(text(env.nodes['jobs-result-body']), /2026-11-10 15:51:03 UTC/);
    descendants(env.nodes['jobs-result-body']).find(node => node.tag === 'button' && node.textContent === 'Download this result page').click();
    assert.equal(JSON.parse(await env.downloads.at(-1).text()).items[0].amount, '1250000000000000000000');
    const token = env.api.loadResults(fixture.completed.id); await reply(env.requests.at(-1), {...native,items:[{...native.items[0],asset_id:'nep141:unknown.near'}]}); await token;
    const rawAmount = descendants(env.nodes['jobs-result-body']).find(node => node.tag === 'td' && node.dataset.label === 'Amount');
    assert.match(text(rawAmount), /1250000000000000000000 base units of nep141:unknown.near/); assert.doesNotMatch(text(rawAmount), /0\.0013 NEAR/);
    const pending = env.api.loadList(); const refused = assert.rejects(pending, /could not be read/); await reply(env.requests.at(-1), {items:[{...fixture.queued,counts:{pages:null}}]}); await refused;
    env.nodes['jobs-kind'].value = 'graph'; env.nodes['jobs-kind'].dispatchEvent({type:'change'}); assert.equal(env.nodes['jobs-rows'].value, '200');
    env.nodes['jobs-account'].value = 'alice.near'; env.nodes['jobs-depth'].value = '6'; const count = env.requests.length;
    await env.nodes['jobs-form'].listeners.submit[0]({preventDefault(){}}); assert.equal(env.requests.length, count); assert.match(text(env.nodes['jobs-status']), /shown ranges/);
  } else if (scenario === 'private-graph-download') {
    const env = environment(); await signed(env, [fixture.completed]);
    const load = env.api.loadResults(fixture.completed.id); await reply(env.requests.at(-1), fixture.result); await load;
    const descendants = node => [node,...node.children.flatMap(descendants)];
    const download = descendants(env.nodes['jobs-result-body']).find(node => node.tag === 'button' && node.textContent === 'Download private graph JSON');
    assert.ok(download, 'Authorized graph result must retain an explicit download');
    assert.equal(descendants(env.nodes['jobs-result-body']).filter(node => node.tag === 'a' && node.href?.startsWith('/live/account-data/')).length, 0);
    download.click(); assert.equal(env.downloads.length, 1);
    assert.deepEqual(JSON.parse(await env.downloads[0].text()), fixture.result.graph);
    assert.equal(env.requests.some(request => request.path.startsWith('/live/')), false);
    env.emit({authenticated:false}); assert.equal(env.nodes['jobs-results'].hidden, true); assert.equal(text(env.nodes['jobs-result-body']).trim(), '');
  } else if (scenario === 'cloud-context-hook') {
    const env = environment(); const seen = []; env.window.addEventListener('siyab:workspace-context', event => seen.push(event.detail));
    const source = fs.readFileSync('intel/ui/cloud-workspace.js','utf8');
    const context = {document:{getElementById:()=>null,querySelectorAll:()=>[]},window:env.window,CustomEvent:class{constructor(type,options={}){this.type=type;this.detail=options.detail;}},AbortController,setTimeout,clearTimeout,
      fetch:async()=>({ok:true,headers:{get:()=> 'application/json'},json:async()=>fixture.session})};
    vm.runInNewContext(source,context); await flush(); assert.equal(seen[0].authenticated,false); assert.equal(seen.at(-1).authenticated,true);
    assert.equal(seen.at(-1).team.id,fixture.team.id); env.window.dispatchEvent({type:'pagehide'}); assert.equal(seen.at(-1).authenticated,false); assert.equal(seen.at(-1).csrfToken,null);
  } else throw new Error('Unknown scenario');
}
main().catch(error => {console.error(error);process.exitCode=1;});
