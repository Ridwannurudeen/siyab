"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { createHash } = require("node:crypto");
const ROOT = path.join(__dirname, "..");
const dir = lane => path.join(ROOT, "integrations", lane, "fixtures");
const load = (lane, name) => JSON.parse(fs.readFileSync(path.join(dir(lane), name), "utf8"));
const ALLOW = load("wallet-selector", "account-check-allow.json");
const ASK = load("langchain", "verdict-ask.json");
const DENY = load("wallet-selector", "account-check-deny.json");
const TOKEN = load("langchain", "verdict-token-allow.json");
const altered = (answer, update) => {
  const result = JSON.parse(JSON.stringify(answer));
  const document = JSON.parse(result.evidence);
  update(document);
  result.evidence = JSON.stringify(document);
  result.evidence_hash = createHash("sha256").update(result.evidence).digest("hex");
  return result;
};
const transfer = receiverId => ({ receiverId, actions: [{ type: "Transfer", params: { deposit: "1" } }] });
const call = (methodName, args) => ({ type: "FunctionCall", params: { methodName, args, gas: "30000000000000", deposit: "1" } });
async function run() {
  const { actionPayees, decideAccountCheck, decidePayment, guardTransfer, guardWallet, preflightAccountCheck } =
    await import(pathToFileURL(path.join(ROOT, "integrations", "wallet-selector", "adapter.mjs")).href);
  let count = 0;
  const failures = [];
  const test = async (name, work) => {
    try { await work(); count++; process.stdout.write("ok " + count + " - " + name + "\n"); }
    catch (error) { failures.push(error); process.stderr.write("not ok - " + name + ": " + error.stack + "\n"); }
  };
  const code = expected => error => error.code === expected;
  const check = decision => async () => ({ decision, reasons: [] });
  let signed = 0;
  class Wallet {
    getAccounts() { return this.accounts; }
    signIn() { return this.accounts; }
    signOut() { return this.accounts; }
    isSignedIn() { return this.accounts.length > 0; }
    async signAndSendTransaction() { signed++; return "sent"; }
    async signAndSendTransactions() { signed++; return "sent-many"; }
    async signMessage() { signed++; }
    async signTransaction() { signed++; }
    async createSignedTransaction() { signed++; }
    async verifyOwner() { signed++; }
    async signDelegateAction() { signed++; }
    async signAndSendDelegateAction() { signed++; }
  }
  const wallet = new Wallet();
  wallet.accounts = ["owner.near"];
  await test("sha256 of every recorded fixture", async () => {
    for (const lane of ["langchain", "wallet-selector"]) {
      for (const name of fs.readdirSync(dir(lane)).filter(name => name.endsWith(".json"))) {
        const fixture = load(lane, name);
        assert.equal(createHash("sha256").update(fixture.evidence).digest("hex"), fixture.evidence_hash, lane + "/" + name);
      }
    }
  });
  await test("recorded account answers", async () => {
    assert.equal((await decideAccountCheck(ALLOW, "intents.near")).decision, "allow");
    assert.equal((await decideAccountCheck(load("wallet-selector", "account-check-ask.json"), "hello.near")).decision, "ask");
    assert.equal((await decideAccountCheck(DENY, "other.near")).decision, "deny");
  });
  await test("evidence denial and signal precede outcome and binding", async () => {
    for (const answer of [DENY, altered(ALLOW, doc => { doc.checks[0].status = "signal"; })]) {
      assert.equal((await decideAccountCheck({ ...answer, outcome: "MAYBE" }, "other.near")).decision, "deny");
    }
  });
  await test("unchecked top level reasons never shown", async () => {
    const answer = altered(DENY, doc => { doc.reasons = []; });
    assert.deepEqual((await decideAccountCheck({ ...DENY, reasons: ["forged"] }, "other.near")).reasons, JSON.parse(DENY.evidence).reasons);
    assert.deepEqual((await decideAccountCheck({ ...answer, reasons: ["forged"] }, "other.near")).reasons, ["The evidence refused this transfer."]);
    assert.deepEqual((await decideAccountCheck({ outcome: "DENY", reasons: ["forged"] }, "other.near")).reasons, ["The check refused this transfer."]);
  });
  await test("edited and incomplete answers ask", async () => {
    for (const answer of [{ ...ALLOW, evidence_hash: "0".repeat(64) }, {}, { ...ALLOW, signing: "sometimes" }, { ...ALLOW, action: "ask" }, altered(ALLOW, doc => { doc.checks[0].status = "unknown"; })]) {
      assert.equal((await decideAccountCheck(answer, "intents.near")).decision, "ask");
    }
    assert.equal((await decideAccountCheck(ALLOW, "other.near")).decision, "ask");
  });
  await test("only explicit allow bypasses confirmation and receiver passed", async () => {
    for (const decision of [undefined, "MAYBE", "ALLOW", "ask"]) {
      await assert.rejects(() => guardTransfer({ receiver: "bob.near", check: check(decision) }), code("SIYAB_ASK"));
    }
    assert.equal((await guardTransfer({ receiver: "bob.near", check: async () => null, confirm: async decision => decision.receiver === "bob.near" })).confirmed, true);
    await assert.rejects(() => guardTransfer({ receiver: "bob.near", check: check("deny"), confirm: async () => true }), code("SIYAB_DENY"));
  });
  await test("HTTP 429 and 503 ask", async () => {
    for (const status of [429, 503]) {
      const decision = await preflightAccountCheck({ receiver: "bob.near", fetchImpl: async () => ({ ok: false, status }) });
      assert.equal(decision.decision, "ask");
      assert.match(decision.reasons[0], new RegExp(String(status)));
    }
  });
  await test("timeout abort and malformed body ask", async () => {
    assert.equal((await preflightAccountCheck({ receiver: "bob.near", timeoutMs: 1,
      fetchImpl: async (_, options) => new Promise((resolve, reject) => options.signal.addEventListener("abort", () => reject(new Error("aborted")))) })).decision, "ask");
    for (const json of [async () => { throw new SyntaxError("bad JSON"); }, async () => null, async () => []]) {
      assert.equal((await preflightAccountCheck({ receiver: "bob.near", fetchImpl: async () => ({ ok: true, json }) })).decision, "ask");
    }
  });
  await test("real Transfer shape and bound prototype wallet methods", async () => {
    const checks = [];
    const guarded = guardWallet(wallet, { check: async options => { checks.push(options.receiver); return { decision: "allow" }; } });
    assert.deepEqual(guarded.getAccounts(), ["owner.near"]);
    assert.deepEqual(await guarded.signIn({ contractId: "app.near" }), ["owner.near"]);
    assert.deepEqual(guarded.signOut(), ["owner.near"]);
    assert.equal(guarded.isSignedIn(), true);
    assert.deepEqual(actionPayees(transfer("bob.near").actions, "bob.near"), ["bob.near"]);
    const before = signed;
    assert.equal(await guarded.signAndSendTransaction(transfer("bob.near")), "sent");
    assert.equal(signed, before + 1);
    assert.deepEqual(checks, ["app.near", "bob.near", "bob.near"]);
  });
  await test("FT methods and decoded object bytes and base64 check token and transfer", async () => {
    const subject = JSON.parse(TOKEN.evidence).subject;
    assert.equal((await decidePayment(TOKEN, subject.receiver, subject.amount, subject.token)).decision, "allow");
    const requests = [];
    for (const method of ["ft_transfer", "ft_transfer_call"]) {
      for (const encode of [value => value, value => JSON.stringify(value), value => new TextEncoder().encode(JSON.stringify(value)), value => Buffer.from(JSON.stringify(value)).toString("base64"), value => new TextEncoder().encode(Buffer.from(JSON.stringify(value)).toString("base64"))]) {
        const before = signed;
        const guarded = guardWallet(wallet, { fetchImpl: async url => {
          requests.push(url);
          const request = new URL(url);
          const answer = request.searchParams.get("token") === "near" ? altered(load("langchain", "verdict-allow.json"), doc => {
            doc.subject.receiver = subject.token;
            doc.subject.amount = "1";
          }) : url.includes("/live/verdict?") ? TOKEN : altered(ALLOW, doc => { doc.subject.account = subject.token; });
          return { ok: true, json: async () => answer };
        } });
        const args = { receiver_id: subject.receiver, amount: subject.amount };
        if (method === "ft_transfer_call") args.msg = "";
        await guarded.signAndSendTransaction({ receiverId: subject.token, actions: [call(method, encode(args))] });
        assert.equal(signed, before + 1);
      }
    }
    assert.equal(requests.length, 30);
    const url = new URL(requests[2]);
    assert.equal(url.pathname, "/live/verdict");
    for (const [key, value] of Object.entries({ receiver: subject.receiver, amount: subject.amount, token: subject.token })) assert.equal(url.searchParams.get(key), value);
    assert.equal((await decidePayment(TOKEN, subject.receiver, "2", subject.token)).decision, "ask");
  });
  await test("mixed batch checks every payee before send", async () => {
    const checks = [];
    const guarded = guardWallet(wallet, { check: async options => { checks.push([options.receiver, options.token, options.amount]); return { decision: "allow" }; } });
    const before = signed;
    await guarded.signAndSendTransactions({ transactions: [{ receiverId: "token.near", actions: [call("ft_transfer", { receiver_id: "clean.near", amount: "1" })] }, transfer("other.near")] });
    assert.deepEqual(checks, [["token.near", undefined, undefined], ["token.near", "near", "1"], ["clean.near", "token.near", "1"],
      ["other.near", undefined, undefined], ["other.near", "near", "1"]]);
    assert.equal(signed, before + 1);
  });
  await test("every unsupported action blocks mixed and second transactions", async () => {
    for (const type of ["DeleteAccount", "AddKey", "DeployContract", "Stake", "CreateAccount", "DeleteKey", "transfer"]) {
      let checked = 0;
      const guarded = guardWallet(wallet, { check: async () => { checked++; return { decision: "allow" }; } });
      const before = signed;
      await assert.rejects(() => guarded.signAndSendTransactions({ transactions: [transfer("bob.near"), { receiverId: "other.near", actions: [{ type, params: {} }] }] }), code("SIYAB_PAYEE"));
      await assert.rejects(() => guarded.signAndSendTransaction({ receiverId: "bob.near", actions: [...transfer("bob.near").actions, { type, params: {} }] }), code("SIYAB_PAYEE"));
      assert.equal(checked, 0);
      assert.equal(signed, before);
    }
  });
  await test("invalid ids transfer fields and empty transactions block", async () => {
    let checked = 0;
    const guarded = guardWallet(wallet, { check: async () => { checked++; return { decision: "allow" }; } });
    const before = signed;
    for (const receiver of [undefined, "a", "bad..near", "bad_-near", "bob.near\n", "bob.near\r", "bob.near\u2028", "bob.near\u2029", "BOB.near"]) {
      await assert.rejects(() => guarded.signAndSendTransaction(transfer(receiver)), code("SIYAB_PAYEE"));
    }
    for (const args of [{ receiver_id: "bad..near", amount: "1" }, { receiver_id: "bob.near", amount: "01" }]) {
      await assert.rejects(() => guarded.signAndSendTransaction({ receiverId: "token.near", actions: [call("ft_transfer", args)] }), code("SIYAB_PAYEE"));
    }
    await assert.rejects(() => guarded.signAndSendTransaction({ receiverId: "bob.near", actions: [] }), code("SIYAB_PAYEE"));
    await assert.rejects(() => guarded.signAndSendTransactions({ transactions: [transfer("bob.near"), { actions: transfer("bob.near").actions }] }), code("SIYAB_PAYEE"));
    const sparse = [transfer("bob.near")];
    sparse.length = 2;
    await assert.rejects(() => guarded.signAndSendTransactions({ transactions: sparse }), code("SIYAB_PAYEE"));
    assert.equal(checked, 0);
    assert.equal(signed, before);
  });
  await test("ordinary function call checks contract", async () => {
    assert.deepEqual(actionPayees([call("register", {})], "app.near"), ["app.near"]);
  });
  await test("deny refuses without signing", async () => {
    const before = signed;
    await assert.rejects(() => guardWallet(wallet, { check: check("deny") }).signAndSendTransaction(transfer("bob.near")), code("SIYAB_DENY"));
    assert.equal(signed, before);
  });
  await test("all other signing paths blocked", async () => {
    const guarded = guardWallet(wallet, { check: check("allow") });
    const before = signed;
    for (const method of ["signTransaction", "signMessage", "createSignedTransaction", "verifyOwner", "signDelegateAction", "signAndSendDelegateAction"]) await assert.rejects(() => guarded[method]({}), code("SIYAB_PAYEE"));
    assert.equal(signed, before);
  });
  await test("recorded verdict ASK requires confirmation and confirmed ask sends", async () => {
    const subject = JSON.parse(ASK.evidence).subject;
    const options = { receiver: subject.receiver, amount: subject.amount, token: "near", fetchImpl: async () => ({ ok: true, json: async () => ASK }) };
    const decision = await preflightAccountCheck(options);
    assert.equal(decision.decision, "ask");
    await assert.rejects(() => guardTransfer(options), code("SIYAB_ASK"));
    const verdictDecision = await guardTransfer({ ...options, confirm: async shown => shown.receiver === subject.receiver });
    assert.equal(verdictDecision.confirmed, true);
    const guarded = guardWallet(wallet, { check: async checkOptions => preflightAccountCheck({ ...checkOptions, amount: subject.amount, token: "near", fetchImpl: options.fetchImpl }), confirm: async shown => shown.receiver === subject.receiver });
    const before = signed;
    assert.equal(await guarded.signAndSendTransaction(transfer(subject.receiver)), "sent");
    assert.equal(signed, before + 1);
  });
  await test("checked transaction snapshot cannot change while awaiting", async () => {
    const params = transfer("bob.near");
    let sent;
    const guarded = guardWallet({ signAndSendTransaction: async transaction => { signed++; sent = transaction; } }, {
      check: async () => { params.receiverId = "evil.near"; params.actions.push({ type: "DeleteAccount", params: {} }); return { decision: "allow" }; },
    });
    await guarded.signAndSendTransaction(params);
    assert.deepEqual(sent, transfer("bob.near"));
  });
  await test("unavailable crypto preserves top level hard refusal", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    try {
      Object.defineProperty(globalThis, "crypto", { value: undefined, configurable: true });
      assert.equal((await decideAccountCheck(DENY, "other.near")).decision, "deny");
      assert.equal((await decideAccountCheck(ALLOW, "intents.near")).decision, "ask");
    } finally {
      Object.defineProperty(globalThis, "crypto", descriptor);
    }
  });
  await test("amounts reject every trailing line terminator and u128 overflow", async () => {
    const guarded = guardWallet(wallet, { check: check("allow") });
    for (const amount of ["1\n", "1\r", "1\u2028", "1\u2029", "340282366920938463463374607431768211456", "9".repeat(1000)]) {
      await assert.rejects(() => guarded.signAndSendTransaction({ receiverId: "token.near", actions: [call("ft_transfer", { receiver_id: "bob.near", amount })] }), code("SIYAB_PAYEE"));
    }
  });
  await test("shared byte arguments cannot change payee during checks", async () => {
    const original = new TextEncoder().encode('{"receiver_id":"clean.near","amount":"1"}');
    const replacement = new TextEncoder().encode('{"receiver_id":"evilx.near","amount":"1"}');
    const args = new Uint8Array(new SharedArrayBuffer(original.length));
    args.set(original);
    let checked = 0;
    const guarded = guardWallet(wallet, { check: async () => { checked++; args.set(replacement); return { decision: "allow" }; } });
    const before = signed;
    await assert.rejects(() => guarded.signAndSendTransaction({ receiverId: "token.near", actions: [call("ft_transfer", args)] }), code("SIYAB_PAYEE"));
    assert.equal(checked, 0);
    assert.equal(signed, before);
  });
  await test("every recipient is checked for mt ft withdrawal and NFT calls", async () => {
    for (const method of ["mt_transfer", "ft_withdraw", "nft_transfer", "nft_transfer_call"]) {
      const checks = [];
      const guarded = guardWallet(wallet, {
        check: async options => { checks.push(options.receiver); return { decision: options.receiver === "drainer.near" ? "deny" : "allow" }; },
        confirm: async () => true,
      });
      const before = signed;
      await assert.rejects(() => guarded.signAndSendTransaction({ receiverId: method.startsWith("nft") ? "nft.near" : "intents.near",
        actions: [call(method, { receiver_id: "drainer.near", amount: "1", token_id: "nft-1", msg: "" })] }), code("SIYAB_DENY"));
      assert.ok(checks.includes("drainer.near"));
      assert.equal(signed, before);
    }
  });
  await test("recipient aliases nested arrays and every named payee are checked", async () => {
    for (const field of ["receiver_id", "account_id", "beneficiary_id", "recipient", "recipient_id", "to", "new_owner_id", "owner_id",
      "receiverId", "accountId", "beneficiaryId", "recipientId", "newOwnerId", "ownerId"]) {
      const checks = [];
      const guarded = guardWallet(wallet, {
        check: async options => { checks.push(options.receiver); return { decision: options.receiver === "drainer.near" ? "deny" : "allow" }; },
        confirm: async () => true,
      });
      const before = signed;
      await assert.rejects(() => guarded.signAndSendTransaction({ receiverId: "intents.near",
        actions: [call("mt_transfer", { transfers: [{ recipient: "clean.near" }, { [field]: "drainer.near" }] })] }), code("SIYAB_DENY"));
      assert.ok(checks.includes("clean.near"));
      assert.ok(checks.includes("drainer.near"));
      assert.equal(signed, before);
    }
    const checked = [];
    const before = signed;
    await assert.rejects(() => guardWallet(wallet, { check: async options => {
      checked.push(options.receiver); return { decision: options.receiver === "drainer.near" ? "deny" : "allow" };
    }, confirm: async () => true }).signAndSendTransaction({ receiverId: "intents.near",
      actions: [call("mt_transfer", { recipients: ["clean.near", "drainer.near"] })] }), code("SIYAB_DENY"));
    assert.ok(checked.includes("clean.near") && checked.includes("drainer.near"));
    assert.equal(signed, before);
    const shared = ["drainer.near"];
    await assert.rejects(() => guardWallet(wallet, { check: async options => ({ decision: options.receiver === "drainer.near" ? "deny" : "allow" }),
      confirm: async () => true }).signAndSendTransaction({ receiverId: "intents.near",
      actions: [call("mt_transfer", { metadata: shared, recipients: shared })] }), code("SIYAB_DENY"));
    assert.equal(signed, before);
  });
  await test("unknown methods ask after allowed recipients and only explicit confirmation sends", async () => {
    const transaction = { receiverId: "app.near", actions: [call("register", { account_id: "clean.near" })] };
    const before = signed;
    await assert.rejects(() => guardWallet(wallet, { check: check("allow") }).signAndSendTransaction(transaction), code("SIYAB_ASK"));
    assert.equal(signed, before);
    const shown = [];
    await guardWallet(wallet, { check: check("allow"), confirm: async decision => { shown.push(decision); return true; } }).signAndSendTransaction(transaction);
    assert.ok(shown.some(decision => decision.receiver === "app.near" && decision.decision === "ask"));
    assert.equal(signed, before + 1);
    await assert.rejects(() => guardWallet(wallet, { check: check("allow"), confirm: async () => "yes" }).signAndSendTransaction(transaction), code("SIYAB_ASK"));
  });
  await test("unreadable arguments ask even if the contract is allowed", async () => {
    const before = signed;
    const invalidUtf8 = Buffer.from('{"receiver_id":"bob.near","amount":"1","note":"X"}');
    invalidUtf8[invalidUtf8.indexOf("X")] = 255;
    for (const method of ["ft_transfer", "register"]) {
      for (const args of [undefined, [], new ArrayBuffer(4), "{", "[]", new Uint8Array([123]), new Uint8Array([255]), invalidUtf8]) {
        await assert.rejects(() => guardWallet(wallet, { check: check("allow") }).signAndSendTransaction({ receiverId: "app.near", actions: [call(method, args)] }), code("SIYAB_ASK"));
      }
    }
    assert.equal(signed, before);
    let shown;
    await guardWallet(wallet, { check: check("allow"), confirm: async decision => { shown = decision; return true; } })
      .signAndSendTransaction({ receiverId: "app.near", actions: [call("register", "{")] });
    assert.equal(shown.decision, "ask");
    assert.equal(signed, before + 1);
  });
  await test("ft_transfer_call message names every onward recipient", async () => {
    for (const msg of [JSON.stringify({ recipient: "drainer.near" }), JSON.stringify({ transfers: [{ receiver_id: "clean.near" }, { to: "drainer.near" }] }),
      ...["receiver", "beneficiary", "destination", "destination_id", "destinationId", "payee", "payee_id", "target", "new_owner", "owner"].map(field => JSON.stringify({ recipient: "clean.near", [field]: "drainer.near" })),
      ...["receiverIds", "accountIds", "beneficiaryIds", "recipientIds", "newOwnerIds", "ownerIds"].map(field => JSON.stringify({ recipient: "clean.near", [field]: ["drainer.near"] })),
      ...["msg", "payload", "args", "data", "message", "instructions"].map(field => JSON.stringify({ recipient: "clean.near", [field]: JSON.stringify({ receiver_id: "drainer.near" }) })),
    ]) {
      const checks = [];
      const guarded = guardWallet(wallet, { check: async options => {
        checks.push(options.receiver); return { decision: options.receiver === "drainer.near" ? "deny" : "allow" };
      }, confirm: async () => true });
      const before = signed;
      await assert.rejects(() => guarded.signAndSendTransaction({ receiverId: "token.near",
        actions: [call("ft_transfer_call", { receiver_id: "clean.near", amount: "1", msg })] }), code("SIYAB_DENY"));
      assert.ok(checks.includes("drainer.near"));
      assert.equal(signed, before);
    }
    for (const msg of [JSON.stringify({ recipient: "other.near" }), JSON.stringify({ receiver_id: "clean.near", deposit_for: "drainer.near" })]) {
      const transaction = { receiverId: "token.near", actions: [call("ft_transfer_call", { receiver_id: "intents.near", amount: "1", msg })] };
      const before = signed;
      await assert.rejects(() => guardWallet(wallet, { check: check("allow") }).signAndSendTransaction(transaction), code("SIYAB_ASK"));
      assert.equal(signed, before);
      let shown = null;
      await guardWallet(wallet, { check: check("allow"), confirm: async decision => { shown = decision; return true; } }).signAndSendTransaction(transaction);
      assert.equal(shown.decision, "ask");
      assert.equal(signed, before + 1);
    }
    const checks = [];
    const before = signed;
    await assert.rejects(() => guardWallet(wallet, { check: async options => { checks.push(options.receiver); return { decision: "allow" }; } })
      .signAndSendTransaction({ receiverId: "token.near", actions: [call("ft_transfer_call", { receiver_id: "clean.near", amount: "1", msg: "intents.near" })] }), code("SIYAB_ASK"));
    assert.ok(checks.includes("intents.near"));
    assert.equal(signed, before);
  });
  await test("nonempty unreadable or accountless FT messages require confirmation", async () => {
    for (const msg of ["opaque", "{}", "[]", "null", "42", '"other.near"', JSON.stringify({ recipient: "bad..near" }), null, undefined,
      ...["msg", "payload", "args", "data", "message", "instructions"].map(field => JSON.stringify({ recipient: "clean.near", [field]: "encoded instructions" })),
    ]) {
      const transaction = { receiverId: "token.near", actions: [call("ft_transfer_call", { receiver_id: "clean.near", amount: "1", msg })] };
      const before = signed;
      await assert.rejects(() => guardWallet(wallet, { check: check("allow") }).signAndSendTransaction(transaction), code("SIYAB_ASK"));
      assert.equal(signed, before);
    }
    const before = signed;
    await guardWallet(wallet, { check: check("allow"), confirm: async decision => decision.decision === "ask" }).signAndSendTransaction({ receiverId: "token.near",
      actions: [call("ft_transfer_call", { receiver_id: "clean.near", amount: "1", msg: "opaque" })] });
    assert.equal(signed, before + 1);
  });
  await test("native Transfer and attached deposits use exact amount verdicts", async () => {
    for (const transaction of [
      { receiverId: "intents.near", actions: [{ type: "Transfer", params: { deposit: "2" } }] },
      { receiverId: "intents.near", actions: [call("register", {})] },
      { receiverId: "intents.near", actions: [call("ft_transfer", { receiver_id: "clean.near", amount: "5" })] },
    ]) {
      if (transaction.actions[0].type === "FunctionCall") transaction.actions[0].params.deposit = "3";
      const requested = [];
      const before = signed;
      const guarded = guardWallet(wallet, { fetchImpl: async url => {
        requested.push(new URL(url));
        return { ok: true, json: async () => ALLOW };
      } });
      await assert.rejects(() => guarded.signAndSendTransaction(transaction), code("SIYAB_ASK"));
      const native = requested.find(url => url.pathname === "/live/verdict" && url.searchParams.get("token") === "near");
      assert.ok(native);
      assert.equal(native.searchParams.get("receiver"), transaction.receiverId);
      assert.equal(native.searchParams.get("amount"), transaction.actions[0].params.deposit);
      assert.equal(signed, before);
    }
    const native = load("langchain", "verdict-allow.json");
    const subject = JSON.parse(native.evidence).subject;
    const before = signed;
    const guarded = guardWallet(wallet, { fetchImpl: async url => ({ ok: true, json: async () => url.includes("/live/verdict?") ? native : ALLOW }) });
    await guarded.signAndSendTransaction({ receiverId: subject.receiver, actions: [{ type: "Transfer", params: { deposit: subject.amount } }] });
    assert.equal(signed, before + 1);
    await assert.rejects(() => guarded.signAndSendTransaction({ receiverId: subject.receiver, actions: [{ type: "Transfer", params: { deposit: "2" } }] }), code("SIYAB_ASK"));
    assert.equal(signed, before + 1);
  });
  await test("signIn checks the contract and snapshots permission parameters", async () => {
    let invoked = 0;
    let received;
    const signInWallet = { signAndSendTransaction: wallet.signAndSendTransaction.bind(wallet), signIn(params) { invoked++; received = params; return ["owner.near"]; } };
    const options = { contractId: "intents.near", methodNames: ["ft_transfer"] };
    const guarded = guardWallet(signInWallet, { check: async decision => {
      assert.equal(decision.receiver, "intents.near");
      options.contractId = "drainer.near"; options.methodNames.push("withdraw");
      return { decision: "allow" };
    } });
    assert.deepEqual(await guarded.signIn(options), ["owner.near"]);
    assert.deepEqual(received, { contractId: "intents.near", methodNames: ["ft_transfer"] });
    assert.equal(invoked, 1);
    for (const decision of ["ask", undefined]) {
      await assert.rejects(() => guardWallet(signInWallet, { check: check(decision) }).signIn({ contractId: "intents.near" }), code("SIYAB_ASK"));
      assert.equal(invoked, 1);
    }
    await assert.rejects(() => guardWallet(signInWallet, { check: check("deny"), confirm: async () => true }).signIn({ contractId: "drainer.near" }), code("SIYAB_DENY"));
    await guardWallet(signInWallet, { check: check("ask"), confirm: async decision => decision.receiver === "intents.near" }).signIn({ contractId: "intents.near", methodNames: [] });
    assert.equal(invoked, 2);
    for (const params of [undefined, {}, { contractId: "bad..near" }, { contractId: "intents.near", methodNames: "ft_transfer" }]) {
      await assert.rejects(() => guardWallet(signInWallet, { check: check("allow") }).signIn(params), code("SIYAB_PAYEE"));
    }
    assert.equal(invoked, 2);
  });
  await test("wallet options cannot supply unrelated amounts to account checks", async () => {
    const requests = [];
    const before = signed;
    await guardWallet(wallet, { amount: "7", token: "near", confirm: async decision => decision.decision === "ask", check: async options => {
      requests.push([options.receiver, options.amount, options.token]); return { decision: "allow" };
    } }).signAndSendTransaction({ receiverId: "token.near", actions: [{ ...call("ft_transfer_call", {}), params: {
      ...call("ft_transfer_call", {}).params, deposit: "0", args: { receiver_id: "clean.near", amount: "1", msg: JSON.stringify({ recipient: "other.near" }) },
    } }] });
    assert.deepEqual(requests, [["token.near", undefined, undefined], ["clean.near", "1", "token.near"], ["other.near", undefined, undefined]]);
    assert.equal(signed, before + 1);
  });
  if (failures.length) throw new AggregateError(failures, failures.length + " wallet-selector scenarios failed");
  process.stdout.write("wallet-selector adapter: " + count + " tests passed; signed=" + signed + "\n");
}
run().catch(error => { process.stderr.write(String(error.stack || error) + "\n"); process.exitCode = 1; });
