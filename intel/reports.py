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
