"""Tiered receiver rules: ALLOW, DENY or UNKNOWN for one NEAR transfer, every check recorded.

ALLOW means every check ran and none found a scam signal; DENY means a check found a signal (a
signal wins over a check that could not run); UNKNOWN means a check could not run. ALLOW is the
absence of a known scam signal, not proof of identity.

Tier 1, every receiver: receiver_exists, label_denylist, issuer_blacklist (when the caller gives
the issuer table of intel/issuers.json, which it does for a mainnet Gate), community_report (only when
the caller passes a reports account), token_in_catalog (ft), ft_metadata_sane (ft), storage_registered
(ft), memo_injection. Tier 2, a receiver not on the agent's allowlist: account_age, dust_funded,
lookalike, code_without_metadata. No contract code is
never a signal on its own. Contract code without contract_source_metadata is a signal only together
with another tier 2 signal (account_age, dust_funded or lookalike); alone it is a pass that says so.
"""

import dataclasses
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from gate import canon, evidence, freshness
from gate.rpc import (
    EMPTY_CODE_HASH,
    FixtureMiss,
    HttpError,
    MalformedResponse,
    RateLimited,
    RpcError,
    TransportError,
)
from intel import reports

MIN_AGE_DAYS = 7
# Rule sets judge can run: 1, the default, is the one every committed judgement was judged with; 2 adds the
# checks of gate/freshness.py.
RULES_VERSIONS = (1, 2)
# The checks judge runs only for a receiver off the agent's allowlist; an owner's set_allowlist call skips them.
TIER2_CHECKS = ("account_age", "dust_funded", "lookalike", "code_without_metadata")
# The tier 2 check rules version 2 adds. It stays out of TIER2_CHECKS, the version 1 set the incident replay
# page and docs/API.md pin.
TIER2_CHECKS_V2 = ("code_changed_recently",)
DAY_MS = 24 * 60 * 60 * 1000
YOCTO_PER_NEAR = 10**24
DUST_YOCTO = 10**23  # 0.1 NEAR
# An access key added in block h starts at nonce (h - 1) times this; a nonce from 1 to below it
# predates that rule.
NONCE_PER_HEIGHT = 10**6
# At most this many blocks a day (one every 0.5 s; the recorded mainnet reads show about 0.58 s), so
# min_age_days days of them before a block reach a block at least min_age_days old; its time is read back.
BLOCKS_PER_DAY_MAX = 2 * 24 * 60 * 60
# A candidate for a younger block (one every 2 s); only its read-back time proves its age.
BLOCKS_PER_DAY_MIN = 43_200
# The NearBlocks keys request carries no page parameter. Its recorded answer for user.intear.near lists 25
# keys, so a list this long may be one page of a longer one (page size observed, not documented). RPC refuses
# that key list as TOO_MANY_ACCESS_KEYS; _state_age dates such a receiver first, this list only after it.
NEARBLOCKS_KEYS_PAGE = 25
# A key creation time below this (in ns, September 2001) predates every NEAR block, so it is malformed.
MIN_CREATION_NS = 10**18
BASE58_HASH = re.compile(r"[1-9A-HJ-NP-Za-km-z]+")
# Confusable normalisation, applied in this order to both names before any comparison.
CONFUSABLES = (("rn", "m"), ("vv", "w"), ("0", "o"), ("1", "l"), ("i", "l"))
NETWORK_SUFFIXES = (".near", ".testnet")
DENY_CATEGORIES = ("denylist", "incident")
# Labelled NEAR accounts a receiver is compared with, besides the allowlist. Token symbols are not
# compared: a ticker is not a payee, and token identity is checked on token_id. A receiver that is
# itself one of these accounts is compared with the allowlist only, never with the other labelled ones.
REFERENCE_CATEGORIES = ("catalog_token", "bridge", "treasury", "infrastructure")
CONTAINMENT_MIN_LENGTH = 4
CONTAINMENT_AFFIX = re.compile(r"[a-z0-9]{1,4}")
# A check that meets one of these could not run: a missing fixture, an HTTP error such as a
# NearBlocks 429, the NearBlocks budget, an RPC error, a network failure, a truncated reply or a
# malformed response.
UNAVAILABLE = (FixtureMiss, HttpError, RateLimited, RpcError, TransportError, OSError, ValueError)
# RPC answers that say the account, its code or the method is absent, which is an answer, not an
# outage. Any other contract execution error could not run, so it reads unknown.
MISSING_CODE_CAUSES = ("UNKNOWN_ACCOUNT", "NO_CONTRACT_CODE")
MISSING_METHOD_MARKERS = ("MethodNotFound", "CodeDoesNotExist")
# Token issuers whose own view says whether an account is barred, per network; the callers choose
# the network from the Gate, never from the receiver.
ISSUERS_PATH = Path(__file__).resolve().parent.parent / "intel" / "issuers.json"

