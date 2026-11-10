const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelector() { return null; }
  focus() { this.ownerDocument.activeElement = this; }
}
const source = fs.readFileSync('intel/ui/graphs.js', 'utf8');
const path = '/graph-data/' + 'a'.repeat(64) + '.json';
const flush = () => new Promise(resolve => setImmediate(resolve));
const text = node => node.textContent + node.children.map(text).join(' ');

function environment(url = path, protocol = 'https:') {
  const container = new Element();
  container.setAttribute('data-graph-src', url);
  const document = new Element();
  container.ownerDocument = document;
  document.readyState = 'loading';
  document.createElement = tag => Object.assign(new Element(tag), {ownerDocument: document});
  document.querySelectorAll = () => [container];
  const location = new URL(protocol === 'file:' ? 'file:///site/index.html' : 'https://siyab.xyz/index.html');
  const window = {location};
  vm.runInNewContext(fs.readFileSync('intel/ui/number-format.js','utf8'),{window});
  const requests = [], timers = new Map();
  let timerId = 0;
  const fetch = (url, options) => new Promise((resolve, reject) => {
    requests.push({url, options, resolve, reject});
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  vm.runInNewContext(source, {
    document, window, fetch, URL, AbortController,
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timers.delete(id); }, console
  });
  return {container, document, window, requests, timers,
    start: () => window.SiyabGraph.mount(container),
    reply(data = {}, status = 200, mime = 'application/json', json) {
      requests.at(-1).resolve({ok: status >= 200 && status < 300, status,
        headers: {get: () => mime}, json: json || (async () => data)});
    },
    timeout() {
      const timer = [...timers.values()].find(timer => timer.delay === 15000);
      assert.ok(timer, 'A bounded loading deadline must exist'); timer.callback();
    }
  };
}
function unavailable(env, expected) {
  assert.match(text(env.container), /Recorded investigation unavailable/);
  assert.match(text(env.container), expected);
  assert.equal(env.container.getAttribute('aria-busy'), 'false');
  assert.equal(env.timers.size, 0, 'Completed failures must release their deadline');
  assert.ok(env.container.children[0].children.find(node => node.tag === 'button' && node.textContent === 'Retry loading graph'));
}

async function main() {
  for (const [status, mime, json, message] of [
    [503, 'application/json', null, /HTTP 503/],
    [200, 'text/html', null, /not JSON/],
    [200, 'application/json', async () => { throw new SyntaxError('Invalid JSON'); }, /Invalid JSON/],
    [200, 'application/json', null, /incomplete/]
  ]) {
    const env = environment(); const pending = env.start();
    assert.equal(env.container.getAttribute('aria-busy'), 'true');
    env.reply({}, status, mime, json); await pending; unavailable(env, message);
  }
  for (const url of ['https://elsewhere.example' + path, '/wrong.json', path + '?x=1', path + '#part']) {
    const env = environment(url); await env.start();
    unavailable(env, /address is invalid/); assert.equal(env.requests.length, 0);
  }
  const local = environment(path, 'file:'); await local.start();
  unavailable(local, /Serve this site over HTTP/); assert.equal(local.requests.length, 0);

  const timeout = environment(); const timeoutPending = timeout.start();
  timeout.timeout(); await timeoutPending; unavailable(timeout, /too long/);
  assert.equal(timeout.requests[0].options.signal.aborted, true);

  const body = environment(); const bodyPending = body.start();
  body.reply({}, 200, 'application/json', () => new Promise((resolve, reject) => {
    body.requests[0].options.signal.addEventListener('abort', () => reject(new Error('body aborted')));
  }));
  await flush(); body.timeout(); await bodyPending; unavailable(body, /too long/);

  const concurrent = environment(); const first = concurrent.start();
  await concurrent.start(); concurrent.document.listeners.DOMContentLoaded();
  assert.equal(concurrent.requests.length, 1, 'Repeated initialization must not duplicate an in-flight request');
  concurrent.reply({}, 503); await first; unavailable(concurrent, /HTTP 503/);
  assert.equal(concurrent.document.activeElement, undefined, 'Automatic loading must not steal focus');
  const retry = concurrent.container.children[0].children.find(node => node.tag === 'button');
  retry.focus();
  const retried = retry.listeners.click();
  assert.equal(concurrent.requests.length, 2, 'Retry must issue a fresh request after failure');
  assert.equal(concurrent.container.getAttribute('aria-busy'), 'true');
  assert.equal(concurrent.document.activeElement, concurrent.container.children[0].children[0],
    'Retry must move focus to the loading heading before discarding the focused button');
  concurrent.reply({}, 502); await retried; unavailable(concurrent, /HTTP 502/);
  assert.equal(concurrent.document.activeElement,
    concurrent.container.children[0].children.find(node => node.tag === 'button'),
    'A failed retry must focus its replacement retry button');
  assert.equal(concurrent.requests[1].options.credentials, 'same-origin');

  const replacement = concurrent.document.activeElement;
  const movedPending = replacement.listeners.click();
  const elsewhere = concurrent.document.createElement('button'); elsewhere.focus();
  concurrent.reply({}, 503); await movedPending;
  assert.equal(concurrent.document.activeElement, elsewhere, 'Completing a request must not steal focus after the user moves elsewhere');

  const automatic = environment();
  const second = automatic.document.createElement('section'); second.setAttribute('data-graph-src', path);
  automatic.document.querySelectorAll = () => [automatic.container, second];
  automatic.document.listeners.DOMContentLoaded();
  assert.equal(automatic.requests.length, 2);
  assert.equal(automatic.document.activeElement, undefined, 'Collection indexes must not become the restoreFocus argument');
  automatic.requests[0].reject(new Error('Unavailable')); automatic.reply({}, 503); await flush();

  // A minimal payload can pass structural validation but fail while constructing controls.
  // Recovery must replace the partial mount with the visible retry panel.
  const partial = environment(); const partialPending = partial.start();
  partial.reply({kind: 'flow', root: 'root.near', total_nodes: 1, total_records: 0,
    nodes: [{id: 'root.near', label: 'root.near'}], edges: [], records: [], source: {}});
  await partialPending; unavailable(partial, /unavailable/);
  const partialRetry = partial.container.children[0].children.find(node => node.tag === 'button');
  const partialRetried = partialRetry.listeners.click();
  assert.equal(partial.requests.length, 2, 'A failed partial mount must remain retryable');
  partial.reply({}, 503); await partialRetried; unavailable(partial, /HTTP 503/);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
