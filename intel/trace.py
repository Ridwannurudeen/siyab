"""A bounded exit trace from an account that carries an incident label, and only from one.

python -m intel flow <subject> [--depth 3] [--max 15000] [--name <file name>] [--record] refuses at
once, with the reason "subject carries no incident label", unless intel/labels.jsonl holds an
incident assertion on the subject, and with "no incident page names the subject" unless a page of
intel/incidents/ lists it among its accounts. Otherwise it reads the transfers the subject sent from
https://transfers.main.fastnear.com/v0/transfers with the body {"account_id", "direction": "sender",
"ignore_system": true, "from_timestamp_ms": <window start>, "limit": 100} and the resume_token of
the previous page, in the order the API returns them, and no server side USD filter (it would drop
the rows that carry no usd_amount). The window starts at 00:00 UTC on the day before the incident
page's date; older outbound rows are outside it. The walk descends breadth first into each receiver
that is not a terminal account, to the depth bound and at most the transfer cap in all, reading at
most MAX_REQUESTS requests, each attempt counted, a retry included; a 429 is retried after BACKOFF_S
seconds and the third 429 in a row stops the run. The command prints the request count and the 429
count after the summary; neither is written to the trace. A terminal account is a NEAR account
the label store labels catalog_token, bridge or infrastructure (intents.near, zec.omft.near, the
bridge token accounts), or the subject itself when the walk comes back to it: its hop is kept and
the trace goes no further.

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
from functools import cache
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
DEFAULT_MAX = MAX_REQUESTS * PAGE_LIMIT
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


def usd_text(text):
    """A USD sum to the cent, with thousands separators."""
    return f"{Decimal(text).quantize(Decimal('0.01')):,f} USD"


def _hops_away(steps):
    return f"{steps} hop{'' if steps == 1 else 's'} from the subject"


def stop_state(completeness):
    """complete, stopped while reading the subject, or stopped while reading an account S hops from it."""
    if completeness == "complete":
        return "complete"
    if completeness["depth"] == 0:
        return "stopped while reading the subject"
    return f"stopped while reading an account {_hops_away(completeness['depth'])}"


def _stop_text(completeness):
    if completeness == "complete":
        return "complete"
    if completeness["depth"] == 0:
        return f"stopped while reading the subject: {completeness['reason']}"
    return (f"stopped while reading {completeness['account']} ({_hops_away(completeness['depth'])}): "
            f"{completeness['reason']}")


def utc_minute(block_timestamp):
    """A block timestamp in nanoseconds as a UTC time to the minute."""
    return datetime.fromtimestamp(block_timestamp // 1000000000, UTC).strftime("%Y-%m-%d %H:%M UTC")


def read_extent(document):
    """Whether the subject's outbound rows in the window were read in full, else to which time."""
    completeness = document["completeness"]
    if completeness == "complete" or completeness["depth"] > 0:
        return ", read in full"
    last = [h["block_timestamp"] for h in document["hops"] if h["from"] == document["subject"]][-1]
    return f" read in part, to {utc_minute(last)}"


def _state_text(reads):
    parts = []
    for balance, status in zip(reads[::2], reads[1::2]):
        answer = json.dumps(status["result"], ensure_ascii=False)
        said = (f"banned by the issuer, {status['method']} answers {answer}" if status["banned"]
                else f"issuer status {status['method']} answers {answer}")
        parts.append(f"{human(balance['result'], balance['decimals'])} of {balance['token']} at block "
                     f"{balance['block']}, {said} at block {status['block']}")
    return "; ".join(parts)


FOLD_PERCENT = 0.5
FOLD_WORDS = {"terminal": ("more terminal account", "more terminal accounts"),
              "named in the report": ("more account the report names", "more accounts the report names")}


def share_items(shares):
    """The summary's shares: each bucket at or above FOLD_PERCENT with its percent (and the not named
    bucket always), then the smaller buckets folded into one item per kind with their USD sum."""
    listed, folded = [], {}
    for bucket in shares["buckets"]:
        if bucket["kind"] in FOLD_WORDS and bucket["percent"] < FOLD_PERCENT:
            folded.setdefault(bucket["kind"], []).append(Decimal(bucket["usd"]))
        else:
            listed.append(f"{bucket['to']} {bucket['percent']} percent")
    for kind, sums in folded.items():
        words = FOLD_WORDS[kind][0 if len(sums) == 1 else 1]
        listed.append(f"{len(sums)} {words}, {usd_text(sum(sums))} together")
    return listed


