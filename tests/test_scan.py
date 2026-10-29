import base64
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import base58, ed25519, rpc, scan
from intel import labels

GATE = "gate-hygiene.testnet"
LIVE_KEY = ed25519.public_from_seed(hashlib.sha256(b"scan test live verifier").digest())
TEST_KEY_TEXT = (Path(__file__).resolve().parent.parent / "fixtures" / "keys" / "test-verifier.pub").read_text(
    encoding="utf-8").split()[0]


def key(name):
    return base58.encode_key(hashlib.sha256(name.encode()).digest())


def call(receiver, methods, allowance):
    return {"FunctionCall": {"allowance": allowance, "receiver_id": receiver, "method_names": methods}}


KEYS = [
    (key("full"), "FullAccess"),
    (key("any method"), call("token.example.testnet", [], "250000000000000000000000")),
    (key("dead"), call("token.example.testnet", ["ft_transfer"], "0")),
    (key("unlimited"), call("token.example.testnet", ["ft_balance_of"], None)),
    (key("agent"), call(GATE, ["request_transfer"], "1000000000000000000000000")),
    (key("agent with a callback"), call(GATE, ["request_transfer", "on_transfer"], "1000000000000000000000000")),
]


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


class ScanTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch("urllib.request.urlopen", side_effect=no_network)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = labels.load()

    def record(self, account, request_type, result, **extra):
        params = {"request_type": request_type, "finality": "final", "account_id": account, **extra}
        response = {"jsonrpc": "2.0", "id": "gate", "result": result}
        fixture = {"request": {"method": "query", "params": params}, "response": response,
                   "url": rpc.TESTNET_RPC_URL, "fetched_at": "2026-11-04T10:00:00Z"}
        path = self.dir / f"{rpc.fixture_key(rpc.TESTNET_RPC_URL, 'query', params)}.json"
        path.write_text(json.dumps(fixture), encoding="utf-8")

    def record_gate(self, keys=KEYS, verifier=None):
        rows = [{"public_key": public, "access_key": {"nonce": 1, "permission": permission}}
                for public, permission in keys]
        self.record(GATE, "view_access_key_list", {"keys": rows, "block_height": 230000100, "block_hash": "x"})
        if verifier is not None:
            value = list(json.dumps(verifier).encode())
            self.record(GATE, "call_function", {"result": value, "logs": [], "block_height": 230000100},
                        method_name="get_verifier_keys", args_base64=base64.b64encode(b"{}").decode())

    def scan(self, account=GATE):
        client = rpc.Client(rpc.TESTNET_RPC_URL, "replay", fixtures_dir=self.dir)
        return scan.scan(account, client=client, store=self.store)

    def assert_heading_order(self, lines):
        headings = [line.split(":", 1)[0] for line in lines if not line.startswith("  ")]
        self.assertEqual(headings, list(scan.HEADINGS))

    def test_the_key_rules(self):
        self.record_gate(verifier={"current": LIVE_KEY.hex(), "previous": None})
        result = self.scan()
        lines, keys = result["lines"], result["findings"]["keys"]
        self.assert_heading_order(lines)
        self.assertEqual(lines[0], f"account: {GATE} on {rpc.TESTNET_RPC_URL}, read at block 230000100")
        self.assertEqual(lines[1], "access keys: 6")
        self.assertEqual(keys["full_access"], [key("full")])
        self.assertEqual(keys["empty_method_names"], [key("any method")])
        self.assertEqual(keys["dead"], [key("dead")])
        self.assertEqual(keys["unlimited"], [key("unlimited")])
        self.assertEqual(keys["agent_keys"], [key("agent"), key("agent with a callback")])
        self.assertEqual(keys["agent_keys_not_request_transfer"], [key("agent with a callback")])
        index = lines.index('dead keys: 1, allowance "0"')
        self.assertEqual(lines[index + 1], f"  {key('dead')}")
        self.assertIn("empty method_names: 1, these may call any method on their receiver", lines)
        self.assertIn("unlimited allowance: 1, allowance null", lines)
        index = lines.index(f"agent keys: 2 scoped to {GATE}, 1 with method_names other than exactly "
                            '["request_transfer"]')
        self.assertEqual(lines[index + 1], f"  {key('agent with a callback')}")

    def test_the_verifier_key_is_labelled_different_from_the_test_key(self):
        self.record_gate(verifier={"current": LIVE_KEY.hex(), "previous": None})
        lines = self.scan()["lines"]
        index = lines.index(f"verifier keys: the TEST key is {TEST_KEY_TEXT}")
        self.assertEqual(lines[index + 1], f"  on chain current: {base58.encode_key(LIVE_KEY)}, "
                                           "different from the TEST key")
        self.assertNotIn("SAME", "".join(lines))

    def test_a_verifier_key_equal_to_the_test_key_says_same_in_capitals(self):
        test_hex = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED).hex()
        self.record_gate(verifier={"current": LIVE_KEY.hex(),
                                   "previous": {"key": test_hex, "valid_until_ms": 1793534400000}})
        lines = self.scan()["lines"]
        self.assertIn(f"  on chain previous until 1793534400000 ms: {TEST_KEY_TEXT}, SAME as the TEST key", lines)

    def test_an_account_that_is_not_a_gate_has_no_agent_key_rule(self):
        self.record_gate()
        lines = self.scan()["lines"]
        self.assert_heading_order(lines)
        self.assertIn(f"agent keys: unknown, {GATE} did not answer get_verifier_keys, so it is not read as a Gate",
                      lines)
        self.assertIn("verifier keys: unknown, get_verifier_keys could not run: no recorded response for the request",
                      lines)

    def test_a_malformed_verifier_answer_is_unknown(self):
        self.record_gate(verifier={"current": "not hex", "previous": None})
        self.assertTrue(self.scan()["findings"]["verifier_error"].startswith("MalformedResponse"))

    def test_the_recorded_mainnet_example_is_unknown_not_clean(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(gate_main.main(["scan", "near"]), 0)
        lines = output.getvalue().splitlines()
        self.assert_heading_order(lines)
        self.assertEqual(lines[:3], [f"account: near on {rpc.MAINNET_RPC_URL}",
                                     ("access keys: unknown, view_access_key_list could not run: RPC error "
                                      "TOO_MANY_ACCESS_KEYS"),
                                     "full access keys: unknown"])
        self.assertEqual(lines[3:7], ["empty method_names: unknown", "dead keys: unknown",
                                      "unlimited allowance: unknown", "agent keys: unknown"])

    def test_labels_from_the_store(self):
        lines = self.scan("intents.near")["lines"]
        rows = self.store.by_subject("intents.near", "near")
        self.assertIn(f"labels: {len(rows)}", lines)
        for row in rows:
            self.assertIn(f"  {json.dumps(row.label)} from the label store, category {row.category}, provenance "
                          f"{row.provenance}, assertion {row.id[:12]}", lines)


if __name__ == "__main__":
    unittest.main()
