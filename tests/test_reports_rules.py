import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import ed25519, rpc, signer, verdict

RPC_URL = "https://rpc.example"
NEARBLOCKS_URL = "https://nearblocks.example"
REPORTS_ACCOUNT = "reports.testnet"
RECEIVER = "vendor.testnet"
NOW_MS = 1800000000000
AGENT = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex()


class Fixtures:
    def __init__(self, directory):
        self.directory = Path(directory)

    def write(self, url, method, params, response):
        key = rpc.fixture_key(url, method, params)
        fixture = {"request": {"method": method, "params": params}, "response": response, "url": url,
                   "fetched_at": "2026-11-01T00:00:00Z"}
        (self.directory / f"{key}.json").write_text(json.dumps(fixture), encoding="utf-8")

    def account(self, account_id):
        params = {"request_type": "view_account", "finality": "final", "account_id": account_id}
        response = {"amount": "1000", "locked": "0", "code_hash": rpc.EMPTY_CODE_HASH,
                    "storage_usage": 182, "storage_paid_at": 0, "block_height": 100, "block_hash": "h"}
        self.write(RPC_URL, "query", params, {"jsonrpc": "2.0", "id": "gate", "result": response})

    def _function_params(self, contract, method, args):
        encoded = json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
        return {"request_type": "call_function", "finality": "final", "account_id": contract,
                "method_name": method, "args_base64": base64.b64encode(encoded).decode()}

    def function(self, contract, method, args, value):
        result = {"result": list(json.dumps(value).encode()), "logs": [], "block_height": 100,
                  "block_hash": "h"}
        self.write(RPC_URL, "query", self._function_params(contract, method, args),
                   {"jsonrpc": "2.0", "id": "gate", "result": result})

    def function_error(self, contract, method, args):
        error = {"name": "HANDLER_ERROR", "cause": {"name": "CONTRACT_EXECUTION_ERROR",
                  "info": {"vm_error": "GuestPanic"}}, "code": -32000}
        self.write(RPC_URL, "query", self._function_params(contract, method, args),
                   {"jsonrpc": "2.0", "id": "gate", "error": error})

    def tier2(self, receiver):
        keys = {"keys": [{"created": {"transaction_hash": "creation", "block_timestamp": 100000000000}}]}
        self.write(f"{NEARBLOCKS_URL}/v1/account/{receiver}/keys", "GET", None, keys)
        txn = {"txns": [{"actions_agg": {"deposit": 5 * 10**23}}]}
        self.write(f"{NEARBLOCKS_URL}/v1/txns/creation", "GET", None, txn)


class Store:
    assertions = []
    sha256 = "test-label-store"

    def by_subject(self, subject, chain):
        return []


class ReportsRulesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.fixtures = Fixtures(self.directory)
        patcher = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, summary, *, allowlisted=True, reports_enabled=True, read_error=False):
        self.fixtures.account(RECEIVER)
        if reports_enabled:
            if read_error:
                self.fixtures.function_error(REPORTS_ACCOUNT, "subject_summary", {"subject": RECEIVER})
            else:
                self.fixtures.function(REPORTS_ACCOUNT, "subject_summary", {"subject": RECEIVER}, summary)
        if not allowlisted:
            self.fixtures.tier2(RECEIVER)
        signer_spy = mock.MagicMock(key_id="cur")
        signer_spy.sign.return_value = bytes(64)
        policy = {"allowlist": [RECEIVER] if allowlisted else [], "caps": []}
        environment = {"REPORTS_ACCOUNT_ID": REPORTS_ACCOUNT} if reports_enabled else {}
        with mock.patch.dict(os.environ, environment, clear=True):
            result = verdict.request(
                "near", None, RECEIVER, "1", None, gate="gate.testnet", agent_pk_hex=AGENT,
                policy=policy, store=Store(), rpc=rpc.Client(RPC_URL, "replay", fixtures_dir=self.directory),
                nearblocks=rpc.Client(NEARBLOCKS_URL, "replay", kind="http", fixtures_dir=self.directory),
                signer=signer_spy, evidence_dir=self.directory, clock=lambda: NOW_MS / 1000)
        return result, signer_spy

    def assert_unsigned(self, result, verdict_signer, outcome):
        self.assertEqual(result["outcome"], outcome)
        self.assertIsNone(result["verdict"])
        self.assertIsNone(result["message"])
        verdict_signer.sign.assert_not_called()
        document = json.loads(Path(result["evidence_path"]).read_bytes())
        self.assertEqual(document["verdict"], outcome)
        self.assertEqual([document[field] for field in ("issued_ms", "expires_ms", "verdict_id")],
                         [None, None, None])

    def test_theft_then_social_is_denied_and_unsigned(self):
        result, verdict_signer = self.request({"accepted": 2, "pending": 0, "contested": 0,
                                               "latest_accepted_id": 19, "deny": True})
        self.assert_unsigned(result, verdict_signer, "DENY")
        check = next(check for check in result["checks"] if check["id"] == "community_report")
        self.assertEqual(check["status"], "signal")
        self.assertIn("deny report", check["detail"])
        self.assertIn("report id is 19", check["detail"])

    def test_contested_deny_is_denied_with_or_without_allowlist(self):
        summary = {"accepted": 1, "pending": 0, "contested": 1, "latest_accepted_id": 18, "deny": True}
        for allowlisted in (True, False):
            with self.subTest(allowlisted=allowlisted):
                result, verdict_signer = self.request(summary, allowlisted=allowlisted)
                self.assert_unsigned(result, verdict_signer, "DENY")

    def test_social_only_is_unknown_and_unsigned(self):
        result, verdict_signer = self.request({"accepted": 1, "pending": 0, "contested": 0,
                                               "latest_accepted_id": 19, "deny": False})
        self.assert_unsigned(result, verdict_signer, "UNKNOWN")

    def test_pending_report_is_unknown_off_allowlist(self):
        result, verdict_signer = self.request({"accepted": 0, "pending": 1, "contested": 0,
                                               "latest_accepted_id": None, "deny": False},
                                              allowlisted=False)
        self.assert_unsigned(result, verdict_signer, "UNKNOWN")
        checks = {check["id"]: check for check in result["checks"]}
        self.assertEqual(checks["community_report"]["status"], "unknown")
        self.assertEqual(checks["pending_report"]["status"], "unknown")
        self.assertEqual(checks["pending_report"]["source"]["provider"], "rpc.example")

    def test_pending_report_does_not_block_allowlisted_payee(self):
        result, _ = self.request({"accepted": 0, "pending": 1, "contested": 0,
                                  "latest_accepted_id": None, "deny": False})
        self.assertEqual(result["outcome"], "ALLOW")
        self.assertEqual(next(check for check in result["checks"] if check["id"] == "community_report")["status"],
                         "pass")
        self.assertNotIn("pending_report", [check["id"] for check in result["checks"]])

    def test_overturned_only_summary_allows(self):
        result, _ = self.request({"accepted": 0, "pending": 0, "contested": 0,
                                  "latest_accepted_id": None, "deny": False})
        self.assertEqual(result["outcome"], "ALLOW")

    def test_unreadable_summary_is_unknown_and_unsigned(self):
        empty = {"accepted": 0, "pending": 0, "contested": 0, "latest_accepted_id": None, "deny": False}
        summaries = (
            None,
            {key: value for key, value in empty.items() if key != "deny"},
            {**empty, "deny": "false"},
            {**empty, "deny": True},
            {**empty, "extra": 0},
            {"accepted": 1, "pending": 0, "contested": 0,
             "latest_accepted_id": 17, "latest_accepted_kind": "theft"},
            {**empty, "accepted": -1},
            {**empty, "pending": True},
            {**empty, "contested": 1},
            {**empty, "accepted": 1, "latest_accepted_id": None},
        )
        for summary in summaries:
            with self.subTest(summary=summary):
                result, verdict_signer = self.request(summary)
                self.assert_unsigned(result, verdict_signer, "UNKNOWN")

    def test_failed_summary_read_is_unknown_and_unsigned(self):
        result, verdict_signer = self.request(None, read_error=True)
        self.assert_unsigned(result, verdict_signer, "UNKNOWN")

    def test_unset_reports_account_preserves_the_existing_checks(self):
        result, _ = self.request(None, reports_enabled=False)
        self.assertEqual(result["outcome"], "ALLOW")
        self.assertEqual([check["id"] for check in result["checks"]],
                         ["receiver_exists", "label_denylist", "memo_injection"])


if __name__ == "__main__":
    unittest.main()
