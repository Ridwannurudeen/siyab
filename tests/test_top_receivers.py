import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import __main__ as intel_main
from intel import labels, top_receivers

ROOT = Path(__file__).resolve().parent.parent
SET_PATH = ROOT / "fixtures" / "receivers" / "set.json"
JUDGEMENTS = ROOT / "fixtures" / "receivers" / "judgements"


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def synthetic_block(height, timestamp_ns, receipts=()):
    return {"synthetic": True, "block": {"header": {"height": height, "timestamp_nanosec": str(timestamp_ns)}},
            "shards": [{"receipt_execution_outcomes": list(receipts)}]}


def executed(predecessor, receiver, actions=(), logs=(), failed=False):
    status = {"Failure": "synthetic failure"} if failed else {"SuccessValue": ""}
    return {"receipt": {"receipt_id": f"receipt-{predecessor}-{receiver}", "predecessor_id": predecessor,
                        "receiver_id": receiver, "receipt": {"Action": {"actions": list(actions)}}},
            "execution_outcome": {"outcome": {"status": status, "logs": list(logs)}}}


def event_log(value):
    return "EVENT_JSON:" + json.dumps(value, separators=(",", ":"))


class FakeFetcher:
    def __init__(self, blocks, fail_once=()):
        self.blocks = blocks
        self.fail_once = set(fail_once)
        self.requests = 0
        self.calls = []

    def get(self, path):
        self.requests += 1
        self.calls.append(path)
        if path.startswith("/v0/block/"):
            height = int(path.rsplit("/", 1)[1])
            if height in self.fail_once and self.calls.count(path) > 1:
                self.fail_once.remove(height)
                raise RuntimeError("synthetic interruption")
            return self.blocks.get(height)
        raise AssertionError(f"unexpected endpoint {path}")


class ReducerTest(unittest.TestCase):
    def test_synthetic_block_keeps_native_and_nep141_and_separates_intents(self):
        nep141 = event_log({"standard": "nep141", "event": "ft_transfer", "data": [
            {"old_owner_id": "ft-payer.near", "new_owner_id": "merchant.near", "amount": "11"},
            {"old_owner_id": "same.near", "new_owner_id": "same.near", "amount": "12"}]})
        nep245 = event_log({"standard": "nep245", "event": "mt_transfer", "data": [
            {"old_owner_id": "intent-payer.near", "new_owner_id": "intent-recipient.near",
             "token_ids": ["nep141:wrap.near"], "amounts": ["13"]}]})
        block = synthetic_block(7, 123, [
            executed("native-payer.near", "merchant.near", [{"Transfer": {"deposit": "5"}}]),
            executed("system", "native-payer.near", [{"Transfer": {"deposit": "99"}}]),
            executed("same.near", "same.near", [{"Transfer": {"deposit": "7"}}]),
            executed("zero-payer.near", "merchant.near", [{"Transfer": {"deposit": "0"}}]),
            executed("failed-payer.near", "merchant.near", [{"Transfer": {"deposit": "17"}}],
                     failed=True),
            executed("ft-contract.near", "token.near", logs=[nep141]),
            executed("intents.near", "intents.near", logs=[nep245]),
        ])

        reduced = top_receivers.reduce_block(block)

        self.assertEqual(reduced["height"], 7)
        self.assertEqual(reduced["timestamp_ns"], "123")
        self.assertEqual(reduced["transfers"], [
            {"payer": "native-payer.near", "receiver": "merchant.near", "asset": "native:near",
             "amount": "5"},
            {"payer": "ft-payer.near", "receiver": "merchant.near", "asset": "token.near", "amount": "11"},
        ])
        self.assertEqual(reduced["intents"], [
            {"payer": "intent-payer.near", "receiver": "intent-recipient.near",
             "asset": "nep141:wrap.near", "amount": "13"},
        ])