def gross_sentence(document):
    """What the gross figure counts more than once, computed from the hops."""
    subject, hops = document["subject"], document["hops"]
    terminal = {b["to"] for b in document["shares"]["buckets"] if b["kind"] == "terminal"}
    direct = [h for h in hops if h["from"] == subject and h["usd_amount"] is not None and h["usd_amount"] >= MIN_USD]
    into = [h for h in direct if h["method_name"] in ("ft_transfer_call", "near_deposit") and h["to"] in terminal]
    inside = [h for h in direct if h["asset_id"].startswith("nep245:")]
    back = [h for h in hops if h["to"] == subject]
    read = {h["from"] for h in hops} - {subject}
    contracts = ", ".join(sorted({h["to"] for h in into})) or "none"

    def total(rows):
        return usd_text(sum((Decimal(str(h["usd_amount"] or 0)) for h in rows), Decimal(0)))

    return (f"Gross means the same value can appear in more than one row: {total(into)} of these rows are "
            f"ft_transfer_call or near_deposit into a terminal contract ({contracts}), {total(inside)} are moves of "
            "balances held inside intents.near (asset ids under nep245:intents.near:), and "
            f"{total(back)} came back to the subject in {len(back)} row{'' if len(back) == 1 else 's'} of the "
            f"{len(read)} account{'' if len(read) == 1 else 's'} read; inbound "
            "rows of the subject were not read, so no net figure is stated, and the incident page states no loss "
            "figure.")


def summary(document):
    """One line on how far the walk went, how the subject's gross outbound transfer value splits by
    where the rows end, what that gross figure counts more than once, and the subject's state at a
    block."""
    accounts = {document["subject"]} | {h["from"] for h in document["hops"]} | {h["to"] for h in document["hops"]}
    shares = document["shares"]
    ends = ", ".join(share_items(shares)) or "none"
    return (f"{len(document['hops'])} hop{'' if len(document['hops']) == 1 else 's'} over "
            f"{len(accounts)} account{'' if len(accounts) == 1 else 's'}; depth {document['depth_reached']} of "
            f"{document['depth']}, {_stop_text(document['completeness'])}; gross outbound transfer value after "
            f"{document['window']['from'][:10]} 00:00 UTC{read_extent(document)}: "
            f"{usd_text(shares['direct_outbound_usd'])} across {shares['rows']} row{'' if shares['rows'] == 1 else 's'} "
            f"({shares['without_usd']['count']} row{'' if shares['without_usd']['count'] == 1 else 's'} without a USD "
            f"amount, {shares['below_min_usd']['count']} below {usd_text(MIN_USD)}), each row counted "
            f"once as the Transfers API values it; shares of the gross outbound transfer value: {ends}; "
            f"{gross_sentence(document)[:-1]}; subject: "
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
                if type(result) is type(entry["banned"]) and result == entry["banned"]:
                    read["banned"] = True
                elif "clear" in entry and type(result) is type(entry["clear"]) and result == entry["clear"]:
                    read["banned"] = False
                else:
                    read["banned"] = None
            reads.append(read)
            fetched.append(rpc_client.last_source["fetched_at"])
    return reads, fetched


def _page(client, body, tally):
    """One transfers page, or None when the request budget is used up first. Every attempt counts in
    tally["requests"], a retry included, and the budget is checked before each; a 429 counts in
    tally["answered_429"], waits BACKOFF_S and retries, and the third in a row is raised."""
    in_a_row = 0
    while tally["requests"] < MAX_REQUESTS:
        tally["requests"] += 1
        try:
            return client.post("/v0/transfers", body)
        except rpc.HttpError as error:
            if error.status != 429:
                raise
            tally["answered_429"] += 1
            in_a_row += 1
            if in_a_row >= MAX_429_IN_A_ROW:
                raise
            client.sleep(BACKOFF_S)
    return None


def _stop(account, level_depth, reason):
    return {"account": account, "depth": level_depth, "reason": reason}


def trace(subject, store, client, rpc_client, incident, issuers, depth=DEFAULT_DEPTH, cap=DEFAULT_MAX, tally=None):
    """The trace document; raises Refused for a subject without an incident label or page. The
    transfers requests made and the 429 answers among them are counted into tally (a dict, when
    given), never into the document."""
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
    tally = {} if tally is None else tally
    tally.update(requests=0, answered_429=0)
    level, stopped = [subject], None
    for level_depth in range(depth):
        following = []
        for account in level:
            resume_token = None
            while stopped is None:
                if len(hops) >= cap:
                    stopped = _stop(account, level_depth, f"the transfer cap of {cap} hops")
                    break
                body = dict(account_id=account, **filters)
                if resume_token is not None:
                    body["resume_token"] = resume_token
                page = _page(client, body, tally)
                if page is None:
                    stopped = _stop(account, level_depth, f"the page budget of {MAX_REQUESTS} requests")
                    break
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


@cache
def asset_decimals(asset_id):
    if asset_id == "native:near":
        return 24
    asset_id = asset_id.removeprefix("nep245:intents.near:")
    if not asset_id.startswith("nep141:"):
        return None
    try:
        return catalog_decimals(asset_id.removeprefix("nep141:"))
    except seed.SeedError:
        return None


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
    tally = {}
    try:
        document = trace(subject, store, client, rpc_client, incident_for(subject), issuers, depth, cap, tally)
    except Refused as error:
        out(f"refused: {error}")
        return 2
    path = flow_path(subject, name, flows_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    out(f"{subject}: {summary(document)}")
    out(f"written to {path.name}")
    out(f"{tally['requests']} requests, {tally['answered_429']} answered 429 and retried after {BACKOFF_S} s")
    return 0
