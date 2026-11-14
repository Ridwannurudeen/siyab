"""python -m gate verify: check a registry record against its evidence file, offline.

A record is read in either shape the registry gives (PLAN-v3 section 3b): the `latest(subject)`
view, {verdict, evidence_hash, observed_block, recorded_ms, verdict_id}, or the data of a NEP-297
`verdict_recorded` event, {subject, verdict, evidence_hash, observed_block, verdict_id}, given
alone or inside the whole event. The verdict may be written ALLOW, Allow or its code (0 Unknown,
1 Allow, 2 Deny), and the evidence hash as 64 hex characters or a list of 32 bytes.

verify recomputes the sha256 of the evidence file's bytes and compares it with the record's
evidence_hash, checks the verdict, event subject and available observed block height, and, given
a Verdict file and a verifier key, rebuilds the canonical message from the evidence and the
Verdict and checks the signature with gate.ed25519.verify. Nothing here touches the network.
"""

import hashlib
import json
import unicodedata
from pathlib import Path

from gate import base58, canon, ed25519, evidence

STANDARD = "shieldbot-near"
EVENT_VERSION = "1.0.0"
EVENT = "verdict_recorded"
VERDICT_CODES = {0: "UNKNOWN", 1: "ALLOW", 2: "DENY"}
VIEW_KEYS = {"verdict", "evidence_hash", "observed_block", "recorded_ms", "verdict_id"}
EVENT_KEYS = {"subject", "verdict", "evidence_hash", "observed_block", "verdict_id"}


class VerifyError(ValueError):
    """A record, evidence or Verdict file that cannot be read in the expected shape."""


def _verdict(value):
    if isinstance(value, bool):
        raise VerifyError(f"verdict: not a verdict: {value!r}")
    if isinstance(value, int) and value in VERDICT_CODES:
        return VERDICT_CODES[value]
    if isinstance(value, str) and value.upper() in VERDICT_CODES.values():
        return value.upper()
    raise VerifyError(f"verdict: not ALLOW, DENY, UNKNOWN or their code: {value!r}")


def _hash(value):
    if isinstance(value, list) and len(value) == 32 and all(isinstance(b, int) and 0 <= b <= 255 for b in value):
        return bytes(value).hex()
    if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    raise VerifyError(f"evidence_hash: not 64 lowercase hex characters or 32 bytes: {value!r}")


def load_record(document):
    """{shape, subject, verdict, evidence_hash, observed_block, verdict_id, recorded_ms} of a record."""
    if isinstance(document, dict) and document.get("event") == EVENT and isinstance(document.get("data"), list):
        if document.get("standard") != STANDARD or len(document["data"]) != 1:
            raise VerifyError("the event is not one shieldbot-near verdict_recorded record")
        document = document["data"][0]
    if not isinstance(document, dict):
        raise VerifyError("the record is not a JSON object")
    if set(document) == VIEW_KEYS:
        shape, subject = "view", None
    elif set(document) == EVENT_KEYS:
        shape, subject = "event", canon.account_id(document["subject"], "subject")
    else:
        raise VerifyError(f"the record has neither the view keys {sorted(VIEW_KEYS)} nor the event keys "
                          f"{sorted(EVENT_KEYS)}")
    block = document["observed_block"]
    if isinstance(block, bool) or not isinstance(block, int) or block < 0:
        raise VerifyError(f"observed_block: not a block height: {block!r}")
    return {"shape": shape, "subject": subject, "verdict": _verdict(document["verdict"]),
            "evidence_hash": _hash(document["evidence_hash"]), "observed_block": block,
            "verdict_id": document["verdict_id"], "recorded_ms": document.get("recorded_ms")}


def rebuild_message(evidence_document, verdict_object):
    """The canonical ALLOW message for the transfer in the evidence and the Verdict's fields; the
    literal is always ALLOW, never the one the Verdict file carries."""
    subject = evidence_document["subject"]
    message = canon.canonical_message(
        gate=subject["gate"], agent_pk_hex=subject["agent_pk"], kind=subject["kind"], token_id=subject["token"],
        receiver_id=subject["receiver"], amount=subject["amount"], memo=None,
        issued_ms=verdict_object["issued_ms"], expires_ms=verdict_object["expires_ms"],
        verdict_id=verdict_object["verdict_id"], evidence_sha256=verdict_object["evidence_hash"],
        key_id=verdict_object["key_id"])
    parts = message.decode("utf-8").split("|")
    if subject["memo_sha256"] is not None:
        # The evidence keeps the memo as its sha256, which is exactly the message's memo field.
        parts[7] = subject["memo_sha256"]
    rebuilt = "|".join(parts).encode("utf-8")
    canon.parse_canonical(rebuilt)
    return rebuilt


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VerifyError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _receipt_text(value):
    return "".join(json.dumps(char)[1:-1] if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp")
                   else char for char in json.dumps(value, ensure_ascii=False)[1:-1])


