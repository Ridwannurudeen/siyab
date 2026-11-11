"""Issuer blacklist status and history rows built from recorded NEAR calls.

The record command records issuer status reads. The history command records transaction details
and the account observations used for history rows. Transaction fixtures drop every signature
field at record time and change no other response field; each transaction fixture carries the
reduction note.
"""

import argparse
import base64
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from gate import rpc
from intel import labels, seed

ROOT = Path(__file__).resolve().parent.parent
ISSUERS_PATH = ROOT / "intel" / "issuers.json"
FIXTURE_DIR = ROOT / "fixtures" / "issuers"
DATA_DIR = ROOT / "intel" / "issuers"
STATUS_PATH = DATA_DIR / "status.jsonl"
HISTORY_PATH = DATA_DIR / "history.jsonl"
READS_PATH = DATA_DIR / "reads.json"
RECEIVERS_PATH = ROOT / "fixtures" / "receivers" / "set.json"
RPC_URL = rpc.MAINNET_RPC_URL
FASTNEAR_TX_URL = "https://tx.main.fastnear.com"
TRANSACTION_REDUCED = "every signature field dropped at record time; nothing else changed"
STATUS_CATEGORIES = ("incident", "denylist", "watch", "treasury", "infrastructure", "bridge")
RPC_INTERVAL_S = 1
BACKOFF_S = 10
TX_BACKOFF_S = 10
TX_MAX_CONSECUTIVE_429 = 3
LONG_BACKOFF_S = 120
MAX_CONSECUTIVE_429 = 5
HISTORY_METHODS = {
    "add_to_blacklist": "added to the issuer blacklist of",
    "remove_from_blacklist": "removed from the issuer blacklist of",
    "destroy_black_funds": "black funds destroyed by the issuer of",
}
HISTORY_PAGE_SIZE = 200
TRANSACTION_BATCH_SIZE = 20


def issuer_entries():
    return json.loads(ISSUERS_PATH.read_text(encoding="utf-8"))["mainnet"]


def _drop_signatures(value):
    if isinstance(value, dict):
        return {key: _drop_signatures(item) for key, item in value.items() if key != "signature"}
    if isinstance(value, list):
        return [_drop_signatures(item) for item in value]
    return value


