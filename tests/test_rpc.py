import base64
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
        account = {"amount": "1", "locked": "0", "storage_usage": 1, "block_hash": rpc.EMPTY_CODE_HASH}
        first = {"jsonrpc": "2.0", "id": "gate", "result": {**account, "block_height": 1, "code_hash": rpc.base58.encode(bytes([1]) * 32)}}
        second = {"jsonrpc": "2.0", "id": "gate", "result": {**account, "block_height": 2, "code_hash": rpc.base58.encode(bytes([2]) * 32)}}
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
        with mock.patch("urllib.request.urlopen", return_value=reply({"jsonrpc": "2.0", "id": "gate", "result": {"x": 1}})):
            self.client("record").call("status", [])
        self.now = T0 + 365 * DAY
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            self.assertEqual(self.client("replay").call("status", []), {"x": 1})

    def test_off_fetches_every_time_and_writes_nothing(self):
        with mock.patch("urllib.request.urlopen", return_value=reply({"jsonrpc": "2.0", "id": "gate", "result": {"x": 1}})) as opened:
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

    def test_authorization_is_only_on_the_original_request(self):
        header = "Bearer synthetic-key"
        for name in ("Authorization", "authorization", "AUTHORIZATION"):
            with self.subTest(header=name):
                response = mock.MagicMock()
                response.__enter__.return_value.read1.side_effect = [b'{}', b'']
                with mock.patch("urllib.request.urlopen", side_effect=no_network), \
                        mock.patch("gate.rpc._open_with_deadline", return_value=response) as opened, \
                        mock.patch("gate.rpc._fetch", wraps=rpc._fetch), rpc.request_deadline(25):
                    self.assertEqual(rpc._fetch("https://transfers.main.fastnear.com/v0/transfers", None,
                                                {name: header, "Accept": "application/json"}), {})
                request = opened.call_args.args[0]
                self.assertFalse(any(key.lower() == "authorization" for key in request.headers))
                self.assertEqual(request.unredirected_hdrs["Authorization"], header)
                self.assertEqual(request.get_header("Authorization"), header)
                for target in ("https://other.example/read", "http://transfers.main.fastnear.com/read"):
                    redirected = urllib.request.HTTPRedirectHandler().redirect_request(
                        request, None, 302, "Found", {}, target)
                    self.assertIsNone(redirected.get_header("Authorization"))
                    self.assertEqual(redirected.get_header("Accept"), "application/json")

    def test_record_mode_strips_authorization_on_unkeyed_hosts(self):
        for host in ("rpc.example", "api.nearblocks.io", "test.rpc.fastnear.com"):
            for header in ("Authorization", "authorization", "AUTHORIZATION"):
                with self.subTest(host=host, header=header), \
                        mock.patch("urllib.request.urlopen", side_effect=no_network), \
                        mock.patch("gate.rpc._open_with_deadline", side_effect=no_network), \
                        mock.patch("gate.rpc._fetch", return_value={"ok": True}) as fetched:
                    rpc._NEARBLOCKS_USE.clear()
                    client = rpc.Client("https://transfers.main.fastnear.com", "record", kind="http",
                                        fixtures_dir=self.tmp.name, clock=lambda: T0,
                                        headers={header: "Bearer synthetic-key", "Accept": "application/json"})
                    client.get(f"https://{host}/{header}")
                    self.assertEqual(fetched.call_args.args[2], {"Accept": "application/json"})
        rpc._NEARBLOCKS_USE.clear()

    def test_kind_mismatch(self):
        with self.assertRaises(ValueError):
            self.client("replay").get("/x")
        with self.assertRaises(ValueError):
            self.client("replay", kind="http").call("status", [])

    def test_call_function_error_in_result(self):
        body = {"jsonrpc": "2.0", "id": "gate", "result": {"error": "wasm execution failed", "block_height": 3}}
        with mock.patch("urllib.request.urlopen", return_value=reply(body)), \
                self.assertRaises(rpc.RpcError):
            self.client("off").call_function("a.near", "missing", {})

    def test_final_block(self):
        body = {"jsonrpc": "2.0", "id": "gate", "result": {"header": {"height": 7, "timestamp": 1, "hash": rpc.EMPTY_CODE_HASH}}}
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

    def test_envelope_binding_is_identical_for_record_and_replay(self):
        account = {"amount": "1", "locked": "0", "storage_usage": 1, "code_hash": rpc.EMPTY_CODE_HASH,
                   "block_height": 7, "block_hash": rpc.EMPTY_CODE_HASH}
        for envelope in ({"id": "different-request"}, {"id": {}}, {"jsonrpc": []}, {}):
            with self.subTest(envelope=envelope), tempfile.TemporaryDirectory() as directory:
                response = {"jsonrpc": "2.0", "id": "gate", "result": account, **envelope}
                client = rpc.Client("https://rpc.example", "record", fixtures_dir=directory)
                with mock.patch.object(rpc, "_fetch", return_value=response) as fetched:
                    if envelope:
                        with self.assertRaises(rpc.MalformedResponse):
                            client.view_account("old.near")
                    else:
                        self.assertEqual(client.view_account("old.near"), account)
                self.assertEqual(fetched.call_args.args[1]["id"], "gate")
                self.assertEqual(fetched.call_args.args[1]["jsonrpc"], "2.0")
                [path] = Path(directory).iterdir()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["response"], response)
                replayed = rpc.Client("https://rpc.example", "replay", fixtures_dir=directory)
                with mock.patch.object(rpc, "_fetch", side_effect=no_network):
                    if envelope:
                        with self.assertRaises(rpc.MalformedResponse):
                            replayed.view_account("old.near")
                    else:
                        self.assertEqual(replayed.view_account("old.near"), account)

    def test_recorded_block_is_trimmed_replayed_and_hygienic(self):
        header = {"hash": rpc.EMPTY_CODE_HASH, "height": 7, "timestamp": 1,
                  "prev_hash": rpc.EMPTY_CODE_HASH}
        full_response = {"jsonrpc": "2.0", "id": "gate", "result": {"header": header,
                         "chunks": [{"signature": "ed25519:" + "1" * 88}]}}
        expected = {"jsonrpc": "2.0", "id": "gate",
                    "result": {"header": {"hash": rpc.EMPTY_CODE_HASH, "height": 7, "timestamp": 1}}}
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
                 "result": {"header": {"hash": rpc.EMPTY_CODE_HASH, "height": 7, "timestamp": 1}}}
        client = rpc.Client("https://rpc.example", "record", fixtures_dir=self.tmp.name)
        with (mock.patch("urllib.request.urlopen", return_value=reply(error)),
              self.assertRaises(rpc.MalformedResponse)):
            client.block_at(7)
        [path] = Path(self.tmp.name).iterdir()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["response"], error)
    def test_malformed_block_response_is_recorded_unchanged(self):
        for header in ({"hash": rpc.EMPTY_CODE_HASH, "height": 7, "timestamp": True},
                       {"hash": 5, "height": 7, "timestamp": 1},
                       {"hash": rpc.EMPTY_CODE_HASH, "height": -1, "timestamp": 1}):
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
            ("view_account", ("a.near",), {"jsonrpc": "2.0", "id": "gate", "result": "oops"}),
            ("view_account", ("a.near",), {"jsonrpc": "2.0", "id": "gate", "result": {"amount": "1"}}),
            ("view_account_at", ("a.near", 5), {"jsonrpc": "2.0", "id": "gate", "result": {"amount": "1"}}),
            ("call_function", ("t.near", "m", {}), {"jsonrpc": "2.0", "id": "gate", "result": {"result": "text"}}),
            ("call_function", ("t.near", "m", {}), {"jsonrpc": "2.0", "id": "gate", "result": {"logs": []}}),
            ("call_function", ("t.near", "m", {}), {"jsonrpc": "2.0", "id": "gate", "result": {"result": [256]}}),
            ("call_function", ("t.near", "m", {}), {"jsonrpc": "2.0", "id": "gate", "result": {"result": list(b"{bad")}}),
            ("view_access_key_list", ("a.near",), {"jsonrpc": "2.0", "id": "gate", "result": {"nonce": 1}}),
            ("final_block", (), {"jsonrpc": "2.0", "id": "gate", "result": {"chunks": []}}),
            ("final_block", (), {"jsonrpc": "2.0", "id": "gate", "result": {"header": {"height": 5}}}),
            ("block_at", (5,), {"jsonrpc": "2.0", "id": "gate", "result": {"chunks": []}}),
            ("block_at", (5,), {"jsonrpc": "2.0", "id": "gate", "result": {"header": {"height": 5, "timestamp": True, "hash": rpc.EMPTY_CODE_HASH}}}),
            ("block_at", (5,), {"jsonrpc": "2.0", "id": "gate", "result": {"header": {"height": 5, "timestamp": -1, "hash": rpc.EMPTY_CODE_HASH}}}),
            ("block_at", (5,), {"jsonrpc": "2.0", "id": "gate", "result": {"header": {"height": True, "timestamp": 1, "hash": rpc.EMPTY_CODE_HASH}}}),
            ("final_block", (), {"jsonrpc": "2.0", "id": "gate", "result": {"header": {"height": 5, "timestamp": False, "hash": rpc.EMPTY_CODE_HASH}}}),
            ("block_at", (5,), {"jsonrpc": "2.0", "id": "gate", "result": {"header": {"height": 5}}}),
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

    def test_a_replayed_fixture_nested_too_deeply_to_parse_is_malformed(self):
        client = rpc.Client("https://api.example", "replay", kind="http", fixtures_dir=self.tmp.name)
        path = Path(self.tmp.name) / (rpc.fixture_key("https://api.example/x", "GET", None) + ".json")
        nested = '{"fetched_at":"2026-10-29T08:00:00Z","response":' + "[" * 100000 + "0" + "]" * 100000 + "}"
        path.write_text(nested, encoding="utf-8", newline="\n")
        with (mock.patch("urllib.request.urlopen", side_effect=no_network),
              self.assertRaises(rpc.MalformedResponse) as caught):
            client.get("/x")
        self.assertEqual(str(caught.exception), "the body is nested too deeply to parse")

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

    def test_a_request_being_served_never_sleeps_for_a_nearblocks_slot(self):
        now = [T0]
        slept = []
        client = rpc.Client(rpc.NEARBLOCKS_URL, "off", kind="http", clock=lambda: now[0], sleep=slept.append)
        with mock.patch("gate.rpc._fetch", return_value={"keys": []}) as opened, rpc.request_deadline(25):
            client.get("/v1/account/a.near/keys")
            now[0] += 4
            with self.assertRaises(rpc.RateLimited) as caught:
                client.get("/v1/account/b.near/keys")
            now[0] += 6
            client.get("/v1/account/c.near/keys")
        self.assertNotIsInstance(caught.exception, rpc.BudgetExhausted)
        self.assertEqual((slept, opened.call_count), ([], 2))
        self.assertEqual(rpc._NEARBLOCKS_USE["api.nearblocks.io"]["count"], 2)

    def test_unkeyed_hosts_never_receive_authorization(self):
        for host in ("rpc.example", "api.nearblocks.io", "test.rpc.fastnear.com"):
            for header in ("Authorization", "authorization", "AUTHORIZATION"):
                with self.subTest(host=host, header=header), \
                        mock.patch("urllib.request.urlopen", side_effect=no_network), \
                        mock.patch("gate.rpc._open_with_deadline", side_effect=no_network), \
                        mock.patch("gate.rpc._fetch", return_value={"jsonrpc": "2.0", "id": "gate", "result": {"ok": True}}) as fetched:
                    rpc._NEARBLOCKS_USE.clear()
                    client = rpc.Client(f"https://{host}", "off", headers={header: "Bearer synthetic-key",
                                                                            "Accept": "application/json"})
                    client.call("status", [])
                    self.assertEqual(fetched.call_args.args[2], {"Accept": "application/json"})

    def test_other_hosts_and_replay_are_not_throttled(self):
        slept = []
        client = rpc.Client("https://rpc.example", "off", clock=lambda: T0, sleep=slept.append)
        with mock.patch("urllib.request.urlopen", return_value=reply({"jsonrpc": "2.0", "id": "gate", "result": {"x": 1}})):
            client.call("status", [])
            client.call("status", [])
        self.assertEqual(slept, [])
        self.assertEqual(rpc._NEARBLOCKS_USE, {})


