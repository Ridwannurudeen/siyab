"""The verdict service: judge one transfer, write its evidence, and sign only an ALLOW.

request() validates the transfer fields first, runs gate.rules.judge, measures the latency with
time.perf_counter, writes the evidence document under its sha256, and on ALLOW alone sets
issued_ms, expires_ms = issued_ms + ttl_ms and a random verdict_id and signs the digest of the
canonical message. DENY and UNKNOWN carry no signature and no verdict id.
"""

import secrets
import time
from pathlib import Path

from gate import canon, evidence, rules

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "evidence"
DEFAULT_TTL_MS = 300000


def ask_reasons(outcome, checks, transfer, policy):
    """Reasons a human must decide before pay signs or sends.

    The Gate still checks the window part of every cap and every cap of an allowlisted payee; only the Gate sees
    the block time when a cap window rolls.
    """
    if outcome == "DENY":
        return []
    if outcome == "UNKNOWN":
        return [f"{check['id']} could not run: {check['detail']}" for check in checks
                if check["status"] == "unknown"]
    receiver = transfer["receiver_id"]
    if receiver in policy.get("allowlist", []):
        return []
    unknown_caps = policy.get("unknown_caps")
    if unknown_caps is None:
        return [f"{receiver} is not on the agent's allowlist and the agent has no unknown payee caps, "
                "so the Gate refuses the transfer (REFUSED:not_allowlisted)"]
    asset = {"Ft": transfer["token_id"]} if transfer["kind"] == "ft" else "Near"
    asset_text = transfer["token_id"] if transfer["kind"] == "ft" else "NEAR"
    cap = next((cap for cap in unknown_caps if cap["asset"] == asset), None)
    if cap is None:
        return [f"{receiver} is not on the agent's allowlist and no unknown payee cap covers {asset_text}, "
                "so the Gate refuses the transfer (REFUSED:unknown_payee_cap)"]
    if int(transfer["amount"]) > int(cap["per_call"]):
        return [f"{receiver} is not on the agent's allowlist and {transfer['amount']} is over its unknown payee cap "
                f"of {cap['per_call']} per call, so the Gate refuses the transfer (REFUSED:unknown_payee_cap)"]
    return []


def request(kind, token, receiver, amount, memo, *, gate, agent_pk_hex, policy, store, rpc, nearblocks,
            signer, ttl_ms=DEFAULT_TTL_MS, injection_scanner=None, archival=None, evidence_dir=EVIDENCE_DIR,
            clock=time.time, issuers=None):
    """Judge one transfer for the agent agent_pk_hex at the Gate gate.

    Returns {"outcome", "action", "ask", "verdict", "request", "checks", "reasons", "evidence_hash",
    "evidence_path", "latency_ms", "message"}: "request" is the TransferRequest and "verdict" the
    Verdict object the Gate's request_transfer takes, or None unless the outcome is ALLOW. "action"
    is sign for an ALLOW the Gate would not refuse on the visible policy checks, refuse for DENY, or ask
    for UNKNOWN or an ALLOW the Gate would refuse on those checks. "ask" lists the reasons for a human
    decision. clock gives the wall time in seconds for the rules and the issue time; the latency always
    uses time.perf_counter. issuers is the issuer table the rules read (gate.rules.issuers_for), or None
    for no issuer_blacklist check.
    """
    amount = canon.validate_transfer(gate=gate, agent_pk_hex=agent_pk_hex, kind=kind, token_id=token,
                                     receiver_id=receiver, amount=amount, memo=memo)
    transfer = {"kind": kind, "token_id": token, "receiver_id": receiver, "amount": amount, "memo": memo}
    started = time.perf_counter()
    judgement = rules.judge(transfer, policy, store, rpc, nearblocks, now_ms=int(clock() * 1000),
                            injection_scanner=injection_scanner, archival=archival, issuers=issuers)
    allow = judgement.verdict == "ALLOW"
    issued_ms = int(clock() * 1000) if allow else None
    expires_ms = issued_ms + ttl_ms if allow else None
    verdict_id = secrets.token_hex(16) if allow else None
    latency_ms = int((time.perf_counter() - started) * 1000)
    document = evidence.document(
        subject=evidence.subject(gate=gate, agent_pk=agent_pk_hex, kind=kind, token=token, receiver=receiver,
                                 amount=amount, memo=memo),
        checks=judgement.checks, labels=judgement.labels, verdict=judgement.verdict,
        reasons=judgement.reasons, observed=judgement.observed, issued_ms=issued_ms,
        expires_ms=expires_ms, verdict_id=verdict_id, latency_ms=latency_ms)
    evidence_path = evidence.write(document, evidence_dir)
    evidence_hash = evidence_path.stem
    ask = ask_reasons(judgement.verdict, judgement.checks, transfer, policy)
    action = ("refuse" if judgement.verdict == "DENY" else
              "ask" if judgement.verdict == "UNKNOWN" or ask else "sign")
    verdict, message = None, None
    if allow:
        message = canon.canonical_message(
            gate=gate, agent_pk_hex=agent_pk_hex, kind=kind, token_id=token, receiver_id=receiver,
            amount=amount, memo=memo, issued_ms=issued_ms, expires_ms=expires_ms, verdict_id=verdict_id,
            evidence_sha256=evidence_hash, key_id=signer.key_id)
        verdict = {
            "literal": canon.LITERAL,
            "issued_ms": issued_ms,
            "expires_ms": expires_ms,
            "verdict_id": verdict_id,
            "evidence_hash": evidence_hash,
            "key_id": signer.key_id,
            "signature": signer.sign(canon.digest(message)).hex(),
        }
    return {
        "outcome": judgement.verdict,
        "action": action,
        "ask": ask,
        "verdict": verdict,
        "request": transfer,
        "checks": judgement.checks,
        "reasons": judgement.reasons,
        "evidence_hash": evidence_hash,
        "evidence_path": str(evidence_path),
        "latency_ms": latency_ms,
        "message": message.decode("utf-8") if message else None,
    }
