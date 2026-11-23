const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor() { this.children = []; this.listeners = {}; this.listenerOptions = {}; this.value = ""; this.textContent = ""; this.values = {}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.textContent = ""; }
  addEventListener(event, callback, options) { this.listeners[event] = callback; this.listenerOptions[event] = options; }
  emit(event, target = this) { return this.listeners[event]({currentTarget: this, target, preventDefault() {}}); }
  setAttribute() {}
  click() { if (this.listeners.click) this.emit("click"); }
  remove() {}
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const text = element => element.textContent + element.children.map(text).join(" ");
function setup(deferVerdict = false) {
  const nodes = {};
  const paths = [];
  const replies = [];
  const answers = [];
  const downloads = [];
  const links = [];
  const document = {
    getElementById: id => nodes[id] ||= Object.assign(new Element(), {value: id === "tool-token" ? "near" : ""}),
    querySelectorAll: () => [],
    createElement: tag => { const node = new Element(); if (tag === "a") links.push(node); return node; },
    body: new Element(),
  };
  vm.runInNewContext(fs.readFileSync("intel/ui/index.js", "utf8"), {
    document, window: {}, URLSearchParams, Blob, setTimeout,
    URL: class extends URL { static createObjectURL(blob) { downloads.push(blob); return "blob:receipt"; } static revokeObjectURL() {} },
    FormData: class { constructor(form) { this.values = form.values; } get(key) { return this.values[key]; } },
    fetch: async path => {
      paths.push(path);
      const data = path === "/search.json" ? {accounts: []} : path.startsWith("/live/reports") ? {reports: []} : path.startsWith("/live/scan") ? {
        access_keys: 0, full_access_keys: [], empty_method_names: [], dead_keys: [], unlimited_allowance: [], findings: [], read_at_block: 1,
      } : {outcome: "ALLOW", action: "sign", evidence_hash: "a".repeat(64), freshness: {block_height: 1}, verdict: null,
        signing: "never", evidence: "{\"note\":\"Recorded reply \u00e9\"}", latency_ms: 1, reasons: [], checks: [{name: "test", status: "pass", detail: "test"}]};
      if (path.startsWith("/live/verdict?")) answers.push(data);
      const response = {ok: true, status: 200, headers: {get: () => "application/json"}, json: async () => data};
      if (deferVerdict && (path.startsWith("/live/verdict?") || path.startsWith("/live/intents?") || (deferVerdict === "checks" && path.startsWith("/live/scan/")))) return new Promise(resolve => replies.push(() => resolve(response)));
      return response;
    },
  });
  return {nodes, paths, replies, answers, downloads, links};
}
async function submit(kind, amount, token = "near") {
  const env = setup();
  await flush();
  env.paths.length = 0;
  if (kind === "account") {
    env.nodes["account-input"].value = "alice.near";
    env.nodes["account-amount"].value = amount;
    env.nodes["account-form"].emit("submit");
  } else {
    env.nodes["tool-verdict-form"].values = {receiver: "alice.near", amount, token};
    await env.nodes["tool-verdict-form"].emit("submit");
  }
  await flush();
  return {...env, verdict: env.paths.find(path => path.startsWith("/live/verdict?"))};
}
async function main() {
  const scenario = process.argv[2];
  if (scenario === "native") {
    const cases = [["1", "1000000000000000000000000"], ["0.1", "100000000000000000000000"],
      ["0.000000000000000000000001", "1"], ["1.000000000000000000000001", "1000000000000000000000001"],
      [" 2.50 ", "2500000000000000000000000"],
      ["340282366920938.463463374607431768211455", "340282366920938463463374607431768211455"]];
    for (const kind of ["account", "tool"]) for (const [amount, expected] of cases) {
      const env = await submit(kind, amount);
      assert.ok(env.verdict, `${kind} must send a request for ${amount}`);
      const params = new URLSearchParams(env.verdict.split("?")[1]);
      assert.equal(params.get("amount"), expected, `${kind} ${amount} NEAR must preserve the exact base-unit value`);
      assert.equal(params.get("token"), "near");
      if (kind === "account") assert.match(text(env.nodes["account-verdict"]), /Amount checked:.*NEAR/);
      const panel = env.nodes[kind === "account" ? "account-verdict" : "tool-verdict-result"];
      const download = panel.children.find(node => node.textContent === "Download evidence");
      assert.ok(download, "A finished payment check must offer an evidence download");
      const expectedReceipt = JSON.stringify(env.answers[0]);
      const requestCount = env.paths.length;
      download.click();
      assert.equal(env.downloads.length, 1);
      assert.deepEqual(Buffer.from(await env.downloads[0].arrayBuffer()), Buffer.from(expectedReceipt, "utf8"), "The download must preserve the displayed answer byte for byte");
      assert.equal(env.downloads[0].type, "application/json");
      assert.equal(env.links[0].download, `siyab-check-${env.answers[0].evidence_hash.slice(0, 12)}.json`);
      assert.equal(env.paths.length, requestCount, "Downloading a receipt must not request another answer");
      assert.equal(JSON.stringify(env.answers[0]), expectedReceipt, "Downloading must leave the displayed answer unchanged");
    }
  } else if (scenario === "invalid") {
    for (const kind of ["account", "tool"]) for (const amount of ["", "0", "0.0", "-1", "+1", "1e3", "01", "0.0000000000000000000000001", "1.1234567890123456789012345", "340282366920938.463463374607431768211456", "340282366920939", "1,000", "Infinity", "NaN"]) {
      const env = await submit(kind, amount);
      assert.equal(env.verdict, undefined, `${kind} invalid amount ${amount} must not be judged`);
      const panel = env.nodes[kind === "account" ? "account-verdict" : "tool-verdict-result"];
      assert.match(text(panel), /amount|NEAR|base units/i, "Invalid amounts need actionable feedback");
      assert.doesNotMatch(text(panel), /Reading the live/);
    }
  } else if (scenario === "intent") {
    for (const [amount, expected] of [["1", "1000000000000000000000000"], ["0.1", "100000000000000000000000"],
      ["0.000000000000000000000001", "1"], ["1.000000000000000000000001", "1000000000000000000000001"],
      [" 2.50 ", "2500000000000000000000000"], ["1000000", "1000000000000000000000000000000"],
      ["340282366920938.463463374607431768211455", "340282366920938463463374607431768211455"]]) {
      const env = setup(); await flush();
      env.nodes["tool-intent-form"].values = {destination_asset: "nep141:usdt.tether-token.near", recipient: "alice.near", amount};
      await env.nodes["tool-intent-form"].emit("submit");
      const path = env.paths.find(path => path.startsWith("/live/intents?"));
      assert.ok(path, `Swap destination check must send a request for ${amount} NEAR`);
      const params = new URLSearchParams(path.split("?")[1]);
      assert.equal(params.get("amount"), expected, "Swap amounts use exact yocto base units");
      assert.equal(params.get("destination_asset"), "nep141:usdt.tether-token.near");
      assert.equal(params.get("recipient"), "alice.near");
    }
    for (const amount of ["", "0", "0.0", "-1", "+1", "1e3", "01", "0.0000000000000000000000001", "1.1234567890123456789012345", "340282366920938.463463374607431768211456", "340282366920939", "1,000", "Infinity", "NaN"]) {
      const env = setup(); await flush();
      env.nodes["tool-intent-form"].values = {destination_asset: "nep141:wrap.near", recipient: "alice.near", amount};
      await env.nodes["tool-intent-form"].emit("submit");
      assert.equal(env.paths.some(path => path.startsWith("/live/intents?")), false, `Invalid swap amount ${amount} must not be sent`);
      assert.match(text(env.nodes["tool-intent-result"]), /amount|NEAR|base units/i);
      assert.doesNotMatch(text(env.nodes["tool-intent-result"]), /Reading the live/);
    }
  } else if (scenario === "tokens") {
    const labels = setup();
    assert.equal(labels.nodes["tool-amount-label"].textContent, "Amount in NEAR");
    labels.nodes["tool-token"].value = "wrap.near"; labels.nodes["tool-token"].emit("input");
    assert.equal(labels.nodes["tool-amount-label"].textContent, "Amount in base units");
    labels.nodes["tool-token"].value = "near"; labels.nodes["tool-token"].emit("input");
    assert.equal(labels.nodes["tool-amount-label"].textContent, "Amount in NEAR");
    for (const amount of ["1", "1000000", "340282366920938463463374607431768211455"]) {
      const env = await submit("tool", amount, "wrap.near");
      assert.equal(new URLSearchParams(env.verdict.split("?")[1]).get("amount"), amount, "FT input is already base units");
    }
    for (const amount of ["0", "01", "0.1", "1e6", "340282366920938463463374607431768211456"]) {
      assert.equal((await submit("tool", amount, "wrap.near")).verdict, undefined, "Invalid FT base units must not be sent");
    }
  } else if (scenario === "race") {
    for (const kind of ["account", "tool"]) {
      const env = setup(true); await flush();
      const form = env.nodes[kind === "account" ? "account-form" : "tool-verdict-form"];
      if (kind === "account") {
        env.nodes["account-input"].value = "alice.near"; env.nodes["account-amount"].value = "1";
      } else form.values = {receiver: "alice.near", amount: "1", token: "near"};
      form.emit("submit");
      assert.equal(env.replies.length, 1);
      if (kind === "account") env.nodes["account-amount"].value = "invalid";
      else form.values.amount = "invalid";
      form.emit("submit");
      assert.equal(env.replies.length, 1, "Invalid resubmission must not request another verdict");
      env.replies[0](); await flush();
      const panel = env.nodes[kind === "account" ? "account-verdict" : "tool-verdict-result"];
      assert.match(text(panel), /Enter a NEAR amount/);
      assert.doesNotMatch(text(panel), /ALLOW/, "An old favorable response cannot overwrite invalid-input feedback");
    }
  } else if (scenario === "required") {
    for (const kind of ["account", "tool", "intent"]) {
      const env = setup("checks"); await flush();
      const form = env.nodes[kind === "account" ? "account-form" : kind === "tool" ? "tool-verdict-form" : "tool-intent-form"];
      if (kind === "account") {
        env.nodes["account-input"].value = "alice.near"; env.nodes["account-amount"].value = "1";
      } else form.values = kind === "tool" ? {receiver: "alice.near", amount: "1", token: "near"}
        : {destination_asset: "nep141:wrap.near", recipient: "alice.near", amount: "1"};
      form.emit("submit");
      const count = env.replies.length;
      assert.equal(form.listenerOptions.invalid, true, "Native non-bubbling invalid events need a capture listener");
      form.emit("invalid", {validity: {valueMissing: true}});
      assert.equal(env.replies.length, count, "A browser-blocked invalid form must not request more checks");
      env.replies.forEach(resolve => resolve()); await flush();
      const panels = kind === "account" ? ["account-verdict", "account-scan"] : kind === "tool" ? ["tool-verdict-result"] : ["tool-intent-result"];
      for (const id of panels) {
        assert.match(text(env.nodes[id]), /required fields/);
        assert.doesNotMatch(text(env.nodes[id]), /ALLOW|access keys read/, "Pending results must not overwrite required-field feedback");
      }
    }
  } else throw new Error(`Unknown scenario ${scenario}`);
  console.log(`Payment units ${scenario}: passed`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
