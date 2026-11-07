"""The OutLayer approver adapter over synthetic ApprovalDetail fixtures, judged offline over recorded replays."""

import ast
import copy
import json
import os
import re
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import outlayer, rpc
from intel import labels
from tests.test_http import POLICY
from tests.test_verdict import no_network

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "gate" / "outlayer.py"
FIXTURE = ROOT / "fixtures" / "outlayer" / "approvals.json"
APPROVALS = json.loads(FIXTURE.read_text(encoding="utf-8"))["approvals"]
ENVIRON = {"GATE_FIXTURES_MODE": "replay", "GATE_ACCOUNT_ID": "gate.near"}
CONFIG = {"gate": "gate.near", "policy": POLICY, "reports_account": None}
# A closed local port: in replay mode a client pointed here finds no recorded response, as in an outage.
UNREACHABLE = "http://127.0.0.1:9"
RPC_OUTAGE = {"NEAR_MAINNET_RPC_URL": UNREACHABLE, "NEARBLOCKS_API_URL": UNREACHABLE,
              "NEAR_ARCHIVAL_RPC_URL": UNREACHABLE}
SIGN = "ALLOW with action sign"
REFUSE = "DENY with action refuse"
UNKNOWN_ASK = "UNKNOWN with action ask: a human decides on OutLayer's approvals page"
ALLOW_ASK = "ALLOW with action ask: a human decides on OutLayer's approvals page"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


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


