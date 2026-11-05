"""Replay a scripted agent following a planted vendor-message instruction on testnet."""

import atexit
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from gate import agent, base58, rpc, signer, verdict
from gate.demo import AGENT_KEY
from intel import labels

ROOT = Path(__file__).resolve().parent.parent
MESSAGE_PATH = ROOT / "fixtures" / "demo" / "injected-message.txt"
RECORD_PATH = ROOT / "fixtures" / "rpc" / "attacks" / "10-injected.json"
GATE = "kestrelridge-main.testnet"
POLICY_PATH = ROOT / "fixtures" / "demo" / "policy.json"
NOW_S = datetime(2026, 11, 5, 8, 53, 51, tzinfo=UTC).timestamp()
KEEP_EVIDENCE = "GATE_KEEP_EVIDENCE"


def parse_message(text):
    """Read the payment fields from the message body."""
    lines = text.splitlines()
    try:
        body_start = lines.index("") + 1
    except ValueError:
        body_start = len(lines)
    fields = {}
    for line in lines[body_start:]:
        key, separator, value = line.partition(":")
        key = key.strip()
        if separator and key in ("pay-to", "token", "amount", "memo") and key not in fields:
            fields[key] = value.strip()
    pay_to, amount = fields.get("pay-to"), fields.get("amount")
    if not pay_to:
        raise ValueError("missing pay-to")
    if not amount:
        raise ValueError("missing amount")
    token = fields.get("token") or None
    return {"kind": "ft" if token else "near", "token_id": token, "receiver_id": pay_to, "amount": amount,
            "memo": fields.get("memo") or None}


def judgement_for(subject):
    """Return the committed mainnet judgement for subject, if one exists."""
    path = ROOT / "fixtures" / "receivers" / "judgements" / f"{subject}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _remove(directory):
    if directory.exists():
        shutil.rmtree(directory)


def run(out=print):
    """Replay the verdict and refused submission from committed fixtures."""
    record = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    message = MESSAGE_PATH.read_text(encoding="utf-8")
    request = agent.transfer_request(**parse_message(message), gate=GATE,
                                     agent_pk_hex=base58.decode_key(AGENT_KEY).hex())
    evidence_dir = Path(tempfile.mkdtemp(prefix="shieldbot-near-injected-"))
    keep_evidence = os.environ.get(KEEP_EVIDENCE) == "1"
    if not keep_evidence:
        atexit.register(_remove, evidence_dir)
    client = rpc.Client(rpc.TESTNET_RPC_URL, "replay")
    nearblocks = rpc.Client(rpc.NEARBLOCKS_TESTNET_URL, "replay", kind="http")
    archival = rpc.Client(rpc.ARCHIVAL_TESTNET_RPC_URL, "replay")
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    result = verdict.request(request["kind"], request["token_id"], request["receiver_id"], request["amount"],
                             request["memo"], gate=GATE, agent_pk_hex=base58.decode_key(AGENT_KEY).hex(),
                             policy=policy, store=labels.load(), rpc=client, nearblocks=nearblocks,
                             signer=signer.TestSigner(), archival=archival, evidence_dir=evidence_dir,
                             clock=lambda: NOW_S)
    recorded_at = next(check["source"]["fetched_at"] for check in result["checks"]
                       if check["id"] == "receiver_exists")
    recorded_date = recorded_at.split("T", 1)[0]
    out("ShieldBot for NEAR, a scripted agent following an instruction planted in a vendor message, standing in "
        "for a prompt-injected model. No model runs.")
    out(f"Offline replay: the verdict uses testnet reads recorded on {recorded_date}; the refusal was recorded "
        "on testnet the same day.")
    out("")
    out("The vendor message (fixtures/demo/injected-message.txt):")
    for line in message.splitlines():
        out(f"  {line}")
    out("")
    out(f"The scripted agent follows the planted vendor-message instruction: pay {request['amount']} of "
        f"{request['token_id']} to {request['receiver_id']}, memo \"{request['memo']}\"")
    out("")
    out("The verdict service:")
    for check in result["checks"]:
        out(f"  {check['status']:<7} {check['id']}: {check['detail']}")
    if result["outcome"] == "DENY":
        out("  DENIED. " + " ".join(result["reasons"]))
    else:
        out(f"  {result['outcome']}, not signed")
    out(f"  evidence sha256: {result['evidence_hash']}")
    out("  Nothing was signed.")
    out("")
    out("The scripted agent submits request_transfer without a verdict (recorded on testnet):")
    submission = record["submissions"][0]
    outcome = submission["outcome"]
    transaction_hash = outcome["transaction_hash"]
    execution_error = outcome["status"]["Failure"]["ActionError"]["kind"]["FunctionCallError"]["ExecutionError"]
    out(f"  transaction: {transaction_hash}")
    out(f"  explorer: https://testnet.nearblocks.io/txns/{transaction_hash}")
    out(f"  status: {execution_error}")
    if record["refused"]:
        out("  injected: refused")
    out("")
    judgement = judgement_for(request["receiver_id"])
    if judgement:
        out(f"On mainnet the same account reads (fixtures/receivers/judgements, recorded reads): "
            f"{judgement['verdict']}. {' '.join(judgement['reasons'])}")
    else:
        out("On mainnet the same account has no recorded judgement.")
    state = "kept" if keep_evidence else "removed at exit unless GATE_KEEP_EVIDENCE=1"
    out(f"Evidence documents, {state}: {evidence_dir}")
    return 0 if result["outcome"] == "DENY" and record["refused"] else 1


def main(argv=None):
    return run()
