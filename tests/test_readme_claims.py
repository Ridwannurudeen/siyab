"""The README claims and adversarial tables are copies of docs/CLAIMS.md and docs/ATTACKS.md, and the README
first screen states what is enforced and where.

Each table is compared line for line with its source, and every file, function, test and fixture the
README copy names must exist in the repository. The first screen is the headline, the one line and the
paragraph before the first second level heading.
"""

import re
import unittest
from pathlib import Path

from intel import feed, labels
from tests.test_attack_evidence import declared_functions, table_rows

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CLAIMS = ROOT / "docs" / "CLAIMS.md"
ATTACKS = ROOT / "docs" / "ATTACKS.md"
TESTNET = ROOT / "docs" / "TESTNET.md"
CLAIMS_HEADER = "| Claim | File:function |"
ATTACKS_HEADER = "| # | Attack and command |"
SUMMARY = re.compile(r"^\d+ attempts, \d+ refusals, \d+ bypasses\..*$", re.MULTILINE)
HEADLINE = "# ShieldBot for NEAR: security intelligence with a source on every row, enforced on chain."
ONE_LINE = "**Every label has a source. Every verdict has evidence. Unknown never passes.**"
RANKING = ("number one", "first on", "the first to")
SHIPPED = ("intel/registries.py:rows", "gate/rules.py:_issuer_check", "intel/trace.py:trace",
           "intel/cluster.py:build_cluster", "intel/feed.py:follow", "intel/render.py:account_pages",
           "contracts/registry/src/lib.rs:record_label")
NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def table_lines(text, header):
    """The header line of the table that starts with header and every table line after it."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith(header)]
    if len(starts) != 1:
        raise AssertionError(f"expected one table starting {header!r}, found {len(starts)}")
    table = []
    for line in lines[starts[0]:]:
        if not line.startswith("|"):
            break
        table.append(line)
    return table


def code_spans(cell):
    return re.findall(r"`([^`]+)`", cell)


def first_screen(text):
    """The blocks of the README before its first second level heading."""
    return text[:text.index("\n## ")].split("\n\n")


class ReadmeTablesTest(unittest.TestCase):
    def setUp(self):
        self.readme = README.read_text(encoding="utf-8")

    def assert_test_exists(self, reference):
        path_text, name = reference.split("::", 1)
        path = ROOT / path_text
        self.assertTrue(path.is_file(), path_text)
        expected = name.split("::")[-1] if path.suffix == ".rs" else name
        self.assertIn(expected, declared_functions(path), reference)

    def test_the_claims_table_equals_the_claims_document_line_for_line(self):
        source = table_lines(CLAIMS.read_text(encoding="utf-8"), CLAIMS_HEADER)
        copy = table_lines(self.readme, CLAIMS_HEADER)
        self.assertGreater(len(source), 2)
        self.assertEqual(copy, source)

    def test_the_adversarial_table_equals_the_attacks_document_line_for_line(self):
        source = table_lines(ATTACKS.read_text(encoding="utf-8"), ATTACKS_HEADER)
        copy = table_lines(self.readme, ATTACKS_HEADER)
        self.assertGreater(len(source), 2)
        self.assertEqual(copy, source)

    def test_the_attack_counts_line_equals_the_attacks_document(self):
        source = SUMMARY.findall(ATTACKS.read_text(encoding="utf-8"))
        self.assertEqual(len(source), 1)
        self.assertEqual(SUMMARY.findall(self.readme), source)

    def test_every_claim_row_names_existing_code_and_tests(self):
        rows = table_rows(self.readme, CLAIMS_HEADER)
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(claim=row[0]):
                path_text, function = row[1].strip("`").rsplit(":", 1)
                path = ROOT / path_text
                self.assertTrue(path.is_file(), path_text)
                self.assertIn(function, declared_functions(path), row[1])
                self.assert_test_exists(row[2].strip("`"))
                for item in (value.strip().strip("`") for value in row[3].split(",")):
                    self.assertTrue(item == "offline" or (ROOT / item).exists(), item)

    def test_every_attack_row_names_existing_fixtures_and_tests(self):
        rows = table_rows(self.readme, ATTACKS_HEADER)
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(attack=row[1]):
                fixtures = code_spans(row[5])
                self.assertTrue(fixtures)
                for fixture in fixtures:
                    self.assertTrue((ROOT / fixture).exists(), fixture)
                tests = code_spans(row[7])
                self.assertTrue(tests)
                for reference in tests:
                    self.assert_test_exists(reference)


class ShippedRowsTest(unittest.TestCase):
    def setUp(self):
        self.rows = {row[1].strip("`"): row for row in table_rows(CLAIMS.read_text(encoding="utf-8"), CLAIMS_HEADER)}

    def test_every_shipped_intelligence_item_has_a_claims_row(self):
        for source in SHIPPED:
            with self.subTest(source=source):
                self.assertIn(source, self.rows)

    def test_the_feed_row_counts_the_signals_the_feed_defines(self):
        self.assertIn(f"its {NUMBER_WORDS[len(feed.SIGNALS)]} signals", self.rows["intel/feed.py:follow"][0])


class FirstScreenTest(unittest.TestCase):
    def setUp(self):
        self.blocks = first_screen(README.read_text(encoding="utf-8"))

    def test_the_headline_and_the_one_line_open_the_readme_before_the_layers(self):
        self.assertEqual(self.blocks[:2], [HEADLINE, ONE_LINE])
        self.assertTrue(self.blocks[2].startswith("ShieldBot for NEAR is a security intelligence layer for NEAR: a "
                                                  "label store in which every assertion names its provenance class "
                                                  "and its source"))

    def test_the_guard_sentence_says_what_is_enforced_on_chain_and_where(self):
        guard = re.search(r"Its first consumer is a guard: [^.]*\.", self.blocks[2]).group(0)
        for words in ("in a preflight before signing", "a NEAR contract on testnet",
                      "pays only on a signed ALLOW bound to that exact transfer"):
            with self.subTest(words=words):
                self.assertIn(words, guard)
        self.assertNotRegex(guard, r"\bevery\b|\beach verdict\b")

    def test_the_registry_sentence_states_the_testnet_total_and_the_payments_without_a_record(self):
        total = re.findall(r"`total_records` (\d+)", TESTNET.read_text(encoding="utf-8"))[-1]
        self.assertIn(f"The testnet registry holds {total} verdict records: ", self.blocks[2])
        self.assertIn(
            "the DENYs of the lookalike, the aged payee and the collector account named in the Rhea Lend incident report",
            self.blocks[2],
        )
        self.assertIn("the first payment and the replay attack's first send have no record, because their evidence "
                      "documents were not kept", self.blocks[2])

    def test_the_first_screen_states_no_rank_and_names_no_host(self):
        screen = "\n\n".join(self.blocks).lower()
        for phrase in RANKING:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, screen)
        self.assertNotIn("://", screen)


class TryItOfflineTest(unittest.TestCase):
    def test_the_step_7_sample_is_the_line_intel_counts_prints(self):
        text = README.read_text(encoding="utf-8")
        step = text[text.index("7. `python -m intel counts`"):]
        start = step.index("   ```\n") + len("   ```\n")
        self.assertEqual(step[start:step.index("\n", start)].strip(), labels.counts_line(labels.load().counts()))


if __name__ == "__main__":
    unittest.main()
