// Reference guard for a NEAR wallet transfer flow, for the browser and Node 18+.
//
// The adapter calls the hosted account check before the wallet signs anything and
// maps its answer to allow, ask or deny. It adds no detection logic of its own and
// never treats an answer as permission to pay.

const DEFAULT_BASE_URL = "https://siyab.xyz";
const DEFAULT_TIMEOUT_MS = 8000;
const ACCOUNT = /^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$/;

function accountOrNull(value) {
  return typeof value === "string" && ACCOUNT.test(value) ? value : null;
}

function strings(value) {
  return Array.isArray(value) ? value.filter(item => typeof item === "string" && item.length > 0) : [];
}

async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

// The evidence string is the canonical evidence document and its sha256 is the
// answer's evidence_hash. The hash is unsigned: matching it shows the answer is
// internally consistent, not that a particular server produced it.
async function parseEvidence(answer) {
  if (!answer || typeof answer !== "object") return null;
  if (typeof answer.evidence !== "string" || typeof answer.evidence_hash !== "string") return null;
  if (await sha256Hex(answer.evidence) !== answer.evidence_hash) return null;
  try {
    const document = JSON.parse(answer.evidence);
    return document && typeof document === "object" && !Array.isArray(document) ? document : null;
  } catch (error) {
    return null;
  }
}

function outcomeOf(answer) {
  if (!answer || typeof answer !== "object" || typeof answer.outcome !== "string") return null;
  const value = answer.outcome.trim().toUpperCase();
  if (value === "UNKNOWN") return "ASK";
  return ["ALLOW", "DENY", "ASK"].includes(value) ? value : null;
}

