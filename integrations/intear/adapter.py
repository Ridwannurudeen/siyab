"""Map ShieldBot verdict answers to the proposed Intear address safety response."""

import json
import time
from collections.abc import Mapping

MAX_VERDICT_AGE_MS = 300_000
# Five minutes matches the Gate's default ttl and limits stale wallet decisions.


def state_for_verdict(answer):
    if not isinstance(answer, dict):
        return "Unknown"
    outcome = answer.get("outcome")
    if outcome == "DENY":
        return "Unsafe"
    if outcome != "ALLOW" or answer.get("action") != "sign":
        return "Unknown"

    evidence_hash = answer.get("evidence_hash")
    signed = answer.get("verdict")
    checks = answer.get("checks")
    if (not isinstance(evidence_hash, str) or not evidence_hash.strip()
            or not isinstance(signed, Mapping) or signed.get("literal") != "ALLOW"
            or signed.get("evidence_hash") != evidence_hash
            or not isinstance(checks, list) or not checks
            or any(not isinstance(check, Mapping) or check.get("status") != "pass" for check in checks)):
        return "Unknown"

    issued_ms = signed.get("issued_ms")
    expires_ms = signed.get("expires_ms")
    if type(issued_ms) is not int or type(expires_ms) is not int:
        return "Unknown"
    now_ms = time.time_ns() // 1_000_000
    if (issued_ms > now_ms or now_ms - issued_ms > MAX_VERDICT_AGE_MS
            or expires_ms <= now_ms or expires_ms < issued_ms
            or expires_ms - issued_ms > MAX_VERDICT_AGE_MS):
        return "Unknown"
    return "Safe"


def address_safety_payload(answer):
    if isinstance(answer, Mapping):
        reasons = answer.get("reasons")
        if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
            reasons = []
        evidence_hash = answer.get("evidence_hash")
        if not isinstance(evidence_hash, str):
            evidence_hash = None
    else:
        reasons, evidence_hash = [], None
    return {"status": state_for_verdict(answer), "reasons": reasons, "evidence_hash": evidence_hash}


def address_safety_json(answer):
    """The JSON body an operator can return from GET /addresssafety?address=<account>."""
    return json.dumps(address_safety_payload(answer), ensure_ascii=False)
