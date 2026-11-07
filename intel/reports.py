import json
import time
from datetime import UTC, datetime
from pathlib import Path


PENDING_COUNTS_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "reports" / "pending-counts.jsonl"
MAX_PENDING_COUNT_REPORT_IDS = 500
SUMMARY_FIELDS = frozenset(("accepted", "pending", "contested", "latest_accepted_id", "latest_deny_id", "deny"))
U64_MAX = (1 << 64) - 1


def _readable(summary):
    if not isinstance(summary, dict):
        return False
    if summary.keys() != SUMMARY_FIELDS:
        return False
    counts = (summary.get("accepted"), summary.get("pending"), summary.get("contested"))
    if any(type(count) is not int or not 0 <= count <= U64_MAX for count in counts):
        return False
    accepted, _, contested = counts
    if contested > accepted:
        return False
    latest_id = summary.get("latest_accepted_id")
    latest_deny_id = summary.get("latest_deny_id")
    deny = summary.get("deny")
    if type(deny) is not bool:
        return False
    if accepted == 0:
        if latest_id is not None or deny:
            return False
    elif type(latest_id) is not int or not 0 <= latest_id <= U64_MAX:
        return False
    if deny:
        if accepted == 0 or type(latest_deny_id) is not int or not 0 <= latest_deny_id <= U64_MAX:
            return False
    elif latest_deny_id is not None:
        return False
    return True


def effect(summary, payee_on_allowlist):
    if not _readable(summary):
        return {"effect": "unreadable", "report_id": None,
                "detail": "the summary is not in the fixed shape; pending unknown"}
    accepted, pending, contested = summary["accepted"], summary["pending"], summary["contested"]
    if summary["deny"]:
        report_id = summary["latest_deny_id"]
        detail = f"deny report {report_id} is accepted; {contested} under dispute; pending {pending}"
        return {"effect": "deny", "report_id": report_id, "detail": detail}
    if accepted > 0:
        report_id = summary["latest_accepted_id"]
        if payee_on_allowlist:
            detail = (f"{accepted} accepted social report(s), latest id {report_id}; the payee is on the allowlist, "
                      f"so no ask; pending {pending}")
            return {"effect": "none", "report_id": report_id, "detail": detail}
        detail = f"{accepted} accepted social report(s), latest id {report_id}; a human decides; pending {pending}"
        return {"effect": "ask", "report_id": report_id, "detail": detail}
    if pending > 0:
        return {"effect": "none", "report_id": None,
                "detail": f"{pending} pending report(s) await moderation; no effect"}
    return {"effect": "none", "report_id": None, "detail": "no community report; pending 0"}


def record_pending_counts(client, reports_account, path=PENDING_COUNTS_PATH, clock=time.time):
    """Append pending report counts and subject summaries at one pinned testnet block."""
    totals = client.call_function(reports_account, "totals", {})
    if (not isinstance(totals, dict) or totals.keys() != {"reports", "disputes"}
            or any(type(totals.get(field)) is not int or totals[field] < 0 for field in ("reports", "disputes"))):
        raise ValueError("reports totals view is unreadable")
    block_height = client.last_block_height
    if type(block_height) is not int or block_height < 0:
        raise ValueError("reports totals read has no block height")
    total_reports = totals["reports"]
    reports_scanned = min(total_reports, MAX_PENDING_COUNT_REPORT_IDS)
    pending_by_subject = {}
    missing_ids = []
    for report_id in range(reports_scanned):
        report = client.call_function(reports_account, "get_report", {"id": report_id}, block_id=block_height)
        if report is None:
            missing_ids.append(report_id)
            continue
        if (not isinstance(report, dict) or not isinstance(report.get("subject"), str)
                or not isinstance(report.get("state"), str)):
            raise ValueError(f"get_report({report_id}) view is unreadable")
        subject = report["subject"]
        pending_by_subject.setdefault(subject, 0)
        if report["state"] == "pending":
            pending_by_subject[subject] += 1
    subjects, mismatch = [], bool(missing_ids)
    for subject, pending in sorted(pending_by_subject.items()):
        summary = client.call_function(reports_account, "subject_summary", {"subject": subject},
                                       block_id=block_height)
        if (not isinstance(summary, dict) or type(summary.get("pending")) is not int
                or summary["pending"] < 0):
            raise ValueError(f"subject_summary for {subject} is unreadable")
        differs = pending != summary["pending"]
        mismatch = mismatch or differs
        subjects.append({"subject": subject, "pending": pending, "summary_pending": summary["pending"],
                         "mismatch": differs})
    row = {"recorded_at_utc": datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "block_height": block_height, "totals": totals,
           "report_scan": {"ids_scanned": reports_scanned, "id_limit": MAX_PENDING_COUNT_REPORT_IDS,
                           "truncated": total_reports > MAX_PENDING_COUNT_REPORT_IDS,
                           "missing_ids": missing_ids},
           "subjects": subjects, "mismatch": mismatch}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return row