class CaseTest(AdapterCase):
    """One test per case in the case table of docs/OUTLAYER.md: the vote the adapter returns and its reason."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        store = labels.load()
        evidence_dir = Path(cls.tmp.name)
        with mock.patch.dict(os.environ, RPC_OUTAGE):
            cls.rpc_outage_judge = staticmethod(outlayer.make_judge(CONFIG, evidence_dir, store))
        with mock.patch.dict(os.environ, {"SHIELDBOT_API_URL": UNREACHABLE}):
            cls.scanner_outage_judge = staticmethod(outlayer.make_judge(CONFIG, evidence_dir, store))
        cls.reports_outage_judge = staticmethod(outlayer.make_judge(
            {**CONFIG, "reports_account": "reports.gate.near"}, evidence_dir, store))
        cls.paused_judge = staticmethod(outlayer.make_judge(
            {**CONFIG, "policy": {**POLICY, "gate_paused": True}}, evidence_dir, store))

    def unjudged(self):
        judge = mock.Mock(side_effect=AssertionError("judged"))
        self.addCleanup(judge.assert_not_called)
        return judge

    def assert_vote(self, name, result, vote, reason):
        approval = APPROVALS[name]
        self.assertEqual((result["approval_id"], result["vote"], result["reason"]), (approval["id"], vote, reason))
        if vote == "abstain":
            self.assertIsNone(result["message"])
        else:
            self.assertEqual(result["message"],
                             f"{vote}:{approval['id']}:{approval['wallet_pubkey']}:{approval['request_hash']}")
            self.assertIsNone(result["judgement"]["verdict"])

    def statuses(self, result):
        return {check["id"]: check["status"] for check in result["judgement"]["checks"]}

    def detail(self, result, check_id):
        return next(check["detail"] for check in result["judgement"]["checks"] if check["id"] == check_id)

    def test_ft_allow_approves(self):
        self.assert_vote("ft_allow", self.vote("ft_allow"), "approve", SIGN)

    def test_near_allow_approves(self):
        result = self.vote("near_allow")
        self.assert_vote("near_allow", result, "approve", SIGN)
        self.assertEqual(result["judgement"]["request"], {"kind": "near", "token_id": None,
                                                          "receiver_id": "intents.near",
                                                          "amount": "1000000000000000000000000", "memo": None})

    def test_ft_gas_allow_approves_with_its_gas_unjudged(self):
        result = self.vote("ft_gas_allow")
        self.assert_vote("ft_gas_allow", result, "approve", SIGN)
        self.assertEqual(result["judgement"]["request"], {"kind": "ft", "token_id": "wrap.near",
                                                          "receiver_id": "intents.near", "amount": "2000",
                                                          "memo": None})

    def test_ft_deny_rejects_a_missing_receiver(self):
        result = self.vote("ft_deny")
        self.assert_vote("ft_deny", result, "reject", REFUSE)
        self.assertEqual(self.statuses(result)["receiver_exists"], "signal")

    def test_near_incident_deny_rejects_an_incident_labelled_receiver(self):
        result = self.vote("near_incident_deny")
        self.assert_vote("near_incident_deny", result, "reject", REFUSE)
        self.assertEqual(self.statuses(result)["label_denylist"], "signal")
        self.assertIn("rhea000453.multica.near carries the incident label", self.detail(result, "label_denylist"))

    def test_ft_unlisted_token_deny_rejects_a_call_for_a_token_off_the_catalog(self):
        result = self.vote("ft_unlisted_token_deny")
        self.assert_vote("ft_unlisted_token_deny", result, "reject", REFUSE)
        self.assertEqual(self.statuses(result)["token_in_catalog"], "signal")
        self.assertIn("token_not_in_catalog: game.hot.tg", self.detail(result, "token_in_catalog"))

    def test_ft_memo_injection_deny_rejects_instruction_shaped_memo_text(self):
        result = self.vote("ft_memo_injection_deny")
        self.assert_vote("ft_memo_injection_deny", result, "reject", REFUSE)
        self.assertEqual(self.statuses(result)["memo_injection"], "signal")

    def test_near_unknown_abstains_when_a_read_is_not_recorded(self):
        result = self.vote("near_unknown")
        self.assert_vote("near_unknown", result, "abstain", UNKNOWN_ASK)
        self.assertTrue(result["judgement"]["ask"][0].startswith("issuer_blacklist could not run"))

    def test_near_alias_unknown_abstains_for_the_same_transfer_named_by_receiver_id(self):
        result = self.vote("near_alias_unknown")
        self.assert_vote("near_alias_unknown", result, "abstain", UNKNOWN_ASK)
        self.assertEqual(result["judgement"]["request"]["receiver_id"], "relay.aurora")

    def test_near_rpc_outage_abstains(self):
        result = self.vote("near_rpc_outage", self.rpc_outage_judge)
        self.assert_vote("near_rpc_outage", result, "abstain", UNKNOWN_ASK)
        self.assertEqual(result["judgement"]["ask"][0],
                         "receiver_exists could not run: could not read intents.near: no recorded response for the "
                         "request")

    def test_ft_scanner_outage_abstains(self):
        result = self.vote("ft_scanner_outage", self.scanner_outage_judge)
        self.assert_vote("ft_scanner_outage", result, "abstain", UNKNOWN_ASK)
        self.assertEqual(result["judgement"]["ask"], [
            "memo_injection could not run: local patterns: no match; ShieldBot injection scan: could not run: "
            "no recorded response for the request"])

    def test_near_reports_outage_abstains(self):
        result = self.vote("near_reports_outage", self.reports_outage_judge)
        self.assert_vote("near_reports_outage", result, "abstain", UNKNOWN_ASK)
        self.assertEqual(result["judgement"]["ask"], [
            "community_report could not run: reports for intents.near: subject_summary could not be read: "
            "no recorded response for the request"])

    def test_near_gate_paused_abstains_on_an_allow_the_gate_would_refuse(self):
        result = self.vote("near_gate_paused", self.paused_judge)
        self.assert_vote("near_gate_paused", result, "abstain", ALLOW_ASK)
        self.assertEqual(result["judgement"]["ask"],
                         ["the Gate is paused, so it refuses every transfer (REFUSED:paused)"])

    def test_changed_amount_under_one_id_is_never_approved(self):
        seen = {}
        first = outlayer.vote_once(copy.deepcopy(APPROVALS["changed_amount_first"]), self.judge, seen)
        self.assert_vote("changed_amount_first", first, "approve", SIGN)
        alone = self.vote("changed_amount_second")
        self.assertEqual((alone["vote"], alone["message"]), ("approve", first["message"]))
        second = outlayer.vote_once(copy.deepcopy(APPROVALS["changed_amount_second"]), self.judge, seen)
        self.assert_vote("changed_amount_second", second, "abstain",
                         "the approval's transfer changed since this approver first saw it, so it casts no vote")
        self.assertEqual((first["judgement"]["request"]["amount"], second["judgement"]["request"]["amount"]),
                         ("1000", "1000000000000000000000000"))
        self.assertEqual(seen[APPROVALS["changed_amount_first"]["id"]]["vote"], "approve")

    def test_replay_is_voted_once(self):
        seen = {}
        first = outlayer.vote_once(copy.deepcopy(APPROVALS["replay"]), self.judge, seen)
        self.assert_vote("replay", first, "approve", SIGN)
        second = outlayer.vote_once(copy.deepcopy(APPROVALS["replay"]), self.judge, seen)
        self.assert_vote("replay", second, "abstain",
                         "this approver already voted approve on the approval, so it casts no vote")

    def test_retry_after_outage_is_voted_once_the_checks_run(self):
        seen = {}
        outage = outlayer.vote_once(copy.deepcopy(APPROVALS["retry_after_outage"]), self.rpc_outage_judge, seen)
        self.assert_vote("retry_after_outage", outage, "abstain", UNKNOWN_ASK)
        retry = outlayer.vote_once(copy.deepcopy(APPROVALS["retry_after_outage"]), self.judge, seen)
        self.assert_vote("retry_after_outage", retry, "approve", SIGN)
        again = outlayer.vote_once(copy.deepcopy(APPROVALS["retry_after_outage"]), self.judge, seen)
        self.assert_vote("retry_after_outage", again, "abstain",
                         "this approver already voted approve on the approval, so it casts no vote")

    def test_stale_reads_approve_because_there_is_no_freshness_bound(self):
        result = self.vote("stale_reads")
        self.assert_vote("stale_reads", result, "approve", SIGN)
        fetched = [datetime.strptime(check["source"]["fetched_at"], TIME_FORMAT).replace(tzinfo=UTC).timestamp()
                   for check in result["judgement"]["checks"] if check["source"]["fetched_at"]]
        self.assertTrue(fetched)
        self.assertTrue(all(time.time() - moment > rpc.MAX_AGE_S for moment in fetched))

    def test_past_expires_at_approves_because_only_status_is_read(self):
        expires_at = datetime.strptime(APPROVALS["past_expires_at"]["expires_at"], TIME_FORMAT).replace(tzinfo=UTC)
        self.assertLess(expires_at.timestamp(), time.time())
        self.assert_vote("past_expires_at", self.vote("past_expires_at"), "approve", SIGN)

    def test_expired_abstains_unjudged(self):
        result = self.vote("expired", self.unjudged())
        self.assert_vote("expired", result, "abstain", "the approval is 'expired', not pending")
        self.assertIsNone(result["judgement"])

    def test_approved_abstains_unjudged(self):
        result = self.vote("approved", self.unjudged())
        self.assert_vote("approved", result, "abstain", "the approval is 'approved', not pending")
        self.assertIsNone(result["judgement"])

    def test_withdraw_abstains_unjudged(self):
        result = self.vote("withdraw", self.unjudged())
        self.assert_vote("withdraw", result, "abstain", "request_type 'withdraw' is not a transfer this adapter judges")
        self.assertIsNone(result["judgement"])

    def test_other_method_abstains_unjudged(self):
        result = self.vote("other_method", self.unjudged())
        self.assert_vote("other_method", result, "abstain", "call to method 'storage_deposit' is not ft_transfer")
        self.assertIsNone(result["judgement"])

    def test_ft_large_deposit_abstains_unjudged(self):
        result = self.vote("ft_large_deposit", self.unjudged())
        self.assert_vote("ft_large_deposit", result, "abstain",
                         "ft_transfer attaches a deposit other than 1 yoctoNEAR, which the verdict does not judge")
        self.assertIsNone(result["judgement"])

    def test_ft_extra_arg_abstains_unjudged(self):
        result = self.vote("ft_extra_arg", self.unjudged())
        self.assert_vote("ft_extra_arg", result, "abstain",
                         "ft_transfer args carry fields this adapter does not judge: msg")
        self.assertIsNone(result["judgement"])

    def test_other_chain_abstains_unjudged(self):
        result = self.vote("other_chain", self.unjudged())
        self.assert_vote("other_chain", result, "abstain", "transfer is on chain 'ethereum', not near")
        self.assertIsNone(result["judgement"])

    def test_amount_leading_zero_abstains_with_no_canonical_form(self):
        result = self.vote("amount_leading_zero")
        self.assert_vote("amount_leading_zero", result, "abstain", "the transfer has no canonical form: amount: "
                         "not a decimal without sign, spaces or leading zeros: '05'")
        self.assertIsNone(result["judgement"])

    def test_malformed_missing_hash_raises_and_is_never_judged(self):
        with self.assertRaisesRegex(outlayer.ApprovalError, "^the approval is missing request_hash$"):
            self.vote("malformed_missing_hash", self.unjudged())

    def test_the_case_table_names_every_fixture(self):
        section = (ROOT / "docs" / "OUTLAYER.md").read_text(encoding="utf-8").split("\n## Cases\n", 1)[1]
        rows = [line.split("|")[2] for line in section.split("\n## ", 1)[0].splitlines()
                if line.startswith("| ") and not line.startswith("| Case |")]
        self.assertEqual(sorted(name for cell in rows for name in re.findall(r"`([a-z_]+)`", cell)),
                         sorted(APPROVALS))


if __name__ == "__main__":
    unittest.main()
