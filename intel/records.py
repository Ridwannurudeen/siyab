"""Read and write the incident record format in docs/RECORD.md."""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from gate.canon import CanonError, account_id
from gate.evidence import canonical_bytes

_FIELDS = (
    "version", "record_id", "kind", "outcome", "subjects", "evidence", "reports", "reasons",
    "assertion_ids", "timeline", "supersedes", "publisher",
)
_ENUMS = json.loads((Path(__file__).resolve().parent / "record_enums.json").read_text(encoding="utf-8"))
_HEX64 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_REPORT_STATES = frozenset(("pending", "accepted", "rejected", "withdrawn", "contested", "overturned"))
_UNDECIDED_STATES = frozenset(("pending", "withdrawn"))
_CHAIN = re.compile(r"[a-z0-9]+", re.ASCII)
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)
_EVENTS = frozenset((
    "first_chain_signal", "first_public_report", "report_submitted", "moderated", "disputed",
    "dispute_resolved", "recorded",
))
_CHAIN_EVENTS = frozenset(("first_chain_signal", "report_submitted", "moderated", "disputed", "dispute_resolved"))


class RecordError(ValueError):
    """An incident record that breaks docs/RECORD.md."""


def canonical_record_id(record) -> str:
    """Return the sha256 of canonical_bytes for the record with record_id omitted."""
    if not isinstance(record, dict):
        raise RecordError("record: an object is required")
    body = {key: value for key, value in record.items() if key != "record_id"}
    try:
        return hashlib.sha256(canonical_bytes(body)).hexdigest()
    except (TypeError, ValueError, UnicodeError) as error:
        raise RecordError(f"record: cannot be canonicalized: {error}") from error


def _shape(value, fields, path):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise RecordError(f"{path}: exactly the fields {tuple(fields)} are required")


def _text(value, path):
    if not isinstance(value, str) or not value:
        raise RecordError(f"{path}: a non empty string is required")


def _hash(value, path):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise RecordError(f"{path}: 64 lowercase hex characters are required")


def _utc_time(value, path):
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise RecordError(f"{path}: a UTC time YYYY-MM-DDTHH:MM:SSZ is required")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise RecordError(f"{path}: a valid UTC calendar time YYYY-MM-DDTHH:MM:SSZ is required") from error


def _https_url(value, path, max_bytes=None):
    if not isinstance(value, str):
        raise RecordError(f"{path}: an https URL is required")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise RecordError(f"{path}: the URL could not be parsed") from error
    if parsed.scheme != "https":
        raise RecordError(f"{path}: an https URL is required")
    if not parsed.hostname:
        raise RecordError(f"{path}: an https URL with a hostname is required")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise RecordError(f"{path}: the URL must be valid UTF-8") from error
    if max_bytes is not None and size > max_bytes:
        raise RecordError(f"{path}: the https URL must be at most {max_bytes} bytes")


def _near_account(value, path):
    try:
        account_id(value, path)
    except CanonError as error:
        raise RecordError(str(error)) from error


def _validate_evidence(evidence):
    if not isinstance(evidence, list) or not evidence:
        raise RecordError("evidence: at least one source is required")
    for index, source in enumerate(evidence):
        path = f"evidence[{index}]"
        _shape(source, ("url", "content_sha256", "fetched_at"), path)
        _https_url(source["url"], f"{path}.url", 512)
        _hash(source["content_sha256"], f"{path}.content_sha256")
        _utc_time(source["fetched_at"], f"{path}.fetched_at")


