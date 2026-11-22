"""Version 3 age-only decisions stay ASK through public receipt consumers."""

import copy
import json
from unittest import mock

from gate import incident_replay, rules, verify
from intel import __main__ as intel_main
from intel import live
from tests import test_incident_replay, test_rules


class RulesVersionThreeLiveTest(test_rules.RulesCase):
    def setUp(self):
        super().setUp()
        self.stranger("young.near", age_ns=2 * test_rules.DAY_NS)
        self.request = test_rules.request("young.near", kind="near", memo=None)

    def judgement(self, version):
        return rules.judge(self.request, {"allowlist": [], "caps": []}, self.store, self.rpc,
                           self.nearblocks, now_ms=test_rules.NOW_MS, injection_scanner=None,
                           rules_version=version)

    def answer(self, judgement):
        service = live.LiveAPI(self.store, wall_clock=lambda: test_rules.NOW_MS / 1000,
                               perf_clock=lambda: 10)
        with mock.patch("intel.live.rules.judge", return_value=judgement):
            status, answer, _ = service.handle("/live/verdict?receiver=young.near&token=near&amount=1000000",
                                               "127.0.0.1", {})
        self.assertEqual(status, 200)
        return answer

    def test_live_version_3_age_only_is_ask_with_the_owner_reason(self):
        judgement = self.judgement(3)
        self.assertEqual(judgement.verdict, "UNKNOWN")
        self.assertEqual({check["id"] for check in judgement.checks if check["status"] == "signal"},
                         {"account_age"})
        answer = self.answer(judgement)
        self.assertEqual((answer["outcome"], answer["action"]), ("ASK", "ask"))
        self.assertEqual(answer["reasons"], judgement.reasons)
        self.assertIn("younger than 7 days", answer["reasons"][0])
        self.assertIn("confirm the receiver before paying", answer["reasons"][0])
        self.assertEqual(json.loads(answer["evidence"])["verdict"], "UNKNOWN")

    def test_live_version_1_and_2_age_only_still_refuse(self):
        for version in (1, 2):
            with self.subTest(version=version):
                judgement = self.judgement(version)
                answer = self.answer(judgement)
                self.assertEqual((judgement.verdict, answer["outcome"], answer["action"]),
                                 ("DENY", "DENY", "refuse"))
                self.assertEqual(answer["reasons"], ["Created 2 d ago.", "Not on the owner's allowlist."])

    def test_verify_accepts_age_only_ask_and_rejects_changed_outcome_or_evidence(self):
        answer = self.answer(self.judgement(3))
        answer.update(outcome="ASK", action="ask")
        path = self.fx.directory / "receipt.json"
        path.write_text(json.dumps(answer), encoding="utf-8")
        self.assertIn("outcome: ASK (ask)", verify.verify_receipt(path))
        for field, value in (("outcome", "DENY"), ("action", "refuse"), ("outcome", "ALLOW"),
                             ("evidence", answer["evidence"] + " "),
                             ("reasons", ["No confirmation needed."])):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(answer)
                changed[field] = value
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(verify.VerifyError):
                    verify.verify_receipt(path)

    def test_age_only_with_an_unavailable_check_stays_ask(self):
        self.stranger("young.near", age_ns=2 * test_rules.DAY_NS, code_hash=test_rules.CODE_HASH)
        judgement = self.judgement(3)
        self.assertEqual(self.status(judgement, "account_age"), "signal")
        self.assertEqual(self.status(judgement, "code_without_metadata"), "unknown")
        answer = self.answer(judgement)
        self.assertEqual(answer["outcome"], "ASK")
        self.assertEqual(answer["reasons"], judgement.reasons)
        self.assertTrue(any("code_without_metadata could not run" in reason for reason in answer["reasons"]))

    def test_receiver_coverage_names_age_confirmation_even_without_missing_checks(self):
        judgement = self.judgement(3)
        result = {"verdict": judgement.verdict, "checks": judgement.checks}
        line = intel_main.top_receivers_coverage_line({"subjects": [self.request]}, [result])
        self.assertEqual(line, "Top receiver coverage: judged 1/1; ALLOW 0/1; DENY 0/1; "
                         "UNKNOWN 1/1; UNKNOWN causes: young_receiver_confirmation 1.")

    def test_incident_replay_uses_unknown_as_ask_and_keeps_default_version_1(self):
        chain = test_incident_replay.Chain()
        document = {"accounts": [{"subject": "young.near", "chain": "near",
                                   "role_as_reported": "collector"}]}
        header = {"height": test_incident_replay.HEIGHT,
                  "timestamp": test_incident_replay.START_NS + test_incident_replay.HEIGHT * 10**9}
        judge = rules.judge
        def judge3(*args, **kwargs):
            return judge(*args, **kwargs, rules_version=3)
        with mock.patch.object(incident_replay, "_resolve", return_value=header):
            first = incident_replay._evaluate("example", document, "2026-04-16T00:00:00Z",
                                              header["timestamp"], chain, None)
            with mock.patch.object(incident_replay.rules, "judge", side_effect=judge3):
                third = incident_replay._evaluate("example", document, "2026-04-16T00:00:00Z",
                                                  header["timestamp"], chain, None)
        self.assertEqual((first["accounts"][0]["verdict"], first["accounts"][0]["outcome"]),
                         ("DENY", "DENY"))
        self.assertEqual((third["accounts"][0]["verdict"], third["accounts"][0]["outcome"]),
                         ("UNKNOWN", "ASK"))
        self.assertEqual(third["accounts"][0]["checks"], first["accounts"][0]["checks"])
        age = next(check for check in third["accounts"][0]["checks"] if check["id"] == "account_age")
        self.assertEqual(age["status"], "signal")
        self.assertIn("less than 7 days ago", age["detail"])
