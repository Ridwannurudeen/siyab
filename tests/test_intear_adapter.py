import json
import unittest

from integrations.intear.adapter import address_safety_json, state_for_verdict


class IntearAdapterTests(unittest.TestCase):
    def test_each_verdict_maps_to_the_wallet_state(self):
        answers = (
            ("ALLOW", "Safe"),
            ({"outcome": "ALLOW", "action": "sign", "verdict": {"literal": "ALLOW"}}, "Safe"),
            ("DENY", "Unsafe"),
            ({"outcome": "DENY", "action": "refuse", "verdict": None}, "Unsafe"),
            ("ASK", "Unknown"),
            ({"verdict": "ASK"}, "Unknown"),
            ("UNKNOWN", "Unknown"),
        )
        for verdict, expected in answers:
            with self.subTest(verdict=verdict):
                self.assertEqual(state_for_verdict(verdict), expected)

    def test_malformed_or_missing_verdict_stays_unknown(self):
        for answer in (None, {}, {"outcome": "ALLOW"}, {"outcome": "ALLOW", "action": "ask"},
                       {"outcome": "ALLOW", "action": "sign", "verdict": None}):
            with self.subTest(answer=answer):
                self.assertEqual(state_for_verdict(answer), "Unknown")

    def test_response_carries_evidence_hash(self):
        answer = {"verdict": "ALLOW", "reasons": ["Every check ran."], "evidence_hash": "recorded-hash"}
        response = json.loads(address_safety_json(answer))
        self.assertEqual(response, {"status": "Safe", "reasons": ["Every check ran."],
                                    "evidence_hash": "recorded-hash"})


if __name__ == "__main__":
    unittest.main()
