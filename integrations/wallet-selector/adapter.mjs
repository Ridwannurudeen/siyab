// Reference guard for a NEAR wallet transfer flow.
//
// The adapter calls hosted account and transfer checks before the wallet signs and
// maps its answer to allow, ask or deny. It adds no detection logic of its own and
// never treats an answer as permission to pay.

const DEFAULT_BASE_URL = "https://siyab.xyz";
const DEFAULT_TIMEOUT_MS = 8000;
const ACCOUNT = /^[a-z0-9]+(?:[-_][a-z0-9]+)*(?:\.[a-z0-9]+(?:[-_][a-z0-9]+)*)*(?![\s\S])/;
const RECIPIENT_FIELDS = new Set([
  "receiver_id", "account_id", "beneficiary_id", "recipient", "recipient_id", "to", "new_owner_id", "owner_id",
  "receiverId", "accountId", "beneficiaryId", "recipientId", "newOwnerId", "ownerId",
  "receiver_ids", "account_ids", "beneficiary_ids", "recipients", "recipient_ids", "new_owner_ids", "owner_ids",
  "receiverIds", "accountIds", "beneficiaryIds", "recipientIds", "newOwnerIds", "ownerIds",
  "receiver", "receivers", "beneficiary", "beneficiaries", "destination", "destinations", "destination_id", "destinationId",
  "destination_ids", "destinationIds", "payee", "payees", "payee_id", "payeeId", "payee_ids", "payeeIds",
  "target", "targets", "target_id", "targetId", "target_ids", "targetIds", "new_owner", "newOwner", "owner", "owners",
]);
const INSTRUCTION_FIELDS = new Set(["msg", "payload", "args", "data", "message", "instructions"]);

function accountOrNull(value) {
  return typeof value === "string" && value.length >= 2 && value.length <= 64 && ACCOUNT.test(value) ? value : null;
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
  try {
    if (await sha256Hex(answer.evidence) !== answer.evidence_hash) return null;
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

async function decide(answer, subjectCheck) {
  const document = await parseEvidence(answer);
  const reasons = strings(document && document.reasons);
  const checks = document && Array.isArray(document.checks) ? document.checks : [];
  if (document && (document.verdict === "DENY" || checks.some(check => check && check.status === "signal"))) {
    return { decision: "deny", reasons: reasons.length ? reasons : ["The evidence refused this transfer."] };
  }
  const outcome = outcomeOf(answer);
  if (!outcome) {
    return { decision: "ask", reasons: ["The check answer has no usable outcome."] };
  }
  if (outcome === "DENY") {
    // A denial blocks a payment, so it is honoured before any binding check.
    return {
      decision: "deny",
      reasons: reasons.length > 0 ? reasons : ["The check refused this transfer."],
    };
  }

  if (!document) {
    return { decision: "ask", reasons: ["The check answer could not be read."] };
  }
  const subject = document.subject;
  if (!subject || !subjectCheck(subject)) {
    return { decision: "ask", reasons: ["The check answer is for a different subject."] };
  }
  const expected = outcome === "ASK" ? "UNKNOWN" : outcome;
  if (document.verdict !== expected) {
    return { decision: "ask", reasons: ["The check answer does not match its evidence."] };
  }
  const shown = reasons;

  if (outcome === "ASK") {
    return { decision: "ask", reasons: shown.length > 0 ? shown : ["The check did not clear this account."] };
  }

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

async function decideAccountCheck(answer, receiver) {
  return decide(answer, subject => accountOrNull(receiver) && subject.kind === "account" && subject.account === receiver);
}

function validAmount(value) {
  return typeof value === "string" && value.length <= 39 && /^[1-9][0-9]*(?![\s\S])/.test(value) && BigInt(value) <= (1n << 128n) - 1n;
}

async function decidePayment(answer, receiver, amount, token) {
  return decide(answer, subject => accountOrNull(receiver) && (token === "near" || accountOrNull(token)) && validAmount(amount) &&
    subject.kind === (token === "near" ? "near" : "ft") && subject.receiver === receiver && subject.amount === amount &&
    subject.token === (token === "near" ? null : token) && subject.memo_sha256 === null);
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
    if (options.amount !== undefined && (!validAmount(options.amount) || (options.token !== "near" && !accountOrNull(options.token)))) {
      return { decision: "ask", reasons: ["The token transfer fields are invalid."] };
    }
    const route = options.amount === undefined ? "/live/account-check/" + encodeURIComponent(receiver) :
      "/live/verdict?" + new URLSearchParams({ receiver, amount: options.amount, token: options.token });
    const response = await fetchImpl(baseUrl + route, {
      method: "GET",
      headers: { accept: "application/json" },
      signal: controller ? controller.signal : undefined,
    });
    if (!response.ok) {
      return { decision: "ask", reasons: ["The check is unavailable (HTTP " + response.status + ")."] };
    }
    const answer = await response.json();
    return options.amount === undefined ? await decideAccountCheck(answer, receiver) :
      await decidePayment(answer, receiver, options.amount, options.token);
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
  const result = await check(options);
  const decision = { ...result, receiver, reasons: strings(result && result.reasons) };
  if (decision.decision !== "allow" && decision.decision !== "deny") decision.decision = "ask";
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
  const record = value => value && typeof value === "object" &&
    (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null) ? value : null;
  if (record(args)) return args;
  if (Array.isArray(args) || args instanceof ArrayBuffer) return null;
  let text = null;
  if (typeof args === "string") {
    try {
      return record(JSON.parse(args));
    } catch (error) {
      text = args;
    }
  } else if (args instanceof Uint8Array) {
    if (!(args.buffer instanceof ArrayBuffer)) return null;
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(args);
    } catch (error) {
      return null;
    }
    try {
      return record(JSON.parse(text));
    } catch (error) {
      // Some callers supply base64 text in bytes.
    }
  }
  if (typeof text !== "string") return null;
  try {
    const binary = typeof atob === "function" ? atob(text) : Buffer.from(text, "base64").toString("binary");
    const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
    return record(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)));
  } catch (error) {
    return null;
  }
}