_ZERO_WIDTH = "".join(chr(code) for code in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))
INJECTION_PATTERNS = tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in (
    ("ignore_instructions", (r"\b(ignore|disregard|forget|override|bypass)\b[^.]{0,40}"
                             r"\b(instructions?|rules?|guidelines?|polic(y|ies)|prompts?|directives?)\b")),
    ("previous_context", (r"\b(previous|prior|above|earlier|preceding)\s+"
                          r"(instructions|messages|context|conversation|rules)\b")),
    ("new_instructions", r"\b(new|updated|revised|real|actual)\s+(instructions?|tasks?|orders?|directives?)\b"),
    ("system_prompt", r"\bsystem\s+(prompt|message|instructions?|override)\b"),
    ("role_marker", r"(^|\s)(system|assistant|developer)\s*:"),
    ("chat_template", r"<\|?(im_start|im_end|system|endoftext)\|?>|\[/?(inst|sys)\]|<<\s*sys\s*>>"),
    ("you_are_now", r"\byou\s+are\s+(now|no\s+longer)\b"),
    ("act_as", r"\b(act|behave|respond)\s+as\s+(if|an?|the)\b"),
    ("pretend", r"\bpretend\s+(to\s+be|you)\b"),
    ("jailbreak", r"\b(jailbreak|dan\s+mode|developer\s+mode|god\s+mode)\b"),
    ("conceal", r"\b(do\s+not|don'?t|never)\s+(tell|inform|alert|notify|mention|reveal)\b"),
    ("drain_funds", (r"\b(send|transfer|pay|move|withdraw|forward)\b[^.]{0,40}\b(all|entire|full|remaining|whole)\b"
                     r"[^.]{0,20}\b(balance|funds|tokens|money|assets)\b")),
    ("pay_instead", r"\b(pay|send|transfer)\b[^.]{0,30}\b(instead|rather)\b|\binstead\b[^.]{0,20}\b(pay|send|transfer)\b"),
    ("change_recipient", r"\b(change|update|replace|switch)\b[^.]{0,30}\b(recipient|receiver|payee|address|wallet)\b"),
    ("unlimited_approval", r"\b(approve|grant|allow)\b[^.]{0,30}\b(unlimited|infinite|max(imum)?)\b"),
    ("secret_request", r"\b(private\s+key|secret\s+key|seed\s+phrase|mnemonic|recovery\s+phrase)\b"),
    ("execute_code", r"\b(execute|run|eval)\b[^.]{0,20}\b(code|command|script|shell)\b"),
    ("urgent_action", r"\b(urgent|urgently|immediately|right\s+now)\b[^.]{0,30}\b(send|transfer|pay|approve|sign)\b"),
    ("admin_override", r"\b(admin|administrator|operator|owner)\s+(override|mode|command)\b"),
    ("tool_call", r"\b(call|invoke|use)\s+the\s+\w+\s+tool\b|\b(function_call|tool_calls?)\b"),
    ("hidden_text", "[" + _ZERO_WIDTH + "]"),
))


@dataclasses.dataclass
class Judgement:
    """The verdict with the checks behind it, the reasons, the label ids consulted and the
    chain state observed."""

    verdict: str
    checks: list
    reasons: list
    labels: list
    observed: dict


def _source(client):
    return dict(client.last_source)


def _local(provider, digest=None):
    return {"provider": provider, "request_sha256": digest, "fetched_at": None}


def _quoted(text):
    """A third party string as quoted data, never as Siyab's own words."""
    return json.dumps(text, ensure_ascii=False)


def issuers_for(network):
    """The issuer entries of intel/issuers.json for the network, mainnet or testnet."""
    return json.loads(ISSUERS_PATH.read_text(encoding="utf-8"))[network]


def _missing_code(error):
    """True only when an RPC error says the account, its code or the method is absent."""
    if error.cause in MISSING_CODE_CAUSES:
        return True
    if error.cause != "CONTRACT_EXECUTION_ERROR" and not isinstance(error.error, str):
        return False
    if isinstance(error.error, str):
        texts = [error.error]
    else:
        data = error.error.get("data")
        info = error.error["cause"].get("info")
        texts = [error.error.get("message"), info.get("vm_error") if isinstance(info, dict) else None,
                 data if isinstance(data, str) else data.get("vm_error") if isinstance(data, dict) else None]
    return any(marker in text for text in texts if isinstance(text, str) for marker in MISSING_METHOD_MARKERS)


def _age_text(age_ms):
    minutes = age_ms // 60000
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} d"


def _near_text(yocto):
    return format((Decimal(yocto) / YOCTO_PER_NEAR).normalize(), "f")


def _cap_tokens(policy):
    return {cap["asset"]["Ft"] for cap in policy.get("caps", [])
            if isinstance(cap.get("asset"), dict) and "Ft" in cap["asset"]}


def _current(store, subject):
    return [a for a in store.by_subject(subject, "near") if a.superseded_by is None]


def _name(account):
    """The account id without its .near or .testnet suffix."""
    for suffix in NETWORK_SUFFIXES:
        if account.endswith(suffix):
            return account[: -len(suffix)]
    return account


def distance(a, b):
    """Damerau Levenshtein distance (optimal string alignment): insertions, deletions,
    substitutions and transpositions of adjacent characters."""
    rows = [list(range(len(b) + 1))]
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            row[j] = min(rows[i - 1][j] + 1, row[j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], rows[i - 2][j - 2] + 1)
        rows.append(row)
    return rows[len(a)][len(b)]


def normalise(name):
    """The name after confusable normalisation: rn to m, vv to w, 0 to o, 1 and i to l."""
    for old, new in CONFUSABLES:
        name = name.replace(old, new)
    return name


def distance_limit(reference):
    """The largest distance that counts as a lookalike of a reference of this length."""
    return 2 if len(reference) >= 6 else 1 if len(reference) >= 4 else 0


def contains_allowlisted(receiver, allowlisted):
    """True when the receiver's first label holds an allowlisted first label of 4 or more
    characters: as a whole segment split on - and _, as a prefix or suffix with at most 4 letters
    or digits left over, or as the same label under another parent."""
    raw = allowlisted.split(".", 1)[0]
    r = normalise(receiver.split(".", 1)[0])
    a = normalise(raw)
    if len(raw) < CONTAINMENT_MIN_LENGTH or receiver == allowlisted:
        return False
    if r == a:
        # The same label under another parent; under the same parent it is a typo for the distance rule.
        return receiver.partition(".")[2] != allowlisted.partition(".")[2]
    if a in re.split(r"[-_]", r):
        return True
    return ((r.startswith(a) and bool(CONTAINMENT_AFFIX.fullmatch(r[len(a):])))
            or (r.endswith(a) and bool(CONTAINMENT_AFFIX.fullmatch(r[: -len(a)]))))


def _references(policy, store, receiver):
    """{account: assertion ids} for the allowlist and every current labelled NEAR account in
    REFERENCE_CATEGORIES, the receiver itself left out."""
    references = {account: [] for account in policy.get("allowlist", []) if account != receiver}
    for assertion in store.assertions:
        if (assertion.chain == "near" and assertion.category in REFERENCE_CATEGORIES
                and assertion.superseded_by is None and assertion.subject != receiver):
            references.setdefault(assertion.subject, []).append(assertion.id)
    return references


