import hashlib
import shutil
import subprocess
import unittest
from pathlib import Path

from intel import render


ROOT = Path(__file__).resolve().parent.parent


class AnalyticsRenderTest(unittest.TestCase):
    def test_account_page_mounts_new_analytics_after_existing_content(self):
        page = render.investigate_page()
        for name in ("portfolio", "enrichment"):
            fragment = (ROOT / f"intel/ui/{name}-panel.html").read_text(encoding="utf-8")
            self.assertIn(fragment, page)
            script = (ROOT / f"intel/ui/{name}.js").read_text(encoding="utf-8")
            self.assertIn(render._script_tag(script), page)
            self.assertTrue(f".{name}" in render.STYLE, f"{name} styles must be included in the generated stylesheet")
        for control in ("investigate-profile", "investigate-holdings", "investigate-read-identity", "investigate-backfill", "investigate-load-graph"):
            self.assertIn('id="' + control + '"', page)

    def test_shared_workspace_is_visible_and_browser_tools_remain(self):
        page = render.workspace_page()
        self.assertIn('<details id="cloud-workspace" class="cloud-workspace" open>', page)
        for control in ("cloud-shell", "browser-workspace", "saved-cases", "watch-form", "watch-notifications", "export-workspace", "reset-workspace"):
            self.assertIn('id="' + control + '"', page)
        for name in ("investigation-jobs", "delivery"):
            self.assertIn((ROOT / f"intel/ui/{name}-panel.html").read_text(encoding="utf-8"), page)
            self.assertIn(render._script_tag((ROOT / f"intel/ui/{name}.js").read_text(encoding="utf-8")), page)

    def test_recorded_feed_readable_amount_keeps_exact_record(self):
        event = {"signal": "large_transfer", "detail": "Recorded transfer", "subject": "alice.near",
                 "amount_yocto": "12345678901234567890123456789", "asset_id": "native:near",
                 "observed": {"timestamp_ms": 1794268800000, "block_height": 1}, "source": {"provider": "RPC"}}
        page = render._feed_panel([(event, "a" * 64)])
        self.assertIn('12,345.6789 NEAR', page)
        self.assertIn(event["amount_yocto"], page)
        event["usd_amount"] = "1234567.123456789012345"
        page = render._feed_panel([(event, "a" * 64)])
        self.assertIn('1,234,567.12 USD', page)
        self.assertIn(event["usd_amount"], page)

    def test_formatter_is_hashed_once_before_deferred_consumers(self):
        source = (ROOT / "intel/ui/number-format.js").read_text(encoding="utf-8")
        page = render.investigate_page()
        path = "/assets/" + hashlib.sha256(source.encode()).hexdigest() + ".js"
        self.assertEqual(page.count(path), 1)
        self.assertLess(page.index(path), page.index('</head>'))

    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_exact_string_rounding_for_primary_display(self):
        result = subprocess.run([shutil.which("node"), "tests/analytics_numbers_runtime.cjs"], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