def record_transaction_request(path, body, fixtures_dir=FIXTURE_DIR, sleep=time.sleep, progress=print):
    client = rpc.Client(FASTNEAR_TX_URL, "off", kind="http", fixtures_dir=fixtures_dir)
    consecutive_429 = 0
    while True:
        try:
            response = client.post(path, body)
            break
        except rpc.HttpError as error:
            if error.status != 429:
                raise
            consecutive_429 += 1
            progress(f"Transactions API POST {path}: HTTP 429, consecutive {consecutive_429}")
            if consecutive_429 >= TX_MAX_CONSECUTIVE_429:
                raise
            sleep(TX_BACKOFF_S)
    url = FASTNEAR_TX_URL + path
    params = rpc.body_params(body)
    key = rpc.fixture_key(url, "POST", params)
    fixture = {"request": {"method": "POST", "params": params}, "response": _drop_signatures(response),
               "url": url, "fetched_at": client.last_source["fetched_at"], "reduced": TRANSACTION_REDUCED}
    fixture_path = Path(fixtures_dir) / f"{key}.json"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_bytes((json.dumps(fixture, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    progress(f"Transactions API POST {path}: HTTP 200, recorded {key[:12]}")
    return fixture["response"]


def _transaction_fixture_path(body, fixtures_dir):
    url = FASTNEAR_TX_URL + "/v0/transactions"
    key = rpc.fixture_key(url, "POST", rpc.body_params(body))
    return Path(fixtures_dir) / f"{key}.json"


def _history_page(body, client, sleep=time.sleep, progress=print):
    consecutive_429 = 0
    while True:
        try:
            return client.post("/v0/account", body)
        except rpc.HttpError as error:
            if error.status != 429:
                raise
            consecutive_429 += 1
            progress(f"Transactions API POST /v0/account: HTTP 429, consecutive {consecutive_429}")
            if consecutive_429 >= TX_MAX_CONSECUTIVE_429:
                raise
            sleep(TX_BACKOFF_S)


def record_history(sleep=time.sleep, progress=print):
    page_client = rpc.Client(FASTNEAR_TX_URL, "record", kind="http", fixtures_dir=FIXTURE_DIR)
    owner = _owner(FIXTURE_DIR)
    hashes = []
    resume_token = None
    while True:
        body = {"account_id": owner, "is_receiver": True, "is_function_call": True,
                "limit": HISTORY_PAGE_SIZE, "desc": True}
        if resume_token is not None:
            body["resume_token"] = resume_token
        page = _history_page(body, page_client, sleep=sleep, progress=progress)
        hashes.extend(row["transaction_hash"] for row in page["account_txs"])
        resume_token = page.get("resume_token")
        if not resume_token:
            break

    detail_count = 0
    for start in range(0, len(hashes), TRANSACTION_BATCH_SIZE):
        body = {"tx_hashes": hashes[start:start + TRANSACTION_BATCH_SIZE]}
        fixture_path = _transaction_fixture_path(body, FIXTURE_DIR)
        if fixture_path.exists():
            detail = json.loads(fixture_path.read_text(encoding="utf-8"))["response"]
        else:
            detail = record_transaction_request("/v0/transactions", body, fixtures_dir=FIXTURE_DIR,
                                                sleep=sleep, progress=progress)
        detail_count += len(detail["transactions"])

    all_subjects = _history_subjects(FIXTURE_DIR)
    observations = _account_observations(FIXTURE_DIR)
    missing_subjects = [subject for subject in all_subjects if subject not in observations]
    if missing_subjects:
        record_history_accounts(missing_subjects, sleep=sleep, progress=progress)
    row_count = write_history_rows()
    progress(f"history scan: {len(hashes)} page transactions, {detail_count} transaction details, "
             f"{row_count} history rows")
    return len(hashes), detail_count, row_count


def _history_subjects(fixtures_dir):
    owner = _owner(fixtures_dir)
    subjects_found = []
    seen = set()
    for path in Path(fixtures_dir).glob("*.json"):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if (fixture.get("request", {}).get("method") != "POST"
                or fixture.get("reduced") != TRANSACTION_REDUCED):
            continue
        for transaction in fixture["response"].get("transactions", []):
            for outer in transaction.get("receipts", []):
                receipt = outer.get("receipt", {})
                status = outer.get("execution_outcome", {}).get("outcome", {}).get("status")
                if (receipt.get("receiver_id") != "usdt.tether-token.near"
                        or receipt.get("predecessor_id") != owner or status != {"SuccessValue": ""}):
                    continue
                actions = receipt.get("receipt", {}).get("Action", {}).get("actions", [])
                for action in actions:
                    call = action.get("FunctionCall") if isinstance(action, dict) else None
                    if call and call.get("method_name") in HISTORY_METHODS:
                        subject = json.loads(base64.b64decode(call["args"]))["account_id"]
                        if subject not in seen:
                            seen.add(subject)
                            subjects_found.append(subject)
    return subjects_found


def record_history_accounts(subjects_to_record=None, sleep=time.sleep, progress=print):
    block_client = rpc.Client(RPC_URL, "off", fixtures_dir=FIXTURE_DIR, sleep=sleep)
    block = block_client.final_block()["header"]["height"]
    client = rpc.Client(RPC_URL, "record", fixtures_dir=FIXTURE_DIR, sleep=sleep)
    subjects_to_read = (_history_subjects(FIXTURE_DIR) if subjects_to_record is None
                        else list(dict.fromkeys(subjects_to_record)))
    observations = _account_observations(FIXTURE_DIR)
    subjects_to_read = [subject for subject in subjects_to_read if subject not in observations]
    for index, subject in enumerate(subjects_to_read):
        if index:
            sleep(RPC_INTERVAL_S)
        try:
            client.call("query", {"request_type": "view_account", "block_id": block,
                                   "account_id": subject})
            progress(f"view_account recorded at block {block}: {json.dumps(subject)} exists")
        except rpc.RpcError as error:
            progress(f"view_account recorded at block {block}: {json.dumps(subject)} {error.cause}")
    return block, len(subjects_to_read)


def _owner(fixtures_dir):
    for path in Path(fixtures_dir).glob("*.json"):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        params = fixture.get("request", {}).get("params", {})
        if params.get("request_type") == "call_function" and params.get("method_name") == "owner":
            response = fixture["response"]["result"]
            return json.loads(bytes(response["result"]))
    raise FileNotFoundError("the recorded token owner view is missing")


def _account_observations(fixtures_dir):
    observations = {}
    for path in Path(fixtures_dir).glob("*.json"):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        params = fixture.get("request", {}).get("params", {})
        if params.get("request_type") != "view_account" or "block_id" not in params:
            continue
        response = fixture["response"]
        error = response.get("error")
        if error:
            observations[params["account_id"]] = {
                "block": params["block_id"], "exists": False,
                "cause": error.get("cause", {}).get("name", "UNKNOWN"),
            }
        else:
            observations[params["account_id"]] = {
                "block": response["result"]["block_height"], "exists": True, "cause": None,
            }
    return observations


def history_rows(fixtures_dir=FIXTURE_DIR):
    owner = _owner(fixtures_dir)
    observations = _account_observations(fixtures_dir)
    rows = []
    seen = set()
    for path in sorted(Path(fixtures_dir).glob("*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        request = fixture.get("request", {})
        if (request.get("method") != "POST" or not request.get("params", {}).get("body_sha256")
                or fixture.get("reduced") != TRANSACTION_REDUCED):
            continue
        for transaction in fixture["response"].get("transactions", []):
            tx_hash = transaction.get("transaction", {}).get("hash")
            for outer in transaction.get("receipts", []):
                receipt = outer.get("receipt", {})
                status = outer.get("execution_outcome", {}).get("outcome", {}).get("status")
                if (receipt.get("receiver_id") != "usdt.tether-token.near"
                        or receipt.get("predecessor_id") != owner or status != {"SuccessValue": ""}):
                    continue
                actions = receipt.get("receipt", {}).get("Action", {}).get("actions", [])
                for index, action in enumerate(actions):
                    call = action.get("FunctionCall") if isinstance(action, dict) else None
                    if not call or call.get("method_name") not in HISTORY_METHODS:
                        continue
                    args = json.loads(base64.b64decode(call["args"]))
                    subject = args["account_id"]
                    key = (tx_hash, receipt["receipt_id"], index)
                    if key in seen:
                        continue
                    seen.add(key)
                    observation = observations[subject]
                    account_note = (f'a string the issuer passed as an account id, '
                                    f'{json.dumps(subject, ensure_ascii=False)}; it does not exist on chain: '
                                    f'view_account returned {observation["cause"]} at block '
                                    f'{observation["block"]}; nothing further is inferred.'
                                    if not observation["exists"] else
                                    f'view_account returned an account record at block {observation["block"]}.')
                    method = call["method_name"]
                    block = receipt["block_height"]
                    token = receipt["receiver_id"]
                    rows.append(seed.make_row(
                        entity=f"{token} issuer action", subject=subject, chain="near",
                        label=(f'{HISTORY_METHODS[method]} {token} in transaction {tx_hash} '
                               f'at block {block}'), category="watch", provenance="onchain",
                        source={"rpc": FASTNEAR_TX_URL, "method": "POST /v0/transactions",
                                "args": {"tx_hashes": [tx_hash]}, "block": block},
                        observed_at=fixture["fetched_at"],
                        note=(f'The issuer action method {method} names '
                              f'{account_note}' if not observation["exists"] else
                              f'The issuer action method {method} names the account id '
                              f'{json.dumps(subject, ensure_ascii=False)}. {account_note}')))
    return sorted(rows, key=lambda row: row["source"]["block"], reverse=True)


def history_rows_jsonl():
    return "".join(labels.row_line(row) + "\n" for row in history_rows())


def write_history_rows(rows_to_write=None, path=None):
    rows_to_write = history_rows() if rows_to_write is None else rows_to_write
    path = Path(HISTORY_PATH if path is None else path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(labels.row_line(row) + "\n" for row in rows_to_write),
                    encoding="utf-8", newline="\n")
    return len(rows_to_write)


def history_coverage(fixtures_dir=FIXTURE_DIR):
    fixtures_dir = Path(fixtures_dir)
    pages = []
    details = []
    for path in fixtures_dir.glob("*.json"):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        if fixture.get("request", {}).get("method") != "POST":
            continue
        response = fixture.get("response", {})
        if "account_txs" in response:
            pages.append(response)
        elif fixture.get("reduced") == TRANSACTION_REDUCED and "transactions" in response:
            details.extend(response["transactions"])
    owner = _owner(fixtures_dir)
    page_rows = [row for page in pages for row in page["account_txs"]]
    blocks = [row["tx_block_height"] for row in page_rows]
    action_counts = {"add_to_blacklist": 0, "destroy_black_funds": 0,
                     "remove_from_blacklist": 0}
    for transaction in details:
        for outer in transaction.get("receipts", []):
            receipt = outer.get("receipt", {})
            status = outer.get("execution_outcome", {}).get("outcome", {}).get("status")
            if (receipt.get("receiver_id") != "usdt.tether-token.near"
                    or receipt.get("predecessor_id") != owner
                    or status != {"SuccessValue": ""}):
                continue
            actions = receipt.get("receipt", {}).get("Action", {}).get("actions", [])
            for action in actions:
                call = action.get("FunctionCall") if isinstance(action, dict) else None
                if call and call.get("method_name") in action_counts:
                    action_counts[call["method_name"]] += 1
    return {"transactions": len(details), "txs_count": pages[0]["txs_count"],
            "tx_block_min": min(blocks), "tx_block_max": max(blocks), "actions": action_counts}


def subjects(store=None):
    store = store or labels.load()
    result = []
    seen = set()
    for assertion in store.assertions:
        if (assertion.chain == "near" and assertion.superseded_by is None
                and assertion.category in STATUS_CATEGORIES and assertion.subject not in seen):
            seen.add(assertion.subject)
            result.append(assertion.subject)
    for item in json.loads(RECEIVERS_PATH.read_text(encoding="utf-8"))["subjects"]:
        subject = item["subject"]
        if subject not in seen:
            seen.add(subject)
            result.append(subject)
    return result


def _query_params(token, method, args):
    encoded = json.dumps(args, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"request_type": "call_function", "finality": "final", "account_id": token,
            "method_name": method, "args_base64": base64.b64encode(encoded).decode("ascii")}


def _fixture_is_fresh(token, method, args, fixture_dir):
    params = _query_params(token, method, args)
    key = rpc.fixture_key(RPC_URL, "query", params)
    path = Path(fixture_dir) / f"{key}.json"
    if not path.exists():
        return False
    fetched = json.loads(path.read_text(encoding="utf-8"))["fetched_at"]
    fetched_at = datetime.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (datetime.now(UTC) - fetched_at).total_seconds() < rpc.MAX_AGE_S


def read_status(mode="replay", store=None, fixture_dir=FIXTURE_DIR, sleep=time.sleep, clock=time.monotonic,
                progress=print):
    client = rpc.Client(RPC_URL, mode, fixtures_dir=fixture_dir)
    observed = []
    total_requests = 0
    total_429 = 0
    consecutive_429 = 0
    long_backoff_used = False
    last_request_started = None
    started = clock()
    pairs = [(subject, issuer) for subject in subjects(store) for issuer in issuer_entries()]
    try:
        for index, (subject, issuer) in enumerate(pairs, start=1):
            args = {issuer["args_key"]: subject}
            while True:
                network_request = mode == "off" or (mode == "record" and not _fixture_is_fresh(
                    issuer["token"], issuer["method"], args, fixture_dir))
                if network_request:
                    if last_request_started is not None:
                        delay = RPC_INTERVAL_S - (clock() - last_request_started)
                        if delay > 0:
                            sleep(delay)
                    last_request_started = clock()
                    total_requests += 1
                try:
                    answer = client.call_function(issuer["token"], issuer["method"], args)
                except rpc.HttpError as error:
                    if error.status != 429:
                        raise
                    total_429 += 1
                    consecutive_429 += 1
                    progress(f"429 #{total_429}, consecutive {consecutive_429}, status read {index}, "
                             f"method {issuer['method']}")
                    if consecutive_429 >= MAX_CONSECUTIVE_429:
                        if long_backoff_used:
                            raise
                        sleep(LONG_BACKOFF_S)
                        long_backoff_used = True
                        consecutive_429 = 0
                    else:
                        sleep(BACKOFF_S)
                    continue
                consecutive_429 = 0
                read = {"issuer": issuer["token"], "subject": subject, "method": issuer["method"],
                        "args": args, "answer": answer, "block": client.last_block_height,
                        "observed_at": client.last_source["fetched_at"]}
                observed.append(read)
                if mode == "record":
                    write_reads(observed, READS_PATH)
                break
    finally:
        elapsed = clock() - started
        if mode == "record":
            progress(f"recording summary: requests={total_requests}, 429s={total_429}, "
                     f"wall_seconds={elapsed:.1f}, status_reads={len(observed)}")
    return observed


def build_rows(reads):
    entries = {entry["token"]: entry for entry in issuer_entries()}
    rows = []
    for read in reads:
        issuer = entries[read["issuer"]]
        if type(read["answer"]) is not type(issuer["banned"]) or read["answer"] != issuer["banned"]:
            continue
        answer = json.dumps(read["answer"], ensure_ascii=False)
        token = read["issuer"]
        rows.append(seed.make_row(
            entity=f"{token} issuer action",
            subject=read["subject"],
            chain="near",
            label=(f'banned by the issuer of {token}: {read["method"]} answers {answer} '
                   f'at block {read["block"]}'),
            category="watch",
            provenance="onchain",
            source={"rpc": RPC_URL, "method": read["method"], "args": read["args"],
                    "block": read["block"]},
            observed_at=read["observed_at"],
            note=f'{token} answered {answer} for this account id at block {read["block"]}',
        ))
    return rows


def reads():
    return json.loads(READS_PATH.read_text(encoding="utf-8"))


def rows():
    return build_rows(read_status())


def _verified_status_read(read, fixture_dir):
    entries = {entry["token"]: entry for entry in issuer_entries()}
    entry = entries.get(read.get("issuer"))
    if (entry is None or read.get("method") != entry["method"]
            or read.get("args") != {entry["args_key"]: read.get("subject")}):
        return None
    client = rpc.Client(RPC_URL, "replay", fixtures_dir=fixture_dir)
    try:
        answer = client.call_function(read["issuer"], read["method"], read["args"])
    except rpc.FixtureMiss:
        return None
    if (type(answer) is not type(read.get("answer")) or answer != read.get("answer")
            or client.last_block_height != read.get("block")
            or client.last_source["fetched_at"] != read.get("observed_at")):
        raise seed.SeedError("issuer status cache does not match its recorded view fixture")
    return {"answer": answer, "rpc": client.url, "method": read["method"], "args": read["args"],
            "block": client.last_block_height, "observed_at": client.last_source["fetched_at"]}


def blacklist_rows(reads_to_seed=None, fixture_dir=FIXTURE_DIR, store_path=labels.STORE_PATH):
    reads_to_seed = reads() if reads_to_seed is None else reads_to_seed
    entries = {entry["token"]: entry for entry in issuer_entries()}
    store_path = Path(store_path)
    catalog_subjects = ({assertion.subject for assertion in labels.load(store_path).assertions
                         if assertion.category == "catalog_token" and assertion.superseded_by is None}
                        if store_path.is_file() else set())
    rows = []
    for read in reads_to_seed:
        entry = entries.get(read.get("issuer"))
        if (entry is None or type(read.get("answer")) is not type(entry["banned"])
                or read.get("answer") != entry["banned"]):
            continue
        if read.get("subject") == entry["token"] or read.get("subject") in catalog_subjects:
            continue
        observed = _verified_status_read(read, fixture_dir)
        if observed is None:
            continue
        token, subject, answer, block = read["issuer"], read["subject"], observed["answer"], observed["block"]
        rows.append(seed.make_row(
            entity=f"issuer of {token}", subject=subject, chain="near",
            label=f"The issuer of {token} blacklists {subject} as of block {block}", category="denylist",
            provenance="onchain",
            source={"rpc": observed["rpc"], "method": observed["method"], "args": observed["args"],
                    "block": block},
            observed_at=observed["observed_at"],
            note=f"The view returned {json.dumps(answer, ensure_ascii=False)} for this account at block {block}"))
    return rows


def _blacklist_key(assertion, entries):
    if (assertion.category != "denylist" or assertion.provenance != "onchain"
            or not assertion.entity.startswith("issuer of ")):
        return None
    token = assertion.entity.removeprefix("issuer of ")
    return (token, assertion.subject) if token in entries else None


def seed_blacklists(store_path=labels.STORE_PATH, fixture_dir=FIXTURE_DIR, reads_path=READS_PATH):
    store_path = Path(store_path)
    reads_to_seed = json.loads(Path(reads_path).read_text(encoding="utf-8"))
    entries = {entry["token"]: entry for entry in issuer_entries()}
    catalog_store_path = store_path if store_path.is_file() else labels.STORE_PATH
    current_rows = blacklist_rows(reads_to_seed, fixture_dir, catalog_store_path)
    current_by_key = {(row["entity"].removeprefix("issuer of "), row["subject"]): row for row in current_rows}
    read_by_key = {(read.get("issuer"), read.get("subject")): read for read in reads_to_seed}
    existing_store = labels.load(store_path) if store_path.is_file() else None
    existing_assertions = [] if existing_store is None else existing_store.assertions
    raw_rows = ([json.loads(line) for line in store_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                if store_path.is_file() else [])
    existing_by_id = {assertion.id: assertion for assertion in existing_assertions}
    existing_ids = set(existing_by_id)
    additions = [row for row in current_rows if row["id"] not in existing_ids]
    superseded = {}
    stale = []
    for assertion in existing_assertions:
        key = _blacklist_key(assertion, entries)
        if key is None or assertion.superseded_by is not None:
            continue
        replacement = current_by_key.get(key)
        if replacement is not None:
            if replacement["id"] != assertion.id:
                superseded[assertion.id] = replacement["id"]
            continue
        read = read_by_key.get(key)
        entry = entries[key[0]]
        if (read is None or "clear" not in entry or type(read.get("answer")) is not type(entry["clear"])
                or read.get("answer") != entry["clear"]):
            continue
        if _verified_status_read(read, fixture_dir) is not None:
            stale.append((assertion, key))

    if stale:
        removals = {}
        for row in history_rows(fixture_dir):
            token = row["entity"].removesuffix(" issuer action")
            key = (token, row["subject"])
            previous = existing_by_id.get(row["id"])
            if (token in entries and "remove_from_blacklist" in row["note"]
                    and (previous is None or previous.superseded_by is None)):
                removals.setdefault(key, row)
        for assertion, key in stale:
            removal = removals.get(key)
            if removal is None:
                raise seed.SeedError(f"no recorded remove_from_blacklist action for {key[1]}")
            superseded[assertion.id] = removal["id"]
            if removal["id"] not in existing_ids:
                additions.append(removal)
                existing_ids.add(removal["id"])

    if not additions and not superseded:
        return 0
    for row in raw_rows:
        replacement = superseded.get(row["id"])
        if replacement is not None:
            row["superseded_by"] = replacement
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text("".join(labels.row_line(row) + "\n" for row in raw_rows + additions),
                          encoding="utf-8", newline="\n")
    labels.load(store_path)
    return len(additions)


def rows_jsonl():
    return "".join(labels.row_line(row) + "\n" for row in rows())


def write_rows(rows_to_write=None, path=None):
    rows_to_write = rows() if rows_to_write is None else rows_to_write
    path = Path(STATUS_PATH if path is None else path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(labels.row_line(row) + "\n" for row in rows_to_write)
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8", newline="\n")
    return len(rows_to_write)


def write_reads(reads_to_write, path=READS_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reads_to_write, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")


def record(sleep=time.sleep, clock=time.monotonic, progress=print):
    observed = read_status("record", sleep=sleep, clock=clock, progress=progress)
    write_reads(observed)
    count = write_rows(build_rows(observed))
    return len(observed), count


def append_rows():
    """Append the history rows first: a status row exists only for a subject the store already holds."""
    added = seed.append(history_rows())
    return added + seed.append(rows())


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m intel.issuers")
    parser.add_argument("command", choices=("record", "rows", "append", "history", "seed"))
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            reads_count, row_count = record()
            print(f"{reads_count} issuer status reads, {row_count} watch rows")
        elif args.command == "rows":
            status_count = write_rows()
            history_count = write_history_rows()
            print(f"{status_count} issuer status rows, {history_count} issuer history rows")
        elif args.command == "append":
            print(f"{append_rows()} issuer rows appended")
        elif args.command == "seed":
            print(f"{seed_blacklists()} issuer assertions appended")
        else:
            record_history()
    except (rpc.FixtureMiss, rpc.HttpError, rpc.TransportError, rpc.RpcError, rpc.MalformedResponse) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
