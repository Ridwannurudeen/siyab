"""The OutLayer approver adapter over synthetic ApprovalDetail fixtures, judged offline over recorded replays."""

import ast
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import outlayer
from intel import labels
from tests.test_http import POLICY
from tests.test_verdict import no_network

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "gate" / "outlayer.py"
FIXTURE = ROOT / "fixtures" / "outlayer" / "approvals.json"
APPROVALS = json.loads(FIXTURE.read_text(encoding="utf-8"))["approvals"]
ENVIRON = {"GATE_FIXTURES_MODE": "replay", "GATE_ACCOUNT_ID": "gate.near"}


class AdapterCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.network = mock.patch("urllib.request.urlopen", side_effect=no_network)
        cls.network.start()
        cls.environ = mock.patch.dict(os.environ, ENVIRON)
        cls.environ.start()
        store = labels.load()
        cls.judge = staticmethod(outlayer.make_judge(
            {"gate": "gate.near", "policy": POLICY, "reports_account": None}, Path(cls.tmp.name), store))
        cls.disabled_judge = staticmethod(outlayer.make_judge(
            {"gate": "gate.near", "policy": {**POLICY, "enabled": False}, "reports_account": None},
            Path(cls.tmp.name), store))

    @classmethod
    def tearDownClass(cls):
        cls.environ.stop()
        cls.network.stop()
        cls.tmp.cleanup()

    def vote(self, name, judge=None):
        return outlayer.vote(copy.deepcopy(APPROVALS[name]), judge or self.judge)


class VoteTest(AdapterCase):
    def test_the_fixture_is_marked_synthetic(self):
        self.assertIn("Not recorded from OutLayer", json.loads(FIXTURE.read_text(encoding="utf-8"))["synthetic"])

    def test_sign_approves_with_the_unsigned_vote_message(self):
        result = self.vote("ft_allow")
        self.assertEqual((result["judgement"]["outcome"], result["judgement"]["action"]), ("ALLOW", "sign"))
        self.assertEqual(result["vote"], "approve")
        self.assertEqual(result["message"], "approve:00000000-0000-4000-8000-000000000001:"
                                            "ed25519:synthetic-wallet-pubkey:synthetic-request-hash-1")
        self.assertIsNone(result["judgement"]["verdict"])
        self.assertIsNone(result["judgement"]["message"])
        self.assertEqual(result["judgement"]["request"], {"kind": "ft", "token_id": "wrap.near",
                                                          "receiver_id": "intents.near", "amount": "1000",
                                                          "memo": "invoice 7"})

    def test_refuse_rejects_with_the_reject_message(self):
        result = self.vote("ft_deny")
        self.assertEqual((result["judgement"]["outcome"], result["judgement"]["action"]), ("DENY", "refuse"))
        self.assertEqual(result["vote"], "reject")
        self.assertEqual(result["message"], "reject:00000000-0000-4000-8000-000000000002:"
                                            "ed25519:synthetic-wallet-pubkey:synthetic-request-hash-2")

    def test_unknown_abstains_and_leaves_the_approval_to_a_human(self):
        for name in ("near_unknown", "near_alias_unknown"):
            with self.subTest(fixture=name):
                result = self.vote(name)
                self.assertEqual((result["judgement"]["outcome"], result["judgement"]["action"]),
                                 ("UNKNOWN", "ask"))
                self.assertEqual(result["judgement"]["request"]["kind"], "near")
                self.assertEqual(result["judgement"]["request"]["receiver_id"], "relay.aurora")
                self.assertEqual((result["vote"], result["message"]), ("abstain", None))
                self.assertIn("a human decides", result["reason"])

    def test_an_allow_with_action_ask_abstains(self):
        result = self.vote("ft_allow", self.disabled_judge)
        self.assertEqual((result["judgement"]["outcome"], result["judgement"]["action"]), ("ALLOW", "ask"))
        self.assertTrue(result["judgement"]["ask"][0].endswith("(REFUSED:agent_disabled)"))
        self.assertEqual((result["vote"], result["message"]), ("abstain", None))

    def test_operations_it_does_not_judge_abstain_without_a_verdict(self):
        judge = mock.Mock(side_effect=AssertionError("judged"))
        for name, words in (("withdraw", "request_type 'withdraw'"), ("other_method", "storage_deposit"),
                            ("ft_large_deposit", "deposit other than 1 yoctoNEAR"), ("ft_extra_arg", "msg"),
                            ("approved", "'approved', not pending")):
            with self.subTest(fixture=name):
                result = self.vote(name, judge)
                self.assertEqual((result["vote"], result["message"], result["judgement"]), ("abstain", None, None))
                self.assertIn(words, result["reason"])
        judge.assert_not_called()

    def test_a_transfer_with_no_canonical_form_abstains(self):
        detail = copy.deepcopy(APPROVALS["near_unknown"])
        detail["request_data"]["amount"] = "five"
        result = outlayer.vote(detail, self.judge)
        self.assertEqual((result["vote"], result["judgement"]), ("abstain", None))
        self.assertIn("no canonical form", result["reason"])


class MalformedTest(unittest.TestCase):
    def test_a_malformed_approval_raises_and_is_never_judged(self):
        judge = mock.Mock(side_effect=AssertionError("judged"))
        good = APPROVALS["ft_allow"]
        cases = [None, [], "approve", {key: value for key, value in good.items() if key != "request_hash"},
                 {**good, "wallet_pubkey": ""}, {**good, "id": 7}]
        for detail in cases:
            with self.subTest(detail=detail if not isinstance(detail, dict) else sorted(detail)):
                with self.assertRaises(outlayer.ApprovalError):
                    outlayer.vote(detail, judge)
        for data in ("not an object", {"to": "a.near", "receiver_id": "a.near", "amount": "1"}, {"amount": "1"}):
            with self.subTest(request_data=data):
                result = outlayer.vote({**good, "request_type": "transfer", "request_data": data}, judge)
                self.assertEqual((result["vote"], result["judgement"]), ("abstain", None))
        judge.assert_not_called()


class NoSigningTest(unittest.TestCase):
    def test_the_module_signs_nothing_and_opens_no_connection(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        for name in ("gate.signer", "urllib", "urllib.request", "http.client", "socket"):
            with self.subTest(module=name):
                self.assertNotIn(name, imported)
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and ast.unparse(node.func) == "verdict.request"]
        self.assertEqual(len(calls), 1)
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
        self.assertEqual((keywords["sign_actions"], keywords["signer"]), ("()", "None"))


if __name__ == "__main__":
    unittest.main()
