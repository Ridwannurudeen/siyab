import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from gate import __main__ as gate_main
from gate import base58, ed25519, verify

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "fixtures" / "registry"
RECORD = REGISTRY / "sample-allow.json"
EVENT = REGISTRY / "sample-allow-event.json"
EVIDENCE = REGISTRY / "sample-allow-evidence.json"
VERDICT = REGISTRY / "sample-allow-verdict.json"
TEST_KEY = (ROOT / "fixtures" / "keys" / "test-verifier.pub").read_text(encoding="utf-8").splitlines()[0]


def run(*argv):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = gate_main.main(["verify", *map(str, argv)])
    return code, output.getvalue(), errors.getvalue()


class VerifyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_the_sample_verifies(self):
        code, output, _ = run(RECORD, EVIDENCE, "--verdict", VERDICT, "--key", TEST_KEY)
        self.assertEqual(code, 0)
        lines = output.splitlines()
        self.assertEqual(lines[-1], "verified")
        self.assertIn("  standard shieldbot-near, version 1.0.0, event verdict_recorded", lines)
        self.assertNotIn("FAIL", output)
        self.assertEqual(sum(line.startswith("ok   ") for line in lines), 5)
        self.assertEqual(run(RECORD, EVIDENCE)[0], 0)

    def test_one_flipped_byte_in_the_evidence_fails(self):
        data = bytearray(EVIDENCE.read_bytes())
        index = data.index(b'"ALLOW"') + 2
        data[index] ^= 0x01
        flipped = self.dir / "evidence.json"
        flipped.write_bytes(bytes(data))
        code, output, _ = run(RECORD, flipped, "--verdict", VERDICT, "--key", TEST_KEY)
        self.assertEqual(code, 1)
        self.assertIn("FAIL evidence sha256 equals the record's evidence_hash", output)
        self.assertEqual(output.splitlines()[-1], "not verified")

    def test_a_wrong_key_fails(self):
        other = base58.encode_key(ed25519.public_from_seed(ed25519.TEST_AGENT_SEED))
        code, output, _ = run(RECORD, EVIDENCE, "--verdict", VERDICT, "--key", other)
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL signature verifies with {other}", output)

    def test_a_changed_verdict_field_fails_the_signature(self):
        verdict_object = json.loads(VERDICT.read_text(encoding="utf-8"))
        verdict_object["expires_ms"] += 1
        changed = self.dir / "verdict.json"
        changed.write_text(json.dumps(verdict_object), encoding="utf-8")
        self.assertEqual(run(RECORD, EVIDENCE, "--verdict", changed, "--key", TEST_KEY)[0], 1)

    def test_a_record_with_another_verdict_fails(self):
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        record["verdict"] = "Deny"
        path = self.dir / "record.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        code, output, _ = run(path, EVIDENCE)
        self.assertEqual(code, 1)
        self.assertIn("FAIL evidence verdict equals the record's verdict", output)

    def test_the_event_data_shape_and_the_view_shape_both_parse(self):
        view = verify.load_record(json.loads(RECORD.read_text(encoding="utf-8")))
        event_document = json.loads(EVENT.read_text(encoding="utf-8"))
        event = verify.load_record(event_document)
        data = verify.load_record(event_document["data"][0])
        self.assertEqual((view["shape"], event["shape"], data["shape"]), ("view", "event", "event"))
        for key in ("verdict", "evidence_hash", "observed_block", "verdict_id"):
            self.assertEqual(view[key], event[key])
        self.assertEqual(event["subject"], "vendor.testnet")
        self.assertEqual(run(EVENT, EVIDENCE, "--verdict", VERDICT, "--key", TEST_KEY)[0], 0)

    def test_verdict_codes_and_byte_hashes_parse(self):
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        for verdict_value in ("ALLOW", "Allow", 1):
            record["verdict"] = verdict_value
            record["evidence_hash"] = list(bytes.fromhex(json.loads(RECORD.read_text())["evidence_hash"]))
            self.assertEqual(verify.load_record(record)["verdict"], "ALLOW")
        self.assertEqual(verify.load_record(dict(record, verdict=0))["verdict"], "UNKNOWN")
        for bad in (dict(record, verdict=True), dict(record, evidence_hash="AB" * 32), dict(record, extra=1)):
            with self.subTest(bad=bad), self.assertRaises(verify.VerifyError):
                verify.load_record(bad)

    def test_a_missing_file_or_a_lone_key_exits_1(self):
        self.assertEqual(run(self.dir / "missing.json", EVIDENCE)[0], 1)
        self.assertEqual(run(RECORD, EVIDENCE, "--key", TEST_KEY)[0], 1)
        shutil.copy(EVIDENCE, self.dir / "e.json")
        self.assertEqual(run(RECORD, self.dir / "e.json")[0], 0)


if __name__ == "__main__":
    unittest.main()
