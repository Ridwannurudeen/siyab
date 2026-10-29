import hashlib
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import rpc

INTENTS_VIEW_KEY = "aeca5be441be45abec5d698a9b5c3dd2a8b8090db3a2abc222c3e6b6463ccdd0"
USDC = "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1"
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
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

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
        body = {"result": {"header": {"height": 7, "timestamp": 1}}}
        with mock.patch("urllib.request.urlopen", return_value=reply(body)) as opened:
            self.assertEqual(self.client("off").final_block()["header"]["height"], 7)
        self.assertEqual(json.loads(opened.call_args.args[0].data)["params"], {"finality": "final"})

    def test_empty_code_hash(self):
        self.assertEqual(rpc.EMPTY_CODE_HASH, "1" * 32)


if __name__ == "__main__":
    unittest.main()
