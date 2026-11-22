const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const tick = () => new Promise(resolve => setImmediate(resolve));
const normal = value => JSON.parse(JSON.stringify(value));
const origin = 'https://siyab.xyz', account = 'alice.near';
const alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const seed = Buffer.from(Array.from({length: 32}, (_, index) => index + 1));
const signingKey = crypto.createPrivateKey({key: Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), seed]), format: 'der', type: 'pkcs8'});
const rawKey = crypto.createPublicKey(signingKey).export({format: 'der', type: 'spki'}).subarray(-32);
let number = BigInt('0x' + rawKey.toString('hex')), encoded = '';
while (number) { encoded = alphabet[Number(number % 58n)] + encoded; number /= 58n; }
for (const byte of rawKey) { if (byte !== 0) break; encoded = '1' + encoded; }
const publicKey = 'ed25519:' + encoded;
const authMessage = challenge => 'Sign in to Siyab. This request does not authorize a transaction or payment.\nAccount: ' + challenge.account_id
  + '\nOrigin: ' + challenge.recipient + '\nChallenge: ' + challenge.challenge_id + '\nIssued at: ' + (challenge.expires_at - 300000) + '\nExpires at: ' + challenge.expires_at;
const fixture = () => ({version: 1, cases: [{id: 'case-confidential-20261121', title: 'Confidential title 20261121', notes: 'Confidential notes 20261121 π', url: '/investigate.html?account=confidential-20261121.near', created_at: '2026-11-21T00:00:00Z'}], watchlist: ['confidential-watch-20261121.near'], alerts: true, notifications: []});
class Element {
  constructor() { this.listeners = {}; this.value = ''; this.textContent = ''; this.disabled = false; this.hidden = false; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
async function until(predicate, message) {
  const deadline = Date.now() + 4000;
  while (!predicate()) { if (Date.now() > deadline) throw new Error('Timed out: ' + message); await tick(); }
}
function environment(options = {}) {
  const ids = {}, events = {}, requests = [], walletCalls = [], connectCalls = [], timers = new Map(), merges = [], writes = [], confirmations = [], cryptoPending = [];
  const storage = options.storage || new Map(), storageWrites = [];
  let timer = 0, snapshotCalls = 0, connectedAccount = options.disconnected ? null : account;
  const server = options.server || {vault: options.vault ? normal(options.vault) : null, revision: options.vaultRevision || (options.vault ? 1 : 0)};
  for (const id of ['near-workspace', ...['sign-in', 'controls', 'identity', 'login', 'account', 'unlock', 'save', 'restore', 'lock', 'logout', 'status', 'reset-form', 'reset-confirmation', 'reset'].map(id => 'near-' + id)]) ids[id] = new Element();
  ids['near-account'].value = account;
  const context = {origin, accountId: account, publicKey};
  const session = {authenticated: true, account_id: account, public_key: publicKey, csrf_token: 's'.repeat(43), expires_ms: Date.now() + 3600000};
  const challenge = {challenge_id: 'c'.repeat(43), state: 'c'.repeat(43), account_id: account, nonce: Buffer.from(Array.from({length: 32}, (_, index) => index)).toString('base64'), recipient: origin, callbackUrl: origin + '/workspace.html', expires_at: Date.now() + 60000, ...options.challenge};
  if (!Object.hasOwn(challenge, 'message')) challenge.message = authMessage(challenge);
  const document = {getElementById: id => ids[id] || null, addEventListener(name, callback) { events['document:' + name] = callback; }};
  const window = {addEventListener(name, callback) { events[name] = callback; }, SiyabBrowserWorkspace: {
    snapshot() { snapshotCalls++; return fixture(); },
    merge(value) { merges.push(normal(value)); writes.push({destination: 'browser', value: normal(value)}); }
  }};
  const subtle = new Proxy(crypto.webcrypto.subtle, {get(target, name) {
    if (name === 'verify' && options.noisyVerification) return async () => true;
    if ((name === 'encrypt' || name === 'decrypt') && options['hold' + name]) return (...args) => {
      const operation = target[name](...args), record = {name}; cryptoPending.push(record);
      return new Promise((resolve, reject) => { record.release = () => operation.then(resolve, reject); });
    };
    return typeof target[name] === 'function' ? target[name].bind(target) : target[name];
  }});
  const runtime = {window, document, location: {origin}, URL, Uint8Array, ArrayBuffer, DataView, TextEncoder, TextDecoder, AbortController,
    crypto: {subtle, getRandomValues: crypto.webcrypto.getRandomValues.bind(crypto.webcrypto)},
    atob: value => Buffer.from(value, 'base64').toString('binary'), btoa: value => Buffer.from(value, 'binary').toString('base64'),
    setTimeout(callback, delay) { const id = ++timer; timers.set(id, {callback, delay}); return id; }, clearTimeout(id) { timers.delete(id); },
    localStorage: {
      getItem(key) { if (options.storageUnavailable) throw new Error('Storage denied'); return storage.get(key) ?? null; },
      setItem(key, value) { if (options.storageUnavailable || options.storageWriteDenied) throw new Error('Storage full'); storage.set(key, value); storageWrites.push([key, value]); }
    },
    sessionStorage: {getItem() { throw new Error('Controller read session storage'); }, setItem() { throw new Error('Controller persisted key material'); }},
    confirm(message) { confirmations.push(message); return options.confirm !== false; },
    fetch(url, settings) {
      const path = url.replace('/workspace-api/near/', ''), body = settings.body === undefined ? undefined : JSON.parse(settings.body);
      const record = {url, path, settings, body}; requests.push(record);
      return new Promise((resolve, reject) => {
        record.reply = (data, status = 200) => resolve({ok: status < 400, status, json: async () => data});
        record.reject = reject;
        if (!options.ignoreAbort) settings.signal.addEventListener('abort', () => reject(new Error('Request aborted')));
        if (options.hold?.(record)) return;
        if (options.reply?.(record)) return;
        if (path === 'session') record.reply(options.authenticated ? session : {authenticated: false});
        else if (path === 'challenge') record.reply(challenge, 201);
        else if (path === 'verify') {
          const request = {message: challenge.message, recipient: challenge.recipient, nonce: new Uint8Array(Buffer.from(challenge.nonce, 'base64')), callbackUrl: challenge.callbackUrl};
          window.SiyabVault.nep413Digest(request).then(digest => {
            const accepted = body.accountId === account && body.publicKey === publicKey && crypto.verify(null, digest, crypto.createPublicKey(signingKey), Buffer.from(body.signature, 'base64'));
            record.reply(accepted ? session : {message: 'Wallet verification refused.'}, accepted ? 200 : 401);
          }, reject);
        } else if (path === 'vault' && settings.method === 'GET') record.reply(options.vaultData || {revision: server.revision, vault: server.vault, updated_ms: server.revision ? Date.now() : null});
        else if (path === 'vault' && settings.method === 'POST') {
          if (options.saveStatus) { record.reply({message: 'Refresh the wallet session.'}, options.saveStatus); return; }
          if (body.revision !== server.revision) { record.reply({message: 'Another device saved this workspace.'}, 409); return; }
          server.vault = normal(body.vault); server.revision++;
          record.reply({revision: server.revision, updated_ms: Date.now()});
        } else if (path === 'vault/reset') {
          if (options.resetStatus) { record.reply({message: 'Sign in again within five minutes.'}, options.resetStatus); return; }
          assert.deepEqual(body, {confirmation: 'DELETE ' + account}); server.vault = null; server.revision++;
          record.reply({reset: true, revision: server.revision});
        } else if (path === 'logout') record.reply({signed_out: true});
        else reject(new Error('Unexpected request: ' + url));
      });
    }
  };
  if (options.storageGetterDenied) Object.defineProperty(runtime, 'localStorage', {get() { throw new Error('Storage blocked'); }});
  vm.createContext(runtime);
  vm.runInContext(fs.readFileSync('intel/ui/workspace-vault.js', 'utf8'), runtime);
  window.near = {isSender: !options.unsupported && !options.okx, isOkxWallet: Boolean(options.okx), isOKExWallet: Boolean(options.okx),
    getAccountId() { return connectedAccount; },
    async connect(params) {
      assert.equal(options.okx, true, 'Sender must never be asked to connect');
      assert.equal(arguments.length, 1); assert.deepEqual(normal(params), {});
      const record = {params: normal(params)}; connectCalls.push(record);
      if (options.connectError) throw new Error('Wallet connection declined.');
      if (options.holdConnect) return new Promise(resolve => { record.release = () => { connectedAccount = account; resolve({accountId: account}); }; });
      connectedAccount = account;
      return {accountId: account};
    }, async signMessage(request) {
    if (options.okx) assert.equal(connectedAccount, account, 'OKX must connect before signing');
    const index = walletCalls.length, record = {request: {...request, nonce: new Uint8Array(request.nonce)}}; walletCalls.push(record);
    const payload = options.signingRequest ? options.signingRequest(record.request, index) : record.request;
    const digest = await window.SiyabVault.nep413Digest(payload);
    const signed = {accountId: account, publicKey, signature: crypto.sign(null, digest, signingKey).toString('base64'), ...(request.state === undefined ? {} : {state: request.state}), ...options.walletPatch?.(index)};
    if (options.noisyVerification && index === 2) { const bytes = Buffer.from(signed.signature, 'base64'); bytes[0] ^= 1; signed.signature = bytes.toString('base64'); }
    record.signed = signed;
    const response = options.walletResult ? options.walletResult(signed, index) : options.okx ? signed : {response: signed};
    if (options.holdWallet?.(index)) return new Promise(resolve => { record.release = () => resolve(response); });
    return response;
  }};
  vm.runInContext(fs.readFileSync('intel/ui/near-workspace.js', 'utf8'), runtime);
  return {options, window, requests, walletCalls, connectCalls, timers, merges, writes, confirmations, cryptoPending, session, challenge, context, storage, storageWrites,
    node: id => ids['near-' + id], snapshotCalls: () => snapshotCalls, stored: () => server.vault, server,
    start: () => events['document:DOMContentLoaded'](),
    submit() { ids['near-sign-in'].listeners.submit({preventDefault() {}}); },
    click(id) { return ids['near-' + id].disabled ? Promise.resolve(false) : ids['near-' + id].listeners.click(); },
    async reset() { ids['near-reset-form'].listeners.submit({preventDefault() {}}); await until(() => !ids['near-login'].disabled, 'reset idle'); },
    event(name, value = {}) { return events[name](value); },
    idle: () => until(() => !ids['near-login'].disabled, 'controller idle')};
}
async function signedIn(env) {
  await env.start(); env.submit(); await env.idle();
  assert.equal(env.node('sign-in').hidden, true, env.node('status').textContent);
  assert.equal(env.node('unlock').disabled, false, env.node('status').textContent);
}
async function unlocked(env) {
  await signedIn(env); await env.click('unlock');
  assert.equal(env.node('save').disabled, false, env.node('status').textContent);
}
async function encryptedCopy(revision = 1) {
  const env = environment(), vault = await env.window.SiyabVault.unlock({...env.context, signMessage: async request => (await env.window.near.signMessage(request)).response});
  return normal(await vault.encrypt('workspace:' + revision, fixture()));
}
function privateBoundary(env) {
  const content = JSON.stringify(env.requests.map(request => request.body)) + JSON.stringify(env.stored());
  for (const value of [fixture().cases[0].title, fixture().cases[0].notes, fixture().cases[0].id, fixture().watchlist[0]]) assert.ok(!content.includes(value), 'Server received private workspace content');
  for (const call of env.walletCalls.filter(call => call.request.message.startsWith('Siyab encrypted workspace key v1'))) assert.ok(!content.includes(call.signed?.signature), 'Server received an unlock signature');
  for (const [key, value] of env.storageWrites) {
    assert.ok(key.includes(origin) && key.includes(account) && key.includes(publicKey));
    assert.match(value, /^\d+$/);
  }
}
async function main() {
  const scenario = process.argv[2];
  if (scenario === 'emit-envelope') { console.log(JSON.stringify(await encryptedCopy())); return; }
  if (scenario === 'sign-in-privacy' || scenario === 'okx-sign-in') {
    const env = environment({okx: scenario === 'okx-sign-in'}); await signedIn(env);
    assert.deepEqual(env.requests.map(request => [request.path, request.settings.method]), [['session', 'GET'], ['challenge', 'POST'], ['verify', 'POST'], ['vault', 'GET']]);
    assert.equal(env.walletCalls.length, 1); assert.equal(env.requests[2].body.signature, env.walletCalls[0].signed.signature);
    assert.equal(env.connectCalls.length, 0); assert.equal(env.walletCalls[0].request.state, env.challenge.challenge_id);
    assert.equal(env.walletCalls[0].request.callbackUrl, origin + '/workspace.html');
    assert.ok(env.walletCalls[0].request.nonce instanceof Uint8Array); assert.equal(env.walletCalls[0].request.nonce.length, 32);
    assert.equal(env.snapshotCalls(), 0); assert.equal(env.merges.length, 0); assert.equal(env.writes.length, 0);
    assert.equal(env.node('save').disabled, true); privateBoundary(env);
  } else if (scenario === 'okx-unlock') {
    const env = environment({okx: true, vault: await encryptedCopy()}); await unlocked(env);
    assert.equal(env.walletCalls.length, 3); assert.equal(env.connectCalls.length, 0);
    assert.equal(env.walletCalls[1].signed.signature, env.walletCalls[2].signed.signature);
    for (const call of env.walletCalls) assert.equal(call.request.callbackUrl, origin + '/workspace.html');
    assert.equal(env.requests.length, 4); assert.equal(env.snapshotCalls(), 0); assert.equal(env.merges.length, 0);
    await env.click('restore'); assert.deepEqual(env.merges, [fixture()]);
    await env.click('save'); assert.equal(env.stored().vault_id, 'workspace:2'); privateBoundary(env);
    const sender = environment({server: env.server}); await unlocked(sender); await sender.click('restore');
    assert.deepEqual(sender.merges, [fixture()]); assert.equal(sender.connectCalls.length, 0); privateBoundary(sender);
  } else if (scenario === 'okx-connect') {
    for (const authenticated of [false, true]) {
      const env = environment({okx: true, disconnected: true, authenticated});
      if (authenticated) await env.start(); else await signedIn(env);
      await env.click('unlock');
      assert.equal(env.node('save').disabled, false, env.node('status').textContent);
      assert.deepEqual(env.connectCalls.map(call => call.params), [{}]);
      assert.equal(env.window.near.getAccountId(), account); assert.equal(env.walletCalls.length, authenticated ? 2 : 3);
      assert.equal(env.requests.filter(request => request.path === 'verify').length, authenticated ? 0 : 1); privateBoundary(env);
    }
    const declined = environment({okx: true, disconnected: true, connectError: true}); await declined.start(); declined.submit(); await declined.idle();
    assert.equal(declined.walletCalls.length, 0); assert.equal(declined.requests.filter(request => request.path === 'verify').length, 0);
    assert.match(declined.node('status').textContent, /connection declined/); assert.equal(declined.node('save').disabled, true); privateBoundary(declined);
  } else if (scenario === 'sender-shape') {
    const env = environment({disconnected: true}); await unlocked(env);
    assert.equal(env.walletCalls.length, 3); assert.equal(env.connectCalls.length, 0); privateBoundary(env);
    const direct = environment({walletResult: signed => signed}); await direct.start(); direct.submit(); await direct.idle();
    assert.equal(direct.requests.filter(request => request.path === 'verify').length, 0);
    assert.match(direct.node('status').textContent, /signed NEP-413 message/); assert.equal(direct.node('save').disabled, true); privateBoundary(direct);
  } else if (scenario === 'unknown-provider') {
    for (const provider of [undefined, {}, {isOKExWallet: true}, {isSender: true, signMessage: null}, {isOkxWallet: true, signMessage: null}]) {
      const env = environment({unsupported: true});
      env.window.near = provider && {...env.window.near, ...provider};
      await env.start(); env.submit(); await env.idle();
      assert.equal(env.requests.length, 1); assert.equal(env.walletCalls.length, 0); assert.equal(env.connectCalls.length, 0);
      assert.match(env.node('status').textContent, /Sender.*OKX Wallet/); assert.equal(env.node('save').disabled, true); privateBoundary(env);
    }
  } else if (scenario === 'okx-validation') {
    const bad = [
      signed => ({...signed, accountId: 'bob.near'}), signed => ({...signed, publicKey: null}), signed => ({...signed, signature: null}),
      signed => ({...signed, state: 'wrong-state'}), () => ({error: 'declined'}), () => ({}), () => null, () => [], signed => ({response: signed})
    ];
    for (const walletResult of bad) {
      const env = environment({okx: true, walletResult}); await env.start(); env.submit(); await env.idle();
      assert.equal(env.requests.filter(request => request.path === 'verify').length, 0);
      assert.equal(env.node('sign-in').hidden, false); assert.equal(env.node('save').disabled, true); privateBoundary(env);
      const unlock = environment({okx: true, authenticated: true, walletResult}); await unlock.start(); await unlock.click('unlock');
      assert.equal(unlock.node('save').disabled, true); assert.equal(unlock.requests.length, 2); privateBoundary(unlock);
    }
  } else if (scenario === 'double-submit') {
    for (const okx of [false, true]) for (const stage of ['challenge', ...(okx ? ['connect'] : []), 'wallet', 'verify']) {
      const env = environment({okx, disconnected: stage === 'connect', holdConnect: stage === 'connect',
        hold: request => request.path === stage, holdWallet: index => stage === 'wallet' && index === 0});
      await env.start(); env.submit(); env.submit();
      await until(() => stage === 'connect' ? env.connectCalls[0]?.release : stage === 'wallet' ? env.walletCalls[0]?.release : env.requests.at(-1)?.path === stage, 'pending ' + stage);
      assert.equal(env.node('login').disabled, true); assert.equal(env.node('account').disabled, true);
      const count = env.requests.length; env.submit(); await tick(); await tick(); assert.equal(env.requests.length, count);
      assert.equal(env.requests.filter(request => request.path === 'challenge').length, 1);
      if (stage === 'connect') env.connectCalls[0].release();
      else if (stage === 'wallet') env.walletCalls[0].release();
      else env.requests.at(-1).reply(stage === 'challenge' ? env.challenge : env.session);
      await env.idle();
      assert.equal(env.node('sign-in').hidden, true, env.node('status').textContent);
      assert.equal(env.walletCalls.length, 1); assert.equal(env.connectCalls.length, stage === 'connect' ? 1 : 0);
      assert.equal(env.requests.filter(request => request.path === 'challenge').length, 1);
      assert.equal(env.requests.filter(request => request.path === 'verify').length, 1); privateBoundary(env);
    }
  } else if (scenario === 'double-unlock') {
    for (const okx of [false, true]) {
      const env = environment({okx, holdWallet: index => index > 0}); await signedIn(env);
      const unlocking = env.node('unlock').listeners.click();
      for (const index of [1, 2]) {
        await until(() => env.walletCalls[index]?.release, 'unlock approval ' + index);
        assert.equal(env.node('unlock').disabled, true); assert.equal(env.node('login').disabled, true);
        env.node('unlock').listeners.click(); env.submit(); await tick();
        assert.equal(env.walletCalls.length, index + 1); assert.equal(env.requests.length, 4);
        env.walletCalls[index].release();
      }
      await unlocking; assert.equal(env.node('save').disabled, false); assert.equal(env.walletCalls.length, 3); privateBoundary(env);
    }
  } else if (scenario === 'save-privacy') {
    const env = environment(); await unlocked(env);
    assert.equal(env.walletCalls.length, 3); assert.equal(env.walletCalls[1].signed.signature, env.walletCalls[2].signed.signature);
    assert.equal(env.requests.length, 4); assert.equal(env.snapshotCalls(), 0); assert.equal(env.writes.length, 0);
    await env.click('save');
    assert.equal(env.snapshotCalls(), 1); assert.equal(env.confirmations.length, 1); assert.equal(env.requests.length, 5);
    assert.deepEqual(Object.keys(env.requests[4].body).sort(), ['revision', 'vault']);
    assert.equal(env.requests[4].body.revision, 0); assert.equal(env.requests[4].body.vault.vault_id, 'workspace:1');
    assert.equal(env.requests[4].settings.headers['X-CSRF-Token'], env.session.csrf_token);
    assert.equal(env.writes.length, 0); privateBoundary(env);
  } else if (scenario === 'restore-explicit') {
    const env = environment({vault: await encryptedCopy(), confirm: false}); await unlocked(env);
    assert.equal(env.merges.length, 0); assert.equal(env.confirmations.length, 0); assert.equal(env.snapshotCalls(), 0);
    await env.click('restore'); assert.equal(env.merges.length, 0); assert.equal(env.writes.length, 0);
    env.options.confirm = true; await env.click('restore');
    assert.deepEqual(env.merges, [fixture()]); assert.equal(env.writes.length, 1); assert.equal(env.writes[0].destination, 'browser');
    assert.equal(env.requests.length, 4); assert.equal(env.walletCalls.length, 3); privateBoundary(env);
  } else if (scenario === 'save-cancelled') {
    const env = environment({confirm: false}); await unlocked(env); await env.click('save');
    assert.equal(env.requests.length, 4); assert.equal(env.stored(), null); assert.equal(env.writes.length, 0);
    assert.equal(env.merges.length, 0); assert.equal(env.node('save').disabled, false); privateBoundary(env);
  } else if (scenario === 'challenge-validation') {
    for (const challenge of [{recipient: 'https://other.example'}, {callbackUrl: origin + '/changed'}, {account_id: 'bob.near'}, {state: 'wrong'}, {expires_at: Date.now() - 1}, {nonce: Buffer.alloc(31).toString('base64')}, {nonce: Buffer.alloc(32).toString('base64') + '\n'}]) {
      const env = environment({challenge}); await env.start(); env.submit(); await env.idle();
      assert.equal(env.walletCalls.length, 0); assert.equal(env.requests.filter(request => request.path === 'verify').length, 0);
      assert.equal(env.node('save').disabled, true); assert.equal(env.snapshotCalls(), 0); privateBoundary(env);
    }
  } else if (scenario === 'auth-domain-substitution') {
    const env = environment(), unlock = await env.window.SiyabVault.unlockRequest(env.context);
    Object.assign(env.challenge, {message: unlock.message, nonce: Buffer.from(unlock.nonce).toString('base64'), recipient: unlock.recipient, callbackUrl: unlock.callbackUrl});
    await env.start(); env.submit(); await env.idle();
    assert.equal(env.walletCalls.length, 0, 'An authentication challenge must never request the vault unlock signature');
    assert.equal(env.requests.filter(request => request.path === 'verify').length, 0);
    assert.equal(env.node('save').disabled, true); assert.equal(env.snapshotCalls(), 0); assert.equal(env.writes.length, 0); privateBoundary(env);
  } else if (scenario === 'auth-message-validation') {
    const changes = [
      challenge => { challenge.message = challenge.message.replace('Sign in to Siyab.', 'Unlock the encrypted workspace.'); },
      challenge => { challenge.message = challenge.message.replace('Account: ' + account, 'Account: bob.near'); },
      challenge => { challenge.message = challenge.message.replace('Origin: ' + origin, 'Origin: https://other.example'); },
      challenge => { challenge.message = challenge.message.replace('Challenge: ' + challenge.challenge_id, 'Challenge: ' + 'd'.repeat(43)); },
      challenge => { challenge.message = challenge.message.replace('Issued at: ' + (challenge.expires_at - 300000), 'Issued at: ' + (challenge.expires_at - 300001)); },
      challenge => { challenge.message = challenge.message.replace('Expires at: ' + challenge.expires_at, 'Expires at: ' + (challenge.expires_at - 1)); },
      challenge => { challenge.message += '\nCallback: ' + challenge.callbackUrl; },
      challenge => { challenge.message += '\n'; },
      challenge => { challenge.message = null; },
      challenge => { delete challenge.message; },
      challenge => { challenge.expires_at = Date.now() + 360000; challenge.message = authMessage(challenge); },
      challenge => { challenge.callbackUrl += '?unexpected=1'; },
      ...['c'.repeat(42), 'c'.repeat(44), 'c'.repeat(42) + '+', 'c'.repeat(42) + ' '].map(identifier => challenge => {
        challenge.challenge_id = identifier; challenge.state = identifier; challenge.message = authMessage(challenge);
      })
    ];
    for (const change of changes) {
      const env = environment(); change(env.challenge); await env.start(); env.submit(); await env.idle();
      assert.equal(env.walletCalls.length, 0, 'Only the exact bounded Siyab authentication message may reach the wallet');
      assert.equal(env.requests.filter(request => request.path === 'verify').length, 0);
      assert.equal(env.node('save').disabled, true); assert.equal(env.snapshotCalls(), 0); privateBoundary(env);
    }
  } else if (scenario === 'wallet-validation') {
    for (const options of [{unsupported: true}, {walletPatch: () => ({accountId: 'bob.near'})}, {walletPatch: () => ({state: 'wrong-state'})}, {walletResult: () => ({error: 'declined'})}, {walletResult: () => ({})}]) {
      const env = environment(options); await env.start(); env.submit(); await env.idle();
      assert.equal(env.requests.filter(request => request.path === 'verify').length, 0);
      assert.equal(env.node('save').disabled, true); assert.equal(env.snapshotCalls(), 0); assert.equal(env.writes.length, 0); privateBoundary(env);
    }
  } else if (scenario === 'unlock-binding') {
    for (const change of [request => ({...request, callbackUrl: origin + '/changed'}), request => ({...request, recipient: 'https://other.example'}), request => ({...request, nonce: new Uint8Array(32)}), request => ({...request, message: request.message + ' changed'})]) {
      const env = environment({signingRequest: (request, index) => index ? change(request) : request}); await signedIn(env); await env.click('unlock');
      assert.equal(env.node('save').disabled, true); assert.match(env.node('status').textContent, /signature does not match/);
      assert.equal(env.requests.length, 4); assert.equal(env.snapshotCalls(), 0); privateBoundary(env);
    }
    const wrong = environment({walletPatch: index => index ? {publicKey: 'ed25519:' + '1'.repeat(32)} : {}}); await signedIn(wrong); await wrong.click('unlock');
    assert.equal(wrong.node('save').disabled, true); assert.match(wrong.node('status').textContent, /different account or signing key/);
  } else if (scenario === 'unlock-nondeterministic') {
    const env = environment({noisyVerification: true}); await signedIn(env); await env.click('unlock');
    assert.equal(env.walletCalls.length, 3); assert.equal(env.node('save').disabled, true);
    assert.match(env.node('status').textContent, /different signatures/); assert.equal(env.requests.length, 4); privateBoundary(env);
  } else if (scenario === 'navigation-challenge') {
    const env = environment({hold: request => request.path === 'challenge', ignoreAbort: true}); await env.start(); env.submit();
    const pending = env.requests.at(-1); assert.equal(pending.path, 'challenge'); env.event('pagehide'); pending.reply(env.challenge, 201); await tick(); await tick();
    assert.equal(env.walletCalls.length, 0); assert.equal(env.requests.length, 2); assert.equal(env.node('save').disabled, true); privateBoundary(env);
  } else if (scenario === 'navigation-wallet') {
    const env = environment({holdWallet: index => index === 0}); await env.start(); env.submit();
    await until(() => env.walletCalls[0]?.release, 'sign-in wallet reply'); env.event('pagehide'); env.walletCalls[0].release(); await tick(); await tick();
    assert.equal(env.requests.length, 2); assert.equal(env.node('sign-in').hidden, false); assert.equal(env.node('save').disabled, true); privateBoundary(env);
  } else if (scenario === 'navigation-verify') {
    const env = environment({hold: request => request.path === 'verify', ignoreAbort: true}); await env.start(); env.submit();
    await until(() => env.requests.at(-1)?.path === 'verify', 'sign-in verification'); const pending = env.requests.at(-1);
    env.event('pagehide'); pending.reply(env.session); await tick(); await tick();
    assert.equal(env.requests.length, 3); assert.equal(env.node('sign-in').hidden, false); assert.equal(env.node('save').disabled, true); privateBoundary(env);
  } else if (scenario === 'navigation-unlock') {
    const env = environment({holdWallet: index => index === 1}); await signedIn(env); const unlocking = env.click('unlock');
    await until(() => env.walletCalls[1]?.release, 'unlock wallet reply'); env.event('pagehide'); env.event('pageshow', {persisted: true}); env.walletCalls[1].release(); await unlocking;
    assert.equal(env.node('save').disabled, true); assert.equal(env.node('restore').disabled, true); assert.equal(env.requests.length, 4); privateBoundary(env);
  } else if (scenario === 'navigation-decrypt') {
    const env = environment({vault: await encryptedCopy(), holddecrypt: true}); await signedIn(env); const unlocking = env.click('unlock');
    await until(() => env.cryptoPending[0]?.release, 'unlock decrypt'); env.event('pagehide'); env.event('pageshow', {persisted: true}); env.cryptoPending[0].release(); await unlocking;
    assert.equal(env.node('save').disabled, true); assert.equal(env.node('restore').disabled, true); assert.equal(env.requests.length, 4); privateBoundary(env);
  } else if (scenario === 'navigation-save') {
    const env = environment({holdencrypt: true}); await unlocked(env); const saving = env.click('save');
    await until(() => env.cryptoPending[0]?.release, 'save encrypt'); env.event('pagehide'); env.cryptoPending[0].release(); await saving;
    assert.equal(env.requests.length, 4); assert.equal(env.stored(), null); assert.equal(env.node('save').disabled, true); privateBoundary(env);
  } else if (scenario === 'unreadable-record') {
    const encrypted = await encryptedCopy();
    for (const vaultData of [{revision: 1}, ...[false, 0, '', [], {}].map(vault => ({revision: 1, vault})),
      {revision: 1, vault: {...encrypted, public_key: 'ed25519:' + '1'.repeat(32)}}, {revision: 2, vault: encrypted}, {revision: 0, vault: encrypted}, {revision: 1, vault: {...encrypted, version: 2}}]) {
      const env = environment({vault: encrypted, vaultData}); await env.start(); env.submit(); await env.idle();
      assert.equal(env.node('unlock').disabled, true); assert.equal(env.node('save').disabled, true); assert.equal(env.snapshotCalls(), 0); assert.deepEqual(env.stored(), encrypted); privateBoundary(env);
    }
    const changed = Buffer.from(encrypted.ciphertext, 'base64'); changed[0] ^= 1;
    const tampered = {...encrypted, ciphertext: changed.toString('base64')}, env = environment({vault: tampered}); await signedIn(env); await env.click('unlock');
    assert.equal(env.node('save').disabled, true); assert.match(env.node('status').textContent, /could not open the saved copy/);
    assert.deepEqual(env.stored(), tampered); assert.equal(env.snapshotCalls(), 0); privateBoundary(env);
  } else if (scenario === 'http-session-conflict') {
    for (const saveStatus of [401, 409]) {
      const env = environment({saveStatus}); await unlocked(env); await env.click('save');
      assert.equal(env.node('save').disabled, true); assert.equal(env.node('unlock').disabled, true); assert.equal(env.node('sign-in').hidden, saveStatus === 409);
      assert.equal(env.node('logout').disabled, saveStatus === 401);
      assert.equal(env.node('reset-confirmation').disabled, saveStatus === 401);
      assert.equal(env.requests.filter(request => request.path === 'vault' && request.settings.method === 'POST').length, 1);
      assert.equal(env.stored(), null); assert.equal(env.writes.length, 0); privateBoundary(env);
    }
  } else if (scenario === 'rollback-after-reload') {
    const vault = await encryptedCopy(2), first = environment({vault, vaultRevision: 2}); await signedIn(first);
    assert.equal(first.storage.size, 1); assert.equal([...first.storage.values()][0], '2');
    for (const options of [{vault: await encryptedCopy()}, {}]) {
      const older = environment({...options, storage: first.storage}); await older.start(); older.submit(); await older.idle();
      assert.match(older.node('status').textContent, /older revision/);
      assert.equal(older.node('sign-in').hidden, true); assert.equal(older.node('unlock').disabled, true);
      assert.equal(older.node('save').disabled, true); assert.equal(older.snapshotCalls(), 0); privateBoundary(older);
      assert.equal([...first.storage.values()][0], '2');
    }
  } else if (scenario === 'rollback-after-save') {
    const first = environment(); await unlocked(first); await first.click('save');
    assert.equal([...first.storage.values()][0], '1');
    const older = environment({storage: first.storage}); await older.start(); older.submit(); await older.idle();
    assert.match(older.node('status').textContent, /older revision/); assert.equal(older.node('unlock').disabled, true); privateBoundary(older);
  } else if (scenario === 'rollback-identity-scope') {
    const first = environment({vault: await encryptedCopy()}); await signedIn(first);
    const key = [...first.storage.keys()][0]; assert.ok(key.includes(origin) && key.includes(account) && key.includes(publicKey));
    for (const [oldValue, newValue] of [[origin, 'https://other.example'], [account, 'bob.near'], [publicKey, 'ed25519:' + '1'.repeat(32)]]) {
      const storage = new Map([[key.replace(oldValue, newValue), '999']]);
      const fresh = environment({storage}); await signedIn(fresh); assert.equal(fresh.node('unlock').disabled, false);
      assert.equal(storage.get(key.replace(oldValue, newValue)), '999'); privateBoundary(fresh);
    }
  } else if (scenario === 'rollback-storage-unavailable') {
    for (const options of [{storageUnavailable: true}, {storageGetterDenied: true}, {storageWriteDenied: true}]) {
      const env = environment(options); await unlocked(env); await env.click('save');
      assert.equal(env.stored().vault_id, 'workspace:1'); assert.match(env.node('status').textContent, /storage.*unavailable/i);
      await env.click('logout'); env.options.vaultData = {revision: 0, vault: null};
      env.submit(); await env.idle(); assert.match(env.node('status').textContent, /older revision/); privateBoundary(env);
    }
  } else if (scenario === 'reset-explicit') {
    const env = environment({vault: await encryptedCopy()}); await unlocked(env);
    for (const value of ['', 'DELETE bob.near', 'DELETE alice.near ']) {
      env.node('reset-confirmation').value = value; await env.reset();
      assert.equal(env.requests.filter(request => request.path === 'vault/reset').length, 0);
      assert.ok(env.stored()); assert.equal(env.node('sign-in').hidden, true);
    }
    env.node('reset-confirmation').value = 'DELETE ' + account; await env.reset();
    const request = env.requests.at(-1); assert.equal(request.path, 'vault/reset');
    assert.deepEqual(request.body, {confirmation: 'DELETE ' + account}); assert.equal(request.settings.headers['X-CSRF-Token'], env.session.csrf_token);
    assert.equal(env.stored(), null); assert.equal(env.node('save').disabled, true); assert.equal(env.node('unlock').disabled, false);
    assert.equal(env.node('sign-in').hidden, true); assert.equal(env.node('reset-confirmation').value, '');
    assert.equal(env.snapshotCalls(), 0); assert.equal(env.merges.length, 0); assert.equal(env.writes.length, 0); privateBoundary(env);
  } else if (scenario === 'reset-changed-key') {
    const encrypted = await encryptedCopy(), changed = {...encrypted, public_key: 'ed25519:' + '1'.repeat(32)};
    const env = environment({vault: changed}); await env.start(); env.submit(); await env.idle();
    assert.equal(env.node('unlock').disabled, true); assert.equal(env.node('sign-in').hidden, true);
    assert.equal(env.node('reset-confirmation').disabled, false);
    env.node('reset-confirmation').value = 'DELETE ' + account; await env.reset();
    assert.equal(env.node('unlock').disabled, false); assert.equal(env.stored(), null);
    await env.click('unlock'); await env.click('save'); assert.equal(env.stored().public_key, publicKey); privateBoundary(env);
    const first = environment({vault: await encryptedCopy(2), vaultRevision: 2}); await signedIn(first);
    first.node('reset-confirmation').value = 'DELETE ' + account; await first.reset();
    const next = environment({storage: first.storage, server: first.server}); await signedIn(next); assert.equal(next.node('unlock').disabled, false); privateBoundary(next);
  } else if (scenario === 'reset-two-devices') {
    const server = {vault: await encryptedCopy(2), revision: 2};
    const first = environment({server}), second = environment({server});
    await unlocked(first); await signedIn(second);
    assert.notEqual(first.storage, second.storage);
    second.node('reset-confirmation').value = 'DELETE ' + account; await second.reset();
    assert.equal(server.revision, 3); assert.equal(server.vault, null);
    await first.click('save'); assert.match(first.node('status').textContent, /Another device/);
    await first.click('logout'); first.submit(); await first.idle();
    assert.equal(first.node('unlock').disabled, false);
    assert.equal(first.node('restore').disabled, true);
    assert.match(first.node('status').textContent, /reset elsewhere/);
    assert.equal([...first.storage.values()][0], '3');
    await second.click('unlock'); await second.click('save');
    assert.equal(server.revision, 4); assert.equal(server.vault.vault_id, 'workspace:4');
    await first.click('logout'); first.submit(); await first.idle();
    assert.equal(first.node('unlock').disabled, false); await first.click('unlock');
    assert.equal(first.node('restore').disabled, false); assert.equal([...first.storage.values()][0], '4');
    assert.equal(first.merges.length, 0); privateBoundary(first); privateBoundary(second);
  } else if (scenario === 'reset-same-device') {
    const first = environment({vault: await encryptedCopy(2), vaultRevision: 2}); await signedIn(first);
    first.node('reset-confirmation').value = 'DELETE ' + account; await first.reset();
    assert.match(first.node('status').textContent, /encrypted data is lost/);
    const resumed = environment({authenticated: true, storage: first.storage, server: first.server}); await resumed.start();
    assert.match(resumed.node('status').textContent, /reset on this device/);
    assert.equal(resumed.node('unlock').disabled, false); assert.equal(resumed.node('save').disabled, true);
    await resumed.click('logout'); resumed.submit(); await resumed.idle();
    assert.match(resumed.node('status').textContent, /reset on this device/);
    resumed.node('reset-confirmation').value = 'DELETE ' + account; await resumed.reset();
    const again = environment({authenticated: true, storage: first.storage, server: first.server}); await again.start();
    assert.equal(again.server.revision, 4); assert.match(again.node('status').textContent, /reset on this device/);
    assert.equal(again.snapshotCalls(), 0); assert.equal(again.writes.length, 0);
    privateBoundary(first); privateBoundary(resumed); privateBoundary(again);
  } else if (scenario === 'reset-identity-scope') {
    const first = environment({vault: await encryptedCopy()}); await signedIn(first);
    first.node('reset-confirmation').value = 'DELETE ' + account; await first.reset();
    const key = [...first.storage.keys()].find(key => key.startsWith('siyab.near-vault-reset.v1:'));
    assert.ok(key && key.includes(origin) && key.includes(account) && key.includes(publicKey));
    for (const [oldValue, newValue] of [[origin, 'https://other.example'], [account, 'bob.near'], [publicKey, 'ed25519:' + '1'.repeat(32)]]) {
      const storage = new Map(first.storage); storage.delete(key); storage.set(key.replace(oldValue, newValue), '2');
      const fresh = environment({storage, server: first.server}); await signedIn(fresh);
      assert.match(fresh.node('status').textContent, /reset elsewhere/); privateBoundary(fresh);
    }
  } else if (scenario === 'reset-later-elsewhere') {
    const first = environment({vault: await encryptedCopy()}); await signedIn(first);
    first.node('reset-confirmation').value = 'DELETE ' + account; await first.reset();
    const other = environment({server: first.server}); await signedIn(other);
    other.node('reset-confirmation').value = 'DELETE ' + account; await other.reset();
    const resumed = environment({authenticated: true, storage: first.storage, server: first.server}); await resumed.start();
    assert.equal(resumed.server.revision, 3); assert.match(resumed.node('status').textContent, /reset elsewhere/);
    assert.equal(resumed.node('unlock').disabled, false); privateBoundary(first); privateBoundary(other); privateBoundary(resumed);
  } else if (scenario === 'reset-storage-unavailable') {
    for (const options of [{storageUnavailable: true}, {storageGetterDenied: true}, {storageWriteDenied: true}]) {
      const first = environment({...options, vault: await encryptedCopy()}); await signedIn(first);
      first.node('reset-confirmation').value = 'DELETE ' + account; await first.reset();
      assert.match(first.node('status').textContent, /encrypted data is lost/);
      assert.match(first.node('status').textContent, /storage.*unavailable/i);
      await first.click('logout'); first.submit(); await first.idle();
      assert.match(first.node('status').textContent, /reset on this device/);
      const resumed = environment({...options, authenticated: true, storage: first.storage, server: first.server}); await resumed.start();
      assert.match(resumed.node('status').textContent, /encrypted vault was reset\./);
      assert.doesNotMatch(resumed.node('status').textContent, /elsewhere|on this device/);
      assert.match(resumed.node('status').textContent, /storage.*unavailable/i);
      assert.equal(resumed.node('unlock').disabled, false); assert.equal(resumed.node('save').disabled, true);
      assert.equal(resumed.snapshotCalls(), 0); assert.equal(resumed.writes.length, 0); privateBoundary(first); privateBoundary(resumed);
    }
  } else if (scenario === 'reset-tombstone-revisions') {
    const first = environment({vault: await encryptedCopy(3), vaultRevision: 3}); await signedIn(first);
    for (const revision of [2, 3, 4]) {
      const env = environment({vaultRevision: revision, storage: new Map(first.storage)});
      await env.start(); env.submit(); await env.idle();
      assert.equal(env.node('unlock').disabled, revision < 3);
      assert.match(env.node('status').textContent, revision < 3 ? /older revision/ : /reset elsewhere/);
      assert.equal([...env.storage.values()][0], String(Math.max(3, revision)));
      assert.equal(env.snapshotCalls(), 0); assert.equal(env.writes.length, 0); privateBoundary(env);
    }
    const resumed = environment({authenticated: true, vaultRevision: 3, storage: new Map(first.storage)});
    await resumed.start(); assert.match(resumed.node('status').textContent, /reset elsewhere/);
    assert.equal(resumed.node('unlock').disabled, false); privateBoundary(resumed);
  } else if (scenario === 'reset-storage-write-denied') {
    const vault = await encryptedCopy(2), first = environment({vault, vaultRevision: 2}); await signedIn(first);
    const env = environment({vault, vaultRevision: 2, storage: first.storage, storageWriteDenied: true}); await signedIn(env);
    env.node('reset-confirmation').value = 'DELETE ' + account; await env.reset();
    await env.click('unlock'); await env.click('save');
    assert.equal(env.stored().vault_id, 'workspace:4'); assert.equal(env.node('save').disabled, false);
    assert.match(env.node('status').textContent, /storage.*unavailable/i);
    await env.click('save'); assert.equal(env.stored().vault_id, 'workspace:5'); privateBoundary(env);
  } else if (scenario === 'reset-refused') {
    for (const resetStatus of [401, 403, 409, 503]) {
      const vault = await encryptedCopy(), env = environment({vault, resetStatus}); await unlocked(env);
      const history = [...env.storage]; env.node('reset-confirmation').value = 'DELETE ' + account; await env.reset();
      assert.deepEqual(env.stored(), vault); assert.deepEqual([...env.storage], history);
      assert.equal(env.node('sign-in').hidden, resetStatus !== 401); assert.equal(env.writes.length, 0); privateBoundary(env);
    }
  } else if (scenario === 'navigation-reset') {
    const env = environment({vault: await encryptedCopy(), hold: request => request.path === 'vault/reset', ignoreAbort: true}); await unlocked(env);
    const history = [...env.storage]; env.node('reset-confirmation').value = 'DELETE ' + account;
    const resetting = env.reset(); await until(() => env.requests.at(-1)?.path === 'vault/reset', 'reset request');
    env.event('pagehide'); env.requests.at(-1).reply({reset: true, revision: 0}); await resetting; await tick();
    assert.equal(env.node('save').disabled, true); assert.deepEqual([...env.storage], history); assert.equal(env.writes.length, 0); privateBoundary(env);
  } else if (scenario === 'eth-implicit') {
    const env = environment(); await env.start(); env.node('account').value = '0x' + 'a'.repeat(40); env.submit(); await env.idle();
    assert.equal(env.walletCalls.length, 0); assert.equal(env.requests.length, 1); assert.match(env.node('status').textContent, /0x/); privateBoundary(env);
  } else if (scenario === 'timeout-after-login') {
    const env = environment({hold: request => request.path === 'vault'}); await env.start(); env.submit();
    await until(() => env.requests.at(-1)?.path === 'vault', 'initial vault read');
    for (const record of env.timers.values()) record.callback(); await env.idle();
    assert.match(env.node('status').textContent, /timed out/); assert.equal(env.node('unlock').disabled, true); assert.equal(env.node('save').disabled, true);
    assert.equal(env.snapshotCalls(), 0); assert.equal(env.writes.length, 0); privateBoundary(env);
  } else if (scenario === 'error-after-login') {
    for (const failure of ['http', 'shape', 'network']) {
      const env = environment({reply(request) {
        if (request.path !== 'vault') return false;
        if (failure === 'http') request.reply({message: 'Vault temporarily unavailable.'}, 503);
        else if (failure === 'shape') request.reply({revision: -1, vault: null});
        else request.reject(new Error('Network unavailable'));
        return true;
      }});
      await env.start(); env.submit(); await env.idle();
      assert.equal(env.node('unlock').disabled, true); assert.equal(env.node('save').disabled, true);
      assert.equal(env.snapshotCalls(), 0); assert.equal(env.walletCalls.length, 1); assert.equal(env.writes.length, 0); privateBoundary(env);
    }
  } else if (scenario === 'logout-and-bfcache') {
    const env = environment(); await unlocked(env); env.event('pagehide'); env.event('pageshow', {persisted: true});
    assert.equal(env.node('save').disabled, true); assert.equal(env.node('restore').disabled, true); assert.equal(env.node('unlock').disabled, false);
    await env.click('unlock'); assert.equal(env.node('save').disabled, false); await env.click('logout');
    assert.equal(env.node('save').disabled, true); assert.equal(env.node('sign-in').hidden, false); assert.equal(env.writes.length, 0);
    assert.equal(env.requests.at(-1).path, 'logout'); privateBoundary(env);
  } else throw new Error('Unknown scenario: ' + scenario);
}
let finished = false;
process.on('beforeExit', () => {
  if (!finished) {
    console.error('Scenario did not settle: ' + process.argv[2]);
    process.exitCode = 1;
  }
});
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => { finished = true; });
