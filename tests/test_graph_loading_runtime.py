import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class GraphLoadingRuntimeTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_async_graph_loader_failure_boundaries_and_retry(self):
        result = subprocess.run(
            [shutil.which("node"), "tests/frontend_graph_loading.cjs"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
