import hashlib
import html
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from intel import labels, render


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

    def test_recorded_feed_detail_uses_readable_units_and_keeps_exact_record(self):
        raw = ("full access key ed25519:abc added to agent.near; transfer of 1000000000000000000000000 yoctoNEAR "
               "to x.near within 452158 ms")
        event = {"signal": "full_access_key_then_outflow", "detail": raw, "subject": "agent.near",
                 "amount_yocto": "1000000000000000000000000",
                 "observed": {"timestamp_ms": 1794268800000, "block_height": 1}, "source": {"provider": "RPC"}}
        page = render._feed_panel([(event, "a" * 64)])
        self.assertIn("<p>full access key ed25519:abc added to agent.near; transfer of 1 NEAR to x.near within 7 min 32 s</p>",
                      page)
        self.assertIn('<span class="feed-amount">1 NEAR</span>', page)
        self.assertIn('data-label="Signal" data-export-value="' + event["signal"] + raw + '"', page)
        self.assertIn("1000000000000000000000000 yoctoNEAR to x.near within 452158 ms", page)

    def test_recorded_feed_signal_export_escapes_raw_values_and_keeps_unknown_fallbacks(self):
        for event, exact in (({}, "unknownunknown"), ({"signal": None, "detail": None}, "unknownunknown"),
                             ({"signal": 'raw_signal"', "detail": '<raw & detail> 2500 ms'},
                              'raw_signal"<raw & detail> 2500 ms')):
            with self.subTest(event=event):
                page = render._feed_panel([(event, "a" * 64)])
                self.assertIn('data-label="Signal" data-export-value="' + html.escape(exact, quote=True) + '"', page)

    def test_recorded_feed_signal_export_normalizes_whitespace_before_escaping(self):
        event = {"signal": "  recorded", "detail": '  <raw & "detail">\n2500 ms  '}
        page = render._feed_panel([(event, "a" * 64)])
        exact = 'recorded <raw & "detail"> 2500 ms'
        self.assertIn('data-label="Signal" data-export-value="' + html.escape(exact, quote=True) + '"', page)

    def test_detail_prose_preserves_boundaries_and_converts_whole_units(self):
        for text, expected in (("1000 ms", "1 s"), ("(1000 ms)", "(1 s)"),
                               ("1000 ms 2000 ms", "1 s 2 s")):
            with self.subTest(text=text):
                self.assertEqual(render._display_detail(text), expected)

    def test_detail_prose_leaves_decimal_and_comma_numbers_unchanged(self):
        for text in ("1.2500 ms", "1,2500 ms", "1.2500 yoctoNEAR", "1,2500 yoctoNEAR",
                     "2.1000000000000000000000000 yoctoNEAR", "1,000 ms", "a1000 ms",
                     "2500 base units of token.near"):
            with self.subTest(text=text):
                self.assertEqual(render._display_detail(text), text)

    def test_account_feed_card_uses_readable_units(self):
        event = {"signal": "full_access_key_then_outflow", "subject": "agent.near",
                 "detail": "transfer of 1000000000000000000000000 yoctoNEAR within 452158 ms <recorded>",
                 "observed": {"block_height": 1}}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "recorded.json").write_text(json.dumps(event), encoding="utf-8")
            page = render.account_page("agent.near", labels.Store([], "0" * 64), {}, [], [], [], events_dir=tmp)
        card = page.split("<h2>Feed events</h2>", 1)[1].split("</div>", 1)[0]
        self.assertIn("<p>transfer of 1 NEAR within 7 min 32 s &lt;recorded&gt;</p>", card)
        self.assertIn("evidence recorded", card)
        self.assertNotIn("yoctoNEAR", card)

    def test_recorded_feed_omits_asset_line_without_asset_id(self):
        event = {"signal": "recorded", "detail": "Recorded transfer", "amount_yocto": "1000000000000000000000000"}
        for asset_id in (None, "", "native:near"):
            with self.subTest(asset_id=asset_id):
                if asset_id is not None:
                    event["asset_id"] = asset_id
                page = render._feed_panel([(event, "a" * 64)])
                amount_cell = page.split('<span class="feed-amount">', 1)[1].split('</td>', 1)[0]
                self.assertNotIn("<small>unknown</small>", amount_cell)
                self.assertEqual("<small>" in amount_cell, bool(asset_id))
                if asset_id:
                    self.assertIn(f"<small>{asset_id}</small>", amount_cell)

    def test_shared_numbers_precede_inbox_on_every_active_page(self):
        numbers = render._script_tag((ROOT / "intel/ui/number-format.js").read_text(encoding="utf-8"))
        cloud = render._script_tag((ROOT / "intel/ui/cloud-workspace.js").read_text(encoding="utf-8"))
        page = render.page("Account", "")
        self.assertLess(page.index(numbers), page.index(cloud))
        inert = render.page("Incident record", "", inert=True)
        self.assertNotIn(cloud, inert)
        self.assertNotIn(numbers, inert)

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
