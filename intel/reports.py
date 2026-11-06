def _readable(summary):
    if not isinstance(summary, dict):
        return False
    fields = {"accepted", "pending", "contested", "latest_accepted_id", "deny"}
    if summary.keys() != fields:
        return False
    counts = (summary.get("accepted"), summary.get("pending"), summary.get("contested"))
    if any(type(count) is not int or count < 0 for count in counts):
        return False
    accepted, _, contested = counts
    if contested > accepted:
        return False
    latest_id, deny = summary.get("latest_accepted_id"), summary.get("deny")
    if type(deny) is not bool:
        return False
    if accepted == 0:
        return latest_id is None and not deny
    return type(latest_id) is int and latest_id >= 0


def effect(summary, payee_on_allowlist) -> str:
    if not _readable(summary):
        return "unknown"
    if summary["deny"]:
        return "deny"
    if summary["accepted"] > 0:
        return "unknown"
    if summary["pending"] > 0 and not payee_on_allowlist:
        return "unknown"
    return "none"
