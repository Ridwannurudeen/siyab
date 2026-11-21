"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const FIXTURES = path.join(ROOT, "integrations", "wallet-selector", "fixtures");
const load = name => JSON.parse(fs.readFileSync(path.join(FIXTURES, name), "utf8"));
const ALLOW = load("account-check-allow.json");
const ASK = load("account-check-ask.json");
const DENY = load("account-check-deny.json");
const COLLECTOR = "31ac7a2705a0686ff427b1a52d3ffd1fcfaa4b1f3cb3e83a0f767494e724a540";

const checkFor = decision => async () => ({ ...decision, reasons: decision.reasons || [] });

async function run() {
  const {
    actionPayees,
    decideAccountCheck,
    guardTransfer,
    guardWallet,
    messageFor,
    preflightAccountCheck,
  } = await import(path.join(ROOT, "integrations", "wallet-selector", "adapter.mjs"));

  // Recorded answers.
  assert.equal((await decideAccountCheck(ALLOW, "intents.near")).decision, "allow");
  const asked = await decideAccountCheck(ASK, "hello.near");
  assert.equal(asked.decision, "ask");
  assert.match(asked.reasons.join(" "), /full access key/);
  assert.equal((await decideAccountCheck(DENY, COLLECTOR)).decision, "deny");

  // A denial is honoured before binding.
  assert.equal((await decideAccountCheck(DENY, "someone-else.near")).decision, "deny");
  assert.equal((await decideAccountCheck({ outcome: "DENY", reasons: ["A signal."] }, "x.near")).decision, "deny");

  // Evidence must be readable and belong to the account.
  const edited = { ...ALLOW, evidence_hash: "0".repeat(64) };
  assert.equal((await decideAccountCheck(edited, "intents.near")).decision, "ask");
  assert.equal((await decideAccountCheck(ALLOW, "another.near")).decision, "ask");
  const noEvidence = { ...ALLOW };
  delete noEvidence.evidence;
  assert.equal((await decideAccountCheck(noEvidence, "intents.near")).decision, "ask");
  assert.equal((await decideAccountCheck({ outcome: "MAYBE" }, "intents.near")).decision, "ask");
  assert.equal((await decideAccountCheck(ALLOW, "Not An Account")).decision, "ask");

  // An allow must be complete and come from the unsigned route.
  assert.equal((await decideAccountCheck({ ...ALLOW, action: "ask" }, "intents.near")).decision, "ask");
  assert.equal((await decideAccountCheck({ ...ALLOW, signing: "sometimes" }, "intents.near")).decision, "ask");
  const failed = JSON.parse(JSON.stringify(ALLOW));
  const document = JSON.parse(failed.evidence);
  document.checks[0].status = "unknown";
  failed.evidence = JSON.stringify(document);
  const crypto = require("node:crypto");
  failed.evidence_hash = crypto.createHash("sha256").update(failed.evidence).digest("hex");
  assert.equal((await decideAccountCheck(failed, "intents.near")).decision, "ask");

  // The message wording.
  assert.equal(messageFor({ decision: "ask", reasons: ["A reason."] }, "shown.near"),
    "Siyab could not clear shown.near. A reason. Continue only if you know this account.");

  // A deny throws before any sign call.
  let signed = 0;
  await assert.rejects(
    () => guardTransfer({ receiver: COLLECTOR, check: checkFor({ decision: "deny" }) }),
    error => error.code === "SIYAB_DENY" && error.name === "SiyabRefusal");
  assert.equal(signed, 0);

  // An ask cannot be skipped.
  await assert.rejects(
    () => guardTransfer({ receiver: "hello.near", check: checkFor({ decision: "ask", reasons: ["Careful."] }) }),
    error => error.code === "SIYAB_ASK" && error.name === "SiyabConfirmationRequired");
  await assert.rejects(
    () => guardTransfer({ receiver: "hello.near", confirm: async () => false,
      check: checkFor({ decision: "ask" }) }),
    error => error.code === "SIYAB_ASK");
  const confirmed = await guardTransfer({ receiver: "hello.near", confirm: async () => true,
    check: checkFor({ decision: "ask" }) });
  assert.equal(confirmed.confirmed, true);
  assert.equal(confirmed.decision, "ask");

  // The allow path resolves.
  assert.equal((await guardTransfer({ receiver: "intents.near", check: checkFor({ decision: "allow" }) })).decision,
    "allow");

  // Preflight failures are ask.
  const failedFetch = await preflightAccountCheck({ receiver: "a.near", fetchImpl: async () => ({ ok: false, status: 503 }) });
  assert.equal(failedFetch.decision, "ask");
  assert.match(failedFetch.reasons[0], /503/);
  assert.equal((await preflightAccountCheck({ receiver: "a.near", fetchImpl: async () => { throw new Error("offline"); } })).decision,
    "ask");
  assert.equal((await preflightAccountCheck({ receiver: "intents.near",
    fetchImpl: async () => ({ ok: true, json: async () => ALLOW }) })).decision, "allow");

  // Payee extraction.
  assert.deepEqual(actionPayees([{ type: "Transfer", params: { receiverId: "bob.near" } }]), ["bob.near"]);
  const calls = [{
    type: "FunctionCall",
    params: {
      receiverId: "usdt.tether-token.near",
      methodName: "ft_transfer",
      args: Buffer.from(JSON.stringify({ receiver_id: "carol.near", amount: "1" })).toString("base64"),
    },
  }];
  assert.deepEqual(actionPayees(calls), ["carol.near"]);
  assert.deepEqual(actionPayees([{ type: "FunctionCall", params: { receiverId: "app.near", methodName: "register" } }]),
    ["app.near"]);
  assert.deepEqual(actionPayees([{ type: "Transfer", params: { receiverId: "dave.near" } },
    { type: "FunctionCall", params: { receiverId: "token.near", methodName: "ft_transfer",
      args: { receiver_id: "erin.near" } } }]), ["dave.near", "erin.near"]);

  // The wallet wrapper signs only after a clean check, and reads the real payee.
  const checks = [];
  const signedParams = [];
  const wallet = {
    signAndSendTransaction: async params => { signedParams.push(params); return "sent"; },
    signAndSendTransactions: async params => { signedParams.push(params); return "sent-many"; },
  };
  const guard = guardWallet(wallet, { check: async options => { checks.push(options.receiver); return { decision: "allow", reasons: [] }; } });
  assert.equal(await guard.signAndSendTransaction({ actions: [{ type: "Transfer", params: { receiverId: "bob.near" } }] }), "sent");
  assert.deepEqual(checks, ["bob.near"]);
  assert.equal(signedParams.length, 1);

  checks.length = 0;
  signedParams.length = 0;
  const batch = guardWallet(wallet, { check: async options => { checks.push(options.receiver); return { decision: "allow", reasons: [] }; } });
  await batch.signAndSendTransactions({ transactions: [
    { actions: [{ type: "Transfer", params: { receiverId: "bob.near" } }] },
    { actions: [calls[0]] },
  ] });
  assert.deepEqual(checks, ["bob.near", "carol.near"]);
  assert.equal(signedParams.length, 1);

  await assert.rejects(() => guard.signAndSendTransaction({ actions: [] }), error => error.code === "SIYAB_PAYEE");

  const blocked = guardWallet(wallet, { check: checkFor({ decision: "deny" }) });
  const before = signedParams.length;
  await assert.rejects(() => blocked.signAndSendTransaction({ actions: [{ type: "Transfer", params: { receiverId: COLLECTOR } }] }),
    error => error.code === "SIYAB_DENY");
  assert.equal(signedParams.length, before);

  process.stdout.write("wallet-selector adapter: ok\n");
}

run().catch(error => {
  process.stderr.write(String(error && error.stack || error) + "\n");
  process.exit(1);
});
