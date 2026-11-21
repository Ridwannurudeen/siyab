(() => {
  "use strict";
  const panel = document.getElementById("near-workspace");
  if (!panel) return;
  const node = id => document.getElementById("near-" + id);
  let revisionStorageAvailable = true;
  const status = text => { node("status").textContent = text + (revisionStorageAvailable ? "" : " Browser storage is unavailable. Revision checks last only until this page closes."); };
  const seenRevisions = new Map();
  const localResets = new Map();
  const resetHere = "The encrypted vault was reset on this device. Its encrypted data is lost. This browser's saved work is unchanged. Unlock with your wallet to save a new encrypted copy.";
  const resetElsewhere = "The encrypted vault was reset elsewhere. Its encrypted data is lost. This browser's saved work is unchanged. Unlock with your wallet to save a new encrypted copy.";
  const resetUnknown = "The encrypted vault was reset. Its encrypted data is lost. This browser's saved work is unchanged. Unlock with your wallet to save a new encrypted copy.";
  let session = null;
  let vault = null;
  let unlocked = null;
  let revision = 0;
  let blocked = false;
  let busy = false;
  let generation = 0;
  let requestController = null;
  function render() {
    node("sign-in").hidden = Boolean(session);
    node("controls").hidden = !session;
    node("identity").textContent = session ? session.account_id : "";
    node("login").disabled = busy;
    node("account").disabled = busy;
    node("unlock").disabled = busy || !session || blocked || Boolean(unlocked);
    node("save").disabled = busy || blocked || !unlocked;
    node("restore").disabled = busy || blocked || !unlocked || !vault;
    node("lock").disabled = busy || !unlocked;
    node("logout").disabled = busy || !session;
    node("reset").disabled = busy || !session;
    node("reset-confirmation").disabled = busy || !session;
  }
  function lock() {
    if (unlocked) unlocked.lock();
    unlocked = null;
  }
  async function request(path, body) {
    const controller = new AbortController();
    requestController = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const headers = {Accept: "application/json"};
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        if (session) headers["X-CSRF-Token"] = session.csrf_token;
      }
      const response = await fetch("/workspace-api/near/" + path, {
        method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store",
        headers, signal: controller.signal, ...(body === undefined ? {} : {body: JSON.stringify(body)})
      });
      const data = await response.json();
      if (!response.ok) {
        if (response.status === 401) { lock(); session = null; vault = null; revision = 0; }
        if (response.status === 409) { lock(); blocked = true; }
        throw new Error(typeof data.message === "string" ? data.message : "The encrypted workspace request failed. Try again.");
      }
      return data;
    } catch (error) {
      if (controller.signal.aborted) throw new Error("The encrypted workspace request timed out. Try again.");
      throw error;
    } finally {
      clearTimeout(timeout);
      if (requestController === controller) requestController = null;
    }
  }
  function wallet() {
    const provider = window.near;
    if (!provider?.isSender || typeof provider.signMessage !== "function") {
      throw new Error("Open this page with Sender's NEP-413 signing interface enabled. This path never adds a key or sends a transaction.");
    }
    return async params => {
      const result = await provider.signMessage(params);
      if (result?.error || !result?.response) throw new Error("The wallet did not return a signed NEP-413 message. Approve the message in Sender and try again.");
      const signed = result.response;
      if (signed.state !== undefined && signed.state !== params.state) throw new Error("The wallet response does not match this signing request.");
      return signed;
    };
  }
  function context() {
    return {origin: location.origin, accountId: session.account_id, publicKey: session.public_key};
  }
  function rememberRevision(next) {
    const key = "siyab.near-vault-revision.v1:" + JSON.stringify([location.origin, session.account_id, session.public_key]);
    let highest = seenRevisions.get(key) || 0;
    try {
      const saved = Number(localStorage.getItem(key));
      if (Number.isSafeInteger(saved) && saved >= 0) highest = Math.max(highest, saved);
    } catch (error) { revisionStorageAvailable = false; }
    if (next < highest) {
      lock();
      blocked = true;
      throw new Error("The server returned an older revision of this encrypted workspace than this browser has already seen. Loading and saving are blocked. Your browser work is unchanged. Use a current saved copy or explicitly reset the encrypted copy.");
    }
    seenRevisions.set(key, next);
    try {
      localStorage.setItem(key, String(next));
    } catch (error) {
      revisionStorageAvailable = false;
    }
  }
  function resetMessage() {
    const key = "siyab.near-vault-reset.v1:" + JSON.stringify([location.origin, session.account_id, session.public_key]);
    let localRevision = localResets.get(key) || 0;
    try {
      const saved = Number(localStorage.getItem(key));
      if (Number.isSafeInteger(saved) && saved > 0) localRevision = Math.max(localRevision, saved);
    } catch (error) { revisionStorageAvailable = false; }
    if (localRevision === revision) return resetHere;
    return revisionStorageAvailable ? resetElsewhere : resetUnknown;
  }
  function readVault(data) {
    if (!Number.isSafeInteger(data.revision) || data.revision < 0 || (data.revision === 0 && data.vault !== null)) {
      blocked = true;
      throw new Error("The encrypted workspace revision could not be read. No saved work was changed.");
    }
    if (data.vault !== null) {
      try { window.SiyabVault.validateEnvelope(data.vault, context(), "workspace:" + data.revision); }
      catch (error) {
        blocked = true;
        throw new Error("This saved copy needs its original wallet signing key and an intact encrypted record. Your browser work and the saved copy were preserved.");
      }
    }
    rememberRevision(data.revision);
    vault = data.vault;
    revision = data.revision;
  }
  async function run(action) {
    if (busy) return;
    busy = true;
    render();
    const current = generation;
    try { await action(current); }
    catch (error) { if (current === generation) status(error.message); }
    finally { if (current === generation) { busy = false; render(); } }
  }
  node("sign-in").addEventListener("submit", event => {
    event.preventDefault();
    run(async current => {
      const signMessage = wallet();
      const account = node("account").value.trim();
      if (/^0x[0-9a-f]{40}$/.test(account)) {
        throw new Error("ETH-implicit (0x) accounts cannot sign in. Use a NEAR account with an ordinary Full Access Ed25519 key.");
      }
      if (!/^[a-z0-9]+(?:[._-][a-z0-9]+)*$/.test(account) || account.length < 2 || account.length > 64) {
        throw new Error("Enter the NEAR mainnet account held by your wallet.");
      }
      status("Approve the sign-in message in your NEAR wallet.");
      const challenge = await request("challenge", {account_id: account});
      if (current !== generation) return;
      const expectedMessage = "Sign in to Siyab. This request does not authorize a transaction or payment.\n"
        + "Account: " + account + "\nOrigin: " + location.origin + "\nChallenge: " + challenge.challenge_id
        + "\nIssued at: " + (challenge.expires_at - 300000) + "\nExpires at: " + challenge.expires_at;
      if (challenge.recipient !== location.origin || challenge.callbackUrl !== location.origin + "/workspace.html"
          || challenge.account_id !== account || challenge.state !== challenge.challenge_id
          || typeof challenge.challenge_id !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(challenge.challenge_id)
          || !Number.isSafeInteger(challenge.expires_at) || challenge.expires_at <= Date.now()
          || challenge.expires_at > Date.now() + 300000 || challenge.message !== expectedMessage) {
        throw new Error("The sign-in challenge does not match this origin or has expired. Start again.");
      }
      const bytes = Uint8Array.from(atob(challenge.nonce), character => character.charCodeAt(0));
      if (bytes.length !== 32 || btoa(String.fromCharCode(...bytes)) !== challenge.nonce) throw new Error("The sign-in nonce is malformed. Start again.");
      const signed = await signMessage({message: challenge.message, recipient: challenge.recipient, nonce: bytes,
        callbackUrl: challenge.callbackUrl, state: challenge.state});
      if (current !== generation) return;
      if (signed.accountId !== account) throw new Error("Choose the same account in Sender and in Siyab, then sign in again.");
      const result = await request("verify", {challenge_id: challenge.challenge_id, accountId: signed.accountId,
        publicKey: signed.publicKey, signature: signed.signature, ...(signed.state === undefined ? {} : {state: signed.state})});
      if (current !== generation) return;
      session = result;
      blocked = true;
      const saved = await request("vault");
      if (current !== generation) return;
      readVault(saved);
      blocked = false;
      status(vault === null && revision > 0 ? resetMessage()
        : "Signed in as " + session.account_id + ". No browser work was uploaded. Unlock with your wallet to save or open an encrypted copy.");
    });
  });
  node("unlock").addEventListener("click", () => run(async current => {
    status("Approve the same unlock message twice in your wallet. These signatures stay in this browser.");
    const next = await window.SiyabVault.unlock({...context(), signMessage: wallet()});
    if (current !== generation) { next.lock(); return; }
    try {
      if (vault) await next.decrypt("workspace:" + revision, vault);
    } catch (error) {
      next.lock();
      throw new Error("This wallet could not open the saved copy. Use its original signing key. Your browser work and the encrypted copy were preserved.");
    }
    if (current !== generation) { next.lock(); return; }
    unlocked = next;
    status("Encryption key unlocked in memory. Choose Save encrypted copy or Merge into this browser. No work was transferred by unlocking.");
  }));
  node("save").addEventListener("click", () => run(async current => {
    const snapshot = window.SiyabBrowserWorkspace.snapshot();
    if (!confirm("Save an encrypted copy of this browser's investigations, notes and watchlist for " + session.account_id
        + "? This replaces the previous encrypted copy. Shared teams receive nothing.")) return;
    const encrypted = await unlocked.encrypt("workspace:" + (revision + 1), snapshot);
    if (current !== generation) return;
    const result = await request("vault", {revision, vault: encrypted});
    if (current !== generation) return;
    if (result.revision !== revision + 1) { lock(); throw new Error("The saved revision could not be confirmed. Sign in again before saving."); }
    vault = encrypted;
    revision = result.revision;
    rememberRevision(revision);
    status("Encrypted copy saved for " + session.account_id + ". Shared teams receive nothing.");
  }));
  node("restore").addEventListener("click", () => run(async current => {
    const restored = await unlocked.decrypt("workspace:" + revision, vault);
    if (current !== generation) return;
    if (!confirm("Merge this encrypted copy into this browser? Existing notes and monitoring settings are preserved. Decrypted data will be saved in browser storage.")) return;
    window.SiyabBrowserWorkspace.merge(restored);
    status("Encrypted copy merged into this browser. Existing notes and monitoring settings were preserved.");
  }));
  node("lock").addEventListener("click", () => {
    lock();
    status("Encryption key locked. Data already saved in this browser remains available below.");
    render();
  });
  node("reset-form").addEventListener("submit", event => {
    event.preventDefault();
    run(async current => {
      if (node("reset-confirmation").value !== "DELETE " + session.account_id) {
        throw new Error("Type DELETE " + session.account_id + " exactly to permanently delete the encrypted copy.");
      }
      const result = await request("vault/reset", {confirmation: node("reset-confirmation").value});
      if (current !== generation) return;
      if (result.reset !== true || !Number.isSafeInteger(result.revision) || result.revision <= revision) {
        lock();
        blocked = true;
        throw new Error("The reset could not be confirmed. Reload before opening or saving the encrypted copy.");
      }
      lock();
      rememberRevision(result.revision);
      vault = null;
      revision = result.revision;
      const key = "siyab.near-vault-reset.v1:" + JSON.stringify([location.origin, session.account_id, session.public_key]);
      localResets.set(key, revision);
      try { localStorage.setItem(key, String(revision)); }
      catch (error) { revisionStorageAvailable = false; }
      blocked = false;
      node("reset-confirmation").value = "";
      status(resetHere);
    });
  });
  node("logout").addEventListener("click", () => run(async current => {
    lock();
    await request("logout", {});
    if (current !== generation) return;
    session = null;
    vault = null;
    revision = 0;
    blocked = false;
    status("Signed out of NEAR. This browser's saved work and your separate team session were preserved.");
  }));
  window.addEventListener("pagehide", () => {
    generation++;
    lock();
    if (requestController) requestController.abort();
    requestController = null;
    busy = false;
    render();
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) { lock(); status("Encryption key locked after leaving this page. Unlock with your wallet again."); render(); }
  });
  document.addEventListener("DOMContentLoaded", () => run(async current => {
    if (!window.SiyabVault || !window.SiyabBrowserWorkspace || !crypto.subtle) throw new Error("Encrypted workspaces need a secure browser with WebCrypto. Local browser work remains available below.");
    const result = await request("session");
    if (current !== generation) return;
    if (result.authenticated) {
      session = result;
      blocked = true;
      const saved = await request("vault");
      if (current !== generation) return;
      readVault(saved);
      blocked = false;
    }
    status(session ? (vault === null && revision > 0 ? resetMessage() : "Signed in as " + session.account_id + ". Unlock with your wallet to open the encrypted workspace.")
      : "Sign in with NEAR to open your encrypted workspace. Browser saves remain local until you choose to upload an encrypted copy.");
  }), {once: true});
})();
