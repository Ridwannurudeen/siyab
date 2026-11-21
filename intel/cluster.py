"""Account relationship edges from repeatable NEAR reads over incident labelled seeds."""

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from gate import rpc
from intel import labels

ROOT = Path(__file__).resolve().parent.parent
CLUSTERS_DIR = Path(__file__).resolve().parent / "clusters"
FIXTURES_DIR = ROOT / "fixtures" / "cluster"
INCIDENT_PATH = ROOT / "intel" / "incidents" / "rhea.json"
RPC_URL = "https://free.rpc.fastnear.com"
FASTNEAR_URL = "https://api.fastnear.com"
TRANSFERS_URL = "https://transfers.main.fastnear.com"
NEARBLOCKS_URL = "https://api.nearblocks.io"
COLLECTOR = json.loads(INCIDENT_PATH.read_text(encoding="utf-8"))["accounts"][0]["subject"]
FUNDER_EXCLUSIONS = {"near", "tg", "testnet", "intents.near", "team.herewallet.near", "relay.aurora"}
MAX_TRANSFER_PAGES = 20
IMPLICIT = re.compile(r"[0-9a-f]{64}|0x[0-9a-f]{40}")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
STANDARD = "Edges use recorded chain reads or name their inference inputs; depositors are counted and not listed."


class Refused(ValueError):
    """A seed has no current incident label."""


def incident_ids(store, subject):
    return [row.id for row in store.assertions
            if row.subject == subject and row.chain == "near" and row.category == "incident"
            and row.superseded_by is None]


def _validate_seeds(seeds, store, require_incident=True):
    if not seeds:
        raise ValueError("at least one seed is required")
    for seed in seeds:
        if require_incident and not incident_ids(store, seed):
            raise Refused("seed carries no incident label")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be distinct")


def _source(client, method, args, block):
    return {"rpc": client.url, "method": method, "args": args, "block": block}


def _view_access_key(client, account_id, public_key):
    result = client.call("query", {"request_type": "view_access_key", "finality": "final",
                                   "account_id": account_id, "public_key": public_key})
    if not isinstance(result, dict) or not isinstance(result.get("permission"), (dict, str)):
        raise rpc.MalformedResponse("the view_access_key result has no permission")
    if not isinstance(client.last_block_height, int):
        raise rpc.MalformedResponse("the view_access_key result has no block height")
    return result, client.last_block_height


def _is_full_access(key):
    return key.get("access_key", {}).get("permission") == "FullAccess"


def _public_key_accounts(response):
    accounts = response.get("account_ids", response.get("accounts", [])) if isinstance(response, dict) else []
    if not isinstance(accounts, list):
        raise rpc.MalformedResponse("the public key response has no accounts list")
    found = []
    for account in accounts:
        account_id = account if isinstance(account, str) else account.get("account_id") if isinstance(account, dict) else None
        if isinstance(account_id, str) and account_id not in found:
            found.append(account_id)
    return found


def _edge(rule, source_id, target_id, provenance, source, confidence=1.0, inputs=None):
    return {"rule": rule, "from": source_id, "to": target_id, "provenance": provenance,
            "source": source, "confidence": confidence, "inputs": inputs or []}


def _full_access_edges(seeds, rpc_client, fastnear_client):
    listed, fetched = {}, []
    for seed in seeds:
        response = rpc_client.view_access_key_list(seed)
        listed[seed] = [key for key in response["keys"] if _is_full_access(key)]
        fetched.append(rpc_client.last_source["fetched_at"])
    keys = {key["public_key"] for seed in seeds for key in listed[seed]}
    seeds_with_full_access = {seed for seed in seeds if listed[seed]}
    edges, confirmed = [], set()
    for public_key in sorted(keys):
        response = _retry_fastnear(lambda key=public_key: fastnear_client.get(f"/v0/public_key/{key}/all"),
                                   fastnear_client)
        fetched.append(fastnear_client.last_source["fetched_at"])
        reported = set(_public_key_accounts(response))
        possible = sorted(set(seeds).intersection(reported))
        for left, right in combinations(possible, 2):
            pair = (left, right)
            confirmations = []
            okay = True
            for account in pair:
                try:
                    result, block = _view_access_key(rpc_client, account, public_key)
                except rpc.RpcError as error:
                    if _unknown_access_key(error):
                        okay = False
                        break
                    raise
                if result["permission"] != "FullAccess":
                    okay = False
                    break
                fetched.append(rpc_client.last_source["fetched_at"])
                confirmations.append(_source(rpc_client, "view_access_key",
                                             {"account_id": account, "public_key": public_key}, block))
            if okay:
                confirmed.add(pair)
                edges.append(_edge("shares_full_access_key", left, right, "onchain",
                                   {"from": confirmations[0], "to": confirmations[1]}))
    eligible_pairs = len(list(combinations(seeds_with_full_access, 2)))
    return edges, len(confirmed), eligible_pairs, len(seeds_with_full_access), fetched


