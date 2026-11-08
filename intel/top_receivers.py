"""Keyless, resumable mainnet receiver scan over finalized neardata blocks."""

import hashlib
import json
import math
import os
import re
import ssl
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from gate import rpc, rules
from intel import feed

ROOT = Path(__file__).resolve().parent.parent
RANKING_PATH = ROOT / "fixtures" / "receivers" / "top.json"
DEFAULT_DATA_DIR = Path.home() / ".cache" / "shieldbot-near" / "top-receivers"
BATCH_SIZE = 1000
RANK_LIMIT = 1000
# neardata answers /v0/block/<height> with a redirect, so each block costs two requests against its published limit
# of 180 a minute per IP for keyless callers; 80 blocks a minute (160 requests) stays under it. Measured on
# 7 Nov 2026: at 170 blocks a minute every request answered 429.
# A probe on 7 Nov 2026, after a 15 minute cooldown, still met 429 on 47 of 90 keyless requests at 30 blocks a minute
# and on all 150 at about 50, so a keyless scan runs at 15 blocks a minute. neardata does not throttle keyed requests,
# so with FASTNEAR_API_KEY_FILE set the scan uses the follower's keyed spacing.
REQUEST_INTERVAL_S = 60 / 15
KEYED_REQUEST_INTERVAL_S = feed.FOLLOW_INTERVAL_KEYED_S
# Bound a persistently throttled height while allowing brief 429 bursts to clear.
MAX_CONSECUTIVE_429S = 5
# A scan of about 150,000 requests meets network failures: a dropped or reset connection, a socket timeout, an
# unreachable host or DNS failure, or a 5xx answer. Each is retried after these pauses, in seconds; a local OSError is
# not, and a refused TLS certificate stops the scan at once.
TRANSIENT_BACKOFF_S = (5, 10, 20, 40, 60, 60)
# A block takes one to two seconds to download, so one request at a time cannot reach the request rate the gate
# allows; this many workers share the one gate, which still spaces request starts REQUEST_INTERVAL_S apart.
SCAN_WORKERS = 6
AVERAGE_BLOCK_NS = 578_160_000
INTENTS_ACCOUNT = "intents.near"
_TIME = "%Y-%m-%dT%H:%M:%SZ"


class TopReceiversError(ValueError):
    """A scan window or reduced block does not meet the recorded format."""


def _data_dir(path):
    path = Path(path).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise TopReceiversError("the scan data directory must be outside the repository")
    return path


def _header(block, expected_height=None):
    if not isinstance(block, dict) or not isinstance(block.get("block"), dict):
        raise TopReceiversError("the neardata block has no block object")
    header = block["block"].get("header")
    if not isinstance(header, dict):
        raise TopReceiversError("the neardata block has no header")
    height = header.get("height")
    timestamp_ns = header.get("timestamp_nanosec")
    if type(height) is not int or height < 0 or isinstance(timestamp_ns, bool):
        raise TopReceiversError("the neardata header has no non-negative height or timestamp")
    if type(timestamp_ns) is not int and not (isinstance(timestamp_ns, str) and timestamp_ns.isdecimal()):
        raise TopReceiversError("the neardata header timestamp is not an integer")
    try:
        timestamp_ns = int(timestamp_ns)
    except (TypeError, ValueError) as error:
        raise TopReceiversError("the neardata header timestamp is not an integer") from error
    if timestamp_ns < 0:
        raise TopReceiversError("the neardata header timestamp is negative")
    if expected_height is not None and height != expected_height:
        raise TopReceiversError(f"neardata returned height {height} for requested height {expected_height}")
    return height, timestamp_ns


def _amount(value, description):
    if type(value) is not int and not (isinstance(value, str) and value.isdecimal()):
        raise TopReceiversError(f"{description} is not an integer amount")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise TopReceiversError(f"{description} is not an integer amount") from error
    if result < 0:
        raise TopReceiversError(f"{description} is negative")
    return result


