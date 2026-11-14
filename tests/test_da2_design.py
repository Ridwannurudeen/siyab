import re
import subprocess
import unittest
from pathlib import Path

from tests.test_design_system import rules, tokens


class DA2DesignTest(unittest.TestCase):
    def test_approved_static_font_weights(self):
        css = Path("intel/ui/site.css").read_text(encoding="utf-8")
        self.assertNotIn("font-family:Inter", css)
        for name, weight in (("ExtraLight", 200), ("Light", 300), ("Regular", 400), ("Medium", 500)):
            self.assertRegex(css, r'@font-face\{font-family:"?Public Sans"?;[^}]*PublicSans-' + name + r'\.woff2[^}]*font-weight:' + str(weight) + r';[^}]*font-display:swap')
        self.assertIn("IBMPlexMono-Light.woff2", css)
        self.assertIn("font-synthesis:none", css)

    def test_approved_dark_and_light_tokens(self):
        css = Path("intel/ui/site.css").read_text(encoding="utf-8")
        parsed = list(rules(css))
        for selector, expected in ((":root", {"--ground": "10 13 22", "--panel": "16 19 28", "--ink": "237 237 237", "--secondary": "171 178 202", "--accent": "96 216 186", "--link": "121 174 242", "--ground-alt": "23 28 39", "--hover": "32 39 53", "--muted": "150 158 179", "--allow-text": "107 196 152", "--block-text": "249 128 154", "--unknown": "230 195 131", "--hairline": "41 49 64", "--hairline-strong": "114 125 148"}), ('[data-theme="light"]', {"--ground": "245 247 250", "--panel": "255 255 255", "--ink": "24 33 47", "--secondary": "72 86 107", "--accent": "0 110 89", "--link": "36 91 165", "--ground-alt": "237 241 246", "--hover": "228 234 242", "--muted": "86 100 122", "--allow-text": "36 107 70", "--block-text": "168 45 73", "--unknown": "118 83 9", "--hairline": "211 219 230", "--hairline-strong": "120 132 154"})):
            body = next(body for other, body, context in parsed if not context and (other == selector if selector == ":root" else selector in other))
            actual = tokens(body)
            for name, value in expected.items():
                with self.subTest(theme=selector, token=name):
                    self.assertEqual(actual.get(name), value)
        self.assertNotIn("prefers-color-scheme: light", css)

    def test_numeric_roles_enable_lining_and_slashed_zero(self):
        css = Path("intel/ui/site.css").read_text(encoding="utf-8")
        self.assertIn("font-variant-numeric:lining-nums tabular-nums slashed-zero", css)
        self.assertIn('"zero" 1', css)

    def test_browser_card_rows_contain_all_fields_without_overlap(self):
        import importlib.util
        if not importlib.util.find_spec("playwright"):
            self.skipTest("Browser package is not importable")
        from playwright.sync_api import sync_playwright

        css = Path("intel/ui/site.css").read_text(encoding="utf-8") + "".join(
            "\n" + path.read_text(encoding="utf-8") for path in sorted(Path("intel/ui").glob("*.css"))
            if path.name != "site.css")
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True)
            context = browser.new_context(java_script_enabled=False)
            context.route("**/*", lambda route: route.abort())
            page = context.new_page()
            for width in (1024, 768, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                for name in ("accounts/wrap.near.html", "entities/catalog.html", "records/index.html", "issuers.html"):
                    document = Path("demo/site", name).read_text(encoding="utf-8")
                    document = re.sub(r'<script\b[^>]*>.*?</script>', '', document, flags=re.DOTALL)
                    document = re.sub(r'<link\b[^>]*>', '', document)
                    page.set_content(document.replace('</head>', '<style>' + css + '</style></head>'))
                    measured = page.locator("tbody tr").evaluate_all("""nodes => nodes.filter(node => node.checkVisibility() && getComputedStyle(node).display === 'grid').map(node => {
                        const bounds=node.getBoundingClientRect();
                        return {bottom:bounds.bottom,children:[...node.cells].filter(cell=>cell.checkVisibility()).map(cell=>cell.getBoundingClientRect().bottom),next:node.nextElementSibling?.getBoundingClientRect().top};
                    })""")
                    for row in measured:
                        with self.subTest(page=name, width=width):
                            self.assertTrue(all(bottom <= row["bottom"] + 1 for bottom in row["children"]), row)
                            if row["next"] is not None:
                                self.assertGreaterEqual(row["next"], max(row["children"]) - 1, row)

            browser.close()

    def test_motion_declarations_and_contexts_match_baseline(self):
        paths = sorted(Path("intel/ui").glob("*.css"))
        for path in paths:
            baseline = subprocess.run(["git", "show", "HEAD:" + path.as_posix()], capture_output=True, text=True, check=True).stdout
            current = path.read_text(encoding="utf-8")
            def motion(css):
                found = []
                for selector, body, context in rules(css):
                    if selector.startswith(".landing-motion-ready ") or any(item in ("@keyframes landing-draw", "@keyframes landing-rail") for item in context):
                        continue
                    declarations = re.findall(r"(?:^|;)((?:animation|transition)(?:-[a-z-]+)?:[^;]+)", body)
                    if declarations or any("keyframes" in item or "prefers-reduced-motion" in item for item in context) or "pause" in selector:
                        found.append((selector, declarations, context, body if any("keyframes" in item or "prefers-reduced-motion" in item for item in context) or "pause" in selector else None))
                return found
            with self.subTest(path=path):
                self.assertEqual(motion(current), motion(baseline))


if __name__ == "__main__":
    unittest.main()