def verify_receipt(path, *, transaction=None):
    """Check an unsigned answer against its canonical evidence and original transaction, offline."""
    from intel.live import LiveAPI

    answer = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    required = {"evidence", "evidence_hash", "checks", "reasons", "outcome", "action",
                "verdict", "signing", "freshness", "latency_ms"}
    if not isinstance(answer, dict) or set(answer) != required:
        raise VerifyError("the receipt must have exactly the answer fields")
    if not isinstance(answer["evidence"], str):
        raise VerifyError("evidence must be a string")
    data = answer["evidence"].encode("utf-8")
    if hashlib.sha256(data).hexdigest() != answer["evidence_hash"]:
        raise VerifyError("evidence sha256 does not equal evidence_hash")
    document = json.loads(data, object_pairs_hook=_unique_object)
    if evidence.canonical_bytes(document) != data:
        raise VerifyError("evidence is not canonical JSON")
    required = {"version", "subject", "checks", "labels", "verdict", "reasons", "observed",
                "issued_ms", "expires_ms", "verdict_id", "latency_ms"}
    if not isinstance(document, dict) or set(document) != required:
        raise VerifyError("evidence must have exactly the document fields")
    if document["version"] != evidence.VERSION:
        raise VerifyError("evidence version is not supported")
    if any(document[field] is not None for field in ("issued_ms", "expires_ms", "verdict_id")):
        raise VerifyError("check evidence must have null issued_ms, expires_ms and verdict_id")
    subject = document["subject"]
    subject_fields = {
        "near": {"network", "kind", "token", "receiver", "amount", "memo_sha256"},
        "ft": {"network", "kind", "token", "receiver", "amount", "memo_sha256"},
        "account": {"network", "kind", "account"},
        "token": {"network", "kind", "token"},
        "transaction": {"network", "kind", "signer", "receiver", "actions_sha256"},
    }
    if (not isinstance(subject, dict) or not isinstance(subject.get("kind"), str)
            or subject["kind"] not in subject_fields or set(subject) != subject_fields[subject["kind"]]
            or subject["network"] != "mainnet"):
        raise VerifyError("evidence subject is not a supported mainnet check subject")
    kind = subject["kind"]
    account_fields = {"near": ("receiver",), "ft": ("receiver", "token"), "account": ("account",),
                      "token": ("token",), "transaction": ("signer", "receiver")}
    for field in account_fields[kind]:
        canon.account_id(subject[field], field)
        if subject[field] == "testnet" or subject[field].endswith(".testnet"):
            raise VerifyError(f"check evidence must have a mainnet {field}")
    if kind in ("near", "ft"):
        if subject["memo_sha256"] is not None:
            raise VerifyError("payment check evidence must have a null memo_sha256")
        if not isinstance(subject["amount"], str):
            raise VerifyError("payment check evidence amount must be a string")
        canon.amount_field(subject["amount"])
        if kind == "near" and subject["token"] is not None:
            raise VerifyError("native payment evidence must have a null token")
    if kind == "transaction":
        from gate.txcheck import _validate

        canon._hex_field(subject["actions_sha256"], 64, "actions_sha256")
        if transaction is None:
            raise VerifyError("transaction check receipt requires the original transaction")
        _validate(transaction)
        if subject["signer"] != transaction["signer_id"] or subject["receiver"] != transaction["receiver_id"]:
            raise VerifyError("transaction signer or receiver does not match the evidence subject")
        if subject["actions_sha256"] != evidence.evidence_hash(transaction["actions"]):
            raise VerifyError("actions_sha256 does not match the canonical transaction actions")
    elif transaction is not None:
        raise VerifyError("transaction input requires a transaction check subject")
    observed = document["observed"]
    if (not isinstance(observed, dict) or set(observed) != {"block_height", "timestamp_ms"}
            or any(value is not None and (type(value) is not int or value < 0)
                   for value in (observed["block_height"], observed["timestamp_ms"]))):
        raise VerifyError("evidence observed block and timestamp are malformed")
    if (not isinstance(document["labels"], list)
            or not all(isinstance(label, str) for label in document["labels"])
            or not isinstance(document["reasons"], list)
            or not all(isinstance(reason, str) for reason in document["reasons"])
            or not isinstance(document["verdict"], str)):
        raise VerifyError("evidence labels, reasons or verdict are malformed")
    if document["latency_ms"] is not None and type(document["latency_ms"]) is not int:
        raise VerifyError("evidence latency_ms must be an integer or null")
    checks = document["checks"]
    if not isinstance(checks, list) or not checks:
        raise VerifyError("evidence checks must be a non-empty list")
    for check in checks:
        if (not isinstance(check, dict) or set(check) != {"id", "status", "detail", "source"}
                or not isinstance(check["id"], str) or not check["id"]
                or check["status"] not in evidence.STATUSES or not isinstance(check["detail"], str)
                or not isinstance(check["source"], dict)
                or set(check["source"]) != {"provider", "request_sha256", "fetched_at"}
                or not isinstance(check["source"]["provider"], str)
                or any(check["source"][field] is not None and not isinstance(check["source"][field], str)
                       for field in ("request_sha256", "fetched_at"))):
            raise VerifyError("an evidence check is malformed")
    reduced = LiveAPI._verdict_shape(document["verdict"], checks, document["reasons"],
                                     answer["evidence_hash"], observed, document["latency_ms"])
    for field in ("checks", "reasons", "outcome", "action", "freshness", "latency_ms"):
        if evidence.canonical_bytes(answer[field]) != evidence.canonical_bytes(reduced[field]):
            raise VerifyError(f"answer {field} does not match the evidence")
    if answer["verdict"] is not None or answer["signing"] != "never":
        raise VerifyError("a check receipt must be unsigned with signing never")
    if kind in ("near", "ft"):
        lines = [f"receiver: {subject['receiver']}", f"token: {subject['token'] or 'near'}",
                 f"amount: {subject['amount']}"]
    elif kind == "account":
        lines = ["kind: account", f"account: {subject['account']}"]
    elif kind == "token":
        lines = ["kind: token", f"token: {subject['token']}"]
    else:
        lines = ["kind: transaction", f"signer: {subject['signer']}", f"receiver: {subject['receiver']}",
                 f"actions sha256: {subject['actions_sha256']}"]
    lines.append(f"observed block: {observed['block_height'] if observed['block_height'] is not None else 'unknown'}")
    lines += [f"{check['status']:<7} {_receipt_text(check['id'])}: {_receipt_text(check['detail'])}"
              for check in checks]
    lines.append(f"outcome: {answer['outcome']} ({answer['action']})")
    lines.append("receipt consistent (unsigned: this checks the answer against its own evidence, "
                 "it does not authenticate the server or prove the checks are true)")
    return lines