def _event_transfers(logs, receiver_id, height):
    transfers, intents = [], []
    if not isinstance(logs, list):
        raise TopReceiversError(f"receipt logs at height {height} are not a list")
    for log in logs:
        if not isinstance(log, str) or not log.startswith("EVENT_JSON:"):
            continue
        try:
            event = json.loads(log[len("EVENT_JSON:"):])
        except json.JSONDecodeError as error:
            raise TopReceiversError(f"malformed EVENT_JSON log at height {height}") from error
        if not isinstance(event, dict):
            raise TopReceiversError(f"EVENT_JSON log at height {height} is not an object")
        standard, name = event.get("standard"), event.get("event")
        if standard == "nep141" and name == "ft_transfer":
            data = event.get("data")
            if not isinstance(data, list):
                raise TopReceiversError(f"nep141 ft_transfer data at height {height} is not a list")
            for item in data:
                if not isinstance(item, dict):
                    raise TopReceiversError(f"nep141 ft_transfer item at height {height} is not an object")
                payer, recipient = item.get("old_owner_id"), item.get("new_owner_id")
                if not isinstance(payer, str) or not isinstance(recipient, str):
                    raise TopReceiversError(f"nep141 ft_transfer owners at height {height} are missing")
                amount = _amount(item.get("amount"), f"nep141 ft_transfer amount at height {height}")
                if amount > 0 and payer != recipient:
                    transfers.append({"payer": payer, "receiver": recipient, "asset": receiver_id,
                                      "amount": str(amount)})
        elif receiver_id == INTENTS_ACCOUNT and standard == "nep245" and name == "mt_transfer":
            data = event.get("data")
            if not isinstance(data, list):
                raise TopReceiversError(f"nep245 mt_transfer data at height {height} is not a list")
            for item in data:
                if not isinstance(item, dict):
                    raise TopReceiversError(f"nep245 mt_transfer item at height {height} is not an object")
                payer, recipient = item.get("old_owner_id"), item.get("new_owner_id")
                token_ids, amounts = item.get("token_ids"), item.get("amounts")
                if (not isinstance(payer, str) or not isinstance(recipient, str)
                        or not isinstance(token_ids, list) or not isinstance(amounts, list)
                        or len(token_ids) != len(amounts)
                        or not all(isinstance(token_id, str) for token_id in token_ids)):
                    raise TopReceiversError(f"nep245 mt_transfer fields at height {height} are invalid")
                if payer == recipient:
                    continue
                for token_id, value in zip(token_ids, amounts):
                    amount = _amount(value, f"nep245 mt_transfer amount at height {height}")
                    if amount > 0:
                        intents.append({"payer": payer, "receiver": recipient, "asset": token_id,
                                        "amount": str(amount)})
    return transfers, intents


def reduce_block(block):
    """Keep successful native and NEP-141 transfers, with NEP-245 Intents transfers separate."""
    height, timestamp_ns = _header(block)
    shards = block.get("shards")
    if not isinstance(shards, list):
        raise TopReceiversError(f"neardata block {height} has no shards list")
    transfers, intents = [], []
    for shard in shards:
        if not isinstance(shard, dict):
            raise TopReceiversError(f"neardata shard at height {height} is not an object")
        for executed in shard.get("receipt_execution_outcomes") or []:
            if not isinstance(executed, dict):
                raise TopReceiversError(f"receipt outcome at height {height} is not an object")
            receipt = executed.get("receipt")
            execution = executed.get("execution_outcome")
            outcome = execution.get("outcome") if isinstance(execution, dict) else None
            if not isinstance(receipt, dict) or not isinstance(outcome, dict):
                raise TopReceiversError(f"receipt outcome at height {height} is incomplete")
            status = outcome.get("status")
            if not isinstance(status, dict):
                raise TopReceiversError(f"receipt status at height {height} is missing")
            if "Failure" in status:
                continue
            body = receipt.get("receipt")
            action_receipt = body.get("Action") if isinstance(body, dict) else None
            predecessor = receipt.get("predecessor_id")
            receiver_id = receipt.get("receiver_id")
            if not isinstance(predecessor, str) or not isinstance(receiver_id, str):
                raise TopReceiversError(f"receipt owners at height {height} are missing")
            if isinstance(action_receipt, dict):
                for action in action_receipt.get("actions") or []:
                    transfer = action.get("Transfer") if isinstance(action, dict) else None
                    if predecessor == "system" or not isinstance(transfer, dict):
                        continue
                    amount = _amount(transfer.get("deposit"), f"native transfer deposit at height {height}")
                    if amount > 0 and predecessor != receiver_id:
                        transfers.append({"payer": predecessor, "receiver": receiver_id,
                                          "asset": "native:near", "amount": str(amount)})
            receipt_transfers, receipt_intents = _event_transfers(outcome.get("logs") or [], receiver_id, height)
            transfers.extend(receipt_transfers)
            intents.extend(receipt_intents)
    return {"height": height, "timestamp_ns": str(timestamp_ns), "transfers": transfers, "intents": intents}


