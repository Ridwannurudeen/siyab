"""A block feed over finalized NEAR mainnet blocks, with three signals.

python -m intel feed [--from <height>] --blocks <n> [--record] walks n block heights from --from
(default: the height after the state's last block; with --record and no state, the height
https://mainnet.neardata.xyz/v0/last_block/final answers). With --record each block is fetched
from https://mainnet.neardata.xyz/v0/block/<height> (keyless, a 30 s timeout, a 10 s back off on a
429, at most MAX_REQUESTS requests a run), reduced and written as fixtures/feed/blocks/<height>.json;
the raw block is never written. Without --record the reduced fixtures are read and nothing
touches the network. A height neardata answers with null holds no block: its fixture says so and
the state lists it as skipped.

python -m intel feed --follow [--from <height>] [--max-blocks <n>] reads the final height from
/v0/last_block/final and walks every height from the state's last height + 1 (with no state:
--from, else the final height) to it in order, never skipping one; once caught up it sleeps 1 s
and reads the final height again. It stops on SIGINT or after --max-blocks heights, a skipped
height included. It has no request budget: it leaves FOLLOW_INTERVAL_S (0.4 s) between one request
and the next, so at most 150 a minute, and backs off 10 s on a 429 as above. No reduced block is
written per height: a block that raised an event is written as
<state dir>/event-blocks/<height>.json, the block_fixture its events' source names. A finalized
height neardata answers with null or a 404 holds no block, as in record mode: the follower adds
it to skipped_heights, writes the state and goes on to the next height. NEAR skips a height
whenever its producer misses the slot, so skipped_heights can grow by a few hundred heights a
day; a list that size is fine for the weeks this hackathon build runs. The state is written after
every height; on SIGINT the loop stops after the height in hand and the command exits 0.

The reduced form keeps, for each executed action receipt, its receipt id, transaction hash,
predecessor, receiver, whether it succeeded, and the actions the signals read: DeployContract
(the base58 sha256 of the code, never the code), CreateAccount, Transfer (the deposit),
FunctionCall (the method name and deposit, never the args) and AddKey (the public key and the
permission). The state directory is the one FEED_STATE_DIR names (default intel/feed/, the
recorded window), so a follower on a host writes no tracked file. Its state.json holds the start
height and time, the last height and time, the block count, the skipped heights, the fresh
accounts with their funding deposit and time, and the event count per signal; a follower adds
mode ("follow"), started_at (when the first follow run began), lag_blocks (the final height at the
last read minus the last height), requests (the neardata requests made on request_day) and
request_day (the UTC date the requests count is for, checked before every request; a new date
resets the count). A block at or below the last height is never counted twice, so a run that stops
can be started again.

The signals, each written as an evidence document under <state dir>/events/<sha256>.json:

  code_hash_matches_incident       a DeployContract whose code hash is the code hash an incident page
                                   reports (intel/incidents/*.json, code_hash_as_reported)
  fresh_dust_funded_pays_labelled  an account created with a deposit below 0.1 NEAR that, within 24 h,
                                   sends a FunctionCall or a Transfer to a NEAR account with a current
                                   incident or denylist row
  watch_account_key_added          an AddKey with full access, or a function call key with an empty
                                   method_names list, on a NEAR account labelled infrastructure,
                                   treasury or watch
"""

import base64
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from gate import evidence, rpc, rules, wasm
from intel import labels

ROOT = Path(__file__).resolve().parent.parent
NEARDATA_URL = "https://mainnet.neardata.xyz"
BLOCKS_DIR = ROOT / "fixtures" / "feed" / "blocks"
FEED_DIR = Path(os.environ.get("FEED_STATE_DIR", Path(__file__).resolve().parent / "feed"))
STATE_PATH = FEED_DIR / "state.json"
EVENTS_DIR = FEED_DIR / "events"
EVENT_BLOCKS_DIR = FEED_DIR / "event-blocks"
INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"
SNAPSHOT_PATH = Path(__file__).resolve().parent / "feed" / "follow-snapshot.json"
SIGNALS = ("code_hash_matches_incident", "fresh_dust_funded_pays_labelled", "watch_account_key_added")
WATCH_CATEGORIES = ("infrastructure", "treasury", "watch")
FRESH_WINDOW_MS = 24 * 60 * 60 * 1000
MAX_REQUESTS = 600
BACKOFF_S = 10
FOLLOW_INTERVAL_S = 0.4
_TIME = "%Y-%m-%dT%H:%M:%SZ"


class FeedError(Exception):
    """A feed run that cannot go on: a missing fixture, the request budget or a bad answer."""


def code_hash(code_base64):
    """The base58 sha256 of the contract code, as view_account reports code_hash."""
    return wasm.codehash(base64.b64decode(code_base64))[1]


