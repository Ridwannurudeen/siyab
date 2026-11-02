"""python -m gate demo: four verdicts, replayed from recorded testnet data with --offline.

The fixtures under fixtures/demo/ are the testnet responses recorded on 1 and 2 Nov 2026 with
GATE_FIXTURES_MODE=record, reduced to the fields the code reads (a block keeps its header hash,
height and timestamp) and stored under their replay keys; policy.json is the agent policy the Gate's
get_agent returned. The four runs: the allowlisted vendor (ALLOW, signed with the TEST key and
verified back), a lookalike of the vendor dated by its oldest access key (DENY), the aged payee with
the archival block its oldest access key names left out of the fixtures (UNKNOWN, naming the checks
that could not run), and the EVM leg over the recorded dry 1Click quote and GoPlus answer (DENY,
unsigned). The demo clock is pinned inside the recording window so the 7 day checks replay as
recorded.

The evidence documents go into a temporary directory, named on the last line and removed when the process
exits unless GATE_KEEP_EVIDENCE=1.

--live runs the same four against testnet, NearBlocks testnet, the testnet archival RPC and a dry
1Click quote on mainnet, reading the policy from get_agent. It reads only, writes no fixture and
sends nothing; an ALLOW is signed with the TEST key, which the testnet Gate does not trust.
"""

import atexit
import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from gate import base58, canon, ed25519, evidence, intents, rpc, signer, verdict
from intel import labels

DEMO_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "demo"
DEMO_NOW_S = datetime(2026, 11, 2, 7, 0, tzinfo=UTC).timestamp()
GATE = "kestrelridge-main.testnet"
TOKEN = "token.kestrelridge-main.testnet"
VENDOR = "kestrelridge-shop.testnet"
LOOKALIKE = "kestrelridge-sh0p.testnet"
AGED = "lindenvale.testnet"
# The agent's function call key on the Gate, from the recorded view_access_key_list of the Gate.
AGENT_KEY = "ed25519:Hz6UZRmRNCKxAV5WiHEBMobwr9T4tUvtcijGycLBAeX2"
AMOUNT = "1000000"
MEMO = "invoice 1"
KEEP_EVIDENCE = "GATE_KEEP_EVIDENCE"
EVM_ORIGIN = "nep141:wrap.near"
EVM_DESTINATION = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
EVM_AMOUNT = "1000000000000000000000000"
EVM_RECIPIENT = "0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107"
RUNS = (
    ("The allowlisted vendor", VENDOR, None),
    ("A lookalike of the vendor, off the allowlist", LOOKALIKE, None),
    ("The aged payee, off the allowlist", AGED,
     ("the archival block its oldest access key names is left out of fixtures/demo, so its account age cannot "
      "be read")),
)


def _run(title, receiver, note, clients, policy, store, evidence_dir, clock, out):
    client, nearblocks, archival = clients
    result = verdict.request("ft", TOKEN, receiver, AMOUNT, MEMO, gate=GATE,
                             agent_pk_hex=base58.decode_key(AGENT_KEY).hex(), policy=policy, store=store,
                             rpc=client, nearblocks=nearblocks, archival=archival, signer=signer.TestSigner(),
                             evidence_dir=evidence_dir, clock=clock)
    out(f'{title}: {AMOUNT} of {TOKEN} to {receiver}, memo "{MEMO}"')
    if note:
        out(f"  offline, {note}")
    for check in result["checks"]:
        out(f"  {check['status']:<7} {check['id']}: {check['detail']}")
    if result["outcome"] == "ALLOW":
        message = result["message"].encode("utf-8")
        public = bytes.fromhex(signer.TestSigner().public_key_hex)
        verified = ed25519.verify(public, canon.digest(message), bytes.fromhex(result["verdict"]["signature"]))
        out("  ALLOW, signed with the TEST key (key id cur); signature verified with gate.ed25519: "
            f"{verified}")
        out(f"  canonical message: {result['message']}")
        if not verified:
            raise RuntimeError("the TEST signature did not verify")
    elif result["outcome"] == "DENY":
        out("  " + " ".join(["DENIED."] + result["reasons"]))
    else:
        failed = [c["id"] for c in result["checks"] if c["status"] == "unknown"]
        out(f"  UNKNOWN, not signed: {', '.join(failed)} could not run")
    out(f"  evidence sha256: {result['evidence_hash']}")
    out(f"  latency: {result['latency_ms']} ms")
    out("")
    return result


