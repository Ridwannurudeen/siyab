import hashlib
import http.client
import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import intents, rpc, rules

ROOT = Path(__file__).resolve().parent.parent

INTENTS_VIEW_KEY = ("aeca5be441be45abec5d698a9b5c3dd2"
                    "a8b8090db3a2abc222c3e6b6463ccdd0")
USDC = ("17208628f84f5d6ad33f0da3bbbeb27f"
        "fcb398eac501a31bd6ad2011e36133a1")
DAY = 24 * 60 * 60
T0 = datetime(2026, 10, 29, 8, 0, tzinfo=UTC).timestamp()


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def reply(payload):
    handle = mock.MagicMock()
    handle.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return handle


class ReplayTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("urllib.request.urlopen", side_effect=no_network)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rpc = rpc.Client(rpc.MAINNET_RPC_URL, "replay")
        self.nearblocks = rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http")

    def test_view_account_and_code_hash(self):
        account = self.rpc.view_account("intents.near")
        self.assertNotEqual(account["code_hash"], rpc.EMPTY_CODE_HASH)
        self.assertEqual(self.rpc.last_block_height, account["block_height"])
        self.assertEqual(self.rpc.last_source, {"provider": "free.rpc.fastnear.com",
                                                "request_sha256": INTENTS_VIEW_KEY,
                                                "fetched_at": "2026-10-29T09:20:50Z"})
        for name in ("omft.near", "wrap.near"):
            self.assertNotEqual(self.rpc.code_hash(name), rpc.EMPTY_CODE_HASH)
        self.assertEqual(self.rpc.code_hash("relay.aurora"), rpc.EMPTY_CODE_HASH)

    def test_missing_account_is_an_rpc_error(self):
        with self.assertRaises(rpc.RpcError) as caught:
            self.rpc.view_account("no-such-account-example.near")
        self.assertEqual(caught.exception.cause, "UNKNOWN_ACCOUNT")

    def test_call_function(self):
        wrap = self.rpc.call_function("wrap.near", "ft_metadata", {})
        self.assertEqual((wrap["symbol"], wrap["decimals"]), ("wNEAR", 24))
        usdc = self.rpc.call_function(USDC, "ft_metadata", {})
        self.assertEqual((usdc["symbol"], usdc["decimals"]), ("USDC", 6))
        self.assertIsNotNone(self.rpc.last_block_height)
        for contract in ("omft.near", "intents.near"):
            metadata = self.rpc.call_function(contract, "contract_source_metadata", {})
            self.assertIn("nep330", [s["standard"] for s in metadata["standards"]])

    def test_storage_balance_of(self):
        registered = self.rpc.call_function("wrap.near", "storage_balance_of",
                                            {"account_id": "intents.near"})
        self.assertIsNotNone(registered)
        self.assertIsNone(self.rpc.call_function("wrap.near", "storage_balance_of",
                                                 {"account_id": "no-such-account-example.near"}))

    def test_access_key_list_over_the_limit_is_an_error(self):
        with self.assertRaises(rpc.RpcError) as caught:
            self.rpc.view_access_key_list("near")
        self.assertEqual(caught.exception.cause, "TOO_MANY_ACCESS_KEYS")

    def test_nearblocks_keys_and_transaction(self):
        keys = self.nearblocks.get("/v1/account/connectors.outlayer.near/keys")["keys"]
        self.assertEqual(self.nearblocks.last_source["provider"], "api.nearblocks.io")
        created = keys[0]["created"]
        transaction = self.nearblocks.get(f"/v1/txns/{created['transaction_hash']}")["txns"][0]
        self.assertEqual(transaction["receiver_account_id"], "connectors.outlayer.near")
        self.assertIn("deposit", transaction["actions_agg"])

    def test_miss_raises_fixture_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=tmp)
            with self.assertRaises(rpc.FixtureMiss) as caught:
                client.view_account("intents.near")
            self.assertEqual(caught.exception.key, INTENTS_VIEW_KEY)
            self.assertEqual(client.last_source["fetched_at"], None)
            self.assertEqual(os.listdir(tmp), [])