def _permission(access_key):
    permission = access_key.get("permission") if isinstance(access_key, dict) else None
    if permission == "FullAccess":
        return "FullAccess"
    call = permission.get("FunctionCall") if isinstance(permission, dict) else None
    if isinstance(call, dict):
        return {"receiver_id": call.get("receiver_id"), "method_names": list(call.get("method_names") or [])}
    return {"other": json.dumps(permission, sort_keys=True)}


def _action(action):
    """The reduced form of one receipt action, or None for an action no signal reads."""
    if action == "CreateAccount":
        return {"type": "CreateAccount"}
    if not isinstance(action, dict) or len(action) != 1:
        return None
    [(name, body)] = action.items()
    if name == "DeployContract":
        return {"type": "DeployContract", "code_hash": code_hash(body["code"])}
    if name == "CreateAccount":
        return {"type": "CreateAccount"}
    if name == "Transfer":
        return {"type": "Transfer", "deposit": str(body["deposit"])}
    if name == "FunctionCall":
        return {"type": "FunctionCall", "method_name": body["method_name"], "deposit": str(body["deposit"])}
    if name == "AddKey":
        return {"type": "AddKey", "public_key": body["public_key"], "permission": _permission(body["access_key"])}
    return None


def reduce_block(block):
    """The reduced form of a neardata block: no code, no args, no signatures, no logs."""
    header = block["block"]["header"]
    receipts = []
    for shard in block["shards"]:
        for executed in shard.get("receipt_execution_outcomes") or []:
            receipt = executed["receipt"]
            body = receipt["receipt"].get("Action") if isinstance(receipt["receipt"], dict) else None
            if not isinstance(body, dict):
                continue
            actions = [reduced for reduced in map(_action, body.get("actions") or []) if reduced is not None]
            if not actions:
                continue
            status = executed["execution_outcome"]["outcome"]["status"]
            receipts.append({"receipt_id": receipt["receipt_id"], "tx_hash": executed.get("tx_hash"),
                             "predecessor_id": receipt["predecessor_id"], "receiver_id": receipt["receiver_id"],
                             "ok": not (isinstance(status, dict) and "Failure" in status), "actions": actions})
    return {"height": header["height"], "hash": header["hash"], "timestamp_ns": str(header["timestamp_nanosec"]),
            "receipts": receipts}


def _write_json(path, value, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=indent, ensure_ascii=False, separators=None if indent else (",", ":"))
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes((text + "\n").encode("utf-8"))
    os.replace(temporary, path)


def new_state():
    return {"start_height": None, "start_time": None, "last_height": None, "last_time": None, "blocks": 0,
            "skipped_heights": [], "fresh_accounts": {}, "events": {name: 0 for name in SIGNALS}}


