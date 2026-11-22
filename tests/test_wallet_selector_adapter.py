import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


class WalletSelectorAdapterTest(unittest.TestCase):
    def test_missing_node_fails_in_ci(self):
        with patch("shutil.which", return_value=None), patch.dict(
            os.environ, {"CI": "1", "SIYAB_SKIP_NODE_TESTS": "0"}
        ):
            with self.assertRaisesRegex(AssertionError, "Node is required"):
                self.test_recorded_answers_drive_the_wallet_guard()

    def test_missing_node_skips_only_with_explicit_flag(self):
        with patch("shutil.which", return_value=None), patch.dict(
            os.environ, {"CI": "1", "SIYAB_SKIP_NODE_TESTS": "1"}
        ):
            with self.assertRaises(unittest.SkipTest):
                self.test_recorded_answers_drive_the_wallet_guard()

    def test_recorded_answers_drive_the_wallet_guard(self):
        node = shutil.which("node")
        if not node:
            if os.environ.get("SIYAB_SKIP_NODE_TESTS") == "1":
                self.skipTest("Node is unavailable and SIYAB_SKIP_NODE_TESTS=1")
            self.fail("Node is required; set SIYAB_SKIP_NODE_TESTS=1 to skip explicitly")
        result = subprocess.run(
            [node, "tests/wallet_selector_runtime.cjs"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("wallet-selector adapter: 30 tests passed; signed=21", result.stdout)


if __name__ == "__main__":
    unittest.main()