class QueryShapeTest(unittest.TestCase):
    def setUp(self):
        self.client = rpc.Client("https://rpc.example", "replay")
        self.context = {"block_height": 7, "block_hash": rpc.EMPTY_CODE_HASH}
        self.permission = {"FunctionCall": {"allowance": None, "receiver_id": "app.near", "method_names": []}}
        self.access = {"nonce": 1, "permission": self.permission}
        self.metadata = {"version": "1", "link": None, "standards": [{"standard": "nep330", "version": "1"}]}
        self.results = {
            "view_account": {"amount": "1", "locked": "0", "storage_usage": 1, "code_hash": rpc.EMPTY_CODE_HASH},
            "view_access_key": self.access,
            "view_access_key_list": {"keys": [{"public_key": "ed25519:" + rpc.EMPTY_CODE_HASH, "access_key": self.access}]},
            "view_code": {"code_base64": "AA==", "hash": rpc.base58.encode(hashlib.sha256(bytes(1)).digest())},
            "call_function": {"result": list(json.dumps(self.metadata).encode("utf-8")), "logs": []},
        }

    def read(self, kind, result):
        params = {"request_type": kind, "account_id": "old.near", "finality": "final"}
        if kind == "call_function":
            params["method_name"] = "contract_source_metadata"
        with mock.patch.object(self.client, "_exchange", return_value={"jsonrpc": "2.0", "id": "gate", "result": result}):
            return self.client.call("query", params)

    def test_query_required_fields_and_types(self):
        invalid = {
            "view_account": {"amount": ({}, 1, True, "", "-1", "1.0", " 1", "\u0661"),
                             "locked": ({}, 0, "-1"), "storage_usage": (True, -1, "1"), "code_hash": (None, 1)},
            "view_access_key": {"nonce": (True, -1, "1"),
                                "permission": ({"FunctionCall": None}, "Other", {"Other": {}}, {})},
            "view_access_key_list": {"keys": ({}, None, [None], [{}],
                                              [{"public_key": 1, "access_key": self.access}],
                                              [{"public_key": "ed25519:" + rpc.EMPTY_CODE_HASH, "access_key": {"nonce": 1}}])},
            "view_code": {"code_base64": (None, [], "!"), "hash": (None, 1)},
            "call_function": {"result": (None, {}, [True], [256], list(b"not JSON")), "logs": (None, {}, [1])},
        }
        for kind, template in self.results.items():
            result = {**template, **self.context}
            self.assertEqual(self.read(kind, result), result)
            for field, values in {**invalid[kind], "block_height": (True, -1, "7"),
                                  "block_hash": (None, 7)}.items():
                missing = dict(result)
                del missing[field]
                for malformed in (missing, *({**result, field: value} for value in values)):
                    with self.subTest(kind=kind, field=field, value=malformed.get(field)):
                        with self.assertRaises(rpc.MalformedResponse):
                            self.read(kind, malformed)

    def test_function_call_permissions_validate_each_required_field(self):
        valid = self.permission["FunctionCall"]
        for allowance in (None, "0", "123"):
            access = {**self.context, "nonce": 1,
                      "permission": {"FunctionCall": {**valid, "allowance": allowance}}}
            self.assertEqual(self.read("view_access_key", access), access)
        self.read("view_access_key", {**self.context, "nonce": 0, "permission": "FullAccess"})
        for field, values in {"allowance": (True, 1, {}, "-1", ""), "receiver_id": (None, 1),
                              "method_names": (None, "method", [1])}.items():
            missing = dict(valid)
            del missing[field]
            for permission in (missing, *({**valid, field: value} for value in values)):
                for kind in ("view_access_key", "view_access_key_list"):
                    access = {"nonce": 1, "permission": {"FunctionCall": permission}}
                    result = (access if kind == "view_access_key" else
                              {"keys": [{"public_key": "ed25519:" + rpc.EMPTY_CODE_HASH, "access_key": access}]})
                    with self.subTest(kind=kind, field=field, permission=permission):
                        with self.assertRaises(rpc.MalformedResponse):
                            self.read(kind, {**result, **self.context})
        with self.assertRaises(rpc.MalformedResponse):
            self.read("view_access_key", {**self.context, "nonce": 1,
                      "permission": {**self.permission, "FullAccess": {}}})

    def test_contract_metadata_shape_is_required(self):
        invalid = [None, [], "metadata", {}, {"version": "1"}]
        for field, values in {"version": (None, 1), "link": (1, {}),
                              "standards": (None, {}, [None], [{}], [{"standard": 1, "version": "1"}])}.items():
            missing = dict(self.metadata)
            del missing[field]
            invalid.extend([missing, *({**self.metadata, field: value} for value in values)])
        for value in invalid:
            result = {**self.context, "logs": [], "result": list(json.dumps(value).encode("utf-8"))}
            with self.subTest(value=value), self.assertRaises(rpc.MalformedResponse):
                self.read("call_function", result)

    def test_storage_and_token_metadata_payload_shapes(self):
        metadata = {"name": "Token", "symbol": "T", "decimals": 6}
        for method, valid, invalid in (
                ("storage_balance_of", [None, {"total": "1", "available": "0"}],
                 [{}, False, 0, [], {"total": "1"}, {"available": "0"},
                  {"total": 1, "available": "0"}, {"total": "1", "available": False},
                  {"total": "-1", "available": "0"}]),
                ("ft_metadata", [metadata], [None, [], {}, {**metadata, "name": 1},
                                             {**metadata, "symbol": None}, {**metadata, "decimals": True},
                                             {**metadata, "decimals": "6"}, {**metadata, "decimals": -1}])):
            for accepted, values in ((True, valid), (False, invalid)):
                for value in values:
                    result = {**self.context, "logs": [], "result": list(json.dumps(value).encode("utf-8"))}
                    with self.subTest(method=method, value=value), \
                            mock.patch.object(self.client, "_exchange", return_value={"jsonrpc": "2.0", "id": "gate", "result": result}):
                        if accepted:
                            self.assertEqual(self.client.call_function("token.near", method, {}), value)
                        else:
                            with self.assertRaises(rpc.MalformedResponse):
                                self.client.call_function("token.near", method, {})

    def test_block_header_requires_fields_and_exact_types(self):
        header = {"height": 7, "timestamp": 1, "hash": rpc.EMPTY_CODE_HASH}
        for field, values in {"height": (True, -1, "7"), "timestamp": (False, -1, "1"),
                              "hash": (None, 7)}.items():
            missing = dict(header)
            del missing[field]
            for value in (missing, *({**header, field: v} for v in values)):
                with self.subTest(field=field, value=value):
                    with mock.patch.object(self.client, "_exchange", return_value={"jsonrpc": "2.0", "id": "gate", "result": {"header": value}}):
                        with self.assertRaises(rpc.MalformedResponse):
                            self.client.call("block", {"finality": "final"})


