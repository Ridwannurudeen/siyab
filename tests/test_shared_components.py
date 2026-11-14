import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "intel" / "ui"


class SharedComponentsTest(unittest.TestCase):
    def test_section_titles_use_the_genuine_display_weight(self):
        for name in ("profiles", "feed-verify", "investigate", "portfolio", "workspace", "cloud-workspace", "enrichment", "investigation-jobs", "delivery"):
            text = (ROOT / (name + ".css")).read_text(encoding="utf-8")
            with self.subTest(stylesheet=name):
                self.assertNotIn("font:600 var(--fs-panel)", text)
                self.assertNotRegex(text, r"font(?:-weight:600|-weight:700|:700)")
                self.assertNotRegex(text, r"font:600 [^;}]*var\(--font-(?:ui|code)\)")
                self.assertIn("font:600 var(--fs-panel-title) var(--font-display)", text)
                self.assertNotRegex(text, r"(?:h2|h3|h4|summary)\{[^}]*font:300 var\(--fs-panel\)")

    def test_primary_account_and_figures_have_distinct_roles(self):
        text = (ROOT / "profiles.css").read_text(encoding="utf-8")
        self.assertIn(".profile-id{font:400 22px/28px var(--font-code)", text)
        self.assertIn(".profile-facts dd{margin:0;font:300 24px/30px var(--font-code)", text)
        for name in ("investigate", "portfolio", "delivery"):
            with self.subTest(stylesheet=name):
                text = (ROOT / (name + ".css")).read_text(encoding="utf-8")
                self.assertIn("font:300 24px/30px var(--font-code)", text)

    def test_desktop_table_cells_use_compact_natural_rows(self):
        for name, selector in (("profiles", ".profile-data-table td"), ("feed-verify", ".feed-table td,.record-index-table td"), ("investigate", ".investigate-table td"), ("portfolio", ".portfolio-table td"), ("investigation-jobs", ".jobs-table td")):
            text = (ROOT / (name + ".css")).read_text(encoding="utf-8")
            rule = re.search(re.escape(selector) + r"\{([^}]+)\}", text).group(1)
            with self.subTest(stylesheet=name):
                self.assertIn("padding:5px 10px", rule)
                self.assertIn("height:40px", rule)
                self.assertIn("font:400 13px/18px", rule)
                self.assertNotIn("overflow:hidden", rule)

    def test_panels_use_six_pixel_radius_token(self):
        for name, selector in (("profiles", ".profile-live-read"), ("workspace", ".workspace-section"), ("cloud-workspace", ".cloud-workspace"), ("feed-verify", ".verify-result")):
            text = (ROOT / (name + ".css")).read_text(encoding="utf-8")
            rule = re.search(re.escape(selector) + r"\{([^}]+)\}", text).group(1)
            with self.subTest(stylesheet=name):
                self.assertIn("border-radius:var(--r-md)", rule)

    def test_shared_sections_keep_compact_separation(self):
        for name, selector in (("profiles", ".profile-intelligence,.profile-section,.profile-case-brief"), ("investigate", ".investigate-section"), ("workspace", ".workspace-section"), ("investigation-jobs", ".investigation-jobs"), ("delivery", ".team-delivery")):
            text = (ROOT / (name + ".css")).read_text(encoding="utf-8")
            rule = re.search(re.escape(selector) + r"\{([^}]+)\}", text).group(1)
            with self.subTest(stylesheet=name):
                self.assertIn("margin:16px 0", rule)
        text = (ROOT / "feed-verify.css").read_text(encoding="utf-8")
        rule = re.search(r"\.verification-workspace\{([^}]+)\}", text).group(1)
        self.assertIn("gap:12px", rule)

    def test_direct_mono_roles_keep_numeric_features_after_font_reset(self):
        for name in ("profiles", "feed-verify", "investigate", "portfolio", "workspace", "cloud-workspace", "enrichment", "investigation-jobs", "delivery"):
            text = (ROOT / (name + ".css")).read_text(encoding="utf-8")
            for selector, rule in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
                if "var(--font-code)" not in rule:
                    continue
                with self.subTest(stylesheet=name, selector=selector):
                    self.assertIn('font-feature-settings:"lnum" 1,"tnum" 1,"zero" 1', rule)
                    self.assertIn("font-variant-numeric:lining-nums tabular-nums slashed-zero", rule)
                    self.assertIn("font-variant-ligatures:none", rule)
                    self.assertGreater(rule.index("font-feature-settings:"), rule.index("var(--font-code)"))

    def test_tabs_keep_native_buttons_and_visible_selection(self):
        text = (ROOT / "cloud-workspace.css").read_text(encoding="utf-8")
        self.assertIn("min-height:40px", text)
        self.assertIn("border-bottom:2px solid rgb(var(--accent))", text)
        self.assertIn("font:400 var(--fs-data) var(--font-code)", text)
        self.assertIn("text-transform:uppercase", text)
        self.assertIn("letter-spacing:.24px", text)

    def test_passive_chips_are_filled_and_phone_controls_remain_large(self):
        for name in ("profiles", "enrichment", "portfolio"):
            text = (ROOT / (name + ".css")).read_text(encoding="utf-8")
            with self.subTest(stylesheet=name):
                self.assertIn("min-height:24px", text)
                self.assertIn("padding:2px 8px", text)
                self.assertIn("border-radius:var(--r-sm)", text)
        for name in ("workspace", "cloud-workspace", "enrichment", "investigation-jobs", "delivery"):
            text = (ROOT / (name + ".css")).read_text(encoding="utf-8")
            with self.subTest(stylesheet=name):
                self.assertIn("15px/22px", text)
                self.assertIn("min-height:44px", text)
