"use strict";

// Reference preflight for a NEAR wallet transfer flow.
//
// The adapter calls the hosted account check before the wallet signs anything and
// maps its answer to one of three decisions: allow, ask or deny. It adds no
// detection logic of its own and never treats an answer as permission to pay.

const crypto = require("node:crypto");

const DEFAULT_BASE_URL = "https://siyab.xyz";
const DEFAULT_TIMEOUT_MS = 8000;

function sha256(text) {
  return crypto.createHash("sha256").update(text, "utf8").digest("hex");
}

function stringReasons(answer) {
  if (!answer || !Array.isArray(answer.reasons)) return [];
  return answer.reasons.filter(reason => typeof reason === "string" && reason.length > 0);
}

// The evidence string is the canonical evidence document and its sha256 is the
// answer's evidence_hash. Parsing it proves the answer was not edited in transit
// and gives the subject the answer was produced for.
function parseEvidence(answer) {
  if (!answer || typeof answer !== "object") return null;
  if (typeof answer.evidence !== "string" || typeof answer.evidence_hash !== "string") return null;
  if (sha256(answer.evidence) !== answer.evidence_hash) return null;
  try {
    const document = JSON.parse(answer.evidence);
    return document && typeof document === "object" && !Array.isArray(document) ? document : null;
  } catch (error) {
    return null;
  }
}

function answerForAccount(answer, receiver) {
  const document = parseEvidence(answer);
  if (!document) return "The check answer could not be verified.";
  const subject = document.subject;
  if (!subject || subject.kind !== "account" || subject.account !== receiver) {
    return "The check answer is for a different account.";
  }
  return null;
}

function decideAccountCheck(answer, receiver) {
  if (typeof receiver !== "string" || receiver.length === 0) {
    return { decision: "ask", reasons: ["The receiver account is missing."] };
  }
  if (!answer || typeof answer !== "object") {
    return { decision: "ask", reasons: ["The check did not return an answer."] };
  }
  const mismatch = answerForAccount(answer, receiver);
  if (mismatch) return { decision: "ask", reasons: [mismatch] };
  const document = parseEvidence(answer);
  const expected = answer.outcome === "ASK" ? "UNKNOWN" : answer.outcome;
  if (document.verdict !== expected) {
    return { decision: "ask", reasons: ["The check answer does not match its evidence."] };
  }

  const reasons = stringReasons(answer);
  const evidenceHash = answer.evidence_hash;

  if (answer.outcome === "DENY") {
    return {
      decision: "deny",
      reasons: reasons.length > 0 ? reasons : ["The check found a refusal signal."],
      evidenceHash,
    };
  }
  if (answer.outcome !== "ALLOW") {
    return {
      decision: "ask",
      reasons: reasons.length > 0 ? reasons : ["The check did not clear this account."],
      evidenceHash,
    };
  }

  // ALLOW is advice from an unsigned hosted route. It clears the account only when
  // every check ran and passed and the answer says what it is.
  const checks = Array.isArray(answer.checks) ? answer.checks : [];
  if (checks.length === 0 || checks.some(check => !check || check.status !== "pass")) {
    return { decision: "ask", reasons: ["The check answer is incomplete."], evidenceHash };
  }
  if (answer.signing !== "never" || answer.verdict !== null) {
    return { decision: "ask", reasons: ["The check answer was not produced by the unsigned route."], evidenceHash };
  }
  if (answer.action !== "sign") {
    return {
      decision: "ask",
      reasons: reasons.length > 0 ? reasons : ["The check requires a decision before sending."],
      evidenceHash,
    };
  }
  return { decision: "allow", reasons, evidenceHash };
}

function messageFor(decision, receiver) {
  const reasons = decision.reasons && decision.reasons.length > 0 ? decision.reasons.join(" ") : "";
  if (decision.decision === "deny") {
    return "Siyab refused this transfer to " + receiver + ". " + reasons;
  }
  if (decision.decision === "ask") {
    return "Siyab could not clear " + receiver + ". " + reasons +
      " Continue only if you know this account.";
  }
  return "Siyab found no refusal signals for " + receiver +
    ". This is advice, not a guarantee that the transfer is what it appears to be.";
}

async function preflightAccountCheck(options) {
  const receiver = options.receiver;
  const baseUrl = (options.baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "");
  const fetchImpl = options.fetchImpl || globalThis.fetch;
  const timeoutMs = options.timeoutMs || DEFAULT_TIMEOUT_MS;
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;

  try {
    const response = await fetchImpl(baseUrl + "/live/account-check/" + encodeURIComponent(receiver), {
      method: "GET",
      headers: { accept: "application/json" },
      signal: controller ? controller.signal : undefined,
    });
    if (!response.ok) {
      return { decision: "ask", reasons: ["The check is unavailable (HTTP " + response.status + ")."] };
    }
    const answer = await response.json();
    return decideAccountCheck(answer, receiver);
  } catch (error) {
    return { decision: "ask", reasons: ["The check could not be reached."] };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// Call this before wallet.signAndSendTransaction or any transfer form submission.
// A deny throws, so a caller that forgets to branch still cannot sign. An ask is
// returned for the caller to show to the person; never treat it as approval.
async function guardTransfer(options) {
  const receiver = options.receiver;
  const decision = await preflightAccountCheck(options);
  decision.message = messageFor(decision, receiver);
  if (decision.decision === "deny") {
    const error = new Error(decision.message);
    error.name = "SiyabRefusal";
    error.code = "SIYAB_DENY";
    error.reasons = decision.reasons;
    error.evidenceHash = decision.evidenceHash;
    throw error;
  }
  return decision;
}

module.exports = {
  DEFAULT_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  decideAccountCheck,
  guardTransfer,
  messageFor,
  parseEvidence,
  preflightAccountCheck,
};
