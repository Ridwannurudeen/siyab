import base64
import contextlib
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import evidence, rpc, wasm
from intel import __main__ as intel_main
from intel import api, feed, labels

ROOT = Path(__file__).resolve().parent.parent
BLOCKS = ROOT / "fixtures" / "feed" / "blocks"
SYNTHETIC = {"synthetic-code-hash.json": "code_hash_matches_incident",
             "synthetic-fresh-dust-incident.json": "fresh_dust_funded_pays_labelled",
             "synthetic-watch-addkey.json": "watch_account_key_added",
             "synthetic-deploy-labelled.json": "deploy_on_labelled_contract"}
MAX_BYTES = 64 * 1024


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def recorded():
    return sorted((p for p in BLOCKS.glob("*.json") if not p.name.startswith("synthetic-")), key=lambda p: int(p.stem))


def keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys(item)


class FakeFetcher:
    """Answers block heights from a dict of raw blocks, with optional 429 answers first."""

    def __init__(self, blocks, final, throttled=0):
        self.blocks, self.final, self.throttled = blocks, final, throttled
        self.sleeps = []
        self.inner = feed.Fetcher(client=self, sleep=self.sleeps.append)

    def get(self, path):
        if self.throttled:
            self.throttled -= 1
            raise rpc.HttpError(429, path)
        if path == "/v0/last_block/final":
            return self.blocks[self.final]
        return self.blocks.get(int(path.rsplit("/", 1)[1]))


def raw_block(height, receipts, timestamp_ns=1793284740730787531):
    return {"block": {"header": {"height": height, "hash": f"hash{height}", "timestamp_nanosec": str(timestamp_ns),
                                 "signature": "ed25519:sig"}},
            "shards": [{"shard_id": 0, "receipt_execution_outcomes": receipts}]}


def raw_receipt(receiver, actions, predecessor="system", status=None):
    return {"execution_outcome": {"outcome": {"status": status or {"SuccessValue": ""}, "logs": ["a log"]}},
            "receipt": {"predecessor_id": predecessor, "receiver_id": receiver, "receipt_id": f"r-{receiver}",
                        "receipt": {"Action": {"actions": actions, "signer_id": predecessor,
                                               "signer_public_key": "ed25519:pk"}}},
            "tx_hash": f"t-{receiver}"}


T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC).timestamp()
T0_MS = int(T0 * 1000)


class TransfersStub:
    """The Transfers API over rows: from_timestamp_ms inclusive, to_timestamp_ms exclusive on the block
    time, page_size rows a page, and a resume_token while rows remain (always for an endless account)."""

    def __init__(self, rows=(), page_size=100, endless=()):
        self.rows, self.page_size, self.endless = list(rows), page_size, set(endless)
        self.calls = []
        self.last_source = {"provider": "transfers.main.fastnear.com", "request_sha256": "request",
                            "fetched_at": "2026-11-03T12:00:00Z"}

    def post(self, path, body):
        self.calls.append((path, body))
        matching = [row for row in self.rows if row["account_id"] == body["account_id"]
                    and body["from_timestamp_ms"] <= int(row["block_timestamp"]) // 1_000_000
                    < body["to_timestamp_ms"]]
        offset = int(body.get("resume_token") or 0)
        more = body["account_id"] in self.endless or offset + self.page_size < len(matching)
        return {"transfers": matching[offset:offset + self.page_size],
                "resume_token": str(offset + self.page_size) if more else None}


def transfer_row(account, timestamp_ms, number):
    """An outbound row as the Transfers API returns it: block height and time as strings, a negative
    usd_amount float and a minus signed amount."""
    return {"account_id": account, "other_account_id": "recipient.near", "asset_id": "native:near",
            "amount": "-1200500000000000000000000", "usd_amount": -1200.5,
            "transaction_id": f"tx-{number}", "receipt_id": f"receipt-{number}",
            "block_height": str(100 + number), "block_timestamp": str(timestamp_ms * 1_000_000)}


def http_reply(data):
    handle = mock.MagicMock()
    handle.__enter__.return_value.read.return_value = data
    return handle


class SignalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = labels.load()
        cls.context = feed.Context(cls.store)

    def events(self, block, state=None):
        return feed.signals(block, state or feed.new_state(), self.context, {"url": "test"})

    def test_each_signal_fires_on_its_synthetic_block_and_no_other(self):
        for name, signal in SYNTHETIC.items():
            with self.subTest(block=name):
                block = json.loads((BLOCKS / name).read_text(encoding="utf-8"))
                events = self.events(block)
                self.assertEqual([event["signal"] for event in events], [signal])
                self.assertTrue(events[0]["detail"])
                for assertion_id in events[0]["labels"]:
                    self.assertIn(assertion_id, {a.id for a in self.store.assertions})

    def test_a_new_feed_event_sends_one_alert_with_its_evidence_hash(self):
        block = json.loads((BLOCKS / "synthetic-watch-addkey.json").read_text(encoding="utf-8"))
        block["receipts"][0]["receiver_id"] = "game.hot.tg"
        delivered = []

        class CaptureSink:
            def send(self, alert):
                delivered.append(alert)
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            url_file = root / "alert-url"
            url_file.write_text("https://alerts.example.test/hook\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "SHIELDBOT_ALERT_URL_FILE": str(url_file),
                "SHIELDBOT_ALERT_SECRET_FILE": str(root / "secret"),
            }, clear=True), mock.patch("intel.alerts.WebhookSink", return_value=CaptureSink()) as webhook_sink:
                state = feed.new_state()
                paths = feed.apply(block, state, self.context, {"url": "test"}, events_dir=root / "events")
                event = json.loads(paths[0].read_text(encoding="utf-8"))
                repeated = feed.apply(block, state, self.context, {"url": "test"}, events_dir=root / "events")

        self.assertEqual(len(paths), 1)
        self.assertEqual(repeated, [])
        webhook_sink.assert_called_once_with("https://alerts.example.test/hook", str(root / "secret"))
        [alert] = delivered
        self.assertEqual(alert.source, "feed")
        self.assertEqual(alert.severity, "warn")
        self.assertEqual(alert.subject, event["subject"])
        self.assertEqual(alert.ref, event["signal"])
        self.assertTrue(alert.title)
        self.assertEqual(alert.detail, event["detail"])
        self.assertEqual(alert.evidence_sha256, paths[0].stem)
        self.assertEqual(alert.created_ms, event["observed"]["timestamp_ms"])

    def test_the_incident_code_hash_comes_from_the_incident_pages(self):
        rhea = json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_text(encoding="utf-8"))
        self.assertEqual(self.context.code_hashes, {rhea["code_hash_as_reported"]["value"]: "rhea"})

    def test_the_watch_set_is_every_infrastructure_treasury_and_watch_near_account(self):
        expected = {a.subject for a in self.store.assertions if a.chain == "near" and a.superseded_by is None
                    and a.category in ("infrastructure", "treasury", "watch")}
        self.assertEqual(set(self.context.watched), expected)
        self.assertEqual(self.context.watch_categories["game.hot.tg"], ["watch"])
        self.assertIn("intents.near", expected)

    def test_a_full_access_key_on_a_watch_labelled_account_fires(self):
        block = json.loads((BLOCKS / "synthetic-watch-addkey.json").read_text(encoding="utf-8"))
        block["receipts"][0]["receiver_id"] = "game.hot.tg"
        [event] = self.events(block)
        self.assertEqual(event["signal"], "watch_account_key_added")
        self.assertEqual(event["detail"], "a full access key added to game.hot.tg, an account labelled watch")
        self.assertTrue(set(event["labels"]) <= {a.id for a in self.store.by_subject("game.hot.tg", "near")})

    def test_a_function_call_key_with_methods_or_on_an_unwatched_account_is_quiet(self):
        block = json.loads((BLOCKS / "synthetic-watch-addkey.json").read_text(encoding="utf-8"))
        scoped = json.loads(json.dumps(block))
        scoped["receipts"][0]["actions"][0]["permission"] = {"receiver_id": "x.near", "method_names": ["m"]}
        self.assertEqual(self.events(scoped), [])
        elsewhere = json.loads(json.dumps(block))
        elsewhere["receipts"][0]["receiver_id"] = "somebody-unlabelled.near"
        self.assertEqual(self.events(elsewhere), [])

    def test_a_fresh_account_older_than_24_hours_is_dropped(self):
        block = json.loads((BLOCKS / "synthetic-fresh-dust-incident.json").read_text(encoding="utf-8"))
        creation, payment = block["receipts"]
        state = feed.new_state()
        self.assertEqual(self.events(dict(block, receipts=[creation]), state), [])
        self.assertIn(creation["receiver_id"], state["fresh_accounts"])
        later = dict(block, receipts=[payment], timestamp_ns=str(int(block["timestamp_ns"]) + 25 * 3600 * 10**9))
        self.assertEqual(self.events(later, state), [])
        self.assertEqual(state["fresh_accounts"], {})

    def test_dust_signal_needs_an_incident_or_denylist_receiver(self):
        labelled = json.loads((BLOCKS / "synthetic-fresh-dust.json").read_text(encoding="utf-8"))
        self.assertIn(labelled["receipts"][1]["receiver_id"], self.context.labelled)
        self.assertNotIn(labelled["receipts"][1]["receiver_id"], self.context.deny_receivers)
        self.assertEqual(self.events(labelled), [])
        incident = json.loads((BLOCKS / "synthetic-fresh-dust-incident.json").read_text(encoding="utf-8"))
        self.assertIn(incident["receipts"][1]["receiver_id"], self.context.deny_receivers)
        self.assertEqual([event["signal"] for event in self.events(incident)], ["fresh_dust_funded_pays_labelled"])

    def test_a_failed_receipt_raises_nothing(self):
        block = json.loads((BLOCKS / "synthetic-code-hash.json").read_text(encoding="utf-8"))
        block["receipts"][0]["ok"] = False
        self.assertEqual(self.events(block), [])

    def test_deploy_on_a_labelled_contract_fires_with_the_code_hash_and_is_quiet_elsewhere(self):
        block = json.loads((BLOCKS / "synthetic-deploy-labelled.json").read_text(encoding="utf-8"))
        [event] = self.events(block)
        self.assertEqual(event["signal"], "deploy_on_labelled_contract")
        self.assertIn(block["receipts"][0]["actions"][0]["code_hash"], event["detail"])
        self.assertTrue(event["labels"])
        elsewhere = json.loads(json.dumps(block))
        elsewhere["receipts"][0]["receiver_id"] = "unlabelled-account.near"
        self.assertEqual(self.events(elsewhere), [])

    def test_signal_order_and_stats_lines_include_the_new_feed_signals(self):
        self.assertEqual(feed.SIGNALS, ("code_hash_matches_incident", "fresh_dust_funded_pays_labelled",
                                       "watch_account_key_added", "labelled_account_large_transfer",
                                       "deploy_on_labelled_contract"))
        state = feed.new_state()
        state.update(mode="follow", started_at="2026-11-03T00:00:00Z", last_height=1,
                     last_time="2026-11-03T00:00:01Z", lag_blocks=0)
        self.assertEqual(feed.stats_lines(state)[3:], [f"{name} 0" for name in feed.SIGNALS])


class ReducerTest(unittest.TestCase):
    def test_the_reducer_keeps_the_hash_and_drops_code_args_signatures_and_logs(self):
        code = (ROOT / "fixtures" / "wasm" / "tiny.wasm").read_bytes()
        actions = ["CreateAccount", {"Transfer": {"deposit": "5"}},
                   {"DeployContract": {"code": base64.b64encode(code).decode("ascii")}},
                   {"FunctionCall": {"method_name": "go", "args": "e30=", "gas": 1, "deposit": "0"}},
                   {"AddKey": {"public_key": "ed25519:pk", "access_key": {"nonce": 0, "permission": "FullAccess"}}},
                   {"Stake": {"stake": "1", "public_key": "ed25519:pk"}}]
        raw = raw_block(7, [raw_receipt("new.near", actions, "maker.near"),
                            {"receipt": {"predecessor_id": "a", "receiver_id": "b", "receipt_id": "d",
                                         "receipt": {"Data": {"data": "x"}}}, "execution_outcome": {}}])
        reduced = feed.reduce_block(raw)
        self.assertEqual(reduced["height"], 7)
        [receipt] = reduced["receipts"]
        self.assertEqual([a["type"] for a in receipt["actions"]],
                         ["CreateAccount", "Transfer", "DeployContract", "FunctionCall", "AddKey"])
        self.assertEqual(receipt["actions"][2]["code_hash"], wasm.codehash(code)[1])
        text = json.dumps(reduced)
        for dropped in ('"code"', '"args"', '"signature"', '"logs"', "e30=", '"signer_public_key"'):
            self.assertNotIn(dropped, text)

    def test_a_failure_status_is_kept_as_not_ok(self):
        raw = raw_block(8, [raw_receipt("x.near", [{"Transfer": {"deposit": "1"}}], status={"Failure": {}})])
        self.assertFalse(feed.reduce_block(raw)["receipts"][0]["ok"])


