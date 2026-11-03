import base64
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import evidence, rpc, wasm
from intel import __main__ as intel_main
from intel import api, feed, labels

ROOT = Path(__file__).resolve().parent.parent
BLOCKS = ROOT / "fixtures" / "feed" / "blocks"
SYNTHETIC = {"synthetic-code-hash.json": "code_hash_matches_incident",
             "synthetic-fresh-dust.json": "fresh_dust_funded_pays_labelled",
             "synthetic-watch-addkey.json": "watch_account_key_added"}
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
                context = self.context
                if signal == "fresh_dust_funded_pays_labelled":
                    block["receipts"][1]["receiver_id"] = next(iter(context.deny_receivers))
                events = feed.signals(block, feed.new_state(), context, {"url": "test"})
                self.assertEqual([event["signal"] for event in events], [signal])
                self.assertTrue(events[0]["detail"])
                for assertion_id in events[0]["labels"]:
                    self.assertIn(assertion_id, {a.id for a in self.store.assertions})

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
        block = json.loads((BLOCKS / "synthetic-fresh-dust.json").read_text(encoding="utf-8"))
        creation, payment = block["receipts"]
        state = feed.new_state()
        self.assertEqual(self.events(dict(block, receipts=[creation]), state), [])
        self.assertIn(creation["receiver_id"], state["fresh_accounts"])
        later = dict(block, receipts=[payment], timestamp_ns=str(int(block["timestamp_ns"]) + 25 * 3600 * 10**9))
        self.assertEqual(self.events(later, state), [])
        self.assertEqual(state["fresh_accounts"], {})

    def test_dust_signal_needs_an_incident_or_denylist_receiver(self):
        block = json.loads((BLOCKS / "synthetic-fresh-dust.json").read_text(encoding="utf-8"))
        context = feed.Context(self.store)
        self.assertEqual(feed.signals(block, feed.new_state(), context, {"url": "test"}), [])
        block["receipts"][1]["receiver_id"] = next(iter(context.deny_receivers))
        self.assertEqual([event["signal"] for event in feed.signals(
            block, feed.new_state(), context, {"url": "test"})], ["fresh_dust_funded_pays_labelled"])

    def test_a_failed_receipt_raises_nothing(self):
        block = json.loads((BLOCKS / "synthetic-code-hash.json").read_text(encoding="utf-8"))
        block["receipts"][0]["ok"] = False
        self.assertEqual(self.events(block), [])


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
    def blocks(self, end):
        return {height: raw_block(height, []) for height in range(1, end + 1)}

    def test_follow_walks_to_the_final_height_without_writing_block_fixtures(self):
        fake = FakeFetcher(self.blocks(3), 3)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state, walked = feed.follow(start=1, fetcher=fake.inner, state_path=root / "state.json",
                                        events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                        max_blocks=3, sleep=lambda _: None)
            self.assertEqual((walked, state["last_height"], state["blocks"]), (3, 3, 3))
            self.assertEqual(list(root.glob("*.json")), [root / "state.json"])
            self.assertEqual(state["mode"], "follow")
            self.assertEqual(state["requests"], 4)

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
                self.assertEqual(state["requests"], 4)
                self.assertEqual(json.loads((root / "state.json").read_text(encoding="utf-8")), state)
                self.assertFalse((root / "events").exists())
                self.assertFalse((root / "event-blocks").exists())

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

    def test_follow_paces_and_backs_off(self):
        fake = FakeFetcher(self.blocks(1), 1, throttled=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feed.follow(fetcher=fake.inner, state_path=root / "state.json", events_dir=root / "events",
                        event_blocks_dir=root / "event-blocks", max_blocks=1, sleep=fake.sleeps.append,
                        clock=lambda: 1793284740.0)
        self.assertIn(feed.BACKOFF_S, fake.sleeps)
        self.assertIn(feed.FOLLOW_INTERVAL_S, fake.sleeps)

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
