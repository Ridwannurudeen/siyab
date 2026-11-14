"""Durable provider budgets (intel/budget.py) and their metering in gate/rpc.py Client._exchange."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import budget

ROOT = Path(__file__).resolve().parent.parent
DAY = 24 * 60 * 60
# 10:30:00 UTC: a minute starts here, the hour ends in 1800 s and the UTC day in 48600 s.
T0 = datetime(2026, 11, 12, 10, 30, tzinfo=UTC).timestamp()
KEY = {"Authorization": "Bearer unit-only-token"}


def reply(payload):
    handle = mock.MagicMock()
    handle.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return handle


class BudgetCase(unittest.TestCase):
    def setUp(self):
        for name in ("urllib.request.urlopen", "gate.rpc._open_with_deadline"):
            patcher = mock.patch(name, side_effect=AssertionError("the test opened the network"))
            patcher.start()
            self.addCleanup(patcher.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state" / budget.DATABASE_NAME
        environment = mock.patch.dict(os.environ, {budget.DATABASE_ENV: str(self.path)})
        environment.start()
        self.addCleanup(environment.stop)
        stderr = mock.patch("sys.stderr", new_callable=io.StringIO)
        self.stderr = stderr.start()
        self.addCleanup(stderr.stop)

    def count(self, counter, span, now=T0):
        if not self.path.exists():
            return 0
        window = int(now // span) if span else 0
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT count FROM counters WHERE counter=? AND window=?",
                                     (counter, window)).fetchone()
        return row[0] if row else 0

    def seed(self, counter, span, count, now=T0):
        budget.reserve("other", "operator", keyed=False, now=now)
        window = int(now // span) if span else 0
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("INSERT INTO counters VALUES(?,?,?,NULL) ON CONFLICT(counter,window) DO UPDATE "
                               "SET count=excluded.count", (counter, window, count))
            connection.commit()

    def alerts(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return [row[0] for row in connection.execute("SELECT kind FROM alerts ORDER BY rowid")]

    def assertRefused(self, provider, caller, keyed, now, retry_after):
        with self.assertRaises(budget.Refused) as caught:
            budget.reserve(provider, caller, keyed=keyed, now=now)
        self.assertEqual(caught.exception.retry_after, retry_after)
        return caught.exception


class BudgetWindowTest(BudgetCase):
    def test_the_anonymous_minute_ceiling_refuses_at_its_limit_counts_nothing_and_reopens_next_minute(self):
        for _ in range(120):
            self.assertTrue(budget.reserve("fastnear_rpc", "anonymous", keyed=True, now=T0))
        self.assertRefused("fastnear_rpc", "anonymous", True, T0 + 5, 55)
        self.assertEqual(self.count("keyed:anonymous:fastnear_rpc", budget.MINUTE_S), 120)
        self.assertEqual(self.count("pooled", budget.DAY_S), 120)
        self.assertEqual(self.count("cumulative", 0), 120)
        self.assertTrue(budget.reserve("fastnear_rpc", "anonymous", keyed=True, now=T0 + 60))
        self.assertEqual(self.count("keyed:anonymous:fastnear_rpc", budget.HOUR_S), 121)

    def test_hour_and_day_budgets_refuse_until_their_utc_window_turns(self):
        self.seed("keyed:anonymous:fastnear_archival", budget.HOUR_S, 130)
        self.assertRefused("fastnear_archival", "anonymous", True, T0, 1800)
        self.assertTrue(budget.reserve("fastnear_archival", "anonymous", keyed=True, now=T0 + 1800))
        self.seed("keyed:anonymous:fastnear_index", budget.DAY_S, 2200)
        self.assertRefused("fastnear_index", "anonymous", True, T0, 48600)
        self.assertTrue(budget.reserve("fastnear_index", "anonymous", keyed=True, now=T0 + 48600))

    def test_the_pooled_cap_refuses_every_caller_class_and_reopens_the_next_utc_day(self):
        self.seed("pooled", budget.DAY_S, budget.POOLED_DAILY)
        for caller in budget.CALLER_CLASSES:
            with self.subTest(caller=caller):
                self.assertRefused("fastnear_index", caller, True, T0, 48600)
        self.assertFalse(budget.reserve("fastnear_rpc", "operator", keyed=False, now=T0))
        self.assertTrue(budget.reserve("fastnear_index", "operator", keyed=True, now=T0 + 48600))

    def test_each_worker_and_team_class_has_its_own_keyed_budget_and_the_feed_has_none(self):
        self.seed("keyed:watch:*", budget.DAY_S, 3000)
        self.assertRefused("fastnear_index", "watch", True, T0, 48600)
        self.assertTrue(budget.reserve("fastnear_index", "investigation", keyed=True, now=T0))
        self.assertTrue(budget.reserve("fastnear_index", "team", keyed=True, now=T0))
        self.assertTrue(budget.reserve("fastnear_index", "anonymous", keyed=True, now=T0))
        self.seed("keyed:investigation:*", budget.HOUR_S, 250)
        self.assertRefused("fastnear_rpc", "investigation", True, T0, 1800)
        self.seed("keyed:team:*", budget.DAY_S, 1500)
        self.assertRefused("fastnear_archival", "team", True, T0, 48600)
        self.assertRefused("neardata", "feed", True, T0, 48600)
        self.assertRefused("neardata", "anonymous", True, T0, 48600)
        self.assertFalse(budget.reserve("neardata", "feed", keyed=False, now=T0))
        self.seed("pooled", budget.DAY_S, budget.POOLED_DAILY - 1)
        self.assertTrue(budget.reserve("fastnear_rpc", "operator", keyed=True, now=T0))
        self.assertEqual(self.count("keyed:operator:fastnear_rpc", budget.DAY_S), 1)

    def test_the_operator_day_cap_refuses_before_the_pooled_cap_and_reopens(self):
        self.seed("keyed:operator:*", budget.DAY_S, 1499)
        self.assertTrue(budget.reserve("neardata", "operator", keyed=True, now=T0))
        error = self.assertRefused("fastnear_rpc", "operator", True, T0, 48600)
        self.assertEqual(str(error), "The provider budget for this kind of read is spent; retry later.")
        self.assertEqual(self.count("keyed:operator:*", budget.DAY_S), 1500)
        self.assertEqual(self.count("pooled", budget.DAY_S), 1)
        self.assertIn("keyed:operator:* allows 1500 a day", self.stderr.getvalue())
        self.assertTrue(budget.reserve("fastnear_rpc", "operator", keyed=True, now=T0 + 48600))

    def test_database_errors_keep_the_path_only_on_stderr(self):
        detail = f"cannot open {self.path}"
        with mock.patch("intel.budget._transaction", side_effect=OSError(detail)):
            error = self.assertRefused("fastnear_rpc", "operator", False, T0, budget.REFUSAL_RETRY_S)
        self.assertEqual(str(error), "The provider budget database could not be read.")
        self.assertIn(detail, self.stderr.getvalue())
        self.assertNotIn(str(self.path), str(error))

    def test_public_backfill_counts_toward_the_anonymous_index_budget_and_its_own_500(self):
        self.assertTrue(budget.reserve("fastnear_index", "backfill", keyed=True, now=T0))
        self.assertEqual(self.count("keyed:anonymous:fastnear_index", budget.DAY_S), 1)
        self.assertEqual(self.count("keyed:backfill:fastnear_index", budget.DAY_S), 1)
        self.seed("keyed:backfill:fastnear_index", budget.DAY_S, 500)
        self.assertRefused("fastnear_index", "backfill", True, T0, 48600)
        self.assertTrue(budget.reserve("fastnear_index", "anonymous", keyed=True, now=T0))
        self.seed("keyed:backfill:fastnear_index", budget.DAY_S, 0)
        self.seed("keyed:anonymous:fastnear_index", budget.MINUTE_S, 90)
        self.assertRefused("fastnear_index", "backfill", True, T0, 60)

    def test_keyless_fastnear_calls_spend_no_credit_but_meet_politeness_ceilings(self):
        self.assertFalse(budget.reserve("fastnear_rpc", "anonymous", keyed=False, now=T0))
        self.assertEqual(self.count("pooled", budget.DAY_S), 0)
        self.seed("keyless:*:fastnear_rpc", budget.HOUR_S, 2000)
        self.assertRefused("fastnear_rpc", "anonymous", False, T0, 1800)
        self.assertRefused("fastnear_rpc", "watch", False, T0, 1800)
        self.assertTrue(budget.reserve("fastnear_rpc", "anonymous", keyed=True, now=T0))
        self.seed("keyless:*:fastnear_archival", budget.DAY_S, 3000)
        self.assertRefused("fastnear_archival", "anonymous", False, T0, 48600)
        for _ in range(3):
            self.assertFalse(budget.reserve("nearblocks", "anonymous", keyed=False, now=T0))
        self.assertEqual(self.count("keyless:anonymous:nearblocks", budget.DAY_S), 3)

    def test_threshold_crossings_are_recorded_once_with_one_stderr_line(self):
        for percent in budget.ALERT_PERCENTS:
            self.seed("pooled", budget.DAY_S, budget.POOLED_DAILY * percent // 100 - 1)
            budget.reserve("fastnear_rpc", "operator", keyed=True, now=T0)
        self.assertRefused("fastnear_rpc", "operator", True, T0, 48600)
        self.seed("cumulative", 0, budget.CUMULATIVE_ALERT - 1)
        self.seed("pooled", budget.DAY_S, 0)
        budget.reserve("fastnear_rpc", "operator", keyed=True, now=T0)
        budget.reserve("fastnear_rpc", "operator", keyed=True, now=T0)
        self.assertEqual(self.alerts(), ["pooled_50", "pooled_80", "pooled_100", "cumulative_300000"])
        lines = self.stderr.getvalue().splitlines()
        lines = [line for line in lines if line.startswith("provider budget alert:")]
        self.assertEqual(len(lines), 4)
        self.assertIn("6500 keyed FastNEAR calls on UTC day 2026-11-12, 50 percent of the pooled cap of 13000",
                      lines[0])
        self.assertIn("300000 keyed FastNEAR calls since the counter started", lines[3])

    def test_two_processes_sharing_the_database_cannot_overshoot_the_pooled_cap(self):
        self.seed("pooled", budget.DAY_S, budget.POOLED_DAILY - 150)
        script = ("import sys\nfrom intel import budget\nreserved = 0\nfor _ in range(200):\n"
                  "    try:\n        budget.reserve('fastnear_index', 'operator', keyed=True, now=float(sys.argv[1]))\n"
                  "    except budget.Refused:\n        continue\n    reserved += 1\nprint(reserved)\n")
        workers = [subprocess.Popen([sys.executable, "-c", script, repr(T0)], cwd=ROOT, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True) for _ in range(2)]
        reserved = []
        for worker in workers:
            out, err = worker.communicate(timeout=300)
            self.assertEqual(worker.returncode, 0, err)
            reserved.append(int(out))
        self.assertEqual(sum(reserved), 150)
        self.assertEqual(self.count("pooled", budget.DAY_S), budget.POOLED_DAILY)
        self.assertEqual(self.count("keyed:operator:fastnear_index", budget.DAY_S), 150)
        self.assertEqual(self.alerts().count("pooled_100"), 1)

    def test_a_key_failure_starts_a_shared_cooldown_that_strips_the_key_and_expires(self):
        budget.key_failure(500, T0)
        self.assertTrue(budget.reserve("fastnear_rpc", "anonymous", keyed=True, now=T0))
        budget.key_failure(429, T0)
        self.assertFalse(budget.reserve("fastnear_rpc", "anonymous", keyed=True, now=T0 + budget.KEY_COOLDOWN_S - 1))
        self.assertEqual(self.count("pooled", budget.DAY_S), 1)
        self.assertEqual(self.count("keyless:anonymous:fastnear_rpc", budget.DAY_S), 1)
        self.assertTrue(budget.reserve("fastnear_rpc", "anonymous", keyed=True, now=T0 + budget.KEY_COOLDOWN_S))
        budget.key_failure(403, T0 + DAY)
        self.assertFalse(budget.reserve("fastnear_index", "watch", keyed=True, now=T0 + DAY + 1))
        self.assertEqual(self.alerts(), ["key_cooldown", "key_cooldown"])
        self.assertIn("HTTP 429 from a keyed FastNEAR call", self.stderr.getvalue())

    def test_without_a_database_nothing_is_counted_and_the_key_is_never_sent(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(budget.DATABASE_ENV)
            self.assertIsNone(budget.database())
            self.assertFalse(budget.reserve("fastnear_rpc", "anonymous", keyed=True, now=T0))
            budget.key_failure(429, T0)
            with budget.use_database(self.path):
                self.assertEqual(budget.database(), self.path)
                self.assertTrue(budget.reserve("fastnear_rpc", "anonymous", keyed=True, now=T0))
            self.assertIsNone(budget.database())
        self.assertEqual(self.count("pooled", budget.DAY_S), 1)
        with budget.use_database(self.path.parent / "other.sqlite3"):
            self.assertEqual(budget.database(), self.path)

    def test_an_unreadable_database_refuses_the_call(self):
        self.path.mkdir(parents=True)
        error = self.assertRefused("fastnear_rpc", "operator", False, T0, budget.REFUSAL_RETRY_S)
        self.assertIn("could not be read", str(error))


class MeteredClientTest(BudgetCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.answers = []

        def urlopen(request, timeout):
            self.sent.append(request)
            answer = self.answers.pop(0) if self.answers else reply({"jsonrpc": "2.0", "id": "gate",
                                                                      "result": {"x": 1}})
            if isinstance(answer, Exception):
                raise answer
            return answer

        patcher = mock.patch("urllib.request.urlopen", side_effect=urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = [T0]

    def client(self, url=rpc.MAINNET_RPC_URL, headers=KEY, mode="off"):
        return rpc.Client(url, mode, headers=headers, clock=lambda: self.now[0])

    def test_a_real_fetch_is_counted_by_caller_class_and_provider_and_carries_the_key(self):
        with rpc.caller_class("anonymous") as refusals:
            self.client().call("status", [])
            self.client(rpc.ARCHIVAL_RPC_URL).call("status", [])
        self.assertEqual(refusals, [])
        self.client().call("status", [])
        self.assertEqual([request.get_header("Authorization") for request in self.sent],
                         ["Bearer unit-only-token"] * 3)
        self.assertEqual(self.count("keyed:anonymous:fastnear_rpc", budget.MINUTE_S), 1)
        self.assertEqual(self.count("keyed:anonymous:fastnear_archival", budget.MINUTE_S), 1)
        self.assertEqual(self.count("keyed:operator:fastnear_rpc", budget.DAY_S), 1)
        self.assertEqual(self.count("pooled", budget.DAY_S), 3)
        self.client("https://rpc.example", headers=None).call("status", [])
        self.assertEqual(self.count("keyless:operator:other", budget.DAY_S), 1)

    def test_replay_and_record_never_touch_the_budget_database(self):
        replayed = rpc.Client(rpc.MAINNET_RPC_URL, "replay", headers=KEY)
        with rpc.caller_class("anonymous"):
            replayed.view_account("intents.near")
            with tempfile.TemporaryDirectory() as fixtures:
                recorder = rpc.Client("https://free.rpc.fastnear.com", "record", fixtures_dir=fixtures,
                                      headers=KEY, clock=lambda: T0)
                recorder.call("status", [])
                recorder.call("status", [])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0].get_header("Authorization"), "Bearer unit-only-token")
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_a_refused_call_sends_nothing_and_reads_as_rate_limited_with_its_retry_time(self):
        self.seed("pooled", budget.DAY_S, budget.POOLED_DAILY)
        with rpc.caller_class("anonymous") as refusals:
            with self.assertRaises(rpc.RateLimited) as caught:
                self.client().call("status", [])
        self.assertIsInstance(caught.exception, rpc.BudgetExhausted)
        self.assertEqual(caught.exception.retry_after, 48600)
        self.assertEqual(refusals, [caught.exception])
        self.assertEqual(self.sent, [])

    def test_a_403_or_429_from_a_keyed_host_starts_the_cooldown_and_later_calls_go_out_keyless(self):
        for status in (429, 403):
            with self.subTest(status=status):
                self.answers.append(urllib.error.HTTPError(rpc.MAINNET_RPC_URL, status, "refused", {}, None))
                with self.assertRaises(rpc.HttpError):
                    self.client().call("status", [])
                self.now[0] += 1
                self.client(headers=dict(KEY, Accept="application/json")).call("status", [])
                self.assertIsNone(self.sent[-1].get_header("Authorization"))
                self.now[0] += budget.KEY_COOLDOWN_S
                self.client().call("status", [])
                self.assertEqual(self.sent[-1].get_header("Authorization"), "Bearer unit-only-token")
        self.assertEqual(self.alerts(), ["key_cooldown", "key_cooldown"])
        self.answers.append(urllib.error.HTTPError("https://rpc.example", 429, "refused", {}, None))
        with self.assertRaises(rpc.HttpError):
            self.client("https://rpc.example", headers=None).call("status", [])
        self.assertEqual(self.alerts(), ["key_cooldown", "key_cooldown"])

    def test_a_rejected_key_logs_one_line_with_the_status_and_host_and_never_the_key(self):
        for status in (401, 402, 403):
            with self.subTest(status=status):
                self.now[0] += budget.KEY_COOLDOWN_S + 1
                self.answers.append(urllib.error.HTTPError(rpc.MAINNET_RPC_URL, status, "refused", {}, None))
                with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr, self.assertRaises(rpc.HttpError):
                    self.client().call("status", [])
                lines = [line for line in stderr.getvalue().splitlines() if line.startswith("provider rejected the key")]
                self.assertEqual(lines, [f"provider rejected the key: HTTP {status} from free.rpc.fastnear.com"])
                self.assertNotIn("unit-only-token", stderr.getvalue())
        self.answers.append(urllib.error.HTTPError(rpc.MAINNET_RPC_URL, 500, "failed", {}, None))
        self.now[0] += budget.KEY_COOLDOWN_S + 1
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr, self.assertRaises(rpc.HttpError):
            self.client().call("status", [])
        self.assertNotIn("provider rejected the key", stderr.getvalue())

    def test_budget_errors_exposed_by_public_details_hide_paths_counters_and_limits(self):
        from gate import rules, scan

        self.seed("keyed:operator:*", budget.DAY_S, 1500)
        cases = [mock.patch("intel.budget.database", return_value=self.path),
                 mock.patch("intel.budget._transaction", side_effect=OSError(f"cannot open {self.path}"))]
        for patcher in cases:
            with patcher, mock.patch("gate.rpc._fetch", side_effect=AssertionError("unexpected fetch")):
                with self.assertRaises(rpc.BudgetExhausted) as caught:
                    self.client().call("status", [])
                store = mock.Mock()
                store.by_subject.return_value = []
                response = scan.scan("account.near", client=self.client(), store=store)
            public = json.dumps(response) + rules._why(caught.exception)
            for private in (str(self.path), "keyed:operator:*", "1500"):
                self.assertNotIn(private, public)
        self.assertEqual(self.sent, [])

    def test_caller_classes_are_named_and_nest(self):
        with self.assertRaises(ValueError):
            with rpc.caller_class("visitor"):
                pass
        with rpc.caller_class("watch") as outer:
            with rpc.caller_class("anonymous") as inner:
                self.seed("pooled", budget.DAY_S, budget.POOLED_DAILY)
                with self.assertRaises(rpc.BudgetExhausted):
                    self.client().call("status", [])
            self.seed("pooled", budget.DAY_S, 0)
            self.client().call("status", [])
        self.assertEqual((len(inner), outer), (1, []))
        self.assertEqual(self.count("keyed:watch:fastnear_rpc", budget.DAY_S), 1)


class EntryPointTest(unittest.TestCase):
    def test_entry_points_declare_their_caller_class_and_state_database_for_the_command_only(self):
        from intel import __main__ as intel_main

        seen = {}

        def capture(name, result):
            def record(*args, **kwargs):
                seen[name] = (rpc._CALLER_CLASS.get(), budget.database())
                return result
            return record

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ):
            os.environ.pop(budget.DATABASE_ENV, None)
            state = Path(directory).resolve()
            database = state / budget.DATABASE_NAME
            with mock.patch("intel.history.run", side_effect=capture("history", {})), \
                    mock.patch("intel.workspace.SQLiteWorkspaceStore"):
                intel_main.main(["history-follow", "--state-dir", directory, "--once"])
            with mock.patch("intel.investigation_jobs.run", side_effect=capture("investigation", {})), \
                    mock.patch("intel.history.AccountHistory"):
                intel_main.main(["investigation-follow", "--state-dir", directory, "--once"])
            with mock.patch("intel.__main__.api.serve", side_effect=capture("serve", 0)):
                intel_main.main(["serve", "--live", "--port", "0", "--state-dir", directory])
            with mock.patch("intel.__main__.api.serve", side_effect=capture("serve-stateless", 0)):
                intel_main.main(["serve", "--live", "--port", "0"])
            with mock.patch("intel.feed.follow", side_effect=capture("feed", ({"last_height": 1, "lag_blocks": 0}, 1))):
                intel_main.main(["feed", "--follow", "--max-blocks", "1"])
            self.assertIsNone(budget.database())
        self.assertEqual(seen, {"history": ("watch", database), "investigation": ("investigation", database),
                                "serve": (None, database), "serve-stateless": (None, None), "feed": ("feed", None)})
        self.assertIsNone(rpc._CALLER_CLASS.get())


if __name__ == "__main__":
    unittest.main()
