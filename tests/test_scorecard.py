import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import scorecard


class ScorecardTest(unittest.TestCase):
    def test_label_metrics_count_only_current_assertions_as_hostile(self):
        rows = {name: value for name, value, _ in scorecard.label_rows(ROOT)}
        self.assertEqual(rows["Label assertions"], "3,295 / 3,295 committed assertion rows")
        self.assertEqual(rows["Current hostile assertions (incident and denylist)"],
                         "61 / 3,286 current assertion rows")
        self.assertEqual(rows["Current reference assertions (all other categories)"],
                         "3,225 / 3,286 current assertion rows")

    def test_rust_sandbox_citations_count_as_test_references(self):
        citation = (
            "contracts/reports/tests/sandbox.rs::"
            "sandbox_submit_accept_dispute_overturn_updates_subject_summary"
        )
        self.assertIsNotNone(scorecard.TEST_CITATION.search(citation))

    def test_check_passes_on_the_committed_page(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "scorecard.py"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_fails_on_a_stale_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / scorecard.SCORECARD
            page.parent.mkdir(parents=True)
            page.write_text(scorecard.render() + "stale\n", encoding="utf-8")
            with mock.patch.object(scorecard, "ROOT", Path(tmp)):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(scorecard.main(["--check"]), 1)
                page.unlink()
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(scorecard.main(["--check"]), 1)

    def test_registry_counts_distinct_verdict_ids_and_shows_readback_mismatches(self):
        registry_dir = ROOT / scorecard.REGISTRY
        verdict_ids = set()
        for path in sorted(registry_dir.glob("testnet-*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            entries = record["data"] if record.get("event") == "verdict_recorded" else [record]
            verdict_ids.update(entry["verdict_id"] for entry in entries)
        for path in sorted(registry_dir.glob("event-*.json")):
            event = json.loads(path.read_text(encoding="utf-8"))
            if event.get("event") != "verdict_recorded":
                continue
            entries = event["data"]
            if all(entry["subject"].endswith(".testnet") for entry in entries):
                verdict_ids.update(entry["verdict_id"] for entry in entries)

        readback = scorecard.REGISTRY_READBACK
        readback_text = (ROOT / readback).read_text(encoding="utf-8")
        readback_count = int(re.search(r"(?m)^total_records after: (\d+)$", readback_text)[1])
        expected_value = (
            f"{readback_count:,} on-chain records / "
            f"{len(verdict_ids):,} distinct fixture verdict IDs"
        )
        expected_row = f"| Testnet registry records | {expected_value} |"
        self.assertIn(expected_row, scorecard.render(ROOT))

        inputs = [
            scorecard.RECEIVER_SET,
            scorecard.LABELS,
            scorecard.FEED_SNAPSHOT,
            scorecard.TESTNET,
            scorecard.CLAIMS,
            readback,
            *(path.relative_to(ROOT) for path in sorted((ROOT / scorecard.JUDGEMENTS).glob("*.json"))),
            *(path.relative_to(ROOT) for path in sorted(registry_dir.glob("*.json"))),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            copy_root = Path(temporary)
            for relative in inputs:
                destination = copy_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)

            repeated = json.loads(
                (copy_root / scorecard.REGISTRY / "testnet-aged-payee.json").read_text(encoding="utf-8")
            )
            (copy_root / scorecard.REGISTRY / "testnet-repeated-id.json").write_text(
                json.dumps(repeated), encoding="utf-8"
            )
            self.assertIn(expected_row, scorecard.render(copy_root))

            readback_copy = copy_root / readback
            mismatched_readback = re.sub(
                r"(?m)^total_records after: \d+$",
                f"total_records after: {readback_count + 1}",
                readback_copy.read_text(encoding="utf-8"),
            )
            readback_copy.write_text(mismatched_readback, encoding="utf-8")
            mismatch_row = (
                f"| Testnet registry records | {readback_count + 1:,} on-chain records / "
                f"{len(verdict_ids):,} distinct fixture verdict IDs (mismatch) |"
            )
            self.assertIn(mismatch_row, scorecard.render(copy_root))

    def test_changing_an_input_fixture_changes_the_render_in_a_temporary_copy(self):
        inputs = [
            scorecard.RECEIVER_SET,
            scorecard.LABELS,
            scorecard.FEED_SNAPSHOT,
            scorecard.TESTNET,
            scorecard.CLAIMS,
            scorecard.REGISTRY_READBACK,
            *(path.relative_to(ROOT) for path in sorted((ROOT / scorecard.JUDGEMENTS).glob("*.json"))),
            *(path.relative_to(ROOT) for path in sorted((ROOT / scorecard.REGISTRY).glob("*.json"))),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            copy_root = Path(temporary)
            for relative in inputs:
                destination = copy_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)

            before = scorecard.render(copy_root)
            judgement_path = next(
                path for path in sorted((copy_root / scorecard.JUDGEMENTS).glob("*.json"))
                if json.loads(path.read_text(encoding="utf-8"))["verdict"] == "ALLOW"
            )
            judgement = json.loads(judgement_path.read_text(encoding="utf-8"))
            judgement["verdict"] = "DENY"
            judgement_path.write_text(json.dumps(judgement), encoding="utf-8")
            after = scorecard.render(copy_root)

        self.assertNotEqual(after, before)

    def test_every_metric_line_names_a_source_file(self):
        page = (ROOT / scorecard.SCORECARD).read_text(encoding="utf-8")
        source_path = re.compile(r"(?:[\w.*-]+/)+[\w.*-]+\.(?:jsonl|json|md|py)\b")
        rows = [
            line for line in page.splitlines()
            if line.startswith("| ") and not line.startswith("| ---")
            and line.split("|")[1].strip() != "Measure"
        ]
        self.assertTrue(rows)
        for line in rows:
            with self.subTest(line=line):
                self.assertRegex(line, source_path)


if __name__ == "__main__":
    unittest.main()