def _validate_report(citation, index, report_reader):
    path = f"reports[{index}]"
    _shape(citation, ("network", "contract", "report_id", "body_sha256"), path)
    if citation["network"] not in ("testnet", "mainnet"):
        raise RecordError(f"{path}.network: testnet or mainnet is required")
    _near_account(citation["contract"], f"{path}.contract")
    report_id = citation["report_id"]
    if type(report_id) is not int or report_id < 0:
        raise RecordError(f"{path}.report_id: a non negative integer is required")
    _hash(citation["body_sha256"], f"{path}.body_sha256")
    if report_reader is None:
        return None
    report = report_reader(citation["network"], citation["contract"], report_id)
    if not isinstance(report, dict) or "body_sha256" not in report:
        raise RecordError(f"{path}: report {report_id} is missing")
    if report["body_sha256"] != citation["body_sha256"]:
        raise RecordError(f"{path}.body_sha256: does not match report {report_id}")
    state = report.get("state")
    if not isinstance(state, str) or state not in _REPORT_STATES:
        raise RecordError(f"{path}.state: report {report_id} has an invalid state")
    dispute_ids = report.get("dispute_ids")
    if not isinstance(dispute_ids, list) or any(
        type(dispute_id) is not int or dispute_id < 0 for dispute_id in dispute_ids
    ):
        raise RecordError(f"{path}.dispute_ids: a list of non negative integers is required")
    if report["state"] == "contested" and not dispute_ids:
        raise RecordError(f"{path}.dispute_ids: a contested report must list its open dispute")
    return report


def _latest_resolved_dispute_id(report):
    dispute_ids = report["dispute_ids"]
    if report["state"] == "contested":
        return dispute_ids[-2] if len(dispute_ids) > 1 else None
    return dispute_ids[-1] if dispute_ids else None


def _validate_reasons(reasons, reports, report_views):
    if not isinstance(reasons, list):
        raise RecordError("reasons: a list is required")
    cited_ids = {citation["report_id"] for citation in reports}
    reasons_by_decision = {}
    for index, reason in enumerate(reasons):
        path = f"reasons[{index}]"
        _shape(reason, ("report_id", "dispute_id", "reason_sha256", "text"), path)
        if type(reason["report_id"]) is not int or reason["report_id"] < 0:
            raise RecordError(f"{path}.report_id: a non negative integer is required")
        dispute_id = reason["dispute_id"]
        if dispute_id is not None and (type(dispute_id) is not int or dispute_id < 0):
            raise RecordError(f"{path}.dispute_id: null or a non negative integer is required")
        _text(reason["text"], f"{path}.text")
        try:
            text_hash = hashlib.sha256(reason["text"].encode("utf-8")).hexdigest()
        except UnicodeEncodeError as error:
            raise RecordError(f"{path}.text: valid UTF-8 text is required") from error
        if reason["reason_sha256"] != text_hash:
            raise RecordError(f"{path}.reason_sha256: does not match the UTF-8 text")
        report_id = reason["report_id"]
        if report_id not in cited_ids:
            raise RecordError(f"{path}.report_id: does not cite a report in this record")
        decision = (report_id, dispute_id)
        if decision in reasons_by_decision:
            raise RecordError(f"reasons: more than one reason for decision {decision!r}")
        reasons_by_decision[decision] = (index, reason)

    if report_views is not None:
        for report_id, views in report_views.items():
            for report_view in views:
                if report_view["state"] in _UNDECIDED_STATES:
                    if any(decision[0] == report_id for decision in reasons_by_decision):
                        raise RecordError(f"reasons: report {report_id} is {report_view['state']} and has no decision")
                    continue
                _hash(report_view.get("reason_sha256"), f"reports[{report_id}].reason_sha256")
                dispute_ids = report_view["dispute_ids"]
                resolved_disputes = dispute_ids[:-1] if report_view["state"] == "contested" else dispute_ids
                expected_decisions = {(report_id, None)}
                expected_decisions.update((report_id, dispute_id) for dispute_id in resolved_disputes)
                actual_decisions = {
                    decision for decision in reasons_by_decision if decision[0] == report_id
                }
                missing = expected_decisions - actual_decisions
                if missing:
                    decision = sorted(missing, key=repr)[0]
                    raise RecordError(f"reasons: exactly one reason is required for decision {decision!r}")
                unexpected = actual_decisions - expected_decisions
                if unexpected:
                    decision = sorted(unexpected, key=repr)[0]
                    raise RecordError(f"reasons: decision {decision!r} does not exist for report {report_id}")
                expected_dispute_id = _latest_resolved_dispute_id(report_view)
                index, reason = reasons_by_decision[(report_id, expected_dispute_id)]
                if reason["reason_sha256"] != report_view["reason_sha256"]:
                    raise RecordError(f"reasons[{index}].reason_sha256: does not match report {report_id}")
    return reasons_by_decision


