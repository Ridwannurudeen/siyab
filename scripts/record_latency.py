"""Record live testnet verdict runs into a latency fixture in the shape scripts/counts.py reads.

Usage:
  GATE_FIXTURES_MODE=off python scripts/record_latency.py --receiver ACCOUNT --runs N --out FIXTURE [--replace]

Each run is one gate.verdict.request, the function python -m gate verdict --live calls, with the clients _clients
in gate/__main__.py builds from the same environment (NEAR_RPC_URL, NEARBLOCKS_API_URL, NEAR_ARCHIVAL_RPC_URL,
SHIELDBOT_API_URL), the same REPORTS_ACCOUNT_ID, the label store, no issuer table (the Gate is on testnet) and the
policy the Gate's get_agent returns for the agent key, read once before the first run. The transfer is the one the
live demo judges (gate/demo.py): AMOUNT of TOKEN to the receiver at GATE, memo MEMO, for the agent function call key
AGENT_KEY. GATE_FIXTURES_MODE must be off, so every run reads the networks and no fixture is written under
fixtures/rpc/.

No live key is read: VERIFIER_KEY_FILE is never opened, and an ALLOW whose action is sign is signed with the TEST
key (gate.signer.TestSigner), which the testnet Gate does not trust, as python -m gate verdict signs without
--live. gate.verdict.request signs after it measures the latency, so the signature is not in latency_ms.

latency_ms is the evidence document's own latency_ms: time.perf_counter around the rules in gate.verdict.request,
the latency python -m gate verdict prints. The runs run in one process, RUN_GAP_S apart, as the committed tier 2
runs did (docs/EVIDENCE.md), so the NearBlocks spacing of gate/rpc.py holds across runs as it did then.

Each row is {receiver, run, verdict, latency_ms, path}, read from the run's evidence document. path says which
tiers ran and which source dated the account, from the checks of the document:

  tier 1                                                            no tier 2 check ran (an allowlisted payee)
  tier 1 and 2, account age from the NearBlocks key creation time   account_age reads "created ... ago"
  tier 1 and 2, account age from the access key nonce               account_age names the oldest access key nonce
  tier 1 and 2, account age from the archival account state         account_age names view_account at a block

A run fails when the verdict raises, reads UNKNOWN or has an account_age that names none of these sources; the
recorder then writes nothing and exits 1. It refuses an invalid receiver, a GATE_FIXTURES_MODE other than off, an
agent key the Gate does not know, and an existing --out without --replace, each with exit 2 before any run. It
writes the fixture once, after the last run, then prints the line scripts/counts.py prints for that file: the
Latency line when every run is tier 1, else the line of the payee off the allowlist. The evidence documents go into
a temporary directory, named on the last line and removed when the process exits unless GATE_KEEP_EVIDENCE=1.
"""

import argparse
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import counts
from gate import base58, canon, demo, rules, signer, verdict
from gate.__main__ import _clients
from intel import labels

RUN_GAP_S = 3
TIER1 = "tier 1"
NEARBLOCKS_AGE = "tier 1 and 2, account age from the NearBlocks key creation time"
NONCE_AGE = "tier 1 and 2, account age from the access key nonce"
STATE_AGE = "tier 1 and 2, account age from the archival account state"


class Refused(Exception):
    """A setting the recorder does not run with; nothing ran and nothing was written."""


class RunFailed(Exception):
    """A run that gave no complete verdict; nothing is written."""


def path_of(checks):
    """Which tiers ran and which source dated the account, from the checks of an evidence document; None when
    account_age could not run or names no source this recorder knows."""
    by_id = {check["id"]: check for check in checks}
    if not set(rules.TIER2_CHECKS) & by_id.keys():
        return TIER1
    age = by_id["account_age"]
    if age["status"] == "unknown":
        return None
    if age["detail"].startswith("created "):
        return NEARBLOCKS_AGE
    if "oldest access key nonce" in age["detail"]:
        return NONCE_AGE
    if "view_account finds" in age["detail"]:
        return STATE_AGE
    return None


