import json
import tempfile
import unittest
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import feed, labels
from tests.test_feed import FakeFetcher, T0, T0_MS, TransfersStub, raw_block, transfer_row


class IncrementalTransfersTest(unittest.TestCase):
    def test_a_late_429_retries_only_that_subject_while_blocks_continue(self):
        subjects = sorted(feed.Context(labels.load()).deny_receivers)
        now, calls, before_retry = [T0], [], []

        class Transfers(TransfersStub):
            def post(self, path, body):
                calls.append(dict(body))
                if len(calls) == 19:
                    raise rpc.HttpError(429, path, retry_after="1200")
                return super().post(path, body)

        class Blocks(FakeFetcher):
            def get(self, path):
                if path == "/v0/block/25":
                    before_retry.append((len(calls), len(list((root / "events").glob("*.json")))))
                    now[0] += 1201
                return super().get(path)

        client = Transfers([transfer_row(subject, T0_MS - 1000, index)
                            for index, subject in enumerate(subjects)])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("intel.feed.rpc.Client", return_value=client):
            root = Path(tmp)
            blocks = Blocks({height: raw_block(height, []) for height in range(1, 41)}, 40)
            state, walked = feed.follow(start=1, fetcher=blocks.inner, state_path=root / "state.json",
                                        events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                        max_blocks=40, clock=lambda: now[0],
                                        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
            events = [json.loads(path.read_text()) for path in (root / "events").glob("*.json")]
        self.assertEqual((walked, state["last_height"]), (40, 40))
        self.assertEqual(before_retry, [(19, 0)])
        self.assertEqual([body["account_id"] for body in calls[:24]], subjects[:19] + subjects[18:])
        self.assertTrue(all(body["to_timestamp_ms"] == T0_MS for body in calls[:24]))
        self.assertTrue(all(body["from_timestamp_ms"] == T0_MS for body in calls[24:]))
        self.assertEqual(len(events), len(subjects))
        self.assertEqual(state["last_large_transfer_poll_ms"], T0_MS)
        self.assertGreaterEqual(state["last_large_transfer_completed_ms"], T0_MS + 1_200_000)

    def test_one_step_never_drains_a_batch_or_advances_an_incomplete_window(self):
        client = TransfersStub()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("intel.feed.rpc.Client", return_value=client):
            root = Path(tmp)
            blocks = FakeFetcher({1: raw_block(1, [])}, 1)
            state, walked = feed.follow(start=1, fetcher=blocks.inner, state_path=root / "state.json",
                                        events_dir=root / "events", event_blocks_dir=root / "event-blocks",
                                        max_blocks=1, clock=lambda: T0, sleep=lambda _: None)
        self.assertEqual((walked, len(client.calls)), (1, 1))
        self.assertEqual(state["last_large_transfer_poll_ms"], T0_MS - 600_000)
        self.assertNotIn("last_large_transfer_completed_ms", state)


class PollStepTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = feed.Context(labels.load())
        self.subject = sorted(self.context.deny_receivers)[0]
        self.context.deny_receivers = {self.subject}
        self.state = {**feed.new_state(), "requests": 0, "transfer_requests": 0}
        self.pending = {}
        self.now = T0

    def step(self, client):
        feed.poll_transfers(client, self.state, self.root / "state.json", self.context,
                            self.root / "events", lambda: self.now, self.pending)

    def test_pagination_retries_the_same_token_after_an_http_date_without_publishing_twice(self):
        calls = []
        deadline = format_datetime(datetime.fromtimestamp(T0 + 1200, UTC), usegmt=True)

        class Transfers(TransfersStub):
            def post(inner, path, body):
                calls.append(dict(body))
                if len(calls) == 2:
                    raise rpc.HttpError(429, path, retry_after=deadline)
                return super().post(path, body)

        client = Transfers([transfer_row(self.subject, T0_MS - n * 1000, n) for n in (1, 2, 3)], page_size=1)
        self.step(client)
        self.assertFalse((self.root / "events").exists())
        self.now += 1
        self.step(client)
        self.assertEqual(self.state["large_transfer_next_poll_ms"], T0_MS + 1_200_000)
        self.now = T0 + 1199
        self.step(client)
        self.assertEqual(len(calls), 2)
        self.now = T0 + 1200
        self.step(client)
        self.assertFalse((self.root / "events").exists())
        self.now += 1
        self.step(client)
        self.assertEqual([body.get("resume_token") for body in calls], [None, "1", "1", "2"])
        self.assertEqual(len(list((self.root / "events").glob("*.json"))), 3)
        self.assertEqual(self.state["events"][feed.SIGNALS[3]], 3)
        self.assertEqual(self.state["last_large_transfer_poll_ms"], T0_MS)

    def test_restart_replays_from_the_persisted_initial_anchor_then_the_completed_cursor(self):
        self.context.deny_receivers = set(sorted(feed.Context(labels.load()).deny_receivers)[:2])
        client = TransfersStub()
        self.step(client)
        anchor = T0_MS - 600_000
        self.assertEqual(self.state["last_large_transfer_poll_ms"], anchor)
        self.assertNotIn("last_large_transfer_completed_ms", self.state)
        self.state = feed.load_state(self.root / "state.json")
        self.pending = {}
        self.now += 60
        self.step(client)
        self.assertEqual(client.calls[-1][1]["from_timestamp_ms"], anchor)
        self.now += 1
        self.step(client)
        self.assertEqual(self.state["last_large_transfer_poll_ms"], T0_MS + 60_000)
        self.assertEqual(self.state["last_large_transfer_completed_ms"], T0_MS + 61_000)
        self.state = feed.load_state(self.root / "state.json")
        self.pending = {}
        self.now += 600
        self.step(client)
        self.assertEqual(client.calls[-1][1]["from_timestamp_ms"], T0_MS + 60_000)
        self.assertNotIn("rows", self.state)

    def test_transfer_request_accounting_resets_on_the_actual_request_day(self):
        self.now = datetime(2026, 11, 4, tzinfo=UTC).timestamp() - 1
        client = TransfersStub([transfer_row(self.subject, int(self.now * 1000) - 1000, n) for n in (1, 2)],
                               page_size=1)
        self.step(client)
        self.assertEqual((self.state["request_day"], self.state["transfer_requests"]), ("2026-11-03", 1))
        self.now += 2
        self.step(client)
        self.assertEqual((self.state["request_day"], self.state["transfer_requests"]), ("2026-11-04", 1))

    def test_retry_after_seconds_start_when_the_slow_error_response_arrives(self):
        client = TransfersStub()

        def refuse(path, body):
            self.now += 30
            raise rpc.HttpError(429, path, retry_after="1200")

        with mock.patch.object(client, "post", side_effect=refuse):
            self.step(client)
        self.assertEqual(self.state["large_transfer_next_poll_ms"], T0_MS + 1_230_000)

    def test_the_initial_anchor_is_durable_before_the_first_network_request(self):
        client = TransfersStub()

        def answer(path, body):
            self.assertTrue((self.root / "state.json").is_file())
            disk = feed.load_state(self.root / "state.json")
            self.assertEqual(disk["last_large_transfer_poll_ms"], T0_MS - 600_000)
            self.assertNotIn("last_large_transfer_completed_ms", disk)
            return {"transfers": [], "resume_token": None}

        with mock.patch.object(client, "post", side_effect=answer):
            self.step(client)


if __name__ == "__main__":
    unittest.main()
