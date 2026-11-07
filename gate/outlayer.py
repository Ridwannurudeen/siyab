"""An OutLayer approver adapter: one pending OutLayer approval in, the vote a policy approver would cast out.

OutLayer Agent Custody asks no outside service for a decision. A wallet whose policy has an approval
threshold holds each fund moving operation as a pending approval; an account named in the policy's
approval.approvers reads it (GET /wallet/v1/approval/{id} answers an ApprovalDetail) and votes by posting a
NEP-413 signature over approve:{id}:{wallet_pubkey}:{request_hash}, or the reject: form, to
/wallet/v1/approve/{id} or /wallet/v1/reject/{id}. docs/OUTLAYER.md records the sources.

vote takes that ApprovalDetail, judges the transfer it describes with gate.verdict.request as gate/http.py
does (no signer, sign_actions empty, so nothing is signed), and translates only the verdict's action:

    sign      approve, with the approve: message for the approver's key to sign
    refuse    reject, with the reject: message
    ask       abstain: no vote, so the approval stays on OutLayer's approvals page for the other approvers
              until its expires_at. A reject vote from a listed approver cancels the request, so ask never
              becomes reject.

Only two operations are judged: request_type transfer (to, or its alias receiver_id, and amount, on chain
near) as a NEAR transfer, and request_type call with method_name ft_transfer (receiver_id the token; args
receiver_id, amount and memo; deposit absent or exactly 1 yoctoNEAR) as a token transfer. Every other
operation, any field this module does not read, an approval that is not pending, and a transfer whose fields
have no canonical form all abstain without a verdict. This module signs nothing, sends nothing, opens no
connection and reads no key: the vote message is returned for the approver's own key to sign elsewhere.
"""

from gate import canon, ed25519, rules, verdict
from gate.__main__ import _clients

VOTES = {"sign": "approve", "refuse": "reject", "ask": "abstain"}
DETAIL_FIELDS = ("id", "request_type", "request_data", "status", "request_hash", "wallet_pubkey")
TRANSFER_FIELDS = {"to", "receiver_id", "amount", "chain"}
CALL_FIELDS = {"receiver_id", "method_name", "args", "gas", "deposit"}
FT_ARGS = {"receiver_id", "amount", "memo"}


class ApprovalError(ValueError):
    """An ApprovalDetail this adapter cannot read at all."""


def make_judge(config, evidence_dir, store):
    """judge(body) for the gate, policy and reports_account in config, built as gate/http.py builds its own."""
    gate, policy = config["gate"], config["policy"]
    reports_account = config["reports_account"]
    agent_pk_hex = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex()
    client, nearblocks, scanner, archival = _clients(gate)
    issuers = None if gate.endswith(".testnet") else rules.issuers_for("mainnet")

    def judge(body):
        return verdict.request(body["kind"], body["token"], body["receiver"], body["amount"], body["memo"],
                               gate=gate, agent_pk_hex=agent_pk_hex, policy=policy, store=store, rpc=client,
                               nearblocks=nearblocks, signer=None, sign_actions=(), injection_scanner=scanner,
                               archival=archival, evidence_dir=evidence_dir, issuers=issuers,
                               reports_account=reports_account)

    return judge


def transfer_body(request_type, data):
    """The gate/http.py body (kind, token, receiver, amount, memo) for the operation, or why there is none."""
    if not isinstance(data, dict):
        return None, "request_data is not an object"
    if request_type == "transfer":
        extra = sorted(set(data) - TRANSFER_FIELDS)
        if extra:
            return None, f"transfer carries fields this adapter does not judge: {', '.join(extra)}"
        if data.get("chain", "near") != "near":
            return None, f"transfer is on chain {data['chain']!r}, not near"
        if ("to" in data) == ("receiver_id" in data):
            return None, "transfer must name exactly one of to and receiver_id"
        return {"kind": "near", "token": None, "receiver": data.get("to", data.get("receiver_id")),
                "amount": data.get("amount"), "memo": None}, None
    if request_type == "call":
        extra = sorted(set(data) - CALL_FIELDS)
        if extra:
            return None, f"call carries fields this adapter does not judge: {', '.join(extra)}"
        if data.get("method_name") != "ft_transfer":
            return None, f"call to method {data.get('method_name')!r} is not ft_transfer"
        if data.get("deposit", "1") != "1":
            return None, "ft_transfer attaches a deposit other than 1 yoctoNEAR, which the verdict does not judge"
        args = data.get("args")
        if not isinstance(args, dict):
            return None, "ft_transfer args are not a JSON object"
        extra = sorted(set(args) - FT_ARGS)
        if extra:
            return None, f"ft_transfer args carry fields this adapter does not judge: {', '.join(extra)}"
        return {"kind": "ft", "token": data.get("receiver_id"), "receiver": args.get("receiver_id"),
                "amount": args.get("amount"), "memo": args.get("memo")}, None
    return None, f"request_type {request_type!r} is not a transfer this adapter judges"


def vote(detail, judge):
    """The vote for one ApprovalDetail: {"approval_id", "vote", "message", "reason", "judgement"}.

    vote is approve, reject or abstain; message is the unsigned vote string for approve and reject, else None;
    judgement is the verdict.request result, or None when nothing was judged. Raises ApprovalError when the
    detail is not an object or lacks a field the vote message needs.
    """
    if not isinstance(detail, dict):
        raise ApprovalError("the approval is not a JSON object")
    missing = [field for field in DETAIL_FIELDS if field not in detail]
    if missing:
        raise ApprovalError(f"the approval is missing {', '.join(missing)}")
    for field in ("id", "request_hash", "wallet_pubkey"):
        if not isinstance(detail[field], str) or not detail[field]:
            raise ApprovalError(f"{field} must be a non-empty string")
    approval_id = detail["id"]

    def abstain(reason, judgement=None):
        return {"approval_id": approval_id, "vote": "abstain", "message": None, "reason": reason,
                "judgement": judgement}

    if detail["status"] != "pending":
        return abstain(f"the approval is {detail['status']!r}, not pending")
    body, reason = transfer_body(detail["request_type"], detail["request_data"])
    if body is None:
        return abstain(reason)
    try:
        judgement = judge(body)
    except canon.CanonError as error:
        return abstain(f"the transfer has no canonical form: {error}")
    choice = VOTES[judgement["action"]]
    if choice == "abstain":
        return abstain(f"{judgement['outcome']} with action ask: a human decides on OutLayer's approvals page",
                       judgement)
    return {"approval_id": approval_id, "vote": choice,
            "message": f"{choice}:{approval_id}:{detail['wallet_pubkey']}:{detail['request_hash']}",
            "reason": f"{judgement['outcome']} with action {judgement['action']}", "judgement": judgement}
