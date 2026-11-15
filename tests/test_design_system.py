import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlparse

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
        cls.origin = "http://siyab.local"

    def local_route(self, route):
        if route.request.url.startswith(self.origin + "/"):
            path = self.out / unquote(urlparse(route.request.url).path).lstrip("/")
            if path.is_file():
                route.fulfill(path=str(path))
                return
        route.abort()

    def setUp(self):
        if self._testMethodName.startswith("test_browser_") and self.browser is None:
            self.skipTest(self.browser_reason)

    def test_dark_default_and_explicit_light_tokens(self):
        explicit = next(body for selector, body, context in self.rules
                        if ':root[data-theme="light"]' in selector and not context)
        self.assertIn("color-scheme:light", explicit)
        self.assertNotIn("record-theme", self.css)
        self.assertNotIn(LIGHT_QUERY, self.css)
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

    def test_landing_motion_uses_the_approved_trace_timings(self):
        dark = next(body for selector, body, context in self.rules if selector == ":root" and not context)
        expected = {"--landing-ease-trace": "cubic-bezier(.65,0,.35,1)",
                    "--landing-draw-duration": "2200ms", "--landing-depth-delay": "500ms",
                    "--landing-highlight-duration": "1200ms", "--landing-highlight-delay": "900ms",
                    "--landing-pipeline-duration": "700ms", "--landing-pipeline-delay": "0ms",
                    "--landing-timeline-duration": "1000ms", "--landing-timeline-delay": "0ms"}
        for name, value in expected.items():
            self.assertEqual(tokens(dark).get(name), value, name)
        edges = next(body for selector, body, context in self.rules
                     if selector == ".landing-motion-ready .is-running .landing-edge")
        self.assertIn("var(--landing-ease-trace)", edges)
        self.assertIn('data-depth="2"', self.css)
        self.assertTrue(any(selector == ".landing-motion-ready .landing-replay" and "display:none" in body
                            and any("prefers-reduced-motion:reduce" in item.replace(" ", "") for item in context)
                            for selector, body, context in self.rules))

    def test_colours_are_tokenized_outside_theme_blocks(self):
        for selector, body, context in self.rules:
            if selector == ":root" or "color-scheme:light" in body or "@media print" in context:
                continue
            literals = re.findall(r"(?:rgba?\(\s*\d[^)]*\)|#[a-fA-F0-9]{3,8}\b)", body)
            with self.subTest(selector=selector, context=context):
                self.assertTrue(not literals, literals)

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

    def test_theme_head_precedes_styles_on_every_page_including_records(self):
        for name, page in self.pages.items():
            with self.subTest(page=name):
                self.assertIn('<meta name="color-scheme" content="dark light">', page)
                self.assertIn('<meta name="theme-color" content="#0A0D16">', page)
                if name.startswith("records/"):
                    self.assertIn('id="theme-toggle"', page)
                    self.assertIn('data-open-search', page)
                if name.startswith("records/") or "<script" in page:
                    self.assertTrue(page.split("<head>", 1)[1].lstrip().startswith(THEME_SCRIPT), name)
                    self.assertLess(page.index(THEME_SCRIPT), page.index('<link rel="stylesheet"'))

    def test_r1_colour_roles_and_focus(self):
        for rule in (
            '.analytics-fill{height:100' + '%;background:rgb(var(--data));border-radius:var(--r-xs)}',
            '.analytics-histogram rect{fill:rgb(var(--data))}',
            '.portfolio-meter-fill{display:block;height:100' + '%;background:rgb(var(--data))}',
            '.portfolio-chip.is-known{color:rgb(var(--ink))}',
            '.enrichment-state[data-state="documented"]{color:rgb(var(--ink))}',
            '.outcome.allow{border-color:rgb(var(--allow)/.6);background:rgb(var(--ground-alt))}',
            '.outcome.deny{border-color:rgb(var(--block)/.6);background:rgb(var(--ground-alt));font-weight:500}',
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

    def test_r2_header_search_preserves_control_padding(self):
        for context, padding in (((), "7px 10px"), (("@media(max-width:760px)",), "8px 10px")):
            body = next(body for selector, body, media in self.rules
                        if selector == ".site-search-button" and media == context)
            with self.subTest(context=context):
                self.assertIn("padding:" + padding + ";", body)

    def test_r2_spacing_and_type_tokens(self):
        root = next(body for selector, body, context in self.rules if selector == ":root" and not context)
        values = tokens(root)
        for name, value in {
            "--s1": "4px", "--s2": "8px", "--s3": "12px", "--s4": "16px",
            "--s5": "24px", "--s6": "32px", "--s7": "48px",
            "--fs-micro": "12px/18px", "--fs-data": "12px/18px",
            "--fs-body": "14px/21px", "--fs-control": "13px/18px",
            "--fs-panel": "18px/24px", "--fs-panel-title": "16px/22px", "--fs-title": "24px/30px", "--fs-page": "24px/30px",
            "--fs-display": "clamp(38px, 4.7vw, 68px)", "--font-display": 'Manrope,"Public Sans",Arial,Helvetica,sans-serif',
        }.items():
            with self.subTest(token=name):
                self.assertEqual(values.get(name), value)

    def test_r2_font_sizes_line_heights_and_tracking_use_scale(self):
        failures = []
        for selector, body, context in self.rules:
            sizes = re.findall(r"(?:^|;)font-size:([0-9.]+)px", body)
            sizes += re.findall(r"(?:^|;)font:(?:[1-9]00 )?([0-9.]+)px", body)
            phone = any("max-width:760px" in item.replace(" ", "") or "pointer:coarse" in item.replace(" ", "") for item in context)
            jobs_phone = selector == ".investigation-jobs" and context == ("@media(max-width:480px)",)
            allowed = {12, 13, 14, 16, 18, 20, 22, 24, 28} | ({15} if phone or jobs_phone else set())
            if jobs_phone:
                self.assertIn("font:400 14px/22px var(--font-ui)", body)
            brand = all(part.strip() in (".site-brand", ".app-mobile-brand") for part in selector.split(","))
            for size in sizes:
                if float(size) not in allowed and not brand:
                    failures.append((selector, "size", size, context))
            for height in re.findall(r"(?:^|;)line-height:([^;]+)", body):
                if not re.fullmatch(r"[0-9.]+px", height) and not ("landing" in selector and height == "0.98"):
                    failures.append((selector, "line", height, context))
            for height in re.findall(r"(?:^|;)font:[^;]*?/[ ]*([0-9.]+)(?![0-9.])([a-z%]*)", body):
                landing_line = {".landing .page-head h1": "1.04", ".landing-counter": "1", ".landing .landing-statement": "1.18"}
                if height[1] != "px" and height != (landing_line.get(selector), ""):
                    failures.append((selector, "font line", height, context))
            for tracking in re.findall(r"(?:^|;)letter-spacing:([^;]+)", body):
                if tracking not in ("0", "0px", "-.01em", "-0.01em", "-.02em", "-0.02em", "-.015em", "-0.015em", "-.025em", "-0.025em", "-.035em", "-0.035em", ".18px", "0.18px", ".24px", "0.24px"):
                    failures.append((selector, "tracking", tracking, context))
        self.assertEqual(failures, [])

    def test_r2_spacing_uses_scale_with_scoped_exceptions(self):
        failures = []
        for selector, body, context in self.rules:
            for property_name, value in re.findall(r"(?:^|;)((?:margin|padding)(?:-[a-z-]+)?|(?:column-|row-)?gap):([^;]+)", body):
                for number in re.findall(r"(?<![a-z0-9.])(-?[0-9.]+)px", value):
                    size = abs(float(number))
                    if size in (0, 2, 4, 5, 8, 10, 12, 16, 24, 32, 48):
                        continue
                    parts = selector.split(",")
                    control_padding = property_name.startswith("padding") and all(re.search(r"(?:button|input|select|textarea|\.button|search-key|kbd|\btd\b|\bth\b)", part) for part in parts)
                    gutter = property_name.startswith("padding") and size in (10, 12, 14, 16, 20, 24, 32) and all(part.strip() in (".app-content", ".app-topbar", ".app-footer") for part in parts)
                    panel_head = property_name == "padding" and size in (7, 14) and all(part.strip() in (".workspace-panel-head", ".analytics-panel-head", ".graph-toolbar", ".graph-list", ".graph-overview-panel") for part in parts)
                    bar_offset = size in (1, 2) and ("bar" in selector or "analytics-track" in selector)
                    mobile_menu = selector == ".mobile-navigation-panel" and property_name == "padding" and value == "12px 20px 24px"
                    view_control = selector == ".graph-view-controls>summary" and property_name == "padding" and size == 6
                    landing_gutter = selector in (".landing-page .app-topbar", ".landing-page .app-footer") and property_name == "padding" and size == 1200 and "calc((100vw - 1200px)/2)" in value
                    if not (control_padding or gutter or panel_head or bar_offset or mobile_menu or view_control or landing_gutter):
                        failures.append((selector, property_name, value, context))
        self.assertEqual(failures, [])

    def test_r2_no_font_longhands_reset_by_shorthand(self):
        dead = []
        for selector, body, context in self.rules:
            declarations = body.split(";")
            for index, declaration in enumerate(declarations):
                if declaration.startswith("font:"):
                    dead.extend((selector, previous, context) for previous in declarations[:index]
                                if previous.startswith(("font-family:", "font-weight:", "font-size:", "line-height:", "font-variant-numeric:")))
        self.assertEqual(dead, [])

    def test_r2_measure_and_page_head(self):
        self.assertNotIn("85ch", self.css)
        page_head = next(body for selector, body, context in self.rules if selector == ".page-head" and not context)
        for declaration in ("display:flex", "align-items:center", "justify-content:space-between", "gap:16px", "min-height:32px", "margin:0 0 16px"):
            self.assertIn(declaration, page_head)
        self.assertTrue(any(selector == "p" and "max-width:35rem" in body for selector, body, context in self.rules))
        for name, page in self.pages.items():
            with self.subTest(page=name):
                self.assertRegex(page, r'<[^>]+class="[^" ]*(?:[^">]* )?page-head(?: [^">]*)?"[^>]*>')

    def test_r2_grid_gutters_surface_insets_and_stat_strip(self):
        declarations = {}
        for selector, body, context in self.rules:
            if context:
                continue
            for part in selector.split(","):
                declarations.setdefault(part.strip(), {}).update(dict(re.findall(r"(?:^|;)([a-z-]+):([^;]+)", body)))
        for selector in (".analytics-grid", ".live-grid", ".report-tools", ".proof-grid", ".explore-grid", ".summary-grid", ".workbench-grid", ".tool-form", ".report-form"):
            with self.subTest(grid=selector):
                self.assertEqual(declarations[selector].get("gap"), "16px" if selector == ".tool-form" else "12px")
        for selector in (".hero-grid", ".workspace-grid", ".profile-grid", ".profile-account-context", ".verification-workspace"):
            with self.subTest(split=selector):
                self.assertEqual(declarations[selector].get("gap"), "12px" if selector == ".verification-workspace" else "24px")
        for selector, padding in {".analytics-note": "0 12px 12px", ".analytics-bin-values": "16px", ".graph-inspector": "12px", ".feed-health": "16px", ".graph-empty": "32px 16px"}.items():
            with self.subTest(surface=selector):
                self.assertEqual(declarations[selector].get("padding"), padding)
        for selector in ("dl.profile-facts", "dl.investigation-summary", ".analytics-inline", ".profile-provenance .analytics-inline", "p.workspace-context", ".feed-health"):
            with self.subTest(strip=selector):
                self.assertEqual(declarations[selector].get("display"), "grid")
                self.assertEqual(declarations[selector].get("grid-template-columns"), "repeat(auto-fit,minmax(160px,max-content))")
                self.assertEqual(declarations[selector].get("gap"), "12px 32px")
        for selector in (".workspace-search", ".investigate-search", ".enrichment-search"):
            self.assertEqual(declarations[selector].get("max-width"), "880px")
        self.assertEqual(declarations[".tool-form"].get("max-width"), "960px")
        self.assertEqual(declarations[".stat-value"].get("font"), "300 24px/30px var(--font-code)")
        self.assertEqual(declarations[".stat-value"].get("letter-spacing"), "0")
        self.assertEqual(declarations[".stat-label"].get("font"), "400 12px/18px var(--font-code)")
        self.assertEqual(declarations[".stat-label"].get("letter-spacing"), "0.24px")
        self.assertEqual(declarations[".stat-label"].get("text-transform"), "uppercase")
        self.assertTrue(any("min-width:2200px" in "".join(context).replace(" ", "") and "max-width:1920px" in body for selector, body, context in self.rules if "main" in selector or ".app-content" in selector))
        for selector in (".proof-grid", ".explore-grid"):
            self.assertTrue(any(selector in other.split(",") and "min-width:1700px" in "".join(context).replace(" ", "") and "grid-template-columns:repeat(3,minmax(0,1fr))" in body for other, body, context in self.rules))

    def template_pages(self):
        pages = [name + ".html" for name in ("index", "explore", "investigate", "feed", "workspace", "verify", "issuers", "top-receivers", "404", "accounts/wrap.near", "accounts/usdt.tether-token.near", "entities/catalog", "entities/organisations", "flows/rhea-collector", "clusters/rhea", "incidents/rhea", "records/index")]
        pages.append(next(name for name in self.pages if re.fullmatch(r"accounts/[a-f0-9]{64}\.html", name)))
        for kind in ("validator", "dao"):
            pages.append(next(name for name, text in self.pages.items() if name.startswith("entities/") and 'class="profile-kind"' in text and re.search(r'class="profile-kind">' + kind, text, re.IGNORECASE)))
        pages.append(next(name for name in self.pages if name.startswith("records/") and name != "records/index.html"))
        self.assertEqual(len(set(pages)), 21)
        return pages

    def test_browser_r2_type_histogram_and_page_head_alignment(self):
        for width in (1440, 390):
            with self.browser.new_context(viewport={"width": width, "height": 900}, java_script_enabled=True) as context:
                context.route("**/*", self.local_route)
                page = context.new_page()
                tops = []
                for name in self.template_pages():
                    page.goto(self.origin + "/" + name)
                    page.wait_for_load_state("networkidle")
                    page.evaluate("document.fonts.ready")
                    heading = page.locator("main h1").first
                    shell = page.locator("main .page-head").first
                    if name != "index.html":
                        tops.append(shell.bounding_box()["y"])
                    self.assertGreaterEqual(heading.bounding_box()["y"], shell.bounding_box()["y"])
                    self.assertLessEqual(heading.bounding_box()["y"] + heading.bounding_box()["height"], shell.bounding_box()["y"] + shell.bounding_box()["height"])
                    sizes = page.locator("body *").evaluate_all("nodes => [...new Set(nodes.filter(node => node.getClientRects().length && getComputedStyle(node).display !== 'none' && !node.matches('.site-brand,.app-mobile-brand,.site-brand *,.app-mobile-brand *')).map(node => getComputedStyle(node).fontSize))]")
                    with self.subTest(width=width, page=name):
                        if name == "index.html":
                            sizes = page.locator("body *").evaluate_all("nodes => [...new Set(nodes.filter(node => !node.closest('.landing') && node.getClientRects().length && getComputedStyle(node).display !== 'none' && !node.matches('.site-brand,.app-mobile-brand,.site-brand *,.app-mobile-brand *')).map(node => getComputedStyle(node).fontSize))]")
                        self.assertTrue(set(sizes) <= ({"12px", "13px", "14px", "16px", "18px", "20px", "22px", "24px", "28px"} | ({"15px"} if width <= 760 else set())), sizes)
                        for spacing in page.locator(".graph-node-label,.graph-node-detail").evaluate_all("nodes => nodes.map(node => getComputedStyle(node).letterSpacing)"):
                            self.assertIn(spacing, ("normal", "0px"))
                self.assertLessEqual(max(tops) - min(tops), 1, (width, tops))

    def test_browser_r2_text_roles(self):
        sample = ('<section id="role-samples"><span id="code-reference" class="mono">1</span><span id="display-reference" style="font-family:var(--font-display)">1</span>'
                  '<table class="profile-data-table"><tbody><tr><td class="num">12</td><td class="mono">account.near</td></tr></tbody></table>'
                  '<table class="investigate-table"><tbody><tr><td class="num">12</td><td class="mono">account.near</td></tr></tbody></table>'
                  '<table class="portfolio-table"><tbody><tr><td class="num">12</td><td class="mono">account.near</td></tr></tbody></table>'
                  '<svg><text class="graph-node-label">account.near</text><text class="graph-node-detail">Recorded</text></svg>'
                  '<button class="graph-account-button">account.near</button><button id="role-button">Read</button>'
                  '<div class="graph-toolbar"><label class="graph-filter"><select><option>All</option></select></label></div>'
                  '<details class="profile-source-detail" open><summary>Source</summary><pre>Recorded source</pre></details>'
                  '<div class="profile-live-result"><pre>Recorded result</pre></div>'
                  '<div class="panel"><h3>Panel</h3></div><div class="graph-loading"><h2>Graph</h2></div>'
                  '<div class="delivery-heading"><h2>Delivery</h2></div>'
                  '<div class="flow-transfer"><p class="mono">sender.near to receiver.near</p><p class="mono muted">Recorded transaction</p><p>Recorded transfer.</p></div>'
                  '<div class="analytics-counterparty"><small>Recorded</small></div><span class="search-result-type">Account</span>'
                  '<div class="graph-range"><input aria-label="Range"></div><div class="chart-range-controls"><select aria-label="Interval"><option>All</option></select></div>'
                  '<div class="graph-share-result"><input aria-label="Saved view"></div><div class="cloud-workspace"><div class="cloud-portability"><input type="file" aria-label="Import"></div></div>'
                  '<dl class="investigation-summary"><div><dt>Rows</dt><dd>12</dd></div></dl>'
                  '<div class="registry-count">1 <span class="stat-connective">of</span> 2</div>'
                  '<span class="stat-label">Rows</span><span class="stat-value">12</span>'
                  '<dl class="profile-facts"><div><dt>Latest label observation</dt><dd class="stat-timestamp">Recorded date</dd></div></dl></section>')
        for width in (1440, 390):
            with self.browser.new_context(viewport={"width": width, "height": 900}, java_script_enabled=False) as context:
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                for name in self.template_pages():
                    document = re.sub(r'<script\b[^>]*>.*?</script>', '', self.pages[name], flags=re.DOTALL)
                    document = re.sub(r'<link\b[^>]*>', '', document)
                    document = document.replace('</head>', '<style>' + self.css + '</style></head>')
                    page.set_content(document.replace('</main>', sample + '</main>'))
                    code_family = page.locator("#code-reference").evaluate("node => getComputedStyle(node).fontFamily")
                    ui_family = page.locator("body").evaluate("node => getComputedStyle(node).fontFamily")
                    display_family = page.locator("#display-reference").evaluate("node => getComputedStyle(node).fontFamily")
                    self.assertTrue(display_family.startswith("Manrope"), display_family)
                    checks = [(".profile-id", "22px", "28px", code_family, "400"),
                              ("#role-samples .stat-label", "12px", "18px", code_family, "400"),
                              ("#role-samples .stat-value", "24px", "30px", code_family, "300"),
                              ("#role-samples .stat-timestamp", "12px", "18px", code_family, "400"),
                              ("#role-samples .graph-account-button", "12px", "18px", code_family, "400"),
                              ("#role-samples .panel h3", "16px", "22px", display_family, "600"),
                              ("#role-samples .investigation-summary dt", "12px", "18px", code_family, "400"),
                              ("#role-samples .registry-count .stat-connective", "14px", "21px" if width > 760 else "22px", ui_family, "400"),
                              ("#role-samples .graph-node-label", "14px", "22px" if width <= 760 else "21px", code_family, "400"),
                              ("#role-samples .graph-node-detail", "12px", "18px", ui_family, "400")]
                    with self.subTest(width=width, page=name, role="connective colour"):
                        self.assertEqual(page.locator("#role-samples .registry-count .stat-connective").evaluate("node => getComputedStyle(node).color"), page.locator("#role-samples .stat-label").evaluate("node => getComputedStyle(node).color"))
                    with self.subTest(width=width, page=name, role="heading alignment"):
                        self.assertEqual(page.locator("#role-samples .delivery-heading").evaluate("node => getComputedStyle(node).alignItems"), "center")
                    with self.subTest(width=width, page=name, role="graph heading weight"):
                        self.assertEqual(page.locator("#role-samples .graph-loading h2").evaluate("node => [getComputedStyle(node).fontWeight,getComputedStyle(node).fontFamily]"), ["600", display_family])
                    if width <= 760:
                        checks += [(".analytics-distribution-row .cell-label", "12px", "18px", ui_family, "400"),
                                   ("#role-samples td.num,#role-samples td.mono", "14px", "20px" if width <= 760 else "22px", code_family, "400"),
                                   ("#role-samples .graph-range input,#role-samples .cloud-portability input", "16px", "24px", ui_family, "400"),
                                   ("#role-samples .chart-range-controls select", "16px", "normal", ui_family, "400"),
                                   ("#role-samples .graph-share-result input", "16px", "24px", code_family, "400"),
                                   ("#role-samples .flow-transfer p.mono", "12px", "18px", code_family, "400"),
                                   ("#role-samples .flow-transfer p:not(.mono)", "14px", "21px" if width > 760 else "22px", ui_family, "400"),
                                   ("#role-samples .analytics-counterparty small,#role-samples .search-result-type,.analytics-full-account", "12px", "18px", ui_family, "400")]
                        micros = page.locator("body *").evaluate_all("nodes => nodes.filter(node => node.getClientRects().length && !node.closest('.search-key') && getComputedStyle(node).fontSize === '11px').map(node => ({tag:node.tagName,classes:node.className}))")
                        with self.subTest(width=width, page=name, role="phone micro"):
                            self.assertEqual(micros, [])
                    else:
                        checks.append(("#role-button", "13px", "18px", ui_family, "400"))
                        checks.append(('.app-header-navigation a:not([aria-current="page"]),.app-menu:not(:has([aria-current="page"]))>summary', "13px", "18px", ui_family, "300"))
                        checks.append(('.app-header-navigation a[aria-current="page"],.app-menu:has([aria-current="page"])>summary', "13px", "18px", ui_family, "500"))
                        with self.subTest(width=width, page=name, role="filter cap"):
                            self.assertLessEqual(page.locator("#role-samples .graph-filter select").bounding_box()["width"], 240)
                    for selector, size, line, family, weight in checks:
                        for actual in page.locator(selector).evaluate_all("nodes => nodes.map(node => {const style=getComputedStyle(node);return [style.fontSize,style.lineHeight,style.fontFamily,style.fontWeight,style.letterSpacing]})"):
                            with self.subTest(width=width, page=name, role=selector):
                                self.assertEqual(actual[:4], [size, line, family, weight])
                                self.assertIn(actual[4], ("0.24px",) if selector in ("#role-samples .stat-label", "#role-samples .investigation-summary dt") else ("normal", "0px"))

    def test_r2_quote_identifier_markup_preserves_escaped_text(self):
        identifier = "abcdef01" * 8
        text = '<quoted> & "source" ' + identifier + ' then ' + identifier
        block = render.quote_block({"source": "read", "text": text}, {"read": {"url": "https://example.test"}})
        self.assertEqual(block.count('<span class="data-id">' + identifier + '</span>'), 2)
        self.assertIn('&lt;quoted&gt; &amp; &quot;source&quot;', block)
        self.assertNotIn('<quoted>', block)
        self.assertIn(render.esc(text), re.sub(r'<span class="data-id">(.*?)</span>', r'\1', block))

    def test_browser_r2_review_prose_word_boundaries(self):
        with self.browser.new_context(java_script_enabled=True) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            for width in (1440, 1024, 768, 390, 360, 320):
                page.set_viewport_size({"width": width, "height": 900})
                for name, selector in (("incidents/rhea.html", "blockquote.data"), ("flows/rhea-collector.html", ".analytics-counterparty"), ("explore.html", "q.data,.analytics-counterparty")):
                    page.goto(self.origin + "/" + name)
                    page.wait_for_load_state("networkidle")
                    page.evaluate("document.fonts.ready")
                    page.locator("details").evaluate_all("nodes => nodes.forEach(node=>node.open=true)")
                    nodes = page.locator(selector)
                    self.assertGreater(nodes.count(), 0)
                    measured = nodes.evaluate_all("""nodes => nodes.filter(node=>node.checkVisibility()).map(node => {
                        const chars=[], walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);
                        while(walker.nextNode()) {
                            const text=walker.currentNode;
                            for(let i=0;i<text.length;i++) {
                                const range=document.createRange();range.setStart(text,i);range.setEnd(text,i+1);
                                const rect=range.getBoundingClientRect();
                                chars.push({letter:text.textContent[i],text,top:rect.width&&rect.height?Math.round(rect.top):null});
                            }
                        }
                        const content=chars.map(char=>char.letter).join(''), ids=new Set(), words=new Set(), lines=new Map(), splits=[];
                        for(const match of content.matchAll(/[a-f0-9]{64}/g)) {
                            for(let i=match.index;i<match.index+64;i++) ids.add(i);
                        }
                        for(const match of content.matchAll(/(?<![a-z0-9_])[a-z]+(?![a-z0-9_])/gi)) {
                            for(let i=match.index;i<match.index+match[0].length;i++) words.add(i);
                        }
                        chars.forEach((char,i)=>{
                            if(char.top!==null&&char.letter.trim()) lines.set(char.top,(lines.get(char.top)||'')+char.letter);
                            if(i&&char.text===chars[i-1].text&&!ids.has(i)&&words.has(i)&&words.has(i-1)&&char.top!==null&&chars[i-1].top!==null&&char.top!==chars[i-1].top) splits.push(content.slice(Math.max(0,i-12),i)+'|'+content.slice(i,i+12));
                        });
                        return {splits,lines:[...lines.values()]};
                    })""")
                    with self.subTest(page=name, width=width):
                        self.assertTrue(measured)
                        self.assertEqual([split for result in measured for split in result["splits"]], [])
                        self.assertEqual([line for result in measured for line in result["lines"] if len(line) == 1], [])
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    def test_browser_delivery_action_buttons_keep_whole_words(self):
        labels = ["Create a new link", "Connect a Telegram group", "Confirm observed destination", "Cancel connection"]
        with self.browser.new_context(java_script_enabled=False) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            for width in (1280, 768, 375, 320):
                page.set_viewport_size({"width": width, "height": 900})
                page.goto(self.origin + "/workspace.html")
                page.wait_for_load_state("networkidle")
                page.evaluate("""labels => {
                    const actions=document.createElement('div');actions.className='delivery-actions';
                    for(const label of labels){const button=document.createElement('button');button.type='button';button.textContent=label;actions.append(button);}
                    document.getElementById('delivery-controls').replaceChildren(actions);
                    document.getElementById('delivery-history').open=true;
                }""", labels)
                measured = page.locator("#team-delivery .delivery-actions button").evaluate_all("""nodes => nodes.map(node => {
                    const text=node.firstChild, tops=[];
                    for(let i=0;i<text.length;i++){const range=document.createRange();range.setStart(text,i);range.setEnd(text,i+1);tops.push(Math.round(range.getBoundingClientRect().top));}
                    const splits=[];
                    for(let i=1;i<text.length;i++) if(/\\S/.test(text.data[i])&&/\\S/.test(text.data[i-1])&&tops[i]!==tops[i-1]) splits.push(text.data.slice(0,i)+'|'+text.data.slice(i));
                    return {label:text.data,splits,lines:new Set(tops.filter((top,i)=>/\\S/.test(text.data[i]))).size,top:Math.round(node.getBoundingClientRect().top),wrap:getComputedStyle(node).overflowWrap};
                })""")
                with self.subTest(width=width):
                    self.assertEqual([row["label"] for row in measured], labels + ["Newer deliveries", "Older deliveries"])
                    self.assertEqual([split for row in measured for split in row["splits"]], [])
                    self.assertNotIn("anywhere", {row["wrap"] for row in measured})
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    if width >= 768:
                        self.assertEqual({row["top"] for row in measured[:4]}, {measured[0]["top"]})
                        self.assertEqual({row["lines"] for row in measured}, {1})

    def test_browser_r2_review_counterparty_identifiers_one_line(self):
        with self.browser.new_context(java_script_enabled=True) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            for width in (1440, 1024, 768):
                page.set_viewport_size({"width": width, "height": 900})
                for name in ("accounts/wrap.near.html", "accounts/usdt.tether-token.near.html"):
                    page.goto(self.origin + "/" + name)
                    page.wait_for_load_state("networkidle")
                    page.evaluate("document.fonts.ready")
                    counts = page.locator("#profile-counterparties q.data").evaluate_all("""nodes => nodes.filter(node=>/^[a-f0-9]{64}$/.test(node.textContent)).map(node=>{
                        const range=document.createRange();range.selectNodeContents(node);
                        return new Set([...range.getClientRects()].map(rect=>Math.round(rect.top))).size;
                    })""")
                    with self.subTest(page=name, width=width):
                        self.assertEqual(len(counts), 12)
                        self.assertEqual(counts, [1] * 12)
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    def test_browser_r2_review_numeric_contexts(self):
        record = next(name for name in self.pages if name.startswith("records/") and name != "records/index.html")
        cases = (("investigate.html", ".enrichment-pagination span"),
                 ("explore.html", ".graph-status,.chart-readout,.chart-selection,.graph-range-message,.graph-meta dd:not(.graph-identifier)"),
                 (record, '[data-record-path$="_at"],[data-record-path$=".at"],[data-record-path="version"]'),
                 ("incidents/rhea.html", ".profile-route-bridge>span"))
        with self.browser.new_context(java_script_enabled=True) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            for name, selectors in cases:
                page.goto(self.origin + "/" + name)
                page.wait_for_load_state("networkidle")
                page.evaluate("document.fonts.ready")
                page.locator("details").evaluate_all("nodes => nodes.forEach(node=>node.open=true)")
                for selector in selectors.split(','):
                    nodes = page.locator(selector)
                    self.assertGreater(nodes.count(), 0, (name, selector))
                    for actual, widths in nodes.evaluate_all("""nodes => nodes.map(node=>{
                        const actual=getComputedStyle(node).fontVariantNumeric;
                        const sample=document.createElement('span');sample.style.whiteSpace='nowrap';node.append(sample);
                        sample.textContent='1111';const first=sample.getBoundingClientRect().width;
                        sample.textContent='8888';const second=sample.getBoundingClientRect().width;sample.remove();
                        return [actual,[first,second]];
                    })"""):
                        with self.subTest(page=name, selector=selector):
                            self.assertTrue({"lining-nums", "tabular-nums", "slashed-zero"} <= set(actual.split()))
                            self.assertGreater(widths[0], 0)
                            self.assertAlmostEqual(widths[0], widths[1], delta=0.01)

    def test_browser_r2_review_tabular_figures(self):
        for width in (1440, 390):
            with self.browser.new_context(viewport={"width": width, "height": 900}) as context:
                context.route("**/*", self.local_route)
                page = context.new_page()
                page.goto(self.origin + "/entities/catalog.html")
                page.evaluate("document.fonts.ready")
                page.locator(".entity-table tbody tr").first.evaluate("node => node.insertAdjacentHTML('beforeend', '<td class=num><span>1111</span><span>8888</span></td>')")
                for selector in ("td.num", ".entity-table-status", ".entity-table-pagination"):
                    with self.subTest(width=width, selector=selector):
                        self.assertIn("tabular-nums", page.locator(selector).first.evaluate("node => getComputedStyle(node).fontVariantNumeric"))
                figures = page.locator("td.num span").evaluate_all("nodes => nodes.map(node => node.getBoundingClientRect().width)")
                self.assertEqual(len(figures), 2)
                self.assertAlmostEqual(figures[0], figures[1], delta=0.01)
                page.locator("main").evaluate("node => node.insertAdjacentHTML('beforeend', '<span class=profile-value>12<q class=\"data profile-asset\">token-issuer.near</q></span><time>2026-11-13</time><div class=stat-value>12<span class=stat-connective>of</span></div>')")
                for selector in (".profile-value", "time", ".stat-value"):
                    with self.subTest(width=width, selector=selector):
                        self.assertIn("tabular-nums", page.locator(selector).first.evaluate("node => getComputedStyle(node).fontVariantNumeric"))
                for selector in ("body", ".entity-table td.mono", ".entity-table td a", ".profile-value q.data", ".stat-connective"):
                    with self.subTest(width=width, selector=selector):
                        actual = page.locator(selector).first.evaluate("node => getComputedStyle(node).fontVariantNumeric")
                        self.assertEqual(actual, "lining-nums tabular-nums slashed-zero" if selector in (".entity-table td.mono", ".entity-table td a") else "normal")
                for name, value, identifier in (("issuers.html", "#issuer-status tbody td:nth-child(4)", "#issuer-status tbody td:first-child q.data"),
                                                 ("accounts/wrap.near.html", "#profile-transfers tbody td:nth-child(5)", "#profile-transfers tbody td:nth-child(3) q.data"),
                                                 ("accounts/usdt.tether-token.near.html", "#profile-transfers tbody td:nth-child(5)", "#profile-transfers tbody td:nth-child(3) q.data")):
                    page.goto(self.origin + "/" + name)
                    page.wait_for_load_state("networkidle")
                    for selector, expected in ((value, "lining-nums tabular-nums slashed-zero"), (identifier, "normal")):
                        nodes = page.locator(selector)
                        self.assertGreater(nodes.count(), 0)
                        for actual in nodes.evaluate_all("nodes => nodes.map(node=>getComputedStyle(node).fontVariantNumeric)"):
                            with self.subTest(width=width, page=name, selector=selector):
                                self.assertEqual(actual, expected)

    def test_browser_r2_review_identifier_hyphens(self):
        record = next(name for name in self.pages if name.startswith("records/") and name != "records/index.html")
        with self.browser.new_context(viewport={"width": 1440, "height": 900}, java_script_enabled=True) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            cases = [(record, '[data-record-path="publisher"]', "-owner.testnet"), ("issuers.html", "td q.data", "usdt.tether-token.near")]
            cases += [(name + ".html", ".graph-meta dd.graph-identifier", "usdt.tether-token.near") for name in ("explore", "flows/rhea-collector")]
            cases += [("clusters/rhea.html", ".graph-inspector-title.graph-identifier", "usdt.tether-token.near"),
                      ("investigate.html", "#investigate-title", "usdt.tether-token.near"),
                      ("workspace.html", "#watched-accounts li>span.mono", "usdt.tether-token.near"),
                      ("records/index.html", 'td[data-label="Subject"] q.data', "kestrelridge-mallory.testnet"),
                      ("accounts/wrap.near.html", ".account-assertions td:not(.mono) q.data", "v2.ref-finance.near"),
                      ("accounts/usdt.tether-token.near.html", ".account-assertions td:not(.mono) q.data", "v2.ref-finance.near"),
                      ("entities/catalog.html", ".entity-table q.data", "nep141:abg-966.meme-cooking.near")]
            for name, selector, identifier in cases:
                page.goto(self.origin + "/" + name)
                page.wait_for_load_state("networkidle")
                page.evaluate("document.fonts.ready")
                if name == "investigate.html":
                    page.locator("#investigate-account").fill(identifier)
                    page.locator("#investigate-form").evaluate("node => node.requestSubmit()")
                elif name == "workspace.html":
                    page.locator("#watch-account").fill(identifier)
                    page.locator("#watch-form").evaluate("node => node.requestSubmit()")
                elif name == "clusters/rhea.html":
                    page.locator(selector).first.evaluate("(node,text) => node.textContent=text", identifier)
                target = page.locator(selector).filter(has_text=identifier).first
                self.assertEqual(target.count(), 1, name)
                target.evaluate("node => {for(let parent=node.parentElement;parent;parent=parent.parentElement) if(parent.tagName==='DETAILS') parent.open=true}")
                variant = target.evaluate("node => getComputedStyle(node).fontVariantNumeric")
                code_variant = "graph-identifier" in selector or name in ("workspace.html", "investigate.html")
                self.assertEqual(variant, "lining-nums tabular-nums slashed-zero" if code_variant else "normal")
                code_family = code_variant or name in (record, "issuers.html", "records/index.html", "entities/catalog.html") or selector.startswith(".account-assertions")
                expected_family = page.locator("body").evaluate("(node, code) => code ? getComputedStyle(node).getPropertyValue('--font-code').trim() : getComputedStyle(node).fontFamily", code_family)
                actual_family = target.evaluate("node => getComputedStyle(node).fontFamily")
                self.assertEqual(re.sub(r"\s*,\s*", ",", actual_family), re.sub(r"\s*,\s*", ",", expected_family), (name, selector))
                widths = target.evaluate("""node => {
                    const text=node.firstChild, index=text.textContent.indexOf('-');
                    const range=document.createRange();range.setStart(text,index);range.setEnd(text,index+1);
                    const actual=range.getBoundingClientRect().width;
                    node.style.setProperty('font-variant-numeric','normal','important');
                    return [actual,range.getBoundingClientRect().width];
                }""")
                with self.subTest(page=name):
                    self.assertGreater(widths[0], 0)
                    self.assertAlmostEqual(widths[0], widths[1], delta=0.01)

    def test_browser_r2_review_no_single_character_lines(self):
        cases = [(name, width, "td q.data", False) for name in ("accounts/wrap.near.html", "accounts/usdt.tether-token.near.html") for width in (1440, 1024, 768)]
        cases += [(name, width, selector, expanded) for name, width, selector in (("incidents/rhea.html", 390, "blockquote.data"), ("flows/rhea-collector.html", 320, ".analytics-counterparty")) for expanded in (False, True)]
        cases += [("issuers.html", width, ":is(#issuer-status,#issuer-history) td q.data", False) for width in (1440, 1024, 768, 390, 320)]
        for theme in ("light", "dark"):
            with self.browser.new_context(color_scheme=theme, java_script_enabled=True) as context:
                context.route("**/*", self.local_route)
                context.add_init_script("localStorage.setItem('siyab-theme', '" + theme + "')")
                page = context.new_page()
                for name, width, selector, expanded in cases:
                    page.set_viewport_size({"width": width, "height": 900})
                    page.goto(self.origin + "/" + name)
                    page.wait_for_load_state("networkidle")
                    page.evaluate("document.fonts.ready")
                    nodes = page.locator(selector)
                    self.assertGreater(nodes.count(), 0)
                    if expanded:
                        nodes.evaluate_all("""nodes => nodes.forEach(node => {
                            for(let parent=node.parentElement;parent;parent=parent.parentElement) {
                                if(parent.tagName==='DETAILS') parent.open=true;
                            }
                            node.querySelectorAll('details').forEach(detail=>detail.open=true);
                        })""")
                    counts = nodes.evaluate_all("""nodes => nodes.filter(node=>node.checkVisibility()).map(node => {
                        const counts=new Map(), walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);
                        while(walker.nextNode()) {
                            const text=walker.currentNode;
                            for(let i=0;i<text.length;i++) {
                                if(!text.textContent[i].trim()) continue;
                                const range=document.createRange();range.setStart(text,i);range.setEnd(text,i+1);
                                const rect=range.getBoundingClientRect();if(!rect.width||!rect.height) continue;
                                const top=Math.round(rect.top);counts.set(top,(counts.get(top)||0)+1);
                            }
                        }
                        return [...counts.values()];
                    })""")
                    with self.subTest(page=name, width=width, expanded=expanded, theme=theme):
                        self.assertTrue(counts)
                        self.assertTrue(all(1 not in lines for lines in counts), counts)
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    def test_browser_r2_review_profile_export_alignment(self):
        for theme in ("light", "dark"):
            with self.browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=theme, java_script_enabled=True) as context:
                context.route("**/*", self.local_route)
                page = context.new_page()
                for name in ("accounts/wrap.near.html", "accounts/usdt.tether-token.near.html"):
                    page.goto(self.origin + "/" + name)
                    page.evaluate("document.fonts.ready")
                    toolbar = page.locator("#account-activity>.profile-table-tools")
                    labels = toolbar.locator("label").evaluate_all("nodes => nodes.map(node => {const rect=node.getBoundingClientRect();return [rect.x,rect.width]})")
                    export = toolbar.locator("[data-export-table]").bounding_box()
                    table = page.locator("#profile-transfers").bounding_box()
                    with self.subTest(page=name, theme=theme):
                        self.assertAlmostEqual(export["x"] + export["width"], table["x"] + table["width"], delta=1)
                        toolbar.locator("[data-export-table]").evaluate("node => node.style.marginLeft='0px'")
                        self.assertEqual(labels, toolbar.locator("label").evaluate_all("nodes => nodes.map(node => {const rect=node.getBoundingClientRect();return [rect.x,rect.width]})"))

    def test_browser_r2_review_record_command_wrap(self):
        with self.browser.new_context(viewport={"width": 1440, "height": 900}, java_script_enabled=True) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            for name in self.pages:
                if not name.startswith("records/") or name == "records/index.html":
                    continue
                page.goto(self.origin + "/" + name)
                page.evaluate("document.fonts.ready")
                command = page.locator(".record-content p>code")
                self.assertGreater(command.count(), 0)
                for lines in command.evaluate_all("nodes => nodes.map(node => {const range=document.createRange();range.selectNodeContents(node);return new Set([...range.getClientRects()].map(rect=>rect.top)).size})"):
                    with self.subTest(page=name):
                        self.assertLessEqual(lines, 2)
                for lines in command.evaluate_all("""nodes => nodes.map(node => {
                    const walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);
                    let text,index=-1;
                    while(walker.nextNode()) {
                        text=walker.currentNode;index=text.textContent.search(/[a-f0-9]{64}/);
                        if(index>=0) break;
                    }
                    if(index<0) return 0;
                    const range=document.createRange();range.setStart(text,index);range.setEnd(text,index+64);
                    return new Set([...range.getClientRects()].map(rect=>rect.top)).size;
                })"""):
                    with self.subTest(page=name, part="fixture identifier"):
                        self.assertEqual(lines, 1)

    def test_browser_r2_review_record_command_word_boundaries(self):
        with self.browser.new_context(java_script_enabled=True) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            for width in (1440, 1024, 768, 390):
                page.set_viewport_size({"width": width, "height": 900})
                for name in self.pages:
                    if not name.startswith("records/") or name == "records/index.html":
                        continue
                    page.goto(self.origin + "/" + name)
                    page.evaluate("document.fonts.ready")
                    result = page.locator(".record-content p>code").evaluate("""node=>{
                        const walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT),chars=[],lines=new Map();
                        while(walker.nextNode()) {
                            const text=walker.currentNode;
                            for(let i=0;i<text.length;i++) {
                                const range=document.createRange();range.setStart(text,i);range.setEnd(text,i+1);
                                const top=Math.round(range.getBoundingClientRect().top);
                                chars.push([text.textContent[i],top]);
                                if(text.textContent[i].trim()) lines.set(top,(lines.get(top)||'')+text.textContent[i]);
                            }
                        }
                        const length='python -m intel record-check'.length;
                        return {commandLines:new Set(chars.slice(0,length).map(char=>char[1])).size,lines:[...lines.values()]};
                    }""")
                    with self.subTest(page=name, width=width):
                        self.assertEqual(result["commandLines"], 1)
                        self.assertTrue(all(len(line) > 1 for line in result["lines"]), result)
                        if width == 1440:
                            self.assertLessEqual(len(result["lines"]), 2)
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    def test_browser_r2_review_component_widths(self):
        with self.browser.new_context(viewport={"width": 1440, "height": 900}) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            for name, selector, width in (("entities/organisations.html", ".registry-group", 1392),
                                          ("incidents/rhea.html", ".profile-route", 1392),
                                          ("incidents/rhea.html", ".profile-timeline>li", 798)):
                page.goto(self.origin + "/" + name)
                with self.subTest(selector=selector):
                    self.assertAlmostEqual(page.locator(selector).first.bounding_box()["width"], width, delta=1)
            page.goto(self.origin + "/workspace.html")
            side = page.locator("#browser-workspace .workbench-grid--stacked>.workspace-side")
            self.assertEqual(side.count(), 1)
            investigations = page.locator("#browser-workspace .workbench-grid--stacked>.workspace-section")
            self.assertEqual(len(side.evaluate("node => getComputedStyle(node).gridTemplateColumns").split()), 1)
            self.assertAlmostEqual(side.bounding_box()["y"], investigations.bounding_box()["y"], delta=1)
            self.assertGreater(side.bounding_box()["x"], investigations.bounding_box()["x"] + investigations.bounding_box()["width"])
            page.set_viewport_size({"width": 1024, "height": 900})
            self.assertEqual(len(side.evaluate("node => getComputedStyle(node).gridTemplateColumns").split()), 2)
            children = side.locator(":scope>section")
            self.assertGreaterEqual(children.count(), 2)
            self.assertAlmostEqual(children.nth(0).bounding_box()["y"], children.nth(1).bounding_box()["y"], delta=1)
            page.set_viewport_size({"width": 390, "height": 900})
            self.assertEqual(len(side.evaluate("node => getComputedStyle(node).gridTemplateColumns").split()), 1)

    def test_browser_r2_review_font_families_and_weights(self):
        tables = ("account-assertions", "account-inferred", "entity-table", "entity-table entity-table-inferred",
                  "investigate-table", "feed-table", "record-index-table", "profile-data-table", "flow-shares-table", "jobs-table")
        sample = '<section id="review-roles"><span class="mono" id="review-code">account.near</span>'
        for table in tables:
            sample += ('<div class="table"><table class="' + table + '"><thead><tr><th>Account</th></tr></thead>'
                       '<tbody><tr><td class="mono"><q class="data">account.near</q></td></tr></tbody></table></div>')
        sample += ('<table id="review-header"><thead><tr><th>Header</th></tr></thead></table>'
                   '<form class="verify-form"><input aria-label="Record"></form><p class="profile-outcome">Unknown</p>'
                   '<div class="investigation-jobs">Jobs<div class="jobs-heading"><h2>Jobs</h2></div></div>'
                   '<div class="delivery-heading"><h2>Delivery</h2></div>'
                   '<div class="cloud-case"><h4><a href="#">Case</a></h4></div>'
                   '<div class="saved-investigation"><time>Recorded date</time></div>'
                   '<table class="feed-table"><tbody><tr><td><time>Recorded date</time><q>account.near</q></td></tr></tbody></table>'
                   '<table class="record-index-table"><tbody><tr><td>Record</td></tr></tbody></table>'
                   '<table class="verify-example-table"><thead><tr><th>Field</th></tr></thead><tbody><tr><td>Value</td></tr></tbody></table>'
                   '<table class="jobs-table"><thead><tr><th>Field</th></tr></thead><tbody><tr><td data-label="Amount">12</td></tr></tbody></table>'
                   '<span class="feed-amount">12</span><span class="profile-value">12</span>'
                   '<ol class="profile-timeline"><li><time>Recorded date</time></li></ol>'
                   '<div class="profile-route"><div>account.near</div></div></section>')
        for width in (390, 768, 1024, 1440):
            with self.browser.new_context(viewport={"width": width, "height": 900}) as context:
                context.route("**/*", self.local_route)
                page = context.new_page()
                page.goto(self.origin + "/explore.html")
                page.locator("main").evaluate("(node, sample) => node.insertAdjacentHTML('beforeend', sample)", sample)
                page.evaluate("document.fonts.ready")
                code = page.locator("#review-code").evaluate("node => getComputedStyle(node).fontFamily")
                ui = page.locator("body").evaluate("node => getComputedStyle(node).fontFamily")
                for table in tables:
                    selector = "#review-roles table." + table.replace(" ", ".") + " td"
                    for target in (selector, selector + " q.data"):
                        with self.subTest(width=width, selector=target):
                            self.assertEqual(page.locator(target).first.evaluate("node => [getComputedStyle(node).fontFamily,getComputedStyle(node).fontWeight]"), [code, "400"])
                for selector, family, weight in ((".app-header-navigation a:not([aria-current])", ui, "300"),
                                                  (".app-menu:not(:has([aria-current]))>summary", ui, "400" if width <= 760 else "300"),
                                                  (".mobile-navigation-panel nav a:not([aria-current])", ui, "400" if width <= 760 else "300"),
                                                  ("#review-roles .account-assertions th,#review-header th", code, "400"),
                                                  ("#review-roles .verify-form input", code, "400"),
                                                  ("#review-roles .profile-outcome", ui, "500"),
                                                  ("#review-roles .saved-investigation time", code, "400"),
                                                  ("#review-roles .feed-table time,#review-roles .feed-table q,#review-roles .feed-amount,#review-roles .record-index-table td:last-child", code, "400"),
                                                  ("#review-roles .verify-example-table th", code, "400"),
                                                  ("#review-roles .verify-example-table td,#review-roles .jobs-table td[data-label=Amount]", code, "400"),
                                                  ("#review-roles .jobs-table th", code, "400"),
                                                  ("#review-roles .profile-value,#review-roles .profile-timeline time,#review-roles .profile-route>div", code, "400")):
                    self.assertGreater(page.locator(selector).count(), 0, selector)
                    for actual in page.locator(selector).evaluate_all("nodes => nodes.map(node => [getComputedStyle(node).fontFamily,getComputedStyle(node).fontWeight])"):
                        with self.subTest(width=width, selector=selector):
                            self.assertEqual(actual, [family, weight])
                self.assertEqual(page.locator("#review-roles .cloud-case h4 a").evaluate("node => [getComputedStyle(node).fontSize,getComputedStyle(node).fontWeight,getComputedStyle(node).fontFamily.split(',')[0]]"), ["16px", "600", "Manrope"])
                for selector in ("#review-roles .jobs-heading h2", "#review-roles .delivery-heading h2"):
                    self.assertEqual(page.locator(selector).evaluate("node => getComputedStyle(node).margin"), "0px")
                if width == 390:
                    self.assertEqual(page.locator("#review-roles .investigation-jobs").evaluate("node => [getComputedStyle(node).fontSize,getComputedStyle(node).lineHeight]"), ["14px", "22px"])

    def test_browser_r2_review_record_identifiers(self):
        record = next(name for name in self.pages if name.startswith("records/") and name != "records/index.html")
        with self.browser.new_context(viewport={"width": 1440, "height": 900}) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            page.goto(self.origin + "/" + record)
            page.evaluate("document.fonts.ready")
            for selector in ('[data-record-path="record_id"]', '[data-record-path$=".content_sha256"]'):
                nodes = page.locator(selector)
                self.assertGreater(nodes.count(), 0)
                for lines in nodes.evaluate_all("nodes => nodes.map(node => {const range=document.createRange();range.selectNodeContents(node);return new Set([...range.getClientRects()].map(rect=>rect.top)).size})"):
                    with self.subTest(selector=selector):
                        self.assertEqual(lines, 1)
            prose = page.locator("main li").filter(has=page.locator('[data-record-path="evidence[0].content_sha256"]'))
            self.assertGreater(prose.count(), 0)
            page.locator("main").evaluate("node => node.insertAdjacentHTML('beforeend', '<ul><li id=review-prose>Recorded prose</li><li class=mono id=review-identifier>account.near</li></ul>')")
            self.assertEqual(page.locator("#review-prose").bounding_box()["width"], 560)
            measure = page.locator("#review-identifier").evaluate("node => {const sample=document.createElement('span');sample.style.cssText='display:block;width:80ch';node.append(sample);const expected=getComputedStyle(sample).width;sample.remove();return [getComputedStyle(node).maxWidth,expected]}")
            self.assertEqual(measure[0], measure[1])
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                for selector in ('[data-record-path="record_id"]', '[data-record-path$=".content_sha256"]', '[data-record-path$=".url"]'):
                    for bounds in page.locator(selector).evaluate_all("nodes => nodes.map(node => [...node.getClientRects()].map(rect => [rect.left,rect.right]))"):
                        self.assertTrue(all(left >= 0 and right <= width for left, right in bounds), (width, selector, bounds))
            page.set_viewport_size({"width": 1440, "height": 900})
            page.locator('[data-record-path="record_id"]').evaluate("node => {const range=document.createRange();range.selectNodeContents(node);const width=range.getBoundingClientRect().width;node.parentElement.style.width=(width-1)+'px';node.parentElement.style.fontFamily=getComputedStyle(node).fontFamily;}")
            counts = page.locator('[data-record-path="record_id"]').evaluate("node => {const counts=new Map();for(let i=0;i<node.firstChild.length;i++){const range=document.createRange();range.setStart(node.firstChild,i);range.setEnd(node.firstChild,i+1);const top=range.getBoundingClientRect().top;counts.set(top,(counts.get(top)||0)+1)}return [...counts.values()]}")
            self.assertEqual(len(counts), 2)
            self.assertGreater(min(counts), 1, counts)

    def test_browser_r2_review_profile_heading_order(self):
        name = next(name for name, text in self.pages.items() if name.startswith("entities/") and re.search(r'class="profile-kind">validator', text, re.IGNORECASE))
        with self.browser.new_context(viewport={"width": 1440, "height": 900}) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            page.goto(self.origin + "/" + name)
            self.assertTrue(page.locator(".profile-kind").evaluate("node => Boolean(node.compareDocumentPosition(node.parentElement.querySelector('h1')) & Node.DOCUMENT_POSITION_FOLLOWING)"))
            self.assertLessEqual(page.locator(".profile-kind").bounding_box()["y"] + page.locator(".profile-kind").bounding_box()["height"], page.locator(".profile-id").bounding_box()["y"])

    def test_browser_r2_flow_reserve_with_full_styles(self):
        for width, reserve in ((1440, "765px"), (1000, "1288px")):
            with self.browser.new_context(viewport={"width": width, "height": 900}) as context:
                held = []
                def route_request(route):
                    if route.request.url.startswith(self.origin + "/graph-data/"):
                        held.append(route)
                    elif route.request.url.startswith(self.origin + "/"):
                        self.local_route(route)
                    else:
                        route.abort()
                context.route("**/*", route_request)
                page = context.new_page()
                page.goto(self.origin + "/flows/rhea-collector.html", wait_until="domcontentloaded")
                page.evaluate("document.fonts.ready")
                page.wait_for_selector(".graph-loading")
                before = page.locator("#transfers").bounding_box()["y"]
                self.assertTrue(held)
                for route in held:
                    asset = self.out / "graph-data" / route.request.url.rsplit("/", 1)[1]
                    route.fulfill(body=asset.read_bytes(), content_type="application/json")
                page.wait_for_selector(".graph-toolbar")
                page.evaluate("document.fonts.ready")
                with self.subTest(width=width):
                    self.assertEqual(page.locator(".siyab-graph").evaluate("node => getComputedStyle(node).minHeight"), reserve)
                    self.assertLessEqual(abs(page.locator("#transfers").bounding_box()["y"] - before), 1)

    def test_browser_r2_no_page_overflow_at_seven_widths(self):
        for theme in ("dark", "light"):
            with self.browser.new_context(java_script_enabled=True) as context:
                context.route("**/*", self.local_route)
                context.add_init_script("localStorage.setItem('siyab-theme', '" + theme + "')")
                page = context.new_page()
                for width in (320, 390, 480, 768, 1024, 1440, 1920):
                    page.set_viewport_size({"width": width, "height": 900})
                    for name in self.template_pages():
                        page.goto(self.origin + "/" + name)
                        page.wait_for_load_state("networkidle")
                        page.evaluate("document.fonts.ready")
                        with self.subTest(theme=theme, width=width, page=name):
                            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                        page.locator("main details").evaluate_all("nodes => nodes.forEach(node => node.open=true)")
                        with self.subTest(theme=theme, width=width, page=name, disclosures="expanded"):
                            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    def test_browser_da2_saved_light_preference(self):
        with self.browser.new_context(color_scheme="dark") as context:
            context.route("**/*", self.local_route)
            context.add_init_script("localStorage.setItem('siyab-theme', 'light')")
            page = context.new_page()
            page.goto(self.origin + "/index.html")
            self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(245, 247, 250)")
            self.assertEqual(page.locator(".theme-toggle-label").inner_text(), "Use dark theme")

    def test_browser_records_follow_the_saved_theme_toggle_and_search(self):
        record = next(name for name in self.pages if name.startswith("records/") and name != "records/index.html")
        for name in ("records/index.html", record):
            with self.browser.new_context(color_scheme="dark") as context:
                context.route("**/*", self.local_route)
                context.add_init_script("localStorage.setItem('siyab-theme', 'light')")
                page = context.new_page()
                page.goto(self.origin + "/" + name)
                with self.subTest(page=name):
                    self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(245, 247, 250)")
                    self.assertEqual(page.locator(".theme-toggle-label").inner_text(), "Use dark theme")
                    page.locator("#theme-toggle").click()
                    self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(10, 13, 22)")
                    self.assertEqual(page.evaluate("localStorage.getItem('siyab-theme')"), "dark")
                    page.locator("[data-open-search]").click()
                    self.assertTrue(page.locator("#global-search").evaluate("node => node.open"))

    def test_browser_da2_real_page_type_roles(self):
        cases = (("explore.html", "main h1", "Manrope", "24px", "30px", "600"),
                 ("accounts/wrap.near.html", ".profile-facts dd", "IBM Plex Mono", "24px", "30px", "300"),
                 ("explore.html", ".app-header-navigation a:not([aria-current])", "Public Sans", "13px", "18px", "300"),
                 ("explore.html", ".app-menu>summary", "Public Sans", "13px", "18px", "300"),
                 ("accounts/wrap.near.html", ".profile-heading p.mono", "IBM Plex Mono", "22px", "28px", "400"),
                 ("accounts/wrap.near.html", "main h2", "Manrope", "16px", "22px", "600"),
                 ("incidents/rhea.html", "main h1", "Manrope", "28px", "34px", "600"),
                 ("issuers.html", "th", "IBM Plex Mono", "12px", "18px", "400"))
        with self.browser.new_context(viewport={"width": 1440, "height": 900}) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            for name, selector, family, size, line, weight in cases:
                page.goto(self.origin + "/" + name)
                page.evaluate("document.fonts.ready")
                target = page.locator(selector).first
                self.assertGreater(target.count(), 0, (name, selector))
                actual = target.evaluate("node => {const s=getComputedStyle(node);return [s.fontFamily,s.fontSize,s.lineHeight,s.fontWeight]}")
                with self.subTest(page=name, role=selector):
                    self.assertIn(family, actual[0])
                    self.assertEqual(actual[1:], [size, line, weight])

    def test_browser_da5_type_roles_chrome_and_label_contrast(self):
        style = """node => {const s=getComputedStyle(node);return [s.fontFamily.split(",")[0].replace(/"/g, ""),s.fontSize,s.lineHeight,s.fontWeight,s.letterSpacing,s.textTransform]}"""
        size = lambda wide, phone: (lambda width: wide if width > 760 else phone)
        roles = (("index.html", "#landing-title", "Manrope", lambda width: "%gpx" % round(max(38, min(width * 0.047, 68)), 2), None, "600", None, "none"),
                 ("index.html", "#landing-problem", "Manrope", size("32px", "32px"), size("38px", "38px"), "600", size("-0.8px", "-0.8px"), "none"),
                 ("index.html", ".landing-steps h3", "Manrope", size("18px", "18px"), size("24px", "24px"), "600", size("-0.18px", "-0.18px"), "none"),
                 ("index.html", ".landing-label", "IBM Plex Mono", size("12px", "12px"), size("18px", "18px"), "400", size("0.24px", "0.24px"), "uppercase"),
                 ("index.html", ".landing-counter", "IBM Plex Mono", None, None, "300", None, "none"),
                 ("explore.html", "main h1", "Manrope", size("24px", "22px"), size("30px", "28px"), "600", size("-0.48px", "-0.44px"), "none"),
                 ("explore.html", ".app-header-navigation a:not([aria-current])", "Public Sans", size("13px", "15px"), size("18px", "22px"), "300", size("normal", "normal"), "none"),
                 ("accounts/wrap.near.html", ".profile-heading h1", "IBM Plex Mono", size("12px", "12px"), size("18px", "18px"), "400", size("0.24px", "0.24px"), "uppercase"),
                 ("accounts/wrap.near.html", ".profile-heading p.mono", "IBM Plex Mono", size("22px", "22px"), size("28px", "28px"), "400", size("normal", "normal"), "none"),
                 ("accounts/wrap.near.html", "main h2", "Manrope", size("16px", "16px"), size("22px", "22px"), "600", size("normal", "normal"), "none"),
                 ("accounts/wrap.near.html", ".profile-facts dt", "IBM Plex Mono", size("12px", "12px"), size("18px", "18px"), "400", size("0.24px", "0.24px"), "uppercase"),
                 ("accounts/wrap.near.html", ".profile-facts dd:not(.stat-timestamp)", "IBM Plex Mono", size("24px", "24px"), size("30px", "30px"), "300", size("normal", "normal"), "none"),
                 ("accounts/wrap.near.html", "label:has(>input[type=search])", "IBM Plex Mono", size("12px", "12px"), size("18px", "18px"), "400", size("0.24px", "0.24px"), "uppercase"),
                 ("issuers.html", "th", "IBM Plex Mono", size("12px", "12px"), size("18px", "18px"), "400", size("0.24px", "0.24px"), "uppercase"),
                 ("incidents/rhea.html", "main h1", "Manrope", size("28px", "22px"), size("34px", "28px"), "600", size("-0.56px", "-0.44px"), "none"))
        labels = ".landing-label,.landing dt,.profile-heading h1:not(.profile-id),.profile-facts dt,.stat-label,.investigation-summary dt,label:has(>input:not([type=checkbox],[type=radio]),>select,>textarea),th"
        contrast = """nodes => nodes.filter(node => node.checkVisibility()).map(node => {
            const rgb = value => value.match(/[0-9.]+/g).map(Number);
            const lum = ([r,g,b]) => [r,g,b].map(v => {v/=255;return v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4}).reduce((sum,v,i) => sum + v*[.2126,.7152,.0722][i], 0);
            let back = node, fill = "rgba(0, 0, 0, 0)";
            while (back && (fill = getComputedStyle(back).backgroundColor) && (rgb(fill)[3] === 0)) back = back.parentElement;
            const [a,b] = [lum(rgb(getComputedStyle(node).color)), lum(rgb(fill))].sort((x,y) => y-x);
            return [node.tagName, node.className, (a+.05)/(b+.05)];
        })"""
        members = ('<div class="cloud-workspace" id="members-sample"><ul class="cloud-members"><li><strong>alice</strong>'
                   '<label>Role for <span class="cloud-member-name">Alice.Near</span><select><option>viewer</option></select></label></li></ul></div>')
        for width in (1440, 390):
            with self.browser.new_context(viewport={"width": width, "height": 900}) as context:
                context.route("**/*", self.local_route)
                page = context.new_page()
                page.goto(self.origin + "/workspace.html")
                page.locator("main").evaluate("(node, sample) => node.insertAdjacentHTML('beforeend', sample)", members)
                page.evaluate("document.fonts.ready")
                with self.subTest(width=width, role="authenticated team member role label"):
                    self.assertEqual(page.locator("#members-sample label").evaluate(style), ["IBM Plex Mono", "12px", "18px", "400", "0.24px", "uppercase"])
                    self.assertEqual(page.locator("#members-sample select").evaluate("node => getComputedStyle(node).textTransform"), "none")
                    self.assertEqual(page.locator("#members-sample .cloud-member-name").evaluate("node => [getComputedStyle(node).textTransform, node.innerText]"), ["none", "Alice.Near"])
                    self.assertTrue(page.locator("#members-sample label").inner_text().startswith("ROLE FOR Alice.Near"))
        with self.browser.new_context(viewport={"width": 1440, "height": 900}) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            for name in self.template_pages():
                page.goto(self.origin + "/" + name)
                page.wait_for_load_state("networkidle")
                page.evaluate("document.fonts.ready")
                page.locator("main details").evaluate_all("nodes => nodes.forEach(node => node.open=true)")
                headings = page.evaluate("[...document.querySelectorAll('main :is(h1,h2,h3):not(.profile-id,.graph-identifier,.landing-observations h3,.profile-heading h1:has(+p.mono))')].filter(node => node.checkVisibility()).map(node => [node.tagName, node.textContent.trim().slice(0, 40), getComputedStyle(node).fontFamily.split(',')[0], getComputedStyle(node).fontWeight])")
                label_roles = page.evaluate("[...document.querySelectorAll('main :is(.stat-label,.profile-facts dt,.investigation-summary dt,.analytics-inline span,.portfolio-figure-label,.landing-label,.landing dt,label:has(>input:not([type=checkbox],[type=radio]),>select,>textarea))')].filter(node => node.checkVisibility()).map(node => [node.textContent.trim().slice(0, 40), getComputedStyle(node).fontFamily.split(',')[0].replace(/\"/g, ''), getComputedStyle(node).textTransform])")
                summaries = page.evaluate("[...document.querySelectorAll('main :is(h4,.cloud-workspace>summary,.enrichment-directory>summary,.feed-recorded-details>summary,.profile-evidence>summary,.registry-individuals>summary)')].filter(node => node.checkVisibility()).map(node => [node.textContent.trim().slice(0, 40), getComputedStyle(node).fontFamily.split(',')[0], getComputedStyle(node).fontSize, getComputedStyle(node).fontWeight])")
                if name == "investigate.html":
                    title = page.locator("#investigate-title")
                    with self.subTest(role="lookup title without a subject"):
                        self.assertEqual(title.evaluate(style)[:5], ["Manrope", "24px", "30px", "600", "-0.48px"])
                    page.locator("#investigate-account").fill("usdt.tether-token.near")
                    page.locator("#investigate-form").evaluate("node => node.requestSubmit()")
                    with self.subTest(role="lookup title holding an account"):
                        self.assertEqual(title.inner_text(), "usdt.tether-token.near")
                        self.assertEqual(title.evaluate(style)[:5], ["IBM Plex Mono", "22px", "28px", "400", "normal"])
                with self.subTest(page=name, role="every section summary"):
                    self.assertEqual([item for item in summaries if item[1:] != ["Manrope", "16px", "600"]], [])
                with self.subTest(page=name, role="every title"):
                    self.assertGreater(len(headings), 0)
                    self.assertEqual([item for item in headings if item[2:] != ["Manrope", "600"]], [])
                with self.subTest(page=name, role="every label"):
                    self.assertEqual([item for item in label_roles if item[1:] != ["IBM Plex Mono", "uppercase"]], [])
        for theme in ("dark", "light"):
            with self.browser.new_context() as context:
                context.route("**/*", self.local_route)
                context.add_init_script("localStorage.setItem('siyab-theme', '" + theme + "')")
                page = context.new_page()
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 900})
                    for name in ("index.html", "explore.html", "accounts/wrap.near.html", "issuers.html", "incidents/rhea.html", "feed.html", "verify.html"):
                        page.goto(self.origin + "/" + name)
                        page.wait_for_load_state("networkidle")
                        page.evaluate("document.fonts.ready")
                        for page_name, selector, family, font_size, line, weight, tracking, case in roles:
                            if page_name != name:
                                continue
                            target = page.locator(selector).first
                            self.assertGreater(page.locator(selector).count(), 0, (name, selector))
                            actual = target.evaluate(style)
                            expected = [family, font_size(width) if font_size else actual[1], line(width) if line else actual[2], weight, tracking(width) if tracking else actual[4], case]
                            with self.subTest(theme=theme, width=width, page=name, role=selector):
                                self.assertEqual(actual, expected)
                        heavy = page.locator("body *").evaluate_all("nodes => nodes.filter(node => node.checkVisibility() && Number(getComputedStyle(node).fontWeight) >= 600 && [...node.childNodes].some(child => child.nodeType === 3 && child.textContent.trim())).map(node => [node.tagName, node.className, getComputedStyle(node).fontFamily])")
                        with self.subTest(theme=theme, width=width, page=name, role="no synthetic bold"):
                            self.assertEqual([item for item in heavy if not item[2].startswith("Manrope")], [])
                        loaded = page.evaluate("[...document.fonts].filter(face => face.status === 'loaded').map(face => face.family.replace(/\"/g, '') + ' ' + face.weight)")
                        with self.subTest(theme=theme, width=width, page=name, role="real font files"):
                            self.assertTrue(set(loaded) <= {"Manrope 600", "Public Sans 200", "Public Sans 300", "Public Sans 400", "Public Sans 500", "IBM Plex Mono 300", "IBM Plex Mono 400", "IBM Plex Mono 500"}, loaded)
                        for tag, classes, ratio in page.locator(labels).evaluate_all(contrast):
                            with self.subTest(theme=theme, width=width, page=name, label=(tag, classes)):
                                self.assertGreaterEqual(ratio, 4.5)
                        quoted = page.locator('.profile-heading p.mono q.data,a[href*="accounts/"]>q.data,q.data-identifier').evaluate_all("nodes => nodes.map(node => getComputedStyle(node).quotes)")
                        with self.subTest(theme=theme, width=width, page=name, role="identifier quotes"):
                            self.assertTrue(all(value == "none" for value in quoted), quoted)
                        borders = page.evaluate("""() => {const probe = (html) => {const holder=document.createElement('div');holder.innerHTML=html;document.querySelector('main').append(holder);const node=holder.firstElementChild;const value=getComputedStyle(node).borderTopColor;holder.remove();return value};
                            const token = name => {const node=document.createElement('span');node.style.color='rgb(var('+name+'))';document.body.append(node);const value=getComputedStyle(node).color;node.remove();return value};
                            return [probe('<button type=button>Read</button>'), probe('<input aria-label=Probe>'), probe('<select aria-label=Probe><option>All</option></select>'), token('--hairline'), token('--hairline-strong')]}""")
                        with self.subTest(theme=theme, width=width, page=name, role="chrome"):
                            self.assertEqual(borders[0], borders[3])
                            self.assertEqual(borders[1:3], [borders[4], borders[4]])

    def test_browser_da2_ordinary_rows_are_forty_pixels(self):
        with self.browser.new_context(viewport={"width": 1440, "height": 900}) as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            found = 0
            for name in ("explore.html", "issuers.html", "entities/catalog.html", "accounts/wrap.near.html"):
                page.goto(self.origin + "/" + name)
                page.evaluate("document.fonts.ready")
                rows = page.locator("main tbody tr").evaluate_all("""nodes => nodes.filter(node => {
                    if(getComputedStyle(node).display !== 'table-row' || !node.checkVisibility()) return false;
                    return [...node.cells].every(cell => {
                        const range=document.createRange();range.selectNodeContents(cell);
                        return new Set([...range.getClientRects()].filter(rect=>rect.width&&rect.height).map(rect=>Math.round(rect.top))).size <= 1;
                    });
                }).map(node => node.getBoundingClientRect().height)""")
                found += len(rows)
                for height in rows:
                    with self.subTest(page=name, height=height):
                        self.assertAlmostEqual(height, 40, delta=2)
            self.assertGreater(found, 0, "No ordinary one-line rows were measured")

    def test_browser_da2_both_theme_width_matrix_and_phone_targets(self):
        names = ("index.html", "explore.html", "accounts/wrap.near.html", "issuers.html", "incidents/rhea.html", "records/index.html")
        for theme in ("dark", "light"):
            with self.browser.new_context() as context:
                context.route("**/*", self.local_route)
                context.add_init_script("localStorage.setItem('siyab-theme', '" + theme + "')")
                page = context.new_page()
                for width in (1440, 1024, 768, 390, 320):
                    page.set_viewport_size({"width": width, "height": 900})
                    for name in names:
                        page.goto(self.origin + "/" + name)
                        page.evaluate("document.fonts.ready")
                        with self.subTest(theme=theme, width=width, page=name):
                            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width + 1)
                            if width <= 390:
                                for item in page.locator("button,.button,input:not([type=radio]),select").evaluate_all("nodes => nodes.filter(node => node.checkVisibility() && !node.closest('[hidden]')).map(node => ({tag:node.tagName,id:node.id,classes:node.className,height:node.getBoundingClientRect().height}))"):
                                    self.assertGreaterEqual(item["height"], 44, item)

    def test_browser_da3_header_menus_keyboard_escape_and_reach(self):
        former = ("/index.html", "/explore.html", "/investigate.html", "/flows/rhea-collector.html", "/feed.html", "/workspace.html",
                  "/explore.html#tools", "/verify.html", "/explore.html#incidents", "/entities/treasuries.html",
                  "/entities/catalog.html", "/entities/infrastructure.html", "/entities/organisations.html",
                  "/records/index.html", "/issuers.html")
        for theme in ("dark", "light"):
            with self.browser.new_context() as context:
                context.route("**/*", self.local_route)
                context.add_init_script("localStorage.setItem('siyab-theme', '" + theme + "')")
                page = context.new_page()
                for width in (1440, 1024, 768, 390, 320):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.goto(self.origin + "/accounts/wrap.near.html")
                    page.evaluate("document.fonts.ready")
                    with self.subTest(theme=theme, width=width):
                        self.assertLessEqual(page.locator(".app-header").bounding_box()["height"], 57)
                        summaries = page.locator(".app-menu>summary" if width >= 1240 else "#mobile-navigation>summary")
                        reached = set()
                        for index in range(summaries.count()):
                            summary = summaries.nth(index)
                            summary.focus()
                            self.assertNotEqual(summary.evaluate("node => getComputedStyle(node).outlineStyle"), "none")
                            page.keyboard.press("Enter")
                            menu = summary.locator("xpath=..")
                            self.assertTrue(menu.evaluate("node => node.open"))
                            reached |= {link.evaluate("node => new URL(node.href).pathname + new URL(node.href).hash")
                                        for link in menu.locator("a").all() if link.is_visible()}
                            page.keyboard.press("Escape")
                            self.assertFalse(menu.evaluate("node => node.open"))
                            self.assertTrue(summary.evaluate("node => node === document.activeElement"))
                        if width >= 1240:
                            reached |= {link.evaluate("node => new URL(node.href).pathname + new URL(node.href).hash")
                                        for link in page.locator(".app-header-navigation a").all() if link.is_visible()}
                            self.assertFalse(page.locator("#mobile-navigation").is_visible())
                        else:
                            self.assertEqual(page.locator(".app-header-navigation a,.app-menu").evaluate_all("nodes => nodes.filter(node => node.checkVisibility()).length"), 0)
                        self.assertEqual(reached, set(former))
                        for path in former:
                            target = path.partition("#")[0].lstrip("/")
                            self.assertTrue((self.out / target).is_file(), path)
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width + 1)
                page.set_viewport_size({"width": 1440, "height": 900})
                page.goto(self.origin + "/accounts/wrap.near.html")
                page.locator(".app-menu>summary").first.click()
                page.locator("main h2").first.click()
                self.assertEqual(page.locator(".app-menu[open]").count(), 0)
                for width in (1024, 768, 390, 320):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.goto(self.origin + "/accounts/wrap.near.html")
                    with self.subTest(theme=theme, width=width, menu="outside click"):
                        page.locator("#mobile-navigation>summary").click()
                        self.assertTrue(page.locator("#mobile-navigation").evaluate("node => node.open"))
                        page.locator(".mobile-navigation-panel a").first.wait_for(state="visible")
                        brand = page.locator(".app-header .site-brand").bounding_box()
                        page.locator(".app-header").click(position={"x": brand["x"] + brand["width"] + 8, "y": 28})
                        self.assertFalse(page.locator("#mobile-navigation").evaluate("node => node.open"))
                        page.locator("#mobile-navigation>summary").click()
                        page.locator(".mobile-navigation-panel").click(position={"x": 4, "y": 4})
                        self.assertTrue(page.locator("#mobile-navigation").evaluate("node => node.open"))
                        page.keyboard.press("Escape")
                        self.assertFalse(page.locator("#mobile-navigation").evaluate("node => node.open"))
                        self.assertTrue(page.locator("#mobile-navigation>summary").evaluate("node => node === document.activeElement"))

    def test_issuer_scope_counts_the_recorded_answers(self):
        sentence = ('Banned answers only. Other recorded answers: get_blacklist_status answered "Allowable" 369 times; '
                    'is_blocklisted answered false 380 times. '
                    'Counts by issuer view are in Coverage below.')
        self.assertIn('<p class="issuers-scope">' + render.data_text(sentence) + '</p>', self.pages["issuers.html"])

    def test_issuer_scope_describes_mixed_answers_without_inventing_a_status(self):
        original = render.issuers.reads()[0]
        answers = ["Allowable", False, None, "Pending <review>", "Pending <review>", {"error": "unavailable"}, "Banned"]
        reads = [dict(original, answer=answer) for answer in answers]
        sentence = ('Banned answers only. Other recorded answers: get_blacklist_status answered "Allowable" 1 time, '
                    'false 1 time, null 1 time, "Pending <review>" 2 times and {"error": "unavailable"} 1 time. '
                    'Counts by issuer view are in Coverage below.')
        with mock.patch.object(render.issuers, "reads", return_value=reads):
            page = render.issuers_page()
        self.assertIn('<p class="issuers-scope">' + render.data_text(sentence) + '</p>', page)
        self.assertNotIn('<review>', page)
        self.assertNotIn('came back Allowable or not blocklisted', page)

    def test_browser_issuer_scope_spans_the_tablet_grid_without_overflow(self):
        for theme in ("dark", "light"):
            with self.browser.new_context(reduced_motion="reduce", viewport={"width": 768, "height": 900}) as context:
                context.route("**/*", self.local_route)
                context.add_init_script("localStorage.setItem('siyab-theme', '" + theme + "')")
                page = context.new_page()
                page.goto(self.origin + "/issuers.html")
                page.evaluate("document.fonts.ready")
                scope = page.locator("#issuer-status-section>.issuers-scope")
                with self.subTest(theme=theme):
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 768)
                    self.assertEqual(scope.evaluate("node => getComputedStyle(node).gridColumn"), "1 / -1")
                    self.assertAlmostEqual(scope.bounding_box()["width"], page.locator("#issuer-status-section").bounding_box()["width"], delta=1)

    def test_browser_da3_reference_page_targets(self):
        with self.browser.new_context(reduced_motion="reduce") as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto(self.origin + "/explore.html")
            page.wait_for_selector(".graph-stage")
            page.evaluate("document.fonts.ready")
            self.assertLessEqual(page.locator(".graph-stage").bounding_box()["y"], 360)
            for selector in (".graph-status", ".graph-coverage", ".graph-legend"):
                self.assertTrue(page.locator(selector).is_visible(), selector)
            page.goto(self.origin + "/issuers.html")
            page.evaluate("document.fonts.ready")
            scope = page.locator("#issuer-status-section>.issuers-scope")
            self.assertEqual(scope.inner_text(), "Banned answers only. Other recorded answers: get_blacklist_status answered Allowable 369 times; is_blocklisted answered false 380 times. Counts by issuer view are in Coverage below.")
            self.assertTrue(scope.is_visible())
            self.assertLessEqual(page.locator("#issuer-status tbody tr").first.bounding_box()["y"], 340)
            for anchor in ("#issuer-status-section", "#issuer-ledger"):
                self.assertEqual(page.locator(anchor).count(), 1)
                self.assertEqual(page.locator('.profile-anchors a[href="' + anchor + '"]').count(), 1)
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(self.origin + "/issuers.html")
            page.evaluate("document.fonts.ready")
            self.assertTrue(scope.is_visible())
            self.assertLessEqual(page.locator("#issuer-status tbody tr").first.bounding_box()["y"], 532)

    def test_browser_da4_page_family_compositions(self):
        record = next(name for name in self.pages if name.startswith("records/") and name != "records/index.html")
        pages = (record, "records/index.html", "entities/treasuries.html", "entities/organisations.html",
                 "entities/rainbow-bridge-token-factory.html", "flows/rhea-collector.html", "clusters/rhea.html",
                 "investigate.html", "workspace.html", "feed.html", "verify.html", "top-receivers.html", "404.html")
        for theme in ("dark", "light"):
            with self.browser.new_context(reduced_motion="reduce") as context:
                context.route("**/*", self.local_route)
                context.add_init_script("localStorage.setItem('siyab-theme', '" + theme + "')")
                page = context.new_page()
                for width in (1440, 1024, 768, 390, 320):
                    page.set_viewport_size({"width": width, "height": 900})
                    for name in pages:
                        page.goto(self.origin + "/" + name)
                        page.evaluate("document.querySelectorAll('details').forEach(node => { node.open = true; })")
                        with self.subTest(theme=theme, width=width, page=name):
                            self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"),
                                             "rgb(245, 247, 250)" if theme == "light" else "rgb(10, 13, 22)")
                            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width + 1)
        with self.browser.new_context(reduced_motion="reduce") as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto(self.origin + "/" + record)
            anchors = page.locator(".record-heading .profile-anchors a")
            self.assertEqual(anchors.count(), len(render._RECORD_SECTIONS))
            for href in anchors.evaluate_all("nodes => nodes.map(node => node.getAttribute('href'))"):
                self.assertEqual(page.locator("section" + href).count(), 1, href)
            fields = page.locator(".record-content>section>dl").first
            self.assertEqual(len(fields.evaluate("node => getComputedStyle(node).gridTemplateColumns").split()), 2)
            self.assertEqual(page.locator("[data-record-path]").count(), self.pages[record].count("data-record-path="))
            page.goto(self.origin + "/entities/treasuries.html")
            self.assertEqual(page.locator(".collection-heading h1").count(), 1)
            self.assertLessEqual(page.locator(".entity-table tbody tr").first.bounding_box()["y"], 400)
            page.goto(self.origin + "/entities/rainbow-bridge-token-factory.html")
            self.assertAlmostEqual(page.locator("#registry-members").bounding_box()["width"], 1392, delta=1)
            self.assertEqual(page.locator(".registry-strip .profile-provenance p").count(), 2)
            page.goto(self.origin + "/flows/rhea-collector.html")
            page.wait_for_selector(".graph-stage")
            self.assertAlmostEqual(page.locator(".graph-status").bounding_box()["y"], page.locator(".graph-coverage").bounding_box()["y"], delta=1)
            self.assertLessEqual(page.locator(".graph-stage").bounding_box()["y"], 460)
            page.goto(self.origin + "/clusters/rhea.html")
            for region in ("#chain-relationships", "#attribution-relationships"):
                self.assertEqual(page.locator(region + ">h2").count(), 1)
            self.assertEqual(page.locator("#attribution-relationships").evaluate("node => getComputedStyle(node).borderTopStyle"), "dashed")
            page.goto(self.origin + "/investigate.html")
            self.assertAlmostEqual(page.locator("#investigate-profile").bounding_box()["y"], page.locator("#investigate-holdings").bounding_box()["y"], delta=1)
            self.assertAlmostEqual(page.locator("#portfolio-current-block").bounding_box()["y"], page.locator("#portfolio-history-block").bounding_box()["y"], delta=1)
            self.assertGreater(page.locator("#portfolio-block-block").bounding_box()["y"], page.locator("#portfolio-history-block").bounding_box()["y"])
            page.goto(self.origin + "/top-receivers.html")
            panels = page.locator(".receiver-breakdown>.receiver-panel")
            self.assertEqual(panels.count(), 2)
            self.assertAlmostEqual(panels.nth(0).bounding_box()["y"], panels.nth(1).bounding_box()["y"], delta=1)
            page.set_viewport_size({"width": 1024, "height": 900})
            page.goto(self.origin + "/investigate.html")
            self.assertGreater(page.locator("#investigate-holdings").bounding_box()["y"], page.locator("#investigate-profile").bounding_box()["y"])
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(self.origin + "/" + record)
            self.assertEqual(len(page.locator(".record-content>section>dl").first.evaluate("node => getComputedStyle(node).gridTemplateColumns").split()), 1)
            page.goto(self.origin + "/top-receivers.html")
            self.assertGreater(panels.nth(1).bounding_box()["y"], panels.nth(0).bounding_box()["y"])

    def test_browser_new_visit_is_dark_under_light_system(self):
        with self.browser.new_context(color_scheme="light") as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            page.goto(self.origin + "/index.html")
            self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(10, 13, 22)")
            self.assertEqual(page.locator(".theme-toggle-label").inner_text(), "Use light theme")

    def test_browser_first_dark_toggle_saves_light(self):
        with self.browser.new_context(color_scheme="light") as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            page.goto(self.origin + "/index.html")
            page.locator(".theme-toggle").click()
            self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(245, 247, 250)")
            self.assertEqual(page.evaluate("localStorage.getItem('siyab-theme')"), "light")
            self.assertEqual(page.locator('meta[name="theme-color"]').evaluate_all("nodes => nodes.map(node => node.content)"), ["#F5F7FA"])

    def test_browser_saved_dark_overrides_light_system(self):
        with self.browser.new_context(color_scheme="light") as context:
            context.route("**/*", self.local_route)
            context.add_init_script("localStorage.setItem('siyab-theme', 'dark')")
            page = context.new_page()
            page.goto(self.origin + "/index.html")
            self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(10, 13, 22)")

    def test_browser_records_default_dark_without_a_saved_choice(self):
        record = next(name for name in self.pages if name.startswith("records/") and name != "records/index.html")
        with self.browser.new_context(color_scheme="light") as context:
            context.route("**/*", self.local_route)
            page = context.new_page()
            page.goto(self.origin + "/" + record)
            self.assertEqual(page.locator("body").evaluate("node => getComputedStyle(node).backgroundColor"), "rgb(10, 13, 22)")
            self.assertEqual(page.locator(".theme-toggle-label").inner_text(), "Use light theme")


    def test_browser_da6_watermark_surfaces_themes_print_and_layout(self):
        surfaces = ".graph-stage,.analytics-chart:not(:has(.analytics-distribution-head)),.analytics-timeline-chart"
        unmarked = ("table,.table,.app-topbar,.empty-state,.feed-empty,.investigate-empty,.portfolio-empty,"
                    ".analytics-chart:has(.analytics-distribution-head),.entity-table-panel,.analytics-panel")
        left_plain = ".empty-state,.feed-empty,.investigate-empty,.portfolio-empty,.analytics-chart:has(.analytics-distribution-head)"
        visible_classes = "nodes => nodes.filter(node => node.checkVisibility()).map(node => node.className.split(' ')[0])"
        glyph = "M26 6H12L6 14l19 5-6 7H5"
        marked = "nodes => nodes.filter(node => getComputedStyle(node).backgroundImage.includes('" + glyph + "')).map(node => node.tagName + ' ' + (node.className.baseVal ?? node.className))"
        surface_state = """nodes => nodes.filter(node => node.checkVisibility()).map(node => {const s=getComputedStyle(node);
            return [node.className, decodeURIComponent(s.backgroundImage), s.backgroundRepeat, s.backgroundPosition, s.backgroundSize, node.querySelector("table") === null]})"""
        rects = "nodes => nodes.map(node => {const r=node.getBoundingClientRect();return [r.x, r.y, r.width, r.height]})"
        mismatched = """([surfaces, glyph]) => {
            const allowed = new Set(document.querySelectorAll(surfaces));
            const name = node => node.tagName + " " + (node.className.baseVal ?? node.className);
            const results = [];
            for (const node of document.querySelectorAll("*")) {
                if (getComputedStyle(node).backgroundImage.includes(glyph) !== allowed.has(node)) results.push(name(node));
                for (const part of ["::before", "::after"]) if (getComputedStyle(node, part).backgroundImage.includes(glyph)) results.push(name(node) + part);
            }
            return results;
        }"""
        contrast = """([alpha, mark, surfaces]) => {
            const rgb = value => value.match(/[0-9.]+/g).map(Number);
            const lum = ([r,g,b]) => [r,g,b].map(v => {v/=255;return v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4}).reduce((sum,v,i) => sum + v*[.2126,.7152,.0722][i], 0);
            const blend = fill => rgb(fill).slice(0, 3).map((v, i) => v * (1 - alpha) + mark[i] * alpha);
            const against = (ink, fill) => {const [a,b] = [lum(ink), lum(fill)].sort((x,y) => y-x);return (a+.05)/(b+.05)};
            const ratio = (ink, fill) => against(rgb(ink), blend(fill));
            const token = name => {const node=document.createElement("span");node.style.color="rgb(var("+name+"))";document.body.append(node);const value=getComputedStyle(node).color;node.remove();return value};
            const results = [];
            for (const surface of document.querySelectorAll(surfaces)) {
                if (!surface.checkVisibility()) continue;
                for (const node of [surface, ...surface.querySelectorAll("*")]) {
                    if (node.closest("svg") || !node.checkVisibility() || ![...node.childNodes].some(child => child.nodeType === 3 && child.textContent.trim())) continue;
                    let back = node, fill = "rgba(0, 0, 0, 0)";
                    while (back && (fill = getComputedStyle(back).backgroundColor) && rgb(fill)[3] === 0) back = back.parentElement;
                    results.push([node.className, ratio(getComputedStyle(node).color, fill)]);
                }
            }
            for (const stage of document.querySelectorAll(".graph-stage")) {
                if (!stage.checkVisibility()) continue;
                let back = stage, ground = "rgba(0, 0, 0, 0)";
                while (back && (ground = getComputedStyle(back).backgroundColor) && rgb(ground)[3] === 0) back = back.parentElement;
                const base = blend(ground);
                for (const group of stage.querySelectorAll(".graph-node")) {
                    const paint = node => {const s = getComputedStyle(node), c = rgb(s.fill);return [c.slice(0, 3), (c[3] ?? 1) * parseFloat(s.fillOpacity) * parseFloat(s.opacity)]};
                    const over = ([colour, cover], under) => colour.map((v, i) => v * cover + under[i] * (1 - cover));
                    let fade = 1;
                    for (let node = group; node && node !== stage; node = node.parentElement) fade *= parseFloat(getComputedStyle(node).opacity);
                    const fill = over(paint(group.querySelector(".graph-node-body")), base);
                    const state = ["is-root", "is-selected"].filter(name => group.classList.contains(name)).join(" ") || "plain";
                    for (const text of group.querySelectorAll("text")) results.push(["graph " + state + " " + text.getAttribute("class"),
                        against(over([over(paint(text), fill), fade], base), over([fill, fade], base)), against(paint(text)[0], fill)]);
                }
            }
            for (const ink of ["--secondary", "--muted"]) for (const fill of ["--panel", "--ground", "--ground-alt", "--hover"]) results.push([ink + " on " + fill, ratio(token(ink), token(fill))]);
            return results;
        }"""
        themes = {"dark": ("#fff", ".06", [255, 255, 255], 0.06), "light": ("#18212F", ".04", [24, 33, 47], 0.04)}
        rule = next(body for selector, body, context in self.rules if "background-image:var(--mark)" in body and not context)
        sizing = re.search(r"background-size:([^;}]+)", rule).group(1).replace(",", ", ")
        self.assertTrue(sizing.startswith("min(30%, 320px) min("), sizing)
        for theme, (stroke, opacity, mark, alpha) in themes.items():
            with self.browser.new_context(reduced_motion="reduce") as context:
                context.route("**/*", self.local_route)
                context.add_init_script("localStorage.setItem('siyab-theme', '" + theme + "')")
                page = context.new_page()
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 900})
                    seen, plain, graph_states = set(), set(), set()
                    for name in ("explore.html", "flows/rhea-collector.html", "clusters/rhea.html", "feed.html", "workspace.html",
                                 "investigate.html", "issuers.html", "accounts/wrap.near.html"):
                        page.goto(self.origin + "/" + name)
                        page.wait_for_load_state("networkidle")
                        page.evaluate("document.fonts.ready")
                        for classes, image, repeat, position, size, table_free in page.locator(surfaces).evaluate_all(surface_state):
                            seen.add(classes.split()[0])
                            with self.subTest(theme=theme, width=width, page=name, surface=classes):
                                self.assertIn(glyph, image)
                                self.assertIn("stroke='" + stroke + "' stroke-opacity='" + opacity + "'", image)
                                self.assertIn("stroke-width='5' stroke-linecap='square' stroke-linejoin='bevel'", image)
                                self.assertEqual((repeat, position, size), ("no-repeat", "50% 50%", sizing))
                                self.assertTrue(table_free)
                        with self.subTest(theme=theme, width=width, page=name, role="marked elements equal the allowed surfaces exactly"):
                            self.assertEqual(page.evaluate(mismatched, [surfaces, glyph]), [])
                        with self.subTest(theme=theme, width=width, page=name, role="no mark on tables, header or placeholders"):
                            self.assertEqual(page.locator(unmarked).evaluate_all(marked), [])
                        plain.update(page.locator(left_plain).evaluate_all(visible_classes))
                        measured = page.evaluate(contrast, [alpha, mark, surfaces])
                        if page.locator(".graph-stage .graph-node:not(.is-root)").count() and page.locator(".graph-stage").first.is_visible():
                            page.locator(".graph-stage .graph-node:not(.is-root)").first.dispatch_event("click")
                            page.wait_for_selector(".graph-stage .graph-node.is-selected:not(.is-root)")
                            measured += page.evaluate(contrast, [alpha, mark, surfaces])
                        graph_states.update(item[0].rsplit(" ", 1)[0] for item in measured if item[0].startswith("graph "))
                        with self.subTest(theme=theme, width=width, page=name, role="text contrast over the mark"):
                            self.assertEqual([item for item in measured if item[1] < 4.5], [])
                        before = page.locator(surfaces).evaluate_all(rects)
                        page.add_style_tag(content="*{background-image:none!important}")
                        with self.subTest(theme=theme, width=width, page=name, role="no layout shift or overflow"):
                            self.assertEqual(page.locator(surfaces).evaluate_all(rects), before)
                            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    if width == 1440:
                        self.assertEqual(seen, {"graph-stage", "analytics-chart", "analytics-timeline-chart"})
                        self.assertTrue({"graph plain", "graph is-root", "graph is-selected"} <= graph_states, graph_states)
                    self.assertEqual(plain, {"feed-empty", "empty-state", "investigate-empty", "portfolio-empty", "analytics-chart"})
                page.set_viewport_size({"width": 1440, "height": 900})
                page.goto(self.origin + "/explore.html")
                page.wait_for_selector(".graph-stage")
                for faded in (".graph-node-label{fill:rgb(var(--ink)/.1)}", ".graph-node-label{fill-opacity:.1}", ".graph-node-label{opacity:.1}", ".graph-node{opacity:.1}"):
                    style = page.add_style_tag(content="*{transition-property:none!important}" + faded)
                    labels = [item for item in page.evaluate(contrast, [alpha, mark, surfaces]) if item[0].endswith(" graph-node-label")]
                    style.evaluate("node => node.remove()")
                    with self.subTest(theme=theme, role="faded graph label fails composited contrast", style=faded):
                        self.assertTrue(labels)
                        self.assertTrue(all(item[1] < 4.5 <= item[2] for item in labels), labels)
                page.emulate_media(media="print")
                with self.subTest(theme=theme, role="print"):
                    self.assertEqual(page.locator(surfaces).evaluate_all("nodes => nodes.map(node => getComputedStyle(node).backgroundImage).filter(value => value !== 'none')"), [])
        record = next(name for name in self.pages if name.startswith("records/") and name != "records/index.html")
        for javascript in (True, False):
            with self.browser.new_context(java_script_enabled=javascript, viewport={"width": 1440, "height": 900}) as context:
                context.route("**/*", self.local_route)
                page = context.new_page()
                for name in ("index.html", "records/index.html", record):
                    page.goto(self.origin + "/" + name)
                    page.wait_for_load_state("networkidle")
                    with self.subTest(javascript=javascript, page=name, role="landing and records stay unmarked"):
                        self.assertEqual(page.locator("body,body *").evaluate_all(marked), [])
                    if name.startswith("records/") and javascript:
                        page.locator("#theme-toggle").click()
                        with self.subTest(javascript=javascript, page=name, role="light record stays unmarked"):
                            self.assertEqual(page.locator("body,body *").evaluate_all(marked), [])


if __name__ == "__main__":
    unittest.main()
