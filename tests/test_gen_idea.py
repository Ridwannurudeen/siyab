"""scripts/gen_idea.py: the idea text from the counts and the claims table, its bounds, the drop order and the full
name."""

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
    "the latency lines": "The verdict latency is measured.",
    "the scanner": "The key scanner lists access keys.",
    "the reproducible build": "The reproducible build matches the hash.",
    "lookalike names": "A lookalike name is refused.",
    "the feed": "The feed reports new events.",
    "the trace": "The exit trace follows one flow.",
}
COUNT_LINES = [
    "On 52 recorded mainnet receivers the rules returned 32 ALLOW, 19 DENY, 1 Unknown.",
    "Label store: 412 assertions over 262 subjects (onchain 40, official_source 350, third_party 18, inferred 4).",
    "Incidents: 3 pages.",
    ("Feed: since 2026-10-29, 300 blocks, 0 events (0 code_hash_matches_incident, 0 fresh_dust_funded_pays_labelled, "
     "0 watch_account_key_added)."),
    "Flows: 1 exit traces, 200 hops.",
    "Latency: p50 1843 ms, p95 2300 ms over 20 verdict runs.",
]
COUNT_PROSE = [
    ("The label store holds 412 assertions over 262 subjects: onchain 40, official_source 350, third_party 18, "
     "inferred 4."),
    "On 52 recorded mainnet receivers the rules return 32 ALLOW, 19 DENY, 1 Unknown.",
    "Incident pages: 3.",
    "The block feed has read 300 blocks since 2026-10-29 and raised 0 events.",
    "The flow trace holds 1 exit trace over 200 hops.",
    "Verdict latency: p50 1843 ms, p95 2300 ms over 20 recorded runs.",
]


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
        text, dropped = gen_idea.build(gen_idea.count_rows(COUNT_LINES) + rows)
        self.assertEqual(dropped, [])
        self.assertEqual(text.split("\n\n"), [
            gen_idea.PROBLEM,
            " ".join([gen_idea.BUILT] + COUNT_PROSE + ["First code claim.", "Second claim."]),
            f"{gen_idea.HOW_NEAR} {gen_idea.OFFLINE}",
        ])
        self.assertEqual(len(re.findall(r"[.!?](?= |$)", gen_idea.PROBLEM)), 2)
        self.assertNotIn("Not for the idea.", text)

    def test_the_counts_lines_become_prose_by_a_fixed_template(self):
        self.assertEqual([row["claim"] for row in gen_idea.count_rows(COUNT_LINES)], COUNT_PROSE)
        self.assertEqual([row["claim"] for row in gen_idea.count_rows(["Feed: not run.", "Latency: not measured.",
                                                                       "Flows: 2 exit traces, 1 hops."])],
                         ["The block feed has not run.", "The flow trace holds 2 exit traces over 1 hop.",
                          "Verdict latency is not measured."])
        with self.assertRaises(ValueError) as raised:
            gen_idea.count_rows(["Receivers: a line in a format no template knows."])
        self.assertIn("no prose template for the counts line", str(raised.exception))
        self.assertEqual(len(gen_idea.count_rows(gen_idea.counts.lines())), 6)

    def test_the_first_300_characters_name_the_label_store_and_the_guard(self):
        code, stdout, _ = self.run_main([])
        self.assertEqual(code, 0)
        first = stdout[:300]
        for word in ("label store", "provenance", "incident", "guard"):
            with self.subTest(word=word):
                self.assertIn(word, first)

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

    def test_dropping_the_scanner_keeps_the_mcp_sentence(self):
        mcp_claim = "The MCP server exposes the guard, EVM quote and account scan tools, and a scanner tool."
        rows = gen_idea.idea_rows(table(filler(300) + [mcp_claim, TOPIC_CLAIMS["the scanner"],
                                                        "Run python -m gate scan on an account."]))
        self.assertIsNone(gen_idea.topic(mcp_claim))
        self.assertIsNone(gen_idea.topic("The MCP server exposes the guard, EVM quote and account scan tools."))
        self.assertEqual(gen_idea.topic("Run python -m gate scan on an account."), "the scanner")
        kept = [row for row in rows if gen_idea.topic(row["claim"]) != "the scanner"]
        with mock.patch.object(gen_idea, "MAX_CHARS", len(gen_idea.assemble(kept))):
            text, dropped = gen_idea.build(rows)
        self.assertEqual(dropped, ["the latency lines", "the scanner"])
        self.assertIn(mcp_claim, text)
        self.assertNotIn(TOPIC_CLAIMS["the scanner"], text)
        self.assertNotIn("Run python -m gate scan on an account.", text)

    def test_a_long_feed_row_is_the_first_dropped_at_2000_characters(self):
        long_feed = "The feed " + "reports one more event " * 80 + "."
        rows = gen_idea.idea_rows(table(["A lookalike name is refused.", long_feed, "The key scan lists keys."]))
        self.assertGreater(len(gen_idea.assemble(rows)), 2000)
        text, dropped = gen_idea.build(rows)
        self.assertEqual(dropped, ["the latency lines", "the scanner", "the reproducible build", "lookalike names",
                                   "the feed"])
        self.assertNotIn("A lookalike name is refused.", text)
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
