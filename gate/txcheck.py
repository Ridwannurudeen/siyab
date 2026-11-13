"""An unsigned, offline check of proposed NEAR mainnet transaction actions."""

import base64
import json

from gate import base58, canon, evidence, rules
from intel.feed import Context, code_hash


def _account(value, field):
    account = canon.account_id(value, field)
    if account == "testnet" or account.endswith(".testnet"):
        raise ValueError(f"{field}: mainnet accounts only")
    return account


def _amount(value):
    if not isinstance(value, str):
        raise ValueError("an action amount must be a decimal string")
    if value != "0":
        canon.amount_field(value)


def _public_key(value):
    if not isinstance(value, str) or ":" not in value:
        raise ValueError("an action public key must name its key type")
    kind, encoded = value.split(":", 1)
    lengths = {"ed25519": 32, "secp256k1": 64}
    if kind not in lengths or len(base58.decode(encoded)) != lengths[kind]:
        raise ValueError("an action public key has an invalid type or length")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate function call argument: {key}")
        result[key] = value
    return result


def _constant(name):
    raise ValueError(f"token transfer args hold the non-JSON constant {name}")


def _token_args(decoded):
    """The object of token transfer args: strict UTF-8 JSON text with valid Unicode and no NaN or Infinity."""
    try:
        args = json.loads(decoded.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_constant)
        json.dumps(args, ensure_ascii=False).encode("utf-8")
    except UnicodeError as error:
        raise ValueError("token transfer args must be valid UTF-8 JSON text") from error
    return args


def _validate(transaction):
    if not isinstance(transaction, dict) or set(transaction) != {"signer_id", "receiver_id", "actions"}:
        raise ValueError("a transaction must contain signer_id, receiver_id and actions")
    _account(transaction["signer_id"], "signer_id")
    receiver = _account(transaction["receiver_id"], "receiver_id")
    actions = transaction["actions"]
    if not isinstance(actions, list) or not actions:
        raise ValueError("transaction actions must be a non-empty list")
    parsed, accounts = [], [receiver]
    for action in actions:
        if isinstance(action, str) and action:
            kind, body = action, {}
        elif isinstance(action, dict) and len(action) == 1:
            [(kind, body)] = action.items()
            if not isinstance(kind, str) or not kind or not isinstance(body, dict):
                raise ValueError("an action must name one kind with an object body")
        else:
            raise ValueError("an action must name one kind")
        args = None
        required = {"Transfer": {"deposit"}, "AddKey": {"public_key", "access_key"},
                    "DeleteKey": {"public_key"}, "DeleteAccount": {"beneficiary_id"},
                    "DeployContract": {"code"}, "FunctionCall": {"method_name", "args", "gas", "deposit"},
                    "CreateAccount": set()}
        if kind in required and set(body) != required[kind]:
            raise ValueError(f"{kind}: malformed action fields")
        if kind in ("Transfer", "FunctionCall"):
            _amount(body["deposit"])
        if kind in ("AddKey", "DeleteKey"):
            _public_key(body["public_key"])
        if kind == "DeleteAccount":
            _account(body["beneficiary_id"], "beneficiary_id")
        if kind == "DeployContract":
            if not isinstance(body["code"], str) or not body["code"]:
                raise ValueError("DeployContract: code must be non-empty base64 text")
            base64.b64decode(body["code"], validate=True)
        if kind == "AddKey":
            key = body["access_key"]
            if (not isinstance(key, dict) or set(key) != {"nonce", "permission"}
                    or type(key["nonce"]) is not int or not 0 <= key["nonce"] <= canon.U64_MAX):
                raise ValueError("AddKey: malformed access_key")
            permission = key["permission"]
            if permission != "FullAccess":
                if not isinstance(permission, dict) or set(permission) != {"FunctionCall"}:
                    raise ValueError("AddKey: unrecognised access key permission")
                call = permission["FunctionCall"]
                if (not isinstance(call, dict) or not {"receiver_id", "method_names"} <= set(call)
                        or set(call) - {"receiver_id", "method_names", "allowance"}):
                    raise ValueError("AddKey: malformed function call permission")
                _account(call["receiver_id"], "key receiver_id")
                if (not isinstance(call["method_names"], list)
                        or not all(isinstance(name, str) and name for name in call["method_names"])):
                    raise ValueError("AddKey: method_names must be a list of non-empty strings")
                if call.get("allowance") is not None:
                    _amount(call["allowance"])
        if kind == "FunctionCall":
            if (not isinstance(body["method_name"], str) or not body["method_name"]
                    or type(body["gas"]) is not int or not 1 <= body["gas"] <= canon.U64_MAX
                    or not isinstance(body["args"], str)):
                raise ValueError("FunctionCall: malformed method, gas or args")
            decoded = base64.b64decode(body["args"], validate=True)
            if body["method_name"] in ("ft_transfer", "ft_transfer_call"):
                args = _token_args(decoded)
                if not isinstance(args, dict) or not {"receiver_id", "amount"} <= set(args):
                    raise ValueError("token transfer args must contain receiver_id and amount")
                target = _account(args["receiver_id"], "token receiver_id")
                if not isinstance(args["amount"], str):
                    raise ValueError("token transfer amount must be a decimal string")
                canon.amount_field(args["amount"])
                if "memo" in args and args["memo"] is not None and not isinstance(args["memo"], str):
                    raise ValueError("token transfer memo must be text or null")
                if body["method_name"] == "ft_transfer_call" and not isinstance(args.get("msg"), str):
                    raise ValueError("ft_transfer_call args must contain a text msg")
                if target not in accounts:
                    accounts.append(target)
        parsed.append((kind, body, args))
    try:
        evidence.canonical_bytes(actions)
    except (TypeError, UnicodeError) as error:
        raise ValueError("transaction actions must have a canonical JSON representation") from error
    return parsed, accounts