def _timestamp_ns(block, expected_height):
    if block is None:
        return None
    return _header(block, expected_height)[1]


def bisect_start_height(end_height, end_timestamp_ns, target_timestamp_ns, fetch_block):
    """Return the first non-null height at or after the target timestamp, plus its timestamp."""
    if target_timestamp_ns >= end_timestamp_ns:
        raise TopReceiversError("the window start must precede its end")
    cache = {}

    def read(height):
        if height not in cache:
            cache[height] = fetch_block(height)
        return cache[height]

    duration_ns = end_timestamp_ns - target_timestamp_ns
    step = max(1, math.ceil(duration_ns / AVERAGE_BLOCK_NS))
    probe = max(0, end_height - step)
    high_height, high_timestamp = end_height, end_timestamp_ns
    while True:
        candidate = probe
        raw = read(candidate)
        while raw is None and candidate > 0:
            candidate -= 1
            raw = read(candidate)
        if raw is None:
            raise TopReceiversError("no block timestamp was found before the window start")
        timestamp_ns = _timestamp_ns(raw, candidate)
        if timestamp_ns < target_timestamp_ns:
            low_height, low_timestamp = candidate, timestamp_ns
            break
        if candidate == 0:
            return candidate, timestamp_ns
        high_height, high_timestamp = candidate, timestamp_ns
        step *= 2
        probe = max(0, end_height - step)

    while high_height - low_height > 1:
        middle = (low_height + high_height) // 2
        raw = read(middle)
        if raw is not None:
            timestamp_ns = _timestamp_ns(raw, middle)
            if timestamp_ns >= target_timestamp_ns:
                high_height, high_timestamp = middle, timestamp_ns
            else:
                low_height, low_timestamp = middle, timestamp_ns
            continue

        left_height = middle - 1
        left_timestamp = low_timestamp
        while left_height > low_height:
            left = read(left_height)
            if left is not None:
                left_timestamp = _timestamp_ns(left, left_height)
                break
            left_height -= 1
        right_height = middle + 1
        right_timestamp = high_timestamp
        while right_height < high_height:
            right = read(right_height)
            if right is not None:
                right_timestamp = _timestamp_ns(right, right_height)
                break
            right_height += 1
        if left_timestamp >= target_timestamp_ns:
            high_height, high_timestamp = left_height, left_timestamp
        elif right_timestamp < target_timestamp_ns:
            low_height, low_timestamp = right_height, right_timestamp
        elif right_height == high_height:
            return high_height, high_timestamp
        else:
            high_height, high_timestamp = right_height, right_timestamp
    return high_height, high_timestamp


def _neardata_fetcher():
    """The neardata fetcher and request interval for a scan: keyed (an Authorization Bearer header read from the file
    FASTNEAR_API_KEY_FILE names, the key never printed) when that variable is set, keyless otherwise. A set variable
    whose file cannot be read or is empty stops the scan rather than falling back to keyless."""
    key_file = os.environ.get("FASTNEAR_API_KEY_FILE")
    if not key_file:
        return feed.Fetcher(max_requests=None, max_consecutive_429s=MAX_CONSECUTIVE_429S), REQUEST_INTERVAL_S
    try:
        api_key = Path(key_file).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise TopReceiversError("the file FASTNEAR_API_KEY_FILE names cannot be read") from error
    if not api_key:
        raise TopReceiversError("the file FASTNEAR_API_KEY_FILE names is empty")
    client = rpc.Client(feed.NEARDATA_URL, "off", kind="http", headers={"Authorization": f"Bearer {api_key}"})
    return feed.Fetcher(client, max_requests=None, max_consecutive_429s=MAX_CONSECUTIVE_429S), KEYED_REQUEST_INTERVAL_S