def _unknown_access_key(error):
    return error.cause == "UNKNOWN_ACCESS_KEY"


def _retry_fastnear(request, client):
    consecutive = 0
    while True:
        try:
            result = request()
        except rpc.HttpError as error:
            if error.status != 429:
                raise
            consecutive += 1
            if consecutive >= 3:
                raise
            client.sleep(10)
            continue
        return result


def _parent(account_id):
    parts = account_id.split(".")
    if len(parts) < 3:
        return None
    parent = ".".join(parts[1:])
    if parent in {"near", "tg", "testnet"}:
        return None
    return parent


def _sub_account_edges(seeds, rpc_client):
    edges, possible, fetched = [], 0, []
    for seed in seeds:
        parent = _parent(seed)
        if parent is None:
            continue
        possible += 1
        source = {}
        for account in (seed, parent):
            rpc_client.view_account(account)
            fetched.append(rpc_client.last_source["fetched_at"])
            source["from" if account == seed else "to"] = _source(
                rpc_client, "view_account", {"account_id": account}, rpc_client.last_block_height)
        edges.append(_edge("sub_account_of", seed, parent, "onchain", source))
    return edges, len(edges), possible, fetched


def _fact_id(fact):
    body = json.dumps(fact, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _creation_facts(seeds, nearblocks):
    facts, fetched = [], []
    for seed in seeds:
        response, _, _ = rpc.nearblocks_keys(nearblocks, seed)
        fetched.append(nearblocks.last_source["fetched_at"])
        keys = response.get("keys", []) if isinstance(response, dict) else []
        creations = [key["created"] for key in keys if isinstance(key, dict)
                     and isinstance(key.get("created"), dict)
                     and isinstance(key["created"].get("transaction_hash"), str)
                     and str(key["created"].get("block_timestamp", "")).isdigit()]
        if not creations:
            continue
        creation = min(creations, key=lambda row: int(row["block_timestamp"]))
        transaction_hash = creation["transaction_hash"]
        transaction_response, _, method = rpc.nearblocks_txn(nearblocks, transaction_hash)
        fetched.append(nearblocks.last_source["fetched_at"])
        transactions = transaction_response.get("txns", []) if isinstance(transaction_response, dict) else []
        if not transactions or not isinstance(transactions[0].get("signer_account_id"), str):
            continue
        transaction = transactions[0]
        funder = transaction["signer_account_id"]
        block = transaction.get("block", {}).get("block_height")
        if not isinstance(block, int):
            continue
        excluded = _excluded_funder(funder, FUNDER_EXCLUSIONS)
        fact = {"kind": "created_by_funder", "subject": seed, "value": funder, "provenance": "onchain",
                "source": {"provider": "api.nearblocks.io", "method": method,
                           "args": {"transaction_hash": transaction_hash}, "block": block},
                "confidence": 1.0, "excluded": excluded}
        if excluded:
            fact["note"] = f"created by {funder}, excluded as a wallet creator or relayer"
        fact["id"] = _fact_id(fact)
        facts.append(fact)
    return facts, fetched


def _excluded_funder(funder, exclusions):
    if funder in exclusions:
        return True
    return funder.endswith(".user.intear.near") or funder.startswith("user.") and funder.endswith(".intear.near")


def _funder_edges(facts):
    by_funder = {}
    for fact in facts:
        if not fact["excluded"]:
            by_funder.setdefault(fact["value"], []).append(fact)
    edges = []
    for funder, rows in by_funder.items():
        for left, right in combinations(sorted(rows, key=lambda row: row["subject"]), 2):
            edges.append(_edge("created_by_funder", left["subject"], right["subject"], "inferred",
                               {"funder": funder}, 0.5, [left["id"], right["id"]]))
    return edges


def deposit_pattern(collector, transfers_client, since, max_pages=MAX_TRANSFER_PAGES):
    since_ms = int(datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
    resume_token, depositors, pages, fetched = None, set(), 0, []
    while pages < max_pages:
        body = {"account_id": collector, "direction": "receiver", "from_timestamp_ms": since_ms, "limit": 100}
        if resume_token is not None:
            body["resume_token"] = resume_token
        response = transfers_client.post("/v0/transfers", body)
        pages += 1
        fetched.append(transfers_client.last_source["fetched_at"])
        if not isinstance(response, dict) or not isinstance(response.get("transfers"), list):
            raise rpc.MalformedResponse("the transfers response has no transfers list")
        for transfer in response["transfers"]:
            other = transfer.get("other_account_id")
            if isinstance(other, str):
                depositors.add(other)
        resume_token = response.get("resume_token")
        if not resume_token:
            break
    return {"collector": collector, "count": len(depositors),
            "implicit_count": sum(bool(IMPLICIT.fullmatch(account)) for account in depositors), "since": since}


def build_cluster(seeds, store, rpc_client, fastnear_client, nearblocks_client, transfers_client=None,
                  include_creation=True, since=None, require_incident=True):
    _validate_seeds(seeds, store, require_incident)
    fetched = []
    edges, full_confirmed, full_total, full_seed_count, times = _full_access_edges(
        seeds, rpc_client, fastnear_client)
    fetched.extend(times)
    sub_edges, sub_confirmed, sub_total, times = _sub_account_edges(seeds, rpc_client)
    edges.extend(sub_edges)
    fetched.extend(times)
    facts = []
    funder_edges = []
    if include_creation:
        facts, times = _creation_facts(seeds, nearblocks_client)
        fetched.extend(times)
        funder_edges = _funder_edges(facts)
        edges.extend(funder_edges)
    depositor_result = None
    if transfers_client is not None:
        collector = COLLECTOR if COLLECTOR in seeds else seeds[0]
        incident = json.loads(INCIDENT_PATH.read_text(encoding="utf-8"))
        since = since or incident["date"]
        depositor_result = deposit_pattern(collector, transfers_client, since)
        if transfers_client.last_source["fetched_at"]:
            fetched.append(transfers_client.last_source["fetched_at"])
    fetched_at = max((value for value in fetched if value), default=None)
    funder_seed_facts = sum(not fact["excluded"] and fact["value"] in seeds for fact in facts)
    excluded_facts = sum(fact["excluded"] for fact in facts)
    missing_creation_records = len(seeds) - len(facts)
    return {"seeds": list(seeds), "edges": edges, "facts": facts,
            "precision": {
                "shares_full_access_key": (
                    f"shares_full_access_key: {full_confirmed} of {full_total} pairs among the "
                    f"{full_seed_count} seeds that hold a full access key"),
                "sub_account_of": f"sub_account_of: {sub_confirmed} of {sub_total} seeds with a parent",
                "created_by_funder": (
                    f"created_by_funder: {funder_seed_facts} of {len(seeds)} seeds with a creation record "
                    f"signed by one seed account (NearBlocks facts), {excluded_facts} seed created through an "
                    f"excluded wallet creator, {missing_creation_records} seeds without a creation record; "
                    f"{len(funder_edges)} inferred pairs at confidence 0.5")},
            "depositors": depositor_result, "fetched_at": fetched_at, "standard": STANDARD}


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _incident_seeds(store):
    incident = json.loads(INCIDENT_PATH.read_text(encoding="utf-8"))
    return [row["subject"] for row in incident["accounts"]
            if row["chain"] == "near" and incident_ids(store, row["subject"])]


def _client_set(mode):
    return (rpc.Client(RPC_URL, mode, fixtures_dir=FIXTURES_DIR),
            rpc.Client(FASTNEAR_URL, mode, kind="http", fixtures_dir=FIXTURES_DIR),
            rpc.Client(NEARBLOCKS_URL, mode, kind="http", fixtures_dir=FIXTURES_DIR),
            rpc.Client(TRANSFERS_URL, mode, kind="http", fixtures_dir=FIXTURES_DIR))


def rebuild(name="rhea", record=False, subject=None, store=None, out=print, output_dir=CLUSTERS_DIR, seeds=None):
    store = store or labels.load()
    seeds = [subject] if subject else seeds or _incident_seeds(store)
    mode = "off" if subject else "record" if record else "replay"
    rpc_client, fastnear_client, nearblocks_client, transfers_client = _client_set(mode)
    document = build_cluster(seeds, store, rpc_client, fastnear_client, nearblocks_client,
                             transfers_client if subject is None else None,
                             require_incident=subject is None)
    if subject is None:
        _write(Path(output_dir) / f"{name}.json", document)
        out(f"written to {name}.json")
        for line in document["precision"].values():
            out(line)
        if document["depositors"] is not None:
            out(f"deposit_pattern: {document['depositors']['count']} depositors, "
                f"{document['depositors']['implicit_count']} implicit accounts since "
                f"{document['depositors']['since']}")
    else:
        out(json.dumps({"edges": document["edges"], "facts": document["facts"]}, indent=2, ensure_ascii=False))
    return document


def main(argv=None, output_dir=CLUSTERS_DIR, out=print):
    parser = argparse.ArgumentParser(prog="python -m intel.cluster")
    parser.add_argument("seeds", nargs="*")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--name", default="rhea")
    parser.add_argument("--subject")
    args = parser.parse_args(argv)
    if args.subject and args.seeds:
        parser.error("--subject cannot be combined with seed arguments")
    try:
        rebuild(args.name, args.record, args.subject,
                store=labels.load(), out=out, output_dir=output_dir,
                seeds=args.seeds or None)
    except Refused as error:
        out(str(error))
        return 2
    except (rpc.FixtureMiss, rpc.HttpError, rpc.TransportError, rpc.RpcError) as error:
        out(f"stopped: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
