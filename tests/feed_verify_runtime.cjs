const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = '') { this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.value = ''; this.textContent = ''; this.hidden = false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.textContent = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  emit(name, event = {}) { for (const callback of this.listeners[name] || []) callback({preventDefault() {}, ...event}); }
  querySelectorAll(tag) { return this.children.flatMap(child => [...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)]); }
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const text = node => node.textContent + node.children.map(text).join(' ');
function environment(source, ignoreAbort = false) {
  const nodes = {}, requests = [], timers = new Map(), notifications = [];
  const document = new Element(), window = new Element();
  vm.runInNewContext(fs.readFileSync('intel/ui/number-format.js','utf8'),{window});
  document.hidden = false;
  document.getElementById = id => nodes[id] ||= new Element();
  document.createElement = tag => new Element(tag);
  document.dispatchEvent = event => notifications.push(event);
  let timerId = 0;
  vm.runInNewContext(source, {document, window, AbortController, TextDecoder, Uint8Array,
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    fetch: (url, options) => new Promise((resolve, reject) => {
      requests.push({url, options, resolve, reject});
      if (!ignoreAbort) options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    }),
    setTimeout(callback, delay) { timers.set(++timerId, {callback, delay}); return timerId; },
    clearTimeout(id) { timers.delete(id); }, console});
  return {nodes, requests, timers, notifications, document, window,
    run(delay) { const entry = [...timers].find(([, value]) => value.delay === delay); assert.ok(entry, `Missing ${delay}ms timer`); timers.delete(entry[0]); entry[1].callback(); },
    async reply(index, body, ok = true) { requests[index].resolve({ok, status: ok ? 200 : 503, headers: {get: () => 'application/json'}, json: async () => body}); await flush(); }};
}
const signal = (name, extra = {}) => ({signal: name, detail: name + ' detail', subject: 'agent.near', observed: {block_height: 1, timestamp_ms: 0}, source: {provider: 'Recorded blocks'}, ...extra});
const response = (events, event_ids) => ({events, event_ids, stats: {mode: 'follow', start_height: 1, last_height: 1, lag_blocks: 0, updated_at: '2026-11-10T00:00:00Z'}, follower: 'running'});
const record = hash => ({verdict: 'Allow', evidence_hash: hash, observed_block: 12, recorded_ms: 0, verdict_id: 'test-record'});
const rpc = value => ({result: {result: Array.from(Buffer.from(JSON.stringify(value)))}});
async function main() {
  const scenario = process.argv[2], verify = scenario.startsWith('verify-');
  const script = verify ? fs.readFileSync(0, 'utf8') : fs.readFileSync('intel/ui/feed.js', 'utf8');
  const env = environment(script, scenario === 'verify-stale');
  if (verify) {
    env.nodes.registry.value = 'registry.example.testnet'; env.nodes.subject.value = 'vendor.testnet'; env.nodes.hash.value = 'a'.repeat(64);
  }
  if (scenario === 'feed-future') {
    await env.reply(0, {...response([], []), stats: {updated_at: new Date(Date.now() + 3000).toISOString()}});
    assert.doesNotMatch(text(env.nodes['feed-health']), /STALE|unreadable/);
    assert.equal(text(env.nodes['feed-update-status']), 'Current as of 0 s ago, from the last successful read.');
  } else if (scenario === 'feed-not-run') {
    await env.reply(0, {...response([], []), stats: null});
    assert.equal(text(env.nodes['feed-health']), 'The feed has not run on this server.');
  } else if (scenario === 'feed-fresh-status') {
    await env.reply(0, {...response([], []), stats: {updated_at: new Date(Date.now() - 119000).toISOString()}});
    assert.equal(text(env.nodes['feed-update-status']), 'Current as of 119 s ago, from the last successful read.');
  } else if (scenario === 'feed-freshness') {
    const now = Date.now();
    const data = (age, follower = 'running') => ({...response([], []), follower, stats: {...response([], []).stats, updated_at: new Date(now - age).toISOString()}});
    for (const item of [data(14 * 86400000), data(20000, 'stopped'), data(20000, null), {...data(20000), stats: {}}, {...data(20000), stats: {updated_at: 'unreadable'}}, {...data(20000), stats: {last_time: new Date(now - 14 * 86400000).toISOString()}}]) {
      const stale = environment(script); await stale.reply(0, item);
      assert.match(text(stale.nodes['feed-health']), /STALE/);
      assert.match(text(stale.nodes['feed-health']), /Events below are from that window, not from now|Freshness unavailable/);
      assert.doesNotMatch(text(stale.nodes['feed-update-status']), /current/i);
    }
    await env.reply(0, data(20000));
    assert.doesNotMatch(text(env.nodes['feed-health']), /STALE/);
    assert.match(text(env.nodes['feed-update-status']), /Current as of 20 s ago/);
    env.run(30000); await env.reply(1, data(121000));
    assert.match(text(env.nodes['feed-health']), /STALE/);
    assert.match(text(env.nodes['feed-update-status']), /Read succeeded. The follower data it returned is 2 min old/);
    env.nodes['feed-pause'].emit('click'); assert.match(text(env.nodes['feed-update-status']), /paused/);
  } else if (scenario === 'feed-filter') {
    const raw = 'full access key ed25519:abc added to agent.near; transfer of 1000000000000000000000000 yoctoNEAR to x.near within 452158 ms';
    const a = signal('full_access_key_then_outflow', {destination: 'implicit', amount_yocto: '1000000000000000000000000', detail: raw});
    const b = signal('labelled_account_large_transfer', {other_account_id: 'counterparty.near', usd_amount: '1000.0001', asset_id: 'native:near', source: {provider: 'FastNEAR Transfers'}});
    await env.reply(0, response([a, b], ['a'.repeat(64), 'b'.repeat(64)]));
    assert.match(text(env.nodes['feed-events']), /1970-01-01/);
    assert.ok(env.nodes['feed-events'].querySelectorAll('td').some(node => node.getAttribute('data-export-value') === 'full access key then outflow' + raw), 'Signal export keeps the exact original text without a separator');
    assert.ok(env.nodes['feed-events'].querySelectorAll('p').some(node => node.textContent === 'full access key ed25519:abc added to agent.near; transfer of 1 NEAR to x.near within 7 min 32 s'), 'Live detail prose uses readable units');
    assert.equal(env.nodes['feed-events'].querySelectorAll('p').some(node => /yoctoNEAR| ms$/.test(node.textContent)), false, 'No raw yocto or millisecond prose in the description');
    assert.match(text(env.nodes['feed-events']), /1000000000000000000000000 yoctoNEAR to x\.near within 452158 ms/, 'Raw event JSON keeps the exact recorded text');
    assert.match(text(env.nodes['feed-events']), /1,000 USD/);
    assert.match(text(env.nodes['feed-events']), /"usd_amount": "1000\.0001"/);
    assert.ok(env.nodes['feed-events'].querySelectorAll('td').some(node=>node.getAttribute('data-export-value')==='1000.0001 USD (provider value)'));
    assert.equal(env.nodes['feed-events'].querySelectorAll('a').some(node => /implicit/.test(node.href)), false);
    assert.equal(env.nodes['feed-events'].querySelectorAll('a').some(node => node.href === '/evidence/' + 'b'.repeat(64)), true);
    for (const query of ['1 NEAR', '7 min', '452158 ms', '1000000000000000000000000 yoctoNEAR']) {
      env.nodes['feed-query'].value = query; env.nodes['feed-query'].emit('input');
      assert.match(text(env.nodes['feed-events']), /1 of 2 events/, 'Search includes readable and raw detail');
      assert.match(text(env.nodes['feed-events']), /full access key/);
    }
    env.nodes['feed-clear'].emit('click');
    env.nodes['feed-source'].value = 'FastNEAR Transfers'; env.nodes['feed-source'].emit('change');
    assert.match(text(env.nodes['feed-events']), /counterparty.near/);
    assert.doesNotMatch(text(env.nodes['feed-events']), /full_access_key_then_outflow/);
    env.nodes['feed-since'].value = '2026-01-01T00:00'; env.nodes['feed-since'].emit('input');
    assert.match(text(env.nodes['feed-events']), /No events match/);
    env.nodes['feed-clear'].emit('click');
    assert.match(text(env.nodes['feed-events']), /1 NEAR/);
    assert.ok(env.nodes['feed-events'].querySelectorAll('td').some(node=>node.getAttribute('data-export-value')==='1000000000000000000000000 yoctoNEAR'));
    const whitespace = environment(script);
    await whitespace.reply(0, response([signal('  recorded', {detail: '  <raw & "detail">\n2500 ms  '})], [null]));
    const signalCell = whitespace.nodes['feed-events'].querySelectorAll('td').find(node => node.getAttribute('data-label') === 'Signal');
    assert.equal(signalCell.getAttribute('data-export-value'), 'recorded <raw & "detail"> 2500 ms');
    const workspace = fs.readFileSync('intel/ui/workspace.js', 'utf8');
    const extracted = workspace.slice(workspace.indexOf('  const csv ='), workspace.indexOf('  function viewUrl()'));
    let exported;
    const context = {document: {getElementById: () => ({querySelectorAll: () => [{hidden: false, querySelectorAll: () => [signalCell]}]})},
      download(name, value) { exported = value; }, message() {}};
    vm.runInNewContext(extracted + ';globalThis.run=tableExport;', context); context.run('feed-signals');
    assert.equal(exported, '"recorded <raw & ""detail""> 2500 ms"\r\n');
  } else if (scenario === 'feed-acceptance') {
    await env.reply(0, response([signal('A')], ['a'.repeat(64)]));
    assert.equal(env.notifications.length, 1);
    env.run(30000); await env.reply(1, response([signal('B')], ['b'.repeat(64)]));
    assert.equal(env.notifications.length, 1, 'Queued rows must not be dispatched as displayed data');
    assert.match(text(env.nodes['feed-events']), /A detail/);
    env.nodes['feed-apply'].emit('click');
    assert.equal(env.notifications.length, 2);
    assert.equal(env.notifications[1].type, 'siyab-feed');
    assert.equal(env.notifications[1].detail.event_ids[0], 'b'.repeat(64));
    env.run(30000); env.nodes['feed-pause'].emit('click'); await env.reply(2, response([signal('C')], ['c'.repeat(64)]));
    assert.equal(env.notifications.length, 2); assert.match(text(env.nodes['feed-events']), /B detail/);
  } else if (scenario === 'feed-invalid') {
    await env.reply(0, response([signal('A')], ['a'.repeat(64)]));
    env.run(30000); await env.reply(1, response([signal('B')], ['../../private']));
    assert.match(text(env.nodes['feed-health']), /unavailable/);
    assert.match(text(env.nodes['feed-events']), /A detail/);
    assert.equal(env.nodes['feed-retry'].hidden, false); assert.equal(env.notifications.length, 1);
  } else if (scenario === 'verify-explicit') {
    assert.equal(env.requests.length, 0, 'Verifier must not run an automatic live read');
    env.nodes.registry.setAttribute('placeholder', 'registry.example.testnet'); env.nodes.subject.setAttribute('placeholder', 'vendor.testnet'); env.nodes.hash.setAttribute('placeholder', 'a'.repeat(64));
    env.nodes['verify-example'].emit('click'); assert.equal(env.requests.length, 0);
    env.nodes.read.emit('click'); assert.equal(env.requests.length, 1);
    const body = JSON.parse(env.requests[0].options.body);
    assert.equal(body.params.method_name, 'latest'); assert.equal(body.params.finality, 'final');
    await env.reply(0, rpc(record('a'.repeat(64))));
    assert.match(text(env.nodes.out), /Evidence hashes match/);
    assert.match(text(env.nodes.out), /does not hash an evidence file or verify a signature/);
    assert.match(text(env.nodes.out), /1970-01-01/);
    assert.equal(env.nodes.read.disabled, false);
    assert.equal(env.nodes.out.querySelectorAll('details').length, 1);
  } else if (scenario === 'verify-stale') {
    env.nodes.read.emit('click'); env.nodes.subject.value = 'second.testnet'; env.nodes.subject.emit('input'); env.nodes.read.emit('click');
    await env.reply(1, rpc(record('b'.repeat(64))));
    assert.match(text(env.nodes.out), /Evidence hashes differ/);
    await env.reply(0, rpc(record('a'.repeat(64))));
    assert.match(text(env.nodes.out), /Evidence hashes differ/); assert.match(text(env.nodes.out), /second.testnet/);
  } else if (scenario === 'verify-timeout' || scenario === 'verify-body-timeout') {
    env.nodes.read.emit('click');
    if (scenario === 'verify-body-timeout') {
      const request = env.requests[0]; request.resolve({ok: true, json: () => new Promise((resolve, reject) => request.options.signal.addEventListener('abort', () => reject(new Error('aborted body'))))}); await flush();
    }
    env.run(12000); await flush();
    assert.match(text(env.nodes.out), /exceeded 12 seconds/); assert.equal(env.nodes.read.disabled, false);
    assert.equal(env.nodes.subject.value, 'vendor.testnet');
    env.nodes.read.emit('click'); await env.reply(1, rpc(null));
    assert.match(text(env.nodes.out), /No current record/); assert.doesNotMatch(text(env.nodes.out), /Evidence hashes match/);
  } else if (scenario === 'verify-invalid') {
    env.nodes.registry.value = 'bad/account'; env.nodes.read.emit('click'); assert.equal(env.requests.length, 0);
    env.nodes.registry.value = 'registry.example.testnet'; env.nodes.read.emit('click');
    await env.reply(0, {result: {result: [256]}});
    assert.match(text(env.nodes.out), /Testnet read unavailable/); assert.doesNotMatch(text(env.nodes.out), /Evidence hashes match/);
  } else throw new Error('Unknown scenario ' + scenario);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
