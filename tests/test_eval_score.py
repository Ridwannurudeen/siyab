import hashlib
import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import score  # noqa: E402
from intel import labels  # noqa: E402


def row(account, label):
    return {"account": account, "label": label,
            "source_url": "https://example.com/report", "note": "A recorded reason."}


def assertion(subject, category="incident", chain="near"):
    item = {
        "entity": "Synthetic example", "subject": subject, "chain": chain,
        "label": "Synthetic assertion", "category": category, "provenance": "third_party",
        "source": {"url": "https://example.com/report", "content_sha256": "a" * 64,
                   "fetched_at": "2026-11-21T00:00:00Z"},
        "observed_at": "2026-11-21T00:00:00Z", "confidence": 1, "rule_id": None,
        "superseded_by": None, "note": "Synthetic test data.",
    }
    item["id"] = labels.assertion_id(item)
    return item


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
        for number, error in zip((1, 2, 3, 5), errors):
            self.assertIn(f"line {number}:", error)
        self.assertIn("first on line 4", errors[-1])

    def test_accepts_a_clean_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [row("one.near", "bad"), row("two.near", "good")])
            rows, errors = score.load_holdout(path)
        self.assertEqual(errors, [])
        self.assertEqual([item["account"] for item in rows], ["one.near", "two.near"])

    def test_rejects_noncanonical_account_ids_with_line_numbers(self):
        accounts = ("Alice.near", " alice.near", "alice.near ", "a", "a" * 65,
                    "alice..near", "alice/near", "\u00e1lice.near", None, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [row(account, "good") for account in accounts])
            rows, errors = score.load_holdout(path)
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), len(accounts))
        for number, error in enumerate(errors, start=1):
            self.assertIn(f"line {number}: account: not a valid NEAR account id", error)

    def test_mixed_case_is_rejected_without_lowercasing_or_hiding_a_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [row("Alice.near", "good"), row("alice.near", "good"),
                                          row("alice.near", "bad")])
            rows, errors = score.load_holdout(path)
        self.assertEqual([item["account"] for item in rows], ["alice.near"])
        self.assertEqual(len(errors), 2)
        self.assertIn("line 1: account: not a valid NEAR account id: 'Alice.near'", errors[0])
        self.assertIn("line 3: duplicate account alice.near (first on line 2)", errors[1])

    def test_requires_a_source_only_for_bad_and_a_note_only_for_good(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [
                {"account": "one.near", "label": "bad", "source_url": "https://example.com/report"},
                {"account": "two.near", "label": "good", "note": "Synthetic ordinary receiver."},
            ])
            rows, errors = score.load_holdout(path)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)

    def test_rejects_missing_required_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [
                {"account": "one.near", "label": "bad"},
                {"account": "two.near", "label": "good"},
            ])
            rows, errors = score.load_holdout(path)
        self.assertEqual(rows, [])
        self.assertIn("line 1: source_url", errors[0])
        self.assertIn("line 2: note", errors[1])

    def test_rejects_blank_accounts_notes_and_urls_without_a_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [
                row("   ", "bad"),
                {"account": "one.near", "label": "good", "note": " \t"},
                {"account": "two.near", "label": "bad", "source_url": "https://"},
                {"account": "three.near", "label": "bad", "source_url": 10},
            ])
            rows, errors = score.load_holdout(path)
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 4)

    def test_reports_invalid_json_and_non_objects_with_physical_line_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holdout.jsonl"
            path.write_text('\n{}\n[1]\n{"account":\n', encoding="utf-8")
            rows, errors = score.load_holdout(path)
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 3)
        for number, error in zip((2, 3, 4), errors):
            self.assertIn(f"line {number}:", error)

    def test_duplicate_is_rejected_even_when_the_first_row_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [
                {"account": "one.near", "label": "bad"}, row("one.near", "good"),
            ])
            rows, errors = score.load_holdout(path)
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 2)
        self.assertIn("line 2: duplicate account one.near (first on line 1)", errors[1])

    def test_rejects_an_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [])
            rows, errors = score.load_holdout(path)
        self.assertEqual(rows, [])
        self.assertTrue(errors)

    def test_reports_invalid_utf8_with_a_line_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holdout.jsonl"
            path.write_bytes(b"\n\xff\n")
            rows, errors = score.load_holdout(path)
        self.assertEqual(rows, [])
        self.assertIn("line 2:", errors[0])


