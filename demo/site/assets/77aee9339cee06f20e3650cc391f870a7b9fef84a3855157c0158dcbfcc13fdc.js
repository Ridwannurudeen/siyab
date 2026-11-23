(() => {
  "use strict";
  const version = 1, algorithm = "AES-256-GCM", derivation = "NEP413-HKDF-SHA256-v1";
  const maxPlaintext = 524288;
  const encoder = new TextEncoder();
  const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  const fields = ["version", "algorithm", "derivation", "origin", "account_id", "public_key", "vault_id", "iv", "ciphertext"];
  const accountId = value => typeof value === "string" && value.length >= 2 && value.length <= 64
    && /^[a-z0-9]+(?:[-_][a-z0-9]+)*(?:\.[a-z0-9]+(?:[-_][a-z0-9]+)*)*$/.test(value);
  const validVaultId = value => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
  function publicKeyBytes(value) {
    if (typeof value !== "string" || !/^ed25519:[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value)) throw new Error("Choose an Ed25519 wallet signing key.");
    const encoded = value.slice(8), bytes = [];
    let number = 0n;
    for (const character of encoded) number = number * 58n + BigInt(alphabet.indexOf(character));
    while (number) { bytes.unshift(Number(number & 255n)); number >>= 8n; }
    for (const character of encoded) { if (character !== "1") break; bytes.unshift(0); }
    if (bytes.length !== 32) throw new Error("The wallet public key must contain 32 bytes.");
    return new Uint8Array(bytes);
  }
  function context(value, currentOrigin = true) {
    if (!value || !accountId(value.accountId)) throw new Error("Choose a valid NEAR account.");
    publicKeyBytes(value.publicKey);
    if (typeof value.origin !== "string" || value.origin.length > 2048) throw new Error("The workspace origin could not be read.");
    const url = new URL(value.origin);
    if (url.origin !== value.origin || !(url.protocol === "https:" || url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))
        || currentOrigin && value.origin !== location.origin) throw new Error("Unlock the workspace on its original Siyab origin.");
    return {origin: value.origin, accountId: value.accountId, publicKey: value.publicKey};
  }
  function base64(bytes) {
    const parts = [];
    for (let offset = 0; offset < bytes.length; offset += 16384) parts.push(String.fromCharCode(...bytes.subarray(offset, offset + 16384)));
    return btoa(parts.join(""));
  }
  function decode64(value, minimum, maximum) {
    if (typeof value !== "string" || value.length > Math.ceil(maximum / 3) * 4
        || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) throw new Error("The encrypted workspace has invalid base64 data.");
    const decoded = atob(value), bytes = new Uint8Array(decoded.length);
    for (let index = 0; index < decoded.length; index++) bytes[index] = decoded.charCodeAt(index);
    if (bytes.length < minimum || bytes.length > maximum || base64(bytes) !== value) throw new Error("The encrypted workspace has invalid byte lengths or encoding.");
    return bytes;
  }
  function join(parts) {
    const bytes = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
    let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.length; }
    return bytes;
  }
  const integer = value => { const bytes = new Uint8Array(4); new DataView(bytes.buffer).setUint32(0, value, true); return bytes; };
  const string = value => { const bytes = encoder.encode(value); return join([integer(bytes.length), bytes]); };
  async function nep413Digest(request) {
    if (!globalThis.crypto?.subtle) throw new Error("WebCrypto is required to unlock the encrypted workspace.");
    if (!request || typeof request.message !== "string" || request.message.length > 4096
        || typeof request.recipient !== "string" || !request.recipient || request.recipient.length > 2048
        || !(request.nonce instanceof Uint8Array) || request.nonce.length !== 32
        || request.callbackUrl !== undefined && (typeof request.callbackUrl !== "string" || request.callbackUrl.length > 2048)) throw new Error("The NEP-413 signing request could not be read.");
    const callback = request.callbackUrl === undefined ? new Uint8Array([0]) : join([new Uint8Array([1]), string(request.callbackUrl)]);
    return new Uint8Array(await crypto.subtle.digest("SHA-256", join([integer(2147484061), string(request.message), request.nonce, string(request.recipient), callback])));
  }
  async function unlockRequest(value) {
    if (!globalThis.crypto?.subtle) throw new Error("WebCrypto is required to unlock the encrypted workspace.");
    const owner = context(value);
    const nonce = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(JSON.stringify(["siyab.workspace.vault.nonce.v1", owner.origin, owner.accountId, owner.publicKey]))));
    return {message: "Siyab encrypted workspace key v1\nOrigin: " + owner.origin + "\nAccount: " + owner.accountId + "\nSigning key: " + owner.publicKey
      + "\nThis signature unlocks your encrypted workspace. It is never sent to Siyab.", recipient: owner.origin, nonce, callbackUrl: owner.origin + "/workspace.html"};
  }
  function validateEnvelope(value, expected = null, vaultId = null) {
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).length !== fields.length
        || !fields.every(field => Object.hasOwn(value, field)) || value.version !== version || value.algorithm !== algorithm || value.derivation !== derivation
        || !validVaultId(value.vault_id)) throw new Error("The encrypted workspace format is not supported.");
    const owner = context({origin: value.origin, accountId: value.account_id, publicKey: value.public_key}, false);
    if (expected) {
      const selected = context(expected);
      if (owner.origin !== selected.origin || owner.accountId !== selected.accountId || owner.publicKey !== selected.publicKey) throw new Error("The encrypted workspace belongs to a different wallet or origin.");
    }
    if (vaultId !== null && (!validVaultId(vaultId) || value.vault_id !== vaultId)) throw new Error("The encrypted workspace belongs to a different vault.");
    decode64(value.iv, 12, 12); decode64(value.ciphertext, 16, maxPlaintext + 16);
    return Object.fromEntries(fields.map(field => [field, value[field]]));
  }
  const additionalData = envelope => encoder.encode(JSON.stringify(["siyab.workspace.vault.aad", envelope.version, envelope.algorithm, envelope.derivation,
    envelope.origin, envelope.account_id, envelope.public_key, envelope.vault_id]));
  async function unlock(value) {
    const request = await unlockRequest(value), owner = context(value);
    if (typeof value.signMessage !== "function") throw new Error("A NEP-413 wallet is required to unlock the workspace.");
    const digest = await nep413Digest(request);
    const verificationKey = await crypto.subtle.importKey("raw", publicKeyBytes(owner.publicKey), "Ed25519", false, ["verify"]);
    let first = null, second = null, key = null, epoch = 0;
    async function signature() {
      const response = await value.signMessage({...request, nonce: request.nonce.slice()});
      if (!response || response.accountId !== owner.accountId || response.publicKey !== owner.publicKey) throw new Error("The wallet returned a different account or signing key.");
      const bytes = decode64(response.signature, 64, 64);
      let verified = false;
      try {
        verified = await crypto.subtle.verify("Ed25519", verificationKey, bytes, digest);
        if (!verified) throw new Error("The wallet signature does not match the workspace unlock request.");
        return bytes;
      } finally { if (!verified) bytes.fill(0); }
    }
    try {
      first = await signature(); second = await signature();
      let difference = 0;
      for (let index = 0; index < first.length; index++) difference |= first[index] ^ second[index];
      if (difference) throw new Error("This wallet returned different signatures for the same request. It cannot recover an encrypted workspace across devices.");
      const material = await crypto.subtle.importKey("raw", first, "HKDF", false, ["deriveKey"]);
      const salt = await crypto.subtle.digest("SHA-256", encoder.encode("siyab.workspace.vault.hkdf.salt.v1"));
      const info = encoder.encode(JSON.stringify(["siyab.workspace.vault.key.v1", owner.origin, owner.accountId, owner.publicKey]));
      key = await crypto.subtle.deriveKey({name: "HKDF", hash: "SHA-256", salt, info}, material, {name: "AES-GCM", length: 256}, false, ["encrypt", "decrypt"]);
    } finally { if (first) first.fill(0); if (second) second.fill(0); }
    return Object.freeze({
      async encrypt(vaultId, value) {
        if (!key) throw new Error("The encrypted workspace is locked.");
        if (!validVaultId(vaultId)) throw new Error("Choose a valid encrypted workspace identifier.");
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("The workspace must be a JSON object.");
        const source = JSON.stringify(value);
        if (typeof source !== "string" || !source.startsWith("{")) throw new Error("The workspace must be a JSON object.");
        const bytes = encoder.encode(source), revision = epoch;
        if (bytes.length > maxPlaintext) { bytes.fill(0); throw new Error("The encrypted workspace must be no larger than 512 KiB."); }
        const envelope = {version, algorithm, derivation, origin: owner.origin, account_id: owner.accountId, public_key: owner.publicKey,
          vault_id: vaultId, iv: base64(crypto.getRandomValues(new Uint8Array(12)))};
        try {
          const encrypted = await crypto.subtle.encrypt({name: "AES-GCM", iv: decode64(envelope.iv, 12, 12), additionalData: additionalData(envelope), tagLength: 128}, key, bytes);
          if (revision !== epoch) throw new Error("The encrypted workspace was locked before saving completed.");
          return {...envelope, ciphertext: base64(new Uint8Array(encrypted))};
        } finally { bytes.fill(0); }
      },
      async decrypt(vaultId, value) {
        if (!key) throw new Error("The encrypted workspace is locked.");
        const envelope = validateEnvelope(value, owner, vaultId), revision = epoch;
        let bytes;
        try {
          bytes = new Uint8Array(await crypto.subtle.decrypt({name: "AES-GCM", iv: decode64(envelope.iv, 12, 12), additionalData: additionalData(envelope), tagLength: 128}, key,
            decode64(envelope.ciphertext, 16, maxPlaintext + 16)));
        } catch (error) { throw new Error("The encrypted workspace could not be decrypted. Use its original wallet key and Siyab origin."); }
        try {
          if (revision !== epoch) throw new Error("The encrypted workspace was locked before opening completed.");
          let result;
          try {
            result = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes));
            if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error("Invalid workspace object");
          } catch (error) { throw new Error("The decrypted workspace could not be read as a JSON object."); }
          return result;
        } finally { bytes.fill(0); }
      },
      lock() { key = null; epoch++; }
    });
  }
  window.SiyabVault = Object.freeze({nep413Digest, unlockRequest, validateEnvelope, unlock});
})();