class RankingTest(unittest.TestCase):
    def test_ties_are_deterministic_and_intents_do_not_join_the_ranking(self):
        transfers = []
        for receiver, payers in (
            ("z.near", ["z1.near", "z2.near", "z3.near"]),
            ("b.near", ["b1.near", "b1.near", "b1.near", "b2.near"]),
            ("d.near", ["d1.near", "d1.near", "d1.near", "d2.near"]),
            ("a.near", ["a1.near", "a1.near", "a2.near"]),
        ):
            transfers.extend({"payer": payer, "receiver": receiver, "asset": "native:near", "amount": "2"}
                             for payer in payers)
        batch = {"start_height": 4, "end_height": 4, "heights": [
            {"height": 4, "timestamp_ns": "400", "transfers": transfers,
             "intents": [{"payer": "intent-payer.near", "receiver": "intents-only.near",
                          "asset": "nep141:wrap.near", "amount": "23"}]}]}
        state = {"start_height": 4, "end_height": 4, "last_height": 4, "hours": 1,
                 "start_timestamp_ns": "400", "end_timestamp_ns": "400", "request_count": 1}

        with tempfile.TemporaryDirectory() as tmp:
            batch_dir = Path(tmp) / "batches"
            batch_dir.mkdir()
            (batch_dir / "4-4.json").write_text(json.dumps(batch), encoding="utf-8")
            document = top_receivers.build_ranking(
                tmp, state, "synthetic rule", set(), set(), "synthetic exclusions", 2, now_ms=0)

        self.assertEqual([entry["subject"] for entry in document["subjects"]],
                         ["z.near", "b.near", "d.near", "a.near"])
        self.assertEqual(document["subjects"][0]["distinct_payers"], 3)
        self.assertEqual(document["subjects"][1]["transfer_count"], 4)
        self.assertEqual(document["subjects"][1]["asset_totals"], {"native:near": "8"})
        self.assertTrue(document["subjects"][0]["age_recorded"])
        self.assertFalse(document["subjects"][2]["age_recorded"])
        self.assertEqual(document["heights_fetched"], 1)
        self.assertEqual(document["intents_cohort"]["transfer_count"], 1)
        self.assertEqual(document["intents_cohort"]["receivers"][0]["subject"], "intents-only.near")
        self.assertNotIn("intents-only.near", [entry["subject"] for entry in document["subjects"]])
        self.assertEqual(len(document["reduction_sha256"]), 64)

    def test_named_exclusions_do_not_get_replaced_after_the_top_limit(self):
        transfers = [{"payer": f"payer-{index}.near", "receiver": receiver, "asset": "native:near",
                      "amount": "1"}
                     for index, receiver in enumerate(("excluded.near", "kept.near", "outside.near"))]
        batch = {"start_height": 1, "end_height": 1, "heights": [
            {"height": 1, "timestamp_ns": "100", "transfers": transfers, "intents": []}]}
        state = {"start_height": 1, "end_height": 1, "last_height": 1, "hours": 1,
                 "start_timestamp_ns": "100", "end_timestamp_ns": "100", "request_count": 1}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(top_receivers, "RANK_LIMIT", 2):
            batch_dir = Path(tmp) / "batches"
            batch_dir.mkdir()
            (batch_dir / "1-1.json").write_text(json.dumps(batch), encoding="utf-8")
            document = top_receivers.build_ranking(
                tmp, state, "synthetic rule", set(), {"excluded.near"}, "synthetic exclusions", 80, now_ms=0)

        self.assertEqual([entry["subject"] for entry in document["subjects"]], ["kept.near"])
        self.assertEqual(document["subjects"][0]["rank"], 2)
        self.assertEqual(document["exclusions"]["named_accounts_excluded_after_limit"], 1)


