const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const flush = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.attributes = {}; this.textContent = ''; this.value = ''; this.isConnected = true; this.classList = {toggle() {}}; }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children = children; this.textContent = ''; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  dispatchEvent(event) { this.listeners[event.type]?.(event); return true; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] || null; }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(selector.split(',').includes(child.tag) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.close?.(); }
  remove() { this.isConnected = false; }
  click() {}
  focus() {}
}
function environment(options = {}) {
  const nodes = {}, requests = [], timers = new Map(), writes = [], downloads = [];
  let next = 0, confirmations = 0;
  const document = {createElement: tag => new Element(tag), getElementById: id => nodes[id] || null, querySelectorAll: () => []};
  document.body = new Element('body'); document.activeElement = document.body;
  const window = new Element('window');
  vm.runInNewContext(fs.readFileSync('intel/ui/number-format.js', 'utf8'), {window});
  nodes['workspace-feedback'] = new Element('p');
  if (options.globalSave) {
    for (const [id, tag] of [['case-form', 'form'], ['case-title', 'input'], ['case-notes', 'textarea'], ['case-source', 'p'], ['case-error', 'p'], ['save-investigation', 'dialog']]) nodes[id] = new Element(tag);
    nodes['case-title'].value = 'Global case'; nodes['case-notes'].value = 'Private notes'; nodes['case-source'].textContent = '/investigate.html?account=alice.near';
    const submit = new Element('button'); submit.type = 'submit'; nodes['case-form'].append(nodes['case-title'], nodes['case-notes'], submit);
    nodes['save-investigation'].append(nodes['case-form']); nodes['save-investigation'].open = true;
  }
  if (options.workspace) { nodes['cloud-workspace'] = new Element('details'); nodes['cloud-shell'] = new Element('div'); }
  const source = fs.readFileSync('intel/ui/cloud-workspace.js', 'utf8');
  const marker = source.lastIndexOf('})();');
  const code = source.slice(0, marker) + 'globalThis.cloudTest={localUrl,importValue,chunks,request,invalidate,allCases,importWorkspace,loadSession,renderCases,renderWatches,renderHistory,editCase,recovery,report,state};\n' + source.slice(marker);
  const context = {document, window, CustomEvent: class {constructor(type, options = {}) {this.type = type; this.detail = options.detail;}}, location: {origin: 'https://siyab.xyz'}, URL: class extends URL {static createObjectURL(blob) {downloads.push(blob); return 'blob:download';} static revokeObjectURL() {}}, Blob, TextEncoder, AbortController,
    confirm: () => { confirmations++; return options.confirm !== false; },
    localStorage: {getItem() { return options.local || null; }, setItem(...value) {writes.push(value);}},
    setTimeout: (callback, delay) => {const id = ++next; timers.set(id, {callback, delay}); return id;}, clearTimeout: id => timers.delete(id),
    FormData: class {constructor(element) {this.inputs = [...element.querySelectorAll('input'), ...element.querySelectorAll('textarea')];} get(name) {return this.inputs.find(input => input.name === name)?.value;}},
    fetch: (url, settings) => new Promise((resolve, reject) => {requests.push({url, settings, resolve, reject}); if (!options.ignoreAbort) settings.signal.addEventListener('abort', () => reject(new Error('aborted')));}),
  };
  vm.runInNewContext(code, context);
  return {api: context.cloudTest, nodes, requests, timers, document, window, writes, downloads, confirmations: () => confirmations};
}
async function reply(request, data, status = 200) {request.resolve({ok: status < 400, status, headers: {get: () => 'application/json'}, json: async () => data}); await flush();}
const team = {id: 'team1', name: 'Research', role: 'owner'};
const session = {authenticated: true, user: {id: 'user1', username: 'alice'}, csrf_token: 'csrf-private', teams: [team]};
const example = () => ({version: 1, cases: [{title: 'Case', notes: 'Evidence', url: '/investigate.html?account=alice.near'}], watchlist: ['alice.near']});
async function main() {
  const scenario = process.argv[2];
  if (scenario === 'registration-access-code') {
    for (const registration_open of [true, false]) {
      const env = environment({workspace: true});
      await reply(env.requests[0], {authenticated: false, teams: [], registration_open});
      const shell = env.nodes['cloud-shell'];
      const create = shell.querySelectorAll('button').find(control => control.dataset.authMode === 'register');
      assert.ok(create); assert.notEqual(create.disabled, true); create.listeners.click();
      const form = shell.querySelector('form');
      const code = form.querySelectorAll('input').find(input => input.name === 'access_code');
      assert.ok(code); assert.equal(code.required, !registration_open);
      form.querySelectorAll('input').find(input => input.name === 'username').value = 'coded';
      form.querySelectorAll('input').find(input => input.name === 'password').value = 'test-password-123';
      code.value = registration_open ? '' : ' ' + 'a'.repeat(43) + ' ';
      const saving = form.listeners.submit({preventDefault() {}});
      const sent = JSON.parse(env.requests[1].settings.body);
      assert.equal(Object.hasOwn(sent, 'access_code'), !registration_open);
      if (!registration_open) assert.equal(sent.access_code, code.value.trim());
      await reply(env.requests[1], {error: 'test_error', message: 'Registration unavailable.'}, 403); await saving;
      code.value = ' \t' + 'b'.repeat(43) + ' \t';
      const retry = form.listeners.submit({preventDefault() {}});
      assert.equal(JSON.parse(env.requests[2].settings.body).access_code, code.value.trim());
      await reply(env.requests[2], {error: 'test_error', message: 'Registration unavailable.'}, 403); await retry;
      if (registration_open) {
        code.value = '   ';
        const blank = form.listeners.submit({preventDefault() {}});
        assert.equal(Object.hasOwn(JSON.parse(env.requests[3].settings.body), 'access_code'), false);
        await reply(env.requests[3], {error: 'test_error', message: 'Registration unavailable.'}, 403); await blank;
      }
    }
  } else if (scenario === 'privacy-validation') {
    const env = environment({local: JSON.stringify(example())});
    assert.equal(env.requests.length, 1); assert.equal(env.requests[0].url, '/workspace-api/session');
    await reply(env.requests[0], session);
    assert.equal(env.requests.length, 1, 'Sign-in must never upload local cases'); assert.equal(env.writes.length, 0);
    for (const url of ['javascript:alert(1)', '//evil.com/index.html', '/\\evil.com/index.html', '/accounts/../../secret.html']) assert.equal(env.api.localUrl(url), null);
    assert.equal(env.api.localUrl('/investigate.html?account=alice.near'), '/investigate.html?account=alice.near');
    assert.throws(() => env.api.importValue({...example(), watchlist: ['alice.testnet']}));
    assert.throws(() => env.api.importValue({...example(), cases: [{...example().cases[0], url: 'https://evil.com'}]}));
    const malicious = example(); malicious.cases[0].title = '<img src=x onerror=alert(1)>';
    assert.equal(env.api.importValue(malicious).cases[0].title, malicious.cases[0].title);
    const parent = new Element('section'); env.api.state.team = team;
    env.api.renderCases(parent, {total: 1, cases: [{...malicious.cases[0], id: 'case1', version: 1, updated_ms: 0}], next_offset: null}, '/teams/team1');
    assert.equal(parent.querySelectorAll('img').length, 0);
    assert.match(parent.querySelectorAll('a')[0].textContent, /<img/);
    const emoji = '\u{1f600}';
    const unicode = {title: emoji.repeat(120), notes: emoji.repeat(4000), url: '/investigate.html?q=' + emoji.repeat(1100)};
    const restored = env.api.importValue({...example(), cases: [unicode]});
    assert.equal(restored.cases[0].url, unicode.url, 'Valid Unicode URLs must round-trip without expanding their stored length');
    env.api.renderCases(new Element('section'), {total: 1, cases: [{...unicode, id: 'unicode', version: 1, updated_ms: 0}], next_offset: null}, '/teams/team1');
    env.api.renderCases(new Element('section'), {total: 1, cases: [{...example().cases[0], title: '\ufeff', id: 'unicode-space', version: 1, updated_ms: 0}], next_offset: null}, '/teams/team1');
    assert.equal(env.api.importValue({...example(), cases: [{...example().cases[0], title: '\ufeff'}]}).cases[0].title, '\ufeff');
    assert.throws(() => env.api.importValue({...example(), cases: [{...unicode, title: emoji.repeat(121)}]}));
    assert.throws(() => env.api.importValue({...example(), cases: [{...unicode, notes: emoji.repeat(4001)}]}));
  } else if (scenario === 'stale-export') {
    const env = environment({ignoreAbort: true}); await reply(env.requests[0], {authenticated: false, teams: []});
    const promise = env.api.allCases('/teams/team1'); const rejected = assert.rejects(promise);
    await reply(env.requests[1], {cases: [{title: 'first'}], next_offset: 50});
    assert.equal(env.requests.length, 3);
    env.api.invalidate(); await reply(env.requests[2], {cases: [{title: 'private'}], next_offset: null});
    await rejected;
  } else if (scenario === 'cancelled-import') {
    const env = environment({confirm: false}); await reply(env.requests[0], {authenticated: false, teams: []}); env.api.state.team = team;
    await env.api.importWorkspace(env.api.importValue(example()), '/teams/team1');
    assert.equal(env.confirmations(), 1); assert.equal(env.requests.length, 1); assert.equal(env.writes.length, 0);
  } else if (scenario === 'chunk-import') {
    const env = environment(); await reply(env.requests[0], session);
    const value = example(); value.cases = Array.from({length: 20}, (_, index) => ({...value.cases[0], title: 'Case ' + index, notes: '字'.repeat(4000)}));
    const batches = env.api.chunks(value); assert.ok(batches.length > 1);
    const tiny = example(); tiny.cases = Array.from({length: 51}, (_, index) => ({...tiny.cases[0], title: 'Tiny ' + index}));
    assert.equal(env.api.chunks(tiny).length, 2); assert.equal(env.api.chunks(tiny)[0].cases.length, 50);
    for (const batch of batches) assert.ok(new TextEncoder().encode(JSON.stringify(batch)).length <= 24576);
    const promise = env.api.importWorkspace(value, '/teams/team1');
    for (let index = 0; index < batches.length; index++) {
      const request = env.requests[index + 1]; assert.equal(request.url, '/workspace-api/teams/team1/import');
      assert.equal(request.settings.credentials, 'same-origin'); assert.equal(request.settings.headers['X-CSRF-Token'], 'csrf-private');
      await reply(request, {created_cases: batches[index].cases.length, added_watches: batches[index].watchlist.length, skipped_cases: 0});
    }
    await promise; assert.equal(env.writes.length, 0);
  } else if (scenario === 'body-timeout') {
    const env = environment(); await reply(env.requests[0], {authenticated: false, teams: []});
    const promise = env.api.request('/teams'); const rejected = assert.rejects(promise, /timed out/);
    let finish; env.requests[1].resolve({ok: true, headers: {get: () => 'application/json'}, json: () => new Promise(resolve => finish = resolve)}); await flush();
    [...env.timers.values()].find(timer => timer.delay === 12000).callback(); finish({teams: []}); await rejected;
    assert.equal(env.timers.size, 0);
  } else if (scenario === 'session-race') {
    const env = environment({ignoreAbort: true}); await reply(env.requests[0], session);
    const stale = env.api.request('/teams/team1/cases'); const rejection = assert.rejects(stale);
    const refresh = env.api.loadSession(); assert.equal(env.api.state.session, null, 'Previous identity must disappear while the replacement session is read');
    await reply(env.requests[2], {...session, user: {id: 'user2', username: 'bob'}}); await refresh;
    await reply(env.requests[1], {cases: [{notes: 'alice private'}]}); await rejection;
    const count = env.requests.length;
    await assert.rejects(env.api.request('/teams/team1/cases', undefined, env.api.state.epoch - 1));
    assert.equal(env.requests.length, count, 'A stale task must not send another request under a replacement session');
  } else if (scenario === 'version-conflict') {
    const env = environment(); await reply(env.requests[0], session);
    const item = {...example().cases[0], id: 'case1', version: 1};
    env.api.editCase(item, '/teams/team1');
    const dialog = env.document.body.children.at(-1), edit = dialog.querySelector('form');
    const notes = edit.querySelector('textarea'); notes.value = 'My unsaved draft';
    const saving = edit.listeners.submit({preventDefault() {}});
    assert.equal(JSON.parse(env.requests[1].settings.body).version, 1);
    await reply(env.requests[1], {error: 'version_conflict', message: 'Reload before editing.'}, 409); await saving;
    assert.equal(notes.value, 'My unsaved draft'); assert.equal(dialog.open, true);
    const reload = edit.querySelectorAll('button').find(button => button.textContent === 'Reload latest version'); assert.equal(reload.hidden, false);
    const loading = reload.listeners.click();
    await reply(env.requests[2], {cases: [{...item, notes: 'Another editor saved this', version: 2}], next_offset: null}); await loading;
    assert.equal(notes.value, 'Another editor saved this');
    const retry = edit.listeners.submit({preventDefault() {}});
    assert.equal(JSON.parse(env.requests[3].settings.body).version, 2);
    await reply(env.requests[3], {...item, version: 3}); await retry; assert.equal(dialog.open, false);
  } else if (scenario === 'viewer-controls') {
    const env = environment(); await reply(env.requests[0], {...session, teams: [{...team, role: 'viewer'}]});
    const parent = new Element('section');
    env.api.renderCases(parent, {total: 1, cases: [{...example().cases[0], id: 'c1', version: 1, updated_ms: 0}], next_offset: null}, '/teams/team1');
    for (const title of ['New investigation', 'Edit notes', 'Delete', 'Import browser workspace']) assert.equal(parent.querySelectorAll('button').find(control => control.textContent === title).disabled, true);
    assert.equal(parent.querySelectorAll('button').find(control => control.textContent === 'Export shared workspace JSON').disabled, false);
  } else if (scenario === 'private-editor-transition') {
    const env = environment(); await reply(env.requests[0], session);
    env.api.editCase({...example().cases[0], id: 'c1', version: 1}, '/teams/team1');
    const dialog = env.document.body.children.at(-1), edit = dialog.querySelector('form'), notes = edit.querySelector('textarea');
    env.api.report(Object.assign(new Error('Session expired'), {status: 401}));
    assert.equal(dialog.isConnected, false, 'Expired sessions must remove private dialogs');
    assert.equal(notes.value, '', 'Detached editor fields must be scrubbed');
    const count = env.requests.length; await edit.listeners.submit({preventDefault() {}});
    assert.equal(env.requests.length, count, 'A detached private editor cannot post using a replacement session');
  } else if (scenario === 'global-save') {
    const env = environment({globalSave: true}); await reply(env.requests[0], session);
    const form = env.nodes['case-form'], select = form.querySelector('select');
    assert.equal(select.value, 'browser', 'Browser remains the default save destination');
    let captured = 0;
    await form.listeners.submit({preventDefault() {captured++;}, stopImmediatePropagation() {captured++;}});
    assert.equal(captured, 0, 'Local submit is left to the existing browser workspace handler');
    select.value = team.id;
    const pending = form.listeners.submit({preventDefault() {captured++;}, stopImmediatePropagation() {captured++;}});
    assert.equal(captured, 2); assert.equal(env.requests[1].url, '/workspace-api/teams/team1/cases');
    assert.equal(env.nodes['case-title'].disabled, true, 'A pending save cannot be edited into a different draft');
    await reply(env.requests[1], {...example().cases[0], id: 'case1', version: 1}); await pending;
    assert.equal(env.nodes['save-investigation'].open, false); assert.equal(select.value, 'browser'); assert.equal(env.writes.length, 0);
  } else if (scenario === 'recovery-identity') {
    const env = environment(); await reply(env.requests[0], session);
    env.api.recovery('one-time-recovery', 'alice', 'user1');
    const dialog = env.document.body.children.at(-1), code = dialog.querySelector('pre');
    const same = env.api.loadSession(); await reply(env.requests[1], session); await same;
    assert.equal(dialog.isConnected, true, 'Same-user refresh must preserve the newly issued recovery code');
    const other = env.api.loadSession(); await reply(env.requests[2], {...session, user: {id: 'user2', username: 'bob'}}); await other;
    assert.equal(dialog.isConnected, false); assert.equal(code.textContent, '', 'Another identity cannot retain the old recovery code');
    assert.equal(env.writes.length, 0);
  } else if (scenario === 'monitor-evidence') {
    const env = environment(); await reply(env.requests[0], session);
    const parent = new Element('section');
    env.api.renderWatches(parent, [], {configured: true, running: true, account_polling: {running: false, heartbeat_ms: 1, monitored_accounts: 4, initialized_accounts: 2, coverage_gap_accounts: 2, retry_pending: 1, backfill_pending: 3, pending_events: 4}}, '/teams/team1');
    const text = node => node.textContent + ' ' + node.children.map(text).join(' ');
    assert.match(text(parent), /Retained-feed monitoring is running/);
    assert.match(text(parent), /Ordinary-transfer polling is not currently reporting a running worker/);
    assert.match(text(parent), /2 accounts with recorded coverage gaps/);
    env.api.state.panel = 'inbox';
    const history = new Element('section'), hash = 'a'.repeat(64);
    const detail = 'full access key ed25519:abc added to alice.near; transfer of 1000000000000000000000000 yoctoNEAR to x.near within 452158 ms';
    const event = {signal: 'full_access_key_then_outflow', detail};
    env.api.renderHistory(history, {items: [{event, event_id: hash, id: 1, account: 'alice.near'}], next_after: 1, next_before: 1, has_older: false}, '/teams/team1');
    assert.ok(history.querySelectorAll('p').some(node => node.textContent === 'full access key ed25519:abc added to alice.near; transfer of 1 NEAR to x.near within 7 min 32 s'));
    assert.doesNotMatch(text(history), /yoctoNEAR|452158 ms/);
    assert.equal(event.detail, detail, 'Displayed prose does not change the evidence record');
    assert.equal(history.querySelectorAll('a').find(link => link.textContent === 'Open evidence').href, '/evidence/' + hash);
    const transfers = new Element('section');
    env.api.renderHistory(transfers, {items: [{event: {signal: 'account_transfer_observed', detail: 'Indexed transfer.'}, event_id: hash, id: 2, account: 'alice.near'}], next_after: 2, next_before: 2, has_older: false}, '/teams/team1');
    assert.equal(transfers.querySelectorAll('a').find(link => link.textContent === 'Open evidence').href, '/workspace-api/teams/team1/events/' + hash);
  } else throw new Error('Unknown scenario');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
