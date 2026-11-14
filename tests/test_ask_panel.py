import shutil
import subprocess
import unittest
from pathlib import Path


class AskPanelTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_ask_panel_runtime(self):
        for scenario in ("signed-out", "no-team", "post", "render", "unavailable", "clear", "late",
                         "outcome-changed", "errors", "no-session", "reasons"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "tests/ask_panel_runtime.cjs", scenario],
                                        cwd=Path(__file__).resolve().parent.parent,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