def check_transaction(transaction, *, store):
    """Check proposed actions using local labels and incident records, without signing."""
    parsed, accounts = _validate(transaction)
    actions_hash = evidence.evidence_hash(transaction["actions"])
    action_source = rules._local("transaction actions", actions_hash)
    store_source = rules._local("label store", store.sha256)
    checks, labels_used = [], []
    for kind, body, args in parsed:
        if kind == "AddKey":
            permission = body["access_key"]["permission"]
            public_key = body["public_key"]
            if permission == "FullAccess":
                checks.append(evidence.check("adds_full_access_key", "signal",
                                             f"adds full access key {public_key}", action_source))
            else:
                call = permission["FunctionCall"]
                limits = []
                if not call["method_names"]:
                    limits.append("empty method_names permits any method")
                if call.get("allowance") is None:
                    limits.append("no allowance limit")
                checks.append(evidence.check("function_call_key", "unknown" if limits else "pass",
                    f"function call key {public_key}: " + ("; ".join(limits) if limits
                    else "method_names and allowance are limited"), action_source))
        elif kind == "DeleteAccount":
            checks.append(evidence.check("deletes_account", "signal",
                f"deletes the account and sends its balance to {body['beneficiary_id']}", action_source))
        elif kind == "DeployContract":
            digest = code_hash(body["code"])
            incident = Context(store).code_hashes.get(digest)
            checks.append(evidence.check("deploys_code", "signal" if incident else "unknown",
                f"deploys code hash {digest}, reported by incident {incident}" if incident
                else f"deploys code hash {digest}; the code has not been assessed",
                rules._local("incident pages") if incident else action_source))
        elif kind not in ("CreateAccount", "Transfer") and not (kind == "FunctionCall" and args is not None):
            detail = f"unmodelled action {kind}"
            if kind == "FunctionCall":
                detail += f" method {rules._quoted(body['method_name'])}"
            if kind == "DeleteKey":
                detail += (f" deletes key {body['public_key']}; the transaction cannot show whether it is "
                           "the last full access key")
            checks.append(evidence.check("unrecognised_action", "unknown", detail, action_source))

    consulted = [assertion for account in accounts for assertion in rules._current(store, account)]
    labels_used += [assertion.id for assertion in consulted]
    hits = [assertion for assertion in consulted if assertion.category in rules.DENY_CATEGORIES]
    detail = "; ".join(f"{a.subject} carries the {a.category} label {rules._quoted(a.label)} (assertion {a.id})"
                       for a in hits) if hits else "no denylist or incident label on " + ", ".join(accounts)
    label_check = evidence.check("receiver_labels", "signal" if hits else "pass", detail, store_source)
    checks.append(label_check)
    lookalikes = []
    for account in accounts:
        own = [a.id for a in rules._current(store, account) if a.category in rules.REFERENCE_CATEGORIES]
        hit = rules._lookalike({}, store, account, own)
        if hit:
            detail, matched = hit[1:]
            lookalikes.append(f"{account}: {detail}")
            labels_used += matched
    checks.append(evidence.check("lookalike", "signal" if lookalikes else "pass",
        "; ".join(lookalikes) if lookalikes else "no labelled NEAR account within the lookalike distance",
        store_source))
    statuses = {check["status"] for check in checks}
    verdict = "DENY" if "signal" in statuses else "UNKNOWN" if "unknown" in statuses else "ALLOW"
    if verdict == "ALLOW":
        label_check["detail"] = "no modelled risk found"
    outcome = "ASK" if verdict == "UNKNOWN" else verdict
    reasons = (["no modelled risk found"] if verdict == "ALLOW" else
               [check["detail"] for check in checks if check["status"] == ("signal" if verdict == "DENY" else "unknown")])
    observed = {"block_height": None, "timestamp_ms": None}
    document = evidence.document(
        subject={"network": "mainnet", "kind": "transaction", "signer": transaction["signer_id"],
                 "receiver": transaction["receiver_id"], "actions_sha256": actions_hash},
        checks=checks, labels=list(dict.fromkeys(labels_used)), verdict=verdict, reasons=reasons,
        observed=observed, issued_ms=None, expires_ms=None, verdict_id=None, latency_ms=None)
    return {"evidence": evidence.canonical_bytes(document).decode("utf-8"),
            "evidence_hash": evidence.evidence_hash(document),
            "checks": [{"name": check["id"], "status": check["status"], "detail": check["detail"]}
                       for check in checks],
            "reasons": reasons, "outcome": outcome,
            "action": {"ALLOW": "sign", "DENY": "refuse", "ASK": "ask"}[outcome],
            "verdict": None, "signing": "never", "freshness": observed, "latency_ms": None}
