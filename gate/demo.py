"""python -m gate demo --offline: three verdicts over synthetic fixtures, no network.

The fixtures under fixtures/demo/ are synthetic (each file is named synthetic-*.json) and hold
responses in the exact JSON-RPC and NearBlocks shapes; no testnet account exists before 1 Nov.
The three runs: the allowlisted vendor (ALLOW, signed with the TEST key and verified back), a
lookalike of the vendor created 3 h before the demo clock by a dust deposit (DENY), and a
receiver whose account age record is missing (UNKNOWN, naming the check that could not run).
"""

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from gate import canon, ed25519, rpc, signer, verdict
from intel import labels

DEMO_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "demo"
DEMO_NOW_S = datetime(2026, 11, 1, 12, 0, tzinfo=UTC).timestamp()
GATE = "gate.testnet"
TOKEN = "token.gate.testnet"
VENDOR = "vendor.testnet"
POLICY = {"allowlist": [VENDOR], "caps": [{"asset": {"Ft": TOKEN}}]}
RUNS = (
    ("The allowlisted vendor", VENDOR),
    ("A lookalike of the vendor, off the allowlist", "vend0r.testnet"),
    ("A receiver off the allowlist whose account age record is missing", "supplier.testnet"),
)


def _replay_dir(directory):
    """Copy each synthetic fixture to <key>.json so the replay client finds it."""
    for path in sorted(DEMO_DIR.glob("synthetic-*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        key = rpc.fixture_key(fixture["url"], fixture["request"]["method"], fixture["request"]["params"])
        shutil.copyfile(path, Path(directory) / f"{key}.json")


def _run(title, receiver, clients, store, evidence_dir, out):
    client, nearblocks = clients
    result = verdict.request("ft", TOKEN, receiver, "1000000", "invoice 1", gate=GATE,
                             agent_pk_hex=ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex(),
                             policy=POLICY, store=store, rpc=client, nearblocks=nearblocks,
                             signer=signer.TestSigner(), evidence_dir=evidence_dir, clock=lambda: DEMO_NOW_S)
    out(f"{title}: 1000000 of {TOKEN} to {receiver}, memo \"invoice 1\"")
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


def run(out=print):
    """Run the three demo verdicts; returns their results."""
    out("ShieldBot for NEAR offline demo v0: synthetic fixtures from fixtures/demo, the TEST verifier key,")
    out("no network. The TEST key is derived from a published seed and is never registered on testnet.")
    out("")
    with tempfile.TemporaryDirectory() as replay:
        _replay_dir(replay)
        clients = (rpc.Client(rpc.TESTNET_RPC_URL, "replay", fixtures_dir=replay),
                   rpc.Client(rpc.NEARBLOCKS_TESTNET_URL, "replay", kind="http", fixtures_dir=replay))
        evidence_dir = Path(tempfile.mkdtemp(prefix="shieldbot-near-demo-"))
        store = labels.load()
        results = [_run(title, receiver, clients, store, evidence_dir, out) for title, receiver in RUNS]
    out(f"Evidence documents: {evidence_dir}")
    return results


def main(offline):
    """The demo command; returns 0. Any exception propagates, so python -m gate exits 1."""
    if not offline:
        print("demo v0 runs offline only: python -m gate demo --offline")
        return 2
    run()
    return 0
