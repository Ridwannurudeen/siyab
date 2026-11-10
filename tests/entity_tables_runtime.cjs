const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor(tag = "") { this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.textContent = ""; this.value = ""; this.hidden = false; this.attributes = {}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = value; }
  focus() { this.focused = true; if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  emit(event) { this.listeners[event](); }
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const response = rows => {
  const body = new TextEncoder().encode(JSON.stringify({rows})); let sent = false;
  return {ok: true, headers: {get: () => null}, body: {getReader: () => ({async read() {if (sent) return {done: true}; sent = true; return {done: false, value: body};}, releaseLock() {}})}};
};
function setup() {
  const selectors = [".entity-table-toolbar", "[data-table-search]", "[data-table-chain]", "[data-table-sort]", ".entity-table-status", ".entity-table-pagination", "[data-table-retry]", "[data-table-empty]"];
  const nodes = Object.fromEntries(selectors.map(selector => [selector, new Node()]));
  nodes["[data-table-sort]"].value = "recorded";
  const panel = new Node(); panel.dataset = {source: "organisations.table.json", page: "1"};
  panel.querySelector = selector => nodes[selector];
  const section = new Node(); section.dataset.entityKind = "stated";
  const tbody = new Node(); tbody.append(new Node("original-row"));
  const sectionCount = new Node();
  section.querySelector = selector => selector === "tbody" ? tbody : sectionCount;
  section.querySelectorAll = () => ["Subject", "Chain", "Label", "Category", "Provenance", "Note"].map(text => Object.assign(new Node("th"), {textContent: text}));
  panel.querySelectorAll = () => [section];
  const requests = [];
  const document = {activeElement: null, querySelectorAll: () => [panel], createElement: tag => Object.assign(new Node(tag), {ownerDocument: document}), createTextNode: text => Object.assign(new Node("text"), {textContent: text})};
  for (const node of Object.values(nodes)) node.ownerDocument = document;
  vm.runInNewContext(fs.readFileSync("intel/ui/tables.js", "utf8"), {
    document,
    fetch: () => new Promise((resolve, reject) => requests.push({resolve, reject})),
    AbortController, TextDecoder, setTimeout, clearTimeout,
  });
  return {nodes, panel, tbody, requests, document};
}
function row(index) {
  const subject = `account-${index}.near`;
  return {id: index.toString(16).padStart(64, "0"), kind: "stated", subject, chain: index % 2 ? "near" : "eth", label: `Label ${index}`, note: "recorded", search: `${subject} Label ${index}`, cells: [[{text: subject, quoted: true, url: `../accounts/${subject}.html`}], [{text: "near"}], [{text: `Label ${index}`}], [{text: "organisation"}], [{text: "onchain"}], [{text: "recorded"}]]};
}
async function main() {
  const rows = Array.from({length: 70}, (_, index) => row(index + 1));
  const env = setup();
  assert.equal(env.requests.length, 0, "Initial page must not fetch the whole collection");
  env.nodes["[data-table-search]"].value = "account-65";
  env.nodes["[data-table-search]"].emit("input");
  env.nodes["[data-table-search]"].value = "account-69";
  env.nodes["[data-table-search]"].emit("input");
  assert.equal(env.requests.length, 1, "Concurrent changes share one dataset load");
  env.requests[0].resolve(response(rows)); await flush();
  assert.equal(env.tbody.children.length, 1);
  assert.equal(env.tbody.children[0].dataset.assertionId, rows[68].id, "Latest query wins after an asynchronous load, including records beyond page one");
  env.nodes["[data-table-search]"].value = ""; env.nodes["[data-table-search]"].emit("input"); await flush();
  assert.equal(env.tbody.children.length, 50);
  env.nodes[".entity-table-pagination"].children[2].emit("click");
  assert.equal(env.tbody.children.length, 20);
  assert.equal(env.nodes[".entity-table-pagination"].children[0].focused, true, "At the last page keyboard focus moves to the enabled previous control");
  env.nodes["[data-table-chain]"].value = "eth"; env.nodes["[data-table-chain]"].emit("change"); await flush();
  assert.equal(env.tbody.children.length, 35);
  env.nodes["[data-table-search]"].value = "absent"; env.nodes["[data-table-search]"].emit("input"); await flush();
  assert.equal(env.nodes["[data-table-empty]"].hidden, false);

  const failure = setup();
  failure.nodes["[data-table-search]"].emit("input");
  failure.requests[0].reject(new Error("offline")); await flush();
  assert.equal(failure.tbody.children[0].tag, "original-row", "Failed search retains the static page");
  assert.equal(failure.nodes["[data-table-retry]"].hidden, false);
  failure.nodes["[data-table-retry]"].focus();
  failure.nodes["[data-table-retry]"].emit("click");
  assert.equal(failure.document.activeElement, failure.nodes[".entity-table-status"], "Retry moves focus to the visible loading status before hiding its control");
  const malicious = row(1); malicious.cells[0][0].url = "javascript:alert(1)";
  failure.requests[1].resolve(response([malicious])); await flush();
  assert.equal(failure.tbody.children[0].tag, "original-row", "Invalid paths never replace the static page");
  assert.equal(failure.document.activeElement, failure.nodes["[data-table-retry]"], "Failed retry restores the retry control");
  failure.nodes["[data-table-retry]"].emit("click");
  failure.requests[2].resolve(response(rows)); await flush();
  assert.equal(failure.tbody.children.length, 50, "Retry recovers without losing data");
  assert.equal(failure.document.activeElement, failure.nodes["[data-table-search]"], "Successful retry restores a usable search control");
  const moved = setup();
  moved.nodes["[data-table-search]"].emit("input"); moved.requests[0].reject(new Error("offline")); await flush();
  moved.nodes["[data-table-retry]"].focus(); moved.nodes["[data-table-retry]"].emit("click");
  moved.nodes["[data-table-chain]"].focus();
  moved.requests[1].resolve(response(rows)); await flush();
  assert.equal(moved.document.activeElement, moved.nodes["[data-table-chain]"], "Retry completion never steals focus after the reader moves away");
  console.log("Entity table runtime: lazy loading, query race, cross-page search, pagination, filtering, empty state, retry and URL validation passed.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
