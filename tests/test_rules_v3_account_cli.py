import json
import tempfile
from pathlib import Path
from unittest import mock

from gate import account_check, agent, demo, ed25519, signer, verdict
from tests.test_rules import CODE_HASH, DAY_NS, NOW_MS, RulesCase, request


class YoungReceiverConsumersTest(RulesCase):
    RECEIVER = "young.near"

    def setUp(self):
        super().setUp()
        self.stranger(self.RECEIVER, age_ns=2 * DAY_NS)
        self.policy = {"allowlist": [], "caps": [{"asset": "Near"}]}
        self.transfer = request(self.RECEIVER, kind="near", memo=None)

    def account(self, version):
        return account_check.check_account(
            self.RECEIVER, store=self.store, rpc=self.rpc, nearblocks=self.nearblocks,
            archival=None, issuers=None, now_ms=NOW_MS, rules_version=version)

    def service(self, version=None):
        options = {} if version is None else {"rules_version": version}
        with tempfile.TemporaryDirectory() as directory:
            return verdict.request(
                "near", None, self.RECEIVER, self.transfer["amount"], None,
                gate="gate.near", agent_pk_hex=ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex(),
                policy=self.policy, store=self.store, rpc=self.rpc, nearblocks=self.nearblocks,
                signer=signer.TestSigner(), evidence_dir=directory, clock=lambda: NOW_MS / 1000, **options)

    def test_account_young_alone_is_ask_with_reason_and_allowlist(self):
        answer = self.account(3)
        self.assertEqual((answer["outcome"], answer["action"]), ("ASK", "ask"))
        self.assertIn("The owner should confirm the receiver before paying.", answer["reasons"][0])
        self.assertIn("Not on the owner's allowlist.", answer["reasons"])
        document = json.loads(answer["evidence"])
        self.assertEqual(document["verdict"], "UNKNOWN")
        self.assertEqual({c["id"] for c in document["checks"] if c["status"] == "signal"}, {"account_age"})

    def test_account_keeps_unknown_code_details(self):
        self.fx.account(self.RECEIVER, CODE_HASH)
        answer = self.account(3)
        self.assertEqual(answer["outcome"], "ASK")
        self.assertIn("confirm the receiver", answer["reasons"][0])
        self.assertTrue(any("contract_control" == c["name"] and c["status"] == "unknown"
                            for c in answer["checks"]))
        self.assertTrue(any("could not read the access key list" in reason for reason in answer["reasons"]))

    def test_extra_incident_signal_still_denies(self):
        self.fx.account(self.RECEIVER, CODE_HASH)
        with mock.patch.object(account_check, "_incident_code_hashes", return_value=({CODE_HASH: "recorded"}, [])):
            answer = self.account(3)
        self.assertEqual(answer["outcome"], "DENY")
        self.assertTrue(any("incident page recorded reports" in reason for reason in answer["reasons"]))
        self.assertFalse(any("confirm the receiver" in reason for reason in answer["reasons"]))

    def test_versions_one_and_two_still_deny_young(self):
        for version in (1, 2):
            with self.subTest(version=version):
                answer = self.account(version)
                self.assertEqual(answer["outcome"], "DENY")
                self.assertEqual(answer["reasons"], ["Created 2 d ago.", "Not on the owner's allowlist."])

    def test_verdict_young_alone_asks_owner_and_never_signs(self):
        result = self.service(3)
        self.assertEqual((result["outcome"], result["action"]), ("UNKNOWN", "ask"))
        self.assertEqual(result["ask"], [result["reasons"][0]])
        self.assertIn("confirm the receiver", result["ask"][0])
        self.assertIsNone(result["verdict"])
        self.assertIsNone(result["message"])

    def test_verdict_keeps_missing_checks_and_policy_after_young_reason(self):
        self.fx.account(self.RECEIVER, CODE_HASH)
        self.policy["gate_paused"] = True
        result = self.service(3)
        self.assertIn("confirm the receiver", result["ask"][0])
        self.assertTrue(result["ask"][1].startswith("code_without_metadata could not run:"))
        self.assertTrue(result["ask"][-1].endswith("(REFUSED:paused)"))

    def test_default_verdict_matches_version_one_and_two(self):
        results = [self.service(version) for version in (None, 1, 2)]
        for result in results:
            self.assertEqual((result["outcome"], result["action"], result["ask"]), ("DENY", "refuse", []))
            self.assertEqual(result["reasons"], ["Created 2 d ago.", "Not on the owner's allowlist."])
        self.assertEqual(results[0]["checks"], results[1]["checks"])

    def test_agent_prints_young_reason_without_sending(self):
        result = self.service(3)
        lines = []
        with tempfile.TemporaryDirectory() as directory:
            context = agent.Agent("gate.testnet", "00" * 32, "unused", mock.Mock(),
                                  (None, None, None, None), signer.TestSigner, Path(directory))
            with (mock.patch.object(agent, "read_policy", return_value=self.policy),
                  mock.patch.object(agent, "_judge", return_value=result)):
                self.assertEqual(agent.pay(context, self.transfer, out=lines.append), 3)
            context.transport.assert_not_called()
        self.assertIn("ask a human: " + result["ask"][0], lines)
        self.assertFalse(any("0 checks could not run" in line for line in lines))

    def test_demo_prints_young_reason_without_empty_cause(self):
        result = self.service(3)
        lines = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(verdict, "request", return_value=result):
            demo._run("Young receiver", self.RECEIVER, None, (None, None, None), self.policy,
                      self.store, Path(directory), lambda: NOW_MS / 1000, lines.append)
        self.assertIn("  UNKNOWN, not signed: " + " ".join(result["ask"]), lines)
        self.assertNotIn("  UNKNOWN, not signed:  could not run", lines)

    def test_cli_young_reason_keeps_actual_missing_checks(self):
        self.fx.account(self.RECEIVER, CODE_HASH)
        result = self.service(3)
        lines = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(verdict, "request", return_value=result):
            demo._run("Young receiver", self.RECEIVER, None, (None, None, None), self.policy,
                      self.store, Path(directory), lambda: NOW_MS / 1000, lines.append)
        self.assertTrue(any("confirm the receiver" in line and "code_without_metadata could not run" in line
                            for line in lines))

    def test_agent_keeps_actual_missing_checks_after_young_reason(self):
        self.fx.account(self.RECEIVER, CODE_HASH)
        result = self.service(3)
        lines = []
        with tempfile.TemporaryDirectory() as directory:
            context = agent.Agent("gate.testnet", "00" * 32, "unused", mock.Mock(),
                                  (None, None, None, None), signer.TestSigner, Path(directory))
            with (mock.patch.object(agent, "read_policy", return_value=self.policy),
                  mock.patch.object(agent, "_judge", return_value=result)):
                self.assertEqual(agent.pay(context, self.transfer, out=lines.append), 3)
            context.transport.assert_not_called()
        self.assertIn("ask a human: " + result["ask"][0], lines)
        self.assertIn("ask a human: " + result["ask"][1], lines)
        self.assertIn("code_without_metadata could not run", result["ask"][1])
