"""The incident and attempt record: one object for a reported event and for one we observe.

    docs/RECORD.md is the spec, intel/record_enums.json holds the vocabulary the contract lane and
    this module share, fixtures/records/ holds examples, and tests/test_records.py holds the rules.

Two axes, not one list: kind is what the event is, outcome is what it cost. An attempt and a loss of
the same kind are the same event with a different outcome.

A record is content addressed. record_id is the sha256 of the compact JSON of the other fields with
sorted keys, so nothing edits a record in place. A correction is a new record whose supersedes names
the one it replaces, and the store marks the old one superseded; no state is rewritten.

verdict_for is the only place a subject's records become an answer. The desk renderer calls it
today with the summary the contract is specified to return; the payment path is not built on this
branch and must call it rather than keep a second copy of the ladder.
"""

import hashlib
import json
import re
from pathlib import Path

ENUMS_PATH = Path(__file__).resolve().parent / "record_enums.json"
ENUMS = json.loads(ENUMS_PATH.read_text(encoding="utf-8"))

KINDS = tuple(ENUMS["kinds"])
OUTCOMES = tuple(ENUMS["outcomes"])
STATES = tuple(ENUMS["states"])

VERSION = "1"
EVENTS = ("first_chain_signal", "first_public_report", "our_record", "report_received",
          "moderation", "status_change", "superseded", "closed")
SOURCE_TYPES = ("chain", "page", "report", "moderation", "internal")

TRANSITIONS = {
    "pending": ("accepted", "rejected", "withdrawn"),
    "accepted": ("contested",),
    "rejected": ("contested",),
    "contested": ("accepted", "rejected", "overturned"),
    "withdrawn": (),
    "overturned": (),
}

FIELDS = ("version", "record_id", "kind", "outcome", "state", "subjects", "reports", "evidence",
          "moderation", "timeline", "supersedes", "publisher", "observed")

SUMMARY_FIELDS = ("accepted", "pending", "contested", "latest_accepted_id", "deny")
REPORT_BODY_FIELDS = ("evidence_sha256", "evidence_url", "kind", "outcome", "subject", "submitter")

# The kinds an accepted report may deny for. A social report is an ask, because a human was
# persuaded rather than a contract exploited, and that is a conversation before it is a block.
DENY_KINDS = ("scam", "theft", "phishing", "exploit", "key_compromise")

# A state or a summary we cannot read is never a pass.
UNREADABLE = "UNKNOWN"

_ACCOUNT_ID = re.compile(r"(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+", re.ASCII)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_URL = re.compile(r"https://\S+")
URL_BYTES = 512


class RecordError(ValueError):
    """A record that breaks one of the rules in docs/RECORD.md."""


