import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class PortfolioUITest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_portfolio_runtime_boundaries_and_exact_disclosures(self):
        result = subprocess.run(
            [shutil.which("node"), "tests/portfolio_runtime.cjs", sys.executable],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fragment_has_explicit_actions_accessible_states_and_scoped_styles(self):
        fragment = (ROOT / "intel/ui/portfolio-panel.html").read_text(encoding="utf-8")
        for identifier in (
            "account-portfolio",
            "portfolio-read",
            "portfolio-history-form",
            "portfolio-history-asset",
            "portfolio-history-scope",
            "portfolio-block-form",
            "portfolio-block-height",
            "portfolio-block-contracts",
        ):
            self.assertIn('id="' + identifier + '"', fragment)
        self.assertEqual(fragment.count('aria-live="polite"'), 3)
        self.assertIn("<noscript>", fragment)
        style = (ROOT / "intel/ui/portfolio.css").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", style)
        self.assertIn("data-label", style)
        self.assertIn("overflow-wrap:anywhere", style.replace(" ", ""))
        self.assertNotIn("@import", style)

    def test_history_axes_use_intrinsic_nonwrapping_labels_at_all_breakpoints(self):
        style = (ROOT / "intel/ui/portfolio.css").read_text(encoding="utf-8")
        self.assertRegex(style, r"\.portfolio-chart\{[^}]*grid-template-columns:max-content minmax\(0,1fr\)")
        self.assertRegex(style, r"\.portfolio-y-axis\{[^}]*white-space:nowrap")
        self.assertNotRegex(style, r"\.portfolio-chart\{[^}]*grid-template-columns:(?:65px|75px|minmax\(75px,100px\))")


if __name__ == "__main__":
    unittest.main()