def _evm_run(mode, deadline, evidence_dir, clock, out):
    started = time.perf_counter()
    result = intents.judge_quote(EVM_ORIGIN, EVM_DESTINATION, EVM_AMOUNT, EVM_RECIPIENT, mode=mode,
                                 firewall_url=os.environ.get("SHIELDBOT_API_URL"), clock=clock, deadline=deadline)
    latency_ms = int((time.perf_counter() - started) * 1000)
    path = evidence.write(result["document"], evidence_dir)
    out(f"The EVM leg: a dry 1Click quote of {EVM_AMOUNT} of {EVM_ORIGIN} to {EVM_DESTINATION}, recipient "
        f"{EVM_RECIPIENT}")
    for check in result["checks"]:
        out(f"  {check['status']:<7} {check['id']}: {check['detail']}")
    if result["verdict"] == "DENY":
        out("  " + " ".join(["DENIED."] + result["reasons"]))
    else:
        out("  " + " ".join([f"{result['verdict']}, unsigned preflight."] + result["reasons"]))
    out("  Nothing was signed, nothing was deposited.")
    out(f"  evidence sha256: {path.stem}")
    out(f"  latency: {latency_ms} ms")
    out("")
    return {"outcome": result["verdict"], "verdict": None, "checks": result["checks"], "reasons": result["reasons"],
            "evidence_hash": path.stem, "evidence_path": str(path), "latency_ms": latency_ms}


def _remove(directory):
    """Remove the evidence directory at exit; a caller that read the results may have removed it already."""
    if directory.exists():
        shutil.rmtree(directory)


def run(out=print, live=False):
    """Run the four demo verdicts, replayed from fixtures/demo or, with live, read from the networks; returns
    their results. The evidence directory is removed when the process exits unless GATE_KEEP_EVIDENCE=1."""
    evidence_dir = Path(tempfile.mkdtemp(prefix="shieldbot-near-demo-"))
    if os.environ.get(KEEP_EVIDENCE) != "1":
        atexit.register(_remove, evidence_dir)
    store = labels.load()
    if live:
        out("ShieldBot for NEAR live demo v1: the same four over testnet RPC, NearBlocks testnet, the testnet")
        out("archival RPC and a dry 1Click quote on mainnet. It reads only: nothing is sent and no fixture is")
        out("written. An ALLOW is signed with the TEST key, which the testnet Gate does not trust.")
        clients = (rpc.Client(rpc.TESTNET_RPC_URL, "off"),
                   rpc.Client(rpc.NEARBLOCKS_TESTNET_URL, "off", kind="http"),
                   rpc.Client(rpc.ARCHIVAL_TESTNET_RPC_URL, "off"))
        policy = clients[0].call_function(GATE, "get_agent", {"public_key": AGENT_KEY})
        clock, mode, deadline = time.time, "off", intents.live_deadline()
    else:
        out("ShieldBot for NEAR offline demo v1: testnet responses recorded on 1 and 2 Nov 2026, replayed from")
        out("fixtures/demo with the TEST verifier key and no network. The TEST key is derived from a published")
        out("seed and is never registered on testnet. The demo clock is pinned to 2026-11-02T07:00:00Z, inside")
        out("the recording window, so the 7 day checks replay as recorded.")
        clients = (rpc.Client(rpc.TESTNET_RPC_URL, "replay", fixtures_dir=DEMO_DIR),
                   rpc.Client(rpc.NEARBLOCKS_TESTNET_URL, "replay", kind="http", fixtures_dir=DEMO_DIR),
                   rpc.Client(rpc.ARCHIVAL_TESTNET_RPC_URL, "replay", fixtures_dir=DEMO_DIR))
        policy = json.loads((DEMO_DIR / "policy.json").read_text(encoding="utf-8"))
        clock, mode, deadline = (lambda: DEMO_NOW_S), "replay", intents.RECORDED_DEADLINE
    out("")
    results = [_run(title, receiver, None if live else note, clients, policy, store, evidence_dir, clock, out)
               for title, receiver, note in RUNS]
    results.append(_evm_run(mode, deadline, evidence_dir, clock, out))
    out(f"Evidence documents, removed at exit unless {KEEP_EVIDENCE}=1: {evidence_dir}")
    return results


def main(offline):
    """The demo command; returns 0. Any exception propagates, so python -m gate exits 1."""
    run(live=not offline)
    return 0
