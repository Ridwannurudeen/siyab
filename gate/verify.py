"""python -m gate verify: check a registry record against its evidence file, offline.

A record is read in either shape the registry gives (PLAN-v3 section 3b): the `latest(subject)`
view, {verdict, evidence_hash, observed_block, recorded_ms, verdict_id}, or the data of a NEP-297
`verdict_recorded` event, {subject, verdict, evidence_hash, observed_block, verdict_id}, given
alone or inside the whole event. The verdict may be written ALLOW, Allow or its code (0 Unknown,
1 Allow, 2 Deny), and the evidence hash as 64 hex characters or a list of 32 bytes.

verify recomputes the sha256 of the evidence file's bytes and compares it with the record's
evidence_hash, checks that the evidence's verdict is the record's, and, given a Verdict file and a
verifier key, rebuilds the canonical message from the evidence and the Verdict and checks the
signature with gate.ed25519.verify. Nothing here touches the network.
"""

import hashlib
import json
from pathlib import Path

from gate import base58, canon, ed25519

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


def verify(record_path, evidence_path, verdict_path=None, key=None):
    """(ok, lines): every comparison made, as printable lines, and whether all of them held."""
    if (verdict_path is None) != (key is None):
        raise VerifyError("--verdict and --key go together")
    record = load_record(json.loads(Path(record_path).read_text(encoding="utf-8")))
    data = Path(evidence_path).read_bytes()
    document = json.loads(data)
    computed = hashlib.sha256(data).hexdigest()
    results = [("evidence sha256 equals the record's evidence_hash", computed == record["evidence_hash"]),
               ("evidence verdict equals the record's verdict", document.get("verdict") == record["verdict"])]
    lines = [f"record shape: {record['shape']}", f"evidence sha256: {computed}",
             f"record evidence_hash: {record['evidence_hash']}"]
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
    subject = record["subject"] or document["subject"]["receiver"]
    lines.append("NEP-297 fields to compare with the explorer:")
    lines.append(f"  standard {STANDARD}, version {EVENT_VERSION}, event {EVENT}")
    lines.append(f"  subject {subject}" + ("" if record["subject"] else " (from the evidence; the view shape has none)"))
    lines.append(f"  verdict {record['verdict']}, evidence_hash {record['evidence_hash']}")
    lines.append(f"  observed_block {record['observed_block']}, verdict_id {record['verdict_id']}")
    lines += [f"{'ok  ' if held else 'FAIL'} {name}" for name, held in results]
    ok = all(held for _, held in results)
    lines.append("verified" if ok else "not verified")
    return ok, lines
