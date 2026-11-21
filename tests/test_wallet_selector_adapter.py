import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class WalletSelectorAdapterTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_recorded_answers_drive_the_wallet_guard(self):
        result = subprocess.run(
            [shutil.which("node"), "tests/wallet_selector_runtime.cjs"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("wallet-selector adapter: ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
