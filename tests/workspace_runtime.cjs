const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const blank = () => ({version: 1, cases: [], watchlist: [], alerts: false, notifications: []});
const fixture = () => ({id: "case-one", title: "Account", notes: "", url: "/accounts/alice.near.html", created_at: "2026-11-10T00:00:00.000Z"});
const flush = () => new Promise(resolve => setImmediate(resolve));
const text = node => node.textContent + node.children.map(text).join(" ");
class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.listeners = {}; this.value = ""; this.textContent = ""; this.dataset = {}; this.attributes = {}; this.isConnected = true; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.textContent = ""; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  emit(name, properties = {}) { return this.listeners[name]?.({target: this, preventDefault() {}, ...properties}); }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return name in this.attributes; }
  closest() { return this; }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(child.tag === selector ? [child] : []), ...child.querySelectorAll(selector)]); }
  focus() { this.ownerDocument.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; this.emit("close"); }
  click() {}
  remove() { this.isConnected = false; }
}
function environment(options = {}) {
  let raw = options.raw ?? (options.state ? JSON.stringify(options.state) : null);
  const nodes = {}, requests = [], writes = [], downloads = [], timers = new Map();
  let timerId = 0, confirmations = 0;
  const document = new Element();
  const create = tag => Object.assign(new Element(tag), {ownerDocument: document});
  document.body = create("body"); document.activeElement = document.body;
  document.getElementById = id => nodes[id] ||= create("div");
  document.createElement = create;
  document.querySelectorAll = () => [];
  for (const id of ["case-error", "reset-workspace", "watch-account"]) document.getElementById(id);
  const watched = create("button"); watched.dataset.watchAccount = "alice.near";
  const heading = create("h1"); heading.textContent = "Account";
  const graph = create("section"); graph.setAttribute("data-graph-src", "/graph-data/" + "a".repeat(64) + ".json");
  const view = {kind: "flow", root: "alice.near", asset: "old", provenance: "", from: "", to: "", focus: "alice.near", limit: 8, mode: "graph", selected: {kind: "node", id: "alice.near"}, recordIds: ["r1"]};
  document.querySelector = selector => selector === "h1" ? heading : selector === "[data-watch-account]" ? watched : selector === "[data-siyab-graph]" && options.graph ? graph : null;
  const window = new Element();
  if (options.graph) window.SiyabGraph = {getView: () => ({...view, selected: {...view.selected}, recordIds: [...view.recordIds]})};
  const location = {href: "https://siyab.xyz/accounts/alice.near.html", origin: "https://siyab.xyz", pathname: "/accounts/alice.near.html"};
  const source = fs.readFileSync("intel/ui/workspace.js", "utf8");
  const marker = source.lastIndexOf("})();");
  assert.ok(marker > 0);
  const injected = source.slice(0, marker) + "globalThis.workspaceTest={localPath,valid,csv,consume,openSave,watch,startMonitor,state:()=>state};\n" + source.slice(marker);
  const context = {document, window, location, Blob, URL: class extends URL { static createObjectURL(blob) { downloads.push(blob); return "blob:test"; } static revokeObjectURL() {} },
    crypto: {randomUUID: () => "11111111-2222-3333-4444-555555555555"},
    confirm: () => { confirmations++; return options.confirm !== false; },
    localStorage: {getItem: () => raw, setItem: (_, value) => { writes.push(value); if (options.failWrite) throw new Error("quota"); raw = value; }, removeItem: () => { if (options.failReset) throw new Error("denied"); raw = null; }},
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, delay}); return id; }, clearTimeout: id => timers.delete(id),
    AbortController,
    fetch: (url, settings) => new Promise((resolve, reject) => { requests.push({url, settings, resolve, reject}); if (!options.ignoreAbort) settings.signal.addEventListener("abort", () => reject(new Error("aborted"))); }),
  };
  if (options.desktopFailure) {
    context.Notification = class { static permission = "granted"; constructor() { throw new Error("Desktop unavailable"); } };
    window.Notification = context.Notification;
  }
  vm.runInNewContext(injected, context);
  return {api: context.workspaceTest, nodes, requests, writes, timers, document, window, downloads, graph, view, raw: () => raw, confirmations: () => confirmations,
    timer(delay) { const found = [...timers].find(([, timer]) => timer.delay === delay); assert.ok(found, `Missing ${delay}ms timer`); timers.delete(found[0]); found[1].callback(); }};
}
async function reply(request, data) { request.resolve({ok: true, headers: {get: () => "application/json"}, json: async () => data}); await flush(); }
async function main() {
  const scenario = process.argv[2];
  if (scenario === "validation") {
    const env = environment();
    assert.equal(env.api.localPath("http://["), null, "Malformed imported URLs must return null");
    for (const url of ["javascript:alert(1)", "https://evil.example/accounts/alice.near.html", "//evil.example/index.html", "/accounts/../../secret.html"]) assert.equal(env.api.localPath(url), null);
    assert.equal(env.api.localPath("/accounts/alice.near.html?g_focus=alice.near#evidence"), "/accounts/alice.near.html?g_focus=alice.near#evidence");
    const data = blank(); data.cases = [fixture()]; assert.equal(env.api.valid(data), true);
    data.cases[0].created_at = "2026-02-30T00:00:00.000Z"; assert.equal(env.api.valid(data), false, "Invalid calendar dates are not accepted");
    const invalid = blank(); invalid.notifications = [{id: "a".repeat(64), account: "alice.near", signal: "signal", at: "x".repeat(10000)}]; assert.equal(env.api.valid(invalid), false);
    invalid.notifications[0].at = "Observed time unavailable"; assert.equal(env.api.valid(invalid), true);
    const bounded = blank(); bounded.cases = Array.from({length: 51}, (_, index) => ({...fixture(), id: `case-${index}`})); assert.equal(env.api.valid(bounded), false);
  } else if (scenario === "modal-reset") {
    const env = environment({state: {...blank(), cases: [fixture()]}});
    env.nodes["case-error"].textContent = "Previous error";
    env.api.openSave();
    assert.equal(env.nodes["case-error"].textContent, "");
    assert.match(env.nodes["case-title"].value, /alice\.near/, "Default investigation titles distinguish accounts");
    env.nodes["case-title"].value = ""; env.nodes["case-form"].emit("submit");
    assert.match(env.nodes["case-error"].textContent, /title/);
    const opener = env.document.createElement("button"); opener.focus(); env.api.openSave(fixture()); opener.isConnected = false;
    env.nodes["case-cancel"].emit("click");
    assert.equal(env.document.activeElement, env.nodes["case-search"], "A rebuilt case card returns dialog focus to the stable case search");
    env.nodes["reset-workspace"].emit("click");
    assert.equal(env.confirmations(), 1); assert.equal(env.raw(), null); assert.equal(env.api.state().cases.length, 0);
    const cancelled = environment({state: {...blank(), cases: [fixture()]}, confirm: false});
    cancelled.nodes["reset-workspace"].emit("click"); assert.equal(cancelled.api.state().cases.length, 1);
  } else if (scenario === "storage-xss-csv") {
    const broken = environment({raw: "{broken-json"}); broken.api.watch("alice.near"); assert.equal(broken.writes.length, 0); assert.equal(broken.raw(), "{broken-json");
    const quota = environment({failWrite: true}); quota.api.watch("alice.near"); quota.api.watch("bob.near"); assert.equal(quota.writes.length, 1); assert.equal(quota.api.state().watchlist.length, 2);
    const malicious = {...fixture(), title: "<img src=x onerror=alert(1)>", notes: "<script>alert(1)</script>"};
    const env = environment({state: {...blank(), cases: [malicious]}});
    assert.match(text(env.nodes["saved-cases"]), /<img src=x/); assert.equal(env.nodes["saved-cases"].querySelectorAll("img").length, 0); assert.equal(env.nodes["saved-cases"].querySelectorAll("script").length, 0);
    assert.equal(env.api.csv([["=SUM(A1)", "\t+cmd", "@test", "-1", 'a"b', "line\nbreak"]]), '"\'=SUM(A1)","\'\t+cmd","\'@test","\'-1","a""b","line\nbreak"\r\n');
    const failedReset = environment({raw: "{broken-json", failReset: true}); failedReset.nodes["reset-workspace"].emit("click"); assert.equal(failedReset.raw(), "{broken-json"); assert.equal(failedReset.writes.length, 0);
  } else if (scenario === "monitor") {
    const env = environment({state: {...blank(), alerts: true, watchlist: ["alice.near"]}});
    assert.equal(env.requests.length, 1); env.timer(12000); await flush(); assert.match(env.nodes["alert-state"].textContent, /unavailable/);
    env.timer(30000); await reply(env.requests[1], {events: [], event_ids: []});
    env.timer(30000); await reply(env.requests[2], {events: [{subject: "alice.near", signal: "key", observed: {timestamp_ms: 8640000000000001}}], event_ids: ["a".repeat(64)]});
    assert.equal(env.api.state().notifications.length, 1); assert.equal(env.api.state().notifications[0].at, "Observed time unavailable");
    env.timer(30000); env.window.emit("pagehide"); assert.equal(env.requests[3].settings.signal.aborted, true); await flush(); assert.equal([...env.timers.values()].some(timer => timer.delay === 30000), false);
    env.window.emit("pageshow", {persisted: true}); assert.equal(env.requests.length, 5);
    const stale = environment({state: {...blank(), alerts: true, watchlist: ["alice.near"]}, ignoreAbort: true});
    stale.api.startMonitor(); await reply(stale.requests[1], {events: [], event_ids: []});
    await reply(stale.requests[0], {events: [{subject: "alice.near", signal: "stale", observed: {timestamp_ms: 1}}], event_ids: ["b".repeat(64)]});
    assert.equal(stale.api.state().notifications.length, 0, "Superseded requests cannot change notification state");
    stale.window.emit("pagehide"); env.window.emit("pagehide");
    const stopped = environment({state: {...blank(), watchlist: ["alice.near"]}, desktopFailure: true});
    stopped.api.consume({follower: "stopped", events: [], event_ids: []}); assert.match(stopped.nodes["alert-state"].textContent, /Follower stopped/);
    stopped.api.consume({events: [{subject: "alice.near", signal: "   ", observed: {timestamp_ms: 1}}], event_ids: ["c".repeat(64)]}); assert.equal(stopped.api.state().notifications.length, 0);
    stopped.api.consume({events: [{subject: "alice.near", signal: "key", observed: {timestamp_ms: 1}}], event_ids: ["d".repeat(64)]}); assert.equal(stopped.api.state().notifications.length, 1); assert.match(stopped.nodes["alert-state"].textContent, /desktop delivery is unavailable/);
    const body = environment({state: {...blank(), alerts: true, watchlist: ["alice.near"]}});
    body.requests[0].resolve({ok: true, json: () => new Promise((resolve, reject) => body.requests[0].settings.signal.addEventListener("abort", () => reject(new Error("body aborted"))))});
    await flush(); body.timer(12000); await flush(); assert.match(body.nodes["alert-state"].textContent, /unavailable/); body.window.emit("pagehide");
  } else if (scenario === "export-watch") {
    const env = environment({graph: true});
    const button = new Element("button"); button.attributes["data-export-graph"] = "";
    button.closest = selector => selector === "[data-siyab-graph]" ? env.graph : button;
    const exporting = env.document.emit("click", {target: button});
    env.view.asset = "new";
    await reply(env.requests[0], {records: [{id: "r1", value: "old"}, {id: "r2", value: "new"}], source: {recorded: true}}); await exporting;
    const exported = JSON.parse(await env.downloads[0].text());
    assert.equal(exported.view.asset, "old"); assert.match(exported.source, /g_asset=old/); assert.equal(exported.records.length, 1);
    env.nodes["watch-account"].value = "alice.near"; env.nodes["watch-form"].emit("submit");
    env.nodes["watch-account"].value = "alice.near"; env.nodes["watch-form"].emit("submit");
    assert.equal(env.api.state().watchlist.length, 1, "Adding an already watched account is idempotent");
    env.nodes["watch-account"].value = "INVALID"; env.nodes["watch-form"].emit("submit");
    assert.equal(env.nodes["watch-account"].value, "INVALID", "Invalid input remains available for correction");
    const cancelled = environment({graph: true, ignoreAbort: true});
    const pendingButton = new Element("button"); pendingButton.attributes["data-export-graph"] = "";
    pendingButton.closest = selector => selector === "[data-siyab-graph]" ? cancelled.graph : pendingButton;
    const pending = cancelled.document.emit("click", {target: pendingButton}); cancelled.window.emit("pagehide");
    await reply(cancelled.requests[0], {records: [{id: "r1"}], source: {recorded: true}}); await pending;
    assert.equal(cancelled.downloads.length, 0, "A page that has been left cannot trigger a delayed export");
    const expired = environment({graph: true});
    const expiringButton = new Element("button"); expiringButton.attributes["data-export-graph"] = "";
    expiringButton.closest = selector => selector === "[data-siyab-graph]" ? expired.graph : expiringButton;
    const timeout = expired.document.emit("click", {target: expiringButton}); expired.timer(15000); await timeout;
    assert.equal(expired.downloads.length, 0); assert.equal(expiringButton.disabled, false); assert.match(expired.nodes["workspace-status"].textContent, /timed out/);
  } else if (scenario === "import") {
    const env = environment();
    const input = env.nodes["import-workspace"];
    let finishOld;
    input.files = [{size: 10, text: () => new Promise(resolve => { finishOld = resolve; })}];
    const old = input.emit("change");
    const newer = {...blank(), cases: [{...fixture(), id: "case-new", title: "New"}]};
    input.files = [{size: 100, text: async () => JSON.stringify(newer)}]; await input.emit("change");
    finishOld(JSON.stringify({...blank(), cases: [fixture()]})); await old;
    assert.equal(env.api.state().cases.length, 1, "Superseded imports cannot append stale data");
    assert.equal(env.api.state().cases[0].id, "case-new");
    input.files = [{size: 10, text: () => new Promise(() => {})}]; const timed = input.emit("change"); env.timer(15000); await timed;
    assert.match(env.nodes["workspace-status"].textContent, /timed out|cancelled/);
    const invalid = {...blank(), cases: [{...fixture(), url: "https://evil.example/"}]};
    input.files = [{size: 100, text: async () => JSON.stringify(invalid)}]; await input.emit("change"); assert.equal(env.api.state().cases.length, 1);
    input.files = [{size: 524289, text: () => { throw new Error("Oversized file must not be read"); }}]; await input.emit("change"); assert.match(env.nodes["workspace-status"].textContent, /512 KB/);
    let finishAfterReset;
    input.files = [{size: 10, text: () => new Promise(resolve => { finishAfterReset = resolve; })}];
    const resetting = input.emit("change"); env.nodes["reset-workspace"].emit("click");
    finishAfterReset(JSON.stringify(newer)); await resetting; assert.equal(env.api.state().cases.length, 0, "Reset cancels a pending import");
  } else if (scenario === "encrypted-merge") {
    const existing = {...blank(), cases: [{...fixture(), notes: "Existing browser notes"}], watchlist: ["alice.near"]};
    const env = environment({state: existing});
    const bridge = env.window.SiyabBrowserWorkspace;
    const snapshot = bridge.snapshot(); snapshot.cases[0].notes = "Changed copy";
    assert.equal(env.api.state().cases[0].notes, "Existing browser notes", "Snapshots cannot mutate browser state");
    const incoming = {...blank(), alerts: true, cases: [{...fixture(), notes: "Incoming notes"}, {...fixture(), id: "case-new", title: "<script>New</script>"}], watchlist: ["alice.near", "bob.near"]};
    bridge.merge(incoming);
    assert.equal(env.api.state().cases.length, 2);
    assert.equal(env.api.state().cases[0].notes, "Existing browser notes");
    assert.equal(env.api.state().alerts, false, "Decrypted imports cannot enable monitoring");
    assert.deepEqual([...env.api.state().watchlist], ["alice.near", "bob.near"]);
    assert.equal(env.nodes["saved-cases"].querySelectorAll("script").length, 0);
    assert.equal(JSON.parse(env.raw()).cases.length, 2);
    const before = env.raw();
    assert.throws(() => bridge.merge({...incoming, cases: [{...fixture(), url: "https://evil.example/"}]}), /valid/);
    assert.equal(env.raw(), before);
    const blocked = environment({raw: "{broken"});
    assert.throws(() => blocked.window.SiyabBrowserWorkspace.snapshot(), /unreadable/);
    assert.throws(() => blocked.window.SiyabBrowserWorkspace.merge(incoming), /unavailable/);
    assert.equal(blocked.raw(), "{broken");
    const quota = environment({state: existing, failWrite: true});
    assert.throws(() => quota.window.SiyabBrowserWorkspace.merge(incoming), /could not save/);
    assert.equal(quota.api.state().cases.length, 1);
    assert.equal(JSON.parse(quota.raw()).cases.length, 1);
  } else throw new Error(`Unknown scenario ${scenario}`);
  console.log(`Workspace ${scenario}: passed`);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
