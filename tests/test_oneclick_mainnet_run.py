import json
import unittest
from pathlib import Path

from gate import base58, evidence, intents, oneclick

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "fixtures" / "oneclick" / "run-20261122" / "report.json"
RECORD = ROOT / "fixtures" / "runs" / "oneclick-mainnet-20261122.txt"
EVIDENCE_HASH = "0b5388453e16d48d5dfaf825f2a22307a0e13286fd21bc4a8758108fb02a4984"
BASE_HASH = "0xf22069c4828ba3b07c0b70cf7dd5291077f061bbe9d0e6e7bf7ec5c628038e77"


class OneClickMainnetRunTest(unittest.TestCase):
    def setUp(self):
        self.state = json.loads(REPORT.read_text(encoding="utf-8"))
        self.record = RECORD.read_text(encoding="utf-8")
        for response in (self.state["quote_response"], self.state["status"]["quoteResponse"]):
            response["signature"] = "ed25519:" + base58.encode(bytes(response.pop("signature_bytes")))

    def test_report_binds_the_judged_signed_quote(self):
        self.assertIs(oneclick._bound_report(self.state), self.state)
        self.assertEqual(self.state["judgement"]["verdict"], "ALLOW")
        self.assertEqual(self.state["verdict"], "ALLOW")
        self.assertEqual(self.state["phase"], "settled")
        self.assertEqual(self.state["judgement"]["evidence_hash"], EVIDENCE_HASH)
        self.assertEqual(evidence.evidence_hash(self.state["judgement"]["document"]), EVIDENCE_HASH)
        self.assertTrue(oneclick.verify_quote_signature(self.state["quote_response"]))
        self.assertTrue(oneclick.verify_quote_signature(self.state["status"]["quoteResponse"]))

    def test_status_is_success_for_the_original_deposit(self):
        status = self.state["status"]
        updated = intents._instant(status["updatedAt"]).timestamp()
        self.assertEqual(oneclick.check_status(self.state["quote_response"], status, clock=lambda: updated), "SUCCESS")
        self.assertEqual(status["swapDetails"]["amountIn"], self.state["amount"])
        self.assertEqual(status["swapDetails"]["originChainTxHashes"], [
            {"hash": self.state["deposit_hash"], "explorerUrl": ""}])

    def test_settlement_amount_equals_amountOut(self):
        settlement = self.state["settlement"]
        details = self.state["status"]["swapDetails"]
        self.assertEqual(settlement["amount"], "296020")
        self.assertEqual(settlement["amount"], details["amountOut"])
        self.assertEqual(settlement["recipient"], self.state["recipient"])
        self.assertEqual(settlement["asset"], self.state["destination"])
        self.assertEqual(len(settlement["receipts"]), 1)
        self.assertEqual(settlement["receipts"][0]["block_number"], 60872158)
        self.assertEqual(settlement["receipts"][0]["transaction_hash"], BASE_HASH)
        self.assertEqual(details["destinationChainTxHashes"], [{"hash": BASE_HASH, "explorerUrl": ""}])

    def test_record_names_the_report_values(self):
        quote = self.state["quote_response"]["quote"]
        for value in (self.state["sender"], self.state["amount"], self.state["destination"],
                      self.state["recipient"], self.state["min_amount_out"], self.state["deposit_hash"],
                      self.state["deposit_reference"]["public_key"], quote["depositAddress"], quote["amountOut"],
                      quote["minAmountOut"], quote["withdrawFee"], EVIDENCE_HASH, BASE_HASH):
            with self.subTest(value=value):
                self.assertIn(value, self.record)
        self.assertIn(REPORT.relative_to(ROOT).as_posix(), self.record)
        self.assertIn("repo commit 43f5b2523", self.record)
        self.assertIn("run ended verdict ALLOW, phase settled", self.record)
        self.assertIn("key file was deleted after each run; the FullAccess key was not removed", self.record)

    def test_net_reading(self):
        actual = self.state["status"]["swapDetails"]
        self.assertEqual(self.state["quote_response"]["quote"]["withdrawFee"], "2400")
        self.assertEqual(actual["withdrawFee"], "2400")
        received = int(self.state["settlement"]["amount"])
        self.assertEqual(received, int(actual["amountOut"]))
        self.assertNotEqual(received, int(actual["amountOut"]) - int(actual["withdrawFee"]))
        self.assertIn("this run supports the net reading of amountOut", self.record)


if __name__ == "__main__":
    unittest.main()
