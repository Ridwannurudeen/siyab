import json
import multiprocessing
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from intel import ask
from tests.test_ask import SERVICE_ANSWER, SERVICE_SUBJECT, SERVICE_USAGE, SERVICE_VERDICT, ServiceCase


def reserve_process(path, ready, start, results, index):
    ledger = ask.Ledger(path, daily_cap=300)
    ready.put(index)
    if not start.wait(10):
        results.put(("error", "start timeout"))
        return
    try:
        reservation = ledger.reserve("user", "team", True, 1200, 120, lambda: 100)
    except ask.Refused as error:
        results.put(("refused", error.reason))
        return
    except Exception as error:
        results.put(("error", repr(error)))
        return
    usage = {"input_tokens": 1200, "output_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0} if index % 2 == 0 else None
    ledger.settle(reservation, usage)
    results.put(("settled" if usage is not None else "retained", reservation.micro))


class LedgerRoundTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ask.sqlite3"

    def counters(self, day):
        with closing(sqlite3.connect(self.path)) as connection:
            return dict(connection.execute("SELECT counter,value FROM counters WHERE day=?", (day,)))

    def test_usage_counters_reject_values_outside_the_integer_bound(self):
        for name in SERVICE_USAGE:
            for value in (True, -1, 1.5, "1", None, 2 ** 53 + 1, 10 ** 40):
                with self.subTest(name=name, value=value):
                    usage = SERVICE_USAGE | {name: value}
                    self.assertFalse(ask.valid_usage(usage))
                    ledger = ask.Ledger(self.path)
                    reservation = ledger.reserve("user", "team", True, 1000, 200, lambda: 100)
                    before = self.counters(0)
                    ledger.settle(reservation, usage)
                    self.assertEqual(self.counters(0), before)
            for value in (0, 2 ** 53):
                with self.subTest(name=name, value=value):
                    self.assertTrue(ask.valid_usage(SERVICE_USAGE | {name: value}))

    def test_process_reservations_never_exceed_the_shared_cap(self):
        context = multiprocessing.get_context("spawn")
        ready, results = context.Queue(), context.Queue()
        start = context.Event()
        with ask._transaction(self.path):
            pass
        processes = [context.Process(target=reserve_process,
                     args=(self.path, ready, start, results, index)) for index in range(6)]
        try:
            for process in processes:
                process.start()
            self.assertEqual({ready.get(timeout=15) for _ in processes}, set(range(6)))
            start.set()
            outcomes = [results.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            self.assertFalse([result for result in outcomes if result[0] == "error"], outcomes)
            accepted = [result for result in outcomes if result[0] in ("settled", "retained")]
            self.assertEqual(len(accepted), 2)
            self.assertEqual(sum(result[1] for result in accepted), 240)
            self.assertEqual(self.counters(0), {"calls": 2, "spend_micro": 240,
                                             "tokens:user:user": 2400, "tokens:team:team": 2400})
        finally:
            start.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            ready.close()
            results.close()


class RequestBoundTests(ServiceCase):
    def test_usage_outside_request_bounds_keeps_answer_reservation_without_check(self):
        projection = ask.project(SERVICE_SUBJECT, SERVICE_VERDICT, self.store)
        for field in SERVICE_USAGE:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                service = ask.AskService(directory, self.store, self.read, client_factory=self.factory,
                                         clock=lambda: self.now)
                params = ask.build_request(service.model, service.effort, projection, ask.QUESTIONS["why_verdict"])
                tokens, micro = ask.reservation_bound(params)
                self.client.create.reset_mock()
                self.client.create.return_value["usage"] = SERVICE_USAGE | {field: 6000000}
                self.unavailable("invalid_output", self.answer(service))
                self.assertEqual(self.client.create.call_count, 1)
                with closing(sqlite3.connect(service.ledger.path)) as connection:
                    counters = dict(connection.execute("SELECT counter,value FROM counters"))
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
                self.assertEqual(counters, {"calls": 1, "spend_micro": micro,
                    "tokens:user:user": tokens, "tokens:team:team": tokens})

    def test_aggregate_input_bound_and_maximum_cost_are_reserved(self):
        service = self.service()
        projection = ask.project(SERVICE_SUBJECT, SERVICE_VERDICT, self.store)
        for params in (ask.build_request(service.model, service.effort, projection, "What was read?"),):
            bound = len(ask.projection_bytes(params)) + 4096
            tokens, micro = ask.reservation_bound(params)
            self.assertEqual(tokens, bound + params["max_tokens"])
            usage = dict.fromkeys(SERVICE_USAGE, 0)
            usage.update(cache_creation_input_tokens=bound, output_tokens=params["max_tokens"])
            self.assertTrue(ask.valid_usage(usage, params))
            self.assertLessEqual(ask.usage_cost(usage, service.model)[1], micro)
            self.assertFalse(ask.valid_usage(usage | {"input_tokens": 1}, params))
            self.assertFalse(ask.valid_usage(usage | {"output_tokens": params["max_tokens"] + 1}, params))

    def test_malformed_current_cache_explanations_are_deleted_and_refetched(self):
        projection = ask.project(SERVICE_SUBJECT, SERVICE_VERDICT, self.store)
        for body in ({}, [], {"answerable": "yes", "statements": [], "not_in_evidence": []},
                     SERVICE_ANSWER | {"statements": ["wrong"]},
                     SERVICE_ANSWER | {"not_in_evidence": [1]}):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                service = ask.AskService(directory, self.store, self.read, client_factory=self.factory,
                                         clock=lambda: self.now)
                key = ask.cache_key(service.model, service.effort, "why_verdict", projection)
                service.cache.put(key, body, self.now)
                self.client.create.reset_mock()
                self.assertEqual(self.answer(service)[1]["status"], "explained")
                self.assertEqual(self.client.create.call_count, 1)
                self.assertNotEqual(service.cache.get(key, self.now), body)

    def test_null_cache_explanation_is_deleted_before_an_unavailable_refetch(self):
        service = self.service()
        projection = ask.project(SERVICE_SUBJECT, SERVICE_VERDICT, self.store)
        key = ask.cache_key(service.model, service.effort, "why_verdict", projection)
        service.cache.put(key, None, self.now)
        self.key.return_value = None
        self.unavailable("key_unavailable", self.answer(service))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)


class MidnightServiceTests(ServiceCase):
    def test_lock_delay_enforces_every_new_day_cap_before_the_call(self):
        for counter, reason in (("spend_micro", "spend_cap"), ("spend_micro_public", "spend_cap"),
                                ("tokens:user:user", "user_quota"), ("tokens:team:team", "team_quota")):
            with self.subTest(counter=counter), tempfile.TemporaryDirectory() as directory:
                self.now = 86399
                service = ask.AskService(directory, self.store, self.read, client_factory=self.factory,
                                         clock=lambda: self.now)
                limit = {"spend_micro": service.ledger.daily_cap,
                         "spend_micro_public": service.ledger.daily_cap * 2 // 3,
                         "tokens:user:user": service.ledger.user_tokens,
                         "tokens:team:team": service.ledger.team_tokens}[counter]
                with ask._transaction(service.ledger.path) as connection:
                    connection.execute("INSERT INTO counters VALUES(?,?,?)", (counter, 1, limit))
                self.client.create.reset_mock()
                def enter():
                    self.now = 86400
                with mock.patch.object(ask, "_DATABASE_LOCK") as lock:
                    lock.__enter__.side_effect = enter
                    response = service.answer({"receiver": "example.near", "amount": "1", "token": "near",
                        "question": "What was recorded?"}, user_id="user", team_id="team", reserved=False,
                        peer="127.0.0.1", headers={})
                body = self.unavailable(reason, response)
                self.assertEqual(body["retry_after_s"], 86400)
                self.client.create.assert_not_called()
                with closing(sqlite3.connect(service.ledger.path)) as connection:
                    self.assertEqual(dict(connection.execute("SELECT counter,value FROM counters WHERE day=1")),
                                     {counter: limit})
                    self.assertIsNone(connection.execute(
                        "SELECT value FROM counters WHERE counter='spend_micro' AND day=0").fetchone())

    def test_timeout_at_token_quota_keeps_one_attempt_and_its_reason(self):
        for setting in ("user_tokens", "team_tokens"):
            with self.subTest(setting=setting), tempfile.TemporaryDirectory() as directory:
                service = ask.AskService(directory, self.store, self.read, client_factory=self.factory,
                                         clock=lambda: self.now)
                projection = ask.project({"network": "mainnet", "receiver": "example.near", "amount": "1", "token": "near"},
                                         SERVICE_VERDICT, self.store)
                tokens, micro = ask.reservation_bound(ask.build_request(service.model, service.effort, projection,
                                                                       ask.QUESTIONS["why_verdict"]))
                setattr(service.ledger, setting, tokens)
                self.client.create.reset_mock()
                self.client.create.side_effect = ask.ProviderTimeout
                body = self.unavailable("timeout", self.answer(service))
                self.assertIsNone(body["retry_after_s"])
                self.assertEqual(self.client.create.call_count, 1)
                with closing(sqlite3.connect(service.ledger.path)) as connection:
                    counters = dict(connection.execute("SELECT counter,value FROM counters"))
                self.assertEqual(counters, {"calls": 1, "spend_micro": micro,
                    "tokens:user:user": tokens, "tokens:team:team": tokens})

    def test_timeout_at_spend_cap_keeps_one_attempt_and_its_reason(self):
        service = self.service()
        projection = ask.project({"network": "mainnet", "receiver": "example.near", "amount": "1", "token": "near"},
                                 SERVICE_VERDICT, self.store)
        tokens, micro = ask.reservation_bound(ask.build_request(service.model, service.effort, projection,
                                                               ask.QUESTIONS["why_verdict"]))
        service.ledger.daily_cap = micro
        self.client.create.side_effect = ask.ProviderTimeout
        body = self.unavailable("timeout", self.answer(service))
        self.assertIsNone(body["retry_after_s"])
        self.assertEqual(self.client.create.call_count, 1)
        self.assertEqual(self.count("calls"), 1)
        self.assertEqual(self.count("spend_micro"), micro)
        self.assertEqual(self.count("tokens:user:user"), tokens)


if __name__ == "__main__":
    unittest.main()
