import hashlib
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import api, live
from intel.workspace import SQLiteWorkspaceStore
from intel.workspace_api import WorkspaceAPI


ORIGIN = "https://dapp.example"
WORKSPACE_ORIGIN = "https://siyab.example"
PUBLIC_PATHS = (
    "/live/verdict?receiver=aurora&amount=1&token=near",
    "/live/account-check/aurora", "/live/token-check/wrap.near", "/live/scan/aurora",
    "/live/intents", "/live/reports", "/live/reports/recent", "/live/health",
    "/live/entity-directory", "/live/ask",
    "/live/accounts/aurora/identity", "/live/accounts/aurora/history?kind=transfers",
    "/live/accounts/aurora/enrichment", "/live/accounts/aurora/portfolio-history",
    "/live/account-data/" + "0" * 64 + ".json",
    "/live/portfolio-data/" + "0" * 64 + ".json",
)
EXCLUDED_PATHS = (
    "/workspace-api", "/workspace-api/session", "/workspace-api/auth/login",
    "/workspace-api/teams/team/ask", "/workspace-api/teams/team/events/" + "0" * 64,
    "/live/accounts/aurora/profile", "/live/accounts/aurora/holdings",
    "/live/accounts/aurora/transactions", "/live/accounts/aurora/transfers",
    "/live/accounts/aurora/graph", "/live/accounts/aurora/backfill",
    "/live/accounts/aurora/portfolio", "/live/accounts/aurora/portfolio-at-block",
    "/live/account-events/" + "0" * 64 + ".json",
    "/live/unknown", "/live/verdict/extra", "/live/account-check/aurora/extra",
    "/live/accounts/aurora/history/extra", "/health", "/index.html",
)


class CorsHTTPTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace_store = SQLiteWorkspaceStore(Path(self.directory.name) / "workspace.sqlite3")
        self.workspace = WorkspaceAPI(self.workspace_store, origin=WORKSPACE_ORIGIN)
        self.live = live.LiveAPI(mock.Mock(assertions=[], sha256="test-store"), clock=lambda: 0, wall_clock=lambda: 1000)
        self.server = api.make_server("127.0.0.1", 0, store=self.live.store,
                                      live_routes=self.live, workspace_routes=self.workspace)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        environment = mock.patch.dict(os.environ, {"SIYAB_CHECKS": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        for target in ("urllib.request.urlopen", "gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            network = mock.patch(target, side_effect=AssertionError("unexpected network read"))
            network.start()
            self.addCleanup(network.stop)

    def stop(self):
        self.server.shutdown()
        self.thread.join()

    def request(self, method, path, headers=None, body=None, *, origin=ORIGIN):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            request_headers = {} if origin is None else {"Origin": origin}
            connection.request(method, path, body, {**request_headers, **(headers or {})})
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def assert_public_headers(self, headers):
        self.assertEqual(headers.get_all("Access-Control-Allow-Origin"), ["*"])
        self.assertEqual(headers.get_all("Access-Control-Expose-Headers"), ["Retry-After"])
        self.assertIsNone(headers.get("Access-Control-Allow-Credentials"))
        self.assertIsNone(headers.get("Access-Control-Allow-Headers"))

    def assert_no_cors(self, headers):
        self.assertFalse([name for name in headers if name.lower().startswith("access-control-")])

    def test_preflight_allows_only_public_read_paths_without_running_them(self):
        with mock.patch.object(self.live, "handle", side_effect=AssertionError("preflight ran a live read")), \
                mock.patch.object(self.workspace, "handle", side_effect=AssertionError("preflight ran a workspace request")):
            for path in PUBLIC_PATHS:
                for method in ("GET", "HEAD", "OPTIONS"):
                    with self.subTest(path=path, method=method):
                        status, headers, body = self.request("OPTIONS", path, {"Access-Control-Request-Method": method})
                        self.assertEqual((status, body), (204, b""))
                        self.assertEqual(headers.get_all("Access-Control-Allow-Origin"), ["*"])
                        self.assertEqual(headers["Access-Control-Allow-Methods"], "GET, HEAD, OPTIONS")
                        self.assertEqual(headers["Access-Control-Max-Age"], "600")
                        self.assertEqual(headers["Cache-Control"], "no-store")
                        self.assertIsNone(headers.get("Content-Type"))
                        self.assertIsNone(headers.get("Content-Length"))
                        self.assertIsNone(headers.get("Access-Control-Allow-Headers"))
                        self.assertIsNone(headers.get("Access-Control-Allow-Credentials"))

    def test_get_and_head_allow_every_public_path(self):
        answer = {"status": "ok"}
        data = (json.dumps(answer) + "\n").encode()
        with mock.patch.object(self.live, "handle", return_value=(200, answer, {})):
            for path in PUBLIC_PATHS:
                for method in ("GET", "HEAD"):
                    with self.subTest(path=path, method=method):
                        status, headers, body = self.request(method, path)
                        self.assertEqual((status, body), (200, data if method == "GET" else b""))
                        self.assertEqual(int(headers["Content-Length"]), len(data))
                        self.assertEqual(headers["Cache-Control"], "no-store")
                        self.assert_public_headers(headers)

    def test_get_and_head_keep_the_health_body_and_cache_headers(self):
        expected = {"ok": True, "network": "mainnet", "rate_per_minute": 60, "checks": False}
        status, headers, body = self.request("GET", "/live/health")
        self.assertEqual((status, json.loads(body)), (200, expected))
        self.assert_public_headers(headers)
        self.assertEqual(headers.get_all("Cache-Control"), ["no-store"])
        self.assertEqual(headers.get_all("Content-Type"), [api.JSON_TYPE])
        status, headers, head = self.request("HEAD", "/live/health")
        self.assertEqual((status, head), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assert_public_headers(headers)

    def test_headers_are_constant_without_an_origin(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", "/live/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            self.assert_public_headers(response.headers)
            self.assertIsNone(response.headers.get("Vary"))
        finally:
            connection.close()

    def test_enabled_account_and_token_checks_are_unsigned_public_reads(self):
        answer = {"outcome": "ASK", "action": "ask", "verdict": None, "signing": "never"}
        with mock.patch.dict(os.environ, {"SIYAB_CHECKS": "1"}), \
                mock.patch("intel.live.rules.issuers_for", return_value=[]), \
                mock.patch("gate.account_check.check_account", return_value=answer), \
                mock.patch("gate.token_check.check_token", return_value=answer):
            for path in ("/live/account-check/aurora", "/live/token-check/wrap.near"):
                with self.subTest(path=path):
                    status, headers, body = self.request("GET", path)
                    self.assertEqual((status, json.loads(body)), (200, answer))
                    self.assert_public_headers(headers)

    def test_validation_errors_remain_readable_with_the_existing_shape(self):
        status, headers, body = self.request("GET", "/live/verdict?receiver=Bad&amount=1&token=near")
        self.assertEqual(status, 400)
        self.assertEqual(set(json.loads(body)), {"error", "message"})
        self.assertEqual(json.loads(body)["error"], "invalid_input")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assert_public_headers(headers)

    def test_head_errors_keep_status_headers_and_get_length_without_a_body(self):
        path = "/live/verdict?receiver=Bad&amount=1&token=near"
        status, headers, body = self.request("GET", path)
        self.assertEqual((status, json.loads(body)["error"]), (400, "invalid_input"))
        status, head_headers, head_body = self.request("HEAD", path)
        self.assertEqual((status, head_body), (400, b""))
        self.assertEqual(int(head_headers["Content-Length"]), len(body))
        self.assertEqual(head_headers["Content-Type"], headers["Content-Type"])
        self.assertEqual(head_headers["Cache-Control"], headers["Cache-Control"])
        self.assert_public_headers(head_headers)

    def test_disabled_checks_keep_their_404_and_cors_headers(self):
        status, headers, body = self.request("GET", "/live/account-check/aurora")
        self.assertEqual((status, json.loads(body)), (404, {
            "error": "not_found", "message": "No live route matches this path."}))
        self.assert_public_headers(headers)

    def test_read_errors_keep_their_503_and_cors_headers(self):
        with mock.patch("intel.live.rpc.Client.call_function", side_effect=rpc.TransportError("offline")):
            status, headers, body = self.request("GET", "/live/reports?subject=vendor.testnet")
        self.assertEqual((status, json.loads(body)["error"]), (503, "read_failed"))
        self.assertEqual(set(json.loads(body)), {"error", "message"})
        self.assert_public_headers(headers)

    def test_preflight_does_not_spend_a_read_or_bypass_the_read_rate_limit(self):
        self.live.rate_per_minute = 1
        for _ in range(2):
            self.assertEqual(self.request("OPTIONS", "/live/health", {"Access-Control-Request-Method": "GET"})[0], 204)
        self.assertEqual(self.request("GET", "/live/health")[0], 200)
        status, headers, body = self.request("GET", "/live/health")
        self.assertEqual((status, json.loads(body)["error"]), (429, "rate_limited"))
        self.assertEqual(headers["Retry-After"], "60")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assert_public_headers(headers)

    def test_public_snapshot_bytes_keep_their_immutable_cache_header(self):
        data = b'{"records":[]}\n'
        identifier = hashlib.sha256(data).hexdigest()
        self.live.history_store = mock.Mock(**{"get_graph_bytes.return_value": data})
        self.live.portfolio_history = mock.Mock(**{"get_bytes.return_value": data})
        for kind in ("account-data", "portfolio-data"):
            with self.subTest(kind=kind):
                status, headers, body = self.request("GET", f"/live/{kind}/{identifier}.json")
                self.assertEqual((status, body), (200, data))
                self.assertEqual(headers.get_all("Cache-Control"), ["public, max-age=31536000, immutable"])
                self.assert_public_headers(headers)

    def test_excluded_get_routes_never_receive_cors_even_on_success(self):
        with mock.patch.object(self.live, "handle", return_value=(200, {"status": "ok"}, {})):
            for path in EXCLUDED_PATHS:
                with self.subTest(path=path):
                    _, headers, _ = self.request("GET", path)
                    self.assert_no_cors(headers)

    def test_path_variants_keep_routing_validation_and_cors_in_agreement(self):
        cases = (
            ("/live/health/", 404, False),
            ("//live/health", 200, True),
            ("/live/accounts/aurora%2Fprofile/history", 400, True),
            ("/live/account-data/" + "A" * 64 + ".json", 400, False),
            ("/live/portfolio-data/" + "A" * 64 + ".json", 400, False),
            ("http://x/live/health", 200, True),
        )
        for path, expected_status, public in cases:
            for method in ("GET", "HEAD", "OPTIONS"):
                with self.subTest(path=path, method=method):
                    status, headers, body = self.request(method, path, {
                        "Access-Control-Request-Method": "GET"} if method == "OPTIONS" else None)
                    self.assertEqual(status, (204 if public else 403) if method == "OPTIONS" else expected_status)
                    if public:
                        self.assertEqual(headers.get_all("Access-Control-Allow-Origin"), ["*"])
                    else:
                        self.assert_no_cors(headers)
                    if method in ("HEAD", "OPTIONS") and status in (200, 204, 400, 404):
                        self.assertEqual(body, b"")
                    if method == "GET" and status == 400:
                        self.assertEqual(json.loads(body)["error"], "invalid_input")

    def test_authenticated_workspace_reads_and_posts_keep_cookie_isolation(self):
        status, headers, body = self.request("POST", "/workspace-api/auth/register", {
            "Origin": WORKSPACE_ORIGIN, "Content-Type": "application/json"},
            json.dumps({"username": "alice", "password": "test-password-123"}).encode())
        self.assertEqual(status, 201, body)
        self.assert_no_cors(headers)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        for method in ("GET", "HEAD"):
            status, headers, body = self.request(method, "/workspace-api/session", {"Cookie": cookie})
            self.assertEqual(status, 200)
            self.assertEqual(headers["Vary"], "Cookie")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assert_no_cors(headers)
            if method == "GET":
                self.assertTrue(json.loads(body)["authenticated"])
        status, headers, _ = self.request("POST", "/workspace-api/auth/logout", {"Cookie": cookie}, b"{}")
        self.assertEqual(status, 403)
        self.assert_no_cors(headers)

    def test_posts_to_public_read_routes_still_refuse_without_cors(self):
        for path in PUBLIC_PATHS:
            with self.subTest(path=path):
                status, headers, _ = self.request("POST", path)
                self.assertEqual(status, 501)
                self.assert_no_cors(headers)

    def test_preflight_refuses_workspace_state_changing_and_unknown_routes(self):
        with mock.patch.object(self.live, "handle", side_effect=AssertionError("preflight ran a live read")), \
                mock.patch.object(self.workspace, "handle", side_effect=AssertionError("preflight ran a workspace request")):
            for path in EXCLUDED_PATHS:
                with self.subTest(path=path):
                    status, headers, _ = self.request("OPTIONS", path, {"Access-Control-Request-Method": "GET"})
                    self.assertEqual(status, 403)
                    self.assert_no_cors(headers)

    def test_options_without_origin_refuses_without_running_a_read(self):
        with mock.patch.object(self.live, "handle", side_effect=AssertionError("preflight ran a live read")):
            for path in ("/live/health", "/workspace-api/session", "/unknown"):
                with self.subTest(path=path):
                    status, headers, body = self.request("OPTIONS", path, {
                        "Access-Control-Request-Method": "GET"}, origin=None)
                    self.assertEqual((status, json.loads(body)["error"]), (403, "cors_forbidden"))
                    self.assert_no_cors(headers)

    def test_preflight_refuses_two_separate_requested_method_headers(self):
        for methods in (("GET", "GET"), ("GET", "POST"), ("POST", "GET")):
            with self.subTest(methods=methods):
                connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
                try:
                    connection.putrequest("OPTIONS", "/live/health")
                    connection.putheader("Origin", ORIGIN)
                    for method in methods:
                        connection.putheader("Access-Control-Request-Method", method)
                    connection.endheaders()
                    response = connection.getresponse()
                    self.assertEqual((response.status, json.loads(response.read())["error"]), (403, "cors_forbidden"))
                    self.assert_no_cors(response.headers)
                finally:
                    connection.close()

    def test_preflight_allows_an_empty_requested_headers_value(self):
        for value in ("", " \t "):
            with self.subTest(value=value):
                status, headers, body = self.request("OPTIONS", "/live/health", {
                    "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": value})
                self.assertEqual((status, body), (204, b""))
                self.assertEqual(headers.get_all("Access-Control-Allow-Origin"), ["*"])
                self.assertIsNone(headers.get("Access-Control-Allow-Headers"))
                self.assertIsNone(headers.get("Content-Type"))
                self.assertIsNone(headers.get("Content-Length"))

    def test_preflight_allows_only_content_type_with_any_case_and_surrounding_whitespace(self):
        with mock.patch.object(self.live, "handle", side_effect=AssertionError("preflight ran a live read")):
            for path in PUBLIC_PATHS:
                for value in ("Content-Type", "content-type", " \tCoNtEnT-tYpE \t"):
                    for method in ("GET", "HEAD", "OPTIONS"):
                        with self.subTest(path=path, value=value, method=method):
                            status, headers, body = self.request("OPTIONS", path, {
                                "Access-Control-Request-Method": method, "Access-Control-Request-Headers": value})
                            self.assertEqual((status, body), (204, b""))
                            self.assertEqual(headers.get_all("Access-Control-Allow-Origin"), ["*"])
                            self.assertEqual(headers.get_all("Access-Control-Allow-Headers"), ["Content-Type"])
                            self.assertEqual(headers["Access-Control-Allow-Methods"], "GET, HEAD, OPTIONS")
                            self.assertIsNone(headers.get("Access-Control-Allow-Credentials"))
                            self.assertIsNone(headers.get("Content-Type"))
                            self.assertIsNone(headers.get("Content-Length"))

    def test_preflight_refuses_writes_and_unneeded_request_headers(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "TRACE", "get", "GET, POST"):
            with self.subTest(method=method):
                status, headers, _ = self.request("OPTIONS", "/live/verdict", {"Access-Control-Request-Method": method})
                self.assertEqual(status, 403)
                self.assert_no_cors(headers)
        for name in ("Authorization", "X-CSRF-Token", "X-Forwarded-For", "Content-Type, X-Other",
                     "cOnTeNt-TyPe, x-OtHeR"):
            with self.subTest(header=name):
                status, headers, _ = self.request("OPTIONS", "/live/verdict", {
                    "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": name})
                self.assertEqual(status, 403)
                self.assert_no_cors(headers)


if __name__ == "__main__":
    unittest.main()
