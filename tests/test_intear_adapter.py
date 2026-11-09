import json
import time
import unittest

from integrations.intear.adapter import MAX_VERDICT_AGE_MS, address_safety_json, state_for_verdict


class IntearAdapterTests(unittest.TestCase):
    def complete_allow(self):
        now_ms = time.time_ns() // 1_000_000
        return {
            "outcome": "ALLOW",
            "action": "sign",
            "verdict": {
                "literal": "ALLOW",
                "issued_ms": now_ms,
                "expires_ms": now_ms + MAX_VERDICT_AGE_MS,
                "evidence_hash": "recorded-hash",
            },
            "request": {"receiver_id": "shown.near"},
            "checks": [{"id": "receiver_exists", "status": "pass"}],
            "evidence_hash": "recorded-hash",
            "reasons": ["Every check ran."],
        }

    def test_each_verdict_maps_to_the_wallet_state(self):
        answers = (
            (self.complete_allow(), "Safe"),
            ({"outcome": "DENY", "action": "refuse", "verdict": None,
              "request": {"receiver_id": "shown.near"}}, "Unsafe"),
            ({"verdict": "ASK"}, "Unknown"),
            ("UNKNOWN", "Unknown"),
            ("DENY", "Unknown"),
        )
        for verdict, expected in answers:
            with self.subTest(verdict=verdict):
                self.assertEqual(state_for_verdict(verdict, "shown.near"), expected)

    def test_bare_allow_is_unknown(self):
        self.assertEqual(state_for_verdict("ALLOW", "shown.near"), "Unknown")

    def test_literal_only_allow_is_unknown(self):
        answer = {"outcome": "ALLOW", "action": "sign", "verdict": {"literal": "ALLOW"}}
        self.assertEqual(state_for_verdict(answer, "shown.near"), "Unknown")

    def test_allow_without_evidence_hash_is_unknown(self):
        answer = self.complete_allow()
        answer.pop("evidence_hash")
        self.assertEqual(state_for_verdict(answer, "shown.near"), "Unknown")

    def test_allow_for_a_different_receiver_is_unknown(self):
        answer = self.complete_allow()
        answer["request"]["receiver_id"] = "other.near"
        self.assertEqual(state_for_verdict(answer, "shown.near"), "Unknown")

    def test_allow_without_a_receiver_is_unknown(self):
        answer = self.complete_allow()
        answer["request"].pop("receiver_id")
        self.assertEqual(state_for_verdict(answer, "shown.near"), "Unknown")

    def test_allow_with_failed_check_is_unknown(self):
        answer = self.complete_allow()
        answer["checks"].append({"id": "label_denylist", "status": "signal"})
        self.assertEqual(state_for_verdict(answer, "shown.near"), "Unknown")

    def test_allow_with_unreadable_check_is_unknown(self):
        answer = self.complete_allow()
        answer["checks"].append({"id": "storage_registered"})
        self.assertEqual(state_for_verdict(answer, "shown.near"), "Unknown")

    def test_stale_allow_is_unknown(self):
        answer = self.complete_allow()
        now_ms = time.time_ns() // 1_000_000
        answer["verdict"]["issued_ms"] = now_ms - MAX_VERDICT_AGE_MS - 1
        answer["verdict"]["expires_ms"] = now_ms - 1
        self.assertEqual(state_for_verdict(answer, "shown.near"), "Unknown")

    def test_complete_fresh_allow_is_safe(self):
        self.assertEqual(state_for_verdict(self.complete_allow(), "shown.near"), "Safe")

    def test_malformed_or_missing_verdict_stays_unknown(self):
        for answer in (None, {}, {"outcome": "ALLOW"}, {"outcome": "ALLOW", "action": "ask"},
                       {"outcome": "ALLOW", "action": "sign", "verdict": None}):
            with self.subTest(answer=answer):
                self.assertEqual(state_for_verdict(answer, "shown.near"), "Unknown")

    def test_response_carries_evidence_hash(self):
        answer = self.complete_allow()
        response = json.loads(address_safety_json(answer, "shown.near"))
        self.assertEqual(response, {"status": "Safe", "reasons": ["Every check ran."],
                                    "evidence_hash": "recorded-hash"})


if __name__ == "__main__":
    unittest.main()