class VerdictLoaderTest(unittest.TestCase):
    def test_unscoreable_answers_never_count_as_a_decision(self):
        for decision in ("ALLOW", "DENY", "ASK", "UNKNOWN", None):
            with self.subTest(decision=decision):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "synthetic.near.json"
                    path.write_text(json.dumps({"outcome": decision, "scoreable": False}), encoding="utf-8")
                    answers, errors, duplicates = score.load_verdicts(directory)
                self.assertEqual(errors, [])
                self.assertEqual(duplicates, [])
                self.assertEqual(answers, {"synthetic.near": "NOT_JUDGED"})
                report = score.score([row("synthetic.near", "bad")], answers)
                self.assertEqual(report["counts"]["judged"], 0)
                self.assertEqual(report["not_judged_accounts"], ["synthetic.near"])
                self.assertEqual(report["matrix"]["bad"], {"DENY": 0, "ALLOW": 0, "ASK": 0, "NOT_JUDGED": 1})

    def test_explicit_scoreable_answers_keep_their_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.near.json"
            path.write_text(json.dumps({"outcome": "DENY", "scoreable": True}), encoding="utf-8")
            answers, errors, duplicates = score.load_verdicts(directory)
        self.assertEqual(errors, [])
        self.assertEqual(duplicates, [])
        self.assertEqual(answers, {"synthetic.near": "DENY"})

    def test_rejects_nonboolean_scoreability(self):
        for value in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "synthetic.near.json"
                    path.write_text(json.dumps({"outcome": "ALLOW", "scoreable": value}), encoding="utf-8")
                    answers, errors, duplicates = score.load_verdicts(directory)
                self.assertEqual(answers, {})
                self.assertEqual(duplicates, [])
                self.assertEqual(len(errors), 1)
                self.assertIn("scoreable must be a boolean", errors[0])

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

    def test_reads_an_object_subject_from_a_real_evidence_fixture_shape(self):
        document = json.loads((ROOT / "fixtures/registry/sample-allow-evidence.json").read_text(encoding="utf-8"))
        document["subject"]["receiver"] = "synthetic-fixture.near"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence-hash.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            answers, errors, duplicates = score.load_verdicts(directory)
        self.assertEqual(errors, [])
        self.assertEqual(duplicates, [])
        self.assertEqual(answers, {"synthetic-fixture.near": "ALLOW"})

    def test_reads_the_receiver_from_a_service_response_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.json"
            path.write_text(json.dumps({"request": {"receiver_id": "synthetic.near"},
                                        "verdict": None, "outcome": "UNKNOWN", "action": "ask"}), encoding="utf-8")
            answers, errors, _ = score.load_verdicts(directory)
        self.assertEqual(errors, [])
        self.assertEqual(answers, {"synthetic.near": "ASK"})

    def test_allow_maps_to_the_same_column_for_evidence_and_service_responses(self):
        for answer in ({"verdict": "ALLOW"},
                       {"outcome": "ALLOW", "action": "ask", "verdict": None},
                       {"outcome": "ALLOW", "action": "ask", "verdict": {"literal": "ALLOW"}}):
            with self.subTest(answer=answer):
                self.assertEqual(score.outcome_of(answer), "ALLOW")

    def test_rejects_noncanonical_accounts_in_every_explicit_location(self):
        for account in ("Alice.near", "alice..near", "a", "a" * 65, None, 1):
            for answer, field in (({"subject": account}, "subject"),
                                  ({"subject": {"receiver": account}}, "subject.receiver"),
                                  ({"account": account}, "account"),
                                  ({"receiver_id": account}, "receiver_id"),
                                  ({"request": {"receiver_id": account}}, "request.receiver_id")):
                with self.subTest(account=account, field=field):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "alice.near.json"
                        path.write_text(json.dumps(dict(answer, outcome="DENY")), encoding="utf-8")
                        answers, errors, duplicates = score.load_verdicts(directory)
                    self.assertEqual(answers, {})
                    self.assertEqual(duplicates, [])
                    self.assertEqual(len(errors), 1)
                    self.assertIn(f"{field}: not a valid NEAR account id", errors[0])
                    self.assertIn(str(path), errors[0])

    def test_rejects_noncanonical_filename_accounts(self):
        for account in ("Alice.near", "alice..near", "a", "a" * 65):
            with self.subTest(account=account):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / f"{account}.json"
                    path.write_text('{"outcome":"DENY"}', encoding="utf-8")
                    answers, errors, duplicates = score.load_verdicts(directory)
                self.assertEqual(answers, {})
                self.assertEqual(duplicates, [])
                self.assertEqual(len(errors), 1)
                self.assertIn("filename: not a valid NEAR account id", errors[0])
                self.assertIn(str(path), errors[0])

    def test_malformed_explicit_subject_does_not_fall_back_to_the_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.near.json"
            path.write_text(json.dumps({"subject": {"receiver": []}, "verdict": "DENY"}), encoding="utf-8")
            answers, errors, _ = score.load_verdicts(directory)
        self.assertEqual(answers, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("receiver", errors[0])

    def test_normalizes_case_and_unknown_without_treating_other_values_as_allow(self):
        for value, expected in ((" unknown ", "ASK"), (" deny ", "DENY"), ("ask", "ASK"),
                                ("allow", "ALLOW"), ("unavailable", None), (True, None)):
            with self.subTest(value=value):
                self.assertEqual(score.outcome_of({"verdict": value}), expected)

    def test_reports_invalid_json_non_objects_and_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "a.json").write_text("{", encoding="utf-8")
            (folder / "b.json").write_text("[]", encoding="utf-8")
            (folder / "c.json").write_bytes(b"\xff")
            answers, errors, duplicates = score.load_verdicts(folder)
        self.assertEqual(answers, {})
        self.assertEqual(duplicates, [])
        self.assertEqual(len(errors), 3)

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
            row("bad-missing.near", "bad"),
            row("good-deny.near", "good"),
            row("good-allow.near", "good"),
            row("good-ask.near", "good"),
            row("missing.near", "good"),
        ]
        answers = {
            "bad-deny.near": "DENY",
            "bad-allow.near": "ALLOW",
            "bad-ask.near": "ASK",
            "good-deny.near": "DENY",
            "good-allow.near": "ALLOW",
            "good-ask.near": "ASK",
        }
        result = score.score(rows, answers)
        self.assertEqual(result["matrix"]["bad"], {"DENY": 1, "ALLOW": 1, "ASK": 1, "NOT_JUDGED": 1})
        self.assertEqual(result["matrix"]["good"], {"DENY": 1, "ALLOW": 1, "ASK": 1, "NOT_JUDGED": 1})
        self.assertAlmostEqual(result["precision"], 0.5)
        self.assertAlmostEqual(result["recall"], 0.25)
        self.assertEqual(result["confusion"], {"true_positive": 1, "false_positive": 1,
                                               "false_negative": 3, "true_negative": 3})
        self.assertEqual(result["counts"]["ask"], 2)
        self.assertEqual(result["counts"]["not_judged"], 2)
        self.assertEqual(result["not_judged_accounts"], ["bad-missing.near", "missing.near"])

    def test_fewer_denials_never_improve_recall(self):
        rows = [row(f"synthetic-{number}.near", "bad") for number in range(3)]
        for decisions in itertools.product(("DENY", "ALLOW", "ASK", None), repeat=3):
            answers = {item["account"]: decision for item, decision in zip(rows, decisions) if decision is not None}
            result = score.score(rows, answers)
            with self.subTest(decisions=decisions):
                self.assertAlmostEqual(result["recall"], decisions.count("DENY") / 3)
                for account, decision in answers.items():
                    if decision != "DENY":
                        continue
                    for replacement in ("ALLOW", "ASK", None):
                        changed = dict(answers)
                        if replacement is None:
                            del changed[account]
                        else:
                            changed[account] = replacement
                        self.assertLessEqual(score.score(rows, changed)["recall"], result["recall"])

    def test_accounts_outside_the_holdout_are_counted_and_sorted(self):
        result = score.score([row("inside.near", "bad")],
                             {"z.near": "DENY", "inside.near": "DENY", "a.near": "ALLOW"})
        self.assertEqual(result["counts"]["outside_holdout"], 2)
        self.assertEqual(result["outside_holdout_accounts"], ["a.near", "z.near"])
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(result["recall"], 1.0)

    def test_all_missing_rows_remain_in_the_confusion_table(self):
        result = score.score([row("bad.near", "bad"), row("good.near", "good")], {})
        self.assertEqual(result["counts"]["not_judged"], 2)
        self.assertEqual(result["confusion"], {"true_positive": 0, "false_positive": 0,
                                               "false_negative": 1, "true_negative": 1})
        self.assertEqual(result["recall"], 0.0)

    def test_no_bad_rows_does_not_divide_by_zero(self):
        result = score.score([row("good.near", "good")], {"good.near": "DENY"})
        self.assertEqual(result["precision"], 0.0)
        self.assertEqual(result["recall"], 0.0)

    def test_no_decided_bad_row_does_not_divide_by_zero(self):
        result = score.score([row("a.near", "bad")], {"a.near": "ASK"})
        self.assertEqual(result["precision"], 0.0)
        self.assertEqual(result["recall"], 0.0)

    def test_report_names_precision_and_recall(self):
        text = score.format_report(score.score([row("a.near", "bad")], {"a.near": "DENY"}))
        self.assertIn("precision (DENY vs bad): 1.000", text)
        self.assertIn("recall (bad refused):    1.000", text)
        self.assertIn("ASK kept apart:", text)

    def test_report_shows_missing_rows_outside_accounts_and_the_full_recall_denominator(self):
        rows = [row(f"missing-{number}.near", "bad") for number in range(7)]
        rows.append(row("denied.near", "bad"))
        result = score.score(rows, {"denied.near": "DENY", "outside.near": "DENY"})
        text = score.format_report(result)
        self.assertIn("NOT_JUDGED", text)
        self.assertIn("(1/8)", text)
        self.assertIn("false negative", text)
        self.assertIn("missing-6.near", text)
        self.assertIn("outside.near", text)