def _validate_event_reasons(timeline, reasons_by_decision, event_reader):
    transaction_events = {}
    matched_decisions = set()
    timeline_event_names = {"moderated": "report_moderated", "dispute_resolved": "dispute_resolved"}
    payload_fields = ("report_id", "subject", "state", "body_sha256", "moderator", "reason_sha256")
    for timeline_index, entry in enumerate(timeline):
        timeline_event = entry["event"]
        expected_event = timeline_event_names.get(timeline_event)
        if expected_event is None:
            continue
        source = entry["source"]
        transaction = source["transaction"]
        if transaction is None:
            raise RecordError(f"timeline[{timeline_index}].source.transaction: required to read decision events")
        network = source["network"]
        transaction_key = (network, transaction)
        if transaction_key not in transaction_events:
            events = event_reader(network, transaction)
            if not isinstance(events, list):
                raise RecordError(f"event_reader({network!r}, {transaction!r}): a list of events is required")
            transaction_events[transaction_key] = events
        events = transaction_events[transaction_key]
        found_event = False
        for event_index, event in enumerate(events):
            event_path = f"event_reader({network!r}, {transaction!r})[{event_index}]"
            if not isinstance(event, dict) or not all(
                isinstance(event.get(field), str) for field in ("standard", "version", "event")
            ) or not isinstance(event.get("data"), list):
                raise RecordError(f"{event_path}: a NEP-297 event object is required")
            if event["standard"] != "shieldbot-near" or event["event"] != expected_event:
                continue
            found_event = True
            if not event["data"]:
                raise RecordError(f"{event_path}.data: a decision event must include a payload")
            fields = payload_fields + (("dispute_id",) if expected_event == "dispute_resolved" else ())
            for data_index, payload in enumerate(event["data"]):
                payload_path = f"{event_path}.data[{data_index}]"
                _shape(payload, fields, payload_path)
                report_id = payload["report_id"]
                if type(report_id) is not int or report_id < 0:
                    raise RecordError(f"{payload_path}.report_id: a non negative integer is required")
                dispute_id = payload.get("dispute_id") if expected_event == "dispute_resolved" else None
                if dispute_id is not None and (type(dispute_id) is not int or dispute_id < 0):
                    raise RecordError(f"{payload_path}.dispute_id: a non negative integer is required")
                _hash(payload["reason_sha256"], f"{payload_path}.reason_sha256")
                decision = (report_id, dispute_id)
                reason_entry = reasons_by_decision.get(decision)
                if reason_entry is None:
                    is_moderation = expected_event == "report_moderated"
                    candidates = [
                        key for key in reasons_by_decision
                        if key not in matched_decisions and (key[1] is None) == is_moderation
                    ]
                    if len(candidates) == 1:
                        candidate_report_id, candidate_dispute_id = candidates[0]
                        if candidate_report_id != report_id:
                            raise RecordError(
                                f"{payload_path}.report_id: event report_id {report_id} does not match "
                                f"reason report_id {candidate_report_id}"
                            )
                        if candidate_dispute_id != dispute_id:
                            raise RecordError(
                                f"{payload_path}.dispute_id: event dispute_id {dispute_id} does not match "
                                f"reason dispute_id {candidate_dispute_id}"
                            )
                    raise RecordError(f"{payload_path}: event has no reason for decision {decision!r}")
                if decision in matched_decisions:
                    raise RecordError(f"{payload_path}: decision {decision!r} has more than one event")
                reason_index, reason = reason_entry
                if reason["reason_sha256"] != payload["reason_sha256"]:
                    raise RecordError(f"reasons[{reason_index}].reason_sha256: does not match event {expected_event}")
                matched_decisions.add(decision)
        if not found_event:
            raise RecordError(
                f"timeline[{timeline_index}]: {expected_event} event cannot be found through the timeline transaction"
            )
    for decision, (reason_index, _) in reasons_by_decision.items():
        if decision not in matched_decisions:
            raise RecordError(f"reasons[{reason_index}]: event cannot be found through the timeline")


