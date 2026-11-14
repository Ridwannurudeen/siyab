"""Live route controls: client keys, slot order and shares, cost weights, normalised cache keys, single flight,
portfolio caching, the reports cap, the NearBlocks stand-in and provider budget refusals."""

import email.message
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from gate import rpc, rules
from intel import api, budget, labels, live, portfolio


def account_answer(path, query):
    return 200, {"path": path, "query": query}


class Blocking:
    """A _route stand-in whose calls wait until released; entered counts the calls that began."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)
        self.calls = []

    def __call__(self, path, query):
        self.calls.append((path, query))
        self.entered.release()
        self.release.wait(5)
        return 200, {"path": path}


class LiveLimitCase(unittest.TestCase):
    def setUp(self):
        self.now = [600.0]
        self.service = live.LiveAPI(object(), clock=lambda: self.now[0], wall_clock=lambda: 1000.0)
        for target in ("urllib.request.urlopen", "gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            network = mock.patch(target, side_effect=AssertionError("unexpected network read"))
            network.start()
            self.addCleanup(network.stop)

    def request(self, path, peer="203.0.113.1", headers=None):
        return self.service.handle(path, peer, headers or {})

    def threaded(self, *requests):
        answers = {}
        threads = [threading.Thread(target=lambda index=index, item=item: answers.__setitem__(index, self.request(*item)))
                   for index, item in enumerate(requests)]
        for thread in threads:
            thread.start()
        return threads, answers


class ClientKeyTest(LiveLimitCase):
    def test_the_rightmost_forwarded_entry_names_the_client_only_from_the_local_proxy(self):
        key = live.LiveAPI._client_ip
        self.assertEqual(key("127.0.0.1", {"X-Forwarded-For": "203.0.113.8, 198.51.100.7"}), "198.51.100.7")
        self.assertEqual(key("127.0.0.1", {"X-Forwarded-For": "198.51.100.7"}), "198.51.100.7")
        self.assertEqual(key("127.0.0.1", {"X-Forwarded-For": "203.0.113.8, not-an-address"}), "127.0.0.1")
        self.assertEqual(key("127.0.0.1", {}), "127.0.0.1")
        self.assertEqual(key("203.0.113.9", {"X-Forwarded-For": "198.51.100.7"}), "203.0.113.9")
        message = email.message.Message()
        message["X-Forwarded-For"] = "203.0.113.8"
        message["X-Forwarded-For"] = "192.0.2.4, 198.51.100.7"
        self.assertEqual(key("127.0.0.1", message), "198.51.100.7")

    def test_two_ipv6_addresses_in_one_64_share_a_bucket(self):
        key = live.LiveAPI._client_ip
        first, second = key("2001:db8:1:2::1", {}), key("2001:db8:1:2:ffff:ee::9", {})
        self.assertEqual(first, second)
        self.assertEqual(first, "2001:db8:1:2::/64")
        self.assertNotEqual(first, key("2001:db8:1:3::1", {}))
        self.assertEqual(key("127.0.0.1", {"X-Forwarded-For": "2001:db8:1:2::77"}), first)
        self.assertEqual(key("::ffff:203.0.113.8", {}), "203.0.113.8")
        self.assertNotEqual(key("203.0.113.8", {}), key("203.0.113.9", {}))
        self.service.rate_per_minute = 1
        self.assertEqual(self.request("/live/health", "2001:db8:1:2::1")[0], 200)
        status, body, headers = self.request("/live/health", "2001:db8:1:2:ffff::2")
        self.assertEqual((status, body["error"]), (429, "rate_limited"))
        self.assertEqual(self.request("/live/health", "2001:db8:1:3::1")[0], 200)


class SlotTest(LiveLimitCase):
    def test_a_rate_limited_client_cannot_hold_a_slot(self):
        self.service.rate_per_minute = 1
        self.assertEqual(self.request("/live/health")[0], 200)
        with mock.patch.object(self.service, "_slots") as slots:
            status, body, headers = self.request("/live/verdict?receiver=alice.near&amount=1&token=near")
        self.assertEqual((status, body["error"]), (429, "rate_limited"))
        self.assertEqual(headers["Retry-After"], "60")
        slots.acquire.assert_not_called()

    def wait_for_admitted(self, count):
        until = time.monotonic() + 2
        while time.monotonic() < until:
            with self.service._lock:
                if self.service._in_flight.get("203.0.113.1", [0])[0] == count:
                    return
            time.sleep(0.005)
        self.fail("requests did not enter the queue")

    def test_fifth_read_waits_and_another_address_keeps_a_slot(self):
        route = Blocking()
        with mock.patch.object(self.service, "_route", side_effect=route):
            threads, answers = self.threaded(*[(f"/live/read-{i}",) for i in range(4)])
            try:
                for _ in range(4):
                    self.assertTrue(route.entered.acquire(timeout=2))
                queued, queued_answers = self.threaded(*[(f"/live/read-{i}",) for i in range(4, 12)])
                threads += queued
                self.wait_for_admitted(12)
                self.assertEqual(len(route.calls), 4)
                status, body, headers = self.request("/live/thirteen")
                self.assertEqual((status, body["error"], headers["Retry-After"]),
                                 (429, "too_many_in_flight", "1"))
                others, other_answers = self.threaded(("/live/other", "203.0.113.2"))
                threads += others
                self.assertTrue(route.entered.acquire(timeout=2))
            finally:
                route.release.set()
                for thread in threads:
                    thread.join(3)
        self.assertEqual([answer[0] for answer in answers.values()], [200] * 4)
        self.assertEqual([answer[0] for answer in queued_answers.values()], [200] * 8)
        self.assertEqual(other_answers[0][0], 200)
        self.assertEqual(self.service._in_flight, {})

    def test_third_heavy_read_waits_for_a_client_turn(self):
        route = Blocking()
        with mock.patch.object(self.service, "_route", side_effect=route):
            threads, answers = self.threaded(*[(f"/live/accounts/a{i}.near/graph",) for i in range(2)])
            try:
                for _ in range(2):
                    self.assertTrue(route.entered.acquire(timeout=2))
                queued, queued_answers = self.threaded(("/live/accounts/a2.near/graph",))
                threads += queued
                self.wait_for_admitted(3)
                self.assertEqual(len(route.calls), 2)
            finally:
                route.release.set()
                for thread in threads:
                    thread.join(3)
        self.assertEqual(queued_answers[0][0], 200)
        self.assertEqual(self.service._in_flight, {})

    def test_client_turn_expires_with_retry_after(self):
        self.service = live.LiveAPI(object(), queue_wait_s=0.05)
        route = Blocking()
        with mock.patch.object(self.service, "_route", side_effect=route):
            threads, _ = self.threaded(*[(f"/live/read-{i}",) for i in range(4)])
            try:
                for _ in range(4):
                    self.assertTrue(route.entered.acquire(timeout=2))
                status, body, headers = self.request("/live/fifth")
                self.assertEqual((status, body["error"], headers["Retry-After"]),
                                 (429, "too_many_in_flight", "1"))
            finally:
                route.release.set()
                for thread in threads:
                    thread.join(3)
        self.assertEqual(self.service._in_flight, {})

    def test_running_key_follows_without_a_turn_and_queued_copies_recheck_flights(self):
        route = Blocking()
        with mock.patch.object(self.service, "_route", side_effect=route):
            threads, answers = self.threaded(*[(f"/live/read-{i}",) for i in range(4)])
            try:
                for _ in range(4):
                    self.assertTrue(route.entered.acquire(timeout=2))
                followers, follower_answers = self.threaded(("/live/read-0",), ("/live/new",), ("/live/new",))
                threads += followers
                self.wait_for_admitted(7)
                self.assertEqual(self.service._slots._value, live.MAX_IN_FLIGHT - 4)
            finally:
                route.release.set()
                for thread in threads:
                    thread.join(3)
        self.assertEqual(len(route.calls), 5)
        self.assertEqual(follower_answers[0][:2], answers[0][:2])
        self.assertEqual([answer[0] for answer in follower_answers.values()], [200] * 3)
        self.assertEqual(self.service._in_flight, {})
        self.assertEqual(self.service._flights, {})

    def test_a_turn_freed_after_queue_expiry_is_not_granted(self):
        self.service.queue_wait_s = 0.05
        now = [0.0]
        state = [4, 4, 0]
        self.service._in_flight["203.0.113.1"] = state

        def wake(remaining):
            now[0] = 0.06
            state[1] -= 1

        with mock.patch("intel.live.time.monotonic", side_effect=lambda: now[0]), \
                mock.patch.object(self.service._condition, "wait", side_effect=wake), \
                mock.patch.object(self.service, "_route", side_effect=account_answer) as route:
            status, body, headers = self.request("/live/late-turn")
        self.assertEqual((status, body["error"], headers["Retry-After"]),
                         (429, "too_many_in_flight", "1"))
        route.assert_not_called()
        self.assertEqual(state[0], 4)

    def test_slot_waits_then_succeeds_or_expires_with_retry_after(self):
        self.service = live.LiveAPI(object(), max_in_flight=1, queue_wait_s=0.05)
        with mock.patch.object(self.service, "_route", side_effect=account_answer):
            for slots, path in ((self.service._slots, "/live/health"),
                                (self.service._heavy_slots, "/live/accounts/a.near/graph")):
                self.assertTrue(slots.acquire(blocking=False))
                try:
                    status, body, headers = self.request(path)
                    self.assertEqual((status, body["error"], headers["Retry-After"]), (503, "busy", "1"))
                    threads, answers = self.threaded((path,))
                    time.sleep(0.01)
                finally:
                    slots.release()
                threads[0].join(2)
                self.assertEqual(answers[0][0], 200)
        self.assertEqual(self.service._in_flight, {})

    def test_request_deadline_includes_slot_wait(self):
        self.service = live.LiveAPI(object(), max_in_flight=1, request_timeout_s=0.05, queue_wait_s=0.2)
        self.service._slots.acquire()
        with mock.patch.object(self.service, "_route", side_effect=account_answer):
            threads, answers = self.threaded(("/live/health",))
            time.sleep(0.07)
            self.service._slots.release()
            threads[0].join(2)
        self.assertEqual(answers[0][0], 504)
        self.assertEqual(self.service._in_flight, {})

    def test_connection_capacity_includes_waiters(self):
        server = api.make_server("127.0.0.1", 0, store=object(), live_routes=self.service)
        try:
            self.assertEqual(server._connection_slots._value, self.service.max_in_flight * 4)
        finally:
            server.server_close()

    def test_heavy_routes_cannot_take_more_than_three_slots(self):
        self.service.queue_wait_s = 0.05
        route = Blocking()
        heavy = [f"/live/accounts/heavy-{index}.near/graph?depth=1" for index in range(4)]
        with mock.patch.object(self.service, "_route", side_effect=route):
            threads, answers = self.threaded(*[(path, f"203.0.113.{index + 10}") for index, path in enumerate(heavy[:3])])
            for _ in range(3):
                self.assertTrue(route.entered.acquire(timeout=5))
            status, body, _ = self.request(heavy[3], "203.0.113.20")
            self.assertEqual((status, body["error"]), (503, "busy"))
            light, _ = self.threaded(("/live/accounts/light.near/profile", "203.0.113.21"))
            self.assertTrue(route.entered.acquire(timeout=5))
            route.release.set()
            for thread in threads + light:
                thread.join(5)
        self.assertEqual(len(route.calls), 4)
        self.assertEqual(self.service._heavy_slots._value, live.HEAVY_SLOTS)
        self.assertEqual(self.service._slots._value, live.MAX_IN_FLIGHT)

    def test_queued_heavy_reads_leave_general_slots_for_a_fifth_client(self):
        route = Blocking()
        waiting = threading.Semaphore(0)
        acquire = self.service._heavy_slots.acquire

        def heavy_slot(*args, **kwargs):
            waiting.release()
            return acquire(*args, **kwargs)

        requests = [(f"/live/accounts/heavy-{client}-{index}.near/graph", f"203.0.113.{client}")
                    for client in range(1, 5) for index in range(2)]
        with mock.patch.object(self.service._heavy_slots, "acquire", side_effect=heavy_slot), \
                mock.patch.object(self.service, "_route", side_effect=route):
            threads, answers = self.threaded(*requests)
            try:
                for _ in requests:
                    self.assertTrue(waiting.acquire(timeout=2))
                for _ in range(live.HEAVY_SLOTS):
                    self.assertTrue(route.entered.acquire(timeout=2))
                self.assertEqual(self.service._slots._value, live.MAX_IN_FLIGHT - live.HEAVY_SLOTS)
                light, light_answers = self.threaded(("/live/accounts/light.near/profile", "203.0.113.5"))
                threads += light
                self.assertTrue(route.entered.acquire(timeout=1))
            finally:
                route.release.set()
                for thread in threads:
                    thread.join(3)
        self.assertEqual([answer[0] for answer in answers.values()], [200] * 8)
        self.assertEqual(light_answers[0][0], 200)
        self.assertEqual(self.service._slots._value, live.MAX_IN_FLIGHT)
        self.assertEqual(self.service._heavy_slots._value, live.HEAVY_SLOTS)
        self.assertEqual(self.service._in_flight, {})

    def test_the_global_ceiling_and_a_full_client_table_fail_closed(self):
        with mock.patch("intel.live.GLOBAL_PER_MINUTE", 3):
            for index in range(3):
                self.assertEqual(self.request("/live/health", f"203.0.113.{index}")[0], 200)
            status, body, headers = self.request("/live/health", "203.0.113.50")
            self.assertEqual((status, body["error"]), (503, "busy"))
            self.assertGreaterEqual(int(headers["Retry-After"]), 1)
            self.now[0] += 60
            self.assertEqual(self.request("/live/health", "203.0.113.50")[0], 200)
        with mock.patch("intel.live.MAX_RATE_CLIENTS", 2):
            self.now[0] += 60
            self.assertEqual(self.request("/live/health", "198.51.100.1")[0], 200)
            self.assertEqual(self.request("/live/health", "198.51.100.2")[0], 200)
            status, body, _ = self.request("/live/health", "198.51.100.3")
            self.assertEqual((status, body["error"]), (503, "busy"))
            self.assertEqual(self.request("/live/health", "198.51.100.1")[0], 200)
            self.now[0] += 60
            self.assertEqual(self.request("/live/health", "198.51.100.3")[0], 200)


class CostTest(LiveLimitCase):
    def test_route_weights(self):
        cost = live.LiveAPI._cost
        weights = {
            ("portfolio", ""): 46, ("portfolio", f"max_tokens={portfolio.MAX_TOKENS}"): 6 + portfolio.MAX_TOKENS,
            ("portfolio", f"max_tokens={portfolio.MAX_TOKENS + 1}"): 6 + portfolio.MAX_TOKENS,
            ("portfolio", "max_tokens=x"): 46,
            ("portfolio-at-block", "block_height=1"): 2,
            ("portfolio-at-block", "block_height=1&contracts=" + ",".join(f"t{i}.near" for i in range(20))): 42,
            ("holdings", ""): 21, ("holdings", "offset=0&limit=5"): 6, ("verdict", ""): 10, ("reports/recent", ""): 21,
            ("reports", ""): 22, ("graph", "depth=2"): 6, ("identity", ""): 4, ("intents", ""): 4, ("scan", ""): 2,
            ("backfill", ""): 2, ("profile", ""): 1, ("transactions", ""): 1, ("transfers", ""): 1, ("health", ""): 1,
            ("history", ""): 1,
        }
        for (route, query), weight in weights.items():
            with self.subTest(route=route, query=query):
                self.assertEqual(cost(route, query), weight)

    @mock.patch.object(portfolio, "MAX_TOKENS", 40)
    def test_portfolio_max_tokens_is_validated_against_the_portfolio_ceiling(self):
        data = mock.Mock()
        data.inspect.return_value = {"account_id": "wrap.near"}
        service = live.LiveAPI(object(), clock=lambda: self.now[0], portfolio_data=data)
        path = "/live/accounts/wrap.near/portfolio?max_tokens="
        self.assertEqual(service.handle(path + str(portfolio.MAX_TOKENS + 1), "203.0.113.1", {})[0], 400)
        data.inspect.assert_not_called()
        self.assertEqual(service.handle(path + str(portfolio.MAX_TOKENS), "203.0.113.1", {})[0], 200)
        data.inspect.assert_called_once_with("wrap.near", max_tokens=portfolio.MAX_TOKENS)

    def test_cache_hits_cost_one_unit_without_copying_before_the_rate_check(self):
        path = "/live/accounts/alice.near/portfolio"
        with mock.patch.object(self.service, "_route", side_effect=account_answer) as route:
            self.assertEqual(self.request(path)[0], 200)
            for _ in range(5):
                self.assertEqual(self.request(path)[0], 200)
            self.assertEqual(self.service._rates["203.0.113.1"][2], 46 + 5)
            self.assertEqual(route.call_count, 1)
            with mock.patch.object(self.service, "_rate_limit", return_value=(429, "rate_limited", "wait", 1)), \
                    mock.patch("intel.live.copy.deepcopy", side_effect=AssertionError("copy before rate check")):
                self.assertEqual(self.request(path)[0], 429)

    def test_a_charged_cache_hit_survives_eviction_during_the_rate_check(self):
        path = "/live/accounts/alice.near/portfolio"
        with mock.patch.object(self.service, "_route", side_effect=account_answer) as route:
            expected = self.request(path)
            rate_limit = self.service._rate_limit

            def evict(client, cost):
                self.assertEqual(cost, 1)
                result = rate_limit(client, cost)
                with self.service._lock:
                    self.service._cache.clear()
                    self.service._cache_bytes = 0
                return result

            with mock.patch.object(self.service, "_rate_limit", side_effect=evict):
                answer = self.request(path)
            self.assertEqual(answer, expected)
            self.assertEqual(route.call_count, 1)
            self.assertEqual(self.service._rates["203.0.113.1"][2], 47)
            answer[1]["path"] = "changed"
            self.assertEqual(expected[1]["path"], path)

    def test_cost_weights_refuse_a_heavy_client_before_the_request_limit(self):
        with mock.patch.object(self.service, "_route", side_effect=account_answer) as route:
            for index in range(5):
                self.assertEqual(self.request(f"/live/accounts/a{index}.near/portfolio")[0], 200)
            status, body, headers = self.request("/live/accounts/a5.near/portfolio")
            self.assertEqual((status, body["error"]), (429, "rate_limited"))
            self.assertIn("cost", body["message"])
            self.assertEqual(headers["Retry-After"], "60")
            self.assertEqual(self.request("/live/accounts/a5.near/profile")[0], 200)
            self.assertEqual(route.call_count, 6)
            self.now[0] += 60
            self.assertEqual(self.request("/live/accounts/a5.near/portfolio")[0], 200)
        self.assertLess(6, self.service.rate_per_minute)


class CacheTest(LiveLimitCase):
    def test_cache_keys_are_built_from_decoded_sorted_lower_cased_values(self):
        with mock.patch.object(self.service, "_route", side_effect=account_answer) as route:
            self.request("/live/accounts/wrap.near/holdings?limit=5&offset=0")
            self.request("/live/accounts/wrap%2Enear/holdings?offset=0&limit=5", "203.0.113.2")
            self.request("/live/scan/agent.near")
            self.request("/live/scan/agent%2Enear")
            recipient = "0x00000000000000000000000000000000000000aB"
            self.request(f"/live/intents?destination_asset=x&recipient={recipient}&amount=1")
            self.request(f"/live/intents?amount=1&recipient={recipient.lower()}&destination_asset=x")
            self.request("/live/verdict?receiver=a.near&amount=1&token=near")
            self.request("/live/verdict?receiver=a.near&amount=2&token=near")
        self.assertEqual(route.call_count, 5)

    def test_single_flight_makes_one_upstream_read_for_concurrent_identical_requests(self):
        route = Blocking()
        with mock.patch.object(self.service, "_route", side_effect=route):
            threads, answers = self.threaded(("/live/accounts/wrap.near/profile", "203.0.113.1"))
            self.assertTrue(route.entered.acquire(timeout=5))
            followers, follower_answers = self.threaded(("/live/accounts/wrap%2Enear/profile", "203.0.113.2"),
                                                        ("/live/accounts/wrap.near/profile", "203.0.113.3"))
            time.sleep(0.1)
            self.assertEqual(self.service._slots._value, live.MAX_IN_FLIGHT - 1)
            route.release.set()
            for thread in threads + followers:
                thread.join(5)
        self.assertEqual(len(route.calls), 1)
        self.assertEqual([answer[:2] for answer in follower_answers.values()], [answers[0][:2]] * 2)
        self.assertEqual(self.service._flights, {})

    def test_the_current_portfolio_is_cached_for_a_minute_by_account_and_max_tokens(self):
        data = mock.Mock()
        data.inspect.side_effect = lambda account, **values: {"account_id": account, "values": values}
        service = live.LiveAPI(object(), clock=lambda: self.now[0], portfolio_data=data)
        first = service.handle("/live/accounts/wrap.near/portfolio?max_tokens=10", "203.0.113.1", {})
        self.assertEqual(service.handle("/live/accounts/wrap%2Enear/portfolio?max_tokens=10", "203.0.113.2", {}), first)
        service.handle("/live/accounts/wrap.near/portfolio?max_tokens=20", "203.0.113.1", {})
        self.assertEqual(data.inspect.call_count, 2)
        self.now[0] += live.CACHE_TTL_S
        service.handle("/live/accounts/wrap.near/portfolio?max_tokens=10", "203.0.113.1", {})
        self.assertEqual(data.inspect.call_count, 3)

    def test_a_complete_block_pinned_portfolio_is_kept_without_expiry_and_a_partial_one_for_a_minute(self):
        def settled(account, height, contracts=()):
            assets = [{"balance_status": "historical", "metadata": {"decimals": 24}}]
            assets += [{"balance_status": "historical" if contract != "gone.near" else "unavailable",
                        "metadata": {"decimals": 6} if contract != "gone.near" else None} for contract in contracts]
            return {"assets": assets, "summary": {"unpriced_assets": 0}, "contracts": contracts}

        data = mock.Mock()
        data.historical.side_effect = settled
        service = live.LiveAPI(object(), clock=lambda: self.now[0], portfolio_data=data)
        complete = "/live/accounts/wrap.near/portfolio-at-block?block_height=7&contracts=b.near,a.near"
        self.assertEqual(service.handle(complete, "203.0.113.1", {})[0], 200)
        self.now[0] += 30 * 24 * 60 * 60
        service.handle("/live/accounts/wrap.near/portfolio-at-block?contracts=a.near,b.near&block_height=7",
                       "203.0.113.1", {})
        self.assertEqual(data.historical.call_count, 1)
        partial = "/live/accounts/wrap.near/portfolio-at-block?block_height=7&contracts=gone.near"
        service.handle(partial, "203.0.113.1", {})
        service.handle(partial, "203.0.113.1", {})
        self.assertEqual(data.historical.call_count, 2)
        self.now[0] += live.CACHE_TTL_S
        service.handle(partial, "203.0.113.1", {})
        self.assertEqual(data.historical.call_count, 3)
        service.cache_size = 1
        service.handle("/live/accounts/other.near/portfolio-at-block?block_height=7", "203.0.113.1", {})
        service.handle(complete, "203.0.113.1", {})
        self.assertEqual(data.historical.call_count, 5)
        self.assertLessEqual(len(service._cache), 1)

    def test_a_real_complete_historical_read_is_kept_without_expiry(self):
        from tests.test_portfolio import NOW, Providers, Reader

        providers = Providers()
        for price in providers.prices.values():
            price["timestamp"] = NOW - 86400
        data = portfolio.PortfolioData(Reader(), client_factory=providers.factory, clock=lambda: NOW)
        service = live.LiveAPI(object(), clock=lambda: self.now[0], portfolio_data=data)
        path = "/live/accounts/alice.near/portfolio-at-block?block_height=50&contracts=wrap.near"
        first = service.handle(path, "203.0.113.1", {})
        self.assertEqual(first[0], 200)
        calls = len(providers.calls) + len(providers.views)
        self.now[0] += 30 * 24 * 60 * 60
        self.assertEqual(service.handle(path, "203.0.113.1", {}), first)
        self.assertEqual(len(providers.calls) + len(providers.views), calls)


class ReportsCapTest(LiveLimitCase):
    def reports(self, total):
        calls = []

        def view(_client, contract, method, args, *, block_id=None):
            calls.append(method)
            if method == "subject_summary":
                return {"accepted": 0, "pending": total, "contested": 0, "latest_accepted_id": None,
                        "latest_deny_id": None, "deny": False}
            if method == "reports_by_subject":
                return [{"id": index} for index in range(total)][args["from"]:args["from"] + args["limit"]]
            if method == "get_report":
                return {"id": args["id"], "subject": "vendor.testnet", "kind": "theft", "outcome": "near_miss",
                        "state": "accepted", "evidence_url": "https://evidence.example/report",
                        "evidence_sha256": "stubbed-report-hash", "submitter": "reporter.testnet", "moderator": None}
            raise AssertionError(method)

        with mock.patch("intel.live.rpc.Client.call_function", autospec=True, side_effect=view):
            status, body, _ = self.request("/live/reports?subject=vendor.testnet")
        self.assertEqual(status, 200)
        return body, calls

    def test_report_index_doubling_stops_at_the_integer_bound(self):
        offsets = []

        def view(_client, contract, method, args, *, block_id=None):
            if method == "subject_summary":
                return {"accepted": 0, "pending": 0, "contested": 0, "latest_accepted_id": None,
                        "latest_deny_id": None, "deny": False}
            self.assertEqual(method, "reports_by_subject")
            offsets.append(args["from"])
            if len(offsets) > 70:
                raise RuntimeError("unbounded report index")
            return [{"id": 1}] * args["limit"]

        with mock.patch("intel.live.rpc.Client.call_function", autospec=True, side_effect=view):
            status, body, _ = self.request("/live/reports?subject=vendor.testnet")
        self.assertEqual((status, body["error"]), (503, "read_failed"))
        self.assertTrue(all(offset <= 2 ** 64 for offset in offsets))
        self.assertLessEqual(len(offsets), 70)

    def test_the_subject_route_returns_the_newest_20_reports_with_a_truncated_flag(self):
        body, calls = self.reports(120)
        self.assertEqual([row["id"] for row in body["reports"]], list(range(100, 120)))
        self.assertTrue(body["truncated"])
        self.assertEqual(calls.count("get_report"), 20)
        self.assertLessEqual(calls.count("reports_by_subject"), 12)
        for total in (0, 3, 20, 50):
            with self.subTest(total=total):
                self.now[0] += 60
                body, calls = self.reports(total)
                self.assertEqual([row["id"] for row in body["reports"]], list(range(max(0, total - 20), total)))
                self.assertEqual(body["truncated"], total > 20)
                self.assertEqual(calls.count("get_report"), min(total, 20))


class VerdictNearBlocksTest(LiveLimitCase):
    def judged_nearblocks(self, flag):
        environment = {"SIYAB_VERDICT_NEARBLOCKS": flag} if flag is not None else {}
        judgement = rules.Judgement("UNKNOWN", [], [], [], {"block_height": None, "timestamp_ms": 1})
        with mock.patch.dict(os.environ, environment), \
                mock.patch("intel.live.rules.judge", return_value=judgement) as judge, \
                mock.patch("intel.live.rules.issuers_for", return_value=[]):
            if flag is None:
                os.environ.pop("SIYAB_VERDICT_NEARBLOCKS", None)
            self.now[0] += 60
            self.assertEqual(self.request("/live/verdict?receiver=young.near&amount=1&token=near")[0], 200)
        return judge.call_args.args[4]

    def test_the_verdict_never_calls_nearblocks_unless_the_flag_is_set_and_then_age_reads_unknown(self):
        for flag in (None, "0", "true"):
            with self.subTest(flag=flag):
                nearblocks = self.judged_nearblocks(flag)
                self.assertNotIsInstance(nearblocks, rpc.Client)
                self.assertEqual(nearblocks.last_source,
                                 {"provider": "nearblocks", "request_sha256": None, "fetched_at": None})
                with self.assertRaises(rpc.RateLimited):
                    nearblocks.get("/v1/account/young.near/keys")
        request = {"kind": "near", "token_id": None, "receiver_id": "young.near", "amount": "1", "memo": None}
        account = {"code_hash": rpc.EMPTY_CODE_HASH}
        checks, _, _ = rules._tier2(request, {"allowlist": [], "caps": []}, labels.load(), account,
                                    {"provider": "stub", "request_sha256": None, "fetched_at": None}, None,
                                    self.judged_nearblocks(None), 1_800_000_000_000, rules.MIN_AGE_DAYS, None)
        ages = [check for check in checks if check["id"] == "account_age"]
        self.assertEqual([check["status"] for check in ages], ["unknown"])
        self.assertEqual(ages[0]["source"]["provider"], "nearblocks")
        enabled = self.judged_nearblocks("1")
        self.assertIsInstance(enabled, rpc.Client)
        self.assertEqual(enabled.url, rpc.NEARBLOCKS_URL)


class ProviderBudgetTest(LiveLimitCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / budget.DATABASE_NAME
        key = self.root / "fastnear.key"
        key.write_text("unit-only-token\n", encoding="utf-8")
        environment = mock.patch.dict(os.environ, {budget.DATABASE_ENV: str(self.database),
                                                   "FASTNEAR_API_KEY_FILE": str(key)})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("NEAR_MAINNET_RPC_URL", "NEAR_ARCHIVAL_RPC_URL", "SIYAB_VERDICT_NEARBLOCKS"):
            os.environ.pop(name, None)
        stderr = mock.patch("sys.stderr", new_callable=io.StringIO)
        stderr.start()
        self.addCleanup(stderr.stop)

    def spend(self, counter, span, count):
        now = time.time()
        budget.reserve("other", "operator", keyed=False, now=now)
        with closing(sqlite3.connect(self.database)) as connection:
            for window in (int(now // span), int(now // span) + 1):
                connection.execute("INSERT INTO counters VALUES(?,?,?,NULL) ON CONFLICT(counter,window) DO UPDATE "
                                   "SET count=excluded.count", (counter, window, count))
            connection.commit()

    def counted(self, counter):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute("SELECT COALESCE(SUM(count),0) FROM counters WHERE counter=? AND window=?",
                                      (counter, int(time.time() // budget.DAY_S))).fetchone()[0]

    def test_live_reads_are_metered_as_anonymous_and_backfill_as_its_own_class(self):
        seen = []

        def capture(path, query):
            seen.append(rpc._CALLER_CLASS.get())
            return 200, {}

        with mock.patch.object(self.service, "_route", side_effect=capture):
            self.request("/live/accounts/wrap.near/profile")
            self.request("/live/accounts/wrap.near/backfill?kind=transfers")
        self.assertEqual(seen, ["anonymous", "backfill"])
        self.assertIsNone(rpc._CALLER_CLASS.get())

    def test_a_spent_budget_answers_503_with_retry_after_and_is_never_cached(self):
        self.spend("pooled", budget.DAY_S, budget.POOLED_DAILY)
        service = live.LiveAPI(labels.load())
        status, body, headers = service.handle("/live/accounts/wrap.near/profile", "203.0.113.1", {})
        self.assertEqual((status, body["error"]), (503, "provider_budget"))
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)
        self.assertEqual(service._cache, {})
        self.assertEqual(self.counted("pooled"), budget.POOLED_DAILY)

    def test_public_budget_responses_hide_paths_counters_and_limits(self):
        private_path = "/private/service/provider-state.sqlite3"
        for failure in (OSError(private_path), None):
            if failure is None:
                for provider in ("fastnear_rpc", "fastnear_archival"):
                    self.spend(f"keyless:*:{provider}", budget.DAY_S, budget.KEYLESS_LIMITS[provider][0])
            for path in ("/live/verdict?receiver=alice.near&amount=1&token=near", "/live/scan/alice.near"):
                with self.subTest(failure=failure, path=path):
                    service = live.LiveAPI(labels.load())
                    if failure is not None:
                        with mock.patch("intel.budget._transaction", side_effect=failure):
                            status, body, _ = service.handle(path, "203.0.113.1", {})
                    else:
                        status, body, _ = service.handle(path, "203.0.113.1", {})
                    self.assertEqual(status, 200)
                    public = json.dumps(body)
                    for hidden in (private_path, "keyless:", "fastnear_rpc", "fastnear_archival", "20000", "3000"):
                        self.assertNotIn(hidden, public)
                    if "/verdict" in path:
                        self.assertEqual(body["outcome"], "ASK")
                    else:
                        self.assertIsNone(body["access_keys"])
                    self.assertEqual(service._cache, {})

    def test_a_refused_verdict_read_is_unknown_never_allow_and_is_not_cached(self):
        for provider in ("fastnear_rpc", "fastnear_archival"):
            self.spend(f"keyless:*:{provider}", budget.DAY_S, budget.KEYLESS_LIMITS[provider][0])
        service = live.LiveAPI(labels.load())
        status, body, _ = service.handle("/live/verdict?receiver=alice.near&amount=1&token=near", "203.0.113.1", {})
        self.assertEqual(status, 200)
        self.assertEqual((body["outcome"], body["action"]), ("ASK", "ask"))
        self.assertNotIn("pass", {check["status"] for check in body["checks"] if check["name"] == "account_age"})
        self.assertEqual(service._cache, {})


class ChecksLimitTest(LiveLimitCase):
    def test_checks_have_cost_weights_and_normalised_distinct_cache_keys(self):
        with mock.patch.dict(os.environ, {"SIYAB_CHECKS": "1"}), \
                mock.patch.object(self.service, "_route", side_effect=account_answer) as route:
            for kind, cost in (("account", 10), ("token", 6)):
                self.assertEqual(live.LiveAPI._route_name(f"/live/{kind}-check/wrap.near"), f"{kind}-check")
                self.assertEqual(live.LiveAPI._cost(f"{kind}-check", ""), cost)
                first = self.request(f"/live/{kind}-check/wrap.near")
                self.assertEqual(self.request(f"/live/{kind}-check/wrap%2Enear"), first)
            self.request("/live/token-check/other.near")
        self.assertEqual(route.call_count, 3)
        self.assertEqual(self.service._rates["203.0.113.1"][2], 24)


if __name__ == "__main__":
    unittest.main()
