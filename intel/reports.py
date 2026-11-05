REPORT_KINDS = ("scam", "theft", "phishing", "exploit", "key_compromise", "social")
DENY_KINDS = ("scam", "theft", "phishing", "exploit", "key_compromise")


def _readable(summary):
    if not isinstance(summary, dict):
        return False
    fields = {"accepted", "pending", "contested", "latest_accepted_id", "latest_accepted_kind"}
    if not fields.issubset(summary):
        return False
    counts = (summary.get("accepted"), summary.get("pending"), summary.get("contested"))
    if any(type(count) is not int or count < 0 for count in counts):
        return False
    accepted, _, contested = counts
    if contested > accepted:
        return False
    latest_id, latest_kind = summary.get("latest_accepted_id"), summary.get("latest_accepted_kind")
    if accepted == 0:
        return latest_id is None and latest_kind is None
    return (type(latest_id) is int and latest_id >= 0 and isinstance(latest_kind, str)
            and latest_kind in REPORT_KINDS)


def effect(summary, payee_on_allowlist) -> str:
    if not _readable(summary):
        return "unknown"
    if summary["accepted"] > 0 and summary["latest_accepted_kind"] in DENY_KINDS:
        return "deny"
    if summary["accepted"] > 0 and summary["latest_accepted_kind"] == "social":
        return "unknown"
    if summary["pending"] > 0 and not payee_on_allowlist:
        return "unknown"
    return "none"
