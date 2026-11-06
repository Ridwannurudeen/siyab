import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ReportsBodyVectorTest(unittest.TestCase):
    def test_python_hash_matches_shared_report_body_vector(self):
        vector = json.loads(
            (ROOT / "fixtures" / "records" / "body-vector.json").read_text(
                encoding="utf-8"
            )
        )
        canonical_json = json.dumps(
            vector["input"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(canonical_json).hexdigest(), vector["expected_sha256"]
        )