class KeyTest(unittest.TestCase):
    def test_key_is_sha256_of_the_sorted_compact_request(self):
        params = {"request_type": "view_account", "finality": "final", "account_id": "intents.near"}
        text = ('{"method":"query","params":{"account_id":"intents.near","finality":"final",'
                '"request_type":"view_account"},"url":"https://free.rpc.fastnear.com"}')
        key = rpc.fixture_key(rpc.MAINNET_RPC_URL, "query", params)
        self.assertEqual(key, hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(key, INTENTS_VIEW_KEY)

    def test_key_ignores_param_order_and_changes_with_any_part(self):
        a = rpc.fixture_key("https://x", "query", {"a": 1, "b": 2})
        self.assertEqual(a, rpc.fixture_key("https://x", "query", {"b": 2, "a": 1}))
        self.assertNotEqual(a, rpc.fixture_key("https://y", "query", {"a": 1, "b": 2}))
        self.assertNotEqual(a, rpc.fixture_key("https://x", "block", {"a": 1, "b": 2}))
        self.assertNotEqual(a, rpc.fixture_key("https://x", "query", {"a": 1, "b": 3}))

    def test_every_committed_fixture_is_named_by_its_key(self):
        for path in sorted(rpc.FIXTURES_DIR.glob("*.json")):
            with self.subTest(path=path.name):
                fixture = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(sorted(fixture), ["fetched_at", "request", "response", "url"])
                key = rpc.fixture_key(fixture["url"], fixture["request"]["method"],
                                      fixture["request"]["params"])
                self.assertEqual(path.stem, key)


class ModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = T0

    def client(self, mode, kind="rpc"):
        return rpc.Client("https://rpc.example", mode, kind=kind, fixtures_dir=self.tmp.name,
                          clock=lambda: self.now)

    def test_default_mode_is_replay(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rpc.Client("https://rpc.example").mode, "replay")
        with mock.patch.dict(os.environ, {"GATE_FIXTURES_MODE": "record"}):
            self.assertEqual(rpc.Client("https://rpc.example").mode, "record")
        with mock.patch.dict(os.environ, {"GATE_FIXTURES_MODE": "live"}), self.assertRaises(ValueError):
            rpc.Client("https://rpc.example")

    def test_record_writes_and_the_24_hour_rule(self):
        first = {"jsonrpc": "2.0", "id": "gate", "result": {"block_height": 1, "code_hash": "a"}}
        second = {"jsonrpc": "2.0", "id": "gate", "result": {"block_height": 2, "code_hash": "b"}}
        with mock.patch("urllib.request.urlopen", return_value=reply(first)) as opened:
            self.assertEqual(self.client("record").view_account("a.near")["block_height"], 1)
        request = opened.call_args.args[0]
        self.assertEqual(opened.call_args.kwargs["timeout"], 30)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data)["params"]["account_id"], "a.near")
        [path] = Path(self.tmp.name).iterdir()
        self.assertEqual(json.loads(path.read_text())["fetched_at"], "2026-10-29T08:00:00Z")

        self.now = T0 + DAY - 1
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            self.assertEqual(self.client("record").view_account("a.near")["block_height"], 1)

        self.now = T0 + DAY
        with mock.patch("urllib.request.urlopen", return_value=reply(second)):
            self.assertEqual(self.client("record").view_account("a.near")["block_height"], 2)
        fixture = json.loads(path.read_text())
        self.assertEqual(fixture["fetched_at"], "2026-10-30T08:00:00Z")
        self.assertEqual(fixture["response"], second)

        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            self.assertEqual(self.client("replay").view_account("a.near")["block_height"], 2)

    def test_replay_serves_old_fixtures(self):
        with mock.patch("urllib.request.urlopen", return_value=reply({"result": {"x": 1}})):
            self.client("record").call("status", [])
        self.now = T0 + 365 * DAY
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            self.assertEqual(self.client("replay").call("status", []), {"x": 1})

    def test_off_fetches_every_time_and_writes_nothing(self):
        with mock.patch("urllib.request.urlopen", return_value=reply({"result": {"x": 1}})) as opened:
            self.client("off").call("status", [])
            self.client("off").call("status", [])
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_http_error_is_raised_and_never_recorded(self):
        error = urllib.error.HTTPError("https://rpc.example/x", 429, "Too Many Requests", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error), \
                self.assertRaises(rpc.HttpError) as caught:
            self.client("record", kind="http").get("/x")
        self.assertEqual(caught.exception.status, 429)
        self.assertIsNone(caught.exception.retry_after)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_selected_http_error_is_recorded_and_replayed(self):
        address = "unknown-status.near"
        params = {"depositAddress": address}
        error = urllib.error.HTTPError("https://rpc.example/v0/status?depositAddress=unknown-status.near",
                                       404, "Not Found", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error), self.assertRaises(rpc.HttpError) as caught:
            self.client("record", kind="http").get("/v0/status", params=params, record_http_errors=(404,))
        self.assertEqual(caught.exception.status, 404)
        [path] = Path(self.tmp.name).iterdir()
        fixture = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["response"], {rpc.HTTP_STATUS_MARKER: 404})
        fixture["synthetic"] = True
        path.write_text(json.dumps(fixture), encoding="utf-8")
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            replayed = self.client("replay", kind="http")
            with self.assertRaises(rpc.HttpError) as caught:
                replayed.get("/v0/status", params=params, record_http_errors=(404,))
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(replayed.last_source["fetched_at"], fixture["fetched_at"])

    def test_a_429_carries_its_retry_after_header(self):
        error = urllib.error.HTTPError("https://rpc.example/x", 429, "Too Many Requests", {"Retry-After": "120"},
                                       None)
        with mock.patch("urllib.request.urlopen", side_effect=error), \
                self.assertRaises(rpc.HttpError) as caught:
            self.client("off", kind="http").get("/x")
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (429, "120"))

    def test_http_get_and_post(self):
        with mock.patch("urllib.request.urlopen", return_value=reply({"ok": True})) as opened:
            client = self.client("record", kind="http")
            self.assertEqual(client.get("/v1/a"), {"ok": True})
            self.assertEqual(client.post("https://other.example/scan", {"content": "m"}), {"ok": True})
        self.assertEqual(opened.call_args_list[0].args[0].full_url, "https://rpc.example/v1/a")
        self.assertEqual(opened.call_args_list[0].args[0].get_method(), "GET")
        posted = opened.call_args_list[1].args[0]
        self.assertEqual((posted.get_method(), json.loads(posted.data)), ("POST", {"content": "m"}))
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())), 2)

    def test_http_get_query_params_are_recorded_separately_and_replayed(self):
        answer = {"value": "1"}
        with mock.patch("urllib.request.urlopen", return_value=reply(answer)) as opened:
            client = self.client("record", kind="http")
            self.assertEqual(client.get("/v1/resource", params={"key": "deposit.near"}), answer)
        self.assertEqual(opened.call_args.args[0].full_url,
                         "https://rpc.example/v1/resource?key=deposit.near")
        [path] = Path(self.tmp.name).iterdir()
        fixture = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["url"], "https://rpc.example/v1/resource")
        self.assertEqual(fixture["request"], {"method": "GET", "params": {"key": "deposit.near"}})
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            replayed = self.client("replay", kind="http")
            self.assertEqual(replayed.get("/v1/resource", params={"key": "deposit.near"}), answer)
        self.assertEqual(replayed.last_source["fetched_at"], fixture["fetched_at"])

    def test_headers_are_sent_and_never_written(self):
        header = "Bearer test-fastnear-header"
        client = rpc.Client("https://transfers.main.fastnear.com", "record", kind="http",
                            fixtures_dir=self.tmp.name, headers={"Authorization": header}, clock=lambda: T0)
        with mock.patch("urllib.request.urlopen", return_value=reply({"transfers": []})) as opened:
            self.assertEqual(client.post("/v0/transfers", {"account_id": "incident.near"}), {"transfers": []})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), header)
        self.assertEqual(client.last_source, {"provider": "transfers.main.fastnear.com",
                                              "request_sha256": rpc.fixture_key(
                                                  "https://transfers.main.fastnear.com/v0/transfers", "POST",
                                                  rpc.body_params({"account_id": "incident.near"})),
                                              "fetched_at": "2026-10-29T08:00:00Z"})
        [path] = Path(self.tmp.name).iterdir()
        fixture = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("Authorization", fixture)
        self.assertNotIn("test-fastnear-header", path.read_text(encoding="utf-8"))

    def test_kind_mismatch(self):
        with self.assertRaises(ValueError):
            self.client("replay").get("/x")
        with self.assertRaises(ValueError):
            self.client("replay", kind="http").call("status", [])

    def test_call_function_error_in_result(self):
        body = {"result": {"error": "wasm execution failed", "block_height": 3}}
        with mock.patch("urllib.request.urlopen", return_value=reply(body)), \
                self.assertRaises(rpc.RpcError):
            self.client("off").call_function("a.near", "missing", {})

    def test_final_block(self):
        body = {"result": {"header": {"height": 7, "timestamp": 1, "hash": "h"}}}
        with mock.patch("urllib.request.urlopen", return_value=reply(body)) as opened:
            self.assertEqual(self.client("off").final_block()["header"]["height"], 7)
        self.assertEqual(json.loads(opened.call_args.args[0].data)["params"], {"finality": "final"})

    def test_empty_code_hash(self):
        self.assertEqual(rpc.EMPTY_CODE_HASH, "1" * 32)