def canonical(record):
    """The bytes record_id hashes: compact JSON, sorted keys, UTF-8, no trailing newline."""
    body = {key: record[key] for key in record if key != "record_id"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def record_id(record):
    """The sha256 hex of the record without its id."""
    return hashlib.sha256(canonical(record)).hexdigest()


def _latest_key(record):
    """Order records the way the contract's counter does: the greatest report seq, then the id."""
    seqs = [entry["seq"] for entry in record["reports"]]
    return (max(seqs) if seqs else 0, record["record_id"])


def summary(records):
    """The bounded subject summary the contract returns, from the records held for one subject.

    The counts are per state, and latest_accepted_id is the accepted record with the greatest report
    seq, so this mirrors subject_summary on chain rather than inventing a second ordering.

    deny is the one bit that keeps the ladder honest over several records: it is true when any
    accepted or contested record holds a DENY kind. Counting records alone cannot answer that, and
    reading only the latest accepted record loses an earlier DENY when a later accepted social
    report arrives, which is the case the ladder exists to prevent.
    """
    accepted = [record for record in records if record["state"] == "accepted"]
    latest = max(accepted, key=_latest_key) if accepted else None
    return {
        "accepted": len(accepted),
        "pending": sum(1 for record in records if record["state"] == "pending"),
        "contested": sum(1 for record in records if record["state"] == "contested"),
        "latest_accepted_id": latest["record_id"] if latest else None,
        "deny": any(record["kind"] in DENY_KINDS
                    for record in records if record["state"] in ("accepted", "contested")),
    }


def report_body(body):
    """The canonical report body the contract hashes into body_sha256.

    Exactly six fields, keys sorted, compact JSON, UTF-8, no nulls and no trailing newline. The
    contract lane must build the same bytes from fixtures/records/report-body-vector.json; that
    Rust test is part of the contract change, not this branch, so today only this module checks the
    vector.
    """
    if not isinstance(body, dict) or tuple(sorted(body)) != REPORT_BODY_FIELDS:
        raise RecordError(f"report body: exactly the fields {REPORT_BODY_FIELDS} are required")
    for field in REPORT_BODY_FIELDS:
        if body[field] is None:
            raise RecordError(f"report body {field}: a null is not allowed")
    if body["kind"] not in KINDS:
        raise RecordError(f"report body kind: {body['kind']!r} is not one of {KINDS}")
    if body["outcome"] not in OUTCOMES:
        raise RecordError(f"report body outcome: {body['outcome']!r} is not one of {OUTCOMES}")
    _hex64(body["evidence_sha256"], "report body evidence_sha256")
    _url(body["evidence_url"], "report body evidence_url")
    _account(body["subject"], "report body subject")
    _account(body["submitter"], "report body submitter")
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def body_sha256(body):
    """The sha256 hex of the canonical report body."""
    return hashlib.sha256(report_body(body)).hexdigest()


def verdict_for(summary_row, *, allowlisted=False):
    """What the rules may answer for one subject, from the contract summary alone.

    One ladder, in order: DENY beats UNKNOWN, which beats no effect. The summary carries deny, so a
    subject with an accepted exploit record and a later accepted social record still denies: the
    ladder reads every accepted and contested record, not the latest one.

    A contested report keeps its DENY with no allowlist exemption, because otherwise an accused
    account could switch its own accepted report off by disputing it. An accepted social report
    asks. pending asks. On an allowlisted payee an ask has no effect, so a report with a storage
    deposit cannot stall a payment to a payee the owner already trusts; a DENY still applies,
    because an allowlisted account can be compromised.

    The summary is the only input, so the contract must refuse a report whose kind is outside the
    vocabulary at accept time; a stored record then always has a known kind. A summary that cannot
    be read, one whose deny bit disagrees with its own counts, or one whose latest_accepted_id
    disagrees with its accepted count reads UNKNOWN and is never forgiven by the allowlist. That
    last check matters on the allowlist: accepted 1 with no latest id, or an id with no accepted
    record, would otherwise reach the no-effect branch and pass.
    """
    if not isinstance(summary_row, dict) or tuple(sorted(summary_row)) != tuple(sorted(SUMMARY_FIELDS)):
        return UNREADABLE
    counts = []
    for field in ("accepted", "pending", "contested"):
        value = summary_row[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return UNREADABLE
        counts.append(value)
    accepted, pending, contested = counts
    latest = summary_row["latest_accepted_id"]
    if latest is not None and not (
            isinstance(latest, str) and _HEX64.fullmatch(latest)):
        return UNREADABLE
    deny = summary_row["deny"]
    if not isinstance(deny, bool):
        return UNREADABLE
    if (accepted > 0) != (latest is not None):
        return UNREADABLE
    if deny and not (accepted or contested):
        return UNREADABLE

    if deny:
        return "DENY"
    if accepted or contested:
        if contested:
            return "UNKNOWN"
        return None if allowlisted else "UNKNOWN"
    if pending:
        return None if allowlisted else "UNKNOWN"
    return None


def may_transition(before, after):
    """True when a record may move from one state to the other."""
    return after in TRANSITIONS.get(before, ())


def _text(value, name, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise RecordError(f"{name}: a non empty string of at most {limit} characters is required")
    return value


def _url(value, name):
    """An https url of at most 512 bytes, measured in bytes because the limit is a byte limit."""
    if not isinstance(value, str) or not _URL.fullmatch(value):
        raise RecordError(f"{name}: an https url of at most {URL_BYTES} bytes is required")
    if len(value.encode("utf-8")) > URL_BYTES:
        raise RecordError(f"{name}: an https url of at most {URL_BYTES} bytes is required, "
                          f"not {len(value.encode('utf-8'))}")
    return value


def _hex64(value, name):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise RecordError(f"{name}: 64 lowercase hex characters are required")
    return value


def _account(value, name="subject"):
    if not isinstance(value, str) or len(value) > 64 or not _ACCOUNT_ID.fullmatch(value):
        raise RecordError(f"{name}: {value!r} is not a NEAR account id")
    return value


def _source(source):
    if not isinstance(source, dict) or tuple(sorted(source)) != ("ref", "type"):
        raise RecordError("source: exactly the fields type and ref are required")
    if source["type"] not in SOURCE_TYPES:
        raise RecordError(f"source type: {source['type']!r} is not one of {SOURCE_TYPES}")
    _text(source["ref"], "source ref", 300)
    if source["type"] == "page":
        _url(source["ref"], "source ref")
    if source["type"] == "chain" and not str(source["ref"]).isdigit():
        raise RecordError(f"source ref: a chain source needs a block height, not {source['ref']!r}")
    if source["type"] in ("report", "moderation") and not _HEX64.fullmatch(str(source["ref"])):
        raise RecordError(f"source ref: a {source['type']} source needs a sha256 hex id")
    return source


def _evidence(entry):
    if not isinstance(entry, dict) or tuple(sorted(entry)) != ("fetched_at", "sha256", "url"):
        raise RecordError("evidence: exactly the fields sha256, url and fetched_at are required")
    _hex64(entry["sha256"], "evidence sha256")
    _url(entry["url"], "evidence url")
    if not isinstance(entry["fetched_at"], str) or not _TIME.fullmatch(entry["fetched_at"]):
        raise RecordError("evidence fetched_at: a UTC time YYYY-MM-DDTHH:MM:SSZ is required")
    return entry


def _report(entry):
    if not isinstance(entry, dict) or tuple(sorted(entry)) != ("seq", "sha256"):
        raise RecordError("reports: exactly the fields seq and sha256 are required")
    _hex64(entry["sha256"], "report sha256")
    if isinstance(entry["seq"], bool) or not isinstance(entry["seq"], int) or entry["seq"] < 1:
        raise RecordError("report seq: the contract counter, a positive integer")
    return entry


def _moderation(entry):
    if not isinstance(entry, dict) or tuple(sorted(entry)) != ("decided_at", "decision", "reason",
                                                               "reason_sha256"):
        raise RecordError("moderation: exactly the fields decision, decided_at, reason_sha256 and "
                          "reason are required")
    if entry["decision"] not in STATES:
        raise RecordError(f"moderation decision: {entry['decision']!r} is not one of {STATES}")
    if not isinstance(entry["decided_at"], str) or not _TIME.fullmatch(entry["decided_at"]):
        raise RecordError("moderation decided_at: a UTC time YYYY-MM-DDTHH:MM:SSZ is required")
    _hex64(entry["reason_sha256"], "moderation reason_sha256")
    _text(entry["reason"], "moderation reason", 2000)
    return entry


def _timeline_entry(entry):
    if not isinstance(entry, dict) or tuple(sorted(entry)) != ("at", "event", "source"):
        raise RecordError("timeline: exactly the fields event, at and source are required")
    if entry["event"] not in EVENTS:
        raise RecordError(f"timeline event: {entry['event']!r} is not one of {EVENTS}")
    if not isinstance(entry["at"], str) or not _TIME.fullmatch(entry["at"]):
        raise RecordError("timeline at: a UTC time YYYY-MM-DDTHH:MM:SSZ is required")
    _source(entry["source"])
    return entry


def validate(record):
    """Return the record unchanged, or raise RecordError naming the first broken rule."""
    if not isinstance(record, dict) or tuple(sorted(record)) != tuple(sorted(FIELDS)):
        raise RecordError(f"a record has exactly the fields {FIELDS}")
    if record["version"] != VERSION:
        raise RecordError(f"version: only {VERSION!r} is known, not {record['version']!r}")
    if record["kind"] not in KINDS:
        raise RecordError(f"kind: {record['kind']!r} is not one of {KINDS}")
    if record["outcome"] not in OUTCOMES:
        raise RecordError(f"outcome: {record['outcome']!r} is not one of {OUTCOMES}")
    if record["state"] not in STATES:
        raise RecordError(f"state: {record['state']!r} is not one of {STATES}")

    subjects = record["subjects"]
    if not isinstance(subjects, list) or not subjects:
        raise RecordError("subjects: at least one account is required")
    for subject in subjects:
        _account(subject)
    if subjects != sorted(set(subjects)):
        raise RecordError("subjects: sorted and without duplicates")

    reports = record["reports"]
    if not isinstance(reports, list):
        raise RecordError("reports: a list, empty when we hold the record alone")
    for entry in reports:
        _report(entry)
    if [entry["seq"] for entry in reports] != sorted({entry["seq"] for entry in reports}):
        raise RecordError("reports: ordered by seq, without duplicates")

    evidence = record["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise RecordError("evidence: at least one document is required")
    for entry in evidence:
        _evidence(entry)

    moderation = record["moderation"]
    if not isinstance(moderation, list):
        raise RecordError("moderation: a list, empty until the first decision")
    for entry in moderation:
        _moderation(entry)

    timeline = record["timeline"]
    if not isinstance(timeline, list) or not timeline:
        raise RecordError("timeline: at least one entry is required")
    for entry in timeline:
        _timeline_entry(entry)
    times = [entry["at"] for entry in timeline]
    if times != sorted(times):
        raise RecordError("timeline: entries are append only, so at never decreases")

    supersedes = record["supersedes"]
    if supersedes is not None:
        _hex64(supersedes, "supersedes")
        if supersedes == record["record_id"]:
            raise RecordError("supersedes: a record cannot supersede itself")

    _account(record["publisher"], "publisher")

    observed = record["observed"]
    if not isinstance(observed, dict) or tuple(sorted(observed)) != ("chain", "first_block"):
        raise RecordError("observed: exactly the fields chain and first_block are required")
    if observed["chain"] != "near":
        raise RecordError(f"observed chain: near is required, not {observed['chain']!r}")
    block = observed["first_block"]
    if block is not None and (isinstance(block, bool) or not isinstance(block, int) or block < 1):
        raise RecordError("observed first_block: null or a positive block height")

    if not isinstance(record["record_id"], str) or record["record_id"] != record_id(record):
        raise RecordError(f"record_id: {record['record_id']!r} is not the sha256 of the record")
    return record


def row_line(record):
    """The committed line for a record: compact JSON with the fields in FIELDS order."""
    return json.dumps({key: record[key] for key in FIELDS}, separators=(",", ":"), ensure_ascii=False)
