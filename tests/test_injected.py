"""Tests for the scripted replay of a prompt injected vendor instruction."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_the_verdict_is_deny_and_the_recorded_submission_is_refused(self):
        lines = []
        record = json.loads(injected.RECORD_PATH.read_text(encoding="utf-8"))
        transaction_hash = record["submissions"][0]["outcome"]["transaction_hash"]
        with patch("urllib.request.urlopen", side_effect=AssertionError("network access during replay")):
            self.assertEqual(injected.run(out=lines.append), 0)
        self.assertIn("a scripted agent", lines[0])
        self.assertIn("No model runs", lines[0])
        self.assertTrue(any("DENIED." in line for line in lines))
        self.assertIn("  Nothing was signed.", lines)
        self.assertTrue(any("REFUSED:no_verdict" in line for line in lines))
        self.assertIn("  injected: refused", lines)
        self.assertIn(f"  explorer: https://testnet.nearblocks.io/txns/{transaction_hash}", lines)
        self.assertTrue(any("On mainnet the same account reads" in line and "DENY" in line for line in lines))
        spec = importlib.util.spec_from_file_location("check_hygiene", ROOT / "scripts" / "check_hygiene.py")
        hygiene = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hygiene)
        self.assertFalse(any(hygiene.AUTHORED_WORD.search(line) for line in lines))

    def test_a_kept_evidence_directory_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = Path(tmp) / "evidence"
            evidence_dir.mkdir()
            with (patch("gate.injected.tempfile.mkdtemp", return_value=str(evidence_dir)),
                  patch.dict(os.environ, {injected.KEEP_EVIDENCE: "1"}),
                  patch("urllib.request.urlopen", side_effect=AssertionError("network access during replay"))):
                lines = []
                self.assertEqual(injected.run(out=lines.append), 0)
            self.assertIn(f"Evidence documents, kept: {evidence_dir}", lines)


if __name__ == "__main__":
    unittest.main()
