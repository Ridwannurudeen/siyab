"""scripts/gen_idea.py: the idea text from the claims table, its bounds, the drop order and the full name."""

import contextlib
import importlib.util
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HEADER = "| Claim | File:function | Test | Evidence | idea |\n|---|---|---|---|---|\n"
SOURCE = "`gate/rules.py:judge`"
TEST = "`tests/test_rules.py::OutputsTest.test_missing_fixture_is_unknown_never_a_pass`"


def load_gen_idea():
    spec = importlib.util.spec_from_file_location("gen_idea", ROOT / "scripts" / "gen_idea.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_idea = load_gen_idea()


def table(claims, idea="yes", source=SOURCE, test=TEST):
    return "# Claims\n\n" + HEADER + "".join(f"| {claim} | {source} | {test} | offline | {idea} |\n"
                                             for claim in claims)


def filler(characters):
    """Claims with no drop order word, about characters long in all."""
    return [f"Filler claim {index} holds plain words only." for index in range(characters // 42 + 1)]


TOPIC_CLAIMS = {
    "the feed": "The feed reports new events.",
    "the trace": "The exit trace follows one flow.",
    "the scanner": "The key scan lists access keys.",
    "the reproducible build": "The reproducible build matches the hash.",
    "lookalike names": "A lookalike name is refused.",
}


class IdeaTextTest(unittest.TestCase):
    def run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = gen_idea.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def claims_file(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "CLAIMS.md"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_the_structure_follows_the_rows_in_table_order(self):
        rows = gen_idea.idea_rows(table(["First `code` claim.", "Second claim."]) +
                                  "| Not for the idea. | x | y | offline | no |\n")
        self.assertEqual([row["claim"] for row in rows], ["First `code` claim.", "Second claim."])
        text, dropped = gen_idea.build(rows)
        self.assertEqual(dropped, [])
        self.assertTrue(text.startswith(gen_idea.ONE_LINE + " " + gen_idea.HEADLINE + " " + gen_idea.PROBLEM))
        self.assertIn(f"{gen_idea.BUILT} First code claim. Second claim. {gen_idea.HOW_NEAR} {gen_idea.OFFLINE}",
                      text)
        self.assertTrue(text.endswith(gen_idea.OFFLINE))
        self.assertNotIn("Not for the idea.", text)

    def test_the_length_bounds_are_20_to_2000_characters(self):
        self.assertIsNone(gen_idea.length_problem("x" * 20))
        self.assertIsNone(gen_idea.length_problem("x" * 2000))
        self.assertIn("19 characters", gen_idea.length_problem("x" * 19))
        self.assertIn("2001 characters", gen_idea.length_problem("x" * 2001))

    def test_a_text_in_bounds_prints_and_exits_0_with_its_length(self):
        path = self.claims_file(table(["One claim."]))
        code, stdout, stderr = self.run_main(["--claims", path])
        self.assertEqual(code, 0)
        text = stdout.rstrip("\n")
        self.assertEqual(stderr, f"idea: {len(text)} characters\n")
        self.assertLessEqual(len(text), 2000)
        code, stdout, _ = self.run_main(["--check", "--claims", path])
        self.assertEqual((code, stdout), (0, f"idea: {len(text)} characters\n"))

    def test_nothing_is_dropped_while_the_text_fits(self):
        rows = gen_idea.idea_rows(table(list(TOPIC_CLAIMS.values())))
        text, dropped = gen_idea.build(rows)
        self.assertEqual(dropped, [])
        for claim in TOPIC_CLAIMS.values():
            self.assertIn(claim, text)

    def test_rows_are_dropped_in_the_section_order_until_the_text_fits(self):
        topics = list(TOPIC_CLAIMS.values())
        rows = gen_idea.idea_rows(table(filler(300) + topics))
        order = [name for name, _ in gen_idea.DROP_ORDER]
        self.assertEqual(list(TOPIC_CLAIMS), order)
        for count in range(len(topics) + 1):
            with self.subTest(dropped=count):
                kept = [row for row in rows if row["claim"] not in topics[:count]]
                limit = len(gen_idea.assemble(kept))
                with mock.patch.object(gen_idea, "MAX_CHARS", limit):
                    text, dropped = gen_idea.build(rows)
                self.assertEqual(dropped, order[:count])
                self.assertEqual(len(text), limit)
                for claim in topics[:count]:
                    self.assertNotIn(claim, text)
                for claim in topics[count:]:
                    self.assertIn(claim, text)

    def test_a_long_feed_row_is_the_first_dropped_at_2000_characters(self):
        long_feed = "The feed " + "reports one more event " * 80 + "."
        rows = gen_idea.idea_rows(table(["A lookalike name is refused.", long_feed, "The key scan lists keys."]))
        self.assertGreater(len(gen_idea.assemble(rows)), 2000)
        text, dropped = gen_idea.build(rows)
        self.assertEqual(dropped, ["the feed"])
        self.assertIn("A lookalike name is refused.", text)
        self.assertIn("The key scan lists keys.", text)
        self.assertIsNone(gen_idea.length_problem(text))

    def test_a_text_still_too_long_after_the_drop_order_exits_1(self):
        path = self.claims_file(table(filler(2100)))
        code, _, stderr = self.run_main(["--claims", path])
        self.assertEqual(code, 1)
        self.assertIn("outside 20 to 2000", stderr)

    def test_check_exits_1_for_a_row_without_a_test_or_with_a_missing_one(self):
        cases = (
            ("", "no test named"),
            ("`tests/test_rules.py::OutputsTest.no_such_test`", "no test 'OutputsTest.no_such_test'"),
            ("`tests/no_such_file.py::T.test`", "no test file"),
            ("`contracts/gate/src/lib.rs::tests::no_such_test`", "no test 'tests::no_such_test'"),
        )
        for test, expected in cases:
            with self.subTest(test=test):
                path = self.claims_file(table(["A claim."], test=test))
                code, stdout, _ = self.run_main(["--check", "--claims", path])
                self.assertEqual(code, 1)
                self.assertIn(expected, stdout)
        path = self.claims_file(table(["A claim."], source="`gate/rules.py:no_such_function`"))
        code, stdout, _ = self.run_main(["--check", "--claims", path])
        self.assertEqual(code, 1)
        self.assertIn("no function 'no_such_function'", stdout)

    def test_every_idea_row_of_the_committed_table_names_existing_code_and_a_test(self):
        rows = gen_idea.idea_rows((ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8"))
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(claim=row["claim"]):
                self.assertEqual(gen_idea.row_problems(row), [])

    def test_the_full_name_is_never_shortened(self):
        rows = gen_idea.idea_rows((ROOT / "docs" / "CLAIMS.md").read_text(encoding="utf-8"))
        for text in (gen_idea.assemble(rows), gen_idea.build(rows)[0], gen_idea.assemble([])):
            self.assertIn("ShieldBot for NEAR", text)
            self.assertIsNone(re.search(r"Shield(?!Bot)", text, re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
