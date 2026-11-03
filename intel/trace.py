"""A bounded exit trace from an account that carries an incident label, and only from one.

python -m intel flow <subject> [--depth 3] [--max 600] [--name <file name>] [--record] refuses at
once, with the reason "subject carries no incident label", unless intel/labels.jsonl holds an
incident assertion on the subject, and with "no incident page names the subject" unless a page of
intel/incidents/ lists it among its accounts. Otherwise it reads the transfers the subject sent from
https://transfers.main.fastnear.com/v0/transfers with the body {"account_id", "direction": "sender",
"ignore_system": true, "from_timestamp_ms": <window start>, "limit": 100} and the resume_token of
the previous page, in the order the API returns them, and no server side USD filter (it would drop
the rows that carry no usd_amount). The window starts at 00:00 UTC on the day before the incident
page's date; older outbound rows are outside it. The walk descends breadth first into each receiver
that is not a terminal account, to the depth bound and at most the transfer cap in all, reading at
most MAX_REQUESTS pages; a 429 is retried after BACKOFF_S seconds and the third 429 in a row stops
the run. A terminal account is a NEAR account the label store labels catalog_token, bridge or
infrastructure (intents.near, zec.omft.near, the bridge token accounts), or the subject itself when
the walk comes back to it: its hop is kept and the trace goes no further.

The trace is written as intel/flows/<name>.json, where the name defaults to the existing trace file
of the subject, else the subject itself: {subject, assertion_ids, depth, filters, window, hops
[{from, to, asset_id, amount, human_amount, usd_amount, transaction_hash, block, block_timestamp,
method_name, receipt_id}], terminal_accounts, truncated, completeness, depth_reached, shares,
state_at_block, fetched_at}. A hop's amount, human_amount and usd_amount are those of the transfer
the account sent, without the minus sign the API gives an outbound row. completeness is "complete"
when every account within the bound was read in full, else the account, its depth and the reason
the walk stopped there. shares splits the
subject's direct outbound rows by where they end (each terminal account, each account the incident
page names, and "accounts the report does not name, not terminal"): the USD sum, the row count and
the percentage of the direct outbound USD total, over the rows whose usd_amount is at least MIN_USD;
rows with a smaller usd_amount and rows without one (usd_amount null) are counted apart, the latter
with their sum per asset_id in base units. Deeper hops are kept and never summed into the shares.
state_at_block holds, for each issuer token of intel/issuers.json (mainnet), ft_balance_of and the
issuer's status view on the subject from https://free.rpc.fastnear.com, each with its method, args,
block, result and result sha256; the decimals a balance is shown with come from the 1Click catalog
snapshot under intel/seeds/raw/.

--record fetches the pages and views that are not recorded under fixtures/fastnear/ and
fixtures/rpc/; without it nothing touches the network. Every hop is a transfer read with its
transaction hash, block and receipt; the trace names no person and no motive.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from gate import rpc, rules
from intel import labels, seed

ROOT = Path(__file__).resolve().parent.parent
FLOWS_DIR = Path(__file__).resolve().parent / "flows"
INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"
FASTNEAR_DIR = ROOT / "fixtures" / "fastnear"
TRANSFERS_URL = "https://transfers.main.fastnear.com"
TERMINAL_CATEGORIES = ("catalog_token", "bridge", "infrastructure")
REFUSAL = "subject carries no incident label"
NO_PAGE = "no incident page names the subject"
NOT_NAMED = "accounts the report does not name, not terminal"
PAGE_LIMIT = 100
MAX_REQUESTS = 150
BACKOFF_S = 10
MAX_429_IN_A_ROW = 3
DEFAULT_DEPTH = 3
DEFAULT_MAX = 600
MIN_USD = 1.0


class Refused(Exception):
    """The subject carries no incident label, or no incident page names it, so it is never traced."""


def incident_ids(store, subject):
    """The ids of the current incident assertions on the subject, on any chain."""
    return [a.id for a in store.assertions
            if a.subject == subject and a.category == "incident" and a.superseded_by is None]


def incident_for(subject, incidents_dir=INCIDENTS_DIR):
    """The incident page that lists the subject among its accounts, or None."""
    for path in sorted(Path(incidents_dir).glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if subject in {account["subject"] for account in document["accounts"]}:
            return document
    return None


def window(incident_date):
    """The window of the walk: from 00:00 UTC on the day before the incident date."""
    start = datetime.strptime(incident_date, "%Y-%m-%d").replace(tzinfo=UTC) - timedelta(days=1)
    return {"from": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "from_timestamp_ms": int(start.timestamp()) * 1000,
            "incident_date": incident_date, "rule": "00:00 UTC on the day before the incident date"}


def hop_depths(document):
    """The depth of each hop: 1 for a hop from the subject, one more for each hop beyond it."""
    depths, found = {document["subject"]: 0}, []
    for hop in document["hops"]:
        depth = depths[hop["from"]] + 1
        depths.setdefault(hop["to"], depth)
        found.append(depth)
    return found


def reached_depth(document):
    """The depth the walk reached."""
    return max(hop_depths(document), default=0)


def human(amount, decimals):
    """A base unit amount in whole tokens, with thousands separators."""
    return f"{(Decimal(int(amount)) / (Decimal(10) ** decimals)).normalize():,f}"


def _usd(text):
    return f"{Decimal(text).quantize(Decimal('0.01')):,f} USD"


def _stop_text(completeness):
    if completeness == "complete":
        return "complete"
    return f"stopped at depth {completeness['depth']} reading {completeness['account']}: {completeness['reason']}"


def _state_text(reads):
    parts = []
    for balance, status in zip(reads[::2], reads[1::2]):
        answer = json.dumps(status["result"], ensure_ascii=False)
        said = (f"banned by the issuer, {status['method']} answers {answer}" if status["banned"]
                else f"issuer status {status['method']} answers {answer}")
        parts.append(f"{human(balance['result'], balance['decimals'])} of {balance['token']} at block "
                     f"{balance['block']}, {said} at block {status['block']}")
    return "; ".join(parts)


def summary(document):
    """One line on how far the walk went, where the direct outbound value ended and the subject's
    state at a block."""
    accounts = {document["subject"]} | {h["from"] for h in document["hops"]} | {h["to"] for h in document["hops"]}
    shares = document["shares"]
    ends = ", ".join(f"{bucket['to']} {bucket['percent']} percent" for bucket in shares["buckets"]) or "none"
    return (f"{len(document['hops'])} hops over {len(accounts)} accounts; depth {document['depth_reached']} of "
            f"{document['depth']}, {_stop_text(document['completeness'])}; direct outbound after "
            f"{document['window']['from'][:10]} 00:00 UTC: {_usd(shares['direct_outbound_usd'])} across "
            f"{shares['rows']} rows ({shares['without_usd']['count']} rows without a USD amount, "
            f"{shares['below_min_usd']['count']} below {MIN_USD} USD); terminals: {ends}; subject: "
            f"{_state_text(document['state_at_block']) or 'no issuer read'}")


def terminal_accounts(store):
    return {a.subject for a in store.assertions
            if a.chain == "near" and a.superseded_by is None and a.category in TERMINAL_CATEGORIES}


def _unsigned(value):
    return None if value is None else abs(value)


def _hop(transfer):
    return {"from": transfer["account_id"], "to": transfer["other_account_id"], "asset_id": transfer["asset_id"],
            "amount": transfer["amount"].lstrip("-"), "human_amount": _unsigned(transfer.get("human_amount")),
            "usd_amount": _unsigned(transfer.get("usd_amount")), "transaction_hash": transfer["transaction_id"],
            "block": int(transfer["block_height"]), "block_timestamp": int(transfer["block_timestamp"]),
            "method_name": transfer["method_name"], "receipt_id": transfer["receipt_id"]}


def shares(subject, hops, terminal, named):
    """The subject's direct outbound rows split by where they end; see the module text."""
    buckets, below_count, below_usd, without_count, without_units = {}, 0, Decimal(0), 0, {}
    for hop in hops:
        if hop["from"] != subject:
            continue
        if hop["usd_amount"] is None:
            without_count += 1
            without_units[hop["asset_id"]] = without_units.get(hop["asset_id"], 0) + int(hop["amount"])
            continue
        usd = Decimal(str(hop["usd_amount"]))
        if usd < Decimal(str(MIN_USD)):
            below_count += 1
            below_usd += usd
            continue
        receiver = hop["to"]
        if receiver in terminal:
            key, kind = receiver, "terminal"
        elif receiver in named:
            key, kind = receiver, "named in the report"
        else:
            key, kind = NOT_NAMED, "not named"
        bucket = buckets.setdefault(key, {"to": key, "kind": kind, "usd": Decimal(0), "count": 0})
        bucket["usd"] += usd
        bucket["count"] += 1
    total = sum((b["usd"] for b in buckets.values()), Decimal(0))
    ordered = sorted(buckets.values(), key=lambda b: (-b["usd"], b["to"]))
    return {"min_usd": MIN_USD, "direct_outbound_usd": str(total), "rows": sum(b["count"] for b in ordered),
            "buckets": [{"to": b["to"], "kind": b["kind"], "usd": str(b["usd"]), "count": b["count"],
                         "percent": round(float(b["usd"] * 100 / total), 2)} for b in ordered],
            "below_min_usd": {"count": below_count, "usd": str(below_usd)},
            "without_usd": {"count": without_count,
                            "base_units": {asset: str(units) for asset, units in sorted(without_units.items())}}}


