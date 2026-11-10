const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor() { this.children = []; this.listeners = {}; this.textContent = ""; this.dataset = {}; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.textContent = ""; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  emit(event) { return this.listeners[event](); }
}
const text = element => element.textContent + element.children.map(text).join(" ");
const flush = () => new Promise(resolve => setImmediate(resolve));
function environment() {
  const output = new Element(), payment = new Element(), keys = new Element(), section = new Element();
  section.dataset.profileLive = "alice.near";
  section.querySelector = selector => selector === "[data-profile-live-result]" ? output : selector === "[data-profile-payment]" ? payment : keys;
  const requests = [], timers = new Map(); let serial = 0;
  const window = new Element();
  vm.runInNewContext(fs.readFileSync("intel/ui/profiles.js", "utf8"), {
    document: {querySelectorAll: selector => selector === "[data-profile-live]" ? [section] : [], createElement: () => new Element()},
    window, URLSearchParams, AbortController,
    fetch: (url, options) => new Promise((resolve, reject) => {
      requests.push({url, options, resolve, reject});
      options.signal.addEventListener("abort", () => reject(new Error("aborted")));
    }),
    setTimeout: callback => { const id = ++serial; timers.set(id, callback); return id; },
    clearTimeout: id => timers.delete(id),
  });
  return {output, payment, keys, requests, timers, window};
}
const allow = () => ({outcome: "ALLOW", action: "sign", verdict: null, signing: "never", evidence_hash: "a".repeat(64), freshness: {block_height: 1, timestamp_ms: 1}, reasons: [], checks: [{name: "account", status: "pass", detail: "Recorded response"}]});
async function answer(request, value) { request.resolve({ok: true, headers: {get: () => "application/json"}, json: async () => value}); await flush(); }
async function main() {
  const env = environment();
  assert.equal(env.requests.length, 0, "Profiles do not issue live checks on page load");
  const payment = env.payment.emit("click");
  const params = new URLSearchParams(env.requests[0].url.split("?")[1]);
  assert.equal(params.get("amount"), "1000000000000000000000000", "The labelled 1 NEAR read must use exact yoctoNEAR");
  await answer(env.requests[0], allow()); await payment;
  assert.match(text(env.output), /ALLOW/);
  assert.match(text(env.output), /signing never/);
  const invalid = env.payment.emit("click");
  const incomplete = allow(); incomplete.checks[0].status = "unknown";
  await answer(env.requests[1], incomplete); await invalid;
  assert.doesNotMatch(text(env.output), /ALLOW/);
  assert.match(text(env.output), /No favorable payment result/);
  const missingKeys = env.keys.emit("click");
  await answer(env.requests[2], {access_keys: null}); await missingKeys;
  assert.match(text(env.output), /unknown, not a clean result/);
  const old = env.payment.emit("click");
  const current = env.keys.emit("click");
  await answer(env.requests[4], {access_keys: 0, full_access_keys: [], empty_method_names: [], dead_keys: [], unlimited_allowance: [], findings: [], read_at_block: 2});
  await current; await old;
  assert.doesNotMatch(text(env.output), /ALLOW/);
  assert.match(text(env.output), /0 access keys read/);
  const timed = env.payment.emit("click");
  [...env.timers.values()][0](); await timed;
  assert.match(text(env.output), /timed out/);
  assert.equal(env.output.attributes["aria-busy"], "false");
  const hidden = env.payment.emit("click"); env.window.emit("pagehide"); await hidden;
  assert.equal(env.requests[6].options.signal.aborted, true);
  console.log("Profile live checks: on-demand reads, exact units, unknown refusal, request ordering, timeout and page lifecycle passed.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
