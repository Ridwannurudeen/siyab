import json
import os
from unittest import mock

from gate import account_check, evidence, rules, verdict
from intel.delivery_checks import UNSIGNED, reply
from mcp import server
from tests.test_rules import DAY_NS, NOW_MS, RulesCase


class RulesVersionThreeMessagesTest(RulesCase):
    def setUp(self):
        super().setUp()
        self.stranger("stranger.near", age_ns=2 * DAY_NS)

    def account(self, version):
        return account_check.check_account("stranger.near", store=self.store, rpc=self.rpc,
                                           nearblocks=self.nearblocks, archival=None, issuers=[],
                                           now_ms=NOW_MS, rules_version=version)

    def mcp(self, answer, mode="off"):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "check_near_account", "arguments": {"account_id": "stranger.near"}}}
        with (mock.patch.dict(server.HANDLERS, {"check_near_account": lambda arguments, directory: answer}),
              mock.patch.dict(os.environ, {"GATE_FIXTURES_MODE": mode})):
            result = server.handle(message, "unused")["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][-1]["text"]), answer)
        return result["content"][0]["text"]

    def test_mcp_young_version_3_asks_with_its_own_headline_and_reason(self):
        answer = self.account(3)
        self.assertEqual(answer["outcome"], "ASK")
        self.assertEqual(self.mcp(answer), "New account, confirm first")
        self.assertEqual(self.mcp(answer, "replay"),
                         "New account, confirm first. Recorded fixtures (replay), block 100; "
                         "this is not a live result.")
        self.assertIn("The owner should confirm the receiver before paying.", answer["reasons"][0])

    def test_delivery_young_version_3_asks_with_its_own_headline_and_reason(self):
        answer = self.account(3)
        self.assertEqual(answer["outcome"], "ASK")
        lines = reply("account", "stranger.near", answer).splitlines()
        self.assertEqual(lines[1], "Result: New account, confirm first")
        self.assertEqual(lines[2], "Reasons: " + "; ".join(answer["reasons"][:3]))
        self.assertEqual(lines[-1], UNSIGNED)

    def test_versions_1_and_2_keep_the_flagged_headline_and_reasons(self):
        for version in (1, 2):
            with self.subTest(version=version):
                answer = self.account(version)
                self.assertEqual(answer["outcome"], "DENY")
                self.assertEqual(answer["reasons"], ["Created 2 d ago.", "Not on the owner's allowlist."])
                self.assertEqual(self.mcp(answer), "Flagged")
                self.assertEqual(reply("account", "stranger.near", answer).splitlines()[1:3],
                                 ["Result: Flagged", "Reasons: Created 2 d ago.; Not on the owner's allowlist."])

    def test_other_asks_keep_not_enough_data_even_with_young_prose(self):
        answer = self.account(1)
        answer["outcome"] = "ASK"
        answer["reasons"] = ["The owner should confirm the young receiver before paying."]
        for check_id, status in (("account_age", "unknown"), ("dust_funded", "signal")):
            with self.subTest(check_id=check_id, status=status):
                document = json.loads(answer["evidence"])
                document["checks"] = [evidence.check(check_id, status, answer["reasons"][0],
                                                      {"provider": "recorded", "request_sha256": None,
                                                       "fetched_at": None})]
                answer["checks"] = [{"name": check_id, "status": status, "detail": answer["reasons"][0]}]
                answer["evidence"] = evidence.canonical_bytes(document).decode("utf-8")
                self.assertEqual(self.mcp(answer), "Not enough data")
                self.assertEqual(reply("account", "stranger.near", answer).splitlines()[1],
                                 "Result: Not enough data")

    def test_young_headline_uses_the_signal_rather_than_reason_prose(self):
        answer = self.account(1)
        answer["outcome"] = "ASK"
        answer["reasons"] = ["An independently worded receiver confirmation reason."]
        self.assertEqual(self.mcp(answer), "New account, confirm first")
        self.assertEqual(reply("account", "stranger.near", answer).splitlines()[1:3],
                         ["Result: New account, confirm first", "Reasons: " + answer["reasons"][0]])

    def test_payment_young_reason_uses_account_age_status_rather_than_prose(self):
        reason = "An independently worded receiver confirmation reason."
        for status, expected in (("signal", [reason]), ("pass", []),
                                 ("unknown", ["account_age could not run: an account age observation"])):
            with self.subTest(status=status):
                checks = [evidence.check("account_age", status, "an account age observation",
                                         {"provider": "recorded", "request_sha256": None,
                                          "fetched_at": None})]
                judgement = rules.Judgement(verdict="UNKNOWN", checks=checks,
                                            reasons=[reason, "Not on the owner's allowlist."],
                                            labels=[], observed={})
                with mock.patch("gate.rules.judge", return_value=judgement):
                    answer = verdict.request("near", None, "stranger.near", "1", None,
                                             gate="gate.near", agent_pk_hex="11" * 32,
                                             policy={}, store=self.store, rpc=self.rpc,
                                             nearblocks=self.nearblocks, signer=None,
                                             evidence_dir=self.fx.directory, rules_version=3)
                self.assertEqual(answer["ask"], expected)
                self.assertEqual((answer["outcome"], answer["action"], answer["verdict"]),
                                 ("UNKNOWN", "ask", None))
