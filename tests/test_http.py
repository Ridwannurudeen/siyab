import ast
import contextlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

from gate import __main__ as cli
from gate import evidence, signer, verdict
from gate import http as endpoint
from intel import labels
from tests.test_verdict import no_network

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "gate" / "http.py"
EVIDENCE_DIR = ROOT / "fixtures" / "evidence"
POLICY = {"allowlist": ["intents.near", "no-such-account-example.near"], "caps": [{"asset": {"Ft": "wrap.near"}}]}
ALLOW = {"kind": "ft", "token": "wrap.near", "receiver": "intents.near", "amount": "1000", "memo": "invoice 7"}
DENY = {"kind": "ft", "token": "wrap.near", "receiver": "no-such-account-example.near", "amount": "1000"}
UNKNOWN = {"kind": "near", "receiver": "relay.aurora", "amount": "5"}
KEYS = "# test keys\n\nkey-one\n  key-two  \n"


def replay_environ(keys_file, **fields):
    environ = {"SHIELDBOT_API_KEYS_FILE": str(keys_file), "GATE_FIXTURES_MODE": "replay",
               "GATE_ACCOUNT_ID": "gate.near", "GATE_POLICY": json.dumps(POLICY)}
    environ.update(fields)
    return environ


class EndpointCase(unittest.TestCase):
    rate = 60

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.keys_file = tmp / "api-keys.txt"
        cls.keys_file.write_text(KEYS, encoding="utf-8")
        cls.evidence_dir = tmp / "evidence"
        cls.evidence_dir.mkdir()
        cls.network = mock.patch("urllib.request.urlopen", side_effect=no_network)
        cls.network.start()
        cls.now = [0.0]
        environ = replay_environ(cls.keys_file, SHIELDBOT_API_RATE=str(cls.rate))
        with mock.patch.dict(os.environ, environ):
            config = endpoint.settings(environ)
            cls.server = endpoint.make_server("127.0.0.1", 0, config, cls.evidence_dir, store=labels.load(),
                                              clock=lambda: cls.now[0])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.network.stop()
        cls.tmp.cleanup()

    def connection(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        self.addCleanup(connection.close)
        return connection

    def answer(self, connection):
        reply = connection.getresponse()
        body = reply.read()
        self.assertEqual(reply.getheader("Content-Type"), endpoint.JSON_TYPE)
        self.assertEqual(reply.getheader("Cache-Control"), "no-store")
        return reply.status, dict(reply.getheaders()), json.loads(body)

    def raw_call(self, method, path, headers=None):
        connection = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        request_headers = {"Host": "localhost", "Connection": "close"}
        request_headers.update(headers or {})
        request = f"{method} {path} HTTP/1.0\r\n" + "".join(
            f"{name}: {value}\r\n" for name, value in request_headers.items()) + "\r\n"
        connection.sendall(request.encode("ascii"))
        response = bytearray()
        while chunk := connection.recv(4096):
            response.extend(chunk)
        head, body = bytes(response).split(b"\r\n\r\n", 1)
        status_line, *header_lines = head.decode("iso-8859-1").split("\r\n")
        status = int(status_line.split()[1])
        response_headers = {name.lower(): value.strip() for name, value in
                            (line.split(":", 1) for line in header_lines)}
        return status, response_headers, json.loads(body)

    def call(self, method, path, body=None, key="key-one"):
        """(status, headers, JSON body) of one request; a dict body is sent as JSON, bytes as they are."""
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        data = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        connection = self.connection()
        connection.request(method, path, body=data, headers=headers)
        return self.answer(connection)


class VerdictRouteTest(EndpointCase):
    def test_health_answers_the_mode_without_a_key(self):
        self.assertEqual(self.call("GET", "/health", key=None)[::2], (200, {"ok": True, "mode": "replay"}))

    def test_a_missing_or_wrong_key_is_401(self):
        for header in (None, "Bearer wrong", "Bearer key-on", "Bearer key-one ", "Basic key-one", "key-one"):
            with self.subTest(header=header):
                connection = self.connection()
                connection.request("POST", "/verdict",
                                   headers={"Authorization": header} if header else {})
                status, headers, body = self.answer(connection)
                self.assertEqual(status, 401)
                self.assertEqual(headers["WWW-Authenticate"], "Bearer")
                self.assertIn("API key", body["error"])

    def test_allow_deny_and_unknown_come_back_unsigned(self):
        before = sorted(path.name for path in EVIDENCE_DIR.iterdir())
        for body, outcome, action, key in ((ALLOW, "ALLOW", "sign", "key-one"), (DENY, "DENY", "refuse", "key-two"),
                                           (UNKNOWN, "UNKNOWN", "ask", "key-one")):
            with self.subTest(outcome=outcome):
                status, _, result = self.call("POST", "/verdict", body, key=key)
                self.assertEqual(status, 200, result)
                self.assertEqual((result["outcome"], result["action"]), (outcome, action))
                self.assertIsNone(result["verdict"])
                self.assertIsNone(result["message"])
                self.assertEqual(result["signing"], endpoint.SIGNING)
                self.assertEqual(result["request"]["receiver_id"], body["receiver"])
                for check in result["checks"]:
                    self.assertEqual(sorted(check), ["detail", "id", "source", "status"])
                    self.assertIn(check["status"], evidence.STATUSES)
                values = [result]
                while values:
                    value = values.pop()
                    if isinstance(value, dict):
                        self.assertNotIn("evidence_path", value)
                        values.extend(value.values())
                    elif isinstance(value, list):
                        values.extend(value)
                    elif isinstance(value, str):
                        self.assertFalse(PurePosixPath(value).is_absolute(), value)
                        self.assertFalse(PureWindowsPath(value).is_absolute(), value)
                document_path = self.evidence_dir / f"{result['evidence_hash']}.json"
                self.assertTrue(document_path.is_relative_to(self.evidence_dir))
                document = json.loads(document_path.read_bytes())
                self.assertEqual(document["verdict"], outcome)
                self.assertEqual([document[k] for k in ("issued_ms", "expires_ms", "verdict_id")], [None, None, None])
        self.assertEqual(sorted(path.name for path in EVIDENCE_DIR.iterdir()), before)

    def test_an_unset_reports_account_omits_the_community_report_check(self):
        status, _, result = self.call("POST", "/verdict", ALLOW)
        self.assertEqual(status, 200)
        self.assertNotIn("community_report", {check["id"] for check in result["checks"]})

    def test_a_reports_deny_summary_is_returned_by_the_verdict_route(self):
        environ = replay_environ(self.keys_file, REPORTS_ACCOUNT_ID="reports.gate.testnet")
        config = endpoint.settings(environ)
        with mock.patch.dict(os.environ, environ):
            server = endpoint.make_server("127.0.0.1", 0, config, self.evidence_dir, store=labels.load())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.server = server
        summary = {"accepted": 2, "pending": 3, "contested": 1, "latest_accepted_id": 29,
                   "latest_deny_id": 17, "deny": True}
        body = {"kind": "near", "receiver": "intents.near", "amount": "1"}
        try:
            with mock.patch("gate.rpc.Client.call_function", autospec=True, return_value=summary) as call_function:
                status, _, result = self.call("POST", "/verdict", body)
            self.assertEqual(status, 200)
            self.assertEqual(result["outcome"], "DENY")
            check = next(check for check in result["checks"] if check["id"] == "community_report")
            self.assertEqual((check["status"], check["detail"]),
                             ("signal", "reports for intents.near: deny report 17 is accepted; "
                              "1 under dispute; pending 3"))
            self.assertEqual(call_function.call_args.args[1:],
                             ("reports.gate.testnet", "subject_summary", {"subject": "intents.near"}))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_no_signer_is_built_for_an_allow(self):
        refuse = mock.Mock(side_effect=AssertionError("a signer was built"))
        with mock.patch.object(signer, "TestSigner", refuse), mock.patch.object(signer, "NaClSigner", refuse), \
                mock.patch.object(signer, "PurePythonLiveSigner", refuse):
            status, _, result = self.call("POST", "/verdict", ALLOW)
        self.assertEqual((status, result["outcome"], result["action"], result["verdict"]), (200, "ALLOW", "sign", None))
        refuse.assert_not_called()

    def test_an_unknown_path_is_404_and_a_wrong_method_is_405(self):
        self.assertEqual(self.call("GET", "/verdicts")[::2], (404, {"error": "not found"}))
        self.assertEqual(self.call("POST", "/")[0], 404)
        for method, path, allowed in (("GET", "/verdict", "POST"), ("PUT", "/verdict", "GET, POST"),
                                      ("DELETE", "/verdict", "GET, POST"), ("POST", "/health", "GET")):
            with self.subTest(method=method, path=path):
                status, headers, _ = self.call(method, path)
                self.assertEqual((status, headers["Allow"]), (405, allowed))

    def test_an_unknown_method_on_an_unknown_path_is_405_before_path_lookup(self):
        status, headers, body = self.raw_call("TRACE", "/not-found")
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, POST")
        self.assertEqual(headers["content-type"], endpoint.JSON_TYPE)
        self.assertIn("error", body)

    def test_every_method_other_than_get_and_post_is_405_with_json(self):
        for method in ("TRACE", "MADEUP", "CONNECT", "PATCH", "PUT", "DELETE", "OPTIONS", "HEAD"):
            with self.subTest(method=method):
                status, headers, body = self.raw_call(method, "/verdict")
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], "GET, POST")
                self.assertEqual(headers["content-type"], endpoint.JSON_TYPE)
                self.assertIn("error", body)

    def test_a_body_that_is_not_a_transfer_is_400(self):
        for body, words in ((b"{not json", "not JSON"), (b"[1]", "a JSON object"), (b"\xff", "not JSON"),
                            ({**ALLOW, "gate": "other.near"}, "unknown fields: gate"),
                            ({**ALLOW, "amount": "0100"}, "invalid request: amount"),
                            ({**ALLOW, "kind": 1}, "invalid request: kind"),
                            ({**ALLOW, "receiver": "Bad"}, "invalid request: receiver_id")):
            with self.subTest(body=body):
                status, _, answer = self.call("POST", "/verdict", body)
                self.assertEqual(status, 400)
                self.assertIn(words, answer["error"])

    def test_a_body_over_the_cap_is_413_before_it_is_read(self):
        connection = self.connection()
        connection.putrequest("POST", "/verdict")
        connection.putheader("Authorization", "Bearer key-one")
        connection.putheader("Content-Length", str(endpoint.MAX_BODY + 1))
        connection.endheaders()
        status, _, body = self.answer(connection)
        self.assertEqual(status, 413)
        self.assertIn(str(endpoint.MAX_BODY), body["error"])

    def test_a_missing_or_malformed_content_length_is_refused(self):
        for length, expected in ((None, 411), ("-1", 400), ("12a", 400)):
            with self.subTest(length=length):
                connection = self.connection()
                connection.putrequest("POST", "/verdict")
                connection.putheader("Authorization", "Bearer key-one")
                if length is not None:
                    connection.putheader("Content-Length", length)
                connection.endheaders()
                self.assertEqual(self.answer(connection)[0], expected)

    def test_a_5000_digit_content_length_is_400_json(self):
        status, headers, body = self.raw_call("POST", "/verdict", {
            "Authorization": "Bearer key-one", "Content-Length": "9" * 5000,
        })
        self.assertEqual(status, 400)
        self.assertEqual(headers["content-type"], endpoint.JSON_TYPE)
        self.assertIn("Content-Length", body["error"])

    def test_an_internal_failure_is_500_without_a_traceback(self):
        stderr = io.StringIO()
        with mock.patch.object(verdict, "request", side_effect=RuntimeError("boom in /opt/private/path")), \
                contextlib.redirect_stderr(stderr):
            status, _, body = self.call("POST", "/verdict", ALLOW)
        self.assertEqual((status, body), (500, {"error": "internal error"}))
        self.assertIn("RuntimeError: boom", stderr.getvalue())