def _block_reader(fetcher, sleep, clock, last_request_at=None, interval=REQUEST_INTERVAL_S):
    """A block reader that spaces requests, reads a 404 as a skipped height, and retries a network failure (a dropped
    connection or other rpc.TransportError, a reset connection raised as ConnectionError, a socket timeout, or an
    unreachable host or DNS failure reported as urllib.error.URLError) or a 5xx answer after each pause of
    TRANSIENT_BACKOFF_S, re-raising after the last attempt. A refused TLS certificate, or any URLError whose reason is
    not an OSError, stops the scan at once with no pause, as do a run of MAX_CONSECUTIVE_429S consecutive 429 answers,
    any other HTTP error, and any other OSError such as a local file error."""
    last_request = [last_request_at]
    lock = threading.Lock()

    def wait_turn():
        # Each request start is booked REQUEST_INTERVAL_S after the previous booked start, under one lock, so the
        # workers together never exceed the gate's rate.
        with lock:
            now = clock()
            start = now if last_request[0] is None else max(now, last_request[0] + interval)
            last_request[0] = start
        if start > now:
            sleep(start - now)

    def get(path):
        for attempt in range(len(TRANSIENT_BACKOFF_S) + 1):
            wait_turn()
            try:
                return fetcher.get(path)
            except rpc.HttpError as error:
                if error.status == 404:
                    return None
                if error.status < 500 or attempt == len(TRANSIENT_BACKOFF_S):
                    raise
            except (rpc.TransportError, TimeoutError, ConnectionError, urllib.error.URLError) as error:
                # urllib.error.HTTPError is a URLError but never arrives here: rpc._fetch turns it into rpc.HttpError.
                # A reset arrives as a raw ConnectionError from the response read, outside urlopen's URLError wrapper.
                if isinstance(error, urllib.error.URLError) and (
                        not isinstance(error.reason, OSError) or isinstance(error.reason, ssl.SSLError)):
                    # As in intel.alerts._retryable: a refused TLS certificate, or a reason that is no transport
                    # failure, will not clear on a retry.
                    raise
                if attempt == len(TRANSIENT_BACKOFF_S):
                    raise
            sleep(TRANSIENT_BACKOFF_S[attempt])

    return get, lambda: last_request[0], lambda value: last_request.__setitem__(0, value)


def _checkpoint_path(data_dir):
    return data_dir / "checkpoint.json"


def _batch_path(data_dir, start_height, end_height):
    return data_dir / "batches" / f"{start_height}-{end_height}.json"