def _validate_timeline_source(source, event, path):
    if not isinstance(source, dict) or not isinstance(source.get("type"), str):
        raise RecordError(f"{path}: a chain, page or record source is required")
    source_type = source["type"]
    if source_type == "chain":
        _shape(source, ("type", "network", "block", "transaction"), path)
        if source["network"] not in ("testnet", "mainnet"):
            raise RecordError(f"{path}.network: testnet or mainnet is required")
        if type(source["block"]) is not int or source["block"] < 0:
            raise RecordError(f"{path}.block: a non negative block height is required")
        if source["transaction"] is not None:
            _text(source["transaction"], f"{path}.transaction")
        if event not in _CHAIN_EVENTS:
            raise RecordError(f"{path}: a chain source is not allowed for {event}")
    elif source_type == "page":
        _shape(source, ("type", "url", "content_sha256"), path)
        _text(source["url"], f"{path}.url")
        _hash(source["content_sha256"], f"{path}.content_sha256")
        if event != "first_public_report":
            raise RecordError(f"{path}: a page source is not allowed for {event}")
    elif source_type == "record":
        _shape(source, ("type",), path)
        if event != "recorded":
            raise RecordError(f"{path}: a record source is allowed only for recorded")
    else:
        raise RecordError(f"{path}.type: chain, page or record is required")
    if event == "recorded" and source_type != "record":
        raise RecordError(f"{path}: recorded requires a record source")
    if event == "first_public_report" and source_type != "page":
        raise RecordError(f"{path}: first_public_report requires a page source")
    if event in _CHAIN_EVENTS and source_type != "chain":
        raise RecordError(f"{path}: {event} requires a chain source")


def _validate_timeline(timeline):
    if not isinstance(timeline, list):
        raise RecordError("timeline: a list is required")
    previous = None
    recorded = 0
    for index, entry in enumerate(timeline):
        path = f"timeline[{index}]"
        _shape(entry, ("at", "event", "source"), path)
        _utc_time(entry["at"], f"{path}.at")
        if previous is not None and entry["at"] < previous:
            raise RecordError(f"{path}.at: timeline times must be non decreasing")
        previous = entry["at"]
        event = entry["event"]
        if not isinstance(event, str) or event not in _EVENTS:
            raise RecordError(f"{path}.event: {event!r} is not an allowed timeline event")
        if event == "recorded":
            recorded += 1
        _validate_timeline_source(entry["source"], event, f"{path}.source")
    if recorded != 1:
        raise RecordError("timeline: exactly one recorded event is required")
    if not timeline or timeline[-1]["event"] != "recorded":
        raise RecordError("timeline: the recorded event must be last")


