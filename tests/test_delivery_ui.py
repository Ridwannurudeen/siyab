import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class DeliveryUITest(unittest.TestCase):
    def test_native_delivery_panel_exposes_safe_linking_and_scope(self):
        panel = (ROOT / "intel/ui/delivery-panel.html").read_text(encoding="utf-8")
        css = (ROOT / "intel/ui/delivery.css").read_text(encoding="utf-8")
        for name in ("team-delivery", "delivery-context", "delivery-status", "delivery-controls", "delivery-log"):
            self.assertIn('id="' + name + '"', panel)
        self.assertIn('aria-live="polite"', panel)
        self.assertIn("confirmation", panel)
        self.assertIn("duplicates", panel)
        self.assertIn("Private notes", panel)
        self.assertNotIn('type="password"', panel)
        self.assertIn("overflow-wrap:anywhere", css)
        self.assertIn("@media", css)

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_delivery_runtime_permissions_private_lifecycle_and_generation(self):
        result = subprocess.run([shutil.which("node"), "tests/delivery_runtime.cjs"], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
