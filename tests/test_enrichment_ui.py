import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class EnrichmentUITest(unittest.TestCase):
    def test_fragment_has_accessible_scoped_mounts_and_responsive_styles(self):
        panel = (ROOT / "intel/ui/enrichment-panel.html").read_text(encoding="utf-8")
        css = (ROOT / "intel/ui/enrichment.css").read_text(encoding="utf-8")
        for name in ("entity-enrichment", "enrichment-account", "enrichment-associations", "enrichment-directory",
                     "enrichment-search", "enrichment-query", "enrichment-category", "enrichment-results"):
            self.assertIn('id="' + name + '"', panel)
        self.assertIn('for="enrichment-query"', panel)
        self.assertIn('for="enrichment-category"', panel)
        self.assertIn('aria-live="polite"', panel)
        self.assertIn("does not establish current ownership", panel)
        self.assertIn("<noscript>", panel)
        self.assertIn("@media", css)
        self.assertIn("overflow-wrap:anywhere", css)
        self.assertNotIn("@font-face", css)

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_real_runtime_source_boundaries_races_and_directory_pagination(self):
        result = subprocess.run([shutil.which("node"), "tests/enrichment_runtime.cjs"], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
