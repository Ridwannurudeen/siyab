"""The README table that maps the phrases of NEAR's track description to code, tests and testnet evidence.

The table has six rows in the track's order; every file:function and test it names must be defined in
the named file, every path it cites must exist, and every explorer link must be one that docs/ATTACKS.md,
docs/EVIDENCE.md or docs/TESTNET.md already gives.
"""

import re
import unittest
from pathlib import Path

from tests.test_attack_evidence import declared_functions, table_rows
from tests.test_readme_claims import code_spans

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
EVIDENCE_DOCUMENTS = ("docs/ATTACKS.md", "docs/EVIDENCE.md", "docs/TESTNET.md")
HEADER = "| Track phrase |"
PHRASES = (
    '"spending limits"',
    '"policy engines"',
    '"sandboxed wallets"',
    '"reputation"',
    '"scam detection"',
    '"Give AI funds without giving it the keys to everything"',
)


class ReadmeTrackMapTest(unittest.TestCase):
    def setUp(self):
        self.rows = table_rows(README.read_text(encoding="utf-8"), HEADER)

    def test_six_rows_quote_the_track_phrases_in_order(self):
        self.assertEqual([row[0] for row in self.rows], list(PHRASES))
        for row in self.rows:
            self.assertEqual(len(row), 5, row[0])

    def test_every_code_reference_is_a_function_defined_in_its_file(self):
        self.assertEqual(len(self.rows), 6)
        for row in self.rows:
            references = code_spans(row[2])
            self.assertTrue(references, row[0])
            for reference in references:
                with self.subTest(reference=reference):
                    path_text, function = reference.rsplit(":", 1)
                    path = ROOT / path_text
                    self.assertTrue(path.is_file(), path_text)
                    self.assertIn(function, declared_functions(path))

    def test_every_test_reference_is_defined_in_its_file(self):
        self.assertEqual(len(self.rows), 6)
        for row in self.rows:
            references = code_spans(row[3])
            self.assertTrue(references, row[0])
            for reference in references:
                with self.subTest(reference=reference):
                    path_text, name = reference.split("::", 1)
                    path = ROOT / path_text
                    self.assertTrue(path.is_file(), path_text)
                    expected = name.split("::")[-1] if path.suffix == ".rs" else name
                    self.assertIn(expected, declared_functions(path))

    def test_every_explorer_link_is_given_by_an_evidence_document(self):
        documents = "".join((ROOT / name).read_text(encoding="utf-8") for name in EVIDENCE_DOCUMENTS)
        self.assertEqual(len(self.rows), 6)
        for row in self.rows:
            cell = row[4]
            links = re.findall(r"https://[^) ]+", cell)
            paths = code_spans(cell)
            self.assertTrue(cell == "offline" or links or paths, row[0])
            for link in links:
                with self.subTest(link=link):
                    self.assertIn(link, documents)
            for path_text in paths:
                with self.subTest(path=path_text):
                    self.assertTrue((ROOT / path_text).exists(), path_text)


if __name__ == "__main__":
    unittest.main()