class StringEncodingTest(unittest.TestCase):
    def setUp(self):
        fixtures = [json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(rpc.FIXTURES_DIR.glob("*.json"))]
        self.account = next(item for item in fixtures
                            if isinstance(item["response"].get("result"), dict)
                            and "amount" in item["response"]["result"])
        self.keys = next(item["response"]["result"]["keys"] for item in fixtures
                         if isinstance(item["response"].get("result"), dict)
                         and item["response"]["result"].get("keys"))
        self.block = next(item["response"]["result"] for item in fixtures
                          if item["request"]["method"] == "block"
                          and "result" in item["response"])
        self.encoded = next(item["request"]["params"]["args_base64"] for item in fixtures
                            if isinstance(item["request"]["params"], dict)
                            and "args_base64" in item["request"]["params"])

    def check_encoding(self, schema, valid, invalid):
        for accepted, values in ((True, valid), (False, invalid)):
            for value in values:
                with self.subTest(accepted=accepted, value=value):
                    if accepted:
                        rpc._validate_schema(value, schema)
                    else:
                        with self.assertRaises(rpc.MalformedResponse):
                            rpc._validate_schema(value, schema)

    def test_hash_encodings(self):
        valid = [self.account["response"]["result"]["block_hash"],
                 self.account["response"]["result"]["code_hash"], self.block["header"]["hash"],
                 rpc.EMPTY_CODE_HASH]
        invalid = ["!", "", "0" * 32, "1" * 31, "1" * 33, " " + valid[0], valid[0] + "\n"]
        for schema, field in ((rpc._CONTEXT_SCHEMA, "block_hash"), (rpc._HEADER_SCHEMA, "hash"),
                              (rpc._RESULT_SCHEMAS["view_account"], "code_hash"),
                              (rpc._RESULT_SCHEMAS["view_code"], "hash"),
                              (rpc._RESULT_SCHEMAS["view_account"], "block_hash")):
            with self.subTest(field=field):
                self.check_encoding(schema[field][1], valid, invalid)

    def test_public_key_encodings(self):
        key = self.keys[0]["public_key"]
        schema = rpc._RESULT_SCHEMAS["view_access_key_list"]["keys"][1][0]["public_key"][1]
        valid = [key, key.split(":")[1], "ed25519:" + "1" * 32, "secp256k1:" + "1" * 64]
        invalid = ["!", "", "rsa:" + "1" * 32,
                   "ed25519:!", "ed25519:" + "1" * 31, "ed25519:" + "1" * 33,
                   "secp256k1:" + "1" * 63, "secp256k1:" + "1" * 65, key + "\n"]
        self.check_encoding(schema, valid, invalid)

    def test_account_id_encodings(self):
        account_id = self.account["request"]["params"]["account_id"]
        valid = [account_id, "aa", "a" * 64, "a-b_c.near", "12"]
        invalid = ["", "a", "a" * 65, "Upper.near", "!", "-a", "a-", ".aa", "aa.",
                   "a..b", "a__b", "a-_b", "a._b", "a b", "a/b", "aa\n", "a\u00e9"]
        for schema, field in ((rpc._CONTEXT_SCHEMA, "account_id"),
                              (rpc._CONTEXT_SCHEMA, "requested_account_id"),
                              (rpc._CONTEXT_SCHEMA, "contract_account_id"),
                              (rpc._RESULT_SCHEMAS["view_account"], "global_contract_account_id")):
            with self.subTest(field=field):
                self.check_encoding(schema[field][1], valid, invalid)
        client = rpc.Client("https://rpc.example", "replay")
        response = {"jsonrpc": "2.0", "id": "gate", "result": self.account["response"]["result"]}
        for accepted, values in ((True, valid), (False, invalid)):
            for value in values:
                with self.subTest(request_account=value), mock.patch.object(client, "_exchange", return_value=response):
                    if accepted:
                        self.assertEqual(client.view_account(value), response["result"])
                    else:
                        with self.assertRaises(rpc.MalformedResponse):
                            client.view_account(value)

    def test_base64_encodings(self):
        valid = [self.encoded, "", "AA==", "AAA=", "AAAA"]
        invalid = ["!", "A", "AA", "AAA", "AA===", "AA==\n", " AA==", "\u00e9"]
        self.check_encoding(rpc._RESULT_SCHEMAS["view_code"]["code_base64"][1], valid, invalid)
        reference = base64.b64encode(rpc.base58.decode(self.block["header"]["hash"])).decode("ascii")
        # Base64VecU8 permits an empty or arbitrary length byte vector.
        schema = rpc._METHOD_SCHEMAS["ft_metadata"]["reference_hash"][1]
        self.check_encoding(schema, [None, reference, *valid], invalid + [reference + "="])

    def test_decimal_amount_encodings(self):
        account = self.account["response"]["result"]
        valid = [account["amount"], account["locked"], "0", str(2**128 - 1), "00"]
        invalid = ["!", "", "-1", "+1", "1.0", " 1", "1\n", "\u0661", str(2**128), "9" * 100]
        self.check_encoding(rpc._balance, valid, invalid)

    def test_timestamp_decimal_encodings(self):
        valid = [str(self.block["header"]["timestamp"]), "0", str(2**64 - 1)]
        invalid = ["!", "", "-1", "+1", "1.0", " 1", "1\n", "\u0661", str(2**64), "9" * 100]
        self.check_encoding(rpc._HEADER_SCHEMA["timestamp_nanosec"][1], valid, invalid)

    def test_free_text_encodings(self):
        metadata = rpc._METHOD_SCHEMAS["contract_source_metadata"]
        token = rpc._METHOD_SCHEMAS["ft_metadata"]
        fields = [(rpc._ERROR_SCHEMA, "message"), (rpc._ERROR_SCHEMA, "data"),
                  (rpc._ERROR_INFO_SCHEMA, "vm_error"), (rpc._ERROR_INFO_SCHEMA, "error_message"),
                  (rpc._ERROR_INFO_SCHEMA, "method_name"), (metadata, "version"), (metadata, "link"),
                  (metadata["standards"][1][0], "standard"), (metadata["standards"][1][0], "version")]
        fields.extend((token, field) for field in ("name", "symbol", "spec", "icon", "reference"))
        # AccessKeyPermissionView receiver_id is String, unlike request AccountId fields.
        fields.append((rpc._FUNCTION_PERMISSION_SCHEMA, "receiver_id"))
        valid = [self.account["response"]["jsonrpc"], "", "\u00e9", "\U0001f600", "!", "a/b", "Upper.near"]
        invalid = ["\ud800", "\udfff", "a\ud800z"]
        for schema, field in fields:
            with self.subTest(field=field):
                self.check_encoding(schema[field][1], valid, invalid)


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
