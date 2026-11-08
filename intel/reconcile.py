"""Reconcile accepted reports contract events into append-only rows."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping


EVENT_PREFIX = "EVENT_JSON:"
STANDARD = "shieldbot-near"
PAGE_SIZE = 50
REVALIDATE_AFTER = timedelta(days=7)
HEX64 = re.compile(r"[0-9a-f]{64}")


class ReconcileError(ValueError):
    """An event, page, or stored row does not match the reports contract shape."""


PageReader = Callable[[str, int, int], Mapping[str, object]]
ReportReader = Callable[[int], Mapping[str, object]]


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ReconcileError(f"{field}: a UTC timestamp is required")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise ReconcileError(f"{field}: expected YYYY-MM-DDTHH:MM:SSZ") from error


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ReconcileError(f"{field}: a positive integer is required")
    return value


def _non_negative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReconcileError(f"{field}: a non-negative integer is required")
    return value


def _required_text(data: Mapping[str, object], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ReconcileError(f"{field}: a non-empty string is required")
    return value


def _event_record(item: object) -> dict | None:
    if not isinstance(item, Mapping):
        raise ReconcileError("event line: a mapping with line, block_height, and fetched_at is required")
    line = item.get("line")
    if not isinstance(line, str) or not line.startswith(EVENT_PREFIX):
        raise ReconcileError("event line: EVENT_JSON: prefix is required")
    block_height = _positive_int(item.get("block_height"), "block_height")
    fetched_at = _required_text(item, "fetched_at")
    _time(fetched_at, "fetched_at")
    try:
        envelope = json.loads(line[len(EVENT_PREFIX):])
    except json.JSONDecodeError as error:
        raise ReconcileError("event line: invalid JSON") from error
    if not isinstance(envelope, dict) or envelope.get("standard") != STANDARD:
        raise ReconcileError(f"event line: standard must be {STANDARD}")
    if envelope.get("version") not in ("1.0.0", "1.1.0") or not isinstance(envelope.get("event"), str):
        raise ReconcileError("event line: unsupported version or missing event name")
    data = envelope.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ReconcileError("event line: data must contain one object")
    name = envelope["event"]
    if name not in ("report_moderated", "dispute_resolved"):
        return None

    payload = data[0]
    report_id = _non_negative_int(payload.get("report_id"), "report_id")
    subject = _required_text(payload, "subject")
    state = _required_text(payload, "state")
    body_sha256 = _required_text(payload, "body_sha256")
    moderator = _required_text(payload, "moderator")
    reason_sha256 = _required_text(payload, "reason_sha256")
    if not HEX64.fullmatch(body_sha256) or not HEX64.fullmatch(reason_sha256):
        raise ReconcileError("event line: body_sha256 and reason_sha256 must be lowercase sha256 values")
    if name == "report_moderated" and state not in ("accepted", "rejected"):
        raise ReconcileError("report_moderated: state must be accepted or rejected")
    if name == "dispute_resolved":
        if state not in ("accepted", "overturned"):
            raise ReconcileError("dispute_resolved: state must be accepted or overturned")
        _non_negative_int(payload.get("dispute_id"), "dispute_id")
    receipt = item.get("receipt_id") or item.get("transaction_id") or item.get("tx_hash") or None
    if receipt is not None and (not isinstance(receipt, str) or not receipt):
        raise ReconcileError("event line: receipt or transaction id must be non-empty text")

    return {
        "contract": None,
        "report_id": report_id,
        "event": name,
        "source_block": block_height,
        "fetched_at": fetched_at,
        "subject": subject,
        "state": state,
        "body_sha256": body_sha256,
        "source_method": "event",
        "source_args": {"receipt": receipt, "report_id": report_id},
        "event_data": payload,
        "supersedes": None,
    }


def _key(row: Mapping[str, object]) -> tuple[str, int, str, int]:
    return (
        str(row["contract"]),
        int(row["report_id"]),
        str(row["event"]),
        int(row["source_block"]),
    )


def _sort_key(row: Mapping[str, object]) -> tuple[int, int, int, str]:
    priority = 0 if row["event"] in ("report_moderated", "reports_by_subject") else 1
    return int(row["source_block"]), int(row["report_id"]), priority, str(row["event"])


def _load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ReconcileError(f"{path}:{number}: invalid JSON row") from error
        if not isinstance(row, dict):
            raise ReconcileError(f"{path}:{number}: a row must be an object")
        required = (
            "contract", "report_id", "event", "source_block", "fetched_at", "revalidate_after",
            "subject", "state", "body_sha256", "source_method", "source_args", "event_data", "supersedes",
        )
        if any(field not in row for field in required):
            raise ReconcileError(f"{path}:{number}: row is missing a required field")
        _time(row["fetched_at"], "fetched_at")
        _time(row["revalidate_after"], "revalidate_after")
        rows.append(row)
    return rows


def _row(
    event: Mapping[str, object],
    contract: str,
    *,
    event_name: str | None = None,
    source_method: str | None = None,
    supersedes: dict | None = None,
) -> dict:
    fetched_at = str(event["fetched_at"])
    return {
        "contract": contract,
        "report_id": int(event["report_id"]),
        "event": event_name or str(event["event"]),
        "source_block": int(event["source_block"]),
        "fetched_at": fetched_at,
        "revalidate_after": _time_text(_time(fetched_at, "fetched_at") + REVALIDATE_AFTER),
        "subject": str(event["subject"]),
        "state": str(event["state"]),
        "body_sha256": str(event["body_sha256"]),
        "source_method": source_method or str(event["source_method"]),
        "source_args": dict(event["source_args"]),
        "event_data": event["event_data"],
        "supersedes": supersedes,
    }


def _recovery_row(
    report: Mapping[str, object],
    contract: str,
    *,
    source_block: int,
    fetched_at: str,
    source_method: str,
    source_args: Mapping[str, object],
    known_state: str | None,
) -> dict | None:
    report_id = _non_negative_int(report.get("id"), "recovered report id")
    subject = _required_text(report, "subject")
    state = _required_text(report, "state")
    if state == "accepted" and known_state != "accepted":
        event_name = "reports_by_subject"
    elif state == "overturned" and known_state != "overturned":
        event_name = "dispute_resolved"
    else:
        return None
    body_sha256 = _required_text(report, "body_sha256")
    if not HEX64.fullmatch(body_sha256):
        raise ReconcileError("report reader: body_sha256 must be a lowercase sha256 value")
    return _row(
        {
            "report_id": report_id,
            "event": event_name,
            "source_block": source_block,
            "fetched_at": fetched_at,
            "subject": subject,
            "state": state,
            "body_sha256": body_sha256,
            "source_method": source_method,
            "source_args": dict(source_args),
            "event_data": dict(report),
        },
        contract,
        event_name=event_name,
        source_method=source_method,
    )


def _page_rows(
    subject: str,
    page_reader: PageReader,
    known_states: dict[tuple[str, int], str],
    contract: str,
) -> list[dict]:
    recovered = []
    start = 0
    scan_block = None
    while True:
        page = page_reader(subject, start, PAGE_SIZE)
        if not isinstance(page, Mapping):
            raise ReconcileError("page reader: expected source_block, fetched_at, and reports")
        source_block = _positive_int(page.get("source_block"), "page source_block")
        fetched_at = _required_text(page, "fetched_at")
        _time(fetched_at, "page fetched_at")
        if scan_block is not None and scan_block != source_block:
            raise ReconcileError("page reader: source_block changed during a subject scan")
        scan_block = source_block
        reports = page.get("reports")
        if not isinstance(reports, list) or len(reports) > PAGE_SIZE:
            raise ReconcileError("page reader: reports must be a list no larger than the page limit")
        for report in reports:
            if not isinstance(report, dict):
                raise ReconcileError("page reader: each report must be an object")
            report_id = _non_negative_int(report.get("id"), "page report id")
            report_subject = _required_text(report, "subject")
            state = _required_text(report, "state")
            if report_subject != subject:
                raise ReconcileError("page reader: report subject does not match the requested subject")
            row = _recovery_row(
                report,
                contract,
                source_block=source_block,
                fetched_at=fetched_at,
                source_method="reports_by_subject",
                source_args={"subject": subject, "from": start, "limit": PAGE_SIZE},
                known_state=known_states.get((subject, report_id)),
            )
            if row is not None:
                recovered.append(row)
                known_states[(subject, report_id)] = state
        if len(reports) < PAGE_SIZE:
            return recovered
        start += PAGE_SIZE


def _apply_ordered(existing: list[dict], candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    known = {_key(row): row for row in existing}
    pending = {}
    for row in candidates:
        key = _key(row)
        previous = known.get(key) or pending.get(key)
        if previous is not None:
            if previous.get("event_data") != row.get("event_data"):
                raise ReconcileError(f"conflicting event for row key {key!r}")
            continue
        pending[key] = row

    ordered = sorted([*existing, *pending.values()], key=_sort_key)
    active: dict[tuple[str, int], dict] = {}
    for row in ordered:
        identity = (str(row["contract"]), int(row["report_id"]))
        if row["event"] in ("report_moderated", "reports_by_subject") and row["state"] == "accepted":
            active.setdefault(identity, row)
        elif row["event"] == "dispute_resolved" and row["state"] == "overturned":
            if _key(row) in known:
                supersedes = row.get("supersedes")
                if supersedes is not None and active.get(identity) is not None:
                    if _key(active[identity]) == (
                        str(supersedes["contract"]),
                        int(supersedes["report_id"]),
                        str(supersedes["event"]),
                        int(supersedes["source_block"]),
                    ):
                        active.pop(identity)
                continue
            previous = active.get(identity)
            if previous is None:
                if row["source_method"] in ("reports_by_subject", "get_report"):
                    continue
                raise ReconcileError(f"overturned report {row['report_id']} has no accepted row to supersede")
            row["supersedes"] = {
                "contract": previous["contract"],
                "report_id": previous["report_id"],
                "event": previous["event"],
                "source_block": previous["source_block"],
            }
            active.pop(identity)

    additions = sorted(pending.values(), key=_sort_key)
    return ordered, additions


def _with_freshness(rows: Iterable[dict], now: str | datetime | None) -> list[dict]:
    current = now or datetime.now(timezone.utc).replace(microsecond=0)
    current_time = _time(current, "now") if isinstance(current, str) else current.astimezone(timezone.utc)
    enriched = []
    for row in sorted(rows, key=_sort_key):
        fetched = _time(row["fetched_at"], "fetched_at")
        due = current_time >= _time(row["revalidate_after"], "revalidate_after")
        enriched.append({**row, "age_seconds": max(0, int((current_time - fetched).total_seconds())),
                         "revalidation_due": due})
    return enriched


def read_rows(path: str | Path, now: str | datetime | None = None) -> list[dict]:
    """Return every stored row, including expired rows with age and revalidation status."""
    return _with_freshness(_load_rows(Path(path)), now)


def reconcile(
    event_lines: Iterable[Mapping[str, object]],
    rows_path: str | Path,
    contract: str,
    *,
    page_reader: PageReader | None = None,
    since_block: int | None = None,
    subjects: Iterable[str] | None = None,
    report_total: int | None = None,
    get_report: ReportReader | None = None,
    now: str | datetime | None = None,
) -> list[dict]:
    """Append accepted and overturned report rows, recovering known subjects from pages.

    Each input item has ``line``, ``block_height``, and ``fetched_at``. A page reader accepts
    ``(subject, from, limit)`` and returns ``source_block``, ``fetched_at``, and ``reports``.
    Supplying ``since_block`` requests page recovery for subjects in the stored and incoming rows;
    ``subjects`` explicitly adds subjects to recover and can start a page scan with an empty store.
    To recover without known subjects, supply ``report_total`` and ``get_report`` together. The
    total is the number of report ids; the reader is called for ids from 0 through total - 1 and
    returns ``source_block``, ``fetched_at``, and the report mapping under ``report``. All reads
    in that scan must use the same source block.
    """
    if not isinstance(contract, str) or not contract:
        raise ReconcileError("contract: a non-empty account id is required")
    if since_block is not None:
        _non_negative_int(since_block, "since_block")
    if subjects is not None and (isinstance(subjects, (str, bytes)) or not hasattr(subjects, "__iter__")):
        raise ReconcileError("subjects: an iterable of non-empty account ids is required")
    requested_subjects = tuple(subjects) if subjects is not None else ()
    if any(not isinstance(subject, str) or not subject for subject in requested_subjects):
        raise ReconcileError("subjects: an iterable of non-empty account ids is required")
    if (report_total is None) != (get_report is None):
        raise ReconcileError("report_total and get_report must be supplied together")
    if report_total is not None:
        _non_negative_int(report_total, "report_total")
    path = Path(rows_path)
    existing = _load_rows(path)
    parsed = [record for item in event_lines if (record := _event_record(item)) is not None]
    for record in parsed:
        record["contract"] = contract

    historical = [row for row in existing if row["contract"] == contract]
    candidates = []
    for record in parsed:
        if record["event"] == "report_moderated" and record["state"] == "accepted":
            candidates.append(_row(record, contract))
        elif record["event"] == "dispute_resolved" and record["state"] == "overturned":
            candidates.append(_row(record, contract))

    recovery_subjects = set(requested_subjects)
    if since_block is not None:
        recovery_subjects.update(str(row["subject"]) for row in (*historical, *parsed))
    known_states = {}
    for row in sorted([*historical, *candidates], key=_sort_key):
        if row["contract"] == contract and row["state"] in ("accepted", "overturned"):
            known_states[(str(row["subject"]), int(row["report_id"]))] = str(row["state"])
    if recovery_subjects:
        if page_reader is None:
            raise ReconcileError("subject recovery requires a reports_by_subject page reader")
        for subject in sorted(recovery_subjects):
            candidates.extend(_page_rows(subject, page_reader, known_states, contract))

    if report_total is not None and get_report is not None:
        scan_block = None
        for report_id in range(report_total):
            result = get_report(report_id)
            if not isinstance(result, Mapping):
                raise ReconcileError("get_report reader: expected source_block, fetched_at, and report")
            source_block = _positive_int(result.get("source_block"), "get_report source_block")
            fetched_at = _required_text(result, "fetched_at")
            _time(fetched_at, "get_report fetched_at")
            if scan_block is not None and scan_block != source_block:
                raise ReconcileError("get_report reader: source_block changed during a report scan")
            scan_block = source_block
            report = result.get("report")
            if not isinstance(report, Mapping):
                raise ReconcileError(f"get_report reader: report {report_id} is missing or invalid")
            returned_id = _non_negative_int(report.get("id"), "get_report report id")
            if returned_id != report_id:
                raise ReconcileError(f"get_report reader: requested report {report_id}, received {returned_id}")
            subject = _required_text(report, "subject")
            state = _required_text(report, "state")
            row = _recovery_row(
                report,
                contract,
                source_block=source_block,
                fetched_at=fetched_at,
                source_method="get_report",
                source_args={"id": report_id},
                known_state=known_states.get((subject, report_id)),
            )
            if row is not None:
                candidates.append(row)
                known_states[(subject, report_id)] = state

    ordered, additions = _apply_ordered(existing, candidates)
    if additions:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for row in additions:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    return _with_freshness(ordered, now)


def to_label_row(
    row: Mapping[str, object],
    *,
    rpc: str,
    superseded_row: dict | None = None,
) -> dict:
    """Convert a reconciler row into a label row and link any superseded accepted row."""
    required = (
        "contract", "report_id", "event", "source_block", "fetched_at", "revalidate_after",
        "subject", "state", "body_sha256", "source_method", "source_args", "event_data", "supersedes",
    )
    if any(field not in row for field in required):
        raise ReconcileError("row: missing fields required for label conversion")
    state = str(row["state"])
    source_method = str(row["source_method"])
    source_args = row["source_args"]
    if source_method == "event":
        method = f"EVENT_JSON:{row['event']}"
    elif source_method in ("reports_by_subject", "get_report"):
        method = source_method
    else:
        raise ReconcileError("row: unsupported source method for label conversion")
    if not isinstance(source_args, Mapping):
        raise ReconcileError("row: source arguments are required for label conversion")
    if source_method == "event" and (
        set(source_args) != {"receipt", "report_id"}
        or source_args.get("report_id") != row["report_id"]
        or source_args.get("receipt") is not None
        and (not isinstance(source_args.get("receipt"), str) or not source_args.get("receipt"))
    ):
        raise ReconcileError("row: event source arguments do not match the report id")
    if method == "get_report" and dict(source_args) != {"id": row["report_id"]}:
        raise ReconcileError("row: get_report source arguments do not match the report id")
    if method == "reports_by_subject" and (
        set(source_args) != {"subject", "from", "limit"}
        or source_args.get("subject") != row["subject"]
        or not isinstance(source_args.get("from"), int)
        or isinstance(source_args.get("from"), bool)
        or source_args["from"] < 0
        or not isinstance(source_args.get("limit"), int)
        or isinstance(source_args.get("limit"), bool)
        or source_args["limit"] < 1
    ):
        raise ReconcileError("row: reports_by_subject source arguments do not match the page")
    if state == "overturned":
        note = (f'Report {row["report_id"]} on "{row["contract"]}" was overturned at block '
                f'{row["source_block"]}; ')
        if row["supersedes"] is None:
            note += "no accepted row was available to supersede."
        else:
            note += "this row supersedes the accepted row."
    else:
        note = (f'"{row["contract"]}" recorded report {row["report_id"]} as "{state}" at block '
                f'{row["source_block"]}.')
    label_row = {
        "entity": str(row["contract"]),
        "subject": str(row["subject"]),
        "chain": "near",
        "label": "accepted report" if state == "accepted" else "overturned report",
        "category": "watch" if state == "overturned" else "incident",
        "provenance": "onchain",
        "source": {
            "rpc": rpc,
            "method": method,
            "args": dict(source_args),
            "block": row["source_block"],
        },
        "observed_at": row["fetched_at"],
        "confidence": 1.0,
        "rule_id": None,
        "superseded_by": None,
        "note": note,
    }
    canonical_row = {key: value for key, value in label_row.items() if key != "superseded_by"}
    canonical = json.dumps(canonical_row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    label_row["id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    supersedes = row["supersedes"]
    if supersedes is not None:
        if not isinstance(supersedes, Mapping) or superseded_row is None:
            raise ReconcileError("row: a correction needs the accepted label row it supersedes")
        previous_source = superseded_row.get("source")
        previous_args = previous_source.get("args") if isinstance(previous_source, Mapping) else None
        previous_method = previous_source.get("method") if isinstance(previous_source, Mapping) else None
        previous_source_matches = (
            previous_method == "EVENT_JSON:report_moderated"
            and isinstance(previous_args, Mapping)
            and set(previous_args) == {"receipt", "report_id"}
            and previous_args.get("report_id") == supersedes.get("report_id")
            and (previous_args.get("receipt") is None or isinstance(previous_args.get("receipt"), str))
        ) or (
            previous_method == "get_report" and previous_args == {"id": supersedes.get("report_id")}
        ) or (
            previous_method == "reports_by_subject"
            and isinstance(previous_args, Mapping)
            and set(previous_args) == {"subject", "from", "limit"}
            and previous_args.get("subject") == row["subject"]
            and isinstance(previous_args.get("from"), int)
            and not isinstance(previous_args.get("from"), bool)
            and previous_args["from"] >= 0
            and isinstance(previous_args.get("limit"), int)
            and not isinstance(previous_args.get("limit"), bool)
            and previous_args["limit"] >= 1
        )
        if (
            superseded_row.get("entity") != supersedes.get("contract")
            or superseded_row.get("subject") != row["subject"]
            or superseded_row.get("label") != "accepted report"
            or not isinstance(previous_source, Mapping)
            or not previous_source_matches
            or previous_source.get("block") != supersedes.get("source_block")
            or superseded_row.get("superseded_by") is not None
        ):
            raise ReconcileError("row: the supplied label row does not match the accepted row being superseded")
        superseded_row["superseded_by"] = label_row["id"]
    return label_row