class RecordedFeedTest(unittest.TestCase):
    def test_the_recorded_blocks_are_reduced_forms_only(self):
        paths = list(BLOCKS.glob("*.json"))
        self.assertGreaterEqual(len(recorded()), 300)
        for path in paths:
            with self.subTest(path=path.name):
                self.assertLessEqual(path.stat().st_size, MAX_BYTES)
                found = set(keys(json.loads(path.read_text(encoding="utf-8"))))
                self.assertFalse(found & {"code", "args", "signature", "logs", "chunks", "shards"})

    def test_the_recorded_blocks_are_consecutive_heights(self):
        heights = [int(p.stem) for p in recorded()]
        self.assertEqual(heights, list(range(heights[0], heights[0] + len(heights))))

    def test_a_replay_of_the_recorded_blocks_rebuilds_the_state_and_the_events(self):
        state = json.loads(feed.STATE_PATH.read_text(encoding="utf-8"))
        heights = [int(p.stem) for p in recorded()]
        with tempfile.TemporaryDirectory() as tmp, mock.patch("urllib.request.urlopen", side_effect=no_network):
            rebuilt, _ = feed.run(heights[0], len(heights), False, state_path=Path(tmp) / "state.json",
                                  events_dir=Path(tmp) / "events")
            self.assertEqual(rebuilt, state)
            written = sorted(p.name for p in (Path(tmp) / "events").glob("*.json"))
        self.assertEqual(written, sorted(p.name for p in feed.EVENTS_DIR.glob("*.json")))
        self.assertEqual(state["blocks"] + len(state["skipped_heights"]), len(heights))
        self.assertEqual(sum(state["events"].values()), len(written))

    def test_every_event_file_is_canonical_and_names_its_block(self):
        for path in feed.EVENTS_DIR.glob("*.json"):
            with self.subTest(path=path.name):
                self.assertTrue(evidence.check_file(path))
                event = json.loads(path.read_bytes())
                self.assertIn(event["signal"], feed.SIGNALS)
                self.assertTrue((BLOCKS / event["source"]["block_fixture"]).is_file())

    def test_the_state_survives_a_restart(self):
        heights = [int(p.stem) for p in recorded()]
        half = len(heights) // 2
        with tempfile.TemporaryDirectory() as tmp:
            state_path, events = Path(tmp) / "state.json", Path(tmp) / "events"
            feed.run(heights[0], half, False, state_path=state_path, events_dir=events)
            feed.run(heights[0], half + 5, False, state_path=state_path, events_dir=events)
            restarted, _ = feed.run(None, len(heights) - half - 5, False, state_path=state_path, events_dir=events)
        with tempfile.TemporaryDirectory() as tmp:
            once, _ = feed.run(heights[0], len(heights), False, state_path=Path(tmp) / "state.json",
                               events_dir=Path(tmp) / "events")
        self.assertEqual(restarted, once)

    def test_the_api_serves_the_committed_state_and_events(self):
        state = json.loads(feed.STATE_PATH.read_text(encoding="utf-8"))
        status, body = api.route("/feed", labels.load())
        self.assertEqual(status, 200)
        self.assertNotIn("fresh_accounts", body["stats"])
        self.assertEqual(body["stats"], {key: value for key, value in state.items() if key != "fresh_accounts"})
        self.assertIsNone(body["follower"])
        self.assertTrue(state["fresh_accounts"])
        self.assertEqual(len(body["events"]), min(len(list(feed.EVENTS_DIR.glob("*.json"))), api.FEED_LIMIT))

    def test_a_missing_reduced_block_stops_the_replay(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(feed.FeedError):
            feed.run(1, 1, False, blocks_dir=Path(tmp), state_path=Path(tmp) / "s.json", events_dir=Path(tmp) / "e")

    def test_feed_stats_prints_the_state(self):
        state = json.loads(feed.STATE_PATH.read_text(encoding="utf-8"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(intel_main.main(["feed-stats"]), 0)
        self.assertEqual(output.getvalue().splitlines(), feed.stats_lines(state))
        self.assertIn(f"blocks {state['blocks']}, skipped heights {len(state['skipped_heights'])}",
                      output.getvalue())
        for name, count in state["events"].items():
            self.assertIn(f"{name} {count}", output.getvalue().splitlines())


class RecordModeTest(unittest.TestCase):
    def test_record_writes_reduced_blocks_marks_missing_heights_and_backs_off_on_429(self):
        blocks = {100: raw_block(100, [raw_receipt("a.near", [{"Transfer": {"deposit": "1"}}])]), 101: None,
                  102: raw_block(102, [])}
        fake = FakeFetcher(blocks, 100, throttled=2)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state, _ = feed.run(None, 3, True, blocks_dir=directory, state_path=directory / "s.json",
                                events_dir=directory / "e", fetcher=fake.inner)
            self.assertEqual(json.loads((directory / "101.json").read_text(encoding="utf-8")),
                             {"height": 101, "no_block": True})
            self.assertEqual(json.loads((directory / "100.json").read_text(encoding="utf-8")),
                             feed.reduce_block(blocks[100]))
        self.assertEqual(fake.sleeps, [feed.BACKOFF_S, feed.BACKOFF_S])
        self.assertEqual((state["start_height"], state["last_height"], state["blocks"], state["skipped_heights"]),
                         (100, 102, 2, [101]))

    def test_the_request_budget_is_enforced(self):
        fake = FakeFetcher({}, 0)
        fake.inner.requests = feed.MAX_REQUESTS
        with self.assertRaises(feed.FeedError):
            fake.inner.get("/v0/block/1")

    def test_a_non_429_error_is_not_retried(self):
        class Refusing:
            def get(self, path):
                raise rpc.HttpError(401, path)

        fetcher = feed.Fetcher(client=Refusing(), sleep=lambda s: self.fail("slept"))
        with self.assertRaises(rpc.HttpError):
            fetcher.get("/v0/block/1")


class FollowModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.subjects = sorted(feed.Context(labels.load()).deny_receivers)

    def setUp(self):
        import os

        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FASTNEAR_API_KEY_FILE", None)

    def blocks(self, end):
        return {height: raw_block(height, []) for height in range(1, end + 1)}

    def follow_transfers(self, root, fake, clock, client=None, max_blocks=1, **kwargs):
        """follow with a temp key file holding a literal test key; client stands in for rpc.Client."""
        import os

        key_file = root / "fastnear.key"
        key_file.write_text("literal-test-key\n", encoding="utf-8")
        kwargs.setdefault("sleep", lambda _: None)
        replaced = (mock.patch("intel.feed.rpc.Client", return_value=client) if client is not None
                    else contextlib.nullcontext())
        with mock.patch.dict(os.environ, {"FASTNEAR_API_KEY_FILE": str(key_file)}), replaced:
            return feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json", events_dir=root / "events",
                               event_blocks_dir=root / "event-blocks", max_blocks=max_blocks, clock=clock, **kwargs)

    def transfer_events(self, root):
        return sorted((json.loads(path.read_text(encoding="utf-8")) for path in (root / "events").glob("*.json")),
                      key=lambda event: event["transaction_id"])

    def test_follow_walks_to_the_final_height_without_writing_block_fixtures(self):
        fake = FakeFetcher(self.blocks(3), 3)
        fixtures = len(list(BLOCKS.glob("*.json")))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, walked = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                        events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                        max_blocks=3, sleep=lambda _: None)
            self.assertEqual((walked, state["last_height"], state["blocks"]), (3, 3, 3))
            self.assertEqual(list(root.glob("*.json")), [root / "state.json"])
            self.assertEqual(state["mode"], "follow")
            self.assertEqual(state["requests"], 3)
        self.assertEqual(len(list(BLOCKS.glob("*.json"))), fixtures)

    def test_follow_records_a_null_or_404_height_as_skipped_and_goes_on(self):
        class NotFound(FakeFetcher):
            def get(self, path):
                if path == "/v0/block/2":
                    raise rpc.HttpError(404, path)
                return super().get(path)

        for fake in (FakeFetcher({1: raw_block(1, []), 2: None, 3: raw_block(3, [])}, 3), NotFound(self.blocks(3), 3)):
            with self.subTest(fetcher=type(fake).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state, walked = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                            events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                            max_blocks=3, sleep=lambda _: None)
                self.assertEqual((walked, state["last_height"], state["blocks"], state["skipped_heights"]),
                                 (3, 3, 2, [2]))
                self.assertEqual(state["requests"], 3)
                self.assertEqual(json.loads((root / "state.json").read_text(encoding="utf-8")), state)
                self.assertFalse((root / "events").exists())
                self.assertFalse((root / "event-blocks").exists())

    def test_a_final_height_one_ahead_at_every_read_costs_one_request_a_height(self):
        paths = []

        class Advancing(FakeFetcher):
            def get(self, path):
                paths.append(path)
                if path == "/v0/last_block/final":
                    self.final += 1
                return super().get(path)

        fake = Advancing(self.blocks(10), 0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, walked = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                        events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                        max_blocks=10, sleep=lambda _: None)
        self.assertEqual((walked, state["last_height"], state["blocks"]), (10, 10, 10))
        self.assertEqual(paths, ["/v0/last_block/final"] * 10)
        self.assertEqual((fake.inner.requests, state["requests"]), (10, 10))

    def test_a_catch_up_walk_of_k_heights_is_one_final_read_and_k_minus_1_block_fetches(self):
        paths = []

        class Logged(FakeFetcher):
            def get(self, path):
                paths.append(path)
                return super().get(path)

        fake = Logged(self.blocks(5), 5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, walked = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                        events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                        max_blocks=5, sleep=lambda _: None)
        self.assertEqual((walked, state["last_height"], state["blocks"]), (5, 5, 5))
        self.assertEqual(paths, ["/v0/last_block/final"] + [f"/v0/block/{height}" for height in range(1, 5)])
        self.assertEqual(state["requests"], 5)

    def test_a_catch_up_walk_paces_its_heights_and_waits_1_s_only_once_it_reaches_the_final_height(self):
        now, log = [T0], []

        class Logged(FakeFetcher):
            def get(self, path):
                log.append("get")
                return super().get(path)

        def sleep(seconds):
            log.append(seconds)
            now[0] += seconds

        fake = Logged(self.blocks(5), 5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, walked = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                        events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                        sleep=sleep, clock=lambda: now[0], stopped=lambda: 1 in log)
        self.assertEqual((walked, state["last_height"], state["requests"]), (5, 5, 5))
        self.assertEqual(log, ["get", 0.4, "get", 0.4, "get", 0.4, "get", 0.4, "get", 1])

    def test_a_follower_that_keeps_up_with_the_chain_makes_about_one_request_a_height(self):
        now = [T0]

        class Chain(FakeFetcher):
            """Finalizes a height every 0.6 s; every request takes 0.15 s."""

            def get(self, path):
                now[0] += 0.15
                self.final = 1 + int((now[0] - T0) / 0.6)
                return super().get(path)

        def sleep(seconds):
            now[0] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            state, walked = self.follow_transfers(Path(tmp), Chain(self.blocks(200), 1), lambda: now[0],
                                                  TransfersStub(), max_blocks=100, sleep=sleep)
        self.assertEqual((walked, state["last_height"]), (100, 100))
        self.assertIn(state["requests"], (100, 101))

    def test_the_final_height_is_the_block_of_the_final_read_with_the_events_a_fetch_of_it_writes(self):
        key = {"AddKey": {"public_key": "ed25519:pk", "access_key": {"nonce": 0, "permission": "FullAccess"}}}
        event_block = raw_block(3, [raw_receipt("game.hot.tg", [key])])

        class FinalRead(FakeFetcher):
            """Answers the final read with the event block and refuses to serve that height again."""

            def get(self, path):
                if path == "/v0/block/3":
                    raise AssertionError("the final height was fetched again")
                if path == "/v0/last_block/final":
                    return event_block
                return super().get(path)

        written = {}
        for name, fake in (("final read", FinalRead(self.blocks(3), 3)),
                           ("fetched", FakeFetcher({**self.blocks(4), 3: event_block}, 4))):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state, walked = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                            events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                            max_blocks=3, sleep=lambda _: None, clock=lambda: T0)
                self.assertEqual((walked, state["last_height"], state["events"]["watch_account_key_added"]),
                                 (3, 3, 1))
                written[name] = {path.relative_to(root).as_posix(): path.read_bytes()
                                 for path in root.rglob("*.json") if path.name != "state.json"}
        self.assertEqual(written["final read"], written["fetched"])
        [event] = [json.loads(data) for path, data in written["final read"].items() if path.startswith("events/")]
        self.assertEqual(event["source"]["url"], f"{feed.NEARDATA_URL}/v0/block/3")
        self.assertEqual(event["source"]["block_fixture_sha256"],
                         hashlib.sha256(written["final read"]["event-blocks/3.json"]).hexdigest())
        self.assertEqual(json.loads(written["final read"]["event-blocks/3.json"]), feed.reduce_block(event_block))

    def test_updated_at_is_stamped_at_every_height_and_every_wait_and_never_by_the_transfers_poll(self):
        now, stamps, waits = [T0], [], []
        write_json = feed._write_json

        class Timed(FakeFetcher):
            def get(self, path):
                now[0] += 1
                return super().get(path)

        def recording(path, value, indent=None):
            if Path(path).name == "state.json":
                stamps.append(value.get("updated_at"))
            write_json(path, value, indent)

        def sleep(seconds):
            now[0] += seconds
            if seconds == 1:
                waits.append(seconds)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(feed, "_write_json", recording):
            state, walked = self.follow_transfers(Path(tmp), Timed(self.blocks(2), 2), lambda: now[0], TransfersStub(),
                                                  max_blocks=None, sleep=sleep, stopped=lambda: len(waits) == 2)
        self.assertEqual((walked, state["last_height"], state["last_large_transfer_poll_ms"]), (2, 2, T0_MS))
        self.assertEqual(stamps, [None, "2026-11-03T12:00:11Z", "2026-11-03T12:00:11Z", "2026-11-03T12:00:13Z"])

    def test_a_restart_neardata_refuses_keeps_updated_at_while_the_transfers_poll_writes_the_state(self):
        class Refused(FakeFetcher):
            def get(self, path):
                raise rpc.HttpError(402, path)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.follow_transfers(root, FakeFetcher(self.blocks(1), 1), lambda: T0, TransfersStub())
            for restart in range(1, 2 + api.FOLLOWER_STOPPED_AFTER_S // (feed.POLL_S + 5)):
                later = T0 + restart * (feed.POLL_S + 5)
                with self.subTest(restart=restart), self.assertRaises(rpc.HttpError):
                    self.follow_transfers(root, Refused({}, 0), lambda later=later: later, TransfersStub())
                state = json.loads((root / "state.json").read_text(encoding="utf-8"))
                self.assertEqual(state["last_large_transfer_poll_ms"], int(later * 1000))
                self.assertEqual(state["updated_at"], "2026-11-03T12:00:00Z")
            self.assertEqual(api._stopped_since(state, later), "2026-11-03T12:00:00Z")

    def test_a_restart_whose_block_fetch_fails_after_its_final_read_never_stamps_in_a_poll_due_between(self):
        class Failing(FakeFetcher):
            def get(self, path):
                if path == "/v0/block/2":
                    raise rpc.HttpError(500, path)
                return super().get(path)

        now = [T0]

        def sleep(seconds):
            now[0] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feed._write_json(root / "state.json", {**feed.new_state(), "mode": "follow",
                                                   "started_at": "2026-11-03T08:00:00Z", "last_height": 1,
                                                   "blocks": 1, "last_time": "2026-11-03T11:10:00Z",
                                                   "updated_at": "2026-11-03T11:10:00Z",
                                                   "last_large_transfer_poll_ms": T0_MS - 599_950,
                                                   "large_transfer_next_poll_ms": T0_MS + 50}, indent=2)
            with self.assertRaises(rpc.HttpError):
                self.follow_transfers(root, Failing(self.blocks(3), 3), lambda: now[0], TransfersStub(),
                                      max_blocks=None, sleep=sleep)
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(state["last_large_transfer_poll_ms"], T0_MS + 50)
        self.assertEqual((state["last_height"], state["updated_at"]), (1, "2026-11-03T11:10:00Z"))
        self.assertEqual(api._stopped_since(state, T0 + 60), "2026-11-03T11:10:00Z")

    def test_a_fresh_install_whose_final_read_always_fails_reads_stopped_by_its_started_at(self):
        import os

        class Refused(FakeFetcher):
            def get(self, path):
                raise rpc.HttpError(402, path)

        first = time.time() - 2 * 3600
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for start in (first, time.time()):
                with self.assertRaises(rpc.HttpError):
                    self.follow_transfers(root, Refused({}, 0), lambda start=start: start, TransfersStub())
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            with mock.patch.dict(os.environ, {"FEED_STATE_DIR": str(root)}):
                status, body = api.route("/feed", labels.load())
                page = api._live_feed_page().decode("utf-8")
        started_at = datetime.fromtimestamp(first, UTC).strftime(feed._TIME)
        self.assertEqual((state["started_at"], state["last_time"]), (started_at, None))
        self.assertGreater(state["last_large_transfer_poll_ms"], int(first * 1000))
        self.assertNotIn("updated_at", state)
        self.assertEqual((status, body["follower"], body["stats"]["lag_blocks"]), (200, "stopped", None))
        self.assertIn(f"<li>follower stopped: its last update was at {started_at}, lag unknown</li>", page)

    def test_a_state_without_updated_at_reads_stopped_after_a_refused_restart_whose_transfers_poll_writes_it(self):
        import os

        class Refused(FakeFetcher):
            def get(self, path):
                raise rpc.HttpError(402, path)

        hours_ago = datetime.fromtimestamp(time.time() - 4 * 3600, UTC).strftime(feed._TIME)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feed._write_json(root / "state.json", {**feed.new_state(), "mode": "follow", "started_at": hours_ago,
                                                   "last_height": 1, "last_time": hours_ago, "blocks": 1,
                                                   "lag_blocks": 0}, indent=2)
            with self.assertRaises(rpc.HttpError):
                self.follow_transfers(root, Refused({}, 0), time.time, TransfersStub())
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            with mock.patch.dict(os.environ, {"FEED_STATE_DIR": str(root)}):
                status, body = api.route("/feed", labels.load())
                page = api._live_feed_page().decode("utf-8")
        self.assertNotIn("updated_at", state)
        self.assertEqual(state["transfer_requests"], len(self.subjects))
        self.assertEqual((status, body["follower"], body["stats"]["lag_blocks"]), (200, "stopped", None))
        self.assertIn(f"<li>follower stopped: its last update was at {hours_ago}, lag unknown</li>", page)

    def test_a_running_follower_stays_running_through_a_transfers_poll_that_pages_to_the_cap(self):
        now, judged, stamps, waits = [T0], [], [], []
        write_json = feed._write_json

        def judge():
            path = root / "state.json"
            judged.append(api._stopped_since(json.loads(path.read_text(encoding="utf-8")), now[0]))

        class Slow(TransfersStub):
            def post(self, path, body):
                now[0] += rpc.TIMEOUT_S
                judge()
                return super().post(path, body)

        class Judged(FakeFetcher):
            def get(self, path):
                judge()
                return super().get(path)

        def recording(path, value, indent=None):
            if Path(path).name == "state.json":
                stamps.append(value.get("updated_at"))
            write_json(path, value, indent)

        def sleep(seconds):
            now[0] += seconds
            if seconds == 1:
                waits.append(seconds)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feed._write_json(root / "state.json", {**feed.new_state(), "mode": "follow", "last_height": 1, "blocks": 1,
                                                   "last_time": "2026-11-03T12:00:00Z",
                                                   "updated_at": "2026-11-03T12:00:00Z",
                                                   "last_large_transfer_poll_ms": T0_MS - 599_000,
                                                   "large_transfer_next_poll_ms": T0_MS + 1000}, indent=2)
            with mock.patch.object(feed, "_write_json", recording):
                state, walked = self.follow_transfers(root, Judged(self.blocks(2), 2), lambda: now[0],
                                                      Slow(endless=[self.subjects[0]]), max_blocks=None, sleep=sleep,
                                                      stopped=lambda: len(waits) == 2)
        requests = len(self.subjects) - 1 + feed.MAX_TRANSFER_PAGES
        self.assertEqual((walked, state["last_height"], state["transfer_requests"]), (1, 2, requests))
        self.assertEqual(len(judged), requests + 2)
        self.assertEqual(judged, [None] * len(judged))
        self.assertEqual(stamps, ["2026-11-03T12:00:00Z", "2026-11-03T12:21:47Z", "2026-11-03T12:21:47Z"])

    def test_follow_counts_requests_on_the_utc_date_they_are_made_across_midnight(self):
        midnight = datetime(2026, 11, 4, tzinfo=UTC).timestamp()
        now = [midnight - 1.0]
        made = []

        class Timed(FakeFetcher):
            def get(self, path):
                made.append(now[0])
                now[0] += 0.3
                return super().get(path)

        def sleep(seconds):
            now[0] += seconds

        fake = Timed(self.blocks(4), 4)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, walked = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                        events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                        max_blocks=4, sleep=sleep, clock=lambda: now[0])
        after = [moment for moment in made if moment >= midnight]
        self.assertEqual(walked, 4)
        self.assertEqual(state["request_day"], "2026-11-04")
        self.assertEqual(state["requests"], len(after))
        self.assertLess(len(after), len(made))

    def test_follow_with_blocks_or_record_stops_with_one_line(self):
        for extra in (["--blocks", "3"], ["--record"]):
            with self.subTest(extra=extra):
                errors = io.StringIO()
                with mock.patch("urllib.request.urlopen", side_effect=no_network), \
                        contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
                    code = intel_main.main(["feed", "--follow", *extra])
                self.assertEqual(code, 2)
                self.assertEqual(errors.getvalue().strip(), "stopped: --follow takes neither --blocks nor --record")

    def test_follow_writes_the_event_block_and_points_the_event_source_at_it(self):
        raw = raw_block(9, [])
        synthetic = json.loads((BLOCKS / "synthetic-code-hash.json").read_text(encoding="utf-8"))
        fake = FakeFetcher({9: raw}, 9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("intel.feed.reduce_block", return_value=synthetic):
                feed.follow(fetcher=fake.inner, state_path=root / "state.json", events_dir=root / "events",
                            event_blocks_dir=root / "event-blocks", max_blocks=1, sleep=lambda _: None)
            event_path = next((root / "events").glob("*.json"))
            event = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertEqual(event["source"]["block_fixture"], "9.json")
            block_path = root / "event-blocks" / "9.json"
            self.assertTrue(block_path.is_file())
            self.assertEqual(event["source"]["block_fixture_sha256"], hashlib.sha256(block_path.read_bytes()).hexdigest())

    def test_large_transfer_signal_polls_each_labelled_subject_once_per_window_with_the_filters(self):
        import os

        rows = [transfer_row(subject, T0_MS - 10_000, index) for index, subject in enumerate(self.subjects)]
        rows += [transfer_row(subject, T0_MS + 400_000, 1000 + index) for index, subject in enumerate(self.subjects)]
        transfers = TransfersStub(rows)
        now, seen = [T0], {}

        class Timed(FakeFetcher):
            def get(self, path):
                answer = super().get(path)
                if path.startswith("/v0/block/"):
                    seen[path] = len(transfers.calls)
                    now[0] += 300
                return answer

        original_read_text = Path.read_text
        key_file = Path("C:/private/fastnear-test-key")
        reads = []

        def read_text(path, *args, **kwargs):
            if path == key_file:
                reads.append(path)
                return "literal-test-key"
            return original_read_text(path, *args, **kwargs)

        fake = Timed(self.blocks(4), 4)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"FASTNEAR_API_KEY_FILE": str(key_file)}), \
                mock.patch.object(Path, "read_text", read_text), \
                mock.patch("intel.feed.rpc.Client", return_value=transfers) as client:
            root = Path(tmp)
            state, walked = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                        events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                        max_blocks=3, sleep=lambda _: None, clock=lambda: now[0])
            events = self.transfer_events(root)
        count = len(self.subjects)
        self.assertEqual(walked, 3)
        self.assertEqual(len(reads), 1)
        self.assertEqual(client.call_args.args[:2], (feed.TRANSFERS_URL, "off"))
        self.assertEqual(client.call_args.kwargs["headers"], {"Authorization": "Bearer literal-test-key"})
        self.assertEqual((seen["/v0/block/1"], seen["/v0/block/2"], seen["/v0/block/3"]), (count, count, 2 * count))
        windows = [(T0_MS - 600_000, T0_MS), (T0_MS, T0_MS + 600_000)]
        for index, (path, body) in enumerate(transfers.calls):
            self.assertEqual(path, "/v0/transfers")
            from_ms, to_ms = windows[index // count]
            self.assertEqual(body, {"account_id": self.subjects[index % count], "direction": "sender",
                                    "from_timestamp_ms": from_ms, "to_timestamp_ms": to_ms, "min_usd_amount": 1000,
                                    "ignore_system": True, "limit": 100})
        self.assertEqual(len(transfers.calls), 2 * count)
        self.assertEqual(len(events), 2 * count)
        self.assertEqual(state["events"]["labelled_account_large_transfer"], 2 * count)
        self.assertEqual(state["last_large_transfer_poll_ms"], T0_MS + 600_000)
        event = events[0]
        self.assertTrue(all(event["signal"] == "labelled_account_large_transfer" for event in events))
        self.assertEqual({key: event[key] for key in ("subject", "other_account_id", "asset_id", "usd_amount",
                                                      "transaction_id", "receipt_id", "block_height")},
                         {"subject": self.subjects[0], "other_account_id": "recipient.near",
                          "asset_id": "native:near", "usd_amount": "1200.5", "transaction_id": "tx-0",
                          "receipt_id": "receipt-0", "block_height": 100})
        self.assertEqual(event["observed"], {"block_height": 100, "timestamp_ms": T0_MS - 10_000})
        self.assertEqual(event["labels"], feed.Context(labels.load()).labelled[self.subjects[0]])
        self.assertIn('"recipient.near"', event["detail"])
        self.assertNotIn("literal-test-key", json.dumps(state))

    def test_a_row_on_the_window_boundary_is_counted_once_and_a_second_page_is_fetched(self):
        subject = self.subjects[0]
        rows = [transfer_row(subject, T0_MS - 200_000, 1), transfer_row(subject, T0_MS - 100_000, 2),
                transfer_row(subject, T0_MS, 3), transfer_row(subject, T0_MS + 100_000, 4)]
        transfers = TransfersStub(rows, page_size=1)
        now = [T0]

        class Timed(FakeFetcher):
            def get(self, path):
                answer = super().get(path)
                if path == "/v0/block/1":
                    now[0] += feed.POLL_S
                return answer

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, _ = self.follow_transfers(root, Timed(self.blocks(2), 2), lambda: now[0], transfers, max_blocks=2)
            events = self.transfer_events(root)
        self.assertEqual([event["transaction_id"] for event in events], ["tx-1", "tx-2", "tx-3", "tx-4"])
        self.assertEqual(state["events"]["labelled_account_large_transfer"], 4)
        own = [body for _, body in transfers.calls if body["account_id"] == subject]
        self.assertEqual([(body["from_timestamp_ms"], body["to_timestamp_ms"], body.get("resume_token")) for body in own],
                         [(T0_MS - 600_000, T0_MS, None), (T0_MS - 600_000, T0_MS, "1"),
                          (T0_MS, T0_MS + 600_000, None), (T0_MS, T0_MS + 600_000, "1")])
        self.assertEqual(events[2]["observed"]["timestamp_ms"], T0_MS)

    def test_a_subject_past_the_page_cap_is_recorded_in_the_state(self):
        subject = self.subjects[0]
        transfers = TransfersStub([transfer_row(subject, T0_MS - 1000, 1)], page_size=1, endless=[subject])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, walked = self.follow_transfers(root, FakeFetcher(self.blocks(1), 1), lambda: T0, transfers)
            events = self.transfer_events(root)
        own = [body for _, body in transfers.calls if body["account_id"] == subject]
        self.assertEqual(len(own), feed.MAX_TRANSFER_PAGES)
        self.assertEqual(state["large_transfer_page_caps"],
                         [{"subject": subject, "from_timestamp_ms": T0_MS - 600_000, "to_timestamp_ms": T0_MS}])
        self.assertEqual([event["transaction_id"] for event in events], ["tx-1"])
        self.assertEqual(walked, 1)

    def test_a_caught_up_follower_still_polls_on_the_schedule(self):
        transfers = TransfersStub()
        now, waits = [T0], []

        def sleep(seconds):
            if seconds == 1:
                waits.append(seconds)
                now[0] += 300

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feed._write_json(root / "state.json", {**feed.new_state(), "last_height": 1, "blocks": 1,
                                                   "last_time": "2026-11-03T11:59:59Z"}, indent=2)
            state, walked = self.follow_transfers(root, FakeFetcher(self.blocks(1), 1), lambda: now[0], transfers,
                                                  max_blocks=None, sleep=sleep, stopped=lambda: len(waits) >= 3)
        self.assertEqual((walked, state["last_height"]), (0, 1))
        self.assertEqual(len(transfers.calls), 2 * len(self.subjects))
        self.assertEqual({(body["from_timestamp_ms"], body["to_timestamp_ms"]) for _, body in transfers.calls},
                         {(T0_MS - 600_000, T0_MS), (T0_MS, T0_MS + 600_000)})
        self.assertEqual(state["last_large_transfer_poll_ms"], T0_MS + 600_000)

    def test_a_transfers_error_never_stops_the_walk_and_the_window_is_asked_again(self):
        import urllib.error

        url = feed.TRANSFERS_URL + "/v0/transfers"
        cases = [("429 with Retry-After", urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "120"},
                                                                 None), 429, "HttpError", 120),
                 ("429", urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None), 429, "HttpError",
                  feed.TRANSFER_BACKOFF_S),
                 ("429 with a Retry-After past the cap", urllib.error.HTTPError(
                     url, 429, "Too Many Requests", {"Retry-After": "86400"}, None), 429, "HttpError", feed.POLL_S),
                 ("500", urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None), 500, "HttpError",
                  feed.POLL_S),
                 ("malformed JSON", http_reply(b"not json"), None, "JSONDecodeError", feed.POLL_S),
                 ("no transfers list", http_reply(b'{"transfers": null}'), None, "MalformedResponse",
                  feed.POLL_S)]
        for name, answer, status, error, wait_s in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                failing = {"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer}
                with mock.patch("urllib.request.urlopen", **failing) as opened:
                    state, walked = self.follow_transfers(root, FakeFetcher(self.blocks(2), 2), lambda: T0)
                self.assertEqual((walked, state["last_height"]), (1, 1))
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(opened.call_args.args[0].get_header("Authorization"), "Bearer literal-test-key")
                self.assertEqual(state["last_large_transfer_error"],
                                 {"at": "2026-11-03T12:00:00Z", "status": status, "error": error})
                self.assertEqual(state["large_transfer_next_poll_ms"], T0_MS + wait_s * 1000)
                self.assertEqual(state["last_large_transfer_poll_ms"], T0_MS - 600_000)
                self.assertEqual(state["transfer_requests"], 1)
                shown = "no HTTP status" if status is None else f"HTTP {status}"
                self.assertIn(f"labelled_account_large_transfer last failed poll at 2026-11-03T12:00:00Z: {error}, "
                              f"{shown}", feed.stats_lines(state))
                self.assertFalse((root / "events").exists())
                self.assertNotIn("literal-test-key", (root / "state.json").read_text(encoding="utf-8"))
                empty = http_reply(b'{"transfers": [], "resume_token": null}')
                with mock.patch("urllib.request.urlopen", return_value=empty) as opened:
                    state, walked = self.follow_transfers(root, FakeFetcher(self.blocks(2), 2),
                                                          lambda later=T0 + wait_s: later)
                first = json.loads(opened.call_args_list[0].args[0].data)
                self.assertEqual((first["from_timestamp_ms"], first["to_timestamp_ms"]),
                                 (T0_MS - 600_000, T0_MS + wait_s * 1000))
                self.assertEqual(opened.call_count, len(self.subjects))
                self.assertEqual((walked, state["last_height"], state["last_large_transfer_poll_ms"]),
                                 (1, 2, T0_MS + wait_s * 1000))
                self.assertEqual(state["transfer_requests"], 1 + len(self.subjects))

    def test_the_usd_amount_is_read_as_a_magnitude_and_an_unreadable_row_is_counted_not_raised(self):
        subject = self.subjects[0]
        rows = [{**transfer_row(subject, T0_MS - 50_000, 1), "usd_amount": -1500.25},
                {**transfer_row(subject, T0_MS - 40_000, 2), "usd_amount": 2000},
                {**transfer_row(subject, T0_MS - 30_000, 3), "usd_amount": -999.99},
                {**transfer_row(subject, T0_MS - 20_000, 4), "usd_amount": 500},
                {**transfer_row(subject, T0_MS - 10_000, 5), "usd_amount": None}]
        missing_usd, missing_receiver = transfer_row(subject, T0_MS - 5_000, 6), transfer_row(subject, T0_MS - 4_000, 7)
        del missing_usd["usd_amount"], missing_receiver["other_account_id"]
        transfers = TransfersStub(rows + [missing_usd, missing_receiver])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, walked = self.follow_transfers(root, FakeFetcher(self.blocks(1), 1), lambda: T0, transfers)
            events = self.transfer_events(root)
        self.assertEqual([(event["transaction_id"], event["usd_amount"], event["direction"]) for event in events],
                         [("tx-1", "1500.25", "outbound"), ("tx-2", "2000", "outbound")])
        self.assertEqual(events[0]["detail"],
                         f'FastNEAR reported a transfer of 1500.25 USD from {subject} to "recipient.near"')
        self.assertEqual((walked, state["skipped_rows"], state["events"]["labelled_account_large_transfer"]),
                         (1, 3, 2))
        self.assertEqual(state["last_large_transfer_poll_ms"], T0_MS)
        self.assertNotIn("last_large_transfer_error", state)
        self.assertEqual(events[1]["detail"],
                         f'FastNEAR reported a transfer of 2000 USD from {subject} to "recipient.near"')
        self.assertFalse(any(line.startswith("-") or " -" in line for line in feed.stats_lines(state)))

    def test_transfers_requests_are_paced_and_counted_apart_from_neardata(self):
        log = []
        transfers = TransfersStub()
        post = transfers.post

        def logged(path, body):
            log.append("post")
            return post(path, body)

        transfers.post = logged
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, _ = self.follow_transfers(root, FakeFetcher(self.blocks(1), 1), lambda: T0, transfers,
                                             sleep=log.append)
        count = len(self.subjects)
        self.assertEqual(log.count("post"), count)
        self.assertEqual(log[:2 * count - 1], ["post", feed.FOLLOW_INTERVAL_S] * (count - 1) + ["post"])
        self.assertEqual((state["transfer_requests"], state["requests"]), (len(self.subjects), 1))

    def test_large_transfer_signal_is_disabled_without_the_key_file_and_the_state_says_so(self):
        import os

        fake = FakeFetcher(self.blocks(1), 1)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("intel.feed.rpc.Client") as client:
            root = Path(tmp)
            state, _ = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                   events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                   max_blocks=1, sleep=lambda _: None, clock=lambda: 1793707200)
        self.assertEqual(state["disabled_signals"], {"labelled_account_large_transfer":
                                                      "FASTNEAR_API_KEY_FILE is not set"})
        self.assertIn("labelled_account_large_transfer disabled: FASTNEAR_API_KEY_FILE is not set",
                      feed.stats_lines(state))
        client.assert_not_called()

    def test_a_usable_key_goes_to_neardata_as_a_bearer_header_and_into_no_file(self):
        import os

        synthetic = json.loads((BLOCKS / "synthetic-code-hash.json").read_text(encoding="utf-8"))
        fixtures = sorted(path.name for path in BLOCKS.glob("*.json"))
        requests = []

        def answer(request, timeout):
            requests.append(request)
            if request.full_url.startswith(feed.TRANSFERS_URL):
                return http_reply(b'{"transfers": [], "resume_token": null}')
            height = 2 if request.full_url.endswith("/v0/last_block/final") else 1
            return http_reply(json.dumps(raw_block(height, [])).encode("utf-8"))

        for keyed in (True, False):
            requests.clear()
            with self.subTest(keyed=keyed), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch("urllib.request.urlopen", side_effect=answer), \
                    mock.patch("intel.feed.reduce_block", return_value=synthetic):
                root = Path(tmp)
                key_file = root / "fastnear.key"
                key_file.write_text("literal-test-key\n", encoding="utf-8")
                if keyed:
                    os.environ["FASTNEAR_API_KEY_FILE"] = str(key_file)
                else:
                    os.environ.pop("FASTNEAR_API_KEY_FILE", None)
                state, walked = feed.follow(start=1, state_path=root / "state.json", events_dir=root / "events",
                                            event_blocks_dir=root / "event-blocks", max_blocks=1,
                                            sleep=lambda _: None, clock=lambda: T0)
                neardata = [r for r in requests if r.full_url.startswith(feed.NEARDATA_URL)]
                self.assertEqual([r.full_url for r in neardata],
                                 [feed.NEARDATA_URL + "/v0/last_block/final", feed.NEARDATA_URL + "/v0/block/1"])
                self.assertEqual({r.get_header("Authorization") for r in neardata},
                                 {"Bearer literal-test-key" if keyed else None})
                self.assertEqual((walked, state["events"]["code_hash_matches_incident"]), (1, 1))
                written = [path for path in root.rglob("*") if path.is_file() and path != key_file]
                self.assertTrue(any(path.parent.name == "event-blocks" for path in written))
                for path in written:
                    self.assertNotIn(b"literal-test-key", path.read_bytes())
                    self.assertNotIn(b"Bearer", path.read_bytes())
        self.assertEqual(sorted(path.name for path in BLOCKS.glob("*.json")), fixtures)

    def test_large_transfer_signal_is_disabled_when_the_key_file_is_missing_unreadable_or_empty(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "empty.key").write_text(" \n", encoding="utf-8")
            (root / "binary.key").write_bytes(b"\xff\xfe\xfa")
            unreadable = "the file FASTNEAR_API_KEY_FILE names cannot be read"
            cases = [(root / "absent.key", unreadable), (root, unreadable), (root / "binary.key", unreadable),
                     (root / "empty.key", "the file FASTNEAR_API_KEY_FILE names is empty")]
            for index, (key_file, reason) in enumerate(cases):
                state_dir = root / f"state-{index}"
                with self.subTest(key_file=key_file.name), mock.patch.dict(
                        os.environ, {"FASTNEAR_API_KEY_FILE": str(key_file)}), \
                        mock.patch("intel.feed.rpc.Client") as client:
                    state, walked = feed.follow(start=1, fetcher=FakeFetcher(self.blocks(1), 1).inner,
                                                state_path=state_dir / "state.json", events_dir=state_dir / "events",
                                                event_blocks_dir=state_dir / "event-blocks", max_blocks=1,
                                                sleep=lambda _: None, clock=lambda: T0)
                    self.assertEqual(walked, 1)
                    self.assertEqual(state["disabled_signals"], {"labelled_account_large_transfer": reason})
                    self.assertIn(f"labelled_account_large_transfer disabled: {reason}", feed.stats_lines(state))
                    self.assertNotIn(key_file.name, (state_dir / "state.json").read_text(encoding="utf-8"))
                    client.assert_not_called()

    def test_follow_paces_and_backs_off(self):
        fake = FakeFetcher(self.blocks(2), 2, throttled=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json", events_dir=root / "events",
                        event_blocks_dir=root / "event-blocks", max_blocks=1, sleep=fake.sleeps.append,
                        clock=lambda: 1793284740.0)
        self.assertIn(feed.BACKOFF_S, fake.sleeps)
        self.assertIn(feed.FOLLOW_INTERVAL_S, fake.sleeps)

    def paced_log(self, end, keyed):
        """The neardata gets, Transfers posts and sleeps of a follow over heights 1 to end, in order, with
        an injected clock that each sleep moves on; keyed follows over a temp key file and a Transfers stub."""
        now, log = [T0], []

        class Logged(FakeFetcher):
            def get(self, path):
                log.append("get")
                return super().get(path)

        def sleep(seconds):
            log.append(seconds)
            now[0] += seconds

        fake = Logged(self.blocks(end), end)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if keyed:
                transfers = TransfersStub()
                post = transfers.post

                def logged(path, body):
                    log.append("post")
                    return post(path, body)

                transfers.post = logged
                state, walked = self.follow_transfers(root, fake, lambda: now[0], transfers, max_blocks=end,
                                                      sleep=sleep)
            else:
                state, walked = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                            events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                            max_blocks=end, sleep=sleep, clock=lambda: now[0])
        self.assertEqual((walked, state["last_height"]), (end, end))
        self.assertEqual(fake.sleeps, [])
        return log

    def test_a_keyed_follower_paces_neardata_at_a_tenth_of_a_second(self):
        log = self.paced_log(3, keyed=True)
        self.assertEqual(log[log.index("get"):], ["get", 0.1, "get", 0.1, "get"])

    def test_a_keyless_follower_paces_neardata_at_four_tenths_of_a_second(self):
        log = self.paced_log(3, keyed=False)
        self.assertEqual(log, ["get", 0.4, "get", 0.4, "get"])
        self.assertNotIn(feed.FOLLOW_INTERVAL_KEYED_S, log)

    def test_a_keyed_follower_keeps_the_transfers_poll_four_tenths_of_a_second_apart(self):
        log = self.paced_log(1, keyed=True)
        count = len(self.subjects)
        self.assertGreater(count, 1)
        self.assertEqual(log, ["post", 0.4] * (count - 1) + ["post", "get"])

    def test_a_restart_resumes_after_the_last_height(self):
        heights = self.blocks(4)
        fake = FakeFetcher(heights, 4)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "state.json"
            state, _ = feed.follow(start=1, fetcher=fake.inner, state_path=state_path, events_dir=root / "events",
                                   event_blocks_dir=root / "event-blocks", max_blocks=2, sleep=lambda _: None)
            state, walked = feed.follow(fetcher=fake.inner, state_path=state_path, events_dir=root / "events",
                                        event_blocks_dir=root / "event-blocks", max_blocks=2, sleep=lambda _: None)
            self.assertEqual((walked, state["last_height"], state["blocks"]), (2, 4, 4))

    def test_the_state_dir_variable_moves_the_state_and_the_api_reads_it(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "feed-state"
            fake = FakeFetcher(self.blocks(1), 1)
            with mock.patch.dict(os.environ, {"FEED_STATE_DIR": str(state_dir)}):
                state, _ = feed.follow(start=1, fetcher=fake.inner, max_blocks=1, sleep=lambda _: None)
                status, body = api.route("/feed", labels.load())
            self.assertEqual(status, 200)
            self.assertEqual(state["last_height"], 1)
            self.assertEqual(body["stats"]["mode"], "follow")
            self.assertNotIn("fresh_accounts", body["stats"])

    def test_the_snapshot_never_changes_the_recorded_window_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            (source / "events").mkdir(parents=True)
            (source / "event-blocks").mkdir()
            recorded_state = feed.load_state()
            feed._write_json(source / "state.json", recorded_state, indent=2)
            before = (source / "state.json").read_bytes()
            destination = root / "follow-snapshot.json"
            intel_main.write_feed_snapshot(source, destination)
            self.assertEqual((source / "state.json").read_bytes(), before)
            self.assertNotIn("fresh_accounts", json.loads(destination.read_text(encoding="utf-8")))

    def test_the_feed_snapshot_command_writes_the_snapshot_from_a_state_dir(self):
        synthetic = json.loads((BLOCKS / "synthetic-code-hash.json").read_text(encoding="utf-8"))
        fake = FakeFetcher({9: raw_block(9, [])}, 9)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "follower"
            with mock.patch("intel.feed.reduce_block", return_value=synthetic):
                feed.follow(fetcher=fake.inner, state_path=state_dir / "state.json", events_dir=state_dir / "events",
                            event_blocks_dir=state_dir / "event-blocks", max_blocks=1, sleep=lambda _: None)
            destination = root / "follow-snapshot.json"
            output = io.StringIO()
            with mock.patch.object(feed, "SNAPSHOT_PATH", destination), contextlib.redirect_stdout(output):
                code = intel_main.main(["feed-snapshot", "--from", str(state_dir)])
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), "snapshot written with 1 events and 1 event blocks\n")
            state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
            event = json.loads(next((state_dir / "events").glob("*.json")).read_text(encoding="utf-8"))
            snapshot = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(snapshot.pop("event_documents"), [event])
        self.assertEqual(snapshot.pop("event_blocks"), {"9.json": synthetic})
        del state["fresh_accounts"]
        self.assertEqual(snapshot, state)
        self.assertEqual(snapshot["mode"], "follow")

    def test_the_state_write_replaces_the_file_whole_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            feed._write_json(path, {"blocks": 1}, indent=2)
            feed._write_json(path, {"blocks": 2}, indent=2)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["state.json"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"blocks": 2})
            write_bytes = Path.write_bytes

            def torn(self, data):
                write_bytes(self, data[:5])
                raise OSError("disk full")

            with mock.patch.object(Path, "write_bytes", torn), self.assertRaises(OSError):
                feed._write_json(path, {"blocks": 3}, indent=2)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"blocks": 2})

    def test_the_api_answers_503_while_the_state_file_does_not_parse(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state.json").write_text('{"blocks": 1', encoding="utf-8")
            with mock.patch.dict(os.environ, {"FEED_STATE_DIR": tmp}):
                self.assertEqual(api.route("/feed", labels.load()), (503, {"error": "feed state unreadable"}))


if __name__ == "__main__":
    unittest.main()
