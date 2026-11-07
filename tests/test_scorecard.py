import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import scorecard


class ScorecardTest(unittest.TestCase):
    def test_check_passes_on_the_committed_page(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "scorecard.py"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changing_an_input_fixture_changes_the_render_in_a_temporary_copy(self):
        inputs = [
            scorecard.RECEIVER_SET,
            scorecard.LABELS,
            scorecard.FEED_SNAPSHOT,
            scorecard.TESTNET,
            scorecard.CLAIMS,
            *(path.relative_to(ROOT) for path in sorted((ROOT / scorecard.JUDGEMENTS).glob("*.json"))),
            *(path.relative_to(ROOT) for path in sorted((ROOT / scorecard.REGISTRY).glob("*.json"))),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            copy_root = Path(temporary)
            for relative in inputs:
                destination = copy_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)

            before = scorecard.render(copy_root)
            judgement_path = next(
                path for path in sorted((copy_root / scorecard.JUDGEMENTS).glob("*.json"))
                if json.loads(path.read_text(encoding="utf-8"))["verdict"] == "ALLOW"
            )
            judgement = json.loads(judgement_path.read_text(encoding="utf-8"))
            judgement["verdict"] = "DENY"
            judgement_path.write_text(json.dumps(judgement), encoding="utf-8")
            after = scorecard.render(copy_root)

        self.assertNotEqual(after, before)

    def test_every_metric_line_names_a_source_file(self):
        page = (ROOT / scorecard.SCORECARD).read_text(encoding="utf-8")
        source_path = re.compile(r"(?:[\w.*-]+/)+[\w.*-]+\.(?:jsonl|json|md|py)\b")
        rows = [
            line for line in page.splitlines()
            if line.startswith("| ") and not line.startswith("| ---")
            and line.split("|")[1].strip() != "Measure"
        ]
        self.assertTrue(rows)
        for line in rows:
            with self.subTest(line=line):
                self.assertRegex(line, source_path)


if __name__ == "__main__":
    unittest.main()