class LabelStoreTest(unittest.TestCase):

    def test_asserted_subjects_reads_current_incident_and_denylist_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.jsonl"
            path.write_text("\n".join(json.dumps(item) for item in [
                assertion("a.near"), assertion("b.near", "denylist"),
                assertion("c.near", "organisation"), assertion("0xabc", chain="eth"),
            ]) + "\n", encoding="utf-8")
            self.assertEqual(score.asserted_subjects(path), {"a.near", "b.near"})

    def test_superseded_assertions_do_not_exclude_a_receiver(self):
        old = assertion("synthetic.near")
        current = assertion("synthetic.near", "organisation")
        old["superseded_by"] = current["id"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.jsonl"
            path.write_text("\n".join(json.dumps(item) for item in (old, current)) + "\n", encoding="utf-8")
            self.assertEqual(score.asserted_subjects(path), set())

    def test_missing_label_store_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                score.asserted_subjects(Path(directory) / "missing.jsonl")

    def test_empty_and_blank_label_stores_raise(self):
        for content in ("", "\n \t\n"):
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "labels.jsonl"
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "no assertions"):
                        score.asserted_subjects(path)

    def test_malformed_label_store_never_returns_partial_subjects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.jsonl"
            path.write_text(json.dumps(assertion("synthetic.near")) + "\n{\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r":2:"):
                score.asserted_subjects(path)

    def test_invalid_label_schema_and_dangling_replacements_raise(self):
        dangling = assertion("synthetic.near")
        dangling["superseded_by"] = "b" * 64
        for item in ({"subject": "synthetic.near", "chain": "near", "category": "incident"}, dangling):
            with self.subTest(item=item):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "labels.jsonl"
                    path.write_text(json.dumps(item) + "\n", encoding="utf-8")
                    with self.assertRaises(ValueError):
                        score.asserted_subjects(path)


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.holdout = self.folder / "holdout.jsonl"
        self.holdout.write_text(json.dumps(row("bad.near", "bad")) + "\n" + json.dumps(row("good.near", "good")) + "\n",
                                encoding="utf-8")
        self.verdicts = self.folder / "verdicts"
        self.verdicts.mkdir()
        for account, decision in (("bad.near", "DENY"), ("good.near", "ALLOW")):
            (self.verdicts / f"{account}.json").write_text(json.dumps({"outcome": decision}), encoding="utf-8")

    def run_score(self, *args):
        return subprocess.run(
            [sys.executable, "eval/score.py", "--verdicts", str(self.verdicts), "--holdout", str(self.holdout), *args],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )

    def test_prints_a_report_and_exits_zero(self):
        result = self.run_score()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("precision (DENY vs bad): 1.000", result.stdout)
        self.assertIn("holdout sha256: " + hashlib.sha256(self.holdout.read_bytes()).hexdigest(), result.stdout)
        self.assertIn("rules commit: ", result.stdout)

    def test_unscoreable_answer_is_reported_as_not_judged(self):
        (self.verdicts / "bad.near.json").write_text('{"outcome":"DENY","scoreable":false}', encoding="utf-8")
        result = self.run_score("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["counts"]["judged"], 1)
        self.assertEqual(report["not_judged_accounts"], ["bad.near"])
        self.assertEqual(report["recall"], 0.0)

    def test_invalid_scoreability_refuses_to_print_partial_scores(self):
        (self.verdicts / "bad.near.json").write_text('{"outcome":"DENY","scoreable":0}', encoding="utf-8")
        result = self.run_score("--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("scoreable must be a boolean", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_json_includes_exact_file_hash_commit_and_missing_and_outside_rows(self):
        self.holdout.write_bytes((json.dumps(row("bad.near", "bad")) + "\r\n\r\n"
                                 + json.dumps(row("good.near", "good")) + "\r\n").encode("utf-8"))
        (self.verdicts / "bad.near.json").unlink()
        (self.verdicts / "outside.near.json").write_text('{"outcome":"DENY"}', encoding="utf-8")
        result = self.run_score("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        expected_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                         text=True, check=True).stdout.strip()
        self.assertEqual(report["holdout_sha256"], hashlib.sha256(self.holdout.read_bytes()).hexdigest())
        self.assertEqual(report["rules_commit"], expected_commit)
        self.assertEqual(report["not_judged_accounts"], ["bad.near"])
        self.assertEqual(report["outside_holdout_accounts"], ["outside.near"])
        self.assertEqual(report["counts"]["outside_holdout"], 1)
        self.assertEqual(report["confusion"]["false_negative"], 1)
        self.assertEqual(report["recall"], 0.0)

    def test_accepts_an_explicit_rules_commit(self):
        expected = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                  text=True, check=True).stdout.strip()
        result = self.run_score("--json", "--rules-commit", expected)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["rules_commit"], expected)

    def test_invalid_rules_commit_refuses_to_score(self):
        result = self.run_score("--json", "--rules-commit", "synthetic-nonexistent-revision")
        self.assertEqual(result.returncode, 2)
        self.assertIn("rules commit", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_empty_verdict_folder_reports_every_row_as_not_judged(self):
        for path in self.verdicts.iterdir():
            path.unlink()
        result = self.run_score("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["counts"]["judged"], 0)
        self.assertEqual(report["not_judged_accounts"], ["bad.near", "good.near"])
        self.assertEqual(report["confusion"]["false_negative"], 1)

    def test_invalid_verdict_refuses_to_print_partial_scores(self):
        (self.verdicts / "invalid.json").write_text("{", encoding="utf-8")
        result = self.run_score("--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid.json", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_mixed_case_verdict_account_refuses_to_print_scores(self):
        (self.verdicts / "good.near.json").write_text('{"account":"Good.near","outcome":"DENY"}', encoding="utf-8")
        result = self.run_score("--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("account: not a valid NEAR account id: 'Good.near'", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_duplicate_verdicts_refuse_instead_of_picking_a_file(self):
        (self.verdicts / "duplicate.json").write_text('{"account":"bad.near","outcome":"ALLOW"}', encoding="utf-8")
        result = self.run_score("--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_invalid_holdout_refuses_to_print_scores_and_names_the_line(self):
        self.holdout.write_text(json.dumps(row("bad.near", "unsure")) + "\n", encoding="utf-8")
        result = self.run_score("--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("holdout line 1:", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_mixed_case_good_holdout_row_refuses_to_print_scores_and_names_the_line(self):
        self.holdout.write_text(json.dumps(row("bad.near", "bad")) + "\n\n"
                                + json.dumps(row("Good.near", "good")) + "\n", encoding="utf-8")
        result = self.run_score("--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("holdout line 3: account: not a valid NEAR account id: 'Good.near'", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_missing_label_store_refuses_to_print_scores(self):
        result = self.run_score("--exclude-labelled", "--labels", str(self.folder / "missing.jsonl"), "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("label store", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_empty_and_blank_label_stores_refuse_to_print_scores(self):
        for content in ("", "\n \t\n"):
            with self.subTest(content=content):
                path = self.folder / "labels.jsonl"
                path.write_text(content, encoding="utf-8")
                result = self.run_score("--exclude-labelled", "--labels", str(path), "--json")
                self.assertEqual(result.returncode, 2)
                self.assertIn("label store", result.stderr)
                self.assertIn("no assertions", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_malformed_label_store_refuses_to_print_scores(self):
        path = self.folder / "labels.jsonl"
        path.write_text(json.dumps(assertion("bad.near")) + "\n{\n", encoding="utf-8")
        result = self.run_score("--exclude-labelled", "--labels", str(path), "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("label store", result.stderr)
        self.assertIn(":2:", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_exclusion_honors_supersession_without_misreporting_removed_rows_as_outside(self):
        old = assertion("good.near")
        current = assertion("good.near", "organisation")
        old["superseded_by"] = current["id"]
        path = self.folder / "labels.jsonl"
        path.write_text("\n".join(json.dumps(item) for item in (assertion("bad.near"), old, current)) + "\n",
                        encoding="utf-8")
        (self.verdicts / "outside.near.json").write_text('{"outcome":"ASK"}', encoding="utf-8")
        result = self.run_score("--exclude-labelled", "--labels", str(path), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        extra = report["excluding_label_store"]
        self.assertEqual(extra["excluded_rows"], 1)
        self.assertEqual(extra["report"]["counts"]["rows"], 1)
        self.assertEqual(extra["report"]["outside_holdout_accounts"], ["outside.near"])
        self.assertEqual(report["outside_holdout_accounts"], ["outside.near"])

    def test_text_exclusion_prints_both_reports(self):
        path = self.folder / "labels.jsonl"
        path.write_text(json.dumps(assertion("bad.near")) + "\n", encoding="utf-8")
        result = self.run_score("--exclude-labelled", "--labels", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("without 1 rows", result.stdout)
        self.assertEqual(result.stdout.count("precision (DENY vs bad):"), 2)

    def test_unused_label_path_is_not_read(self):
        result = self.run_score("--labels", str(self.folder / "missing.jsonl"), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)


class HoldoutIgnoreTest(unittest.TestCase):
    def test_default_and_other_private_jsonl_paths_are_ignored(self):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "eval/holdout/receivers.jsonl", "eval/holdout/synthetic.jsonl",
             "eval/holdout/README.md", "eval/score.py"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["eval/holdout/receivers.jsonl", "eval/holdout/synthetic.jsonl"])


if __name__ == "__main__":
    unittest.main()
