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
  vm.runInNewContext(fs.readFileSync('intel/ui/number-format.js', 'utf8'), {window});
  let serial = 0;
  const fetch = (url, options = {}) => new Promise((resolve, reject) => {
    requests.push({url, options, resolve, reject});
    if (options.signal) options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  vm.runInNewContext(source, {document, window, fetch, AbortController, URLSearchParams,
    location: {search}, localStorage: {getItem() { return null; }, setItem() {}},
    matchMedia: () => ({matches: false}),
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
  const script = scenario === 'menu' ? 'intel/ui/shell.js' : scenario.startsWith('url-') ? `intel/ui/${scenario.slice(4)}.js` : 'intel/ui/feed.js';
  const env = environment(script, scenario === 'url-shell' ? '?q=victim' : '');
  if (scenario === 'menu') {
    const menu = env.document.getElementById('mobile-navigation');
    const summary = Object.assign(new Element(), {tag: 'summary'});
    summary.focus = () => { env.document.activeElement = summary; };
    menu.append(summary); menu.open = true;
    menu.emit('click', {target: {closest: () => null}});
    assert.equal(menu.open, true, 'Native summary click must retain its own toggle behavior');
    menu.emit('click', {target: {closest: () => ({href: '/explore.html#tools'})}});
    assert.equal(menu.open, false, 'Same-page navigation must close the mobile sheet');
    menu.open = true; env.document.emit('keydown', {key: 'Escape'});
    assert.equal(menu.open, false); assert.equal(env.document.activeElement, summary);
    menu.open = true; env.nodes['global-search'].open = true;
    env.document.emit('keydown', {key: 'Escape'});
    assert.equal(menu.open, true, 'Search dialog Escape must not also close navigation');
  } else if (scenario === 'rate-limit') {
    await env.reply(0, payload('retained'));
    env.run(30000);
    env.requests[1].resolve({ok: false, status: 429, headers: {get: () => 'application/json'}});
    await flush();
    assert.match(text(env.nodes['feed-health']), /read was rate limited \(HTTP 429\)/);
    assert.match(text(env.nodes['feed-events']), /retained/);
    assert.equal(env.nodes['feed-retry'].hidden, false);
    env.run(30000); assert.equal(env.requests.length, 3);
  } else if (scenario === 'stall' || scenario === 'body-stall') {
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
    const links = target.querySelectorAll('a');
    if (scenario === 'url-shell') {
      assert.equal(links.length, 1, 'A valid mainnet query remains investigable when the recorded index is rejected');
      assert.equal(links[0].href, '/investigate.html?account=victim');
      assert.equal(links[0].children[0].textContent, 'Investigate victim');
    } else assert.equal(links.length, 0, 'Malformed path must not become a link');
    assert.equal(links.some(link => link.href === '/evil.example/path'), false, 'Malformed recorded paths must never become links');
    const valid = environment(script, scenario === 'url-shell' ? '?q=victim' : '');
    await valid.reply(0, {accounts: [{subject: 'victim.near', labels: [], label_sources: [], url: 'accounts/victim.near.html'}], entities: []});
    if (scenario === 'url-index') { valid.nodes['account-input'].value = 'victim.near'; valid.nodes['account-input'].emit('input'); }
    const validTarget = valid.nodes[scenario === 'url-index' ? 'account-labels' : 'global-search-results'];
    const validLinks = validTarget.querySelectorAll('a');
    if (scenario === 'url-shell') {
      assert.equal(validLinks.length, 2);
      assert.equal(validLinks[0].href, '/investigate.html?account=victim');
      assert.equal(validLinks[1].href, '/accounts/victim.near.html', 'Recorded matches remain after the live investigation result');
    } else assert.equal(validLinks[0].href, '/accounts/victim.near.html');
  } else { throw new Error(`Unknown scenario ${scenario}`); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
