"""Map ShieldBot verdict answers to the proposed Intear address safety response."""

import json
from collections.abc import Mapping


def state_for_verdict(answer):
    if isinstance(answer, str):
        return {"ALLOW": "Safe", "DENY": "Unsafe", "ASK": "Unknown"}.get(answer, "Unknown")
    if not isinstance(answer, Mapping):
        return "Unknown"
    if "outcome" in answer:
        outcome = answer["outcome"]
        action = answer.get("action")
        if action == "ask":
            return "Unknown"
        if outcome == "DENY" and action == "refuse":
            return "Unsafe"
        signed = answer.get("verdict")
        if (outcome == "ALLOW" and action == "sign" and isinstance(signed, Mapping)
                and signed.get("literal") == "ALLOW"):
            return "Safe"
        return "Unknown"
    verdict = answer.get("verdict")
    if isinstance(verdict, str):
        return {"ALLOW": "Safe", "DENY": "Unsafe", "ASK": "Unknown"}.get(verdict, "Unknown")
    return "Unknown"


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
