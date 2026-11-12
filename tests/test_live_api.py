"""Offline contract tests for the opt-in live JSON routes."""

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gate import evidence, rpc, rules
from intel import __main__ as intel_main
from intel import api, labels, live


SUMMARY = {"accepted": 1, "pending": 0, "contested": 0, "latest_accepted_id": 7,
           "latest_deny_id": None, "deny": False}
REPORT_VIEW = {"id": 7, "subject": "vendor.testnet", "kind": "theft", "outcome": "near_miss",
               "state": "accepted", "evidence_url": "https://evidence.example/report",
               "evidence_sha256": "stubbed-report-hash", "submitter": "reporter.testnet",
               "moderator": "moderator.testnet"}
REPORT_ROW = {"id": 7, "subject": "vendor.testnet", "kind": "theft", "outcome": "near_miss",
              "status": "accepted", "evidence_url": "https://evidence.example/report",
              "evidence_sha256": "stubbed-report-hash", "submitter": "reporter.testnet",
              "moderator": "moderator.testnet"}
INTENTS_ASSET = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
RECIPIENT = "0x0000000000000000000000000000000000000001"


class LiveRouteTest(unittest.TestCase):
    def setUp(self):
        self.store = object()
        self.now = [0.0]
        self.perf = [10.0]

        def perf_clock():
            value = self.perf[0]
            self.perf[0] += 0.012
            return value

        self.live = live.LiveAPI(self.store, clock=lambda: self.now[0], wall_clock=lambda: 1000.0,
                                 perf_clock=perf_clock)
        self.server = api.make_server("127.0.0.1", 0, store=self.store, live_routes=self.live)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.network = mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network read"))
        self.network.start()
        for target in ("gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            network = mock.patch(target, side_effect=AssertionError("unexpected network read"))
            network.start()
            self.addCleanup(network.stop)

    def tearDown(self):
        self.network.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    @staticmethod
    def allow_judgement():
        checks = [{"id": "receiver_exists", "status": "pass", "detail": "alice.near exists",
                   "source": {"provider": "stub", "request_sha256": None, "fetched_at": "2026-11-08T00:00:00Z"}}]
        return rules.Judgement("ALLOW", checks, ["Every check ran and none found a signal."], [],
                               {"block_height": 123, "timestamp_ms": 456})

    def test_contract_shape_field_names_and_routes(self):
        judgement = self.allow_judgement()
        subject = {"network": "mainnet", "kind": "near", "token": None, "receiver": "alice.near",
                   "amount": "1", "memo_sha256": None}
        document = evidence.document(subject=subject, checks=judgement.checks, labels=[], verdict="ALLOW",
                                     reasons=judgement.reasons, observed=judgement.observed, issued_ms=None,
                                     expires_ms=None, verdict_id=None, latency_ms=12)
        verdict_answer = {"outcome": "ALLOW", "action": "sign", "reasons": judgement.reasons,
                          "checks": [{"name": "receiver_exists", "status": "pass", "detail": "alice.near exists"}],
                          "evidence_hash": evidence.evidence_hash(document), "freshness": judgement.observed,
                          "verdict": None, "signing": "never", "latency_ms": 12}
        with mock.patch("intel.live.rules.judge", return_value=judgement) as judge, \
                mock.patch("intel.live.rules.issuers_for", return_value=[]) as issuers, \
                mock.patch("gate.verdict.request", side_effect=AssertionError("signing path used")):
            status, _, body = self.request("/live/verdict?receiver=alice.near&amount=1&token=near")
        self.assertEqual((status, body), (200, verdict_answer))
        self.assertEqual(judge.call_args.args[0], {"kind": "near", "token_id": None, "receiver_id": "alice.near",
                                                   "amount": "1", "memo": None})
        issuers.assert_called_once_with("mainnet")

        scan_answer = {"access_keys": 1, "full_access_keys": [], "empty_method_names": [], "dead_keys": [],
                       "unlimited_allowance": [],
                       "findings": ["The scan read 1 access key entries.", "The scan found 0 full access keys.",
                                    "The scan found 0 keys with empty method names.",
                                    "The scan found 0 keys with zero allowance.",
                                    "The scan found 0 keys with no allowance limit."], "read_at_block": 99}
        scan_result = {"findings": {"keys": {"count": 1, "full_access": [], "empty_method_names": [],
                                               "dead": [], "unlimited": []},
                                    "block_height": 99, "verifier": {"current": "stub"}}}
        with mock.patch("intel.live.scan.scan", return_value=scan_result) as scanned:
            status, _, body = self.request("/live/scan/agent.near")
        self.assertEqual((status, body), (200, scan_answer))
        scanned.assert_called_once()

        intent_check = {"id": "ofac_sanctioned", "status": "pass", "detail": "no match",
                        "source": {"provider": "stub", "request_sha256": None, "fetched_at": None}}
        intent_result = {"verdict": "ALLOW", "checks": [intent_check],
                         "reasons": ["Every check ran and none found a signal."],
                         "evidence_hash": "stubbed-evidence-hash", "document": {"observed": {"timestamp_ms": 789}}}
        intent_answer = {"outcome": "ALLOW", "action": "sign", "reasons": intent_result["reasons"],
                         "checks": [{"name": "ofac_sanctioned", "status": "pass", "detail": "no match"}],
                         "evidence_hash": "stubbed-evidence-hash", "freshness": {"timestamp_ms": 789},
                         "verdict": None, "signing": "never", "latency_ms": 12}
        with mock.patch("intel.live.intents.judge_quote", return_value=intent_result) as judged:
            status, _, body = self.request(f"/live/intents?destination_asset={INTENTS_ASSET}&recipient={RECIPIENT}&amount=5")
        self.assertEqual((status, body), (200, intent_answer))
        args, kwargs = judged.call_args
        self.assertEqual(args[:4], (live.INTENTS_ORIGIN_ASSET, INTENTS_ASSET, "5", RECIPIENT))
        self.assertEqual(kwargs["mode"], "off")
        self.assertEqual(kwargs["firewall_url"], None)

        def reports_view(_client, contract, method, args, *, block_id=None):
            self.assertEqual(contract, live.REPORTS_ACCOUNT)
            if method == "subject_summary":
                return SUMMARY
            if method == "reports_by_subject":
                self.assertEqual(args, {"subject": "vendor.testnet", "from": 0, "limit": 50})
                return [{"id": 7}]
            if method == "get_report":
                self.assertEqual(args, {"id": 7})
                return REPORT_VIEW
            raise AssertionError(f"unexpected view {method}")

        with mock.patch("intel.live.rpc.Client.call_function", autospec=True, side_effect=reports_view):
            status, _, body = self.request("/live/reports?subject=vendor.testnet")
        self.assertEqual((status, body), (200, {"subject_summary": SUMMARY, "reports": [REPORT_ROW], "truncated": False}))

        def recent_view(_client, contract, method, args, *, block_id=None):
            self.assertEqual(contract, live.REPORTS_ACCOUNT)
            if method == "totals":
                return {"reports": 2, "disputes": 1}
            if method == "get_report":
                return {**REPORT_VIEW, "id": args["id"], "subject": f"vendor-{args['id']}.testnet"}
            raise AssertionError(f"unexpected view {method}")

        with mock.patch("intel.live.rpc.Client.call_function", autospec=True, side_effect=recent_view):
            status, _, body = self.request("/live/reports/recent")
        recent = [{**REPORT_ROW, "id": index, "subject": f"vendor-{index}.testnet"} for index in (1, 0)]
        self.assertEqual((status, body), (200, {"reports": recent}))
        self.assertEqual(self.request("/live/health")[::2],
                         (200, {"ok": True, "network": "mainnet", "rate_per_minute": 60}))

    def test_validation_refusals_and_testnet_receiver(self):
        bad_paths = (
            "/live/verdict?receiver=Bad&amount=1&token=near",
            "/live/verdict?receiver=alice.near&amount=0&token=near",
            "/live/verdict?receiver=alice.near&amount=01&token=near",
            "/live/verdict?receiver=alice.near&amount=340282366920938463463374607431768211456&token=near",
            "/live/verdict?receiver=alice.near&amount=1&token=bad!",
            "/live/verdict?receiver=alice.near&amount=1&token=alice.testnet",
            "/live/verdict?receiver=alice.testnet&amount=1&token=near",
            "/live/verdict?receiver=alice.near&amount=1&token=near&extra=x",
            "/live/scan/bad!",
            "/live/scan/alice.testnet",
            "/live/intents?destination_asset=wrong&recipient=" + RECIPIENT + "&amount=1",
            "/live/intents?destination_asset=nep141:x&recipient=" + RECIPIENT + "&amount=1",
            "/live/intents?destination_asset=nep141:wrap.near&recipient=not-an-address&amount=1",
            "/live/intents?destination_asset=nep141:wrap.near&recipient=" + RECIPIENT + "&amount=01",
            "/live/reports?subject=bad!",
            "/live/health?extra=x",
        )
        with mock.patch("intel.live.rules.judge", side_effect=AssertionError("read before validation")), \
                mock.patch("intel.live.scan.scan", side_effect=AssertionError("read before validation")), \
                mock.patch("intel.live.intents.judge_quote", side_effect=AssertionError("read before validation")), \
                mock.patch("intel.live.rpc.Client.call_function", side_effect=AssertionError("read before validation")):
            for path in bad_paths:
                with self.subTest(path=path):
                    status, _, body = self.request(path)
                    self.assertEqual(status, 400)
                    self.assertEqual(set(body), {"error", "message"})

    def test_unreadable_checks_answer_ask_never_allow(self):
        failed = rules.Judgement("UNKNOWN", [{"id": "receiver_exists", "status": "unknown",
                                              "detail": "could not read account",
                                              "source": {"provider": "stub", "request_sha256": None,
                                                         "fetched_at": None}}],
                                 ["receiver_exists could not run: could not read account."], [],
                                 {"block_height": None, "timestamp_ms": 456})
        with mock.patch("intel.live.rules.judge", return_value=failed), \
                mock.patch("intel.live.rules.issuers_for", return_value=[]):
            status, _, body = self.request("/live/verdict?receiver=alice.near&amount=1&token=near")
        self.assertEqual((status, body["outcome"], body["action"], body["signing"]),
                         (200, "ASK", "ask", "never"))
        self.assertEqual(body["checks"][0]["status"], "unknown")

        intent_result = {"verdict": "UNKNOWN", "checks": [failed.checks[0]], "reasons": failed.reasons,
                         "evidence_hash": "stubbed-unknown-hash", "document": {"observed": {"timestamp_ms": 456}}}
        with mock.patch("intel.live.intents.judge_quote", return_value=intent_result):
            status, _, body = self.request(f"/live/intents?destination_asset={INTENTS_ASSET}&recipient={RECIPIENT}&amount=1")
        self.assertEqual((status, body["outcome"], body["action"]), (200, "ASK", "ask"))

        with mock.patch("intel.live.rpc.Client.call_function", side_effect=rpc.TransportError("offline")):
            status, _, body = self.request("/live/reports?subject=vendor.testnet")
        self.assertEqual((status, body["error"]), (503, "read_failed"))

    def test_cache_rate_slots_and_forwarded_ip_rules(self):
        judgement = self.allow_judgement()
        with mock.patch("intel.live.rules.judge", return_value=judgement) as judge, \
                mock.patch("intel.live.rules.issuers_for", return_value=[]):
            path = "/live/verdict?receiver=alice.near&amount=1&token=near"
            first = self.request(path)
            second = self.request(path)
            self.assertEqual(first[::2], second[::2])
            judge.assert_called_once()
            self.now[0] = 61
            self.request(path)
            self.assertEqual(judge.call_count, 2)

        self.live.rate_per_minute = 1
        self.now[0] = 120
        self.assertEqual(self.request("/live/health")[0], 200)
        status, headers, body = self.request("/live/health")
        self.assertEqual((status, body["error"]), (429, "rate_limited"))
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)

        self.assertEqual(live.LiveAPI._client_ip("127.0.0.1", {"X-Forwarded-For": "203.0.113.8, 10.0.0.2"}),
                         "10.0.0.2")
        self.assertEqual(live.LiveAPI._client_ip("203.0.113.9", {"X-Forwarded-For": "203.0.113.8"}),
                         "203.0.113.9")

        self.live.rate_per_minute = 60
        self.now[0] = 300
        self.live.queue_wait_s = 0.05
        acquired = []
        try:
            for _ in range(live.MAX_IN_FLIGHT):
                acquired.append(self.live._slots.acquire(blocking=False))
            status, _, body = self.request("/live/health")
            self.assertEqual((status, body["error"]), (503, "busy"))
        finally:
            for held in acquired:
                if held:
                    self.live._slots.release()

    def test_timeout_is_504_and_below_30_seconds(self):
        self.assertLess(self.live.request_timeout_s, 30)
        handle = self.live.handle

        def route_budget(*args):
            with mock.patch.object(self.live, "request_timeout_s", 0.02):
                return handle(*args)

        with mock.patch.object(self.live, "handle", side_effect=route_budget), \
                mock.patch("intel.live.rules.judge", side_effect=lambda *args, **kwargs: (time.sleep(0.04),
                                                                                       self.allow_judgement())[1]), \
                mock.patch("intel.live.rules.issuers_for", return_value=[]):
            status, _, body = self.request("/live/verdict?receiver=alice.near&amount=1&token=near")
        self.assertEqual((status, body["error"]), (504, "timeout"))

    def test_oversized_live_response_returns_a_small_503(self):
        with mock.patch("intel.live.MAX_RESPONSE_BYTES", 128), \
                mock.patch.object(self.live, "_route", return_value=(200, {"payload": "x" * 129})):
            status, _, body = self.request("/live/oversized")
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": "response_too_large", "message": "The live response exceeds the size limit."})
        self.assertEqual(self.live._cache, {})

    def test_cache_byte_cap_evicts_oldest_responses(self):
        with mock.patch("intel.live.MAX_RESPONSE_BYTES", 1000), \
                mock.patch("intel.live.MAX_CACHE_BYTES", 300), \
                mock.patch.object(self.live, "_route", return_value=(200, {"payload": "x" * 160})) as route:
            self.request("/live/first")
            self.request("/live/second")
            self.assertLessEqual(self.live._cache_bytes, live.MAX_CACHE_BYTES)
            self.request("/live/first")
        self.assertEqual(route.call_count, 3)

    def test_slow_request_line_and_headers_share_an_absolute_deadline(self):
        self.live.request_timeout_s = 0.2
        for prefix, byte in ((b"G", b"E"), (b"GET /live/health HTTP/1.1\r\nHost: ", b"o")):
            with self.subTest(prefix=prefix):
                connection = socket.create_connection(self.server.server_address, timeout=3)
                self.addCleanup(connection.close)
                connection.sendall(prefix)
                started = time.monotonic()
                while time.monotonic() - started < 0.3:
                    try:
                        connection.sendall(byte)
                    except OSError:
                        break
                    time.sleep(0.03)
                connection.settimeout(0.12)
                try:
                    closed = connection.recv(1) == b""
                except (ConnectionAbortedError, ConnectionResetError):
                    closed = True
                self.assertTrue(closed)

    def test_connection_cap_rejects_before_request_parsing(self):
        limited = live.LiveAPI(self.store, max_in_flight=1)
        server = api.make_server("127.0.0.1", 0, store=self.store, live_routes=limited)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connections = []
        try:
            for _ in range(4):
                connections.append(socket.create_connection(server.server_address, timeout=3))
                time.sleep(0.05)
            excess = socket.create_connection(server.server_address, timeout=3)
            connections.append(excess)
            excess.settimeout(1)
            self.assertIn(b" 503 ", excess.recv(256))
        finally:
            for connection in connections:
                connection.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_live_capacity_does_not_block_static_or_read_only_routes(self):
        limited = live.LiveAPI(self.store, max_in_flight=2)
        started = threading.Barrier(3)
        release = threading.Event()

        def slow_route(_path, _query):
            started.wait(timeout=2)
            release.wait(timeout=2)
            return 200, {"ok": True}

        def request(server, path):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()

        with tempfile.TemporaryDirectory() as site_directory:
            Path(site_directory, "index.html").write_text("site", encoding="utf-8")
            store = labels.load()
            server = api.make_server("127.0.0.1", 0, site=site_directory,
                                     store=store, live_routes=limited)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            answers = []
            workers = []
            try:
                with mock.patch.object(limited, "_route", side_effect=slow_route):
                    workers = [threading.Thread(target=lambda path=path: answers.append(request(server, path)))
                               for path in ("/live/slow-1", "/live/slow-2")]
                    for worker in workers:
                        worker.start()
                    started.wait(timeout=2)
                    self.assertEqual(request(server, "/")[0], 200)
                    health_status, health_body = request(server, "/health")
                    self.assertEqual(health_status, 200)
                    self.assertEqual(json.loads(health_body)["status"], "ok")
                    release.set()
                    for worker in workers:
                        worker.join(timeout=2)
                    self.assertEqual(answers, [(200, b'{"ok": true}\n')] * 2)
            finally:
                release.set()
                for worker in workers:
                    worker.join(timeout=2)
                server.shutdown()
                server.server_close()
                thread.join()

    def test_live_route_is_404_without_opt_in_and_cli_flag_enables_it(self):
        server = api.make_server("127.0.0.1", 0, store=self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            connection.request("GET", "/live/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        with mock.patch("intel.__main__.api.serve", return_value=0) as serve:
            self.assertEqual(intel_main.main(["serve", "--live", "--port", "0"]), 0)
        self.assertTrue(serve.call_args.kwargs["live"])


if __name__ == "__main__":
    unittest.main()