def _lookalike(policy, store, receiver, own):
    """(the account the receiver imitates, the check detail, assertion ids) or None. A receiver with
    its own reference assertions (own) is compared with the allowlist only."""
    for account in policy.get("allowlist", []):
        if contains_allowlisted(receiver, account):
            return account, f"contains the allowlisted name {account}", []
    references = _references(policy, store, receiver)
    if own:
        references = {account: ids for account, ids in references.items() if account in policy.get("allowlist", [])}
    for account, ids in references.items():
        detail = lookalike_detail(receiver, account)
        if detail:
            return account, detail, ids
    return None


def lookalike_detail(receiver, reference):
    """The check detail when the receiver is a lookalike of the reference account, else None.

    Under the same parent account (everything after the first dot) only the first labels are
    compared, with the limit of the reference's first label: only the parent can create a sub
    account on NEAR, so siblings such as zec.omft.near and qtc.omft.near are not each other's
    lookalikes. Across parents the names without their network suffix are compared."""
    sibling = receiver.partition(".")[2] == reference.partition(".")[2]
    if sibling:
        name, target = normalise(receiver.split(".", 1)[0]), normalise(reference.split(".", 1)[0])
    else:
        name, target = normalise(_name(receiver)), normalise(_name(reference))
    limit = distance_limit(target)
    if abs(len(name) - len(target)) > limit:
        return None
    gap = distance(name, target)
    if gap > limit:
        return None
    if sibling:
        return (f"lookalike of {reference}: first label {receiver.split('.', 1)[0]} against "
                f"{reference.split('.', 1)[0]}, distance {gap} after confusable normalisation")
    return f"lookalike of {reference} (distance {gap} after confusable normalisation)"