class NestedJsonTest(EndpointCase):
    rate = 1000

    def test_excessive_json_nesting_is_400_and_the_next_request_succeeds(self):
        for size in (10000, 100000):
            with self.subTest(size=size), mock.patch.object(endpoint, "MAX_BODY", 100000):
                status, _, body = self.call("POST", "/verdict", b"[" * size)
                self.assertEqual(status, 400)
                self.assertIn("the body is not JSON", body["error"])
                self.assertEqual(self.call("GET", "/health")[::2], (200, {"ok": True, "mode": "replay"}))

    def test_nested_transfer_fields_are_refused_without_signing(self):
        errors = io.StringIO()
        before = sorted(self.evidence_dir.iterdir())
        with contextlib.redirect_stderr(errors):
            for depth in (990, *range(2980, 3000)):
                nested = b"[" * depth + b"0" + b"]" * depth
                for field in endpoint.FIELDS:
                    with self.subTest(depth=depth, field=field):
                        fields = {key: value for key, value in ALLOW.items() if key != field}
                        fields = json.dumps(fields).encode("utf-8")
                        body = fields[:-1] + b', "' + field.encode("ascii") + b'":' + nested + b"}"
                        status, _, reply = self.call("POST", "/verdict", body)
                        self.assertEqual(status, 400)
                        self.assertIn("error", reply)
        self.assertEqual(sorted(self.evidence_dir.iterdir()), before)
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertNotIn("verdict failed", errors.getvalue())
        self.assertEqual(self.call("GET", "/health")[0], 200)