def _result_sha256(value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def state_at_block(subject, rpc_client, issuers):
    """For each issuer token: ft_balance_of and the issuer's status view on the subject, each read
    once with its block; returns (reads, fetched_at times)."""
    reads, fetched = [], []
    for entry in issuers:
        token = entry["token"]
        for method, args in (("ft_balance_of", {"account_id": subject}), (entry["method"], {entry["args_key"]: subject})):
            result = rpc_client.call_function(token, method, args)
            read = {"token": token, "method": method, "args": args, "block": rpc_client.last_block_height,
                    "result": result, "result_sha256": _result_sha256(result)}
            if method == "ft_balance_of":
                read["decimals"] = entry["decimals"]
            else:
                read["banned"] = result == entry["banned"]
            reads.append(read)
            fetched.append(rpc_client.last_source["fetched_at"])
    return reads, fetched


def _page(client, body):
    """One transfers page; a 429 waits BACKOFF_S and retries, the third in a row is raised."""
    in_a_row = 0
    while True:
        try:
            return client.post("/v0/transfers", body)
        except rpc.HttpError as error:
            if error.status != 429:
                raise
            in_a_row += 1
            if in_a_row >= MAX_429_IN_A_ROW:
                raise
            client.sleep(BACKOFF_S)


def _stop(account, level_depth, reason):
    return {"account": account, "depth": level_depth, "reason": reason}


def trace(subject, store, client, rpc_client, incident, issuers, depth=DEFAULT_DEPTH, cap=DEFAULT_MAX):
    """The trace document; raises Refused for a subject without an incident label or page."""
    assertion_ids = incident_ids(store, subject)
    if not assertion_ids:
        raise Refused(REFUSAL)
    if incident is None:
        raise Refused(NO_PAGE)
    span = window(incident["date"])
    filters = {"direction": "sender", "ignore_system": True, "from_timestamp_ms": span["from_timestamp_ms"],
               "limit": PAGE_LIMIT}
    terminal = terminal_accounts(store) | {subject}
    named = {account["subject"] for account in incident["accounts"]} - {subject}
    hops, reached, fetched, seen = [], [], [], {subject}
    level, requests, stopped = [subject], 0, None
    for level_depth in range(depth):
        following = []
        for account in level:
            resume_token = None
            while stopped is None:
                if len(hops) >= cap:
                    stopped = _stop(account, level_depth, f"the transfer cap of {cap} hops")
                    break
                if requests >= MAX_REQUESTS:
                    stopped = _stop(account, level_depth, f"the page budget of {MAX_REQUESTS} requests")
                    break
                body = dict(account_id=account, **filters)
                if resume_token is not None:
                    body["resume_token"] = resume_token
                page = _page(client, body)
                requests += 1
                fetched.append(client.last_source["fetched_at"])
                if not isinstance(page, dict) or not isinstance(page.get("transfers"), list):
                    raise rpc.MalformedResponse("the transfers page has no transfers list")
                for transfer in page["transfers"]:
                    if not str(transfer.get("amount", "")).startswith("-") or transfer["account_id"] != account:
                        continue
                    if len(hops) >= cap:
                        stopped = _stop(account, level_depth, f"the transfer cap of {cap} hops")
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
                if not resume_token or not page["transfers"]:
                    break
            if stopped is not None:
                break
        if stopped is not None:
            break
        level = following
    reads, read_times = state_at_block(subject, rpc_client, issuers)
    document = {"subject": subject, "assertion_ids": assertion_ids, "depth": depth, "filters": filters,
                "window": span, "hops": hops, "terminal_accounts": reached, "truncated": stopped is not None,
                "completeness": "complete" if stopped is None else stopped}
    document["depth_reached"] = reached_depth(document)
    document["shares"] = shares(subject, hops, terminal, named)
    document["state_at_block"] = reads
    times = [t for t in fetched + read_times if t]
    document["fetched_at"] = max(times) if times else None
    return document


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


def catalog_decimals(token):
    """The decimals the 1Click catalog snapshot gives the NEP-141 token."""
    data, _, _ = seed.snapshot("catalog", "replay")
    for asset in json.loads(data):
        if asset["assetId"] == f"nep141:{token}":
            return asset["decimals"]
    raise seed.SeedError(f"the catalog snapshot has no nep141:{token}")


def main(subject, depth, cap, name, record, out=print, store=None, flows_dir=FLOWS_DIR):
    """The flow command: 0 when the trace was written, 2 when it is refused."""
    store = store or labels.load()
    mode = "record" if record else "replay"
    client = rpc.Client(TRANSFERS_URL, mode, kind="http", fixtures_dir=FASTNEAR_DIR)
    rpc_client = rpc.Client(rpc.MAINNET_RPC_URL, mode)
    issuers = [dict(entry, decimals=catalog_decimals(entry["token"])) for entry in rules.issuers_for("mainnet")]
    try:
        document = trace(subject, store, client, rpc_client, incident_for(subject), issuers, depth, cap)
    except Refused as error:
        out(f"refused: {error}")
        return 2
    path = flow_path(subject, name, flows_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    out(f"{subject}: {summary(document)}")
    out(f"written to {path.name}")
    return 0
