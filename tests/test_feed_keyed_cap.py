"""The follower's real transport across keyed budget and cooldown transitions."""

import os
import sqlite3
import unittest
import urllib.error
from contextlib import closing
from unittest import mock

from gate import rpc
from intel import budget, feed, first_seen
from tests import test_budget, test_feed


class KeyedFeedTest(test_budget.BudgetCase):
    def setUp(self):
        super().setUp()
        self.root = self.path.parent / "feed"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key_file = self.path.parent / "synthetic.key"
        self.key_file.write_text("unit-only-token\n", encoding="utf-8")
        environment = mock.patch.dict(os.environ, {"FASTNEAR_API_KEY_FILE": str(self.key_file)}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        self.now = test_budget.T0
        self.sent, self.waits = [], []
        self.final = 5
        self.failures = {}
        self.throttled_until = {}
        self.after_request = lambda request: None

        def answer(request, timeout):
            if request.full_url.startswith(feed.TRANSFERS_URL):
                self.assertIsNone(request.get_header("Authorization"))
                return test_budget.reply({"transfers": [], "resume_token": None})
            self.assertTrue(request.full_url.startswith(feed.NEARDATA_URL))
            self.sent.append((request.full_url.removeprefix(feed.NEARDATA_URL),
                              request.get_header("Authorization"), self.now))
            path = self.sent[-1][0]
            failures = self.failures.get(path, [])
            if failures:
                status = failures.pop(0)
                raise urllib.error.HTTPError(request.full_url, status, "synthetic refusal", {}, None)
            if (request.get_header("Authorization") is None
                    and self.now < self.throttled_until.get(path, 0)):
                raise urllib.error.HTTPError(request.full_url, 429, "synthetic throttle", {}, None)
            height = self.final if path == "/v0/last_block/final" else int(path.rsplit("/", 1)[1])
            self.after_request(request)
            return test_budget.reply(test_feed.raw_block(height, []))

        opened = mock.patch("urllib.request.urlopen", side_effect=answer)
        opened.start()
        self.addCleanup(opened.stop)

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def walk(self, count=5, start=1):
        return feed.follow(start=start, state_path=self.root / "state.json", events_dir=self.root / "events",
                           event_blocks_dir=self.root / "event-blocks", max_blocks=count,
                           sleep=self.sleep, clock=lambda: self.now)

    def assert_walk(self, state, walked, requests=5):
        self.assertEqual((walked, state["last_height"], state["blocks"], state["skipped_heights"]),
                         (5, 5, 5, []))
        self.assertEqual(state["requests"], requests)
        self.assertEqual(feed.load_state(self.root / "state.json"), state)
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(1, 5)])
        paths = [path for path, _, _ in self.sent]
        for height in range(1, 5):
            self.assertEqual(paths.count(f"/v0/block/{height}"), 1)
        self.assertNotIn("/v0/block/5", paths)

    def test_under_the_cap_sends_the_key_and_uses_keyed_pacing(self):
        state, walked = self.walk()
        self.assert_walk(state, walked)
        self.assertEqual([key for _, key, _ in self.sent], ["Bearer unit-only-token"] * 5)
        self.assertEqual(self.count("keyed:feed:*", budget.DAY_S), 5)
        self.assertEqual(self.count("pooled", budget.DAY_S), 0)
        self.assertEqual(self.count("cumulative", 0), 5)
        for before, after in zip(self.sent, self.sent[1:]):
            self.assertAlmostEqual(after[2] - before[2], feed.FOLLOW_INTERVAL_KEYED_S, places=5)

    def test_the_120000th_keyed_read_is_allowed_then_the_same_walk_goes_keyless(self):
        self.seed("keyed:feed:*", budget.DAY_S, 119998)
        state, walked = self.walk()
        self.assert_walk(state, walked)
        self.assertEqual([key for _, key, _ in self.sent], ["Bearer unit-only-token"] * 2 + [None] * 3)
        self.assertEqual(self.count("keyed:feed:*", budget.DAY_S), 120000)
        self.assertEqual(self.count("keyless:feed:neardata", budget.DAY_S), 3)
        for before, after in zip(self.sent[1:], self.sent[2:]):
            self.assertAlmostEqual(after[2] - before[2], feed.FOLLOW_INTERVAL_S, places=5)

    def test_a_spent_cap_keeps_walking_keylessly_without_skipping_or_refetching_a_height(self):
        self.seed("keyed:feed:*", budget.DAY_S, 120000)
        state, walked = self.walk()
        self.assert_walk(state, walked)
        self.assertEqual([key for _, key, _ in self.sent], [None] * 5)
        self.assertEqual(self.count("keyed:feed:*", budget.DAY_S), 120000)
        self.assertEqual(self.count("keyless:feed:neardata", budget.DAY_S), 5)

    def test_a_keyed_402_retries_the_same_height_and_the_next_height_is_keyless(self):
        self.failures["/v0/block/2"] = [402]
        state, walked = self.walk()
        self.assertEqual((walked, state["last_height"], state["blocks"], state["requests"]), (5, 5, 5, 6))
        self.assertEqual(self.sent[:4], [("/v0/last_block/final", "Bearer unit-only-token", test_budget.T0),
                                         ("/v0/block/1", "Bearer unit-only-token", self.sent[1][2]),
                                         ("/v0/block/2", "Bearer unit-only-token", self.sent[2][2]),
                                         ("/v0/block/2", None, self.sent[3][2])])
        self.assertEqual([key for _, key, _ in self.sent[3:]], [None] * 3)
        self.assertEqual(self.count("keyed:feed:*", budget.DAY_S), 3)
        self.assertEqual(self.count("keyless:feed:neardata", budget.DAY_S), 3)
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(1, 5)])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM key_cooldown").fetchone(), (402,))
        self.assertIn("key_cooldown", self.alerts())

    def test_a_shared_cooldown_strips_the_key_and_its_exact_expiry_restores_it(self):
        budget.key_failure(402, self.now)
        self.now += budget.KEY_COOLDOWN_S - 1
        state, walked = self.walk(count=1)
        self.assertEqual((walked, state["last_height"]), (1, 1))
        self.assertTrue(all(key is None for _, key, _ in self.sent))
        self.now = test_budget.T0 + budget.KEY_COOLDOWN_S
        self.sent.clear()
        state, walked = self.walk(count=1, start=None)
        self.assertEqual((walked, state["last_height"]), (1, 2))
        self.assertEqual([key for _, key, _ in self.sent], ["Bearer unit-only-token"] * 2)

    def test_cooldown_expiry_inside_a_walk_restores_the_key_without_a_height_gap(self):
        self.failures["/v0/block/2"] = [402]

        def advance(request):
            if request.full_url.endswith("/v0/block/2") and request.get_header("Authorization") is None:
                self.now += budget.KEY_COOLDOWN_S

        self.after_request = advance
        state, walked = self.walk()
        self.assertEqual((walked, state["last_height"], state["blocks"], state["requests"]), (5, 5, 5, 6))
        self.assertEqual([(path, key) for path, key, _ in self.sent], [
            ("/v0/last_block/final", "Bearer unit-only-token"), ("/v0/block/1", "Bearer unit-only-token"),
            ("/v0/block/2", "Bearer unit-only-token"), ("/v0/block/2", None),
            ("/v0/block/3", "Bearer unit-only-token"), ("/v0/block/4", "Bearer unit-only-token")])
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(1, 5)])

    def test_401_falls_back_for_one_request_without_starting_a_shared_cooldown(self):
        self.failures["/v0/block/2"] = [401]
        state, walked = self.walk()
        self.assertEqual((walked, state["last_height"], state["requests"]), (5, 5, 6))
        self.assertEqual([key for _, key, _ in self.sent],
                         ["Bearer unit-only-token"] * 3 + [None] + ["Bearer unit-only-token"] * 2)
        self.assertNotIn("key_cooldown", self.alerts())

    def test_a_throttled_keyless_retry_restores_the_key_when_cooldown_expires_on_the_same_height(self):
        self.failures["/v0/block/2"] = [402]
        self.throttled_until["/v0/block/2"] = self.now + budget.KEY_COOLDOWN_S + 10
        state, walked = self.walk()
        same_height = [(key, now) for path, key, now in self.sent if path == "/v0/block/2"]
        self.assertEqual((walked, state["last_height"], state["blocks"]), (5, 5, 5))
        self.assertGreater(len(same_height), 80)
        self.assertIsNone(same_height[-2][0])
        self.assertEqual(same_height[-1][0], "Bearer unit-only-token")
        self.assertGreaterEqual(same_height[-1][1], same_height[0][1] + budget.KEY_COOLDOWN_S)
        self.assertEqual(state["requests"], len(self.sent))
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(1, 5)])

    def test_a_cap_switch_crossing_midnight_reserves_and_sends_the_next_read_on_the_new_day(self):
        midnight = (int(self.now // budget.DAY_S) + 1) * budget.DAY_S
        self.now = midnight - 0.35
        self.seed("keyed:feed:*", budget.DAY_S, 119999, now=self.now)
        state, walked = self.walk()
        self.assertEqual((walked, state["last_height"], state["requests"]), (5, 5, 4))
        self.assertEqual([key for _, key, _ in self.sent], ["Bearer unit-only-token"] * 5)
        self.assertEqual(self.count("keyless:feed:neardata", budget.DAY_S, now=midnight - 1), 0)
        self.assertEqual(self.count("keyed:feed:*", budget.DAY_S, now=midnight), 4)

    def test_a_cooldown_expiring_during_fallback_spacing_is_rechecked_before_reserving(self):
        def cool_down(request):
            if request.full_url.endswith("/v0/last_block/final"):
                budget.key_failure(402, self.now - budget.KEY_COOLDOWN_S + 0.2)

        self.after_request = cool_down
        state, walked = self.walk()
        self.assert_walk(state, walked)
        self.assertEqual([key for _, key, _ in self.sent], ["Bearer unit-only-token"] * 5)
        self.assertEqual(self.count("keyless:feed:neardata", budget.DAY_S), 0)

    def test_keyed_403_and_429_fall_back_and_429_still_backs_off(self):
        for status in (403, 429):
            with self.subTest(status=status):
                self.now += budget.DAY_S
                self.root = self.root.with_name(f"feed-{status}")
                self.sent.clear()
                self.waits.clear()
                self.failures["/v0/last_block/final"] = [status]
                state, walked = self.walk()
                self.assertEqual((walked, state["last_height"], state["requests"]), (5, 5, 6))
                self.assertEqual([key for _, key, _ in self.sent], ["Bearer unit-only-token"] + [None] * 5)
                self.assertEqual(self.sent[0][0], self.sent[1][0])
                if status == 429:
                    self.assertIn(feed.BACKOFF_S, self.waits)
                else:
                    self.assertNotIn(feed.BACKOFF_S, self.waits)

    def test_a_keyless_429_retries_the_same_height_after_the_existing_backoff(self):
        self.seed("keyed:feed:*", budget.DAY_S, 120000)
        self.failures["/v0/block/2"] = [429]
        state, walked = self.walk()
        self.assertEqual((walked, state["last_height"], state["blocks"], state["requests"]), (5, 5, 5, 6))
        self.assertEqual([path for path, _, _ in self.sent], ["/v0/last_block/final", "/v0/block/1",
                                                             "/v0/block/2", "/v0/block/2",
                                                             "/v0/block/3", "/v0/block/4"])
        self.assertTrue(all(key is None for _, key, _ in self.sent))
        self.assertIn(feed.BACKOFF_S, self.waits)

    def test_utc_midnight_restores_the_key_and_counts_each_attempt_on_its_actual_day(self):
        midnight = (int(self.now // budget.DAY_S) + 1) * budget.DAY_S
        self.now = midnight - 0.2
        self.seed("keyed:feed:*", budget.DAY_S, 120000, now=self.now)
        state, walked = self.walk()
        self.assertEqual((walked, state["last_height"], state["request_day"]), (5, 5, "2026-11-13"))
        self.assertEqual([key for _, key, _ in self.sent], [None] + ["Bearer unit-only-token"] * 4)
        self.assertEqual(state["requests"], 4)
        self.assertEqual(self.count("keyed:feed:*", budget.DAY_S, now=midnight), 4)

    def test_without_a_key_the_follower_uses_keyless_spacing_and_its_state_database(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FASTNEAR_API_KEY_FILE")
            os.environ.pop(budget.DATABASE_ENV)
            state, walked = self.walk()
        self.assert_walk(state, walked)
        self.assertTrue(all(key is None for _, key, _ in self.sent))
        self.assertTrue((self.root / budget.DATABASE_NAME).is_file())
        for before, after in zip(self.sent, self.sent[1:]):
            self.assertAlmostEqual(after[2] - before[2], feed.FOLLOW_INTERVAL_S, places=5)

    def test_a_500_stays_an_error_and_does_not_advance_or_invent_coverage(self):
        self.failures["/v0/block/2"] = [500]
        with self.assertRaises(rpc.HttpError) as caught:
            self.walk()
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(feed.load_state(self.root / "state.json")["last_height"], 1)
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(1, 1)])

    def test_an_unreadable_budget_never_sends_a_key_or_bypasses_metering(self):
        self.path.mkdir(parents=True)
        with self.assertRaises(rpc.BudgetExhausted):
            self.walk()
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