function recipientAccounts(value) {
  const accounts = new Set();
  const seen = [new Set(), new Set(), new Set(), new Set()];
  const pending = [[value, false, false]];
  let unreadable = false;
  while (pending.length) {
    const [item, recipient, instruction] = pending.pop();
    if (typeof item === "string") {
      if (recipient && accountOrNull(item)) { accounts.add(item); continue; }
      if (recipient) unreadable = true;
      try {
        const nested = JSON.parse(item);
        if (nested && typeof nested === "object") pending.push([nested, false, false]);
        else if (instruction && item !== "") unreadable = true;
      } catch (error) {
        if (instruction && item !== "") unreadable = true;
      }
    } else if (item && typeof item === "object") {
      const visited = seen[(recipient ? 1 : 0) + (instruction ? 2 : 0)];
      if (visited.has(item)) { unreadable = true; continue; }
      visited.add(item);
      if (Array.isArray(item)) {
        for (let index = item.length - 1; index >= 0; index--) pending.push([item[index], recipient, instruction]);
      } else {
        if (recipient) unreadable = true;
        const entries = Object.entries(item);
        for (let index = entries.length - 1; index >= 0; index--) {
          const [key, child] = entries[index];
          pending.push([child, RECIPIENT_FIELDS.has(key), INSTRUCTION_FIELDS.has(key)]);
        }
      }
    } else if (recipient || instruction) unreadable = true;
  }
  return { accounts: [...accounts], unreadable };
}

