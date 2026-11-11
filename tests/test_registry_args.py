import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from gate import __main__ as gate_main
from gate import evidence, registry

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "fixtures" / "registry"
SAMPLE_HASH = "c9afcae56b4f7e1d5ab6b4cb9b1a01d8fa8a129e4f510a8aab63acc49b3a2782"
SAMPLE_ID = "2ebb8f1dcba43f2861590189bfa9c2fa"
RECORD_KEYS = ["subject", "verdict", "evidence_hash", "observed_block", "verdict_id"]


def document(kind, verdict, receiver="vendor-pay.testnet", block=230000123):
    doc = evidence.document(
        subject=evidence.subject(gate="gate.testnet", agent_pk="ab" * 32, kind=kind,
                                 token="token.gate.testnet" if kind == "ft" else None, receiver=receiver,
                                 amount="1000000", memo=None),
        checks=[], labels=[], verdict=verdict, reasons=["a test document"],
        observed={"block_height": block, "timestamp_ms": 1793534400000}, issued_ms=None, expires_ms=None,
        verdict_id=None, latency_ms=3)
    return doc


class RegistryArgsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        shutil.copyfile(SAMPLES / "sample-allow-evidence.json", self.dir / f"{SAMPLE_HASH}.json")
        self.allow = self.dir / "allow.verdict.json"
        shutil.copyfile(SAMPLES / "sample-allow-verdict.json", self.allow)

    def unsigned(self, doc, name):
        evidence_hash = evidence.write(doc, self.dir).stem
        path = self.dir / name
        path.write_text(json.dumps({"literal": doc["verdict"], "evidence_hash": evidence_hash}), encoding="utf-8")
        return path, evidence_hash

    def run_command(self, *argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = gate_main.main(["registry-args", *map(str, argv), "--evidence-dir", str(self.dir)])
        return code, output.getvalue(), errors.getvalue()

    def test_an_allow_record_has_the_interface_shape(self):
        code, output, errors = self.run_command(self.allow)
        self.assertEqual((code, errors), (0, ""))
        batch = json.loads(output)
        self.assertEqual(list(batch), ["records"])
        self.assertEqual(batch["records"], [{"subject": "vendor.testnet", "verdict": "Allow",
                                             "evidence_hash": SAMPLE_HASH, "observed_block": 230000000,
                                             "verdict_id": SAMPLE_ID}])
        self.assertEqual(list(batch["records"][0]), RECORD_KEYS)

    def test_an_unsigned_allow_passed_to_a_human_is_not_recorded(self):
        unsigned_allow, _ = self.unsigned(document("ft", "ALLOW"), "unsigned-allow.verdict.json")
        with self.assertRaises(registry.RegistryError) as raised:
            registry.record_batch([unsigned_allow], self.dir)
        message = "an ALLOW passed to a human is not recorded; record the human's decision instead"
        self.assertEqual(str(raised.exception), message)
        code, output, errors = self.run_command(unsigned_allow)
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(errors, f"refused: {message}\n")

    def test_unsigned_deny_and_unknown_use_the_evidence_hash_as_the_id_and_say_so(self):
        deny, deny_hash = self.unsigned(document("ft", "DENY"), "deny.verdict.json")
        unknown, unknown_hash = self.unsigned(document("near", "UNKNOWN", receiver="supplier.testnet"),
                                              "unknown.verdict.json")
        code, output, errors = self.run_command(self.allow, deny, unknown)
        self.assertEqual(code, 0)
        records = json.loads(output)["records"]
        self.assertEqual([r["verdict"] for r in records], ["Allow", "Deny", "Unknown"])
        self.assertEqual(records[1], {"subject": "vendor-pay.testnet", "verdict": "Deny", "evidence_hash": deny_hash,
                                      "observed_block": 230000123, "verdict_id": deny_hash[:32]})
        self.assertEqual(records[2]["subject"], "supplier.testnet")
        self.assertEqual(records[2]["verdict_id"], unknown_hash[:32])
        said = "so verdict_id is the first 32 hex of the evidence hash"
        self.assertEqual(errors.splitlines(), [f"vendor-pay.testnet: unsigned DENY, {said}: {deny_hash[:32]}",
                                               f"supplier.testnet: unsigned UNKNOWN, {said}: {unknown_hash[:32]}"])

    def test_a_differing_observed_block_override_is_refused(self):
        with self.assertRaisesRegex(registry.RegistryError, "--observed-block 230000999 differs from the evidence "
                                                            "block height 230000000"):
            registry.record_input(self.allow, self.dir, observed_block=230000999)
        code, output, _ = self.run_command(self.allow, "--observed-block", "230000999")
        self.assertEqual((code, output), (2, ""))

    def test_an_equal_observed_block_override_is_accepted(self):
        code, output, errors = self.run_command(self.allow, "--observed-block", "230000000")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["records"][0]["observed_block"], 230000000)

    def test_an_observed_block_override_fills_a_missing_evidence_height(self):
        blockless, _ = self.unsigned(document("ft", "DENY", block=None), "blockless.verdict.json")
        code, output, errors = self.run_command(blockless)
        self.assertEqual((code, output), (2, ""))
        self.assertIn("pass --observed-block", errors)
        code, output, _ = self.run_command(blockless, "--observed-block", "7")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["records"][0]["observed_block"], 7)
        self.assertEqual(self.run_command(blockless, "--observed-block", "-1")[0], 2)

    def test_an_intents_verdict_is_refused_with_one_line(self):
        doc = document("ft", "DENY")
        doc["subject"] = {"kind": "intents", "origin_asset": "nep141:wrap.near", "recipient": "0x" + "1" * 40}
        intents_file, _ = self.unsigned(doc, "intents.verdict.json")
        code, output, errors = self.run_command(self.allow, intents_file)
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(len(errors.splitlines()), 1)
        self.assertEqual(errors, f"refused: {intents_file}: an intents verdict is a preflight on an EVM recipient; "
                                 "the registry records NEAR accounts only\n")

    def test_files_that_do_not_match_their_evidence_are_refused(self):
        tampered = self.dir / "tampered.verdict.json"
        verdict = json.loads(self.allow.read_text(encoding="utf-8"))
        cases = {
            "another literal": {**verdict, "literal": "DENY"},
            "another verdict id": {**verdict, "verdict_id": "0" * 32},
            "no evidence": {**verdict, "evidence_hash": "0" * 64},
            "not a literal": {**verdict, "literal": "MAYBE"},
        }
        for name, body in cases.items():
            tampered.write_text(json.dumps(body), encoding="utf-8")
            with self.subTest(case=name), self.assertRaises(registry.RegistryError):
                registry.record_input(tampered, self.dir)
        evidence_path = self.dir / f"{SAMPLE_HASH}.json"
        evidence_path.write_bytes(evidence_path.read_bytes() + b"\n")
        with self.assertRaises(registry.RegistryError):
            registry.record_input(self.allow, self.dir)

    def test_more_than_29_files_are_refused(self):
        with self.assertRaises(registry.RegistryError):
            registry.record_batch([self.allow] * 30, self.dir)
        self.assertEqual(len(registry.record_batch([self.allow] * 29, self.dir)[0]["records"]), 29)


if __name__ == "__main__":
    unittest.main()
