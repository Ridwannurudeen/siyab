import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import score  # noqa: E402


def row(account, label):
    return {"account": account, "label": label,
            "source_url": "https://example.com/report", "note": "A recorded reason."}


class HoldoutLoaderTest(unittest.TestCase):
    def write(self, directory, rows):
        path = Path(directory) / "holdout.jsonl"
        path.write_text("\n".join(json.dumps(item) for item in rows) + "\n", encoding="utf-8")
        return path

    def test_rejects_bad_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [
                {"account": "one.near", "label": "unsure", "source_url": "https://example.com", "note": "x"},
                {"account": "two.near", "label": "bad", "source_url": "", "note": "x"},
                {"account": "three.near", "label": "good", "source_url": "https://example.com", "note": ""},
                row("four.near", "bad"),
                row("four.near", "good"),
            ])
            rows, errors = score.load_holdout(path)
        self.assertEqual([item["account"] for item in rows], ["four.near"])
        self.assertEqual(len(errors), 4)

    def test_accepts_a_clean_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [row("one.near", "bad"), row("two.near", "good")])
            rows, errors = score.load_holdout(path)
        self.assertEqual(errors, [])
        self.assertEqual([item["account"] for item in rows], ["one.near", "two.near"])


class VerdictLoaderTest(unittest.TestCase):
    def test_reads_both_shapes_and_the_file_name(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "judged.json").write_text(json.dumps({"subject": "a.near", "verdict": "DENY"}), encoding="utf-8")
            (folder / "live.json").write_text(json.dumps({"account": "b.near", "outcome": "ASK"}), encoding="utf-8")
            (folder / "c.near.json").write_text(json.dumps({"verdict": "UNKNOWN"}), encoding="utf-8")
            (folder / "d.near.json").write_text(json.dumps({"outcome": "ALLOW"}), encoding="utf-8")
            answers, errors, duplicates = score.load_verdicts(folder)
        self.assertEqual(errors, [])
        self.assertEqual(duplicates, [])
        self.assertEqual(answers, {"a.near": "DENY", "b.near": "ASK", "c.near": "ASK", "d.near": "ALLOW"})

    def test_a_file_without_an_outcome_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "x.near.json").write_text(json.dumps({"note": "nothing"}), encoding="utf-8")
            answers, errors, _ = score.load_verdicts(folder)
        self.assertEqual(answers, {})
        self.assertEqual(len(errors), 1)


class ScoreTest(unittest.TestCase):
    def test_the_matrix_counts_ask_separately(self):
        rows = [
            row("bad-deny.near", "bad"),
            row("bad-allow.near", "bad"),
            row("bad-ask.near", "bad"),
            row("good-deny.near", "good"),
            row("good-allow.near", "good"),
            row("missing.near", "good"),
        ]
        answers = {
            "bad-deny.near": "DENY",
            "bad-allow.near": "ALLOW",
            "bad-ask.near": "ASK",
            "good-deny.near": "DENY",
            "good-allow.near": "ALLOW",
        }
        result = score.score(rows, answers)
        self.assertEqual(result["matrix"]["bad"], {"DENY": 1, "ALLOW": 1, "ASK": 1})
        self.assertEqual(result["matrix"]["good"], {"DENY": 1, "ALLOW": 1, "ASK": 0})
        self.assertAlmostEqual(result["precision"], 0.5)
        self.assertAlmostEqual(result["recall"], 0.5)
        self.assertEqual(result["counts"]["ask"], 1)
        self.assertEqual(result["counts"]["not_judged"], 1)
        self.assertEqual(result["not_judged_accounts"], ["missing.near"])

    def test_no_decided_bad_row_does_not_divide_by_zero(self):
        result = score.score([row("a.near", "bad")], {"a.near": "ASK"})
        self.assertEqual(result["precision"], 0.0)
        self.assertEqual(result["recall"], 0.0)

    def test_report_names_precision_and_recall(self):
        text = score.format_report(score.score([row("a.near", "bad")], {"a.near": "DENY"}))
        self.assertIn("precision (DENY vs bad): 1.000", text)
        self.assertIn("recall (bad refused):    1.000", text)
        self.assertIn("ASK kept apart:", text)

    def test_asserted_subjects_reads_current_incident_and_denylist_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.jsonl"
            path.write_text("\n".join(json.dumps(item) for item in [
                {"subject": "a.near", "chain": "near", "category": "incident"},
                {"subject": "b.near", "chain": "near", "category": "denylist"},
                {"subject": "c.near", "chain": "near", "category": "organisation"},
                {"subject": "0xabc", "chain": "eth", "category": "incident"},
            ]) + "\n", encoding="utf-8")
            self.assertEqual(score.asserted_subjects(path), {"a.near", "b.near"})


class CommandLineTest(unittest.TestCase):
    def test_prints_a_report_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            holdout = folder / "holdout.jsonl"
            holdout.write_text(json.dumps(row("bad.near", "bad")) + "\n" + json.dumps(row("good.near", "good")) + "\n",
                               encoding="utf-8")
            verdicts = folder / "verdicts"
            verdicts.mkdir()
            (verdicts / "bad.near.json").write_text(json.dumps({"outcome": "DENY"}), encoding="utf-8")
            (verdicts / "good.near.json").write_text(json.dumps({"outcome": "ALLOW"}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "eval/score.py", "--verdicts", str(verdicts), "--holdout", str(holdout)],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("precision (DENY vs bad): 1.000", result.stdout)


if __name__ == "__main__":
    unittest.main()
