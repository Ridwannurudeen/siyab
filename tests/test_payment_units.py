import shutil
import subprocess
import unittest
from pathlib import Path


class PaymentUnitsTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_payment_form_unit_boundaries(self):
        for scenario in ("native", "invalid", "intent", "tokens", "race", "required"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "tests/payment_units_runtime.cjs", scenario],
                                        cwd=Path(__file__).resolve().parent.parent,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
