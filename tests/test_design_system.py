import functools
import importlib.util
import re
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from intel import render


THEME_SCRIPT = "<script>try{var t=localStorage.getItem('siyab-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}</script>"
LIGHT_QUERY = "(prefers-color-scheme: light)"


def rules(css, contexts=()):
    start = 0
    while start < len(css):
        opening = css.find("{", start)
        if opening < 0:
            return
        depth, end = 1, opening + 1
        while depth:
            if css[end] == "{":
                depth += 1
            elif css[end] == "}":
                depth -= 1
            end += 1
        selector = css[start:opening].strip()
        body = css[opening + 1:end - 1]
        if selector.startswith(("@media", "@keyframes")):
            yield from rules(body, contexts + (selector,))
        else:
            yield selector, body, contexts
        start = end


def tokens(body):
    return dict(re.findall(r"(--[a-z0-9-]+):([^;}]+)", body))


class DesignSystemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.out = Path(cls.tmp.name)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network read")):
            written = render.render(cls.out)
        cls.pages = {name: (cls.out / name).read_text(encoding="utf-8")
                     for name in written if name.endswith(".html")}
        cls.css = (cls.out / "style.css").read_text(encoding="utf-8")
        cls.rules = list(rules(cls.css))
        cls.browser = None
        cls.browser_reason = "Browser package is not importable"
        if importlib.util.find_spec("playwright"):
            from playwright.sync_api import Error, sync_playwright

            runtime = None
            try:
                runtime = sync_playwright().start()
                cls.browser = runtime.chromium.launch(headless=True)
            except (Error, OSError) as error:
                cls.browser_reason = "Browser cannot launch in this sandbox: " + str(error)
                if runtime is not None:
                    runtime.stop()
            else:
                cls.addClassCleanup(runtime.stop)
                cls.addClassCleanup(cls.browser.close)
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(cls.out))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.thread.join)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        if self._testMethodName.startswith("test_browser_") and self.browser is None:
            self.skipTest(self.browser_reason)

    def test_system_light_tokens_match_explicit_light(self):
        explicit = next(body for selector, body, context in self.rules
                        if selector == ':root[data-theme="light"]' and not context)
        system = next(body for selector, body, context in self.rules
                      if selector == ':root:not([data-theme="dark"])'
                      and context == ('@media ' + LIGHT_QUERY,))
        self.assertEqual(tokens(system), tokens(explicit))
        self.assertIn("color-scheme:light", system)
        dark = next(body for selector, body, context in self.rules if selector == ":root" and not context)
        for name, value in {"--r-xs": "2px", "--r-sm": "4px", "--r-md": "6px",
                            "--r-lg": "8px", "--r-dialog": "12px"}.items():
            self.assertEqual(tokens(dark)[name], value)

    def test_every_radius_uses_the_role_scale(self):
        values = re.findall(r"border-radius:([^;}]+)", self.css)
        self.assertTrue(values)
        for value in values:
            with self.subTest(radius=value):
                self.assertRegex(value, r"^(?:var\(--r-(?:xs|sm|md|lg|dialog)\)|0|50%|999px)$")

    def test_colours_are_tokenized_outside_theme_blocks(self):
        for selector, body, context in self.rules:
            if selector in (":root", ':root[data-theme="light"]', ':root:not([data-theme="dark"])'):
                continue
            literals = re.findall(r"(?:rgba?\(\s*\d[^)]*\)|#[a-fA-F0-9]{3,8}\b)", body)
            with self.subTest(selector=selector, context=context):
                self.assertTrue(all(value == "rgb(0 117 78)" for value in literals), literals)

    def test_forced_colours_keep_graph_and_theme_icon_visible(self):
        forced = {selector: body for selector, body, context in self.rules
                  if any("forced-colors: active" in item for item in context)}
        for selector, body in {
            ".graph-node-body": "fill:Canvas;stroke:CanvasText",
            ".graph-node-label,.graph-node-detail": "fill:CanvasText",
            ".graph-node.is-selected .graph-node-body": "stroke:Highlight;stroke-width:3px",
            ".graph-edge": "stroke:CanvasText",
            ".chart-bin-bar,.analytics-fill": "fill:Highlight;background:Highlight",
            ".theme-toggle::before": "background-color:ButtonText;forced-color-adjust:none",
            "[aria-selected=true],[aria-pressed=true]": "border:2px solid Highlight",
        }.items():
            self.assertEqual(forced.get(selector), body)

    def test_theme_head_precedes_styles_and_records_stay_script_free(self):
        for name, page in self.pages.items():
            with self.subTest(page=name):
                self.assertIn('<meta name="color-scheme" content="dark light">', page)
                self.assertIn('<meta name="theme-color" content="#06101a" media="(prefers-color-scheme: dark)">', page)
                self.assertIn('<meta name="theme-color" content="#f4f9f7" media="(prefers-color-scheme: light)">', page)
                if name.startswith("records/"):
                    self.assertNotIn("<script", page)
                elif "<script" in page:
                    self.assertTrue(page.split("<head>", 1)[1].lstrip().startswith(THEME_SCRIPT), name)
                    self.assertLess(page.index(THEME_SCRIPT), page.index('<link rel="stylesheet"'))

    def test_explicit_light_rules_have_system_light_twins(self):
        for selector, body, context in self.rules:
            if '[data-theme="light"]' not in selector:
                continue
            if selector == ':root[data-theme="light"]' and not context:
                continue
            twin = selector.replace(':root[data-theme="light"]', ':root:not([data-theme="dark"])')
            with self.subTest(selector=selector, context=context):
                self.assertTrue(any(other == twin and value == body
                                    and any(LIGHT_QUERY in item for item in media)
                                    and all(item in media for item in context)
                                    for other, value, media in self.rules))

    def test_r1_colour_roles_and_focus(self):
        for rule in (
            '.analytics-fill{height:100' + '%;background:rgb(var(--data));border-radius:var(--r-xs)}',
            '.analytics-histogram rect{fill:rgb(var(--data))}',
            '.portfolio-meter-fill{display:block;height:100' + '%;background:rgb(var(--data))}',
            '.portfolio-chip.is-known{color:rgb(var(--ink))}',
            '.enrichment-state[data-state="documented"]{color:rgb(var(--ink))}',
            '.outcome.allow{border-color:rgb(var(--allow)/.6);background:rgb(var(--allow)/.12)}',
            '.outcome.deny{border-color:rgb(var(--block)/.6);background:rgb(var(--block)/.16);font-weight:500}',
            '.outcome.ask{border-color:rgb(var(--unknown)/.6)}',
            '.analytics-fill.unknown{background:repeating-linear-gradient(90deg,rgb(var(--unknown)) 0 4px,transparent 4px 6px)}',
            '.graph-status+.graph-source{border-top:0}',
        ):
            self.assertTrue(rule in self.css, rule)
        for selector, body, context in self.rules:
            if "outline:" in body and "outline:none" not in body:
                with self.subTest(selector=selector):
                    self.assertIn("outline:2px solid rgb(var(--focus))", body)
                    self.assertIn("outline-offset:3px", body)
        self.assertNotIn("outline-offset:2px", self.css)
        self.assertNotIn('.investigation-jobs ::selection', self.css)

    def test_select_borders_at_rest_and_on_interaction(self):
        for rule in (
            'select{border-color:rgb(var(--hairline-strong))}',
            'input:hover,input:focus,textarea:hover,textarea:focus,select:hover,select:focus{border-color:rgb(var(--edge))}',
            '.cloud-workspace select,.case-dialog select{border-color:rgb(var(--hairline-strong))}',
            '.cloud-workspace :is(input,select,textarea):is(:hover,:focus),.case-dialog select:is(:hover,:focus){border-color:rgb(var(--edge))}',
            '.investigation-jobs select{border-color:rgb(var(--hairline-strong))}',
            '.investigation-jobs :is(input,select):is(:hover,:focus){border-color:rgb(var(--edge))}',
        ):
            self.assertIn(rule, self.css)

    def test_browser_system_light_default(self):
        with self.browser.new_context(color_scheme="light") as context:
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
            page = context.new_page()
            page.goto(self.origin + "/index.html")
            self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(244, 249, 247)")
            self.assertEqual(page.locator(".theme-toggle-label").inner_text(), "Use dark theme")

    def test_browser_first_light_toggle_saves_dark(self):
        with self.browser.new_context(color_scheme="light") as context:
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
            page = context.new_page()
            page.goto(self.origin + "/index.html")
            page.locator(".theme-toggle").click()
            self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(6, 16, 26)")
            self.assertEqual(page.evaluate("localStorage.getItem('siyab-theme')"), "dark")
            self.assertEqual(page.locator('meta[name="theme-color"]').evaluate_all("nodes => nodes.map(node => node.content)"), ["#06101a", "#06101a"])

    def test_browser_saved_dark_overrides_light_system(self):
        with self.browser.new_context(color_scheme="light") as context:
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
            context.add_init_script("localStorage.setItem('siyab-theme', 'dark')")
            page = context.new_page()
            page.goto(self.origin + "/index.html")
            self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(6, 16, 26)")

    def test_browser_records_follow_light_system_without_scripts(self):
        record = next(name for name in self.pages if name.startswith("records/") and name != "records/index.html")
        with self.browser.new_context(color_scheme="light") as context:
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
            page = context.new_page()
            page.goto(self.origin + "/" + record)
            self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(244, 249, 247)")
            self.assertEqual(page.locator("script").count(), 0)


if __name__ == "__main__":
    unittest.main()