def validate(record, report_reader=None, event_reader=None) -> None:
    """Validate a record; readers check cited reports and decision events when provided."""
    if not isinstance(record, dict):
        raise RecordError("record: an object is required")
    if set(record) != set(_FIELDS):
        raise RecordError(f"record: exactly the fields {_FIELDS} are required")
    if type(record["version"]) is not int or record["version"] != 1:
        raise RecordError("version: integer 1 is required")
    if not isinstance(record["kind"], str) or record["kind"] not in _ENUMS["kind"]:
        raise RecordError(f"kind: {record['kind']!r} is not one of {tuple(_ENUMS['kind'])}")
    if not isinstance(record["outcome"], str) or record["outcome"] not in _ENUMS["outcome"]:
        raise RecordError(f"outcome: {record['outcome']!r} is not one of {tuple(_ENUMS['outcome'])}")
    if not isinstance(record["subjects"], list):
        raise RecordError("subjects: a list is required")
    seen_subjects = set()
    previous_subject = None
    for index, subject in enumerate(record["subjects"]):
        path = f"subjects[{index}]"
        _shape(subject, ("chain", "account"), path)
        chain = subject["chain"]
        if not isinstance(chain, str) or not _CHAIN.fullmatch(chain):
            raise RecordError(f"{path}.chain: near or a lowercase alphanumeric chain code is required")
        _text(subject["account"], f"{path}.account")
        if chain == "near":
            _near_account(subject["account"], f"{path}.account")
        pair = (chain, subject["account"])
        if pair in seen_subjects:
            raise RecordError(f"subjects: duplicate subject {pair!r}")
        seen_subjects.add(pair)
        if previous_subject is not None and pair < previous_subject:
            raise RecordError("subjects: entries must be sorted by chain then account")
        previous_subject = pair
    _validate_evidence(record["evidence"])
    if not isinstance(record["reports"], list):
        raise RecordError("reports: a list is required")
    report_views = {} if report_reader is not None else None
    for index, citation in enumerate(record["reports"]):
        report_view = _validate_report(citation, index, report_reader)
        if report_views is not None:
            report_views.setdefault(citation["report_id"], []).append(report_view)
    if not isinstance(record["assertion_ids"], list):
        raise RecordError("assertion_ids: a list is required")
    for index, assertion_id in enumerate(record["assertion_ids"]):
        _hash(assertion_id, f"assertion_ids[{index}]")
    _validate_timeline(record["timeline"])
    reasons_by_decision = _validate_reasons(record["reasons"], record["reports"], report_views)
    if event_reader is not None:
        _validate_event_reasons(record["timeline"], reasons_by_decision, event_reader)
    supersedes = record["supersedes"]
    if supersedes is not None and (not isinstance(supersedes, str) or not _HEX64.fullmatch(supersedes)):
        raise RecordError("supersedes: null or 64 hex characters are required")
    _near_account(record["publisher"], "publisher")
    record_id = record["record_id"]
    if not isinstance(record_id, str) or not _HEX64.fullmatch(record_id):
        raise RecordError("record_id: 64 lowercase hex characters are required")
    expected = canonical_record_id(record)
    if record_id != expected:
        raise RecordError(f"record_id: expected {expected}, got {record_id!r}")


def write(record, directory) -> Path:
    """Fill record_id, validate, and write the canonical object as <record_id>.json."""
    if not isinstance(record, dict):
        raise RecordError("record: an object is required")
    document = {key: value for key, value in record.items() if key != "record_id"}
    document["record_id"] = canonical_record_id(document)
    validate(document)
    data = canonical_bytes(document)
    directory = Path(directory)
    path = directory / f"{document['record_id']}.json"
    directory.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise RecordError(f"{path}: refuses to overwrite a different file with the same record id")
        return path
    path.write_bytes(data)
    return path


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def read(path, report_reader=None) -> dict:
    """Read and validate a record; without report_reader, citations are checked for shape only."""
    path = Path(path)
    try:
        data = path.read_bytes()
        record = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise RecordError(f"{path}: cannot read a UTF-8 JSON record: {error}") from error
    validate(record, report_reader=report_reader)
    if data != canonical_bytes(record):
        raise RecordError(f"{path}: file bytes do not match canonical_bytes of the complete record")
    expected_name = f"{record['record_id']}.json"
    if path.name != expected_name:
        raise RecordError(f"{path}: filename must be {expected_name}")
    return record