class ScanTest(unittest.TestCase):
    def test_bisection_finds_first_height_at_or_after_start_with_null_heights(self):
        hour = 60 * 60 * 1_000_000_000
        blocks = {0: synthetic_block(0, 0), 1: synthetic_block(1, 10 * hour),
                  2: None, 3: synthetic_block(3, 30 * hour), 4: synthetic_block(4, 40 * hour)}

        start, timestamp_ns = top_receivers.bisect_start_height(
            4, 40 * hour, 25 * hour, blocks.get)

        self.assertEqual((start, timestamp_ns), (3, 30 * hour))

    def test_scan_resumes_at_the_checkpoint_without_refetching_completed_heights(self):
        hour = 60 * 60 * 1_000_000_000
        blocks = {0: synthetic_block(0, 0), 1: synthetic_block(1, 5 * hour),
                  2: synthetic_block(2, 9 * hour), 3: synthetic_block(3, 10 * hour),
                  4: None, 5: synthetic_block(5, 20 * hour)}
        now = [1000.0]

        def clock():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(top_receivers, "BATCH_SIZE", 2):
            interrupted = FakeFetcher(blocks, fail_once={5})
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                top_receivers.scan_window(10, 5, True, tmp, interrupted, sleep, clock)
            checkpoint = json.loads((Path(tmp) / "checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["start_height"], 3)
            self.assertEqual(checkpoint["last_height"], 4)

            resumed = FakeFetcher(blocks)
            state = top_receivers.scan_window(10, 5, True, tmp, resumed, sleep, clock)

            self.assertEqual(resumed.calls, ["/v0/block/5"])
            self.assertEqual(state["last_height"], 5)
            self.assertEqual(state["heights_fetched"], 2)
            self.assertEqual(state["skipped_heights"], [4])
            batch = json.loads((Path(tmp) / "batches" / "3-4.json").read_text(encoding="utf-8"))
            self.assertNotIn("block", batch["heights"][0])
            self.assertNotIn("shards", batch["heights"][0])


class FlakyFetcher:
    def __init__(self, errors, block):
        self.errors = list(errors)
        self.block = block
        self.requests = 0

    def get(self, path):
        self.requests += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.block


class BlockReaderRetryTest(unittest.TestCase):
    def reader(self, fetcher):
        pauses = []
        now = [0.0]

        def sleep(seconds):
            pauses.append(seconds)
            now[0] += seconds

        get, _, _ = top_receivers._block_reader(fetcher, sleep, lambda: now[0])
        return get, pauses

    def test_a_dropped_connection_is_retried_after_its_pause(self):
        block = synthetic_block(7, 0)
        fetcher = FlakyFetcher([rpc.TransportError("IncompleteRead from x"), rpc.HttpError(503, "x")], block)
        get, pauses = self.reader(fetcher)
        self.assertEqual(get("/v0/block/7"), block)
        self.assertEqual(fetcher.requests, 3)
        self.assertIn(top_receivers.TRANSIENT_BACKOFF_S[0], pauses)
        self.assertIn(top_receivers.TRANSIENT_BACKOFF_S[1], pauses)

    def test_a_failure_that_outlasts_every_pause_stops_the_scan(self):
        attempts = len(top_receivers.TRANSIENT_BACKOFF_S) + 1
        fetcher = FlakyFetcher([rpc.TransportError("x")] * attempts, synthetic_block(7, 0))
        get, _ = self.reader(fetcher)
        with self.assertRaises(rpc.TransportError):
            get("/v0/block/7")
        self.assertEqual(fetcher.requests, attempts)

    def test_a_client_error_is_not_retried_and_a_404_is_a_skipped_height(self):
        get, _ = self.reader(FlakyFetcher([rpc.HttpError(400, "x")], None))
        with self.assertRaises(rpc.HttpError):
            get("/v0/block/7")
        fetcher = FlakyFetcher([rpc.HttpError(404, "x")], None)
        get, _ = self.reader(fetcher)
        self.assertIsNone(get("/v0/block/7"))
        self.assertEqual(fetcher.requests, 1)


class GateSpacingTest(unittest.TestCase):
    def test_workers_share_one_gate_so_request_starts_stay_spaced(self):
        class Recorder:
            requests = 0

            def get(self, path):
                return synthetic_block(int(path.rsplit("/", 1)[1]), 0)

        get, last_booked, _ = top_receivers._block_reader(Recorder(), lambda seconds: None, lambda: 0.0)
        with ThreadPoolExecutor(max_workers=top_receivers.SCAN_WORKERS) as pool:
            list(pool.map(lambda h: get(f"/v0/block/{h}"), range(30)))
        self.assertAlmostEqual(last_booked(), 29 * top_receivers.REQUEST_INTERVAL_S)


class ReceiverFixtureTest(unittest.TestCase):
    def test_named_exclusions_match_the_recorded_set(self):
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            excluded = intel_main.top_receiver_exclusions(labels.load())
        document = json.loads(SET_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(excluded), 18)
        self.assertEqual(document["exclusion"], intel_main.EXCLUSION.format(len(excluded)))

    def test_judging_writes_to_its_own_directory_and_preserves_the_84_files(self):
        document = json.loads(SET_PATH.read_text(encoding="utf-8"))
        entry = document["subjects"][0]
        before = {path.name: path.read_bytes() for path in JUDGEMENTS.glob("*.json")}
        single = {"judged_at_ms": document["judged_at_ms"], "subjects": [entry]}

        with tempfile.TemporaryDirectory() as tmp, mock.patch("urllib.request.urlopen", side_effect=no_network):
            results = intel_main.judge_receivers(single, labels.load(), Path(tmp) / "top")
            written = Path(tmp) / "top" / "judgements" / f"{entry['subject']}.json"
            self.assertEqual(written.read_bytes(), (JUDGEMENTS / written.name).read_bytes())

        after = {path.name: path.read_bytes() for path in JUDGEMENTS.glob("*.json")}
        self.assertEqual(after, before)
        self.assertEqual(len(results), 1)

    def test_coverage_line_includes_denominators_and_each_unknown_cause(self):
        document = {"subjects": [{}, {}, {}]}
        results = [
            {"verdict": "UNKNOWN", "checks": [
                {"id": "account_age", "status": "unknown"},
                {"id": "receiver_exists", "status": "unknown"}]},
            {"verdict": "UNKNOWN", "checks": [{"id": "account_age", "status": "unknown"}]},
            {"verdict": "ALLOW", "checks": []},
        ]

        line = intel_main.top_receivers_coverage_line(document, results)

        self.assertEqual(line, "Top receiver coverage: judged 3/3; ALLOW 1/3; DENY 0/3; "
                         "UNKNOWN 2/3; UNKNOWN causes: account_age 2, receiver_exists 1.")


if __name__ == "__main__":
    unittest.main()
