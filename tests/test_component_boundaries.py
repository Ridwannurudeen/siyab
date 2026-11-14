import re
import unittest
from pathlib import Path

from tests.test_design_system import rules, tokens


class ComponentBoundariesTest(unittest.TestCase):
    def test_labelled_controls_use_hairlines_and_inputs_keep_strong_boundaries(self):
        css = "\n".join(path.read_text(encoding="utf-8") for path in sorted(Path("intel/ui").glob("*.css")))
        parsed = list(rules(css))
        labelled = ("button,.button", ".graph-view-controls>summary", ".mobile-navigation>summary",
                    ".entity-table-pagination a", ".graph-mode-controls",
                    ".graph-mode-controls button+button", ".flow-evidence-pagination a",
                    ".graph-compact-filters>summary,.graph-compact-actions>summary")
        inputs = ("input,select,textarea", ".site-search-button",
                  ".cloud-workspace input,.cloud-workspace select,.cloud-workspace textarea,.case-dialog select",
                  ".investigation-jobs input,.investigation-jobs select")
        for selectors, token in ((labelled, "var(--hairline))"), (inputs, "var(--hairline-strong))")):
            for selector in selectors:
                bodies = [body for name, body, context in parsed if name == selector]
                self.assertTrue(bodies, selector)
                borders = [part for body in bodies for part in body.split(";")
                           if part.startswith(("border:", "border-left:"))]
                with self.subTest(selector=selector):
                    self.assertTrue(borders, selector)
                    self.assertTrue(all(border.endswith(token) for border in borders), borders)

    def test_semantic_chip_text_contrast_on_row_surfaces(self):
        css = Path("intel/ui/site.css").read_text(encoding="utf-8")
        parsed = list(rules(css))
        themes = [tokens(body) for selector, body, context in parsed
                  if not context and (selector == ":root" or selector.startswith(':root[data-theme="light"],'))]

        def luminance(rgb):
            values = [value / 255 for value in rgb]
            return sum(weight * (value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4)
                       for value, weight in zip(values, (.2126, .7152, .0722)))

        base = next(body for selector, body, context in parsed if selector == ".outcome" and not context)
        for theme in themes:
            colors = {key: tuple(map(int, value.split())) for key, value in theme.items()
                      if re.fullmatch(r"\d+ \d+ \d+", value)}
            for selector, foreground in ((".outcome.allow", "--allow-text"), (".outcome.deny", "--block-text"), (".outcome.ask", "--unknown")):
                body = base + ";" + ";".join(body for name, body, context in parsed
                                                if not context and name == selector)
                fill, alpha = re.findall(r"background:rgb\(var\((--[a-z-]+)\)(?:/([.0-9]+))?\)", body)[-1]
                opacity = float(alpha) if alpha else 1
                for surface in ("--ground", "--panel", "--ground-alt", "--hover"):
                    background = tuple(opacity * color + (1 - opacity) * parent
                                       for color, parent in zip(colors[fill], colors[surface]))
                    lower, higher = sorted((luminance(colors[foreground]), luminance(background)))
                    with self.subTest(theme=colors["--ground"], chip=selector, surface=surface):
                        self.assertGreaterEqual((higher + .05) / (lower + .05), 4.5)

    def test_browser_typed_issuer_values_use_monospace(self):
        import importlib.util
        if not importlib.util.find_spec("playwright"):
            self.skipTest("Browser package is not importable")
        from playwright.sync_api import Error, sync_playwright

        css = Path("intel/ui/site.css").read_text(encoding="utf-8") + "".join(
            "\n" + path.read_text(encoding="utf-8") for path in sorted(Path("intel/ui").glob("*.css"))
            if path.name != "site.css")
        document = Path("demo/site/issuers.html").read_text(encoding="utf-8")
        document = re.sub(r'<script\b[^>]*>.*?</script>', '', document, flags=re.DOTALL)
        document = re.sub(r'<link\b[^>]*>', '', document)
        with sync_playwright() as runtime:
            try:
                browser = runtime.chromium.launch(headless=True)
            except (Error, OSError) as error:
                self.skipTest("Browser cannot launch: " + str(error))
            with browser.new_context(java_script_enabled=False) as context:
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.set_content(document.replace('</head>', '<style>' + css + '</style></head>'))
                    for selector in ('#issuer-status td:first-child q.data', '#issuer-status td:nth-child(2) q.data',
                                     '#issuer-status td:nth-child(4)', '#issuer-history td:first-child q.data',
                                     '#issuer-history td:nth-child(3)'):
                        actual = page.locator(selector).first.evaluate("node => {const s=getComputedStyle(node);return [s.fontFamily,s.fontSize,s.lineHeight,s.fontWeight,s.fontFeatureSettings,s.letterSpacing]}")
                        with self.subTest(width=width, role=selector):
                            self.assertIn("IBM Plex Mono", actual[0])
                            self.assertEqual(actual[1:4], ["14px" if width <= 390 else "12px", "20px" if width <= 390 else "18px", "400"])
                            self.assertRegex(actual[4], r'(?:^|,\s*)"zero"(?:\s+1)?(?=,|$)')
                            self.assertIn(actual[5], ("normal", "0px"))
                    for selector in ('#issuer-status td:nth-child(3)', '#issuer-status .cell-label'):
                        self.assertIn("Public Sans", page.locator(selector).first.evaluate("node => getComputedStyle(node).fontFamily"))
            browser.close()

    def test_browser_record_scalar_identifiers_and_times_use_monospace(self):
        import importlib.util
        if not importlib.util.find_spec("playwright"):
            self.skipTest("Browser package is not importable")
        from playwright.sync_api import Error, sync_playwright

        css = Path("intel/ui/site.css").read_text(encoding="utf-8") + "".join(
            "\n" + path.read_text(encoding="utf-8") for path in sorted(Path("intel/ui").glob("*.css"))
            if path.name != "site.css")
        record = next(path for path in Path("demo/site/records").glob("*.html") if path.name != "index.html")
        document = record.read_text(encoding="utf-8")
        document = re.sub(r'<script\b.*?</script>', '', re.sub(r'<link\b[^>]*>', '', document), flags=re.DOTALL)
        with sync_playwright() as runtime:
            try:
                browser = runtime.chromium.launch(headless=True)
            except (Error, OSError) as error:
                self.skipTest("Browser cannot launch: " + str(error))
            with browser.new_context(java_script_enabled=False) as context:
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.set_content(document.replace('</head>', '<style>' + css + '</style></head>'))
                    for path in ("record_id", "publisher", "evidence[0].fetched_at", "timeline[0].at"):
                        actual = page.locator('[data-record-path="' + path + '"]').evaluate("node => {const s=getComputedStyle(node);return [s.fontFamily,s.fontSize,s.lineHeight,s.fontWeight,s.fontFeatureSettings]}")
                        with self.subTest(width=width, path=path):
                            self.assertIn("IBM Plex Mono", actual[0])
                            self.assertEqual(actual[1:4], ["14px" if width <= 390 else "12px", "20px" if width <= 390 else "18px", "400"])
                            self.assertRegex(actual[4], r'(?:^|,\s*)"zero"(?:\s+1)?(?=,|$)')
                    self.assertIn("Public Sans", page.locator(".record-content dt").first.evaluate("node => getComputedStyle(node).fontFamily"))
            browser.close()

    def test_quoted_hashes_keep_monospace_with_prose_around_them(self):
        import importlib.util
        if not importlib.util.find_spec("playwright"):
            self.skipTest("Browser package is not importable")
        from playwright.sync_api import sync_playwright

        css = Path("intel/ui/site.css").read_text(encoding="utf-8")
        document = Path("demo/site/incidents/rhea.html").read_text(encoding="utf-8")
        document = re.sub(r'<script\b[^>]*>.*?</script>', '', document, flags=re.DOTALL)
        document = re.sub(r'<link\b[^>]*>', '', document)
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True)
            with browser.new_context(java_script_enabled=False) as context:
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_content(document.replace('</head>', '<style>' + css + '</style></head>'))
                self.assertIn("IBM Plex Mono", page.locator("blockquote.data .data-id").first.evaluate("node => getComputedStyle(node).fontFamily"))
                self.assertIn("Public Sans", page.locator("blockquote.data").first.evaluate("node => getComputedStyle(node).fontFamily"))
            browser.close()

    def test_quoted_account_identifiers_have_a_distinct_type_role(self):
        from intel import render

        for value in ("nep141:usdt.tether-token.near", "usdt.tether-token.near", "v2.ref-finance.near", "kestrelridge-owner.testnet"):
            with self.subTest(identifier=value):
                self.assertEqual(render.data_text('"' + value + '"'), '<q class="data data-identifier">' + value + '</q>')
        from tests.test_render import own_text
        self.assertEqual(own_text(render.data_text('Note "v2.ref-finance.near" retained')).split(), ["Note", "retained"])
        for value in ("USDT", "near", "A source says this is unknown", "v2.ref-finance.near is recorded"):
            with self.subTest(prose=value):
                self.assertEqual(render.data_text('"' + value + '"'), '<q class="data">' + value + '</q>')

    def test_print_uses_the_complete_light_palette(self):
        css = Path("intel/ui/site.css").read_text(encoding="utf-8")
        parsed = list(rules(css))
        light = next(tokens(body) for name, body, context in parsed
                     if not context and name == ':root[data-theme="light"]')
        printed = next(tokens(body) for name, body, context in parsed if context == ("@media print",))
        for key, value in light.items():
            if re.fullmatch(r"\d+ \d+ \d+", value):
                with self.subTest(role=key):
                    self.assertEqual(printed.get(key), value)

    def test_investigation_figures_use_light_data_role(self):
        css = Path("intel/ui/viz.css").read_text(encoding="utf-8")
        body = next(body for name, body, context in rules(css) if name == ".investigation-summary dd")
        self.assertIn("font:300 24px/30px var(--font-code)", body)


if __name__ == "__main__":
    unittest.main()
