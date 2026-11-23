const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');

const alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
function base58(bytes) {
  let number = BigInt('0x' + bytes.toString('hex')), result = '';
  while (number) { result = alphabet[Number(number % 58n)] + result; number /= 58n; }
  for (const byte of bytes) { if (byte !== 0) break; result = '1' + result; }
  return result;
}
function signingKey(offset = 1) {
  const seed = Buffer.from(Array.from({length: 32}, (_, index) => index + offset));
  const key = crypto.createPrivateKey({key: Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'), seed]), format: 'der', type: 'pkcs8'});
  const raw = crypto.createPublicKey(key).export({format: 'der', type: 'spki'}).subarray(-32);
  return {key, publicKey: 'ed25519:' + base58(raw)};
}
const signing = signingKey();
const identity = {origin: 'https://siyab.xyz', accountId: 'alice.near', publicKey: signing.publicKey};
function digest(request) {
  const string = value => { const body = Buffer.from(value, 'utf8'), length = Buffer.alloc(4); length.writeUInt32LE(body.length); return Buffer.concat([length, body]); };
  const body = Buffer.concat([Buffer.from([157, 1, 0, 128]), string(request.message), Buffer.from(request.nonce), string(request.recipient),
    request.callbackUrl === undefined ? Buffer.from([0]) : Buffer.concat([Buffer.from([1]), string(request.callbackUrl)])]);
  return crypto.createHash('sha256').update(body).digest();
}
function environment(options = {}) {
  const calls = [], imports = [];
  const subtle = new Proxy(crypto.webcrypto.subtle, {get(target, name) {
    if (name === 'importKey') return (...args) => { imports.push({format: args[0], extractable: args[3], usages: [...args[4]]}); return target.importKey(...args); };
    if (name === 'verify' && options.verify) return options.verify;
    return typeof target[name] === 'function' ? target[name].bind(target) : target[name];
  }});
  const window = {};
  const source = fs.readFileSync('intel/ui/workspace-vault.js', 'utf8');
  const context = {window, Uint8Array, ArrayBuffer, DataView, TextEncoder, TextDecoder, URL,
    location: {origin: options.origin || identity.origin},
    crypto: options.unavailable ? {} : {subtle, getRandomValues: crypto.webcrypto.getRandomValues.bind(crypto.webcrypto)},
    atob: value => Buffer.from(value, 'base64').toString('binary'), btoa: value => Buffer.from(value, 'binary').toString('base64'),
    fetch() { throw new Error('Vault accessed the network'); }, localStorage: {getItem() { throw new Error('Vault accessed browser storage'); }, setItem() { throw new Error('Vault persisted key material'); }},
    sessionStorage: {getItem() { throw new Error('Vault accessed session storage'); }, setItem() { throw new Error('Vault persisted key material'); }}};
  vm.runInNewContext(source, context);
  const signMessage = async request => {
    calls.push({message: request.message, recipient: request.recipient, nonce: Buffer.from(request.nonce), callbackUrl: request.callbackUrl});
    return {accountId: identity.accountId, publicKey: signing.publicKey, signature: crypto.sign(null, digest(request), signing.key).toString('base64')};
  };
  return {api: window.SiyabVault, signMessage, calls, imports};
}
const plain = () => ({version: 1, cases: [{id: 'case-private', title: 'Private investigation', notes: 'Only this browser knows π <svg>', url: '/investigate.html?account=target.near', created_at: '2026-11-21T00:00:00Z'}], watchlist: ['watched.near'], alerts: false, notifications: []});
const normal = value => JSON.parse(JSON.stringify(value));
const mutate64 = value => { const bytes = Buffer.from(value, 'base64'); bytes[0] ^= 1; return bytes.toString('base64'); };
function nativeCipher(signature, context, vaultId, value, bytes = undefined) {
  const salt = crypto.createHash('sha256').update('siyab.workspace.vault.hkdf.salt.v1').digest();
  const info = Buffer.from(JSON.stringify(['siyab.workspace.vault.key.v1', context.origin, context.accountId, context.publicKey]));
  const key = crypto.hkdfSync('sha256', signature, salt, info, 32);
  const envelope = {version: 1, algorithm: 'AES-256-GCM', derivation: 'NEP413-HKDF-SHA256-v1', origin: context.origin,
    account_id: context.accountId, public_key: context.publicKey, vault_id: vaultId, iv: crypto.randomBytes(12).toString('base64')};
  const aad = Buffer.from(JSON.stringify(['siyab.workspace.vault.aad', envelope.version, envelope.algorithm, envelope.derivation,
    envelope.origin, envelope.account_id, envelope.public_key, envelope.vault_id]));
  const cipher = crypto.createCipheriv('aes-256-gcm', key, Buffer.from(envelope.iv, 'base64')); cipher.setAAD(aad);
  envelope.ciphertext = Buffer.concat([cipher.update(bytes || Buffer.from(JSON.stringify(value))), cipher.final(), cipher.getAuthTag()]).toString('base64');
  return {key, aad, envelope};
}
async function main() {
  const scenario = process.argv[2], env = environment();
  if (scenario === 'spec-vector') {
    const request = {message: 'hi', nonce: new Uint8Array(Array.from({length: 32}, (_, index) => index)), recipient: 'myapp.com', callbackUrl: 'myapp.com/callback'};
    assert.equal(Buffer.from(await env.api.nep413Digest(request)).toString('hex'), 'c9ce5de288f7de6d83e2303e56c7615949da83867ea33c0747ed45c47ef50148');
    delete request.callbackUrl;
    assert.equal(Buffer.from(await env.api.nep413Digest(request)).toString('hex'), '0e80e1606a46e66fcb450f6ceb55803921e465fddee8216463cf7b2680c09f37');
    request.message = 'π世界'; request.callbackUrl = 'https://siyab.xyz/workspace.html';
    assert.equal(Buffer.from(await env.api.nep413Digest(request)).toString('hex'), digest(request).toString('hex'));
  } else if (scenario === 'deterministic-request') {
    const first = await env.api.unlockRequest(identity), second = await environment().api.unlockRequest(identity);
    assert.deepEqual(normal({...first, nonce: Array.from(first.nonce)}), normal({...second, nonce: Array.from(second.nonce)}));
    assert.equal(first.nonce.length, 32); assert.equal(first.recipient, identity.origin);
    assert.equal(first.callbackUrl, identity.origin + '/workspace.html'); assert.equal(Object.hasOwn(first, 'state'), false);
    assert.match(first.message, /^Siyab encrypted workspace key v1\n/);
    for (const value of [identity.accountId, identity.origin, identity.publicKey]) assert.ok(first.message.includes(value));
    const other = await env.api.unlockRequest({...identity, accountId: 'bob.near'});
    assert.notEqual(Buffer.from(first.nonce).toString('hex'), Buffer.from(other.nonce).toString('hex'));
  } else if (scenario === 'round-trip') {
    const vault = await env.api.unlock({...identity, signMessage: env.signMessage});
    assert.equal(env.calls.length, 2); assert.deepEqual(env.calls[0], env.calls[1]);
    const first = await vault.encrypt('workspace', plain()), second = await vault.encrypt('workspace', plain());
    assert.notEqual(first.iv, second.iv); assert.notEqual(first.ciphertext, second.ciphertext);
    assert.equal(Buffer.from(first.iv, 'base64').length, 12);
    assert.deepEqual(normal(await vault.decrypt('workspace', first)), plain());
    const revision = await vault.encrypt('workspace:1', plain());
    assert.deepEqual(normal(await vault.decrypt('workspace:1', revision)), plain());
    await assert.rejects(vault.decrypt('workspace:2', revision), /different vault/);
    const other = environment(), recovered = await other.api.unlock({...identity, signMessage: other.signMessage});
    assert.deepEqual(normal(await recovered.decrypt('workspace', first)), plain());
    for (const forbidden of ['Private investigation', 'Only this browser knows', 'watched.near', 'case-private', 'watchlist', 'notes', 'signature']) assert.ok(!JSON.stringify(first).includes(forbidden));
    assert.deepEqual(Object.keys(vault).sort(), ['decrypt', 'encrypt', 'lock']);
    assert.equal(env.imports.filter(item => item.usages.includes('deriveKey')).length, 1);
    assert.ok(env.imports.every(item => !item.extractable));
  } else if (scenario === 'tamper') {
    const vault = await env.api.unlock({...identity, signMessage: env.signMessage}), envelope = await vault.encrypt('workspace', plain());
    for (const field of ['iv', 'ciphertext']) await assert.rejects(vault.decrypt('workspace', {...envelope, [field]: mutate64(envelope[field])}), /could not be decrypted/);
    const changed = Buffer.from(envelope.ciphertext, 'base64'); changed[changed.length - 1] ^= 1;
    await assert.rejects(vault.decrypt('workspace', {...envelope, ciphertext: changed.toString('base64')}), /could not be decrypted/);
  } else if (scenario === 'context') {
    const vault = await env.api.unlock({...identity, signMessage: env.signMessage}), envelope = await vault.encrypt('workspace', plain());
    await assert.rejects(vault.decrypt('other-workspace', envelope), /different vault/);
    for (const patch of [{origin: 'https://other.example'}, {account_id: 'bob.near'}, {public_key: signingKey(2).publicKey}]) await assert.rejects(vault.decrypt('workspace', {...envelope, ...patch}), /different wallet or origin/);
    for (const context of [{...identity, accountId: 'bob.near'}, {...identity, origin: 'https://other.example'}, {...identity, publicKey: signingKey(2).publicKey}]) {
      const other = environment({origin: context.origin}), key = context.publicKey === identity.publicKey ? signing : signingKey(2);
      const opened = await other.api.unlock({...context, signMessage: async request => ({accountId: context.accountId, publicKey: context.publicKey, signature: crypto.sign(null, digest(request), key.key).toString('base64')})});
      const rewritten = {...envelope, origin: context.origin, account_id: context.accountId, public_key: context.publicKey};
      await assert.rejects(opened.decrypt('workspace', rewritten), /could not be decrypted/);
    }
    await assert.rejects(vault.decrypt('changed', {...envelope, vault_id: 'changed'}), /could not be decrypted/);
  } else if (scenario === 'strict-envelope') {
    const vault = await env.api.unlock({...identity, signMessage: env.signMessage}), envelope = await vault.encrypt('workspace', plain());
    const bad = [{...envelope, version: 2}, {...envelope, algorithm: 'AES-CBC'}, {...envelope, derivation: 'unknown'}, {...envelope, signature: 'hidden'},
      {...envelope, iv: Buffer.alloc(11).toString('base64')}, {...envelope, iv: envelope.iv + '\n'}, {...envelope, ciphertext: Buffer.alloc(15).toString('base64')},
      {...envelope, ciphertext: envelope.ciphertext.replace(/=+$/, '') + '!'}, {...envelope, ciphertext: 'A'.repeat(699076)}, {...envelope, vault_id: '../workspace'}, null, []];
    const missing = {...envelope}; delete missing.ciphertext; bad.push(missing);
    for (const value of bad) { assert.throws(() => env.api.validateEnvelope(value)); await assert.rejects(vault.decrypt('workspace', value)); }
    assert.deepEqual(normal(env.api.validateEnvelope(envelope, identity, 'workspace')), normal(envelope));
    for (const [field, value] of [['account_id', 'not..near'], ['origin', 'https://siyab.xyz/'], ['public_key', 'ed25519:1']]) assert.throws(() => env.api.validateEnvelope({...envelope, [field]: value}));
  } else if (scenario === 'payload-binding') {
    for (const change of [request => ({...request, message: request.message + '!'}), request => ({...request, recipient: 'https://other.example'}),
      request => ({...request, callbackUrl: request.callbackUrl + '?different'}), request => { const copy = {...request}; delete copy.callbackUrl; return copy; },
      request => ({...request, nonce: new Uint8Array(request.nonce.map(byte => byte ^ 1))})]) {
      let calls = 0;
      await assert.rejects(env.api.unlock({...identity, signMessage: async request => { calls++; return env.signMessage(change(request)); }}), /signature does not match/);
      assert.equal(calls, 1);
    }
    await assert.rejects(env.api.unlock({...identity, signMessage: async request => { const altered = {...request}; request.nonce.fill(0); return env.signMessage(altered); }}), /signature does not match/);
  } else if (scenario === 'wallet-identity') {
    for (const patch of [{accountId: 'bob.near'}, {publicKey: signingKey(2).publicKey}, {publicKey: 'secp256k1:key'}, {signature: ''}, {signature: 'A'.repeat(88)}, {signature: 'A'.repeat(87) + '='}]) {
      await assert.rejects(env.api.unlock({...identity, signMessage: async request => ({...await env.signMessage(request), ...patch})}));
    }
    for (const value of [null, undefined, [], {}]) await assert.rejects(env.api.unlock({...identity, signMessage: async () => value}));
    await assert.rejects(env.api.unlock({...identity, signMessage: async () => { throw new Error('Wallet declined'); }}), /Wallet declined/);
  } else if (scenario === 'nondeterministic') {
    let verified = 0, calls = 0;
    const noisy = environment({verify: async () => { verified++; return true; }});
    await assert.rejects(noisy.api.unlock({...identity, signMessage: async request => {
      const signed = await noisy.signMessage(request); calls++;
      if (calls === 2) signed.signature = mutate64(signed.signature);
      return signed;
    }}), /different signatures/);
    assert.equal(verified, 2); assert.equal(calls, 2);
    assert.equal(noisy.imports.filter(item => item.usages.includes('deriveKey')).length, 0);
  } else if (scenario === 'limits') {
    const vault = await env.api.unlock({...identity, signMessage: env.signMessage});
    for (const value of [null, undefined, [], 'private', {notes: 'x'.repeat(524288)}, {notes: '世'.repeat(174763)}]) await assert.rejects(vault.encrypt('workspace', value));
    const exact = {notes: 'x'.repeat(524276)}, envelope = await vault.encrypt('workspace', exact);
    assert.equal(Buffer.byteLength(JSON.stringify(exact)), 524288);
    assert.equal(Buffer.from(envelope.ciphertext, 'base64').length, 524304);
    assert.deepEqual(normal(await vault.decrypt('workspace', envelope)), exact);
    for (const vaultId of ['', '../private', 'x'.repeat(129)]) await assert.rejects(vault.encrypt(vaultId, plain()));
    const request = await env.api.unlockRequest(identity);
    for (const patch of [{nonce: new Uint8Array(31)}, {message: null}, {recipient: ''}, {callbackUrl: null}]) await assert.rejects(env.api.nep413Digest({...request, ...patch}));
  } else if (scenario === 'lock') {
    const vault = await env.api.unlock({...identity, signMessage: env.signMessage}), envelope = await vault.encrypt('workspace', plain());
    const saving = vault.encrypt('workspace', plain()); vault.lock();
    await assert.rejects(saving, /locked/); await assert.rejects(vault.encrypt('workspace', plain()), /locked/);
    await assert.rejects(vault.decrypt('workspace', envelope), /locked/); vault.lock();
    const second = await env.api.unlock({...identity, signMessage: env.signMessage});
    const reading = second.decrypt('workspace', envelope); second.lock(); await assert.rejects(reading, /locked/);
  } else if (scenario === 'crypto-unavailable') {
    await assert.rejects(environment({unavailable: true}).api.unlock({...identity, signMessage: env.signMessage}), /WebCrypto/);
    await assert.rejects(environment({verify: async () => { throw new Error('Ed25519 unavailable'); }}).api.unlock({...identity, signMessage: env.signMessage}), /Ed25519 unavailable/);
    for (const patch of [{origin: 'https://other.example'}, {origin: identity.origin + '/'}, {origin: 'http://example.com'}, {accountId: 'not..near'}, {publicKey: 'ed25519:' + '1'.repeat(33)}]) await assert.rejects(env.api.unlock({...identity, ...patch, signMessage: env.signMessage}));
    const local = environment({origin: 'http://localhost:8792'});
    assert.equal((await local.api.unlockRequest({...identity, origin: 'http://localhost:8792'})).recipient, 'http://localhost:8792');
  } else if (scenario === 'interoperability') {
    let signature;
    const vault = await env.api.unlock({...identity, signMessage: async request => { const signed = await env.signMessage(request); signature = Buffer.from(signed.signature, 'base64'); return signed; }});
    const native = nativeCipher(signature, identity, 'workspace', plain());
    assert.deepEqual(normal(await vault.decrypt('workspace', native.envelope)), plain());
    const sealed = await vault.encrypt('workspace', plain()), ciphertext = Buffer.from(sealed.ciphertext, 'base64');
    const decipher = crypto.createDecipheriv('aes-256-gcm', native.key, Buffer.from(sealed.iv, 'base64')); decipher.setAAD(native.aad); decipher.setAuthTag(ciphertext.subarray(-16));
    assert.deepEqual(JSON.parse(Buffer.concat([decipher.update(ciphertext.subarray(0, -16)), decipher.final()]).toString('utf8')), plain());
    for (const bytes of [Buffer.from('not JSON'), Buffer.from('[1,2,3]'), Buffer.from([255])]) await assert.rejects(vault.decrypt('workspace', nativeCipher(signature, identity, 'workspace', null, bytes).envelope), /could not be read/);
  } else if (scenario === 'workspace-boundary') {
    const vault = await env.api.unlock({...identity, signMessage: env.signMessage}), received = [];
    const server = body => { received.push(JSON.parse(body)); return body; };
    const body = JSON.stringify(await vault.encrypt('workspace', plain())), returned = server(body);
    assert.deepEqual(Object.keys(received[0]).sort(), ['account_id', 'algorithm', 'ciphertext', 'derivation', 'iv', 'origin', 'public_key', 'vault_id', 'version']);
    assert.deepEqual(normal(await vault.decrypt('workspace', JSON.parse(returned))), plain());
    for (const value of [plain().cases[0].notes, plain().cases[0].title, plain().watchlist[0]]) assert.ok(!body.includes(value));
    assert.equal(env.calls.length, 2); assert.ok(!body.includes(crypto.sign(null, digest(env.calls[0]), signing.key).toString('base64')));
  } else throw new Error('Unknown scenario: ' + scenario);
}
main().catch(error => { console.error(error); process.exitCode = 1; });