def _key_creations(response, now_ms):
    """[(block timestamp in ns, transaction hash)] for every key NearBlocks lists, each None when the
    key carries no creation time. A creation that is not null or an object, a time that is not an int
    or a digit string from MIN_CREATION_NS to now_ms, or a hash that is not base58 raises
    MalformedResponse."""
    if not isinstance(response, dict) or not isinstance(response.get("keys"), list):
        raise MalformedResponse("the NearBlocks keys response has no keys list")
    creations = []
    for key in response["keys"]:
        if not isinstance(key, dict):
            raise MalformedResponse("a NearBlocks key is not an object")
        info = key.get("created")
        if info is None or isinstance(info, dict) and info.get("block_timestamp") is None:
            creations.append((None, None))
            continue
        if not isinstance(info, dict):
            raise MalformedResponse(f"a key creation is not an object: {info!r}")
        value, transaction_hash = info["block_timestamp"], info.get("transaction_hash")
        if isinstance(value, str) and value.isascii() and value.isdigit():
            created_ns = int(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            created_ns = value
        else:
            raise MalformedResponse(f"a key creation time is not an integer: {value!r}")
        if not MIN_CREATION_NS <= created_ns <= now_ms * 1_000_000:
            raise MalformedResponse(f"a key creation time is out of range: {value!r}")
        if transaction_hash is not None and not (isinstance(transaction_hash, str)
                                                 and BASE58_HASH.fullmatch(transaction_hash)):
            raise MalformedResponse(f"a key creation transaction hash is not base58: {transaction_hash!r}")
        creations.append((created_ns, transaction_hash))
    return creations


def _creates(response, receiver, transaction_hash):
    """Whether the NearBlocks answer for transaction_hash, the transaction that added the receiver's
    oldest listed key, shows receiver being created. The answer must name transaction_hash and its
    outcomes status must be JSON true: an attempted creation proves nothing, so a status that is
    false, null, missing or of another type is no proof. Two shapes then count. A CREATE_ACCOUNT action
    sent to receiver, which succeeds only for an account that does not exist yet. A create_account call
    on receiver's own parent (everything after its first dot) naming it as new_account_id: only the
    parent can create a sub account, and the parent cannot add a key to a sub account that already
    exists, so a successful call there that added the receiver's key created it. A sub account running
    its own contract could add a key to itself during such a transaction, so an old cooperating sub
    account whose older keys were deleted could read young; that misreads only that receiver. A receiver
    with no dot has no parent. Anything else, a body without those fields included, is no proof."""
    transactions = response.get("txns") if isinstance(response, dict) else None
    if not isinstance(transactions, list) or not transactions or not isinstance(transactions[0], dict):
        return False
    transaction = transactions[0]
    outcomes = transaction.get("outcomes")
    if (transaction.get("transaction_hash") != transaction_hash or not isinstance(outcomes, dict)
            or outcomes.get("status") is not True):
        return False
    target, actions = transaction.get("receiver_account_id"), transaction.get("actions")
    parent = receiver.partition(".")[2]
    if not isinstance(actions, list):
        return False
    for action in actions:
        if not isinstance(action, dict):
            continue
        if action.get("action") == "CREATE_ACCOUNT" and target == receiver:
            return True
        if (action.get("action") == "FUNCTION_CALL" and action.get("method") == "create_account"
                and parent and target == parent and isinstance(action.get("args"), str)):
            try:
                args = json.loads(action["args"])
            except (ValueError, RecursionError):
                continue
            if isinstance(args, dict) and args.get("new_account_id") == receiver:
                return True
    return False


def _creation_deposit(response):
    """The yoctoNEAR attached by a NearBlocks transaction, as an integral Decimal from 0 to below 2**128
    (a u128). The recorded answers give it as a JSON number, often in exponent form such as 5e+23; a
    string is read only when it is ASCII digits, and a value with a fraction raises MalformedResponse."""
    if not isinstance(response, dict) or not isinstance(response.get("txns"), list) or not response["txns"]:
        raise MalformedResponse("the NearBlocks transaction response has no transaction")
    if not isinstance(response["txns"][0], dict):
        raise MalformedResponse("the NearBlocks transaction is not an object")
    aggregate = response["txns"][0].get("actions_agg")
    if not isinstance(aggregate, dict) or aggregate.get("deposit") is None:
        raise MalformedResponse("the NearBlocks transaction has no deposit total")
    value = aggregate["deposit"]
    if isinstance(value, str) and not (value.isascii() and value.isdigit()):
        raise MalformedResponse(f"the deposit total is not a number: {value!r}")
    try:
        deposit = Decimal(str(value))
    except InvalidOperation as error:
        raise MalformedResponse(f"the deposit total is not a number: {value!r}") from error
    if not deposit.is_finite() or not 0 <= deposit < 2**128 or deposit != deposit.to_integral_value():
        raise MalformedResponse(f"the deposit total is not a u128 amount: {value!r}")
    return Decimal(0) if deposit.is_zero() else deposit


def _why(error):
    if isinstance(error, FixtureMiss):
        return "no recorded response for the request"
    if isinstance(error, HttpError):
        return f"HTTP {error.status}"
    if isinstance(error, RpcError):
        return f"RPC error {error.cause or error.error}"
    return f"{type(error).__name__}: {error}"


def _issuer_check(rpc, receiver, issuers):
    """One issuer_blacklist row over every issuer: only exact typed clear answers pass; exact typed
    banned answers signal. Missing clear configuration, unexpected answers or missing views read unknown."""
    parts, sources = [], {}
    for entry in issuers:
        token, method = entry["token"], entry["method"]
        try:
            answer = rpc.call_function(token, method, {entry["args_key"]: receiver})
            if "clear" not in entry:
                status, part = "unknown", f"no clear answer configured for {token}"
            elif type(answer) is type(entry["banned"]) and answer == entry["banned"]:
                status = "signal"
                part = f"{receiver} is banned by the issuer of {token}: {method} answers {_quoted(answer)}"
            elif type(answer) is type(entry["clear"]) and answer == entry["clear"]:
                status, part = "pass", f"{token} {method} answers {_quoted(answer)}"
            else:
                status = "unknown"
                part = f"unexpected issuer blacklist answer on {token}: {method} answers {_quoted(answer)}"
        except RpcError as error:
            if _missing_code(error):
                status, part = "unknown", f"no issuer blacklist view on {token}"
            else:
                status, part = "unknown", f"could not read {method} on {token}: {_why(error)}"
        except UNAVAILABLE as error:
            status, part = "unknown", f"could not read {method} on {token}: {_why(error)}"
        parts.append(part)
        sources.setdefault(status, _source(rpc))
    status = next(s for s in ("signal", "unknown", "pass") if s in sources)
    return evidence.check("issuer_blacklist", status, "; ".join(parts), sources[status])


def _tier1(request, policy, store, rpc, injection_scanner, issuers, reports_account):
    """Tier 1 checks; returns (checks, the receiver account or None, its source, label ids)."""
    kind, token, receiver, memo = (request["kind"], request["token_id"], request["receiver_id"],
                                   request["memo"])
    checks = []
    account = None
    try:
        account = rpc.view_account(receiver)
        checks.append(evidence.check("receiver_exists", "pass", f"{receiver} exists", _source(rpc)))
    except RpcError as error:
        status = "signal" if error.cause == "UNKNOWN_ACCOUNT" else "unknown"
        detail = (f"receiver_missing: {receiver} does not exist" if status == "signal"
                  else f"could not read {receiver}: {_why(error)}")
        checks.append(evidence.check("receiver_exists", status, detail, _source(rpc)))
    except UNAVAILABLE as error:
        checks.append(evidence.check("receiver_exists", "unknown",
                                     f"could not read {receiver}: {_why(error)}", _source(rpc)))
    account_source = _source(rpc)

    store_source = _local("label store", store.sha256)
    consulted = _current(store, receiver) + (_current(store, token) if kind == "ft" else [])
    hits = [a for a in consulted if a.category in DENY_CATEGORIES]
    if hits:
        detail = "; ".join(f"{a.subject} carries the {a.category} label {_quoted(a.label)} (assertion {a.id})"
                           for a in hits)
        checks.append(evidence.check("label_denylist", "signal", detail, store_source))
    else:
        checks.append(evidence.check("label_denylist", "pass", "no denylist or incident label on "
                                     + " or ".join([receiver] + ([token] if kind == "ft" else [])),
                                     store_source))

    if issuers:
        checks.append(_issuer_check(rpc, receiver, issuers))

    if kind == "ft":
        in_caps = token in _cap_tokens(policy)
        in_catalog = any(a.category == "catalog_token" for a in _current(store, token))
        if in_caps or in_catalog:
            where = "a cap asset of the agent" if in_caps else "a catalog_token in the label store"
            checks.append(evidence.check("token_in_catalog", "pass", f"{token} is {where}", store_source))
        else:
            checks.append(evidence.check("token_in_catalog", "signal", f"token_not_in_catalog: {token} is "
                                         "neither a cap asset nor a catalog_token", store_source))
        checks.append(_ft_metadata_check(rpc, token))
        try:
            balance = rpc.call_function(token, "storage_balance_of", {"account_id": receiver})
            if balance is None:
                checks.append(evidence.check("storage_registered", "signal", f"receiver_not_registered: "
                                             f"{receiver} is not registered on {token}", _source(rpc)))
            else:
                checks.append(evidence.check("storage_registered", "pass",
                                             f"{receiver} is registered on {token}", _source(rpc)))
        except UNAVAILABLE as error:
            checks.append(evidence.check("storage_registered", "unknown",
                                         f"could not read storage_balance_of: {_why(error)}", _source(rpc)))

    checks.append(_memo_check(memo, injection_scanner))
    report_id = None
    if reports_account:
        try:
            report_summary = rpc.call_function(reports_account, "subject_summary", {"subject": receiver})
            report_effect = reports.effect(report_summary, receiver in policy.get("allowlist", []))
            report_id = report_effect["report_id"]
        except UNAVAILABLE as error:
            report_effect = {"effect": "unreadable", "report_id": None,
                             "detail": f"subject_summary could not be read: {_why(error)}"}
        status = {"deny": "signal", "ask": "unknown", "none": "pass", "unreadable": "unknown"}[
            report_effect["effect"]]
        detail = f"reports for {receiver}: {report_effect['detail']}"
        checks.append(evidence.check("community_report", status, detail, _source(rpc)))
    return checks, account, account_source, [a.id for a in consulted], report_id


def _ft_metadata_check(rpc, token):
    try:
        meta = rpc.call_function(token, "ft_metadata", {})
    except RpcError as error:
        status = "signal" if _missing_code(error) else "unknown"
        detail = (f"ft_metadata missing on {token}: {_why(error)}" if status == "signal"
                  else f"could not read ft_metadata: {_why(error)}")
        return evidence.check("ft_metadata_sane", status, detail, _source(rpc))
    except UNAVAILABLE as error:
        return evidence.check("ft_metadata_sane", "unknown", f"could not read ft_metadata: {_why(error)}",
                              _source(rpc))
    decimals = meta.get("decimals") if isinstance(meta, dict) else None
    sane = (isinstance(meta, dict) and all(isinstance(meta.get(k), str) and meta[k] for k in ("name", "symbol"))
            and isinstance(decimals, int) and not isinstance(decimals, bool) and 0 <= decimals <= 24)
    if not sane:
        return evidence.check("ft_metadata_sane", "signal", f"ft_metadata on {token} lacks a name, a symbol "
                              "or decimals in 0 to 24", _source(rpc))
    return evidence.check("ft_metadata_sane", "pass", f"symbol {_quoted(meta['symbol'])}, {decimals} decimals",
                          _source(rpc))


def _memo_check(memo, injection_scanner):
    """Local instruction shaped patterns, merged with ShieldBot's injection scan when it is set."""
    if memo is None:
        return evidence.check("memo_injection", "pass", "no memo", _local("local patterns"))
    hits = [name for name, pattern in INJECTION_PATTERNS if pattern.search(memo)]
    local = f"local patterns: {', '.join(hits) if hits else 'no match'}"
    if injection_scanner is None:
        return evidence.check("memo_injection", "signal" if hits else "pass", local, _local("local patterns"))
    try:
        result = injection_scanner.post("/api/scan/injection", {"content": memo})
        clean = result.get("clean") if isinstance(result, dict) else None
        if not isinstance(clean, bool):
            raise MalformedResponse("the injection scan answer has no clean flag")
        remote, remote_status = ("clean" if clean else f"flagged {result.get('risk_level')}",
                                 "pass" if clean else "signal")
    except UNAVAILABLE as error:
        remote, remote_status = f"could not run: {_why(error)}", "unknown"
    if hits or remote_status == "signal":
        status = "signal"
    else:
        status = remote_status
    return evidence.check("memo_injection", status, f"{local}; ShieldBot injection scan: {remote}",
                          _source(injection_scanner))


def _block_time(archival, height):
    """(time in ms, ISO time) of block height on an archival node; a block of another height or a
    time out of range raises MalformedResponse."""
    header = archival.block_at(height)["header"]
    if header["height"] != height:
        raise MalformedResponse(f"block {height} came back as block {header['height']}")
    block_ms = header["timestamp"] // 10**6
    try:
        iso = datetime.fromtimestamp(block_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError) as error:
        raise MalformedResponse(f"block {height} has a time out of range: {header['timestamp']}") from error
    return block_ms, iso


def _state_age(archival, receiver, account, account_source, now_ms, min_age_days, why):
    """(the account_age check, False) when view_account on an archival node finds the receiver at a
    block at least min_age_days old: min_age_days times BLOCKS_PER_DAY_MAX blocks before the block its
    own view_account (account) was read at, with that block's time read back. Only _nonce_age calls
    it, for a receiver with no access key or none with a creation height, such as a locked contract, for one
    whose key list RPC refuses as TOO_MANY_ACCESS_KEYS, and for one whose oldest access key was added in a
    block younger than min_age_days, which shows only that the account existed by then.
    No step down: archival view_account answered HTTP 500 at many testnet blocks (docs/SPIKES.md S8),
    which reads unknown here. If the receiver is missing at the old block, a second candidate's time
    must prove it younger than min_age_days before reading the receiver there. Missing there too signals.
    Otherwise the original unknown answer remains. Both give True: an older NearBlocks key creation
    then cannot pass the account. Any other unknown check gives None."""
    reference = account.get("block_height")
    if not isinstance(reference, int) or isinstance(reference, bool):
        return evidence.check("account_age", "unknown", f"{why}; the receiver's view_account names no block "
                              "height", account_source), None
    height = reference - min_age_days * BLOCKS_PER_DAY_MAX
    try:
        block_ms, iso = _block_time(archival, height)
    except UNAVAILABLE as error:
        return evidence.check("account_age", "unknown", f"{why}; archival block_at could not run: {_why(error)}",
                              _source(archival)), None
    block = f"block {height} ({iso}) from {_source(archival)['provider']}"
    if now_ms - block_ms < min_age_days * DAY_MS:
        return evidence.check("account_age", "unknown", f"{why}; {block} is younger than {min_age_days} days",
                              _source(archival)), None
    try:
        state = archival.view_account_at(receiver, height)
        if state.get("block_height") != height:
            raise MalformedResponse(f"view_account at block {height} came back at block {state.get('block_height')}")
    except UNAVAILABLE as error:
        if isinstance(error, RpcError) and error.cause == "UNKNOWN_ACCOUNT":
            detail = (f"{receiver} did not exist at {block}, so it was created after that block, which is at "
                      f"least {min_age_days} d old; whether it is younger than {min_age_days} days is not known")
            unknown = evidence.check("account_age", "unknown", f"{why}; {detail}", _source(archival))
            info = error.error["cause"].get("info")
            if not (isinstance(info, dict) and type(info.get("block_height")) is int
                    and info["block_height"] == height and info.get("requested_account_id") == receiver):
                return unknown, True
            young_height = reference - min_age_days * BLOCKS_PER_DAY_MIN
            try:
                young_ms, young_iso = _block_time(archival, young_height)
                reference_ms, _ = _block_time(archival, reference)
            except UNAVAILABLE:
                return unknown, True
            # Ten minutes allows clock skew and finality lag; more means a stale read or a bad clock.
            # Date both proof blocks against the receiver's reference block, never the local clock.
            if (abs(now_ms - reference_ms) > 10 * 60 * 1000
                    or reference_ms - block_ms < min_age_days * DAY_MS
                    or not 0 <= reference_ms - young_ms < min_age_days * DAY_MS):
                return unknown, True
            young_block = f"block {young_height} ({young_iso}) from {_source(archival)['provider']}"
            try:
                state = archival.view_account_at(receiver, young_height)
                if state.get("block_height") != young_height:
                    raise MalformedResponse(f"view_account at block {young_height} came back at block "
                                            f"{state.get('block_height')}")
            except UNAVAILABLE as young_error:
                info = (young_error.error["cause"].get("info") if isinstance(young_error, RpcError)
                        and young_error.cause == "UNKNOWN_ACCOUNT" else None)
                if (isinstance(info, dict) and type(info.get("block_height")) is int
                        and info["block_height"] == young_height and info.get("requested_account_id") == receiver):
                    detail = (f"{receiver} did not exist at {block} or {young_block}, which is younger than "
                              f"{min_age_days} days, so it was created less than {min_age_days} days ago")
                    return evidence.check("account_age", "signal", f"{why}; {detail}", _source(archival)), True
                return unknown, True
            return unknown, True
        return evidence.check("account_age", "unknown", f"{why}; archival view_account could not run: "
                              f"{_why(error)}", _source(archival)), None
    return evidence.check("account_age", "pass", f"{why}; view_account finds {receiver} at {block}, at least "
                          f"{min_age_days} d old", _source(archival)), False


def _nonce_age(rpc, archival, receiver, now_ms, min_age_days, account, account_source):
    """(the account_age check, False when it passed) from the oldest access key: the account existed
    by the block its smallest nonce encodes, dated on an archival node. That block at least min_age_days
    old passes. A younger one is a lower bound on the age, not the creation time, since every key the
    account was created with may have been deleted, so it never signals: _state_age dates the receiver
    instead. A key with nonce 0, or a permission other than full access or function call, carries no
    creation height. A receiver with no access key, or none with a creation height, or one whose key list
    RPC refuses as TOO_MANY_ACCESS_KEYS, is dated by _state_age too, which gives True when the receiver did
    not exist at its old block. Any other read that could not run gives an unknown check and None."""
    try:
        keys = rpc.view_access_key_list(receiver)["keys"]
        nonces = []
        for key in keys:
            access = key.get("access_key") if isinstance(key, dict) else None
            nonce = access.get("nonce") if isinstance(access, dict) else None
            if not isinstance(nonce, int) or isinstance(nonce, bool) or nonce < 0:
                raise MalformedResponse("an access key has no non negative int nonce")
            permission = access.get("permission")
            if nonce and (permission == "FullAccess"
                          or isinstance(permission, dict) and "FunctionCall" in permission):
                nonces.append(nonce)
    except UNAVAILABLE as error:
        if isinstance(error, RpcError) and error.cause == "TOO_MANY_ACCESS_KEYS":
            return _state_age(archival, receiver, account, account_source, now_ms, min_age_days,
                              "view_access_key_list answers TOO_MANY_ACCESS_KEYS, so no key nonce dates the account")
        return evidence.check("account_age", "unknown", f"view_access_key_list could not run: "
                              f"{_why(error)}", _source(rpc)), None
    rpc_source = _source(rpc)
    if not keys:
        return _state_age(archival, receiver, account, account_source, now_ms, min_age_days,
                          f"{receiver} has no access key")
    if not nonces:
        return _state_age(archival, receiver, account, account_source, now_ms, min_age_days,
                          "no access key carries a creation height")
    nonce = min(nonces)
    oldest = f"oldest access key nonce {nonce} from {rpc_source['provider']}"
    if nonce < NONCE_PER_HEIGHT:
        return evidence.check("account_age", "pass", f"{oldest} predates block height nonces, at least "
                              f"{min_age_days} d old", rpc_source), False
    height = nonce // NONCE_PER_HEIGHT + 1
    try:
        block_ms, iso = _block_time(archival, height)
    except UNAVAILABLE as error:
        return evidence.check("account_age", "unknown", f"archival block_at could not run: "
                              f"{_why(error)}", _source(archival)), None
    archival_source = _source(archival)
    block = f"block {height} ({iso}) from {archival_source['provider']}"
    if now_ms - block_ms < min_age_days * DAY_MS:
        return _state_age(archival, receiver, account, account_source, now_ms, min_age_days,
                          f"{oldest}: existed by {block}, a key block younger than {min_age_days} days that "
                          "does not date the account")
    return evidence.check("account_age", "pass", f"{oldest}: existed by {block}, at least {min_age_days} d old",
                          archival_source), False


def _tier2(request, policy, store, account, account_source, rpc, nearblocks, now_ms, min_age_days, archival):
    """Tier 2 checks; returns (checks, facts for the reasons, matched catalog assertion ids)."""
    receiver = request["receiver_id"]
    checks, facts, matched = [], {}, []
    creation_hash, creation = None, None
    archival_detail, archival_young = None, None
    if archival is not None and account is not None:
        age_check, archival_young = _nonce_age(rpc, archival, receiver, now_ms, min_age_days,
                                               account, account_source)
        if age_check["status"] == "signal":
            facts["age"] = f"less than {min_age_days} days"
        if age_check["status"] == "unknown":
            archival_detail = age_check["detail"]
    if archival is None or account is None or archival_detail is not None:
        try:
            keys = nearblocks.get(f"/v1/account/{receiver}/keys")
            creations = _key_creations(keys, now_ms)
            dated = [item for item in creations if item[0] is not None]
            if not dated:
                # NearBlocks lists no access key and view_account shows contract code; these are the observed reads.
                locked = not keys["keys"] and account is not None and account.get("code_hash") != EMPTY_CODE_HASH
                age_check = evidence.check("account_age", "unknown",
                                           "NearBlocks lists no access key for the account and view_account shows "
                                           "contract code, so no creation time could be read" if locked
                                           else f"no key of {receiver} carries a creation time", _source(nearblocks))
            elif len(dated) < len(creations):
                age_check = evidence.check("account_age", "unknown", f"a key of {receiver} carries no creation time, "
                                           "so the oldest key NearBlocks lists may not be its first",
                                           _source(nearblocks))
            elif len(creations) >= NEARBLOCKS_KEYS_PAGE:
                age_check = evidence.check("account_age", "unknown", f"NearBlocks lists {len(creations)} keys, a "
                                           "full page that may leave out older ones", _source(nearblocks))
            else:
                created_ns, creation_hash = min(dated, key=lambda item: item[0])
                age_ms = now_ms - created_ns // 1_000_000
                age = _age_text(age_ms)
                # The oldest listed key is young: the account is young only if that key's transaction created it,
                # since an account can be created without a key and given one later.
                if age_ms < min_age_days * DAY_MS:
                    oldest = f"the oldest key NearBlocks lists was created {age} ago"
                    try:
                        if creation_hash is None:
                            raise MalformedResponse("the key creation names no transaction")
                        creation = nearblocks.get(f"/v1/txns/{creation_hash}")
                    except UNAVAILABLE as error:
                        creation_hash = None
                        age_check = evidence.check("account_age", "unknown", f"{oldest}; could not read its "
                                                   f"transaction: {_why(error)}", _source(nearblocks))
                    else:
                        if _creates(creation, receiver, creation_hash):
                            facts["age"] = age
                            age_check = evidence.check("account_age", "signal", f"created {age} ago, younger than "
                                                       f"{min_age_days} days", _source(nearblocks))
                        else:
                            age_check = evidence.check("account_age", "unknown", f"{oldest} by transaction "
                                                       f"{creation_hash}, which does not show {receiver} being "
                                                       "created, so the account may be older", _source(nearblocks))
                            creation_hash, creation = None, None
                elif archival_young:
                    creation_hash = None
                    age_check = evidence.check("account_age", "unknown", f"the oldest key NearBlocks lists was "
                                               f"created {age} ago, but the archival node found no {receiver} at "
                                               "the old block, so that key does not date the account",
                                               _source(nearblocks))
                else:
                    age_check = evidence.check("account_age", "pass", f"oldest listed key created {age} ago",
                                               _source(nearblocks))
        except UNAVAILABLE as error:
            age_check = evidence.check("account_age", "unknown", f"could not read the account keys: "
                                       f"{_why(error)}", _source(nearblocks))
        if archival_detail is not None:
            age_check = evidence.check("account_age", age_check["status"],
                                       f"no archival age ({archival_detail}); {age_check['detail']}",
                                       age_check["source"])
    checks.append(age_check)

    dust = None
    if creation_hash is None:
        dust_check = evidence.check("dust_funded", "unknown", "the creation transaction is not known",
                                    _local("nearblocks"))
    else:
        try:
            # On the pass branch the oldest listed key's transaction gives the creation deposit only if it
            # created the account; the signal branch has already shown that for creation.
            if creation is None:
                creation = nearblocks.get(f"/v1/txns/{creation_hash}")
                if not _creates(creation, receiver, creation_hash):
                    raise MalformedResponse(f"transaction {creation_hash} of the oldest listed key does not show "
                                            f"{receiver} being created")
            deposit = _creation_deposit(creation)
            facts["deposit"] = _near_text(deposit)
            dust = deposit < DUST_YOCTO
            dust_source = _source(nearblocks)
        except UNAVAILABLE as error:
            dust_check = evidence.check("dust_funded", "unknown", f"could not read the creation "
                                        f"transaction: {_why(error)}", _source(nearblocks))

    own = [a.id for a in _current(store, receiver) if a.category in REFERENCE_CATEGORIES]
    hit = _lookalike(policy, store, receiver, own)
    source = _local("allowlist and label store", store.sha256)
    if hit:
        facts["lookalike"], detail, matched = hit
        lookalike_check = evidence.check("lookalike", "signal", detail, source)
    elif own:
        lookalike_check = evidence.check("lookalike", "pass", f"{receiver} is itself a labelled reference account "
                                         f"(assertion {', '.join(own)})", source)
    else:
        lookalike_check = evidence.check("lookalike", "pass", "no allowlisted name contained and no allowlisted "
                                         "or labelled NEAR account within the length scaled distance after "
                                         "confusable normalisation", source)

    # When account_age passed by any path and nothing is a lookalike, no creation deposit can change the
    # verdict, so a deposit that was not read, or could not be read, does not hold it at UNKNOWN.
    if archival_young is False and not hit:
        dust_check = evidence.check("dust_funded", "pass", "creation deposit not read: a deposit is a signal "
                                    "only with lookalike or account_age", _local("nearblocks"))
    elif age_check["status"] == "pass" and not hit and dust is None:
        dust_check = evidence.check("dust_funded", "pass", f"{dust_check['detail']}; not needed: account_age "
                                    "passed and nothing is a lookalike, and a deposit is a signal only with "
                                    "lookalike or account_age", dust_check["source"])

    # A dust creation deposit is the ordinary wallet pattern, so it is a signal only together with a
    # lookalike or a young account (DISPOSITION-dust option A).
    if dust is not None:
        young = any(c["id"] == "account_age" and c["status"] == "signal" for c in checks)
        if dust and (hit or young):
            dust_check = evidence.check("dust_funded", "signal", f"created by a {facts['deposit']} NEAR deposit, "
                                        "below 0.1 NEAR", dust_source)
        elif dust:
            dust_check = evidence.check("dust_funded", "pass", f"created by a {facts['deposit']} NEAR deposit; not "
                                        "a signal without lookalike or account_age", dust_source)
        else:
            dust_check = evidence.check("dust_funded", "pass", f"created by a {facts['deposit']} NEAR deposit",
                                        dust_source)
    checks += [dust_check, lookalike_check]

    # Deployed protocol contracts also publish no contract_source_metadata, so code without it is a signal
    # only together with another tier 2 signal (PLAN-v4 B6).
    checks.append(_code_check(rpc, receiver, account, account_source,
                              any(check["status"] == "signal" for check in checks)))
    return checks, facts, matched


def _code_check(rpc, receiver, account, account_source, other_signal):
    if account is None:
        return evidence.check("code_without_metadata", "unknown", "the receiver account could not be read",
                              account_source)
    if account.get("code_hash") == EMPTY_CODE_HASH:
        return evidence.check("code_without_metadata", "pass", "no contract code", account_source)
    try:
        rpc.call_function(receiver, "contract_source_metadata", {})
        return evidence.check("code_without_metadata", "pass", "contract code with contract_source_metadata",
                              _source(rpc))
    except RpcError as error:
        if not _missing_code(error):
            status, detail = "unknown", f"could not read contract_source_metadata: {_why(error)}"
        elif other_signal:
            status, detail = "signal", "code_without_metadata: contract code without contract_source_metadata"
        else:
            status, detail = "pass", "contract code without contract_source_metadata; not a signal on its own"
        return evidence.check("code_without_metadata", status, detail, _source(rpc))
    except UNAVAILABLE as error:
        return evidence.check("code_without_metadata", "unknown",
                              f"could not read contract_source_metadata: {_why(error)}", _source(rpc))


_SIGNAL_REASONS = {
    "receiver_exists": "{receiver} does not exist.",
    "label_denylist": "{detail}.",
    "issuer_blacklist": "{receiver} is banned by a token issuer ({detail}).",
    "token_in_catalog": "{token} is not a catalog token or a cap asset of the agent.",
    "ft_metadata_sane": "{token} has no valid ft_metadata.",
    "storage_registered": "Not registered on the token.",
    "memo_injection": "The memo carries instruction shaped text ({detail}).",
    "community_report": "The reports contract {reports_account} accepted deny report {report_id} for {receiver}.",
    "code_without_metadata": "{receiver} has contract code without contract_source_metadata.",
    "code_changed_recently": "{receiver} changed its contract code ({detail}).",
}


def _reasons(verdict, checks, facts, request, tier2, reports_account, report_id):
    receiver, token = request["receiver_id"], request["token_id"]
    if verdict == "ALLOW":
        reasons = ["Every check ran and none found a signal."]
        return reasons + (["Not on the owner's allowlist."] if tier2 else [])
    if verdict == "UNKNOWN":
        reasons = []
        for check in checks:
            if check["status"] != "unknown":
                continue
            if check["id"] == "community_report":
                reason = ("community reports need a human decision"
                          if "; a human decides; " in check["detail"]
                          else "community reports could not be checked")
                reasons.append(reason)
            else:
                reasons.append(f"{check['id']} could not run: {check['detail']}.")
        return reasons
    signals = {c["id"]: c for c in checks if c["status"] == "signal"}
    reasons = []
    if "lookalike" in signals:
        reasons.append(f"{receiver} is a lookalike of {facts['lookalike']}.")
    if "account_age" in signals and "dust_funded" in signals:
        reasons.append(f"Created {facts['age']} ago by a {facts['deposit']} NEAR deposit.")
    elif "account_age" in signals:
        reasons.append(f"Created {facts['age']} ago.")
    elif "dust_funded" in signals:
        reasons.append(f"Created by a {facts['deposit']} NEAR deposit.")
    if tier2:
        reasons.append("Not on the owner's allowlist.")
    for check_id, template in _SIGNAL_REASONS.items():
        if check_id in signals:
            reasons.append(template.format(receiver=receiver, token=token, detail=signals[check_id]["detail"],
                                            reports_account=reports_account, report_id=report_id))
    return reasons


def judge(request, policy, store, rpc, nearblocks, *, now_ms, injection_scanner, min_age_days=MIN_AGE_DAYS,
          archival=None, issuers=None, reports_account=None, rules_version=1):
    """Judge one TransferRequest for the agent whose policy (allowlist and caps, in the get_agent
    shape) is given; store is the label store, rpc the NEAR JSON-RPC client, nearblocks the
    NearBlocks client, injection_scanner the ShieldBot API client or None and archival the archival
    JSON-RPC client that dates the receiver before NearBlocks is used as a fallback, or None.
    issuers is the list of issuer entries (issuers_for) the issuer_blacklist check reads; None or an
    empty list adds no check. reports_account is the reports contract account or None to omit its
    check. rules_version is one of RULES_VERSIONS; 2 adds label_stale after label_denylist and, for tier 2,
    code_changed_recently (gate/freshness.py), and drops code_without_metadata for a receiver on a global
    contract, whose empty code hash that check reads as no contract code. A receiver that is not a valid account
    id, or another rules_version, raises a ValueError (canon.CanonError for the id) before any request."""
    canon.account_id(request["receiver_id"], "receiver_id")
    if type(rules_version) is not int or rules_version not in RULES_VERSIONS:
        raise ValueError(f"rules_version must be one of {RULES_VERSIONS}, not {rules_version!r}")
    receiver = request["receiver_id"]
    tier1_checks, account, account_source, labels, report_id = _tier1(
        request, policy, store, rpc, injection_scanner, issuers, reports_account)
    block_height = account.get("block_height") if account else None
    tier2 = receiver not in policy.get("allowlist", [])
    checks, facts = list(tier1_checks), {}
    if rules_version == 2:
        missing = any(c["id"] == "receiver_exists" and c["status"] == "signal" for c in checks)
        stale = freshness.label_stale(_current(store, receiver), receiver, account, missing, account_source)
        if stale is not None:
            checks.insert(1 + [c["id"] for c in checks].index("label_denylist"), stale)
    if tier2:
        tier2_checks, facts, matched = _tier2(request, policy, store, account, account_source, rpc,
                                              nearblocks, now_ms, min_age_days, archival)
        if rules_version == 2:
            if account and (account.get("global_contract_hash") is not None
                            or account.get("global_contract_account_id") is not None):
                tier2_checks = [check for check in tier2_checks if check["id"] != "code_without_metadata"]
            tier2_checks.append(freshness.code_changed(archival, receiver, account, account_source, now_ms,
                                                       min_age_days,
                                                       any(check["status"] == "signal" for check in tier2_checks)))
        checks += tier2_checks
        labels += [assertion_id for assertion_id in matched if assertion_id not in labels]
    statuses = {check["status"] for check in checks}
    verdict = "DENY" if "signal" in statuses else "UNKNOWN" if "unknown" in statuses else "ALLOW"
    return Judgement(verdict=verdict, checks=checks,
                     reasons=_reasons(verdict, checks, facts, request, tier2, reports_account, report_id),
                     labels=labels, observed={"block_height": block_height, "timestamp_ms": now_ms})
