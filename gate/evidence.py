"""The evidence document behind a verdict, and the sha256 hash that the signed message binds.

A document is a dict with these keys:

    version      "1"
    subject      {gate, agent_pk, kind, token, receiver, amount, memo_sha256 or null}
    checks       [{id, status: "pass" | "signal" | "unknown", detail,
                   source: {provider, request_sha256, fetched_at}}]
    labels       the label assertion ids the rules consulted
    verdict      "ALLOW", "DENY" or "UNKNOWN"
    reasons      the reasons for the verdict, as strings
    observed     {block_height, timestamp_ms}
    issued_ms, expires_ms, verdict_id   set on ALLOW only, else null
    latency_ms   the time the verdict took

Its canonical bytes are compact JSON with sorted keys in UTF-8 and no trailing newline. The
evidence hash is the sha256 hex of those bytes, and the document is stored as <hash>.json
holding exactly those bytes, so `sha256sum` of the file equals its name and the hash in the
signed message.
"""

import hashlib
import json
from pathlib import Path

VERSION = "1"
STATUSES = ("pass", "signal", "unknown")


def subject(*, gate, agent_pk, kind, token, receiver, amount, memo):
    """The subject block: the transfer the verdict is about; the memo is kept as its hash."""
    return {
        "gate": gate,
        "agent_pk": agent_pk,
        "kind": kind,
        "token": token,
        "receiver": receiver,
        "amount": amount,
        "memo_sha256": None if memo is None else hashlib.sha256(memo.encode("utf-8")).hexdigest(),
    }


def check(check_id, status, detail, source):
    """One check result; status is pass, signal or unknown."""
    if status not in STATUSES:
        raise ValueError(f"check {check_id}: status {status!r} is not one of {STATUSES}")
    return {"id": check_id, "status": status, "detail": detail, "source": source}


def document(*, subject, checks, labels, verdict, reasons, observed, issued_ms, expires_ms,
             verdict_id, latency_ms):
    """A version 1 evidence document."""
    return {
        "version": VERSION,
        "subject": subject,
        "checks": checks,
        "labels": labels,
        "verdict": verdict,
        "reasons": reasons,
        "observed": observed,
        "issued_ms": issued_ms,
        "expires_ms": expires_ms,
        "verdict_id": verdict_id,
        "latency_ms": latency_ms,
    }


def canonical_bytes(doc):
    """Compact JSON with sorted keys, UTF-8, no trailing newline."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def evidence_hash(doc):
    """The sha256 hex of the canonical bytes."""
    return hashlib.sha256(canonical_bytes(doc)).hexdigest()


def write(doc, directory):
    """Store the canonical bytes as <directory>/<hash>.json and return the path."""
    data = canonical_bytes(doc)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{hashlib.sha256(data).hexdigest()}.json"
    path.write_bytes(data)
    return path


def check_file(path):
    """True when the file's sha256 equals its name and its bytes are the canonical form."""
    path = Path(path)
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest() == path.stem and canonical_bytes(json.loads(data)) == data
