import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from gate import evidence

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "evidence"


def sample(**changes):
    doc = evidence.document(
        subject=evidence.subject(gate="gate.testnet", agent_pk="ab" * 32, kind="ft",
                                 token="token.gate.testnet", receiver="vendor.testnet",
                                 amount="1000000", memo="café"),
        checks=[evidence.check("receiver_exists", "pass", "account exists",
                               {"provider": "rpc", "request_sha256": "0" * 64,
                                "fetched_at": "2026-10-30T00:00:00Z"})],
        labels=[],
        verdict="ALLOW",
        reasons=["every check ran and none found a signal"],
        observed={"block_height": 1, "timestamp_ms": 2},
        issued_ms=3,
        expires_ms=4,
        verdict_id="5" * 32,
        latency_ms=6,
    )
    doc.update(changes)
    return doc


class CanonicalBytesTest(unittest.TestCase):
    def test_exact_bytes_and_hash(self):
        memo_hash = hashlib.sha256("café".encode()).hexdigest()
        expected = (
            '{"checks":[{"detail":"account exists","id":"receiver_exists","source":'
            '{"fetched_at":"2026-10-30T00:00:00Z","provider":"rpc","request_sha256":"' + "0" * 64
            + '"},"status":"pass"}],"expires_ms":4,"issued_ms":3,"labels":[],"latency_ms":6,'
            '"observed":{"block_height":1,"timestamp_ms":2},'
            '"reasons":["every check ran and none found a signal"],'
            '"subject":{"agent_pk":"' + "ab" * 32 + '","amount":"1000000","gate":"gate.testnet",'
            '"kind":"ft","memo_sha256":"' + memo_hash + '","receiver":"vendor.testnet",'
            '"token":"token.gate.testnet"},"verdict":"ALLOW","verdict_id":"' + "5" * 32 + '",'
            '"version":"1"}'
        ).encode("utf-8")
        self.assertEqual(evidence.canonical_bytes(sample()), expected)
        self.assertEqual(evidence.evidence_hash(sample()), hashlib.sha256(expected).hexdigest())

    def test_hash_is_independent_of_key_order(self):
        doc = sample()
        reordered = dict(reversed(list(doc.items())))
        reordered["subject"] = dict(reversed(list(doc["subject"].items())))
        self.assertEqual(evidence.evidence_hash(doc), evidence.evidence_hash(reordered))

    def test_hash_changes_with_any_field(self):
        base = evidence.evidence_hash(sample())
        for changes in ({"verdict": "DENY"}, {"latency_ms": 7}, {"labels": ["x"]}, {"reasons": []}):
            with self.subTest(changes=changes):
                self.assertNotEqual(evidence.evidence_hash(sample(**changes)), base)

    def test_non_ascii_is_kept_as_utf8(self):
        data = evidence.canonical_bytes({"detail": "café"})
        self.assertEqual(data, '{"detail":"café"}'.encode())
        self.assertFalse(data.endswith(b"\n"))

    def test_memo_absent_is_null(self):
        subject = evidence.subject(gate="g.testnet", agent_pk="ab" * 32, kind="near", token=None,
                                   receiver="r.testnet", amount="1", memo=None)
        self.assertIsNone(subject["memo_sha256"])

    def test_document_keys(self):
        self.assertEqual(sorted(sample()), sorted([
            "version", "subject", "checks", "labels", "verdict", "reasons", "observed",
            "issued_ms", "expires_ms", "verdict_id", "latency_ms"]))
        self.assertEqual(sample()["version"], "1")

    def test_check_status_is_one_of_three(self):
        for status in evidence.STATUSES:
            self.assertEqual(evidence.check("x", status, "d", {})["status"], status)
        with self.assertRaises(ValueError):
            evidence.check("x", "ok", "d", {})


class FileTest(unittest.TestCase):
    def test_write_and_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = evidence.write(sample(), tmp)
            self.assertEqual(path.name, evidence.evidence_hash(sample()) + ".json")
            self.assertEqual(path.read_bytes(), evidence.canonical_bytes(sample()))
            self.assertTrue(evidence.check_file(path))

    def test_one_changed_byte_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = evidence.write(sample(), tmp)
            original = path.read_bytes()
            for index in (0, len(original) // 2, len(original) - 1):
                changed = bytearray(original)
                changed[index] ^= 0x01
                with self.subTest(index=index):
                    path.write_bytes(bytes(changed))
                    self.assertFalse(evidence.check_file(path))

    def test_non_canonical_form_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = json.dumps(sample(), indent=2).encode("utf-8")
            path = Path(tmp) / f"{hashlib.sha256(data).hexdigest()}.json"
            path.write_bytes(data)
            self.assertFalse(evidence.check_file(path))

    def test_committed_evidence_files_check(self):
        paths = sorted(EVIDENCE_DIR.glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                self.assertTrue(evidence.check_file(path))


if __name__ == "__main__":
    unittest.main()
