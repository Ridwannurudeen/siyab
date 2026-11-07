import contextlib
import io
import tempfile
import unittest

from intel import __main__ as intel_main
from intel import records


TARGET_RECORD_ID = "b" * 64
SYNTHETIC_URL = "https://example.test/synthetic-report"


def competing_record(content_sha256):
    return {
        "version": 1,
        "kind": "social",
        "outcome": "unknown",
        "subjects": [{"chain": "near", "account": "kestrelridge-mallory.testnet"}],
        "evidence": [{
            "url": SYNTHETIC_URL,
            "content_sha256": content_sha256,
            "fetched_at": "2026-11-07T05:12:01Z",
        }],
        "reports": [],
        "reasons": [],
        "assertion_ids": [],
        "timeline": [{
            "at": "2026-11-07T05:12:02Z",
            "event": "recorded",
            "source": {"type": "record"},
        }],
        "supersedes": TARGET_RECORD_ID,
        "publisher": "kestrelridge-owner.testnet",
    }


class RecordCheckSupersedesTest(unittest.TestCase):
    def test_competing_records_are_both_refused_across_supplied_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [
                records.write(competing_record("a" * 64), directory),
                records.write(competing_record("c" * 64), directory),
            ]
            for path in paths:
                records.read(path)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = intel_main.main(["record-check", *(str(path) for path in paths)])

        self.assertEqual(result, 1)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        for path, line in zip(paths, lines):
            self.assertIn(f"refused {path}: multiple records supersede {TARGET_RECORD_ID}", line)


if __name__ == "__main__":
    unittest.main()