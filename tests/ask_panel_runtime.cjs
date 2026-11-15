const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag = "") { this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.value = ""; this.textContent = ""; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.textContent = ""; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  emit(event) { return this.listeners[event]({currentTarget: this, target: this, preventDefault() {}}); }
  setAttribute(name, value) { this.attributes[name] = value; }
  remove() {}
  focus() { this.focused = true; }
}
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
const text = element => element.textContent + " " + element.children.map(text).join(" ");
const find = (element, predicate) => predicate(element) ? [element] : element.children.flatMap(child => find(child, predicate));
const json = (data, status = 200, headers = {}) => ({ok: status >= 200 && status < 300, status,
  headers: {get: name => ({"content-type": "application/json", ...headers})[name.toLowerCase()] ?? null}, json: async () => data});
const verdict = {outcome: "DENY", action: "deny", evidence_hash: "a".repeat(64), freshness: {block_height: 1}, verdict: null,
  signing: "never", latency_ms: 1, reasons: ["receiver does not exist"],
  checks: [{name: "receiver_exists", status: "signal", detail: "receiver does not exist"}]};
const signedIn = teams => ({authenticated: true, user: {id: "u1", username: "alice"}, csrf_token: "csrf-value", teams, registration_open: true});
const explained = {ask_version: "13", question_id: "why_verdict", question: "Why did the checks reach this outcome?", status: "explained",
  reason: null, message: null, outcome: "DENY", evidence_hash: "a".repeat(64), answerable: true,
  statements: [{text: "The evidence records a signal for the receiver exists check.", evidence_ids: ["check.receiver_exists"],
    evidence_chip: {item_id: "check.receiver_exists", fields: {status: "signal", detail: "<img src=x onerror=alert(1)>"}}}],
  not_in_evidence: ["The checks do not cover off chain identity."], unknown_notice: null, unknown_checks: [], cached: false,
  model: "m", generated_at: "2026-11-13T10:00:00Z", retry_after_s: null};