async function decideAccountCheck(answer, receiver) {
  if (!accountOrNull(receiver)) {
    return { decision: "ask", reasons: ["The receiver account is not a valid account id."] };
  }
  const outcome = outcomeOf(answer);
  if (!outcome) {
    return { decision: "ask", reasons: ["The check answer has no usable outcome."] };
  }
  const topLevelReasons = strings(answer.reasons);
  if (outcome === "DENY") {
    // A denial blocks a payment, so it is honoured before any binding check.
    return {
      decision: "deny",
      reasons: topLevelReasons.length > 0 ? topLevelReasons : ["The check refused this transfer."],
    };
  }

  const document = await parseEvidence(answer);
  if (!document) {
    return { decision: "ask", reasons: ["The check answer could not be read."] };
  }
  const subject = document.subject;
  if (!subject || subject.kind !== "account" || subject.account !== receiver) {
    return { decision: "ask", reasons: ["The check answer is for a different account."] };
  }
  const expected = outcome === "ASK" ? "UNKNOWN" : outcome;
  if (document.verdict !== expected) {
    return { decision: "ask", reasons: ["The check answer does not match its evidence."] };
  }
  const reasons = strings(document.reasons);
  const shown = reasons.length > 0 ? reasons : topLevelReasons;

  if (outcome === "ASK") {
    return { decision: "ask", reasons: shown.length > 0 ? shown : ["The check did not clear this account."] };
  }

  const checks = Array.isArray(document.checks) ? document.checks : [];
  if (checks.length === 0 || checks.some(check => !check || check.status !== "pass")) {
    return { decision: "ask", reasons: ["The check answer is incomplete."] };
  }
  if (answer.signing !== "never" || answer.verdict !== null) {
    return { decision: "ask", reasons: ["The check answer did not come from the unsigned route."] };
  }
  if (answer.action !== "sign") {
    return { decision: "ask", reasons: shown.length > 0 ? shown : ["The check needs a decision before sending."] };
  }
  return { decision: "allow", reasons: shown };
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
  if (!accountOrNull(receiver)) {
    return { decision: "ask", reasons: ["The receiver account is not a valid account id."] };
  }
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
    return await decideAccountCheck(answer, receiver);
  } catch (error) {
    return { decision: "ask", reasons: ["The check could not be reached."] };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function refusal(message, code, decision) {
  const error = new Error(message);
  error.name = code === "SIYAB_ASK" ? "SiyabConfirmationRequired" : "SiyabRefusal";
  error.code = code;
  error.reasons = decision.reasons || [];
  return error;
}

// Call this before signing. A deny throws SiyabRefusal. An ask throws
// SiyabConfirmationRequired unless confirm(decision) returns true, so an ask
// cannot be skipped by a caller that only branches on allow.
async function guardTransfer(options) {
  const receiver = options.receiver;
  const check = options.check || preflightAccountCheck;
  const decision = await check(options);
  decision.message = messageFor(decision, receiver);
  if (decision.decision === "deny") {
    throw refusal(decision.message, "SIYAB_DENY", decision);
  }
  if (decision.decision === "ask") {
    let approved = false;
    if (typeof options.confirm === "function") {
      try {
        approved = (await options.confirm(decision)) === true;
      } catch (error) {
        approved = false;
      }
    }
    if (!approved) {
      throw refusal(decision.message, "SIYAB_ASK", decision);
    }
    decision.confirmed = true;
  }
  return decision;
}

function decodeArgs(args) {
  if (!args) return null;
  if (typeof args === "object" && !(args instanceof Uint8Array)) return args;
  let text = null;
  if (typeof args === "string") {
    try {
      return JSON.parse(args);
    } catch (error) {
      text = args;
    }
  } else if (args instanceof Uint8Array) {
    text = new TextDecoder().decode(args);
  }
  if (typeof text !== "string") return null;
  try {
    const binary = typeof atob === "function" ? atob(text) : Buffer.from(text, "base64").toString("binary");
    const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch (error) {
    return null;
  }
}

// The real payee: the Transfer receiver, or receiver_id inside ft_transfer and
// ft_transfer_call. Any other function call pays the contract it calls.
function actionPayees(actions) {
  const payees = [];
  const add = value => {
    const account = accountOrNull(value);
    if (account && !payees.includes(account)) payees.push(account);
  };
  for (const action of Array.isArray(actions) ? actions : []) {
    if (!action || typeof action !== "object") continue;
    const type = String(action.type || action.action_type || action.action || "").toUpperCase();
    const params = action.params || {};
    if (type === "TRANSFER") {
      add(params.receiverId ?? params.receiver_id ?? (action.transfer && action.transfer.receiver_id));
    } else if (type === "FUNCTIONCALL") {
      const method = params.methodName ?? params.method_name ?? action.method;
      const contract = params.receiverId ?? params.receiver_id;
      if (method === "ft_transfer" || method === "ft_transfer_call") {
        const args = decodeArgs(params.args ?? action.args);
        add(args && args.receiver_id);
      } else {
        add(contract);
      }
    }
  }
  return payees;
}

function payeeError() {
  const error = new Error("No payee could be read from the actions. Refusing to sign.");
  error.name = "SiyabPayeeError";
  error.code = "SIYAB_PAYEE";
  return error;
}

// Wrap a Wallet Selector wallet so every transfer is checked before signing.
// options.check injects a different preflight in tests; options.confirm is the
// person's answer for an ask.
function guardWallet(wallet, options = {}) {
  if (!wallet || typeof wallet.signAndSendTransaction !== "function") {
    throw new TypeError("A wallet with signAndSendTransaction is required.");
  }
  const run = async payees => {
    if (payees.length === 0) throw payeeError();
    for (const payee of payees) {
      await guardTransfer({ ...options, receiver: payee });
    }
  };
  return {
    async signAndSendTransaction(params) {
      const payees = actionPayees(params && params.actions);
      await run(payees);
      return wallet.signAndSendTransaction(params);
    },
    async signAndSendTransactions(params) {
      const transactions = (params && params.transactions) || [];
      const payees = [];
      for (const transaction of transactions) {
        for (const payee of actionPayees(transaction && transaction.actions)) {
          if (!payees.includes(payee)) payees.push(payee);
        }
      }
      await run(payees);
      return wallet.signAndSendTransactions(params);
    },
  };
}

export {
  DEFAULT_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  actionPayees,
  decideAccountCheck,
  guardTransfer,
  guardWallet,
  messageFor,
  parseEvidence,
  preflightAccountCheck,
};
