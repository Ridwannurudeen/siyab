"""Tests for the prompt injected agent replay."""

import json
import unittest
from pathlib import Path

from gate import injected, rules

ROOT = Path(__file__).resolve().parent.parent


class InjectedTest(unittest.TestCase):
    def test_the_message_names_the_collector_of_the_rhea_incident_page(self):
        message = injected.parse_message(injected.MESSAGE_PATH.read_text(encoding="utf-8"))
        incident = json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_text(encoding="utf-8"))
        collector = incident["accounts"][0]["subject"]
        self.assertEqual(message["receiver_id"], collector)
        self.assertEqual(message["kind"], "ft")
        self.assertEqual(message["token_id"], "token.kestrelridge-main.testnet")
        self.assertEqual(message["amount"], "10000000")
        self.assertTrue(message["memo"].lower().startswith("ignore"))
        self.assertTrue(dict(rules.INJECTION_PATTERNS)["ignore_instructions"].search(message["memo"]))

    def test_the_memo_trips_the_local_injection_patterns(self):
        message = injected.parse_message(injected.MESSAGE_PATH.read_text(encoding="utf-8"))
        matched = {name for name, pattern in rules.INJECTION_PATTERNS if pattern.search(message["memo"])}
        self.assertTrue({"ignore_instructions", "drain_funds"} <= matched)

    def test_a_message_without_a_payee_or_amount_is_refused(self):
        with self.assertRaisesRegex(ValueError, "pay-to"):
            injected.parse_message("From: billing\nSubject: invoice\n\namount: 1\n")
        with self.assertRaisesRegex(ValueError, "amount"):
            injected.parse_message("From: billing\nSubject: invoice\n\npay-to: vendor.testnet\n")

    def test_the_collector_judgement_on_mainnet_is_deny_with_the_label_and_the_issuer(self):
        incident = json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_text(encoding="utf-8"))
        collector = incident["accounts"][0]["subject"]
        judgement = injected.judgement_for(collector)
        self.assertEqual(judgement["verdict"], "DENY")
        checks = {check["id"]: check for check in judgement["checks"]}
        self.assertEqual(checks["label_denylist"]["status"], "signal")
        self.assertEqual(checks["issuer_blacklist"]["status"], "signal")


if __name__ == "__main__":
    unittest.main()
