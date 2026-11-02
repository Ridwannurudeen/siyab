"""The README counts block is the one scripts/counts.py writes from the committed files."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
COUNTS = ROOT / "scripts" / "counts.py"


class ReadmeCountsTest(unittest.TestCase):
    def test_rewriting_the_block_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "README.md"
            shutil.copyfile(README, copy)
            subprocess.run([sys.executable, str(COUNTS), "--readme", str(copy)], cwd=ROOT, check=True,
                           capture_output=True)
            self.assertEqual(copy.read_bytes(), README.read_bytes())

    def test_the_block_holds_the_printed_lines_in_order(self):
        printed = subprocess.run([sys.executable, str(COUNTS)], cwd=ROOT, check=True, capture_output=True,
                                 text=True).stdout.splitlines()
        text = README.read_text(encoding="utf-8")
        start, end = text.index("<!-- counts:start -->"), text.index("<!-- counts:end -->")
        block = [line for line in text[start:end].splitlines()[1:] if line]
        self.assertEqual(len(printed), 6)
        self.assertEqual(block, printed)


if __name__ == "__main__":
    unittest.main()