class RateTest(EndpointCase):
    rate = 2

    def test_a_key_over_its_budget_gets_429_with_retry_after_until_the_next_window(self):
        self.now[0] = 15.5
        for _ in range(2):
            self.assertEqual(self.call("POST", "/verdict", b"[]")[0], 400)
        status, headers, body = self.call("POST", "/verdict")
        self.assertEqual((status, headers["Retry-After"]), (429, "45"))
        self.assertIn("2 requests a minute", body["error"])
        self.assertEqual(self.call("POST", "/verdict", b"[]", key="key-two")[0], 400)
        self.now[0] = 59.9
        self.assertEqual(self.call("POST", "/verdict")[1]["Retry-After"], "1")
        self.now[0] = 60.0
        self.assertEqual(self.call("POST", "/verdict", b"[]")[0], 400)

    def test_a_clock_going_backwards_does_not_reset_a_later_window(self):
        readings = iter((120.0, 60.0))
        limit = endpoint.RateLimit(1, lambda: next(readings))
        self.assertIsNone(limit.retry_after("key-one"))
        self.assertEqual(limit.retry_after("key-one"), 120)


class SocketLimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys_file = Path(self.tmp.name) / "api-keys.txt"
        self.keys_file.write_text(KEYS, encoding="utf-8")
        self.entered = threading.Event()
        environ = replay_environ(self.keys_file)
        self.config = endpoint.settings(environ)
        self.network = mock.patch("urllib.request.urlopen", side_effect=no_network)
        self.network.start()
        self.addCleanup(self.network.stop)

    def start_server(self, request_timeout, max_in_flight=8):
        def clock():
            self.entered.set()
            return 0.0

        self.server = endpoint.make_server(
            "127.0.0.1", 0, self.config, Path(self.tmp.name) / "evidence", store=labels.load(), clock=clock,
            request_timeout=request_timeout, max_in_flight=max_in_flight,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def open_incomplete_verdict(self):
        connection = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        connection.sendall(b"POST /verdict HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer key-one\r\n"
                           b"Content-Length: 1\r\nConnection: close\r\n\r\n")
        return connection

    def receive_all(self, connection):
        response = bytearray()
        while chunk := connection.recv(4096):
            response.extend(chunk)
        return bytes(response)

    def test_a_request_with_a_body_that_never_arrives_is_closed_after_the_socket_timeout(self):
        self.start_server(request_timeout=0.15)
        connection = self.open_incomplete_verdict()
        self.assertTrue(self.entered.wait(1))
        self.assertEqual(self.receive_all(connection), b"")

    def test_requests_over_the_in_flight_cap_get_503_without_reading_their_body(self):
        self.start_server(request_timeout=2, max_in_flight=1)
        first = self.open_incomplete_verdict()
        self.assertTrue(self.entered.wait(1))
        second = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(second.close)
        second.sendall(b"POST /verdict HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer key-one\r\n"
                       b"Content-Length: 1\r\nConnection: close\r\n\r\n")
        response = self.receive_all(second)
        head, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(b" 503 ", head.split(b"\r\n", 1)[0])
        headers = head.decode("iso-8859-1").lower()
        self.assertIn(f"content-type: {endpoint.JSON_TYPE}".lower(), headers)
        self.assertEqual(json.loads(body), {"error": "server busy"})

        trace = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(trace.close)
        trace.sendall(b"TRACE /not-found HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        trace_response = self.receive_all(trace)
        trace_head, trace_body = trace_response.split(b"\r\n\r\n", 1)
        self.assertIn(b" 405 ", trace_head.split(b"\r\n", 1)[0])
        trace_headers = trace_head.decode("iso-8859-1").lower()
        self.assertIn("allow: get, post", trace_headers)
        self.assertIn(f"content-type: {endpoint.JSON_TYPE}".lower(), trace_headers)
        self.assertIn("error", json.loads(trace_body))
        self.assertEqual(self.receive_all(first), b"")


class KeyComparisonTest(unittest.TestCase):
    def test_keys_are_compared_as_fixed_length_digests(self):
        lengths = []
        compare = endpoint.hmac.compare_digest

        def record(left, right):
            lengths.append((len(left), len(right)))
            return compare(left, right)

        with mock.patch.object(endpoint.hmac, "compare_digest", side_effect=record):
            self.assertEqual(endpoint._key("Bearer key-two", ("a much longer key", "key-two")), "key-two")
        self.assertEqual(lengths, [(32, 32), (32, 32)])


class StartTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.keys_file = self.tmp / "api-keys.txt"
        self.keys_file.write_text(KEYS, encoding="utf-8")

    def start(self, environ):
        """The exit code and stderr of python -m gate http under environ, which never reaches serve_forever."""
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(endpoint, "make_server", side_effect=AssertionError("the server started")), \
                contextlib.redirect_stderr(stderr):
            code = cli.main(["http", "--port", "0"])
        return code, stderr.getvalue()

    def test_the_server_refuses_to_start_without_keys_or_with_a_refused_mode(self):
        comments = self.tmp / "comments.txt"
        comments.write_text("# no key yet\n\n", encoding="utf-8")
        for fields, words in (({"SHIELDBOT_API_KEYS_FILE": None}, "no open mode"),
                              ({"SHIELDBOT_API_KEYS_FILE": str(comments)}, "holds no key"),
                              ({"SHIELDBOT_API_KEYS_FILE": str(self.tmp / "missing.txt")}, "cannot read"),
                              ({"GATE_FIXTURES_MODE": "record"}, "replay or off"),
                              ({"GATE_FIXTURES_MODE": "off"}, "a .testnet Gate only, not gate.near"),
                              ({"GATE_FIXTURES_MODE": "off", "GATE_ACCOUNT_ID": "gate.near"}, ".testnet Gate only"),
                              ({"SHIELDBOT_API_RATE": "0"}, "at least 1"),
                              ({"SHIELDBOT_API_RATE": "many"}, "whole number"),
                              ({"GATE_POLICY": "{"}, "not JSON"), ({"GATE_POLICY": "[]"}, "JSON object")):
            with self.subTest(fields=fields):
                environ = replay_environ(self.keys_file)
                environ.update(fields)
                environ = {key: value for key, value in environ.items() if value is not None}
                if "GATE_ACCOUNT_ID" not in fields and fields.get("GATE_FIXTURES_MODE") == "off":
                    environ.pop("GATE_ACCOUNT_ID")
                code, stderr = self.start(environ)
                self.assertEqual(code, 2)
                self.assertIn("not started: ", stderr)
                self.assertIn(words, stderr)

    def test_settings_read_the_keys_rate_and_mode(self):
        config = endpoint.settings(replay_environ(self.keys_file))
        self.assertEqual((config["keys"], config["rate"], config["mode"]), (("key-one", "key-two"), 60, "replay"))
        self.assertEqual(config["policy"], POLICY)
        config = endpoint.settings({"SHIELDBOT_API_KEYS_FILE": str(self.keys_file)})
        self.assertEqual((config["gate"], config["policy"]), ("gate.near", {"allowlist": [], "caps": []}))
        live = endpoint.settings(replay_environ(self.keys_file, GATE_FIXTURES_MODE="off",
                                                GATE_ACCOUNT_ID="gate.testnet", SHIELDBOT_API_RATE="5"))
        self.assertEqual((live["mode"], live["gate"], live["rate"]), ("live", "gate.testnet", 5))

    def test_live_mode_reports_live_on_health_without_a_read(self):
        environ = replay_environ(self.keys_file, GATE_FIXTURES_MODE="off", GATE_ACCOUNT_ID="gate.testnet")
        with mock.patch("urllib.request.urlopen", side_effect=no_network), mock.patch.dict(os.environ, environ):
            server = endpoint.make_server("127.0.0.1", 0, endpoint.settings(environ), self.tmp, store=labels.load())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
                connection.request("GET", "/health")
                reply = connection.getresponse()
                self.assertEqual((reply.status, json.loads(reply.read())), (200, {"ok": True, "mode": "live"}))
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_the_command_is_on_the_gate_command_line(self):
        process = subprocess.run([sys.executable, "-m", "gate", "http", "--help"], capture_output=True, cwd=ROOT,
                                 timeout=60, check=False)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))
        self.assertIn(b"--port", process.stdout)


class NoSigningKeyTest(unittest.TestCase):
    def test_the_module_never_imports_the_signer_or_names_the_verifier_key_file(self):
        text = SOURCE.read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
                imported.add(node.module)
        self.assertIn("gate.verdict", imported)
        self.assertNotIn("gate.signer", imported)
        for word in ("VERIFIER_KEY_FILE", "NaClSigner", "TestSigner", "PurePythonLiveSigner", "GATE_LIVE_SIGNING"):
            with self.subTest(word=word):
                self.assertNotIn(word, text)

    def test_the_verdict_call_signs_no_action(self):
        calls = [node for node in ast.walk(ast.parse(SOURCE.read_text(encoding="utf-8")))
                 if isinstance(node, ast.Call) and ast.unparse(node.func) == "verdict.request"]
        self.assertEqual(len(calls), 1)
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
        self.assertEqual((keywords["sign_actions"], keywords["signer"]), ("()", "None"))


if __name__ == "__main__":
    unittest.main()
