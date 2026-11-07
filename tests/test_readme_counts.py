"""The README counts block is the one scripts/counts.py writes from the committed files, and the prose beside it
and the receivers sample of the Reproduce steps state its numbers."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
COUNTS = ROOT / "scripts" / "counts.py"
TIER2_LATENCY_RUNS = ROOT / "fixtures" / "latency" / "verdict-runs-tier2.json"
TIER2_FALLBACK_LATENCY_RUNS = ROOT / "fixtures" / "latency" / "verdict-runs-tier2-fallback.json"
RECEIVER_SET = ROOT / "fixtures" / "receivers" / "set.json"


def printed_lines():
    return subprocess.run([sys.executable, str(COUNTS)], cwd=ROOT, check=True, capture_output=True,
                          text=True).stdout.splitlines()


class ReadmeCountsTest(unittest.TestCase):
    def test_rewriting_the_block_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "README.md"
            shutil.copyfile(README, copy)
            subprocess.run([sys.executable, str(COUNTS), "--readme", str(copy)], cwd=ROOT, check=True,
                           capture_output=True)
            self.assertEqual(copy.read_bytes(), README.read_bytes())

    def test_the_block_holds_the_printed_lines_in_order(self):
        printed = printed_lines()
        text = README.read_text(encoding="utf-8")
        start, end = text.index("<!-- counts:start -->"), text.index("<!-- counts:end -->")
        block = [line for line in text[start:end].splitlines()[1:] if line]
        self.assertEqual(len(printed), 7 + TIER2_LATENCY_RUNS.is_file()
                         + TIER2_FALLBACK_LATENCY_RUNS.is_file())
        self.assertEqual(block, printed)

    def test_the_receivers_paragraph_states_the_first_line_count_and_the_exclusion_of_the_set_file(self):
        count = re.fullmatch(r"On (\d+) recorded mainnet receivers .*", printed_lines()[0]).group(1)
        exclusion = json.loads(RECEIVER_SET.read_text(encoding="utf-8"))["exclusion"]
        left_out = re.search(r"\((\d+) accounts\)", exclusion).group(1)
        paragraphs = [p for p in README.read_text(encoding="utf-8").split("\n\n") if "receivers of the first line" in p]
        self.assertEqual(len(paragraphs), 1)
        self.assertTrue(paragraphs[0].startswith(f"The {count} receivers of the first line "))
        self.assertIn(f'"{exclusion}"', paragraphs[0])
        self.assertEqual(re.findall(r"\b\d+\b", paragraphs[0]), [count, left_out])

    def test_the_reproduce_sample_holds_the_two_receiver_lines(self):
        text = README.read_text(encoding="utf-8")
        step = text[text.index("9. `python -m intel receivers`"):]
        start = step.index("   ```\n") + len("   ```\n")
        sample = step[start:step.index("   ```\n", start)]
        self.assertEqual([line.strip() for line in sample.splitlines()], printed_lines()[:2])


if __name__ == "__main__":
    unittest.main()
