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
  if (scenario === 'feed-filter') {
    const a = signal('full_access_key_then_outflow', {destination: 'implicit', amount_yocto: '1000000000000000000000000'});
    const b = signal('labelled_account_large_transfer', {other_account_id: 'counterparty.near', usd_amount: '1000.0001', asset_id: 'native:near', source: {provider: 'FastNEAR Transfers'}});
    await env.reply(0, response([a, b], ['a'.repeat(64), 'b'.repeat(64)]));
    assert.match(text(env.nodes['feed-events']), /1970-01-01/);
    assert.match(text(env.nodes['feed-events']), /1000\.0001 USD/);
    assert.equal(env.nodes['feed-events'].querySelectorAll('a').some(node => /implicit/.test(node.href)), false);
    assert.equal(env.nodes['feed-events'].querySelectorAll('a').some(node => node.href === '/evidence/' + 'b'.repeat(64)), true);
    env.nodes['feed-source'].value = 'FastNEAR Transfers'; env.nodes['feed-source'].emit('change');
    assert.match(text(env.nodes['feed-events']), /counterparty.near/);
    assert.doesNotMatch(text(env.nodes['feed-events']), /full_access_key_then_outflow/);
    env.nodes['feed-since'].value = '2026-01-01T00:00'; env.nodes['feed-since'].emit('input');
    assert.match(text(env.nodes['feed-events']), /No events match/);
    env.nodes['feed-clear'].emit('click');
    assert.match(text(env.nodes['feed-events']), /1000000000000000000000000 yoctoNEAR/);
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
