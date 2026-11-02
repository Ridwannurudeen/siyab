"""The README claims and adversarial tables are copies of docs/CLAIMS.md and docs/ATTACKS.md.

Each table is compared line for line with its source, and every file, function, test and fixture the
README copy names must exist in the repository.
"""

import re
import unittest
from pathlib import Path

from tests.test_attack_evidence import declared_functions, table_rows

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CLAIMS = ROOT / "docs" / "CLAIMS.md"
ATTACKS = ROOT / "docs" / "ATTACKS.md"
CLAIMS_HEADER = "| Claim | File:function |"
ATTACKS_HEADER = "| # | Attack and command |"
SUMMARY = re.compile(r"^\d+ attempts, \d+ refusals, \d+ bypasses\..*$", re.MULTILINE)


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


if __name__ == "__main__":
    unittest.main()
