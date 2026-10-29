"""A bounded exit trace from an account that carries an incident label, and only from one.

python -m intel flow <subject> [--depth 3] [--max 200] [--name <file name>] [--record] refuses at
once, with the reason "subject carries no incident label", unless intel/labels.jsonl holds an
incident assertion on the subject. Otherwise it reads the subject's transfers from
https://transfers.main.fastnear.com/v0/transfers ({"account_id": <account>, "limit": 100},
following resume_token), keeps the transfers the account sent (a negative amount), and descends
into each receiver that is not a terminal account, breadth first, to the depth bound and at most
the transfer cap in all, reading at most MAX_REQUESTS pages. A terminal account is a NEAR account
the label store labels catalog_token, bridge or infrastructure (intents.near, zec.omft.near, the
bridge token accounts): its hop is kept and the trace goes no further. The trace is written as
intel/flows/<name>.json, where the name defaults to the existing trace file of the subject, else
the subject itself: {subject, assertion_ids, depth, hops [{from, to, asset_id, amount,
transaction_hash, block, method_name, receipt_id}], terminal_accounts, truncated, fetched_at}.
truncated is true when the cap or the page budget stopped the walk before every account within the
depth was read in full. --record fetches the pages that are not recorded under fixtures/fastnear/;
without it nothing touches the network. Every hop is a transfer read with its transaction hash,
block and receipt; the trace names no person and no motive.
"""

import json
from pathlib import Path

from gate import rpc
from intel import labels

ROOT = Path(__file__).resolve().parent.parent
FLOWS_DIR = Path(__file__).resolve().parent / "flows"
FASTNEAR_DIR = ROOT / "fixtures" / "fastnear"
TRANSFERS_URL = "https://transfers.main.fastnear.com"
TERMINAL_CATEGORIES = ("catalog_token", "bridge", "infrastructure")
REFUSAL = "subject carries no incident label"
PAGE_LIMIT = 100
MAX_REQUESTS = 60
DEFAULT_DEPTH = 3
DEFAULT_MAX = 200


class Refused(Exception):
    """The subject carries no incident label, so it is never traced."""


def incident_ids(store, subject):
    """The ids of the current incident assertions on the subject, on any chain."""
    return [a.id for a in store.assertions
            if a.subject == subject and a.category == "incident" and a.superseded_by is None]


def reached_depth(document):
    """The depth the walk reached: 1 for a hop from the subject, one more for each hop beyond it."""
    depths, reached = {document["subject"]: 0}, 0
    for hop in document["hops"]:
        depth = depths[hop["from"]] + 1
        depths.setdefault(hop["to"], depth)
        reached = max(reached, depth)
    return reached


def summary(document):
    """One line on how far the walk went and why it stopped."""
    stop = ("; the transfer cap or the page budget stopped it" if document["truncated"]
            else "; every account within the bound was read")
    return (f"{len(document['hops'])} hops; the walk reached depth {reached_depth(document)} of a bound of "
            f"{document['depth']}{stop}; terminal accounts {', '.join(document['terminal_accounts']) or 'none'}")


def terminal_accounts(store):
    return {a.subject for a in store.assertions
            if a.chain == "near" and a.superseded_by is None and a.category in TERMINAL_CATEGORIES}


def _hop(transfer):
    return {"from": transfer["account_id"], "to": transfer["other_account_id"], "asset_id": transfer["asset_id"],
            "amount": transfer["amount"].lstrip("-"), "transaction_hash": transfer["transaction_id"],
            "block": int(transfer["block_height"]), "method_name": transfer["method_name"],
            "receipt_id": transfer["receipt_id"]}


def trace(subject, store, client, depth=DEFAULT_DEPTH, cap=DEFAULT_MAX):
    """The trace document; raises Refused for a subject without an incident label."""
    assertion_ids = incident_ids(store, subject)
    if not assertion_ids:
        raise Refused(REFUSAL)
    terminal = terminal_accounts(store)
    hops, reached, fetched, seen = [], [], [], {subject}
    level, requests, truncated = [subject], 0, False
    for _ in range(depth):
        following = []
        for account in level:
            resume_token = None
            while True:
                if len(hops) >= cap or requests >= MAX_REQUESTS:
                    truncated = True
                    break
                body = {"account_id": account, "limit": PAGE_LIMIT}
                if resume_token is not None:
                    body["resume_token"] = resume_token
                page = client.post("/v0/transfers", body)
                requests += 1
                fetched.append(client.last_source["fetched_at"])
                if not isinstance(page, dict) or not isinstance(page.get("transfers"), list):
                    raise rpc.MalformedResponse("the transfers page has no transfers list")
                for transfer in page["transfers"]:
                    if not str(transfer.get("amount", "")).startswith("-") or transfer["account_id"] != account:
                        continue
                    if len(hops) >= cap:
                        truncated = True
                        break
                    hop = _hop(transfer)
                    hops.append(hop)
                    receiver = hop["to"]
                    if receiver in terminal:
                        if receiver not in reached:
                            reached.append(receiver)
                    elif receiver not in seen:
                        seen.add(receiver)
                        following.append(receiver)
                resume_token = page.get("resume_token")
                if truncated or not resume_token or not page["transfers"]:
                    break
            if truncated:
                break
        if truncated:
            break
        level = following
    return {"subject": subject, "assertion_ids": assertion_ids, "depth": depth, "hops": hops,
            "terminal_accounts": reached, "truncated": truncated, "fetched_at": max(fetched) if fetched else None}


def find(subject, flows_dir=FLOWS_DIR):
    """The committed trace file whose subject is the given one, or None."""
    for path in sorted(Path(flows_dir).glob("*.json")):
        if json.loads(path.read_text(encoding="utf-8")).get("subject") == subject:
            return path
    return None


def flow_path(subject, name=None, flows_dir=FLOWS_DIR):
    """intel/flows/<name>.json: the given name, the subject's existing trace file, else the subject."""
    if name is None:
        existing = find(subject, flows_dir)
        if existing is not None:
            return existing
        name = subject
    path = Path(flows_dir) / f"{name}.json"
    if path.resolve().parent != Path(flows_dir).resolve():
        raise ValueError(f"not a trace file name: {name!r}")
    return path


def main(subject, depth, cap, name, record, out=print, store=None, flows_dir=FLOWS_DIR):
    """The flow command: 0 when the trace was written, 2 when it is refused."""
    store = store or labels.load()
    client = rpc.Client(TRANSFERS_URL, "record" if record else "replay", kind="http", fixtures_dir=FASTNEAR_DIR)
    try:
        document = trace(subject, store, client, depth, cap)
    except Refused as error:
        out(f"refused: {error}")
        return 2
    path = flow_path(subject, name, flows_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    out(f"{subject}: {summary(document)}")
    out(f"written to {path.name}")
    return 0