function setup(session, askReply = explained) {
  const nodes = {};
  const calls = [];
  const document = {
    getElementById: id => nodes[id] ||= Object.assign(new Element(), {value: id === "tool-token" ? "near" : ""}),
    querySelectorAll: () => [],
    createElement: tag => new Element(tag),
    body: new Element(),
  };
  vm.runInNewContext(fs.readFileSync("intel/ui/index.js", "utf8"), {
    document, window: {}, URLSearchParams, Blob, setTimeout, URL,
    FormData: class { constructor(form) { this.values = form.values; } get(key) { return this.values[key]; } },
    fetch: async (path, options = {}) => {
      calls.push({path, options});
      if (path === "/search.json") return json({accounts: []});
      if (path.startsWith("/live/reports")) return json({reports: []});
      if (path.startsWith("/live/verdict?")) return json(verdict);
      if (path === "/workspace-api/session") return session.error ? json(session, 404) : json(session);
      if (path.includes("/ask")) return typeof askReply === "function" ? askReply() : json(askReply);
      throw new Error("unexpected fetch " + path);
    },
  });
  return {nodes, calls};
}
async function check(env, receiver = "alice.near", amount = "2", token = "near") {
  env.nodes["tool-verdict-form"].values = {receiver, amount, token};
  await env.nodes["tool-verdict-form"].emit("submit");
  await flush();
  return env.nodes["tool-verdict-result"];
}
const buttons = panel => find(panel, node => node.tag === "button" && node.type === "button" && node.textContent !== "Download evidence");
async function main() {
  const scenario = process.argv[2];
  if (scenario === "signed-out") {
    const env = setup({authenticated: false, user: null, teams: [], registration_open: true});
    const panel = await check(env);
    assert.match(text(panel), /Sign in to ask Siyab to explain this outcome\./);
    const link = find(panel, node => node.tag === "a")[0];
    assert.equal(link.href, "/workspace.html");
    assert.equal(buttons(panel).length, 0);
    assert.ok(!env.calls.some(call => call.path.includes("/ask")), "Signed out must never call the ask route");
  } else if (scenario === "no-team") {
    const env = setup(signedIn([]));
    const panel = await check(env);
    assert.match(text(panel), /Create a team to use Ask Siyab\./);
    assert.ok(!env.calls.some(call => call.path.includes("/ask")));
  } else if (scenario === "post") {
    const env = setup(signedIn([{id: "t1", name: "One", role: "owner"}, {id: "t 2", name: "Two", role: "viewer"}]));
    const panel = await check(env, "alice.near", "2", "near");
    const labels = buttons(panel).map(button => button.textContent);
    assert.deepEqual(labels, ["Why did the checks reach this outcome?", "What is unknown, and why?",
      "What in the evidence would have to change for the outcome to change?",
      "What do the labels in the evidence say about these accounts?"]);
    const select = find(panel, node => node.tag === "select")[0];
    select.value = "t 2";
    select.emit("change");
    const verdictPath = env.calls.find(call => call.path.startsWith("/live/verdict?")).path;
    const sent = new URLSearchParams(verdictPath.split("?")[1]);
    buttons(panel)[2].emit("click");
    await flush();
    const ask = env.calls.find(call => call.path.includes("/ask"));
    assert.equal(ask.path, "/workspace-api/teams/t%202/ask");
    assert.equal(ask.options.method, "POST");
    assert.equal(ask.options.credentials, "same-origin");
    assert.equal(ask.options.headers["X-CSRF-Token"], "csrf-value");
    assert.equal(ask.options.headers["Content-Type"], "application/json");
    const body = JSON.parse(ask.options.body);
    assert.deepEqual(Object.keys(body).sort(), ["amount", "question_id", "receiver", "token"]);
    assert.deepEqual(body, {receiver: sent.get("receiver"), amount: sent.get("amount"), token: sent.get("token"), question_id: "what_changes"});
    assert.equal(body.amount, "2000000000000000000000000");
  } else if (scenario === "render") {
    const env = setup(signedIn([{id: "t1", name: "One", role: "owner"}]));
    const panel = await check(env);
    assert.equal(find(panel, node => node.tag === "select").length, 0);
    buttons(panel)[0].emit("click");
    await flush();
    const answer = find(panel, node => node.className === "ask-answer")[0];
    assert.equal(answer.attributes["aria-live"], "polite");
    const output = text(answer);
    assert.match(output, /The rules decided this outcome\. Ask Siyab only explains it\./);
    assert.match(output, /The evidence records a signal for the receiver exists check\./);
    const chip = find(answer, node => node.className === "ask-chip")[0];
    assert.equal(chip.textContent, "check.receiver_exists · status: signal · detail: <img src=x onerror=alert(1)>");
    assert.equal(find(answer, node => node.tag === "img").length, 0);
    assert.match(output, /off chain identity/);
    assert.ok(find(panel, node => "innerHTML" in node).length === 0);
  } else if (scenario === "unavailable") {
    const cases = [
      [{status: "unavailable", reason: "user_quota", retry_after_s: 3600}, /daily limit.*Try again in 3600 seconds\./],
      [{status: "unavailable", reason: "question_refused", retry_after_s: null}, /does not answer this question/],
      [{status: "unavailable", reason: "evidence_empty", retry_after_s: null}, /no evidence to explain/],
      [{status: "unavailable", reason: "disabled", retry_after_s: null}, /turned off/],
      [{status: "unavailable", reason: "brand_new_code", retry_after_s: null}, /Reason code: brand_new_code\./],
    ];
    for (const [reply, pattern] of cases) {
      const env = setup(signedIn([{id: "t1", name: "One", role: "owner"}]),
        {...explained, ...reply, statements: [], answerable: null, message: "No explanation is available. The outcome and its evidence are shown unchanged.",
          unknown_notice: "At least one check is unresolved.", unknown_checks: ["receiver_exists"]});
      const panel = await check(env);
      buttons(panel)[0].emit("click");
      await flush();
      const output = text(find(panel, node => node.className === "ask-answer")[0]);
      assert.match(output, pattern);
      assert.match(output, /No explanation is available/);
      assert.match(output, /At least one check is unresolved\./);
    }
    const env = setup(signedIn([{id: "t1", name: "One", role: "owner"}]),
      () => json({error: "rate_limited", message: "Too many workspace requests. Try again later."}, 429, {"retry-after": "7"}));
    const panel = await check(env);
    buttons(panel)[0].emit("click");
    await flush();
    assert.match(text(panel), /Too many requests were made\. Try again in 7 seconds\./);
    assert.doesNotMatch(text(panel), /Try again later\./);
    assert.equal(buttons(panel)[0].focused, true);
  } else if (scenario === "clear") {
    const env = setup(signedIn([{id: "t1", name: "One", role: "owner"}]));
    let panel = await check(env);
    buttons(panel)[0].emit("click");
    await flush();
    assert.match(text(panel), /Ask Siyab only explains it/);
    env.nodes["tool-verdict-form"].values = {receiver: "bob.near", amount: "1", token: "near"};
    const pending = env.nodes["tool-verdict-form"].emit("submit");
    assert.doesNotMatch(text(panel), /Ask Siyab/, "A new check must clear the Ask section at once");
    await pending;
    await flush();
    panel = env.nodes["tool-verdict-result"];
    assert.equal(find(panel, node => node.className === "ask-panel").length, 1);
    assert.doesNotMatch(text(panel), /only explains it/);
  } else if (scenario === "late") {
    let release;
    const env = setup(signedIn([{id: "t1", name: "One", role: "owner"}]),
      () => new Promise(resolve => { release = () => resolve(json(explained)); }));
    let panel = await check(env);
    buttons(panel)[0].emit("click");
    await flush();
    await check(env, "bob.near", "1", "near");
    release();
    await flush();
    panel = env.nodes["tool-verdict-result"];
    assert.doesNotMatch(text(panel), /only explains it|receiver exists check/, "A late answer from an older check must be ignored");
    assert.equal(buttons(panel).length, 4);
  } else if (scenario === "outcome-changed") {
    const env = setup(signedIn([{id: "t1", name: "One", role: "owner"}]), {...explained, outcome: "ALLOW"});
    const panel = await check(env);
    buttons(panel)[0].emit("click");
    await flush();
    const output = text(find(panel, node => node.className === "ask-answer")[0]);
    assert.match(output, /The outcome changed since this check\. Run the check again\./);
    assert.doesNotMatch(output, /receiver exists check|only explains it/);
  } else if (scenario === "errors") {
    const cases = [
      [() => json({error: "not_found", message: "This route does not exist."}, 404), /This route does not exist\./],
      [() => json({error: "invalid_question", message: "Unknown question."}, 400), /Unknown question\./],
      [() => json({error: "csrf_denied", message: "x"}, 403), /Refresh the page and try again\./],
      [() => json({error: "forbidden", message: "x"}, 403), /You are not a member of this team\./],
      [() => json({error: "authentication_required", message: "x"}, 401), /Your session has ended\. Sign in again to ask Siyab\./],
      [() => Promise.reject(new Error("offline")), /Ask Siyab did not return an answer\./],
      [() => json(null, 502, {"content-type": "text/html"}), /Ask Siyab did not return an answer\./],
    ];
    for (const [reply, pattern] of cases) {
      const env = setup(signedIn([{id: "t1", name: "One", role: "owner"}]), reply);
      const panel = await check(env);
      buttons(panel)[0].emit("click");
      await flush();
      const output = text(find(panel, node => node.className === "ask-answer")[0]);
      assert.match(output, pattern);
      assert.doesNotMatch(output, /Reason code/);
      assert.ok(buttons(panel).every(button => !button.disabled));
    }
    const env = setup(signedIn([{id: "t1", name: "One", role: "owner"}]), () => json({error: "authentication_required"}, 401));
    const panel = await check(env);
    buttons(panel)[0].emit("click");
    await flush();
    assert.equal(find(panel, node => node.tag === "a")[0].href, "/workspace.html");
  } else if (scenario === "no-session") {
    const env = setup({error: "not_found"}, explained);
    const panel = await check(env);
    assert.match(text(panel), /Ask Siyab is not available on this copy\./);
    assert.ok(!env.calls.some(call => call.path.includes("/ask")));
  } else if (scenario === "reasons") {
    const source = fs.readFileSync("intel/ui/index.js", "utf8");
    const block = source.slice(source.indexOf("const askReasons = {"), source.indexOf("};", source.indexOf("const askReasons = {")));
    const reasons = [...block.matchAll(/^  (\w+): "([^"]+)"/gm)].map(match => [match[1], match[2]]);
    assert.equal(reasons.length, 23);
    for (const [reason, expected] of reasons) {
      const env = setup(signedIn([{id: "t1", name: "One", role: "owner"}]),
        {...explained, status: "unavailable", reason, statements: [], message: "No explanation is available."});
      const panel = await check(env);
      buttons(panel)[0].emit("click");
      await flush();
      assert.ok(text(find(panel, node => node.className === "ask-answer")[0]).includes(expected), reason);
    }
  } else {
    throw new Error("unknown scenario " + scenario);
  }
}
main().catch(error => { console.error(error); process.exit(1); });