def load_state(path=STATE_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else new_state()


class Context:
    """What the signals compare with: the incident code hashes, the labelled and the watched NEAR
    accounts with their assertion ids."""

    def __init__(self, store, incidents_dir=INCIDENTS_DIR):
        self.code_hashes = {}
        for path in sorted(Path(incidents_dir).glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            reported = document.get("code_hash_as_reported")
            if reported:
                self.code_hashes[reported["value"]] = document["id"]
        self.labelled, self.watched, self.watch_categories, self.deny_receivers = {}, {}, {}, set()
        for assertion in store.assertions:
            if assertion.chain != "near" or assertion.superseded_by is not None:
                continue
            self.labelled.setdefault(assertion.subject, []).append(assertion.id)
            if assertion.category in ("incident", "denylist"):
                self.deny_receivers.add(assertion.subject)
            if assertion.category in WATCH_CATEGORIES:
                self.watched.setdefault(assertion.subject, []).append(assertion.id)
                categories = self.watch_categories.setdefault(assertion.subject, [])
                if assertion.category not in categories:
                    categories.append(assertion.category)
        self.store_sha256 = store.sha256


def _near_text(yocto):
    return format((Decimal(yocto) / rules.YOCTO_PER_NEAR).normalize(), "f")


def _event(signal, subject, detail, block, receipt, labels_used, source):
    return {"version": "1", "kind": "feed_event", "signal": signal, "subject": subject, "detail": detail,
            "labels": labels_used, "receipt_id": receipt["receipt_id"], "tx_hash": receipt["tx_hash"],
            "observed": {"block_height": block["height"], "timestamp_ms": int(block["timestamp_ns"]) // 1_000_000},
            "source": source}


def signals(block, state, context, source):
    """The events one reduced block raises; updates the fresh accounts of the state."""
    events = []
    now_ms = int(block["timestamp_ns"]) // 1_000_000
    fresh = state["fresh_accounts"]
    for account in [a for a, info in fresh.items() if now_ms - info["created_ms"] > FRESH_WINDOW_MS]:
        del fresh[account]
    for receipt in block["receipts"]:
        if not receipt["ok"]:
            continue
        types = [action["type"] for action in receipt["actions"]]
        receiver, predecessor = receipt["receiver_id"], receipt["predecessor_id"]
        for action in receipt["actions"]:
            if action["type"] == "DeployContract" and action["code_hash"] in context.code_hashes:
                incident_id = context.code_hashes[action["code_hash"]]
                events.append(_event(SIGNALS[0], receiver, f"{receiver} deployed code hash {action['code_hash']}, the "
                                     f"code hash the incident page {incident_id} reports", block, receipt, [], source))
            if action["type"] == "AddKey" and receiver in context.watched:
                permission = action["permission"]
                if permission == "FullAccess" or (isinstance(permission, dict) and "method_names" in permission
                                                  and not permission["method_names"]):
                    kind = ("a full access key" if permission == "FullAccess"
                            else f"a function call key for {permission['receiver_id']} with no method_names")
                    events.append(_event(SIGNALS[2], receiver, f"{kind} added to {receiver}, an account labelled "
                                         f"{' and '.join(context.watch_categories[receiver])}", block, receipt,
                                         context.watched[receiver], source))
        if "CreateAccount" in types:
            deposit = sum(int(a["deposit"]) for a in receipt["actions"] if a["type"] == "Transfer")
            if deposit < rules.DUST_YOCTO:
                fresh[receiver] = {"deposit": str(deposit), "created_ms": now_ms, "height": block["height"]}
        elif (predecessor in fresh and receiver in context.labelled and receiver in context.deny_receivers
              and ({"FunctionCall", "Transfer"} & set(types))):
            info = fresh[predecessor]
            age_minutes = (now_ms - info["created_ms"]) // 60000
            events.append(_event(SIGNALS[1], predecessor, f"{predecessor}, created at block {info['height']} with a "
                                 f"{_near_text(info['deposit'])} NEAR deposit, called or paid the labelled "
                                 f"account {receiver} {age_minutes} min later", block, receipt,
                                 context.labelled[receiver], source))
    return events


def apply(block, state, context, source, events_dir=EVENTS_DIR):
    """Run the signals over a block not seen yet and write its events; returns the event paths."""
    if state["last_height"] is not None and block["height"] <= state["last_height"]:
        return []
    paths = []
    for event in signals(block, state, context, source):
        state["events"][event["signal"]] += 1
        paths.append(evidence.write(event, events_dir))
    stamp = datetime.fromtimestamp(int(block["timestamp_ns"]) // 10**9, UTC).strftime(_TIME)
    if state["start_height"] is None:
        state["start_height"], state["start_time"] = block["height"], stamp
    state["last_height"], state["last_time"] = block["height"], stamp
    state["blocks"] += 1
    return paths


def block_source(path):
    """Where an event's block came from: the neardata URL and the reduced fixture with its sha256."""
    path = Path(path)
    return {"url": f"{NEARDATA_URL}/v0/block/{path.stem}", "block_fixture": path.name,
            "block_fixture_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class Fetcher:
    """neardata blocks with the request budget and the 429 back off."""

    def __init__(self, client=None, sleep=time.sleep, max_requests=MAX_REQUESTS):
        self.client = client or rpc.Client(NEARDATA_URL, "off", kind="http")
        self.sleep = sleep
        self.requests = 0
        self.max_requests = max_requests

    def get(self, path):
        while True:
            if self.max_requests is not None and self.requests >= self.max_requests:
                raise FeedError(f"the neardata budget of {self.max_requests} requests is used up")
            self.requests += 1
            try:
                return self.client.get(path)
            except rpc.HttpError as error:
                if error.status != 429:
                    raise
                self.sleep(BACKOFF_S)

    def final_height(self):
        answer = self.get("/v0/last_block/final")
        return answer["block"]["header"]["height"]


def run(start, count, record, store=None, blocks_dir=BLOCKS_DIR, state_path=STATE_PATH, events_dir=EVENTS_DIR,
        fetcher=None, incidents_dir=INCIDENTS_DIR):
    """Walk count heights from start (None: after the state's last height); returns (state, events)."""
    state = load_state(state_path)
    context = Context(store or labels.load(), incidents_dir)
    if record:
        fetcher = fetcher or Fetcher()
    if start is None:
        if state["last_height"] is not None:
            start = state["last_height"] + 1
        elif record:
            start = fetcher.final_height()
        else:
            raise FeedError("no state yet: give --from <height>")
    paths = []
    for height in range(start, start + count):
        path = Path(blocks_dir) / f"{height}.json"
        if record and not path.exists():
            raw = fetcher.get(f"/v0/block/{height}")
            _write_json(path, {"height": height, "no_block": True} if raw is None else reduce_block(raw))
        if not path.exists():
            raise FeedError(f"no reduced block {path.name} under {blocks_dir}; record it with --record")
        block = json.loads(path.read_text(encoding="utf-8"))
        if block.get("no_block"):
            if height not in state["skipped_heights"]:
                state["skipped_heights"].append(height)
            continue
        paths += apply(block, state, context, block_source(path), events_dir)
    _write_json(Path(state_path), state, indent=2)
    return state, paths


def follow(start=None, fetcher=None, store=None, state_path=None, events_dir=None, event_blocks_dir=None,
           max_blocks=None, sleep=time.sleep, clock=time.time, stopped=lambda: False):
    """Follow finalized heights, retaining only event blocks and the moving state."""
    fetcher = fetcher or Fetcher(max_requests=None)
    state_dir = Path(os.environ.get("FEED_STATE_DIR", FEED_DIR))
    state_path = Path(state_path) if state_path is not None else state_dir / "state.json"
    events_dir = Path(events_dir) if events_dir is not None else state_dir / "events"
    event_blocks_dir = Path(event_blocks_dir) if event_blocks_dir is not None else state_dir / "event-blocks"
    state = load_state(state_path)
    state.setdefault("mode", "follow")
    state.setdefault("started_at", datetime.fromtimestamp(clock(), UTC).strftime(_TIME))
    state.setdefault("lag_blocks", 0)
    state.setdefault("requests", 0)
    state.setdefault("request_day", datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%d"))
    context = Context(store or labels.load())
    walked = 0
    last_request = None
    while not stopped() and (max_blocks is None or walked < max_blocks):
        if last_request is not None:
            sleep(max(0, FOLLOW_INTERVAL_S - (clock() - last_request)))
        request_day = datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%d")
        if state["request_day"] != request_day:
            state["request_day"], state["requests"] = request_day, 0
        requests_before = fetcher.requests
        final_height = fetcher.final_height()
        state["requests"] += fetcher.requests - requests_before
        last_request = clock()
        next_height = state["last_height"] + 1 if state["last_height"] is not None else (
            start if start is not None else final_height)
        start = None
        if next_height > final_height:
            state["lag_blocks"] = 0
            state["mode"] = "follow"
            _write_json(Path(state_path), state, indent=2)
            sleep(1)
            continue
        while next_height <= final_height and not stopped() and (max_blocks is None or walked < max_blocks):
            if last_request is not None:
                sleep(max(0, FOLLOW_INTERVAL_S - (clock() - last_request)))
            request_day = datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%d")
            if state["request_day"] != request_day:
                state["request_day"], state["requests"] = request_day, 0
            requests_before = fetcher.requests
            try:
                raw = fetcher.get(f"/v0/block/{next_height}")
            except rpc.HttpError as error:
                if error.status != 404:
                    raise
                raw = None
            state["requests"] += fetcher.requests - requests_before
            last_request = clock()
            if raw is None:
                if next_height not in state["skipped_heights"]:
                    state["skipped_heights"].append(next_height)
            else:
                block = reduce_block(raw)
                event_paths = apply(block, state, context,
                                    {"url": f"{NEARDATA_URL}/v0/block/{next_height}",
                                     "block_fixture": f"{next_height}.json",
                                     "block_fixture_sha256": hashlib.sha256(
                                         (json.dumps(block, separators=(",", ":"), ensure_ascii=False) + "\n")
                                         .encode("utf-8")
                                     ).hexdigest()}, events_dir)
                if event_paths:
                    _write_json(Path(event_blocks_dir) / f"{next_height}.json", block)
            walked += 1
            state["mode"] = "follow"
            state["lag_blocks"] = max(0, final_height - (state["last_height"] or final_height))
            _write_json(Path(state_path), state, indent=2)
            next_height += 1
    return state, walked


def stats_lines(state):
    """The feed-stats output: the start date, the block count and the event count per signal."""
    if state.get("mode") == "follow":
        return [f"following since {state['started_at']}",
                f"last block {state['last_height']} at {state['last_time']}",
                f"lag {state['lag_blocks']} blocks"] + [f"{name} {count}" for name, count in state["events"].items()]
    lines = [f"start {state['start_time']} at block {state['start_height']}",
             f"last {state['last_time']} at block {state['last_height']}",
             f"blocks {state['blocks']}, skipped heights {len(state['skipped_heights'])}"]
    return lines + [f"{name} {count}" for name, count in state["events"].items()]
