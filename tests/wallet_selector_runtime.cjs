"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  decideAccountCheck,
  guardTransfer,
  messageFor,
} = require(path.join(__dirname, "..", "integrations", "wallet-selector", "adapter.js"));

const FIXTURES = path.join(__dirname, "..", "integrations", "wallet-selector", "fixtures");
const load = name => JSON.parse(fs.readFileSync(path.join(FIXTURES, name), "utf8"));

const allow = load("account-check-allow.json");
const ask = load("account-check-ask.json");
const deny = load("account-check-deny.json");

function testRecordedAnswers() {
  assert.equal(decideAccountCheck(allow, "intents.near").decision, "allow");
  const askDecision = decideAccountCheck(ask, "hello.near");
  assert.equal(askDecision.decision, "ask");
  assert.match(askDecision.reasons.join(" "), /full access key/);
  const denyDecision = decideAccountCheck(deny, "31ac7a2705a0686ff427b1a52d3ffd1fcfaa4b1f3cb3e83a0f767494e724a540");
  assert.equal(denyDecision.decision, "deny");
  assert.ok(denyDecision.reasons.length > 0);
  assert.equal(denyDecision.evidenceHash, deny.evidence_hash);
}

function testBinding() {
  assert.equal(decideAccountCheck(allow, "another.near").decision, "ask");
  const edited = { ...allow, outcome: "DENY" };
  assert.equal(decideAccountCheck(edited, "intents.near").decision, "ask");
  const noEvidence = { ...allow };
  delete noEvidence.evidence;
  assert.equal(decideAccountCheck(noEvidence, "intents.near").decision, "ask");
}

function testAllowMustBeComplete() {
  const actionAsk = { ...allow, action: "ask" };
  assert.equal(decideAccountCheck(actionAsk, "intents.near").decision, "ask");
  const notNever = { ...allow, signing: "sometimes" };
  assert.equal(decideAccountCheck(notNever, "intents.near").decision, "ask");
  const failedCheck = JSON.parse(JSON.stringify(allow));
  failedCheck.checks[0].status = "unknown";
  assert.equal(decideAccountCheck(failedCheck, "intents.near").decision, "ask");
  assert.equal(decideAccountCheck(null, "intents.near").decision, "ask");
  assert.equal(decideAccountCheck(allow, "").decision, "ask");
}

async function testUnavailableIsAsk() {
  const denied = await guardTransfer({
    receiver: "intents.near",
    fetchImpl: async () => ({ ok: false, status: 503 }),
  });
  assert.equal(denied.decision, "ask");
  assert.match(denied.message, /unavailable/);

  const thrown = await guardTransfer({
    receiver: "intents.near",
    fetchImpl: async () => { throw new Error("offline"); },
  });
  assert.equal(thrown.decision, "ask");
  assert.match(thrown.message, /could not be reached/);
}

async function testDenyThrows() {
  let caught = null;
  try {
    await guardTransfer({
      receiver: "31ac7a2705a0686ff427b1a52d3ffd1fcfaa4b1f3cb3e83a0f767494e724a540",
      fetchImpl: async () => ({ ok: true, status: 200, json: async () => deny }),
    });
  } catch (error) {
    caught = error;
  }
  assert.ok(caught);
  assert.equal(caught.code, "SIYAB_DENY");
  assert.equal(caught.name, "SiyabRefusal");
  assert.ok(Array.isArray(caught.reasons) && caught.reasons.length > 0);
}

async function testAllowReturnsDecision() {
  const decision = await guardTransfer({
    receiver: "intents.near",
    fetchImpl: async () => ({ ok: true, status: 200, json: async () => allow }),
  });
  assert.equal(decision.decision, "allow");
  assert.match(decision.message, /advice/);
  assert.equal(messageFor({ decision: "ask", reasons: ["A reason."] }, "shown.near"),
    "Siyab could not clear shown.near. A reason. Continue only if you know this account.");
}

(async () => {
  testRecordedAnswers();
  testBinding();
  testAllowMustBeComplete();
  await testUnavailableIsAsk();
  await testDenyThrows();
  await testAllowReturnsDecision();
  process.stdout.write("wallet-selector adapter: ok\n");
})().catch(error => {
  process.stderr.write(String(error && error.stack || error) + "\n");
  process.exit(1);
});