// Check the contract, each attached native amount and every named recipient.
// Only the two FT methods have a supported transfer shape. No other method is
// allowlisted as having no recipients, so its effects always need confirmation.
function transactionChecks(transaction) {
  if (!transaction || !accountOrNull(transaction.receiverId) || !Array.isArray(transaction.actions) || !transaction.actions.length) throw payeeError();
  const checks = [{ receiver: transaction.receiverId }];
  const unresolved = [];
  for (const action of transaction.actions) {
    if (!action || !action.params || Object.getPrototypeOf(action.params) !== Object.prototype) throw payeeError();
    if (action.type !== "Transfer" && action.type !== "FunctionCall") throw payeeError();
    const deposit = action.params.deposit;
    if (deposit !== "0" && !validAmount(deposit)) throw payeeError();
    if (deposit !== "0") checks.push({ receiver: transaction.receiverId, token: "near", amount: deposit });
    if (action.type === "Transfer") {
      continue;
    }
    const { methodName, args } = action.params;
    if (typeof methodName !== "string" || !methodName.length ||
        args instanceof Uint8Array && !(args.buffer instanceof ArrayBuffer)) throw payeeError();
    const decoded = decodeArgs(args);
    if (!decoded) {
      unresolved.push("The function arguments could not be read, so its recipients are unknown.");
      continue;
    }
    const recipients = recipientAccounts(decoded);
    if (methodName === "ft_transfer" || methodName === "ft_transfer_call") {
      if (!accountOrNull(decoded.receiver_id) || !validAmount(decoded.amount)) throw payeeError();
      checks.push({ receiver: decoded.receiver_id, token: transaction.receiverId, amount: decoded.amount });
    } else {
      unresolved.push("The called method's recipients and effects are not fully checked.");
    }
    for (const receiver of recipients.accounts) {
      if ((methodName === "ft_transfer" || methodName === "ft_transfer_call") && receiver === decoded.receiver_id) continue;
      checks.push({ receiver });
    }
    if (recipients.unreadable) unresolved.push("A recipient or instruction field could not be read.");
    if (methodName === "ft_transfer_call" || Object.prototype.hasOwnProperty.call(decoded, "msg")) {
      if (decoded.msg === "") continue;
      let message = null;
      if (typeof decoded.msg === "string") {
        try { message = JSON.parse(decoded.msg); } catch (error) { message = null; }
      }
      const onward = recipientAccounts(message);
      if (message === null && accountOrNull(decoded.msg)) onward.accounts.push(decoded.msg);
      for (const receiver of onward.accounts) {
        if (!recipients.accounts.includes(receiver)) checks.push({ receiver });
      }
      unresolved.push("The transfer message can name onward recipients that are not all checked.");
    }
  }
  if (unresolved.length) checks.push({ receiver: transaction.receiverId, reasons: [...new Set(unresolved)] });
  return checks;
}

function actionPayees(actions, receiverId) {
  return [...new Set(transactionChecks({ actions, receiverId }).map(check => check.receiver))];
}

function payeeError() {
  const error = new Error("The transaction has an unsupported signing path, action or unreadable payee. Refusing to sign.");
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
  const run = async transactions => {
    if (!Array.isArray(transactions) || !transactions.length) throw payeeError();
    const checks = [];
    for (const transaction of transactions) checks.push(...transactionChecks(transaction));
    for (const check of checks) {
      await guardTransfer({ ...options, amount: undefined, token: undefined, ...check,
        ...(check.reasons ? { check: async () => ({ decision: "ask", reasons: check.reasons }) } : {}) });
    }
  };
  const guarded = {
    async signIn(params) {
      let permissions;
      try { permissions = structuredClone(params); } catch (error) { throw payeeError(); }
      if (!permissions || !accountOrNull(permissions.contractId) ||
          permissions.methodNames !== undefined && (!Array.isArray(permissions.methodNames) ||
            permissions.methodNames.some(method => typeof method !== "string" || !method.length))) throw payeeError();
      await guardTransfer({ ...options, receiver: permissions.contractId, amount: undefined, token: undefined });
      return wallet.signIn(permissions);
    },
    async signAndSendTransaction(params) {
      let transaction;
      try { transaction = structuredClone(params); } catch (error) { throw payeeError(); }
      await run([transaction]);
      return wallet.signAndSendTransaction(transaction);
    },
    async signAndSendTransactions(params) {
      let batch;
      try { batch = structuredClone(params); } catch (error) { throw payeeError(); }
      await run(batch && batch.transactions);
      return wallet.signAndSendTransactions(batch);
    },
  };
  return new Proxy(guarded, {
    get(target, property) {
      if (Object.prototype.hasOwnProperty.call(target, property)) return target[property];
      if (typeof property === "string" && !["signIn", "signOut", "isSignedIn"].includes(property) && (/sign|delegate/i.test(property) || property === "verifyOwner")) {
        return async () => { throw payeeError(); };
      }
      const value = Reflect.get(wallet, property, wallet);
      return typeof value === "function" ? value.bind(wallet) : value;
    },
  });
}

export {
  DEFAULT_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  actionPayees,
  decideAccountCheck,
  decidePayment,
  guardTransfer,
  guardWallet,
  messageFor,
  parseEvidence,
  preflightAccountCheck,
};
