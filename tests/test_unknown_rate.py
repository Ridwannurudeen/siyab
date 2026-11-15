import copy
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import counts

ROOT = Path(__file__).resolve().parent.parent
COUNTS = ROOT / "scripts" / "counts.py"


class UnknownRateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.row = counts.unknown_rate_row()

    def test_committed_row_is_reproducible(self):
        rows = [json.loads(line) for line in counts.UNKNOWN_RATE.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], self.row)
        self.assertEqual(rows[1], counts.unknown_rate_row("7f624c8ac"))
        self.assertEqual(rows[1]["observed"], "2026-11-09")
        self.assertEqual(rows[1]["commit_date"], "2026-11-15")
        self.assertEqual(rows[1]["sample_id"], self.row["sample_id"])
        self.assertEqual(rows[1]["totals"], {"ALLOW": 831, "DENY": 19, "UNKNOWN": 140, "total": 990})
        self.assertEqual(rows[1]["causes"], {"account_age": 138, "code_without_metadata": 2,
                                           "dust_funded": 138})
        self.assertEqual(rows[2], counts.unknown_rate_row("7d2b8134b"))
        self.assertEqual(rows[2]["observed"], "2026-11-09")
        self.assertEqual(rows[2]["commit_date"], "2026-11-15")
        self.assertEqual(rows[2]["sample_id"], self.row["sample_id"])
        self.assertEqual(rows[2]["totals"], {"ALLOW": 850, "DENY": 138, "UNKNOWN": 2, "total": 990})
        self.assertEqual(rows[2]["causes"], {"code_without_metadata": 2})
        self.assertEqual(self.row["observed"], "2026-11-09")
        self.assertEqual(self.row["commit_date"], "2026-11-14")
        self.assertNotIn("date", self.row)
        commit = subprocess.run(["git", "rev-parse", "hackathon-submission^{commit}"], cwd=ROOT,
                                check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(self.row["commit"], commit)
        sample = json.loads(subprocess.run(
            ["git", "show", "hackathon-submission:fixtures/receivers/top.json"], cwd=ROOT,
            check=True, capture_output=True, text=True).stdout)
        canonical = json.dumps(sorted(entry["subject"] for entry in sample["subjects"]),
                               separators=(",", ":")).encode("utf-8")
        self.assertEqual(self.row["sample_id"], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(self.row["totals"], {"ALLOW": 270, "DENY": 1, "UNKNOWN": 719, "total": 990})
        self.assertEqual(self.row["causes"], {"issuer_blacklist": 560, "account_age": 545,
                                            "dust_funded": 545, "code_without_metadata": 255,
                                            "receiver_exists": 248})

    def archived_row(self, sample, judgements):
        archive = io.BytesIO()
        files = {"fixtures/receivers/top.json": sample}
        files.update({f"fixtures/receivers/top/judgements/{row['subject']}.json": row for row in judgements})
        with tarfile.open(fileobj=archive, mode="w") as tree:
            for name, document in files.items():
                data = json.dumps(document).encode("utf-8")
                member = tarfile.TarInfo(name)
                member.size = len(data)
                tree.addfile(member, io.BytesIO(data))

        def run(command, **kwargs):
            if command[1] == "rev-parse":
                output = "ab" * 20 + "\n"
            elif command[1] == "show":
                output = "2026-11-15T00:39:15+02:00\n"
            else:
                self.assertEqual(command[1], "archive")
                output = archive.getvalue()
            return subprocess.CompletedProcess(command, 0, stdout=output)

        with mock.patch.object(counts.subprocess, "run", side_effect=run):
            return counts.unknown_rate_row()

    def test_sample_id_uses_only_the_sorted_subjects_and_commit_date_is_utc(self):
        sample = {"chosen_at": "2026-11-09T01:00:00Z", "subjects": [
            {"subject": "second.near", "judged_at_ms": 1},
            {"subject": "first.near", "judged_at_ms": 2}]}
        judgements = [{"subject": entry["subject"], "judged_at_ms": 3, "verdict": "UNKNOWN",
                       "observed": {"timestamp_ms": 1794182400000},
                       "checks": [{"id": "account_age", "status": "unknown"}]}
                      for entry in sample["subjects"]]
        first = self.archived_row(sample, judgements)
        changed = copy.deepcopy(sample)
        changed["chosen_at"] = "2026-11-15T01:00:00Z"
        changed["subjects"].reverse()
        for entry in changed["subjects"]:
            entry["judged_at_ms"] = 4
        later_judgements = [dict(row, judged_at_ms=5) for row in judgements]
        later = self.archived_row(changed, later_judgements)
        self.assertEqual(first, later)
        self.assertEqual(first["commit_date"], "2026-11-14")
        canonical = b'["first.near","second.near"]'
        self.assertEqual(first["sample_id"], hashlib.sha256(canonical).hexdigest())
        changed["subjects"][0]["subject"] = "third.near"
        later_judgements[1]["subject"] = "third.near"
        self.assertNotEqual(first["sample_id"], self.archived_row(changed, later_judgements)["sample_id"])

    def test_append_preserves_every_previous_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.jsonl"
            original = (json.dumps(self.row, separators=(", ", ": ")) + "\r\n").encode()
            path.write_bytes(original)
            later = dict(self.row, observed="2026-11-15")
            counts.append_unknown_rate(path, later)
            self.assertEqual(path.read_bytes()[:len(original)], original)
            self.assertEqual([json.loads(line) for line in path.read_bytes().splitlines()], [self.row, later])

    def test_duplicate_sample_observed_and_commit_is_refused_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.jsonl"
            counts.append_unknown_rate(path, self.row, new_series=True)
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "already recorded"):
                counts.append_unknown_rate(path, dict(self.row, causes={}))
            self.assertEqual(path.read_bytes(), original)

    def test_same_observed_date_with_a_different_commit_can_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.jsonl"
            counts.append_unknown_rate(path, self.row, new_series=True)
            later = dict(self.row, commit="cd" * 20, commit_date="2026-11-15")
            counts.append_unknown_rate(path, later)
            same_date = dict(later, commit="ef" * 20)
            counts.append_unknown_rate(path, same_date)
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "already recorded"):
                counts.append_unknown_rate(path, later)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual([json.loads(line) for line in original.splitlines()], [self.row, later, same_date])

    def test_earlier_commit_or_observed_date_is_refused_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.jsonl"
            counts.append_unknown_rate(path, self.row, new_series=True)
            last = dict(self.row, observed="2026-11-15", commit="cd" * 20, commit_date="2026-11-16")
            counts.append_unknown_rate(path, last)
            original = path.read_bytes()
            for changed in (dict(last, commit="ef" * 20, commit_date="2026-11-15"),
                            dict(last, observed="2026-11-14")):
                with self.subTest(row=changed), self.assertRaisesRegex(ValueError, "earlier"):
                    counts.append_unknown_rate(path, changed)
                self.assertEqual(path.read_bytes(), original)

    def test_changed_sample_requires_an_explicit_new_series(self):
        changed = dict(self.row, sample_id="ab" * 32, observed="2026-11-15")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.jsonl"
            with self.assertRaisesRegex(ValueError, "new series"):
                counts.append_unknown_rate(path, self.row)
            counts.append_unknown_rate(path, self.row, new_series=True)
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "sample id"):
                counts.append_unknown_rate(path, changed)
            with self.assertRaisesRegex(ValueError, "empty"):
                counts.append_unknown_rate(path, changed, new_series=True)
            self.assertEqual(path.read_bytes(), original)
            other = Path(tmp) / "other.jsonl"
            counts.append_unknown_rate(other, changed, new_series=True)
            self.assertEqual(json.loads(other.read_bytes()), changed)

    def test_output_has_one_entry_per_row_with_both_dates(self):
        first = "UNKNOWN on the pinned 990: 73% (reads of 9 Nov, rules of 14 Nov)"
        self.assertEqual(counts.unknown_rate_line(),
                         first + "; 14% (reads of 9 Nov, rules of 15 Nov); 0.2% (reads of 9 Nov, rules of 15 Nov).")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.jsonl"
            counts.append_unknown_rate(path, self.row, new_series=True)
            counts.append_unknown_rate(path, dict(self.row, observed="2026-11-15", commit_date="2026-11-16",
                                                totals={"ALLOW": 494, "DENY": 1, "UNKNOWN": 495,
                                                        "total": 990}))
            self.assertEqual(counts.unknown_rate_line(path),
                             first + "; 50% (reads of 15 Nov, rules of 16 Nov).")
            counts.append_unknown_rate(path, dict(self.row, observed="2026-11-16", commit_date="2026-11-17",
                                                totals={"ALLOW": 990, "DENY": 0, "UNKNOWN": 0,
                                                        "total": 990}))
            self.assertEqual(counts.unknown_rate_line(path),
                             first + "; 50% (reads of 15 Nov, rules of 16 Nov); 0% (reads of 16 Nov, rules of 17 Nov).")

    def test_cli_exposes_the_line_only_through_its_own_flag(self):
        result = subprocess.run([sys.executable, str(COUNTS), "--unknown-rate"], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, counts.unknown_rate_line() + "\n")
        with mock.patch.object(counts, "unknown_rate_line", side_effect=AssertionError("unexpected series read")):
            self.assertFalse(any(line.startswith("UNKNOWN on the pinned ") for line in counts.lines()))

    def test_cli_prints_the_reproducible_row(self):
        result = subprocess.run([sys.executable, str(COUNTS), "--unknown-rate-row"], cwd=ROOT,
                                check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), self.row)

    def test_cli_append_refuses_a_duplicate_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "series.jsonl"
            command = [sys.executable, str(COUNTS), "--append-unknown-rate", str(path)]
            subprocess.run(command + ["--new-series"], cwd=ROOT, check=True, capture_output=True)
            original = path.read_bytes()
            duplicate = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn("already recorded", duplicate.stderr)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