class DeadlineFetchTest(unittest.TestCase):
    def test_a_deadline_bounds_slow_response_headers(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()

        def respond():
            connection, _ = listener.accept()
            with connection:
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    request.extend(chunk)
                connection.sendall(b"HTTP/1.1 200 OK\r\n")
                header = b"X-Slow: " + b"a" * 40 + b"\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
                for byte in header:
                    try:
                        connection.sendall(bytes((byte,)))
                    except OSError:
                        return
                    time.sleep(0.02)

        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with rpc.request_deadline(0.15), self.assertRaises(TimeoutError):
                rpc._fetch(f"http://127.0.0.1:{listener.getsockname()[1]}/", None)
        finally:
            listener.close()
            thread.join(timeout=2)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_a_deadline_bounds_the_whole_response_read(self):
        now = [0.0]

        class SlowReply:
            def __init__(self):
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

            def read1(self, _size):
                self.reads += 1
                now[0] += 0.6
                return b'{"ok": true}'

        response = SlowReply()
        token = rpc._REQUEST_DEADLINE.set(1.0)
        try:
            with mock.patch.object(rpc.time, "monotonic", side_effect=lambda: now[0]), \
                    mock.patch.object(rpc, "_open_with_deadline", return_value=response):
                with self.assertRaises(TimeoutError):
                    rpc._fetch("https://rpc.example/read", None)
        finally:
            rpc._REQUEST_DEADLINE.reset(token)
        self.assertEqual(response.reads, 2)

    def test_without_a_deadline_the_existing_unbounded_read_path_is_preserved(self):
        response = reply({"ok": True})
        with mock.patch("urllib.request.urlopen", return_value=response) as opened:
            self.assertEqual(rpc._fetch("https://rpc.example/read", None), {"ok": True})
        self.assertEqual(opened.call_args.kwargs["timeout"], rpc.TIMEOUT_S)
        response.__enter__.return_value.read.assert_called_once_with()
        response.__enter__.return_value.read1.assert_not_called()

    def test_transport_errors_and_public_details_hide_endpoint_urls(self):
        url = "https://rpc.example/private/path"
        error = rpc.TransportError(f"BadStatusLine from {url}")
        self.assertNotIn(url, str(error))
        self.assertNotIn(url, rules._why(error))
        self.assertNotIn(url, intents._why(error))

        with mock.patch("urllib.request.urlopen", side_effect=http.client.BadStatusLine("x")), \
                self.assertRaises(rpc.TransportError) as caught:
            rpc._fetch(url, None)
        self.assertNotIn(url, str(caught.exception))



class FailClosedClientTest(unittest.TestCase):
    """Malformed bodies, what record mode writes, and the NearBlocks limits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(rpc._NEARBLOCKS_USE.clear)
        rpc._NEARBLOCKS_USE.clear()

    def test_recorded_block_is_trimmed_replayed_and_hygienic(self):
        header = {"hash": "block-hash", "height": 7, "timestamp": 1,
                  "prev_hash": "previous-hash"}
        full_response = {"jsonrpc": "2.0", "id": "gate", "result": {"header": header,
                         "chunks": [{"signature": "ed25519:" + "1" * 88}]}}
        expected = {"jsonrpc": "2.0", "id": "gate",
                    "result": {"header": {"hash": "block-hash", "height": 7, "timestamp": 1}}}
        client = rpc.Client("https://rpc.example", "record", fixtures_dir=self.tmp.name)
        with mock.patch("urllib.request.urlopen", return_value=reply(full_response)):
            self.assertEqual(client.block_at(7)["header"]["height"], 7)
        [path] = Path(self.tmp.name).iterdir()
        fixture = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["response"], expected)
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            replayed = rpc.Client("https://rpc.example", "replay", fixtures_dir=self.tmp.name)
            self.assertEqual(replayed.block_at(7), expected["result"])
        spec = importlib.util.spec_from_file_location("check_hygiene", ROOT / "scripts" / "check_hygiene.py")
        hygiene = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hygiene)
        self.assertFalse(hygiene.check_bytes(f"fixtures/rpc/{path.name}", path.read_bytes()))

    def test_block_error_response_is_not_trimmed(self):
        error = {"jsonrpc": "2.0", "id": "gate", "error": {"code": -32000, "message": "UNKNOWN_BLOCK"},
                 "result": {"header": {"hash": "h", "height": 7, "timestamp": 1}}}
        client = rpc.Client("https://rpc.example", "record", fixtures_dir=self.tmp.name)
        with (mock.patch("urllib.request.urlopen", return_value=reply(error)),
              self.assertRaises(rpc.RpcError)):
            client.block_at(7)
        [path] = Path(self.tmp.name).iterdir()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["response"], error)
    def test_malformed_block_response_is_recorded_unchanged(self):
        for header in ({"hash": "h", "height": 7, "timestamp": True},
                       {"hash": 5, "height": 7, "timestamp": 1},
                       {"hash": "h", "height": -1, "timestamp": 1}):
            with self.subTest(header=header):
                for stale in Path(self.tmp.name).iterdir():
                    stale.unlink()
                malformed = {"jsonrpc": "2.0", "id": "gate", "result": {"header": header, "chunks": []}}
                client = rpc.Client("https://rpc.example", "record", fixtures_dir=self.tmp.name)
                with (mock.patch("urllib.request.urlopen", return_value=reply(malformed)),
                      self.assertRaises(rpc.MalformedResponse)):
                    client.block_at(7)
                [path] = Path(self.tmp.name).iterdir()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["response"], malformed)

    def test_malformed_bodies_raise_malformed_response(self):
        client = rpc.Client("https://rpc.example", "off")
        cases = [
            ("view_account", ("a.near",), {"jsonrpc": "2.0", "id": "gate", "result": None}),
            ("view_account", ("a.near",), {"jsonrpc": "2.0", "id": "gate"}),
            ("view_account", ("a.near",), ["a", "list"]),
            ("view_account", ("a.near",), {"result": "oops"}),
            ("view_account", ("a.near",), {"result": {"amount": "1"}}),
            ("view_account_at", ("a.near", 5), {"result": {"amount": "1"}}),
            ("call_function", ("t.near", "m", {}), {"result": {"result": "text"}}),
            ("call_function", ("t.near", "m", {}), {"result": {"logs": []}}),
            ("call_function", ("t.near", "m", {}), {"result": {"result": [256]}}),
            ("call_function", ("t.near", "m", {}), {"result": {"result": list(b"{bad")}}),
            ("view_access_key_list", ("a.near",), {"result": {"nonce": 1}}),
            ("final_block", (), {"result": {"chunks": []}}),
            ("final_block", (), {"result": {"header": {"height": 5}}}),
            ("block_at", (5,), {"result": {"chunks": []}}),
            ("block_at", (5,), {"result": {"header": {"height": 5, "timestamp": True, "hash": "h"}}}),
            ("block_at", (5,), {"result": {"header": {"height": 5, "timestamp": -1, "hash": "h"}}}),
            ("block_at", (5,), {"result": {"header": {"height": True, "timestamp": 1, "hash": "h"}}}),
            ("final_block", (), {"result": {"header": {"height": 5, "timestamp": False, "hash": "h"}}}),
            ("block_at", (5,), {"result": {"header": {"height": 5}}}),
        ]
        for method, args, body in cases:
            with (self.subTest(method=method, body=body),
                  mock.patch("urllib.request.urlopen", return_value=reply(body)),
                  self.assertRaises(rpc.MalformedResponse)):
                getattr(client, method)(*args)
        self.assertTrue(issubclass(rpc.MalformedResponse, ValueError))

    def test_a_body_nested_too_deeply_to_parse_is_malformed(self):
        deep = b"[" * 100000
        unbounded = mock.MagicMock()
        unbounded.__enter__.return_value.read.return_value = deep
        bounded = mock.MagicMock()
        bounded.__enter__.return_value.read1.side_effect = [deep, b""]
        with (mock.patch("urllib.request.urlopen", return_value=unbounded),
              self.assertRaises(rpc.MalformedResponse) as caught):
            rpc._fetch("https://api.example/x", None)
        self.assertEqual(str(caught.exception), "the body is nested too deeply to parse")
        with (rpc.request_deadline(60), mock.patch.object(rpc, "_open_with_deadline", return_value=bounded),
              self.assertRaises(rpc.MalformedResponse) as caught):
            rpc._fetch("https://api.example/x", None)
        self.assertEqual(str(caught.exception), "the body is nested too deeply to parse")
        bounded.__enter__.return_value.read1.assert_called()

    def test_a_body_nested_too_deeply_to_record_is_malformed_and_not_written(self):
        nested = b'{"result": ' + b"[" * 2000 + b"]" * 2000 + b"}"
        handle = mock.MagicMock()
        handle.__enter__.return_value.read.return_value = nested
        client = rpc.Client("https://api.example", "record", kind="http", fixtures_dir=self.tmp.name)
        with (mock.patch("urllib.request.urlopen", return_value=handle),
              self.assertRaises(rpc.MalformedResponse) as caught):
            client.get("/x")
        self.assertEqual(str(caught.exception), "the body is nested too deeply to record")
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_a_truncated_reply_is_a_transport_error(self):
        client = rpc.Client("https://rpc.example", "record", fixtures_dir=self.tmp.name)
        for error in (http.client.IncompleteRead(b""), http.client.BadStatusLine("x")):
            with (self.subTest(error=type(error).__name__),
                  mock.patch("urllib.request.urlopen", side_effect=error),
                  self.assertRaises(rpc.TransportError)):
                client.view_account("a.near")
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_a_post_body_is_never_written(self):
        memo = "pay me, seed phrase abandon abandon"
        client = rpc.Client("https://api.example", "record", kind="http", fixtures_dir=self.tmp.name)
        with mock.patch("urllib.request.urlopen", return_value=reply({"clean": True})) as opened:
            client.post("/api/scan/injection", {"content": memo})
        self.assertEqual(json.loads(opened.call_args.args[0].data), {"content": memo})
        [path] = Path(self.tmp.name).iterdir()
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("abandon", text)
        self.assertEqual(json.loads(text)["request"]["params"], rpc.body_params({"content": memo}))
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            replayed = rpc.Client("https://api.example", "replay", kind="http", fixtures_dir=self.tmp.name)
            self.assertEqual(replayed.post("/api/scan/injection", {"content": memo}), {"clean": True})

    def test_user_info_in_a_url_is_refused(self):
        for url in ("https://user:pass@api.example", "https://token@api.example"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                rpc.Client(url, "off", kind="http")
        client = rpc.Client("https://api.example", "off", kind="http")
        with self.assertRaises(ValueError):
            client.get("https://key@other.example/x")

    def test_a_query_string_in_a_url_is_refused(self):
        for url in ("https://api.example?apikey=k", "https://api.example/v1?token=t"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                rpc.Client(url, "off", kind="http")
        client = rpc.Client("https://api.example", "off", kind="http")
        with mock.patch("urllib.request.urlopen", side_effect=no_network), self.assertRaises(ValueError):
            client.get("https://other.example/x?apikey=k")
        with mock.patch("urllib.request.urlopen", side_effect=no_network), self.assertRaises(ValueError):
            client.get("/api/v1/address_security/0xabc?apikey=k")

    def test_nearblocks_fetches_are_spaced_and_budgeted(self):
        now = [T0]
        slept = []
        client = rpc.Client(rpc.NEARBLOCKS_URL, "off", kind="http", clock=lambda: now[0], sleep=slept.append)
        with mock.patch("urllib.request.urlopen", return_value=reply({"keys": []})) as opened:
            client.get("/v1/account/a.near/keys")
            now[0] += 4
            client.get("/v1/account/b.near/keys")
            now[0] += 30
            client.get("/v1/account/c.near/keys")
        self.assertEqual(slept, [6])
        self.assertEqual(opened.call_count, 3)
        rpc._NEARBLOCKS_USE["api.nearblocks.io"]["count"] = rpc.NEARBLOCKS_DAILY_BUDGET
        with mock.patch("urllib.request.urlopen", side_effect=no_network), self.assertRaises(rpc.RateLimited):
            client.get("/v1/account/d.near/keys")
        now[0] += DAY
        with mock.patch("urllib.request.urlopen", return_value=reply({"keys": []})):
            client.get("/v1/account/d.near/keys")

    def test_other_hosts_and_replay_are_not_throttled(self):
        slept = []
        client = rpc.Client("https://rpc.example", "off", clock=lambda: T0, sleep=slept.append)
        with mock.patch("urllib.request.urlopen", return_value=reply({"result": {"x": 1}})):
            client.call("status", [])
            client.call("status", [])
        self.assertEqual(slept, [])
        self.assertEqual(rpc._NEARBLOCKS_USE, {})


class HygieneRuleTest(unittest.TestCase):
    def test_a_128_hex_string_under_fixtures_rpc_is_rejected(self):
        spec = importlib.util.spec_from_file_location("check_hygiene", ROOT / "scripts" / "check_hygiene.py")
        hygiene = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hygiene)
        secret = "ab" * 64
        self.assertTrue(hygiene.check_text("fixtures/rpc/x.json", f'{{"memo": "{secret}"}}'))
        self.assertTrue(hygiene.check_text("fixtures/demo/x.json", f'{{"memo": "{secret}"}}'))
        self.assertTrue(hygiene.check_text("fixtures/scan/x.json", f'{{"memo": "{secret}"}}'))
        for directory in ("cluster", "fastnear", "feed", "goplus", "intents", "receivers", "registries", "runs"):
            self.assertTrue(hygiene.check_text(f"fixtures/{directory}/x.json", f'{{"memo": "{secret}"}}'))
        self.assertFalse(hygiene.check_text("fixtures/canon/x.json", f'{{"signature_hex": "{secret}"}}'))
        self.assertFalse(hygiene.check_text("fixtures/rpc/x.json", '{"hash": "' + "ab" * 32 + '"}'))

if __name__ == "__main__":
    unittest.main()
