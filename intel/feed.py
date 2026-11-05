"""A block feed over finalized NEAR mainnet blocks, with five signals.

python -m intel feed [--from <height>] --blocks <n> [--record] walks n block heights from --from
(default: the height after the state's last block; with --record and no state, the height
https://mainnet.neardata.xyz/v0/last_block/final answers). With --record each block is fetched
from https://mainnet.neardata.xyz/v0/block/<height> (keyless, a 30 s timeout, a 10 s back off on a
429, at most MAX_REQUESTS requests a run), reduced and written as fixtures/feed/blocks/<height>.json;
the raw block is never written. Without --record the reduced fixtures are read and nothing
touches the network. A height neardata answers with null holds no block: its fixture says so and
the state lists it as skipped.

python -m intel feed --follow [--from <height>] [--max-blocks <n>] reads the final block from
/v0/last_block/final, which neardata redirects to /v0/block/<final height>, and walks every height
from the state's last height + 1 (with no state: --from, else the final height) to it in order,
never skipping one: it fetches each height below the final one from /v0/block/<height> and takes
the final height's block from that read, so it fetches no block twice. Once caught up it sleeps
1 s and reads the final block again. It stops on SIGINT or after --max-blocks heights, a skipped
height included. It has no request budget and backs off 10 s on a 429 as above. When
FASTNEAR_API_KEY_FILE names a usable key, its neardata requests (the final block and the blocks)
carry that key as an Authorization Bearer header, read once at start and kept in memory; without
one they are keyless, and record mode never sends it. Between one neardata request and the next
it leaves FOLLOW_INTERVAL_KEYED_S (0.1 s, so at most 600 a minute) when keyed and FOLLOW_INTERVAL_S
(0.4 s, so at most 150 a minute) when keyless, since neardata does not throttle keyed requests from
the shared host the follower runs on and does throttle keyless ones from it. No reduced block is
written per height: a block that raised an event is written as
<state dir>/event-blocks/<height>.json, the block_fixture its events' source names. A finalized
height neardata answers with null or a 404 holds no block, as in record mode: the follower adds
it to skipped_heights, writes the state and goes on to the next height. NEAR skips a height
whenever its producer misses the slot, so skipped_heights can grow by a few hundred heights a
day; a list that size is fine for the weeks this hackathon build runs. The state is written after
every height and at every read of the final height while it waits there; on SIGINT the loop stops
after the height in hand and the command exits 0.

The reduced form keeps, for each executed action receipt, its receipt id, transaction hash,
predecessor, receiver, whether it succeeded, and the actions the signals read: DeployContract
(the base58 sha256 of the code, never the code), CreateAccount, Transfer (the deposit),
FunctionCall (the method name and deposit, never the args) and AddKey (the public key and the
permission). The state directory is the one FEED_STATE_DIR names (default intel/feed/, the
recorded window), so a follower on a host writes no tracked file. Its state.json holds the start
height and time, the last height and time, the block count, the skipped heights, the fresh
accounts with their funding deposit and time, and the event count per signal; a follower adds
mode ("follow"), started_at (when the first follow run began), lag_blocks (the final height at the
last read minus the last height), updated_at (the UTC time of the follower's last state write at a
height or at a read of the final height; the transfer poll's own writes keep it, so a follower that
restarts on failed neardata reads never moves it, and the read only API judges the follower stopped
once updated_at is more than FOLLOWER_STOPPED_AFTER_S old), requests (the neardata requests made on
request_day), transfer_requests (the Transfers API requests made on request_day), request_day (the
UTC date both counts are for, checked before every neardata request; a new date resets them) and
disabled_signals (a signal that is off and why: the transfer signal when FASTNEAR_API_KEY_FILE is
unset or names a file that cannot be read or is empty; never the path or the key). The transfer
poll adds last_large_transfer_poll_ms (the start of the last complete poll, or POLL_S before the
first one: the next window's from_timestamp_ms), large_transfer_next_poll_ms (the earliest start
of the next poll), last_large_transfer_error (the UTC time, the HTTP status or null, and the error
type of the last failed poll; never the key or a header), large_transfer_page_caps (each subject
and window that still had rows after MAX_TRANSFER_PAGES pages) and skipped_rows (Transfers API rows
the poll could not read: not an object, a field missing, or a null or missing usd_amount). A block
at or below the last height is never counted twice, so a run that stops can be started again.

The signals, each written as an evidence document under <state dir>/events/<sha256>.json:

  code_hash_matches_incident       a DeployContract whose code hash is the code hash an incident page
                                   reports (intel/incidents/*.json, code_hash_as_reported)
  fresh_dust_funded_pays_labelled  an account created with a deposit below 0.1 NEAR that, within 24 h,
                                   sends a FunctionCall or a Transfer to a NEAR account with a current
                                   incident or denylist row
  watch_account_key_added          an AddKey with full access, or a function call key with an empty
                                   method_names list, on a NEAR account labelled infrastructure,
                                   treasury or watch
  labelled_account_large_transfer  a Transfers API row of at least 1000 USD (the magnitude of its
                                   usd_amount) sent by a NEAR account with a current incident or
                                   denylist row (follow mode only)
  deploy_on_labelled_contract      a successful DeployContract on a NEAR account with a current
                                   catalog_token, infrastructure, bridge, treasury, organisation or
                                   watch row

The transfer poll runs in follow mode only, from the follower's loop both while it walks heights
and while it waits at the final height, every POLL_S (600 s), sooner after a 429 as below. It
reads the key once at start from the file FASTNEAR_API_KEY_FILE names and sends it only as the
Authorization Bearer header of clients that write no fixture. Each poll asks POST
https://transfers.main.fastnear.com/v0/transfers for each subject, FOLLOW_INTERVAL_S apart
(direction sender, min_usd_amount 1000, ignore_system true, limit 100, from_timestamp_ms the last
complete poll's start, to_timestamp_ms this poll's start, which the API treats as exclusive, so no
row is in two windows), following resume_token up to MAX_TRANSFER_PAGES (20) pages. The API gives
an outbound row a negative usd_amount, so a row becomes an event when the magnitude of its
usd_amount is at least LARGE_TRANSFER_USD (1000), whatever the server filter did, and the event
holds that magnitude with direction "outbound". A row the poll cannot read is counted in
skipped_rows and never fails the poll; only a response without a transfers list does. Events are
written and the window moves on only when every request of the poll succeeded. A 429 moves the
next poll to Retry-After seconds later, at most POLL_S (TRANSFER_BACKOFF_S, 60 s, when the header
is absent or not a number of seconds), and keeps the window; any other HTTP, transport or parse
error is recorded in last_large_transfer_error and the window is asked again at the next poll.
Neither stops the block walk. feed-stats and the feed page show the last failed poll.
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
SIGNALS = ("code_hash_matches_incident", "fresh_dust_funded_pays_labelled", "watch_account_key_added",
           "labelled_account_large_transfer", "deploy_on_labelled_contract")
WATCH_CATEGORIES = ("infrastructure", "treasury", "watch")
DEPLOY_CATEGORIES = ("catalog_token", "infrastructure", "bridge", "treasury", "organisation", "watch")
TRANSFERS_URL = "https://transfers.main.fastnear.com"
FRESH_WINDOW_MS = 24 * 60 * 60 * 1000
POLL_S = 600
TRANSFER_BACKOFF_S = 60
LARGE_TRANSFER_USD = 1000
MAX_TRANSFER_PAGES = 20
MAX_REQUESTS = 600
BACKOFF_S = 10
FOLLOW_INTERVAL_S = 0.4
FOLLOW_INTERVAL_KEYED_S = 0.1
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
        self.labelled, self.watched, self.watch_categories, self.deployable, self.deny_receivers = {}, {}, {}, {}, set()
        for assertion in store.assertions:
            if assertion.chain != "near" or assertion.superseded_by is not None:
                continue
            self.labelled.setdefault(assertion.subject, []).append(assertion.id)
            if assertion.category in DEPLOY_CATEGORIES:
                self.deployable.setdefault(assertion.subject, []).append(assertion.id)
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
            if action["type"] == "DeployContract" and receiver in context.deployable:
                events.append(_event(SIGNALS[4], receiver,
                                     f"{receiver} deployed code hash {action['code_hash']}", block, receipt,
                                     context.deployable[receiver], source))
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
        state["events"].setdefault(event["signal"], 0)
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

    def final_block(self):
        """The block at the final height: neardata redirects /v0/last_block/final to /v0/block/<height>."""
        return self.get("/v0/last_block/final")

    def final_height(self):
        return self.final_block()["block"]["header"]["height"]


def poll_transfers(client, state, state_path, context, events_dir, clock, sleep):
    """The large transfer poll when it is due: every subject's rows of the window since the last
    complete poll, asked FOLLOW_INTERVAL_S apart and written as events only when every request
    succeeded."""
    now_ms = int(clock() * 1000)
    if now_ms < state.get("large_transfer_next_poll_ms", now_ms):
        return
    from_ms = state.setdefault("last_large_transfer_poll_ms", now_ms - POLL_S * 1000)
    rows, caps, skipped, last_request = [], [], 0, None
    try:
        for subject in sorted(context.deny_receivers):
            body = {"account_id": subject, "direction": "sender", "from_timestamp_ms": from_ms,
                    "to_timestamp_ms": now_ms, "min_usd_amount": LARGE_TRANSFER_USD, "ignore_system": True,
                    "limit": 100}
            for _ in range(MAX_TRANSFER_PAGES):
                if last_request is not None:
                    sleep(max(0, FOLLOW_INTERVAL_S - (clock() - last_request)))
                state["transfer_requests"] += 1
                response = client.post("/v0/transfers", body)
                last_request = clock()
                if not isinstance(response, dict) or not isinstance(response.get("transfers"), list):
                    raise rpc.MalformedResponse("the transfers response has no transfers list")
                for transfer in response["transfers"]:
                    if not (isinstance(transfer, dict)
                            and all(isinstance(transfer.get(name), str)
                                    for name in ("other_account_id", "asset_id", "transaction_id", "receipt_id"))
                            and str(transfer.get("block_height")).isdecimal()
                            and str(transfer.get("block_timestamp")).isdecimal()
                            and isinstance(transfer.get("usd_amount"), (int, float))):
                        skipped += 1
                    elif abs(transfer["usd_amount"]) >= LARGE_TRANSFER_USD:
                        rows.append((subject, transfer, dict(client.last_source)))
                if not response.get("resume_token"):
                    break
                body = {**body, "resume_token": response["resume_token"]}
            else:
                caps.append({"subject": subject, "from_timestamp_ms": from_ms, "to_timestamp_ms": now_ms})
    except (rpc.HttpError, rpc.TransportError, OSError, ValueError) as error:
        status = error.status if isinstance(error, rpc.HttpError) else None
        state["last_large_transfer_error"] = {"at": datetime.fromtimestamp(now_ms / 1000, UTC).strftime(_TIME),
                                              "status": status, "error": type(error).__name__}
        wait_s = POLL_S
        if status == 429:
            retry_after = (error.retry_after or "").strip()
            wait_s = min(int(retry_after), POLL_S) if retry_after.isdecimal() else TRANSFER_BACKOFF_S
        state["large_transfer_next_poll_ms"] = now_ms + wait_s * 1000
        _write_json(Path(state_path), state, indent=2)
        return
    for subject, transfer, source in rows:
        height, usd_amount = int(transfer["block_height"]), abs(transfer["usd_amount"])
        event = {"version": "1", "kind": "feed_event", "signal": SIGNALS[3], "subject": subject,
                 "detail": f"FastNEAR reported a transfer of {usd_amount} USD from {subject} to "
                           f"\"{transfer['other_account_id']}\"",
                 "labels": context.labelled[subject], "other_account_id": transfer["other_account_id"],
                 "asset_id": transfer["asset_id"], "direction": "outbound", "usd_amount": str(usd_amount),
                 "transaction_id": transfer["transaction_id"], "tx_hash": transfer["transaction_id"],
                 "receipt_id": transfer["receipt_id"], "block_height": height,
                 "observed": {"block_height": height, "timestamp_ms": int(transfer["block_timestamp"]) // 1_000_000},
                 "source": source}
        evidence.write(event, events_dir)
        state["events"][SIGNALS[3]] += 1
    if caps:
        state.setdefault("large_transfer_page_caps", []).extend(caps)
    state["skipped_rows"] = state.get("skipped_rows", 0) + skipped
    state["last_large_transfer_poll_ms"] = now_ms
    state["large_transfer_next_poll_ms"] = now_ms + POLL_S * 1000
    _write_json(Path(state_path), state, indent=2)


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
    key_file = os.environ.get("FASTNEAR_API_KEY_FILE")
    headers, disabled_reason = {}, "FASTNEAR_API_KEY_FILE is not set"
    if key_file:
        try:
            api_key = Path(key_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            api_key = None
        if api_key is None:
            disabled_reason = "the file FASTNEAR_API_KEY_FILE names cannot be read"
        elif not api_key:
            disabled_reason = "the file FASTNEAR_API_KEY_FILE names is empty"
        else:
            headers, disabled_reason = {"Authorization": f"Bearer {api_key}"}, None
    interval_s = FOLLOW_INTERVAL_KEYED_S if headers else FOLLOW_INTERVAL_S
    fetcher = fetcher or Fetcher(rpc.Client(NEARDATA_URL, "off", kind="http", headers=headers), max_requests=None)
    state_dir = Path(os.environ.get("FEED_STATE_DIR", FEED_DIR))
    state_path = Path(state_path) if state_path is not None else state_dir / "state.json"
    events_dir = Path(events_dir) if events_dir is not None else state_dir / "events"
    event_blocks_dir = Path(event_blocks_dir) if event_blocks_dir is not None else state_dir / "event-blocks"
    state = load_state(state_path)
    state.setdefault("mode", "follow")
    state.setdefault("started_at", datetime.fromtimestamp(clock(), UTC).strftime(_TIME))
    state.setdefault("lag_blocks", 0)
    state.setdefault("requests", 0)
    state.setdefault("transfer_requests", 0)
    state.setdefault("request_day", datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%d"))
    for signal in SIGNALS:
        state["events"].setdefault(signal, 0)
    context = Context(store or labels.load())
    disabled = state.setdefault("disabled_signals", {})
    transfers_client = None
    if disabled_reason is None:
        transfers_client = rpc.Client(TRANSFERS_URL, "off", kind="http", headers=headers)
        disabled.pop(SIGNALS[3], None)
    else:
        disabled[SIGNALS[3]] = disabled_reason
    walked = 0
    last_request = None
    while not stopped() and (max_blocks is None or walked < max_blocks):
        if last_request is not None:
            sleep(max(0, interval_s - (clock() - last_request)))
        request_day = datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%d")
        if state["request_day"] != request_day:
            state["request_day"], state["requests"], state["transfer_requests"] = request_day, 0, 0
        if transfers_client is not None:
            poll_transfers(transfers_client, state, state_path, context, events_dir, clock, sleep)
        requests_before = fetcher.requests
        final_block = fetcher.final_block()
        final_height = final_block["block"]["header"]["height"]
        state["requests"] += fetcher.requests - requests_before
        last_request = clock()
        next_height = state["last_height"] + 1 if state["last_height"] is not None else (
            start if start is not None else final_height)
        start = None
        if next_height > final_height:
            state["lag_blocks"] = 0
            state["mode"] = "follow"
            state["updated_at"] = datetime.fromtimestamp(clock(), UTC).strftime(_TIME)
            _write_json(Path(state_path), state, indent=2)
            sleep(1)
            continue
        while next_height <= final_height and not stopped() and (max_blocks is None or walked < max_blocks):
            if last_request is not None and next_height < final_height:
                sleep(max(0, interval_s - (clock() - last_request)))
            request_day = datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%d")
            if state["request_day"] != request_day:
                state["request_day"], state["requests"], state["transfer_requests"] = request_day, 0, 0
            if transfers_client is not None:
                poll_transfers(transfers_client, state, state_path, context, events_dir, clock, sleep)
            if next_height == final_height:
                raw = final_block
            else:
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
            state["updated_at"] = datetime.fromtimestamp(clock(), UTC).strftime(_TIME)
            _write_json(Path(state_path), state, indent=2)
            next_height += 1
    return state, walked


def stats_lines(state, stopped_since=None):
    """The feed-stats output: the start date, the block count and the event count per signal. For a follower
    judged stopped, stopped_since is when it last wrote its state, and a line saying so with an unknown lag
    takes the place of the lag line."""
    if state.get("mode") == "follow":
        lines = [f"following since {state['started_at']}",
                 f"last block {state['last_height']} at {state['last_time']}",
                 f"lag {state['lag_blocks']} blocks" if stopped_since is None
                 else f"follower stopped: it last wrote its state at {stopped_since}, lag unknown"]
        events = [f"{name} {state['events'].get(name, 0)}" for name in SIGNALS]
    else:
        lines = [f"start {state['start_time']} at block {state['start_height']}",
                 f"last {state['last_time']} at block {state['last_height']}",
                 f"blocks {state['blocks']}, skipped heights {len(state['skipped_heights'])}"]
        events = [f"{name} {count}" for name, count in state['events'].items()]
    lines += events
    lines += [f"{name} disabled: {reason}" for name, reason in state.get("disabled_signals", {}).items()]
    error = state.get("last_large_transfer_error")
    if error:
        status = "no HTTP status" if error["status"] is None else f"HTTP {error['status']}"
        lines.append(f"{SIGNALS[3]} last failed poll at {error['at']}: {error['error']}, {status}")
    return lines
