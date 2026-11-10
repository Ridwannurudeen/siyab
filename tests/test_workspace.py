import shutil
import subprocess
import unittest
from pathlib import Path


class WorkspaceTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_workspace_security_and_lifecycle(self):
        for scenario in ("validation", "modal-reset", "storage-xss-csv", "monitor", "export-watch", "import"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "tests/workspace_runtime.cjs", scenario],
                                        cwd=Path(__file__).resolve().parent.parent,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
