"""Checks for stale and missing documentation citations."""

import tempfile
import unittest
from pathlib import Path

from scripts import check_citations

ROOT = Path(__file__).resolve().parent.parent


class CitationCheckerTest(unittest.TestCase):
    def scan_temp_repo(self, markdown, source='def example():\n    return "present"\n'):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("# Temp repo\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "guide.md").write_text(markdown + "\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "example.py").write_text(source, encoding="utf-8")
            return check_citations.scan(root)

    def test_real_tree_passes(self):
        self.assertEqual(check_citations.scan(ROOT), [])

    def test_adjacent_identifier_on_cited_line_passes(self):
        self.assertEqual(self.scan_temp_repo("`example` (`src/example.py:1`)"), [])

    def test_parenthetical_identifier_on_cited_line_passes(self):
        self.assertEqual(self.scan_temp_repo("(`example`, `src/example.py:1`)"), [])

    def test_adjacent_identifier_missing_from_cited_line_fails(self):
        problems = self.scan_temp_repo("`different` (`src/example.py:1`)")
        self.assertEqual(problems, [
            ("docs/guide.md", 1, "citation", "different not in cited lines")
        ])

    def test_identifier_on_last_line_of_range_passes(self):
        source = "# first line\ndef example():\n    return \"present\"\n"
        self.assertEqual(
            self.scan_temp_repo("`example` (`src/example.py:1-2`)", source), []
        )

    def test_identifier_on_continuation_line_passes(self):
        source = "# first line\ndef example():\n    return \"present\"\n"
        self.assertEqual(
            self.scan_temp_repo("`example` (`src/example.py:1`, `:2`)", source), []
        )

    def test_missing_file_fails(self):
        problems = self.scan_temp_repo("`src/missing.py:1`")
        self.assertEqual(len(problems), 1)
        self.assertIn("file does not exist", problems[0][3])

    def test_line_past_end_fails(self):
        problems = self.scan_temp_repo("`src/example.py:3`")
        self.assertEqual(len(problems), 1)
        self.assertIn("past end of file", problems[0][3])

    def test_missing_symbol_fails(self):
        problems = self.scan_temp_repo("`src/example.py:missing_symbol`")
        self.assertEqual(len(problems), 1)
        self.assertIn("does not occur", problems[0][3])

    def test_bad_continuation_fails(self):
        problems = self.scan_temp_repo("`src/example.py:1`\n`:3`")
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0][2], ":3")
        self.assertIn("past end of file", problems[0][3])


if __name__ == "__main__":
    unittest.main()
