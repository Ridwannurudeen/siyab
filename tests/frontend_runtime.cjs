const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() { this.children = []; this.listeners = {}; this.hidden = false; this.value = ''; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.textContent = ''; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  emit(name, event = {}) { for (const callback of this.listeners[name] || []) callback({currentTarget: this, preventDefault() {}, ...event}); }
  setAttribute(name, value) { this[name] = value; }
  focus() {}
  showModal() { this.open = true; }
  querySelectorAll(tag) { return this.children.flatMap(child => [...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)]); }
  querySelector(tag) { return this.querySelectorAll(tag)[0] || null; }
}
const flush = () => new Promise(resolve => setImmediate(resolve));
function environment(filename, search = '') {
  const source = fs.readFileSync(filename, 'utf8');
  const nodes = {};
  for (const match of source.matchAll(/getElementById\("([^"]+)"\)/g)) nodes[match[1]] = new Element();
  const document = new Element(); document.hidden = false; document.documentElement = {dataset: {}};
  document.getElementById = id => nodes[id] ||= new Element(); document.querySelectorAll = () => [];
  document.createElement = tag => Object.assign(new Element(), {tag});
  const window = new Element(), timers = new Map(), requests = [];
  let serial = 0;
  const fetch = (url, options = {}) => new Promise((resolve, reject) => {
    requests.push({url, options, resolve, reject});
    if (options.signal) options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  vm.runInNewContext(source, {document, window, fetch, AbortController, URLSearchParams,
    location: {search}, localStorage: {getItem() { return null; }, setItem() {}},
    setTimeout(callback, delay) { const id = ++serial; timers.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timers.delete(id); }, console});
  return {nodes, document, window, timers, requests,
    run(delay) { const entry = [...timers].find(([, timer]) => timer.delay === delay); assert.ok(entry, `No ${delay}ms timer`); timers.delete(entry[0]); entry[1].callback(); },
    async reply(index, data) { requests[index].resolve({ok: true, status: 200, headers: {get: () => 'application/json'}, json: async () => data}); await flush(); }};
}
const event = label => ({signal: label, detail: label, observed: {block_height: 1}, tx_hash: label});
const payload = label => ({stats: null, follower: null, events: [event(label)]});
const text = node => node.textContent + node.children.map(text).join(' ');
async function main() {
  const scenario = process.argv[2];
  const script = scenario.startsWith('url-') ? `intel/ui/${scenario.slice(4)}.js` : 'intel/ui/feed.js';
  const env = environment(script, scenario === 'url-shell' ? '?q=victim' : '');
  if (scenario === 'stall' || scenario === 'body-stall') {
    if (scenario === 'body-stall') {
      const request = env.requests[0];
      request.resolve({ok: true, headers: {get: () => 'application/json'}, json: () => new Promise((resolve, reject) => request.options.signal.addEventListener('abort', () => reject(new Error('body aborted'))))});
      await flush();
    }
    env.run(12000); await flush();
    assert.match(text(env.nodes['feed-health']), /unavailable/);
    env.run(30000); assert.equal(env.requests.length, 2);
  } else if (scenario === 'queue-revert') {
    await env.reply(0, payload('A')); env.run(30000); await env.reply(1, payload('B'));
    assert.equal(env.nodes['feed-apply'].hidden, false);
    env.run(30000); await env.reply(2, payload('A'));
    assert.equal(env.nodes['feed-apply'].hidden, true);
    env.nodes['feed-apply'].emit('click');
    assert.doesNotMatch(text(env.nodes['feed-events']), /\bB\b/);
  } else if (scenario === 'bfcache') {
    await env.reply(0, payload('A')); env.window.emit('pagehide');
    env.window.emit('pageshow', {persisted: true}); assert.equal(env.requests.length, 2);
    env.nodes['feed-pause'].emit('click'); env.window.emit('pagehide');
    env.window.emit('pageshow', {persisted: true}); assert.equal(env.requests.length, 2);
  } else if (scenario.startsWith('url-')) {
    await env.reply(0, {accounts: [{subject: 'victim.near', labels: [], label_sources: [], url: '/evil.example/path'}], entities: []});
    if (scenario === 'url-index') { env.nodes['account-input'].value = 'victim.near'; env.nodes['account-input'].emit('input'); }
    const target = env.nodes[scenario === 'url-index' ? 'account-labels' : 'global-search-results'];
    assert.equal(target.querySelectorAll('a').length, 0, 'Malformed path must not become a link');
    const valid = environment(script, scenario === 'url-shell' ? '?q=victim' : '');
    await valid.reply(0, {accounts: [{subject: 'victim.near', labels: [], label_sources: [], url: 'accounts/victim.near.html'}], entities: []});
    if (scenario === 'url-index') { valid.nodes['account-input'].value = 'victim.near'; valid.nodes['account-input'].emit('input'); }
    const validTarget = valid.nodes[scenario === 'url-index' ? 'account-labels' : 'global-search-results'];
    assert.equal(validTarget.querySelector('a').href, '/accounts/victim.near.html');
  } else { throw new Error(`Unknown scenario ${scenario}`); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
