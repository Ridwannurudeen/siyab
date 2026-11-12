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

async function recordedAmounts() {
  const section = new Element(), body = new Element(), counterBody = new Element(), table = new Element(), counterparties = new Element();
  const controls = Object.fromEntries(["[data-activity-search]", "[data-activity-direction]", "[data-activity-status]", "[data-activity-pages]", "[data-activity-retry]", ".profile-table-tools", "[data-counterparties-scope]"].map(key => [key, new Element()]));
  controls["[data-activity-search]"].value = ""; controls["[data-activity-direction]"].value = "";
  const config = new Element(); config.textContent = JSON.stringify({subject: "alice.near", total_rows: 3, sources: [{name: "recorded", url: "/graph-data/" + "a".repeat(64) + ".json"}]});
  section.querySelector = selector => selector === "[data-profile-sources]" ? config : selector === "#profile-transfers" ? table : selector === '[data-profile-table="profile-counterparties"]' ? counterparties : controls[selector];
  table.querySelector = () => body; table.querySelectorAll = () => ["Time", "Direction", "Counterparty", "Amount", "USD", "Evidence"].map(text => Object.assign(new Element(), {textContent: text}));
  counterparties.querySelector = selector => selector === "tbody" ? counterBody : new Element(); counterparties.dispatchEvent = () => {};
  const records = [
    {amount: "100000000000000000000000", amount_decimals: 24, human_amount: "0.09999999999999999"},
    {amount: "1", amount_decimals: 24, human_amount: "1e-24"},
    {amount: "123", amount_decimals: null, human_amount: "1.23"},
  ].map(row => ({from: "alice.near", to: "bob.near", asset_id: "nep141:token.near", block_timestamp: "1776237164627685607", block: 1, transaction_hash: "tx", receipt_id: "receipt", usd_amount: "0.09999999999999999", ...row}));
  const window = new Element();
  vm.runInNewContext(fs.readFileSync("intel/ui/number-format.js", "utf8"), {window});
  vm.runInNewContext(fs.readFileSync("intel/ui/profiles.js", "utf8"), {
    window, URLSearchParams, AbortController, Event,
    document: {querySelectorAll: selector => selector === "[data-profile-activity]" ? [section] : [], createElement: () => new Element(), createTextNode: value => Object.assign(new Element(), {textContent: value})},
    fetch: async () => ({ok: true, json: async () => ({kind: "flow", records, source: {fetched_at: "2026-11-01T00:00:00Z"}})}), setTimeout, clearTimeout,
  });
  await controls["[data-activity-search]"].emit("input");
  assert.equal(body.children[0].children[4].children[1].textContent, "0.1");
  assert.equal(body.children[0].children[3].children[1].textContent, "0.1");
  assert.equal(body.children[1].children[3].children[1].textContent, "<0.0001");
  assert.equal(body.children[2].children[3].children[1].textContent, "123 base units");
  assert.match(text(body.children[0].children[5]), /100000000000000000000000/, "Transfer details retain exact base units");
}
recordedAmounts().catch(error => { console.error(error); process.exitCode = 1; });