def verify(record_path, evidence_path, verdict_path=None, key=None):
    """(ok, lines): every comparison made, as printable lines, and whether all of them held."""
    if (verdict_path is None) != (key is None):
        raise VerifyError("--verdict and --key go together")
    record = load_record(json.loads(Path(record_path).read_text(encoding="utf-8")))
    data = Path(evidence_path).read_bytes()
    document = json.loads(data)
    computed = hashlib.sha256(data).hexdigest()
    lines = [f"record shape: {record['shape']}", f"evidence sha256: {computed}",
             f"record evidence_hash: {record['evidence_hash']}"]
    if not isinstance(document, dict):
        lines += ["FAIL evidence is a JSON object", "not verified"]
        return False, lines
    results = [("evidence sha256 equals the record's evidence_hash", computed == record["evidence_hash"]),
               ("evidence verdict equals the record's verdict", document.get("verdict") == record["verdict"])]
    subject = document.get("subject")
    judged_account = (subject.get("recipient" if subject.get("kind") == "intents" else "receiver")
                      if isinstance(subject, dict) else None)
    unchecked = []
    if record["subject"] is not None:
        results.append(("evidence subject equals the record's subject", judged_account == record["subject"]))
    else:
        unchecked.append("subject not checked")
    observed = document.get("observed")
    block = observed.get("block_height") if isinstance(observed, dict) else None
    if block is not None:
        results.append(("evidence block_height equals the record's observed_block", block == record["observed_block"]))
    else:
        unchecked.append("observed block not checked")
    if verdict_path is not None:
        verdict_object = json.loads(Path(verdict_path).read_text(encoding="utf-8"))
        public = base58.decode_key(key)
        if len(public) != 32:
            raise VerifyError("--key: not a 32 byte ed25519 public key")
        results.append(("verdict literal is ALLOW", verdict_object.get("literal") == canon.LITERAL))
        results.append(("verdict evidence_hash equals the evidence sha256", verdict_object["evidence_hash"] == computed))
        results.append(("verdict verdict_id equals the record's verdict_id",
                        verdict_object["verdict_id"] == record["verdict_id"]))
        try:
            message = rebuild_message(document, verdict_object)
            signed = ed25519.verify(public, canon.digest(message), bytes.fromhex(verdict_object["signature"]))
        except (canon.CanonError, KeyError, ValueError):
            message, signed = None, False
        results.append((f"signature verifies with {key}", signed))
        if message is not None:
            lines.append(f"canonical message: {message.decode('utf-8')}")
    subject = record["subject"] or judged_account
    lines.append("NEP-297 fields to compare with the explorer:")
    lines.append(f"  standard {STANDARD}, version {EVENT_VERSION}, event {EVENT}")
    lines.append(f"  subject {subject if subject is not None else 'unknown'}" +
                 ("" if record["subject"] else " (from the evidence; the view shape has none)"))
    lines.append(f"  verdict {record['verdict']}, evidence_hash {record['evidence_hash']}")
    lines.append(f"  observed_block {record['observed_block']}, verdict_id {record['verdict_id']}")
    lines += [f"{'ok  ' if held else 'FAIL'} {name}" for name, held in results]
    ok = all(held for _, held in results)
    lines.append(("verified" + (f" ({'; '.join(unchecked)})" if unchecked else "")) if ok else "not verified")
    return ok, lines