def live_setup():
    """(clients, policy): the clients python -m gate verdict builds for the Gate, and the policy the Gate's get_agent
    returns for the agent key; raises Refused unless GATE_FIXTURES_MODE is off, and when the key is not registered."""
    mode = os.environ.get("GATE_FIXTURES_MODE")
    if mode != "off":
        raise Refused(f"live runs need GATE_FIXTURES_MODE=off, and it is {mode or 'unset (replay)'}")
    clients = _clients(demo.GATE)
    policy = clients[0].call_function(demo.GATE, "get_agent", {"public_key": demo.AGENT_KEY})
    if policy is None:
        raise Refused(f"the agent key {demo.AGENT_KEY} is not registered on {demo.GATE}")
    return clients, policy


def record(receiver, runs, clients, policy, evidence_dir):
    """The rows of runs live verdicts for receiver, printed as they come; raises RunFailed at the first that fails."""
    client, nearblocks, scanner, archival = clients
    store, test_signer = labels.load(), signer.TestSigner()
    agent_pk_hex = base58.decode_key(demo.AGENT_KEY).hex()
    reports_account = os.environ.get("REPORTS_ACCOUNT_ID")
    rows = []
    for run in range(1, runs + 1):
        if run > 1:
            time.sleep(RUN_GAP_S)
        try:
            result = verdict.request("ft", demo.TOKEN, receiver, demo.AMOUNT, demo.MEMO, gate=demo.GATE,
                                     agent_pk_hex=agent_pk_hex, policy=policy, store=store, rpc=client,
                                     nearblocks=nearblocks, signer=test_signer, injection_scanner=scanner,
                                     archival=archival, evidence_dir=evidence_dir, issuers=None,
                                     reports_account=reports_account)
        except rules.UNAVAILABLE as error:
            raise RunFailed(f"run {run}: {type(error).__name__}: {error}") from error
        document = json.loads(Path(result["evidence_path"]).read_text(encoding="utf-8"))
        if document["verdict"] == "UNKNOWN":
            failed = "; ".join(f"{check['id']}: {check['detail']}" for check in document["checks"]
                               if check["status"] == "unknown")
            raise RunFailed(f"run {run}: UNKNOWN, a check could not run ({failed})")
        path = path_of(document["checks"])
        if path is None:
            age = next(check for check in document["checks"] if check["id"] == "account_age")
            raise RunFailed(f"run {run}: account_age names no source that dated the account: {age['detail']}")
        row = {"receiver": document["subject"]["receiver"], "run": run, "verdict": document["verdict"],
               "latency_ms": document["latency_ms"], "path": path}
        print(f"run {run}: {row['verdict']} in {row['latency_ms']} ms, {path}")
        rows.append(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="record live testnet verdict latency runs into a latency fixture")
    parser.add_argument("--receiver", required=True, help="the payee account each verdict judges")
    parser.add_argument("--runs", required=True, type=int, help="the number of verdicts to run")
    parser.add_argument("--out", required=True, help="the fixture to write, such as fixtures/latency/verdict-runs.json")
    parser.add_argument("--replace", action="store_true", help="overwrite --out when it exists")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    out = Path(args.out)
    try:
        canon.account_id(args.receiver, "--receiver")
        if out.exists() and not args.replace:
            raise Refused(f"{args.out} exists; pass --replace to overwrite it")
        clients, policy = live_setup()
    except (Refused, canon.CanonError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    except rules.UNAVAILABLE as error:
        print(f"cannot read get_agent on {demo.GATE}: {type(error).__name__}: {error}; nothing written",
              file=sys.stderr)
        return 1
    evidence_dir = Path(tempfile.mkdtemp(prefix="shieldbot-near-latency-"))
    if os.environ.get(demo.KEEP_EVIDENCE) != "1":
        atexit.register(shutil.rmtree, evidence_dir)
    try:
        rows = record(args.receiver, args.runs, clients, policy, evidence_dir)
    except RunFailed as error:
        print(f"failed: {error}; nothing written", file=sys.stderr)
        return 1
    out.write_bytes((json.dumps(rows, indent=2) + "\n").encode("utf-8"))
    print(f"wrote {out.as_posix()}")
    print(counts.latency_line(out) if all(row["path"] == TIER1 for row in rows) else counts.tier2_latency_line(out))
    print(f"Evidence documents, removed at exit unless {demo.KEEP_EVIDENCE}=1: {evidence_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
