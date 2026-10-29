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


def request(kind, token, receiver, amount, memo, *, gate, agent_pk_hex, policy, store, rpc, nearblocks,
            signer, ttl_ms=DEFAULT_TTL_MS, injection_scanner=None, archival=None, evidence_dir=EVIDENCE_DIR,
            clock=time.time):
    """Judge one transfer for the agent agent_pk_hex at the Gate gate.

    Returns {"outcome", "verdict", "request", "checks", "reasons", "evidence_hash", "evidence_path",
    "latency_ms", "message"}: "request" is the TransferRequest and "verdict" the Verdict object the
    Gate's request_transfer takes, or None unless the outcome is ALLOW. clock gives the wall time in
    seconds for the rules and the issue time; the latency always uses time.perf_counter.
    """
    amount = canon.validate_transfer(gate=gate, agent_pk_hex=agent_pk_hex, kind=kind, token_id=token,
                                     receiver_id=receiver, amount=amount, memo=memo)
    transfer = {"kind": kind, "token_id": token, "receiver_id": receiver, "amount": amount, "memo": memo}
    started = time.perf_counter()
    judgement = rules.judge(transfer, policy, store, rpc, nearblocks, now_ms=int(clock() * 1000),
                            injection_scanner=injection_scanner, archival=archival)
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
        "verdict": verdict,
        "request": transfer,
        "checks": judgement.checks,
        "reasons": judgement.reasons,
        "evidence_hash": evidence_hash,
        "evidence_path": str(evidence_path),
        "latency_ms": latency_ms,
        "message": message.decode("utf-8") if message else None,
    }
