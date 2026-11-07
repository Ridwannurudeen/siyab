import base64
import inspect
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import demo, ed25519, rpc, rules, signer, verdict
from intel import reports

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


class ReportsEffectTest(unittest.TestCase):
    def test_ladder_matrix_returns_the_exact_effect_dict_for_both_allowlist_states(self):
        unreadable = None
        deny = {"accepted": 2, "pending": 3, "contested": 1, "latest_accepted_id": 29,
                "latest_deny_id": 17, "deny": True}
        social = {"accepted": 2, "pending": 4, "contested": 1, "latest_accepted_id": 23,
                  "latest_deny_id": None, "deny": False}
        pending = {"accepted": 0, "pending": 5, "contested": 0, "latest_accepted_id": None,
                   "latest_deny_id": None, "deny": False}
        empty = {"accepted": 0, "pending": 0, "contested": 0, "latest_accepted_id": None,
                 "latest_deny_id": None, "deny": False}
        expected = (
            ("unreadable", None, "the summary is not in the fixed shape; pending unknown"),
            ("deny", 17, "deny report 17 is accepted; 1 under dispute; pending 3"),
            ("ask", 23, "2 accepted social report(s), latest id 23; a human decides; pending 4"),
            ("none", 23, "2 accepted social report(s), latest id 23; the payee is on the allowlist, "
             "so no ask; pending 4"),
            ("none", None, "5 pending report(s) await moderation; no effect"),
            ("none", None, "no community report; pending 0"),
        )
        rows = (
            (unreadable, False, expected[0]), (unreadable, True, expected[0]),
            (deny, False, expected[1]), (deny, True, expected[1]),
            (social, False, expected[2]), (social, True, expected[3]),
            (pending, False, expected[4]), (pending, True, expected[4]),
            (empty, False, expected[5]), (empty, True, expected[5]),
        )
        for summary, allowlisted, (effect, report_id, detail) in rows:
            with self.subTest(effect=effect, allowlisted=allowlisted):
                self.assertEqual(reports.effect(summary, allowlisted),
                                 {"effect": effect, "report_id": report_id, "detail": detail})

    def test_every_unreadable_invariant_has_a_case(self):
        valid = {"accepted": 1, "pending": 0, "contested": 0, "latest_accepted_id": 7,
                 "latest_deny_id": None, "deny": False}
        missing = dict(valid)
        del missing["latest_deny_id"]
        cases = (
            None,
            missing,
            {**valid, "extra": 0},
            {**valid, "accepted": -1},
            {**valid, "pending": True},
            {**valid, "contested": -1},
            {**valid, "accepted": 1.5},
            {**valid, "deny": "false"},
            {**valid, "accepted": 0, "contested": 1, "latest_accepted_id": None},
            {**valid, "latest_accepted_id": None},
            {**valid, "accepted": 0, "latest_accepted_id": 7},
            {**valid, "latest_accepted_id": True},
            {**valid, "latest_accepted_id": -1},
            {**valid, "accepted": 0, "latest_accepted_id": None, "deny": True,
             "latest_deny_id": None},
            {**valid, "latest_deny_id": 8},
            {**valid, "accepted": 0, "latest_accepted_id": None, "deny": True,
             "latest_deny_id": 8},
            {**valid, "deny": True, "latest_deny_id": -1},
            {**valid, "deny": True, "latest_deny_id": True},
        )
        expected = {"effect": "unreadable", "report_id": None,
                    "detail": "the summary is not in the fixed shape; pending unknown"}
        for summary in cases:
            with self.subTest(summary=summary):
                self.assertEqual(reports.effect(summary, False), expected)


class ReportsRulesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.fixtures = Fixtures(self.directory)
        patcher = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, summary, *, allowlisted=True, reports_account=REPORTS_ACCOUNT, read_error=False,
                missing_fixture=False):
        self.fixtures.account(RECEIVER)
        if reports_account is not None and not missing_fixture:
            if read_error:
                self.fixtures.function_error(reports_account, "subject_summary", {"subject": RECEIVER})
            else:
                self.fixtures.function(reports_account, "subject_summary", {"subject": RECEIVER}, summary)
        if not allowlisted:
            self.fixtures.tier2(RECEIVER)
        signer_spy = mock.MagicMock(key_id="cur")
        signer_spy.sign.return_value = bytes(64)
        policy = {"allowlist": [RECEIVER] if allowlisted else [], "caps": []}
        result = verdict.request(
            "near", None, RECEIVER, "1", None, gate="gate.testnet", agent_pk_hex=AGENT,
            policy=policy, store=Store(), rpc=rpc.Client(RPC_URL, "replay", fixtures_dir=self.directory),
            nearblocks=rpc.Client(NEARBLOCKS_URL, "replay", kind="http", fixtures_dir=self.directory),
            signer=signer_spy, evidence_dir=self.directory, clock=lambda: NOW_MS / 1000,
            reports_account=reports_account)
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

    def test_every_effect_outcome_flows_through_the_report_check(self):
        cases = (
            ({"accepted": 1, "pending": 2, "contested": 0, "latest_accepted_id": 19,
              "latest_deny_id": 7, "deny": True}, True, "DENY", "signal",
             "reports for vendor.testnet: deny report 7 is accepted; 0 under dispute; pending 2", None),
            ({"accepted": 1, "pending": 2, "contested": 0, "latest_accepted_id": 19,
              "latest_deny_id": None, "deny": False}, False, "UNKNOWN", "unknown",
             "reports for vendor.testnet: 1 accepted social report(s), latest id 19; a human decides; pending 2",
             "community reports need a human decision"),
            ({"accepted": 0, "pending": 2, "contested": 0, "latest_accepted_id": None,
              "latest_deny_id": None, "deny": False}, False, "ALLOW", "pass",
             "reports for vendor.testnet: 2 pending report(s) await moderation; no effect", None),
            (None, True, "UNKNOWN", "unknown",
             "reports for vendor.testnet: the summary is not in the fixed shape; pending unknown",
             "community reports could not be checked"),
        )
        for summary, allowlisted, outcome, status, detail, reason in cases:
            with self.subTest(outcome=outcome, status=status):
                with mock.patch.object(reports, "effect", wraps=reports.effect) as effect_spy:
                    result, signer_spy = self.request(summary, allowlisted=allowlisted)
                effect_spy.assert_called_once_with(summary, allowlisted)
                check = next(check for check in result["checks"] if check["id"] == "community_report")
                self.assertEqual((result["outcome"], check["status"], check["detail"]),
                                 (outcome, status, detail))
                if reason:
                    self.assertEqual(result["reasons"], [reason])
                if outcome != "ALLOW":
                    self.assert_unsigned(result, signer_spy, outcome)

    def test_a_deny_report_is_the_only_signal_and_names_its_id_and_contract(self):
        summary = {"accepted": 1, "pending": 2, "contested": 0, "latest_accepted_id": 19,
                   "latest_deny_id": 7, "deny": True}
        result, verdict_signer = self.request(summary)
        self.assert_unsigned(result, verdict_signer, "DENY")
        signals = [check["id"] for check in result["checks"] if check["status"] == "signal"]
        self.assertEqual(signals, ["community_report"])
        self.assertEqual(result["reasons"], [
            "The reports contract reports.testnet accepted deny report 7 for vendor.testnet."])

    def test_a_deny_cites_latest_deny_even_when_a_newer_social_report_exists(self):
        summary = {"accepted": 2, "pending": 0, "contested": 1, "latest_accepted_id": 29,
                   "latest_deny_id": 17, "deny": True}
        result, verdict_signer = self.request(summary)
        self.assert_unsigned(result, verdict_signer, "DENY")
        check = next(check for check in result["checks"] if check["id"] == "community_report")
        self.assertEqual(check["status"], "signal")
        self.assertEqual(check["detail"], "reports for vendor.testnet: deny report 17 is accepted; "
                         "1 under dispute; pending 0")

    def test_contested_deny_is_denied_with_or_without_allowlist(self):
        summary = {"accepted": 1, "pending": 0, "contested": 1, "latest_accepted_id": 18,
                   "latest_deny_id": 18, "deny": True}
        for allowlisted in (True, False):
            with self.subTest(allowlisted=allowlisted):
                result, verdict_signer = self.request(summary, allowlisted=allowlisted)
                self.assert_unsigned(result, verdict_signer, "DENY")

    def test_pending_report_is_a_count_only_for_allowlisted_and_other_payees(self):
        summary = {"accepted": 0, "pending": 1, "contested": 0, "latest_accepted_id": None,
                   "latest_deny_id": None, "deny": False}
        for allowlisted in (True, False):
            with self.subTest(allowlisted=allowlisted):
                result, _ = self.request(summary, allowlisted=allowlisted)
                checks = {check["id"]: check for check in result["checks"]}
                self.assertEqual(result["outcome"], "ALLOW")
                self.assertEqual(checks["community_report"]["status"], "pass")
                self.assertEqual(checks["community_report"]["detail"],
                                 "reports for vendor.testnet: 1 pending report(s) await moderation; no effect")
                self.assertNotIn("pending_report", checks)

    def test_unreadable_summary_is_unknown_and_unsigned(self):
        summary = {"accepted": 0, "pending": 0, "contested": 0, "latest_accepted_id": None,
                   "latest_deny_id": None, "deny": False, "extra": 0}
        result, verdict_signer = self.request(summary)
        self.assert_unsigned(result, verdict_signer, "UNKNOWN")
        check = next(check for check in result["checks"] if check["id"] == "community_report")
        self.assertEqual((check["status"], check["detail"]),
                         ("unknown", "reports for vendor.testnet: the summary is not in the fixed shape; "
                          "pending unknown"))

    def test_failed_summary_read_is_unreadable_and_unsigned_without_calling_effect(self):
        with mock.patch.object(reports, "effect", wraps=reports.effect) as effect_spy:
            result, verdict_signer = self.request(None, read_error=True)
        self.assert_unsigned(result, verdict_signer, "UNKNOWN")
        effect_spy.assert_not_called()
        check = next(check for check in result["checks"] if check["id"] == "community_report")
        self.assertEqual((check["status"], check["detail"]),
                         ("unknown", "reports for vendor.testnet: subject_summary could not be read: "
                          "RPC error CONTRACT_EXECUTION_ERROR"))
        self.assertEqual(result["reasons"], ["community reports could not be checked"])

    def test_replay_with_a_missing_reports_fixture_is_unreadable_not_a_crash(self):
        result, verdict_signer = self.request(None, missing_fixture=True)
        self.assert_unsigned(result, verdict_signer, "UNKNOWN")
        check = next(check for check in result["checks"] if check["id"] == "community_report")
        self.assertEqual((check["status"], check["detail"]),
                         ("unknown", "reports for vendor.testnet: subject_summary could not be read: "
                          "no recorded response for the request"))

    def test_unset_reports_account_preserves_the_existing_checks(self):
        result, _ = self.request(None, reports_account=None)
        self.assertEqual(result["outcome"], "ALLOW")
        self.assertEqual([check["id"] for check in result["checks"]],
                         ["receiver_exists", "label_denylist", "memo_injection"])

    def test_judge_source_does_not_read_the_environment(self):
        self.assertNotIn("os.environ", inspect.getsource(rules.judge))

    def test_no_other_module_maps_report_summary_fields_to_effects(self):
        root = Path(__file__).resolve().parents[1]
        summary_field = re.compile(
            r"\b(?:summary|report_summary)\s*\[\s*['\"]"
            r"(?:accepted|pending|contested|latest_accepted_id|latest_deny_id|deny)['\"]\s*\]"
        )
        effect_call = re.compile(r"\breports\.effect\s*\(")
        calls = []
        for directory in ("gate", "intel", "mcp", "scripts"):
            for path in (root / directory).rglob("*.py"):
                relative = path.relative_to(root).as_posix()
                if relative == "intel/reports.py":
                    continue
                source = path.read_text(encoding="utf-8")
                self.assertIsNone(summary_field.search(source), relative)
                calls.extend((relative, match.start()) for match in effect_call.finditer(source))
        self.assertEqual([relative for relative, _ in calls], ["gate/rules.py"])

    def test_demo_keeps_the_default_reports_account_none(self):
        result = {"checks": [], "outcome": "DENY", "action": "refuse", "reasons": [],
                  "evidence_hash": "evidence", "latency_ms": 0}
        with mock.patch("gate.demo.verdict.request", return_value=result) as request_spy:
            demo._run("title", RECEIVER, None, (None, None, None), {}, Store(), self.directory,
                      lambda: NOW_MS / 1000, lambda line: None)
        self.assertNotIn("reports_account", request_spy.call_args.kwargs)
        self.assertIsNone(inspect.signature(verdict.request).parameters["reports_account"].default)


class U64RangeTest(unittest.TestCase):
    def test_counts_and_ids_above_u64_are_unreadable(self):
        from intel import reports
        good = {"accepted": 1, "pending": 0, "contested": 0, "latest_accepted_id": 3, "latest_deny_id": 3, "deny": True}
        self.assertEqual(reports.effect(good, False)["effect"], "deny")
        for field in ("accepted", "pending", "latest_accepted_id", "latest_deny_id"):
            with self.subTest(field=field):
                row = dict(good, **{field: reports.U64_MAX + 1})
                self.assertEqual(reports.effect(row, False)["effect"], "unreadable")
        edge = dict(good, accepted=reports.U64_MAX, latest_accepted_id=reports.U64_MAX, latest_deny_id=reports.U64_MAX)
        self.assertEqual(reports.effect(edge, False)["effect"], "deny")

if __name__ == "__main__":
    unittest.main()