def _read_checkpoint(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TopReceiversError(f"cannot read scan checkpoint {path}") from error
    if not isinstance(state, dict):
        raise TopReceiversError(f"scan checkpoint {path} is not an object")
    return state


def _validate_checkpoint(state, hours, to_height):
    if state.get("version") != 1 or state.get("hours") != hours or state.get("batch_size") != BATCH_SIZE:
        raise TopReceiversError("the checkpoint uses a different scan window or batch size; choose another data directory")
    if to_height is not None and state.get("end_height") != to_height:
        raise TopReceiversError("the requested end height differs from the pinned checkpoint; choose another data directory")
    required = ("start_height", "end_height", "start_timestamp_ns", "end_timestamp_ns", "last_height",
                "heights_fetched", "skipped_heights", "request_count")
    if any(key not in state for key in required) or not isinstance(state["skipped_heights"], list):
        raise TopReceiversError("the scan checkpoint is incomplete")


def _new_checkpoint(hours, start_height, end_height, start_timestamp_ns, end_timestamp_ns, requests,
                    last_request_at):
    return {"version": 1, "hours": hours, "batch_size": BATCH_SIZE, "start_height": start_height,
            "end_height": end_height, "start_timestamp_ns": str(start_timestamp_ns),
            "end_timestamp_ns": str(end_timestamp_ns), "last_height": start_height - 1,
            "heights_fetched": 0, "skipped_heights": [], "request_count": requests,
            "last_request_at": last_request_at}


def _load_batch(path, start_height, end_height):
    try:
        batch = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TopReceiversError(f"cannot read reduced batch {path}") from error
    if (not isinstance(batch, dict) or batch.get("start_height") != start_height
            or batch.get("end_height") != end_height
            or not isinstance(batch.get("heights"), list)
            or len(batch["heights"]) != end_height - start_height + 1):
        raise TopReceiversError(f"reduced batch {path.name} does not match its height range")
    for offset, reduction in enumerate(batch["heights"]):
        if not isinstance(reduction, dict) or reduction.get("height") != start_height + offset:
            raise TopReceiversError(f"reduced batch {path.name} has a height gap")
    return batch


def _batch_counts(batch):
    skipped = [entry["height"] for entry in batch["heights"] if entry.get("skipped")]
    return len(batch["heights"]) - len(skipped), skipped


def scan_window(hours, to_height=None, record=False, data_dir=DEFAULT_DATA_DIR, fetcher=None, sleep=time.sleep,
                clock=time.time):
    """Record the pinned window in reduced batches, or load a completed scan without network access."""
    if type(hours) is not int or hours <= 0:
        raise TopReceiversError("hours must be a positive integer")
    if to_height is not None and (type(to_height) is not int or to_height < 0):
        raise TopReceiversError("to-height must be a non-negative integer")
    data_dir = _data_dir(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = _checkpoint_path(data_dir)
    interval = REQUEST_INTERVAL_S
    if checkpoint_path.is_file():
        state = _read_checkpoint(checkpoint_path)
        _validate_checkpoint(state, hours, to_height)
    else:
        if not record:
            raise TopReceiversError(f"no checkpoint under {data_dir}; run with --record")
        if fetcher is None:
            fetcher, interval = _neardata_fetcher()
        before_pin = fetcher.requests
        get, last_request_at, _ = _block_reader(fetcher, sleep, clock, interval=interval)
        end_path = "/v0/last_block/final" if to_height is None else f"/v0/block/{to_height}"
        final = get(end_path)
        if final is None:
            raise TopReceiversError("neardata returned no block for the pinned window end")
        end_height, end_timestamp_ns = _header(final, to_height)
        target_timestamp_ns = end_timestamp_ns - hours * 60 * 60 * 1_000_000_000
        start_height, start_timestamp_ns = bisect_start_height(
            end_height, end_timestamp_ns, target_timestamp_ns,
            lambda height: get(f"/v0/block/{height}"))
        state = _new_checkpoint(hours, start_height, end_height, start_timestamp_ns, end_timestamp_ns,
                                fetcher.requests - before_pin, last_request_at())
        feed._write_json(checkpoint_path, state, indent=2)
    if state["last_height"] >= state["end_height"]:
        return state
    if not record:
        raise TopReceiversError(f"scan stops at height {state['last_height']}; resume with --record")
    if fetcher is None:
        fetcher, interval = _neardata_fetcher()
    get, last_request_at, set_last_request_at = _block_reader(fetcher, sleep, clock, clock(), interval=interval)
    while state["last_height"] < state["end_height"]:
        batch_start = state["last_height"] + 1
        batch_end = min(batch_start + BATCH_SIZE - 1, state["end_height"])
        path = _batch_path(data_dir, batch_start, batch_end)
        if path.is_file():
            batch = _load_batch(path, batch_start, batch_end)
            batch_requests = batch.get("requests", 0)
            last_at = batch.get("last_request_at", state.get("last_request_at"))
            set_last_request_at(last_at)
        else:
            before_batch = fetcher.requests
            def fetch(height):
                raw = get(f"/v0/block/{height}")
                if raw is None:
                    return {"height": height, "skipped": True}
                reduction = reduce_block(raw)
                if reduction["height"] != height:
                    raise TopReceiversError(f"neardata returned height {reduction['height']} for {height}")
                return reduction

            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
                heights = list(pool.map(fetch, range(batch_start, batch_end + 1)))
            batch_requests = fetcher.requests - before_batch
            last_at = last_request_at()
            batch = {"start_height": batch_start, "end_height": batch_end, "heights": heights,
                     "requests": batch_requests, "last_request_at": last_at}
            feed._write_json(path, batch, indent=2)
        fetched, skipped = _batch_counts(batch)
        state["last_height"] = batch_end
        state["heights_fetched"] += fetched
        state["skipped_heights"].extend(skipped)
        state["request_count"] += batch_requests
        state["last_request_at"] = last_at
        feed._write_json(checkpoint_path, state, indent=2)
    return state


def _new_stats():
    return {"payers": set(), "transfer_count": 0, "asset_totals": {}}


def _add_transfer(groups, transfer):
    row = groups.setdefault(transfer["receiver"], _new_stats())
    row["payers"].add(transfer["payer"])
    row["transfer_count"] += 1
    asset_totals = row["asset_totals"]
    asset_totals[transfer["asset"]] = asset_totals.get(transfer["asset"], 0) + int(transfer["amount"])


def _ranked_rows(groups):
    rows = []
    for subject, stats in groups.items():
        rows.append({"subject": subject, "distinct_payers": len(stats["payers"]),
                     "transfer_count": stats["transfer_count"],
                     "asset_totals": {asset: str(amount) for asset, amount in sorted(stats["asset_totals"].items())}})
    return sorted(rows, key=lambda row: (-row["distinct_payers"], -row["transfer_count"], row["subject"]))


def _iso_timestamp(timestamp_ns):
    seconds, nanos = divmod(int(timestamp_ns), 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


def build_ranking(data_dir, state, rule, catalog_accounts, manual_exclusions, exclusion_rule, age_limit,
                  now_ms=None):
    """Aggregate the completed reduced scan, keeping NEP-245 Intents transfers in a separate cohort."""
    if state["last_height"] != state["end_height"]:
        raise TopReceiversError("the scan checkpoint is incomplete")
    data_dir = _data_dir(data_dir)
    groups, intents_groups = {}, {}
    digest = hashlib.sha256()
    skipped_heights, heights_fetched = [], 0
    batch_start = state["start_height"]
    while batch_start <= state["end_height"]:
        batch_end = min(batch_start + BATCH_SIZE - 1, state["end_height"])
        batch = _load_batch(_batch_path(data_dir, batch_start, batch_end), batch_start, batch_end)
        for reduction in batch["heights"]:
            encoded = json.dumps(reduction, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest.update(encoded.encode("utf-8") + b"\n")
            if reduction.get("skipped"):
                skipped_heights.append(reduction["height"])
                continue
            heights_fetched += 1
            for transfer in reduction.get("transfers", []):
                _add_transfer(groups, transfer)
            for transfer in reduction.get("intents", []):
                _add_transfer(intents_groups, transfer)
        batch_start = batch_end + 1

    catalog_accounts = set(catalog_accounts)
    implicit_excluded = {subject for subject in groups if re.fullmatch(
        r"[0-9a-f]{64}|0x[0-9a-f]{40}", subject) and subject not in catalog_accounts}
    eligible = [row for row in _ranked_rows(groups) if row["subject"] not in implicit_excluded]
    selected = eligible[:RANK_LIMIT]
    manual_exclusions = set(manual_exclusions)
    excluded_selected = [row for row in selected if row["subject"] in manual_exclusions]
    subjects = []
    for candidate_rank, row in enumerate(selected, start=1):
        if row["subject"] in manual_exclusions:
            continue
        index = len(subjects)
        subject = {**row, "rank": candidate_rank, "as": "receiver", "age_recorded": index < age_limit,
                   "request": {"kind": "near", "token_id": None, "receiver_id": row["subject"],
                               "amount": str(rules.YOCTO_PER_NEAR), "memo": None}}
        subjects.append(subject)

    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return {"kind": "near_top_receivers", "chosen_at": datetime.fromtimestamp(now_ms / 1000, UTC).strftime(_TIME),
            "judged_at_ms": now_ms, "window": {
                "hours": state["hours"], "start_height": state["start_height"], "end_height": state["end_height"],
                "start_timestamp_ns": state["start_timestamp_ns"], "end_timestamp_ns": state["end_timestamp_ns"],
                "start_timestamp": _iso_timestamp(state["start_timestamp_ns"]),
                "end_timestamp": _iso_timestamp(state["end_timestamp_ns"])},
            "rule": rule, "heights_fetched": heights_fetched, "skipped_heights": skipped_heights,
            "request_count": state["request_count"], "reduction_sha256": digest.hexdigest(),
            "exclusions": {"implicit_accounts_excluded": len(implicit_excluded),
                           "implicit_account_rule": "excluded unless the label store holds a current catalog_token row",
                           "named_account_rule": exclusion_rule,
                           "named_accounts_in_set": len(manual_exclusions),
                           "named_accounts_excluded_after_limit": len(excluded_selected),
                           "named_account_ids_published": False},
            "subjects": subjects,
            "intents_cohort": {"contract": INTENTS_ACCOUNT, "standard": "nep245", "event": "mt_transfer",
                               "transfer_count": sum(row["transfer_count"] for row in intents_groups.values()),
                               "receivers": _ranked_rows(intents_groups)}}
