import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from eval import score
from gate import rpc, rules
from intel import __main__ as intel_main
from intel import budget, judge_accounts
from tests import test_rules as rule_fixtures
from tests import test_top_receiver_recording as recording


class ReceiverFixtures(rule_fixtures.Fixtures):
    def write(self, url, method, params, response):
        for original, replacement in ((rule_fixtures.RPC_URL, rpc.MAINNET_RPC_URL),
                                      (rule_fixtures.ARCHIVAL_URL, rpc.ARCHIVAL_RPC_URL),
                                      (rule_fixtures.NEARBLOCKS_URL, rpc.NEARBLOCKS_URL)):
            if url.startswith(original):
                url = replacement + url[len(original):]
                break
        super().write(url, method, params, response)


class JudgeAccountsCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.accounts = self.directory / "accounts.txt"
        self.out = self.directory / "answers"
        self.database = self.directory / "state" / budget.DATABASE_NAME
        self.key = self.directory / "dummy-provider-key.txt"
        self.key.write_text("unit-only-token\n", encoding="utf-8")
        self.store = rule_fixtures.Store()
        for patcher in (
            mock.patch.dict(os.environ, {"TEMP": tempfile.gettempdir(), "TMP": tempfile.gettempdir(),
                "USERPROFILE": str(self.directory), "FASTNEAR_API_KEY_FILE": str(self.key),
                budget.DATABASE_ENV: str(self.database)}, clear=True),
            mock.patch.object(judge_accounts.labels, "load", side_effect=lambda path=None: self.store),
            mock.patch.object(rpc, "_fetch", side_effect=AssertionError("test opened the network")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("test opened the network")),
            mock.patch.object(rpc, "_open_with_deadline", side_effect=AssertionError("test opened the network")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        rpc._NEARBLOCKS_USE.clear()
        self.addCleanup(rpc._NEARBLOCKS_USE.clear)

    def command(self, record=False, labels_path=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = intel_main.main(["judge-accounts", "--accounts", str(self.accounts), "--out", str(self.out)]
                                     + (["--record"] if record else [])
                                     + (["--labels", str(labels_path)] if labels_path is not None else []))
        self.stdout, self.stderr = stdout.getvalue(), stderr.getvalue()
        return result

    def fixtures(self, destination=None):
        directory = destination or self.out / "rpc"
        directory.mkdir(parents=True, exist_ok=True)
        return ReceiverFixtures(directory)

    def write_accounts(self, accounts):
        ordered = sorted(accounts, key=lambda account: hashlib.sha256(account.encode("utf-8")).digest())
        self.accounts.write_text("\n".join(ordered) + "\n", encoding="utf-8")
        return ordered

    def saved_run(self):
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "set.json").write_text(json.dumps({
            "accounts": self.accounts.read_text(encoding="utf-8").splitlines(), "rules_version": 3,
            "label_store_sha256": self.store.sha256, "reports_account": None,
            "judged_at_ms": rule_fixtures.NOW_MS}), encoding="utf-8")

    def old_account(self, fixtures, account):
        fixtures.account(account)
        fixtures.access_keys(account, 1)
        for issuer in rules.issuers_for("mainnet"):
            fixtures.function(issuer["token"], issuer["method"], {issuer["args_key"]: account}, issuer["clear"])

    def answer(self, account):
        return json.loads((self.out / "judgements" / f"{account}.json").read_text(encoding="utf-8"))

    def count(self, counter):
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute("SELECT count FROM counters WHERE counter=? AND window=?",
                (counter, int(rpc.time.time() // budget.DAY_S))).fetchone()
        return row[0] if row else 0

    def seed(self, count):
        with budget.use_database(self.database):
            budget.reserve("other", "operator", keyed=False, now=rpc.time.time())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO counters VALUES(?,?,?,NULL) ON CONFLICT(counter,window) "
                "DO UPDATE SET count=excluded.count", ("keyed:recording:*", int(rpc.time.time() // budget.DAY_S), count))

    def fetch_from(self, fixtures):
        def fetch(url, body, headers, errors):
            method, params = (body["method"], body["params"]) if body else ("GET", None)
            path = fixtures.directory / f"{rpc.fixture_key(url, method, params)}.json"
            return json.loads(path.read_text(encoding="utf-8"))["response"]
        return fetch


class JudgeAccountsTest(JudgeAccountsCase):
    def test_module_entry_point_refuses_replay_without_a_recorded_run(self):
        self.accounts.write_text("synthetic-unread-input.near\n", encoding="utf-8")
        environment = {**os.environ, "GATE_FIXTURES_MODE": "off", "FASTNEAR_API_KEY_FILE": "not-read"}
        result = subprocess.run([sys.executable, "-m", "intel", "judge-accounts", "--accounts", str(self.accounts),
            "--out", str(self.out)], cwd=intel_main.ROOT, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("recorded run", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.out.exists())
        self.assertFalse(self.database.exists())

    def test_replay_without_checkpoint_refuses_even_with_read_fixtures(self):
        self.write_accounts(["ordinary.near"])
        self.old_account(self.fixtures(), "ordinary.near")
        self.assertEqual(self.command(), 1)
        self.assertIn("recorded run", self.stderr)
        self.assertFalse((self.out / "set.json").exists())
        self.assertFalse((self.out / "judgements").exists())

    def test_unsorted_input_is_refused_before_keys_or_output(self):
        accounts = self.write_accounts(["ordinary.near", "second.near"])
        self.accounts.write_text("\n".join(reversed(accounts)) + "\n", encoding="utf-8")
        with mock.patch.object(judge_accounts.top_receivers, "fastnear_headers") as headers:
            self.assertEqual(self.command(True), 1)
        self.assertIn("sorted by sha256", self.stderr)
        headers.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_output_in_repository_refuses_except_under_holdout(self):
        self.write_accounts(["ordinary.near"])
        root = self.directory / "repository"
        for destination in (root, root / "answers", root / "fixtures", root / "fixtures/holdout",
                root / "fixtures/holdout/../answers"):
            with self.subTest(destination=destination), mock.patch.object(intel_main, "ROOT", root), \
                    mock.patch.object(judge_accounts.top_receivers, "fastnear_headers") as headers:
                self.out = destination
                self.assertEqual(self.command(True), 1)
                self.assertIn("outside the source repository", self.stderr)
                headers.assert_not_called()
                self.assertFalse(root.exists())
        self.out = root / "fixtures/holdout/unit"
        self.old_account(self.fixtures(), "ordinary.near")
        self.saved_run()
        with mock.patch.object(intel_main, "ROOT", root):
            self.assertEqual(self.command(), 0, self.stderr)
        self.assertEqual(self.answer("ordinary.near")["verdict"], "ALLOW")

    def test_published_holdout_run_refuses_output_before_any_writes_or_keys(self):
        self.write_accounts(["ordinary.near"])
        root = self.directory / "repository"
        published = root / "fixtures/holdout/2026-11-22/run"
        self.out = published
        self.old_account(self.fixtures(), "ordinary.near")
        self.saved_run()
        (published.parent / "receivers.jsonl").write_text("{}" + chr(10), encoding="utf-8")
        before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        for destination in (published, published / "judgements", published / "rpc/new", published / "../run", published.parent,
                            published.parent / "rerun"):
            for record in (False, True):
                with self.subTest(destination=destination, record=record), \
                        mock.patch.object(intel_main, "ROOT", root), \
                        mock.patch.object(judge_accounts.top_receivers, "fastnear_headers") as headers:
                    self.out = destination
                    self.assertEqual(self.command(record), 1)
                    self.assertIn("outside an existing published holdout", self.stderr)
                    headers.assert_not_called()
                    self.assertEqual({path.relative_to(root): path.read_bytes()
                        for path in root.rglob("*") if path.is_file()}, before)

    def test_explicit_label_store_is_used_and_bound_to_the_run(self):
        self.write_accounts(["flagged.near"])
        self.store = rule_fixtures.Store(rule_fixtures.assertion("unit-label", "flagged.near", "incident", "Recorded"))
        self.old_account(self.fixtures(), "flagged.near")
        self.saved_run()
        path = self.directory / "frozen-labels.jsonl"
        with mock.patch.object(judge_accounts.labels, "load", return_value=self.store) as load:
            self.assertEqual(self.command(labels_path=path), 0, self.stderr)
        load.assert_called_once_with(path)
        self.assertEqual(self.answer("flagged.near")["verdict"], "DENY")
        self.store.sha256 = "6" * 64
        self.assertEqual(self.command(labels_path=path), 1)
        self.assertIn("saved run differs", self.stderr)

    def test_programming_errors_are_not_reported_as_malformed_fixtures(self):
        self.write_accounts(["ordinary.near"])
        self.saved_run()
        for error in (KeyError("unit-code-error"), TypeError("unit-code-error")):
            with self.subTest(error=error), mock.patch.object(intel_main, "_judge", side_effect=error):
                with self.assertRaises(type(error)):
                    self.command()
        self.assertFalse((self.out / "judgements").exists())

    def test_missing_issuer_read_is_not_hidden_by_another_recorded_unknown_source(self):
        self.write_accounts(["ordinary.near"])
        fixtures = self.fixtures()
        self.old_account(fixtures, "ordinary.near")
        first, second = rules.issuers_for("mainnet")
        fixtures.function_error(first["token"], first["method"], {first["args_key"]: "ordinary.near"})
        params = fixtures._function_params(second["token"], second["method"], {second["args_key"]: "ordinary.near"})
        key = rpc.fixture_key(rpc.MAINNET_RPC_URL, "query", params)
        (fixtures.directory / f"{key}.json").unlink()
        self.saved_run()
        self.assertEqual(self.command(), 0, self.stderr)
        answer = self.answer("ordinary.near")
        issuer = next(check for check in answer["checks"] if check["id"] == "issuer_blacklist")
        self.assertIsNotNone(issuer["source"]["fetched_at"])
        self.assertIs(answer.get("scoreable"), False)
        self.assertEqual(score.load_verdicts(self.out / "judgements"), ({"ordinary.near": "NOT_JUDGED"}, [], []))

    def test_feed_state_directory_supplies_the_external_recording_budget(self):
        self.accounts.write_text("ordinary.near\n", encoding="utf-8")
        fixtures = self.fixtures(self.directory / "source")
        self.old_account(fixtures, "ordinary.near")
        with mock.patch.dict(os.environ, {"FEED_STATE_DIR": str(self.database.parent)}), \
                mock.patch.object(rpc, "_fetch", side_effect=self.fetch_from(fixtures)) as fetched:
            os.environ.pop(budget.DATABASE_ENV)
            self.assertEqual(self.command(True), 0, self.stderr)
        self.assertEqual(fetched.call_count, 4)
        self.assertEqual(self.count("keyed:recording:*"), 4)
        self.assertEqual(self.answer("ordinary.near")["verdict"], "ALLOW")

    def test_corrupt_read_fixture_stops_without_publishing_an_answer(self):
        self.accounts.write_text("ordinary.near\n", encoding="utf-8")
        fixtures = self.fixtures()
        self.old_account(fixtures, "ordinary.near")
        key = rpc.fixture_key(rpc.MAINNET_RPC_URL, "query",
            {"request_type": "view_account", "finality": "final", "account_id": "ordinary.near"})
        path = fixtures.directory / f"{key}.json"
        self.saved_run()
        for value in ({}, [], None, {"response": {}, "fetched_at": None}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(self.command(), 1)
                self.assertIn("malformed read fixture", self.stderr)
                self.assertFalse((self.out / "judgements").exists())
        for content in (b"{", b"\xff"):
            with self.subTest(content=content):
                path.write_bytes(content)
                self.assertEqual(self.command(), 1)
                self.assertIn("malformed read fixture JSON", self.stderr)
                self.assertFalse((self.out / "judgements").exists())

    def test_recorded_legacy_http_error_is_still_a_scoreable_unknown(self):
        self.write_accounts(["ordinary.near"])
        fixtures = self.fixtures()
        self.old_account(fixtures, "ordinary.near")
        fixtures.rpc_error(fixtures.key_list_params("ordinary.near"), "NO_SYNCED_BLOCKS")
        fixtures.write(rpc.NEARBLOCKS_URL + "/v1/account/ordinary.near/keys", "GET", None,
                       {rpc.HTTP_STATUS_MARKER: 503})
        self.saved_run()
        self.assertEqual(self.command(), 0, self.stderr)
        self.assertEqual(self.answer("ordinary.near")["verdict"], "UNKNOWN")
        self.assertNotIn("scoreable", self.answer("ordinary.near"))
        self.assertEqual(score.load_verdicts(self.out / "judgements"), ({"ordinary.near": "ASK"}, [], []))

    def test_replay_matches_receiver_rules_and_scorer_without_policy_denials(self):
        accounts = self.write_accounts(["ordinary.near", "flagged.near", "young.near", "unread.near"])
        self.store = rule_fixtures.Store(rule_fixtures.assertion("unit-label", "flagged.near", "incident", "Recorded"))
        fixtures = self.fixtures()
        for account in ("ordinary.near", "flagged.near", "young.near"):
            self.old_account(fixtures, account)
        fixtures.rpc_error(fixtures.key_list_params("young.near"), "NO_SYNCED_BLOCKS")
        fixtures.keys("young.near", rule_fixtures.NOW_MS * 10**6 - rule_fixtures.DAY_NS)
        fixtures.transaction(str(rules.YOCTO_PER_NEAR), "young.near")
        self.saved_run()
        with mock.patch.object(judge_accounts.time, "time", return_value=rule_fixtures.NOW_MS / 1000):
            self.assertEqual(self.command(), 0, self.stderr)
        answers, errors, duplicates = score.load_verdicts(self.out / "judgements")
        self.assertEqual((answers, errors, duplicates), ({"ordinary.near": "ALLOW", "flagged.near": "DENY",
            "young.near": "ASK", "unread.near": "NOT_JUDGED"}, [], []))
        self.assertEqual(len(list((self.out / "judgements").glob("*.json"))), 4)
        for account in accounts:
            answer = self.answer(account)
            self.assertEqual(answer["rules_version"], 3)
            self.assertEqual(answer["request"], intel_main._request(account, False))
            clients = [rpc.Client(url, "replay", fixtures_dir=fixtures.directory,
                kind="http" if url == rpc.NEARBLOCKS_URL else "rpc")
                for url in (rpc.MAINNET_RPC_URL, rpc.NEARBLOCKS_URL, rpc.ARCHIVAL_RPC_URL)]
            expected = intel_main._judge({"subject": account, "as": "receiver", "request": answer["request"]},
                self.store, clients[0], clients[1], rule_fixtures.NOW_MS, clients[2], rules_version=3)
            self.assertEqual(answer, {**expected, "rules_version": 3,
                **({"scoreable": False} if account == "unread.near" else {})})
        young = self.answer("young.near")
        self.assertEqual({check["id"] for check in young["checks"] if check["status"] == "signal"}, {"account_age"})
        self.assertIn("confirm the receiver before paying", young["reasons"][0])
        self.assertFalse(self.database.exists())

    def test_replay_ignores_live_environment_credentials_and_provider_budget(self):
        self.accounts.write_text("ordinary.near\n", encoding="utf-8")
        self.old_account(self.fixtures(), "ordinary.near")
        self.saved_run()
        with mock.patch.dict(os.environ, {"GATE_FIXTURES_MODE": "off", "FASTNEAR_API_KEY_FILE": "not-read"}), \
                mock.patch.object(judge_accounts.top_receivers, "fastnear_headers", side_effect=AssertionError("key read")), \
                mock.patch.object(budget, "reserve", side_effect=AssertionError("budget touched")):
            self.assertEqual(self.command(), 0, self.stderr)
        self.assertEqual(self.answer("ordinary.near")["verdict"], "ALLOW")

    def test_missing_reads_are_unknown_and_a_second_replay_is_identical(self):
        self.accounts.write_text("unread.near\n", encoding="utf-8")
        self.saved_run()
        self.assertEqual(self.command(), 0, self.stderr)
        before = self.answer("unread.near")
        self.assertEqual(before["verdict"], "UNKNOWN")
        self.assertIs(before.get("scoreable"), False)
        self.assertEqual(score.load_verdicts(self.out / "judgements"), ({"unread.near": "NOT_JUDGED"}, [], []))
        self.assertEqual(self.command(), 0, self.stderr)
        self.assertEqual(self.answer("unread.near"), before)
        self.assertFalse((self.out / "judged-block.json").exists())

    def test_label_deny_with_missing_age_reads_still_scores_deny(self):
        self.write_accounts(["flagged.near"])
        self.store = rule_fixtures.Store(rule_fixtures.assertion("unit-label", "flagged.near", "incident", "Recorded"))
        fixtures = self.fixtures()
        self.old_account(fixtures, "flagged.near")
        fixtures.rpc_error(fixtures.key_list_params("flagged.near"), "NO_SYNCED_BLOCKS")
        self.saved_run()
        self.assertEqual(self.command(), 0, self.stderr)
        answer = self.answer("flagged.near")
        self.assertEqual({check["id"] for check in answer["checks"] if check["status"] == "signal"}, {"label_denylist"})
        self.assertTrue(intel_main._age_unknown(answer))
        self.assertEqual(answer["verdict"], "DENY")
        self.assertNotIn("scoreable", answer)
        self.assertEqual(score.load_verdicts(self.out / "judgements"), ({"flagged.near": "DENY"}, [], []))

    def test_missing_required_age_reads_are_not_judged(self):
        self.write_accounts(["ordinary.near"])
        fixtures = self.fixtures()
        self.old_account(fixtures, "ordinary.near")
        fixtures.rpc_error(fixtures.key_list_params("ordinary.near"), "NO_SYNCED_BLOCKS")
        self.saved_run()
        self.assertEqual(self.command(), 0, self.stderr)
        answer = self.answer("ordinary.near")
        self.assertEqual(answer["verdict"], "UNKNOWN")
        self.assertTrue(intel_main._age_unknown(answer))
        self.assertIs(answer.get("scoreable"), False)
        self.assertEqual(score.load_verdicts(self.out / "judgements"), ({"ordinary.near": "NOT_JUDGED"}, [], []))

    def test_recorded_age_fallback_keeps_optional_missing_reads_scoreable(self):
        self.write_accounts(["ordinary.near"])
        fixtures = self.fixtures()
        for verdict, outcome in (("ALLOW", "ALLOW"), ("UNKNOWN", "ASK")):
            with self.subTest(verdict=verdict):
                self.old_account(fixtures, "ordinary.near")
                key = rpc.fixture_key(rpc.MAINNET_RPC_URL, "query", fixtures.key_list_params("ordinary.near"))
                (fixtures.directory / f"{key}.json").unlink()
                fixtures.keys("ordinary.near", rule_fixtures.NOW_MS * 10**6 - 30 * rule_fixtures.DAY_NS)
                if verdict == "UNKNOWN":
                    issuer = rules.issuers_for("mainnet")[0]
                    fixtures.function_error(issuer["token"], issuer["method"], {issuer["args_key"]: "ordinary.near"})
                self.saved_run()
                self.assertEqual(self.command(), 0, self.stderr)
                answer = self.answer("ordinary.near")
                self.assertEqual(answer["verdict"], verdict)
                self.assertTrue(all(check["status"] == "pass" for check in answer["checks"]
                                    if check["id"] in ("account_age", "dust_funded")))
                self.assertNotIn("scoreable", answer)
                self.assertEqual(score.load_verdicts(self.out / "judgements"), ({"ordinary.near": outcome}, [], []))

    def test_non_account_lines_are_refused_before_outputs_or_keys(self):
        for contents, diagnostic in (("", "no accounts"), ("ordinary.near\n\n", ":2:"),
                ("ordinary.near\nUPPER.near\n", ":2:"), ("ordinary.near good\n", ":1:"),
                ('{"account":"ordinary.near","label":"good"}\n', ":1:"), ("# comment\n", ":1:"),
                (" ordinary.near\n", ":1:"), ("ordinary.near\t\n", ":1:"), ("x\n", ":1:"),
                ("testnet\n", "mainnet"), ("ordinary.testnet\n", "mainnet"),
                ("ordinary.near\nordinary.near\n", "first on line 1")):
            with self.subTest(contents=contents), mock.patch.object(judge_accounts.top_receivers, "fastnear_headers") as headers:
                self.accounts.write_text(contents, encoding="utf-8")
                self.assertEqual(self.command(True), 1)
                self.assertIn(diagnostic, self.stderr)
                headers.assert_not_called()
                self.assertFalse(self.out.exists())

    def test_implicit_and_other_canonical_mainnet_ids_are_kept(self):
        accounts = ["a" * 64, "0x" + "b" * 40, "factory", "sub_account.near"]
        self.write_accounts(accounts)
        self.saved_run()
        self.assertEqual(self.command(), 0, self.stderr)
        self.assertEqual(set(score.load_verdicts(self.out / "judgements")[0]), set(accounts))

    def test_missing_input_and_invalid_utf8_refuse_cleanly(self):
        self.assertEqual(self.command(), 1)
        self.accounts.write_bytes(b"\xff\n")
        self.assertEqual(self.command(), 1)
        self.assertFalse(self.out.exists())

    def test_record_requires_key_and_external_budget_before_any_fetch(self):
        self.accounts.write_text("ordinary.near\n", encoding="utf-8")
        configurations = ({}, {"FASTNEAR_API_KEY_FILE": str(self.key)},
            {"FASTNEAR_API_KEY_FILE": str(self.key), "FEED_STATE_DIR": str(intel_main.ROOT / "intel/feed")},
            {"FASTNEAR_API_KEY_FILE": str(self.key), budget.DATABASE_ENV: str(intel_main.ROOT / "budget.sqlite3"),
             "FEED_STATE_DIR": str(self.directory / "state")})
        for configuration in configurations:
            with self.subTest(configuration=configuration), mock.patch.dict(os.environ, configuration, clear=True), \
                    mock.patch.object(judge_accounts.top_receivers, "fastnear_headers") as headers:
                self.assertEqual(self.command(True), 1)
                headers.assert_not_called()
                self.assertFalse((self.out / "set.json").exists())

    def test_invalid_provider_key_refuses_without_echoing_contents(self):
        self.accounts.write_text("ordinary.near\n", encoding="utf-8")
        for contents in ("", "unit-only-token\nsecond-line", "\xff"):
            with self.subTest(contents=contents):
                self.key.write_bytes(contents.encode("latin1"))
                self.assertEqual(self.command(True), 1)
                self.assertIn("FASTNEAR_API_KEY_FILE", self.stderr)
                self.assertNotIn("unit-only-token", self.stderr)
                self.assertFalse((self.out / "set.json").exists())

    def test_record_meters_only_misses_and_replays_old_fixtures_without_rewriting(self):
        self.accounts.write_text("ordinary.near\n", encoding="utf-8")
        fixtures = self.fixtures(self.directory / "source")
        self.old_account(fixtures, "ordinary.near")
        with mock.patch.object(rpc, "_fetch", side_effect=self.fetch_from(fixtures)) as fetched:
            self.assertEqual(self.command(True), 0, self.stderr)
        self.assertEqual(fetched.call_count, 4)
        self.assertTrue(all(call.args[2] == {"Authorization": "Bearer unit-only-token"}
                            for call in fetched.call_args_list))
        self.assertEqual(self.count("keyed:recording:*"), 4)
        self.assertEqual(self.count("pooled"), 4)
        for path in (self.out / "rpc").glob("*.json"):
            fixture = json.loads(path.read_text(encoding="utf-8"))
            fixture["fetched_at"] = "2000-01-01T00:00:00Z"
            path.write_text(json.dumps(fixture), encoding="utf-8")
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.out / "rpc").glob("*.json")}
        self.assertEqual(self.command(), 0, self.stderr)
        answer = self.answer("ordinary.near")
        self.assertEqual(self.command(True), 0, self.stderr)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}, before)
        self.assertEqual(self.answer("ordinary.near")["checks"], answer["checks"])
        self.assertEqual(self.count("keyed:recording:*"), 4)
        self.assertEqual(score.load_verdicts(self.out / "judgements"), ({"ordinary.near": "ALLOW"}, [], []))
        self.assertIsNone(budget._database)
        self.assertIsNone(rpc._CALLER_CLASS.get())

    def test_partial_run_retains_completed_account_and_resumes_only_missing_reads(self):
        first, second = self.write_accounts(["ordinary.near", "second.near"])
        fixtures = self.fixtures(self.directory / "source")
        for account in ("ordinary.near", "second.near"):
            self.old_account(fixtures, account)
        self.seed(2995)
        with mock.patch.object(rpc, "_fetch", side_effect=self.fetch_from(fixtures)) as fetched:
            self.assertEqual(self.command(True), 1)
        self.assertEqual(fetched.call_count, 5)
        self.assertEqual(self.answer(first)["verdict"], "ALLOW")
        self.assertFalse((self.out / "judgements" / f"{second}.json").exists())
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.out / "rpc").glob("*.json")}
        self.seed(0)
        with mock.patch.object(rpc, "_fetch", side_effect=self.fetch_from(fixtures)) as fetched:
            self.assertEqual(self.command(True), 0, self.stderr)
        self.assertEqual(fetched.call_count, 3)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}, before)
        self.assertEqual(self.answer(second)["verdict"], "ALLOW")
        self.assertEqual(self.count("pooled"), 8)

    def test_key_rejections_stop_after_one_fetch_with_no_answer(self):
        self.write_accounts(["ordinary.near", "second.near"])
        for status in (401, 402, 403, 429):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary, \
                    mock.patch.dict(os.environ, {budget.DATABASE_ENV: str(Path(temporary) / budget.DATABASE_NAME)}), \
                    mock.patch.object(rpc, "_fetch", side_effect=rpc.HttpError(status, rpc.MAINNET_RPC_URL)) as fetched:
                self.assertEqual(self.command(True), 1)
                self.assertEqual(fetched.call_count, 1)
                self.assertIn(f"HTTP {status}", self.stderr)
                self.assertNotIn("unit-only-token", self.stderr)
                self.assertFalse((self.out / "judgements").exists())

    def test_spent_pool_cooldown_and_broken_database_stop_before_fetch(self):
        self.accounts.write_text("ordinary.near\n", encoding="utf-8")
        for kind in ("daily", "pool", "cooldown", "database"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                self.database = Path(temporary) / budget.DATABASE_NAME
                with mock.patch.dict(os.environ, {budget.DATABASE_ENV: str(self.database)}):
                    self.seed(3000 if kind == "daily" else 0)
                    if kind == "pool":
                        with closing(sqlite3.connect(self.database)) as connection, connection:
                            connection.execute("INSERT INTO counters VALUES(?,?,?,NULL)",
                                ("pooled", int(rpc.time.time() // budget.DAY_S), budget.POOLED_DAILY))
                    elif kind == "cooldown":
                        budget.key_failure(429, rpc.time.time())
                    elif kind == "database":
                        os.environ[budget.DATABASE_ENV] = temporary
                    self.assertEqual(self.command(True), 1)
                    self.assertIn("budget", self.stderr.lower())
                    self.assertFalse((self.out / "judgements").exists())

    def test_resume_refuses_changed_accounts_store_reports_or_rules(self):
        accounts = self.write_accounts(["ordinary.near", "second.near"])
        self.saved_run()
        self.assertEqual(self.command(), 0, self.stderr)
        checkpoint = self.out / "set.json"
        before = checkpoint.read_bytes()
        for field, value in (("accounts", list(reversed(accounts))),
                ("label_store_sha256", "different"), ("reports_account", "reports.near"),
                ("rules_version", 1), ("judged_at_ms", True)):
            with self.subTest(field=field):
                document = json.loads(before)
                document[field] = value
                checkpoint.write_text(json.dumps(document), encoding="utf-8")
                with mock.patch.object(judge_accounts.top_receivers, "fastnear_headers") as headers:
                    self.assertEqual(self.command(True), 1)
                    self.assertIn("saved run differs", self.stderr)
                    headers.assert_not_called()
        checkpoint.write_bytes(before)
        self.accounts.write_text("ordinary.near\n", encoding="utf-8")
        self.assertEqual(self.command(), 1)
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_missing_checkpoint_never_reuses_unbound_judgements(self):
        self.accounts.write_text("ordinary.near\n", encoding="utf-8")
        self.saved_run()
        self.assertEqual(self.command(), 0, self.stderr)
        (self.out / "set.json").unlink()
        self.assertEqual(self.command(True), 1)
        self.assertIn("without a saved run", self.stderr)

    def test_only_first_eighty_accounts_record_nearblocks_and_never_send_the_key(self):
        accounts = self.write_accounts([f"subject{index:03}.near" for index in range(81)])

        def judge(entry, store, client, nearblocks, now_ms, archival, reports_account, *, rules_version):
            try:
                nearblocks.get(f"/v3/accounts/{entry['subject']}/keys")
                verdict = "ALLOW"
            except rpc.FixtureMiss:
                verdict = "UNKNOWN"
            return {**entry, "verdict": verdict,
                "checks": [{"id": "account_age", "status": "unknown" if verdict == "UNKNOWN" else "pass"}],
                "reasons": [], "labels": [], "observed": {}}

        with mock.patch.object(intel_main, "_judge", side_effect=judge), \
                mock.patch.object(intel_main, "top_judged_block", return_value=None), \
                mock.patch.object(rpc.Client, "_respect_nearblocks"), \
                mock.patch.object(rpc, "_fetch", return_value={"data": []}) as fetched:
            self.assertEqual(self.command(True), 0, self.stderr)
        self.assertEqual(fetched.call_count, 80)
        self.assertTrue(all(call.args[2] == {} for call in fetched.call_args_list))
        self.assertEqual(self.count("keyless:recording:nearblocks"), 80)
        self.assertEqual(self.count("pooled"), 0)
        self.assertEqual(self.answer(accounts[-1])["verdict"], "UNKNOWN")
        self.assertIs(self.answer(accounts[-1])["scoreable"], False)
        self.assertTrue(all("scoreable" not in self.answer(account) for account in accounts[:-1]))


class PinnedJudgeAccountsTest(JudgeAccountsCase):
    def setUp(self):
        super().setUp()
        self.archive = recording.FakeArchive()
        self.now_ms = (recording.START_NS + recording.JUDGED * 10**9) // 10**6 + 500

    def fetch_archive(self, url, body, headers, errors):
        if body is None:
            return {"data": []}
        return self.archive.fetch(url, body, headers, errors)

    def test_pinned_record_and_replay_match_top_receiver_fallback_under_v3(self):
        self.write_accounts(recording.CREATED)
        with mock.patch.object(judge_accounts.time, "time", return_value=self.now_ms / 1000), \
                mock.patch.object(rpc, "_fetch", side_effect=self.fetch_archive):
            self.assertEqual(self.command(True), 0, self.stderr)
        block = json.loads((self.out / "judged-block.json").read_text(encoding="utf-8"))
        self.assertEqual(block["height"], recording.JUDGED)
        self.assertEqual(block["next_height"], recording.JUDGED + 2)
        expected = {"old.near": "ALLOW", "aged.near": "ALLOW", "young.near": "UNKNOWN",
                    "missing.near": "UNKNOWN", "flaky.near": "UNKNOWN"}
        self.assertEqual({account: self.answer(account)["verdict"] for account in recording.CREATED}, expected)
        self.assertEqual(self.answer("aged.near")["observed"]["block_height"], recording.JUDGED)
        self.assertEqual(self.answer("young.near")["observed"]["block_height"], recording.JUDGED)
        for account in ("missing.near", "flaky.near"):
            self.assertEqual(self.answer(account)["observed"]["block_height"], recording.LATER)
        before = {path: path.read_bytes() for path in (self.out / "judgements").glob("*.json")}
        self.assertEqual(self.command(), 0, self.stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        with mock.patch.object(intel_main, "RECEIVER_RPC_DIR", self.out / "rpc"), \
                mock.patch.object(intel_main, "JUDGED_BLOCK_PATH", self.out / "judged-block.json"):
            document = {"judged_at_ms": self.now_ms, "subjects": [
                {"subject": account, "as": "receiver", "request": intel_main._request(account, False)}
                for account in recording.CREATED]}
            results = intel_main.judge_receivers(document, self.store, self.directory / "top-parity",
                                                  pin_unknown_age=True, rules_version=3)
        for result in results:
            self.assertEqual(self.answer(result["subject"]), {**result, "rules_version": 3,
                **({"scoreable": False} if result["subject"] == "flaky.near" else {})})

    def test_invalid_saved_block_proof_stops_before_judgements_are_overwritten(self):
        self.accounts.write_text("aged.near\n", encoding="utf-8")
        with mock.patch.object(judge_accounts.time, "time", return_value=self.now_ms / 1000), \
                mock.patch.object(rpc, "_fetch", side_effect=self.fetch_archive):
            self.assertEqual(self.command(True), 0, self.stderr)
        answer = self.answer("aged.near")
        path = self.out / "judged-block.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for field, value in (("judged_at_ms", self.now_ms + 1), ("hash", recording._hash(recording.JUDGED - 1))):
            with self.subTest(field=field):
                path.write_text(json.dumps({**original, field: value}), encoding="utf-8")
                self.assertEqual(self.command(), 1)
                self.assertEqual(self.answer("aged.near"), answer)

    def test_malformed_saved_block_is_refused_without_replacing_the_answer(self):
        self.accounts.write_text("ordinary.near\n", encoding="utf-8")
        self.old_account(self.fixtures(), "ordinary.near")
        self.saved_run()
        self.assertEqual(self.command(), 0, self.stderr)
        answer = self.answer("ordinary.near")
        path = self.out / "judged-block.json"
        for value in (None, [], {}, {"judged_at_ms": self.now_ms}, {"height": True}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(self.command(), 1)
                self.assertIn("malformed", self.stderr)
                self.assertEqual(self.answer("ordinary.near"), answer)


if __name__ == "__main__":
    unittest.main()
