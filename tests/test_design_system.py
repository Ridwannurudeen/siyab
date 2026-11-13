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

    def test_r2_header_search_preserves_control_padding(self):
        for context, padding in (((), "7px 10px"), (("@media(max-width:760px)",), "8px 10px")):
            body = next(body for selector, body, media in self.rules
                        if selector == ".site-search-link,.site-search-button" and media == context)
            with self.subTest(context=context):
                self.assertIn("padding:" + padding + ";", body)

    def test_r2_spacing_and_type_tokens(self):
        root = next(body for selector, body, context in self.rules if selector == ":root" and not context)
        values = tokens(root)
        for name, value in {
            "--s1": "4px", "--s2": "8px", "--s3": "12px", "--s4": "16px",
            "--s5": "24px", "--s6": "32px", "--s7": "48px",
            "--fs-micro": "11px/16px", "--fs-data": "12px/18px",
            "--fs-body": "14px/22px", "--fs-control": "14px/20px",
            "--fs-panel": "16px/24px", "--fs-title": "20px/28px", "--fs-page": "28px/36px",
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
            allowed = {11, 12, 14, 16, 20, 28} | ({15} if phone or jobs_phone else set())
            if jobs_phone:
                self.assertIn("font:400 15px/24px var(--font-ui)", body)
            brand = all(part.strip() in (".site-brand", ".app-mobile-brand") for part in selector.split(","))
            for size in sizes:
                if float(size) not in allowed and not brand:
                    failures.append((selector, "size", size, context))
            for height in re.findall(r"(?:^|;)line-height:([^;]+)", body):
                if not re.fullmatch(r"[0-9.]+px", height):
                    failures.append((selector, "line", height, context))
            for height in re.findall(r"(?:^|;)font:[^;]*?/[ ]*([0-9.]+)(?![0-9.])([a-z%]*)", body):
                if height[1] != "px":
                    failures.append((selector, "font line", height, context))
            for tracking in re.findall(r"(?:^|;)letter-spacing:([^;]+)", body):
                if tracking not in ("0", "0px", "-.02em", "-0.02em", "-.015em", "-0.015em", "-.035em", "-0.035em"):
                    failures.append((selector, "tracking", tracking, context))
        self.assertEqual(failures, [])

    def test_r2_spacing_uses_scale_with_scoped_exceptions(self):
        failures = []
        for selector, body, context in self.rules:
            for property_name, value in re.findall(r"(?:^|;)((?:margin|padding)(?:-[a-z-]+)?|(?:column-|row-)?gap):([^;]+)", body):
                for number in re.findall(r"(?<![a-z0-9.])(-?[0-9.]+)px", value):
                    size = abs(float(number))
                    if size in (0, 4, 8, 12, 16, 24, 32, 48):
                        continue
                    parts = selector.split(",")
                    control_padding = property_name.startswith("padding") and all(re.search(r"(?:button|input|select|textarea|\.button|search-key|site-search-link|kbd|\btd\b|\bth\b)", part) for part in parts)
                    gutter = property_name.startswith("padding") and size in (14, 20, 32) and all(part.strip() in (".app-content", ".app-topbar", ".app-footer") for part in parts)
                    panel_head = property_name == "padding" and size == 14 and all(part.strip() in (".workspace-panel-head", ".analytics-panel-head", ".graph-toolbar", ".graph-list", ".graph-overview-panel") for part in parts)
                    bar_offset = size in (1, 2) and ("bar" in selector or "analytics-track" in selector)
                    rail_offset = property_name == "margin-left" and size == 248 and all(part.strip() in (".app-content", ".app-topbar", ".app-footer") for part in parts)
                    layout_offset = selector == ".app-content" and property_name == "margin-left" and value.startswith("calc(248px + ")
                    mobile_menu = selector == ".mobile-navigation-panel" and property_name == "padding" and value == "12px 20px 24px"
                    if not (control_padding or gutter or panel_head or bar_offset or rail_offset or layout_offset or mobile_menu):
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
                self.assertEqual(declarations[selector].get("gap"), "16px")
        for selector in (".hero-grid", ".workspace-grid", ".profile-grid", ".profile-account-context", ".verification-workspace"):
            with self.subTest(split=selector):
                self.assertEqual(declarations[selector].get("gap"), "24px")
        for selector, padding in {".analytics-note": "0 16px 16px", ".analytics-bin-values": "16px", ".graph-inspector": "16px", ".feed-health": "16px", ".graph-empty": "32px 16px"}.items():
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
        self.assertEqual(declarations[".stat-value"].get("font"), "500 20px/28px var(--font-code)")
        self.assertEqual(declarations[".stat-value"].get("letter-spacing"), "0")
        self.assertEqual(declarations[".stat-label"].get("font"), "400 12px/18px var(--font-ui)")
        self.assertTrue(any("min-width:2200px" in "".join(context).replace(" ", "") and "max-width:1920px" in body for selector, body, context in self.rules if "main" in selector or ".app-content" in selector))
        for selector in (".proof-grid", ".explore-grid"):
            self.assertTrue(any(selector in other.split(",") and "min-width:1700px" in "".join(context).replace(" ", "") and "grid-template-columns:repeat(3,minmax(0,1fr))" in body for other, body, context in self.rules))

    def template_pages(self):
        pages = [name + ".html" for name in ("index", "investigate", "feed", "workspace", "verify", "issuers", "top-receivers", "404", "accounts/wrap.near", "accounts/usdt.tether-token.near", "entities/catalog", "entities/organisations", "flows/rhea-collector", "clusters/rhea", "incidents/rhea", "records/index")]
        pages.append(next(name for name in self.pages if re.fullmatch(r"accounts/[a-f0-9]{64}\.html", name)))
        for kind in ("validator", "dao"):
            pages.append(next(name for name, text in self.pages.items() if name.startswith("entities/") and 'class="profile-kind"' in text and re.search(r'class="profile-kind">' + kind, text, re.IGNORECASE)))
        pages.append(next(name for name in self.pages if name.startswith("records/") and name != "records/index.html"))
        self.assertEqual(len(set(pages)), 20)
        return pages

    def test_browser_r2_type_histogram_and_page_head_alignment(self):
        for width in (1440, 390):
            with self.browser.new_context(viewport={"width": width, "height": 900}, java_script_enabled=True) as context:
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
                page = context.new_page()
                tops = []
                for name in self.template_pages():
                    page.goto(self.origin + "/" + name)
                    page.wait_for_load_state("networkidle")
                    page.evaluate("document.fonts.ready")
                    heading = page.locator("main h1").first
                    shell = page.locator("main .page-head").first
                    tops.append(shell.bounding_box()["y"])
                    self.assertGreaterEqual(heading.bounding_box()["y"], shell.bounding_box()["y"])
                    self.assertLessEqual(heading.bounding_box()["y"] + heading.bounding_box()["height"], shell.bounding_box()["y"] + shell.bounding_box()["height"])
                    sizes = page.locator("body *").evaluate_all("nodes => [...new Set(nodes.filter(node => node.getClientRects().length && getComputedStyle(node).display !== 'none' && !node.matches('.site-brand,.app-mobile-brand,.site-brand *,.app-mobile-brand *')).map(node => getComputedStyle(node).fontSize))]")
                    with self.subTest(width=width, page=name):
                        self.assertTrue(set(sizes) <= ({"11px", "12px", "14px", "16px", "20px", "28px"} | ({"15px"} if width <= 760 else set())), sizes)
                        for spacing in page.locator(".graph-node-label,.graph-node-detail").evaluate_all("nodes => nodes.map(node => getComputedStyle(node).letterSpacing)"):
                            self.assertIn(spacing, ("normal", "0px"))
                self.assertLessEqual(max(tops) - min(tops), 1, (width, tops))

    def test_browser_r2_text_roles(self):
        sample = ('<section id="role-samples"><span id="code-reference" class="mono">1</span>'
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
                    checks = [(".profile-id", "20px", "28px", code_family, "600"),
                              ("#role-samples .stat-label", "12px", "18px", ui_family, "400"),
                              ("#role-samples .stat-value", "20px", "28px", code_family, "500"),
                              ("#role-samples .stat-timestamp", "12px", "18px", code_family, "400"),
                              ("#role-samples .graph-account-button", "12px", "18px", code_family, "400"),
                              ("#role-samples .panel h3", "16px", "24px", ui_family, "600"),
                              ("#role-samples .investigation-summary dt", "12px", "18px", ui_family, "400"),
                              ("#role-samples .registry-count .stat-connective", "14px", "22px", ui_family, "400"),
                              ("#role-samples .graph-node-label", "14px", "22px", code_family, "400"),
                              ("#role-samples .graph-node-detail", "12px", "18px", ui_family, "400")]
                    with self.subTest(width=width, page=name, role="connective colour"):
                        self.assertEqual(page.locator("#role-samples .registry-count .stat-connective").evaluate("node => getComputedStyle(node).color"), page.locator("#role-samples .stat-label").evaluate("node => getComputedStyle(node).color"))
                    with self.subTest(width=width, page=name, role="heading alignment"):
                        self.assertEqual(page.locator("#role-samples .delivery-heading").evaluate("node => getComputedStyle(node).alignItems"), "center")
                    with self.subTest(width=width, page=name, role="graph heading weight"):
                        self.assertEqual(page.locator("#role-samples .graph-loading h2").evaluate("node => getComputedStyle(node).fontWeight"), "600")
                    if width <= 760:
                        checks += [(".analytics-distribution-row .cell-label", "12px", "18px", ui_family, "400"),
                                   ("#role-samples td.num,#role-samples td.mono", "14px", "22px", code_family, "400"),
                                   ("#role-samples .graph-range input,#role-samples .cloud-portability input", "16px", "24px", ui_family, "400"),
                                   ("#role-samples .chart-range-controls select", "16px", "normal", ui_family, "400"),
                                   ("#role-samples .graph-share-result input", "16px", "24px", code_family, "400"),
                                   ("#role-samples .flow-transfer p.mono", "12px", "18px", code_family, "400"),
                                   ("#role-samples .flow-transfer p:not(.mono)", "14px", "22px", ui_family, "400"),
                                   ("#role-samples .analytics-counterparty small,#role-samples .search-result-type,.analytics-full-account", "12px", "18px", ui_family, "400")]
                        micros = page.locator("body *").evaluate_all("nodes => nodes.filter(node => node.getClientRects().length && !node.closest('.search-key') && getComputedStyle(node).fontSize === '11px').map(node => ({tag:node.tagName,classes:node.className}))")
                        with self.subTest(width=width, page=name, role="phone micro"):
                            self.assertEqual(micros, [])
                    else:
                        checks.append(("#role-button", "14px", "20px", ui_family, "400"))
                        checks.append(('.app-sidebar nav a:not([aria-current="page"])', "14px", "20px", ui_family, "400"))
                        with self.subTest(width=width, page=name, role="filter cap"):
                            self.assertLessEqual(page.locator("#role-samples .graph-filter select").bounding_box()["width"], 240)
                    for selector, size, line, family, weight in checks:
                        for actual in page.locator(selector).evaluate_all("nodes => nodes.map(node => {const style=getComputedStyle(node);return [style.fontSize,style.lineHeight,style.fontFamily,style.fontWeight,style.letterSpacing]})"):
                            with self.subTest(width=width, page=name, role=selector):
                                self.assertEqual(actual[:4], [size, line, family, weight])
                                self.assertIn(actual[4], ("normal", "0px"))

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
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
            page = context.new_page()
            for width in (1440, 1024, 768, 390, 360, 320):
                page.set_viewport_size({"width": width, "height": 900})
                for name, selector in (("incidents/rhea.html", "blockquote.data"), ("flows/rhea-collector.html", ".analytics-counterparty"), ("index.html", "q.data,.analytics-counterparty")):
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

    def test_browser_r2_review_counterparty_identifiers_one_line(self):
        with self.browser.new_context(java_script_enabled=True) as context:
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
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
                 ("index.html", ".graph-status,.chart-readout,.chart-selection,.graph-range-message,.graph-meta dd:not(.graph-identifier)"),
                 (record, '[data-record-path$="_at"],[data-record-path$=".at"],[data-record-path="version"]'),
                 ("incidents/rhea.html", ".profile-route-bridge>span"))
        with self.browser.new_context(java_script_enabled=True) as context:
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
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
                            self.assertEqual(actual, "tabular-nums")
                            self.assertGreater(widths[0], 0)
                            self.assertAlmostEqual(widths[0], widths[1], delta=0.01)

    def test_browser_r2_review_tabular_figures(self):
        for width in (1440, 390):
            with self.browser.new_context(viewport={"width": width, "height": 900}) as context:
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
                page = context.new_page()
                page.goto(self.origin + "/entities/catalog.html")
                page.evaluate("document.fonts.ready")
                page.locator(".entity-table tbody tr").first.evaluate("node => node.insertAdjacentHTML('beforeend', '<td class=num><span>1111</span><span>8888</span></td>')")
                for selector in ("td.num", ".entity-table-status", ".entity-table-pagination"):
                    with self.subTest(width=width, selector=selector):
                        self.assertEqual(page.locator(selector).first.evaluate("node => getComputedStyle(node).fontVariantNumeric"), "tabular-nums")
                figures = page.locator("td.num span").evaluate_all("nodes => nodes.map(node => node.getBoundingClientRect().width)")
                self.assertEqual(len(figures), 2)
                self.assertAlmostEqual(figures[0], figures[1], delta=0.01)
                page.locator("main").evaluate("node => node.insertAdjacentHTML('beforeend', '<span class=profile-value>12<q class=\"data profile-asset\">token-issuer.near</q></span><time>2026-11-13</time><div class=stat-value>12<span class=stat-connective>of</span></div>')")
                for selector in (".profile-value", "time", ".stat-value"):
                    with self.subTest(width=width, selector=selector):
                        self.assertEqual(page.locator(selector).first.evaluate("node => getComputedStyle(node).fontVariantNumeric"), "tabular-nums")
                for selector in ("body", ".entity-table td.mono", ".entity-table td a", ".profile-value q.data", ".stat-connective"):
                    with self.subTest(width=width, selector=selector):
                        self.assertEqual(page.locator(selector).first.evaluate("node => getComputedStyle(node).fontVariantNumeric"), "normal")
                for name, value, identifier in (("issuers.html", "#issuer-status tbody td:nth-child(4)", "#issuer-status tbody td:first-child q.data"),
                                                 ("accounts/wrap.near.html", "#profile-transfers tbody td:nth-child(5)", "#profile-transfers tbody td:nth-child(3) q.data"),
                                                 ("accounts/usdt.tether-token.near.html", "#profile-transfers tbody td:nth-child(5)", "#profile-transfers tbody td:nth-child(3) q.data")):
                    page.goto(self.origin + "/" + name)
                    page.wait_for_load_state("networkidle")
                    for selector, expected in ((value, "tabular-nums"), (identifier, "normal")):
                        nodes = page.locator(selector)
                        self.assertGreater(nodes.count(), 0)
                        for actual in nodes.evaluate_all("nodes => nodes.map(node=>getComputedStyle(node).fontVariantNumeric)"):
                            with self.subTest(width=width, page=name, selector=selector):
                                self.assertEqual(actual, expected)

    def test_browser_r2_review_identifier_hyphens(self):
        record = next(name for name in self.pages if name.startswith("records/") and name != "records/index.html")
        with self.browser.new_context(viewport={"width": 1440, "height": 900}, java_script_enabled=True) as context:
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
            page = context.new_page()
            cases = [(record, '[data-record-path="publisher"]', "-owner.testnet"), ("issuers.html", "td q.data", "usdt.tether-token.near")]
            cases += [(name + ".html", ".graph-meta dd.graph-identifier", "usdt.tether-token.near") for name in ("index", "flows/rhea-collector")]
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
                self.assertEqual(target.evaluate("node => getComputedStyle(node).fontVariantNumeric"), "normal")
                if "graph-identifier" in selector or name == "workspace.html":
                    target.evaluate("node => node.style.fontFamily=getComputedStyle(document.body).fontFamily")
                self.assertEqual(target.evaluate("node => getComputedStyle(node).fontFamily"), page.locator("body").evaluate("node => getComputedStyle(node).fontFamily"))
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
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
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
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
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
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
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
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
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
                        self.assertEqual(page.locator("script").count(), 0)
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    def test_browser_r2_review_component_widths(self):
        with self.browser.new_context(viewport={"width": 1440, "height": 900}) as context:
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
            page = context.new_page()
            for name, selector, width in (("entities/organisations.html", ".registry-group", 1152),
                                          ("incidents/rhea.html", ".profile-route", 1152),
                                          ("incidents/rhea.html", ".profile-timeline>li", 900)):
                page.goto(self.origin + "/" + name)
                with self.subTest(selector=selector):
                    self.assertAlmostEqual(page.locator(selector).first.bounding_box()["width"], width, delta=1)
            page.goto(self.origin + "/workspace.html")
            side = page.locator("#browser-workspace .workbench-grid--stacked>.workspace-side")
            self.assertEqual(side.count(), 1)
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
                context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
                page = context.new_page()
                page.goto(self.origin + "/index.html")
                page.locator("main").evaluate("(node, sample) => node.insertAdjacentHTML('beforeend', sample)", sample)
                page.evaluate("document.fonts.ready")
                code = page.locator("#review-code").evaluate("node => getComputedStyle(node).fontFamily")
                ui = page.locator("body").evaluate("node => getComputedStyle(node).fontFamily")
                for table in tables:
                    selector = "#review-roles table." + table.replace(" ", ".") + " td"
                    for target in (selector, selector + " q.data"):
                        with self.subTest(width=width, selector=target):
                            self.assertEqual(page.locator(target).first.evaluate("node => [getComputedStyle(node).fontFamily,getComputedStyle(node).fontWeight]"), [code, "400"])
                for selector, family, weight in ((".app-context", ui, "600"),
                                                  ("#review-roles .account-assertions th,#review-header th", ui, "600"),
                                                  ("#review-roles .verify-form input", code, "400"),
                                                  ("#review-roles .profile-outcome", ui, "600"),
                                                  ("#review-roles .saved-investigation time", ui, "400"),
                                                  ("#review-roles .feed-table time,#review-roles .feed-table q,#review-roles .feed-amount,#review-roles .record-index-table td:last-child", code, "400"),
                                                  ("#review-roles .verify-example-table th", ui, "600"),
                                                  ("#review-roles .verify-example-table td,#review-roles .jobs-table td[data-label=Amount]", code, "400"),
                                                  ("#review-roles .jobs-table th", ui, "400"),
                                                  ("#review-roles .profile-value,#review-roles .profile-timeline time,#review-roles .profile-route>div", code, "400")):
                    self.assertGreater(page.locator(selector).count(), 0, selector)
                    for actual in page.locator(selector).evaluate_all("nodes => nodes.map(node => [getComputedStyle(node).fontFamily,getComputedStyle(node).fontWeight])"):
                        with self.subTest(width=width, selector=selector):
                            self.assertEqual(actual, [family, weight])
                self.assertEqual(page.locator("#review-roles .cloud-case h4 a").evaluate("node => getComputedStyle(node).fontSize"), "16px")
                for selector in ("#review-roles .jobs-heading h2", "#review-roles .delivery-heading h2"):
                    self.assertEqual(page.locator(selector).evaluate("node => getComputedStyle(node).margin"), "0px")
                if width == 390:
                    self.assertEqual(page.locator("#review-roles .investigation-jobs").evaluate("node => [getComputedStyle(node).fontSize,getComputedStyle(node).lineHeight]"), ["15px", "24px"])

    def test_browser_r2_review_record_identifiers(self):
        record = next(name for name in self.pages if name.startswith("records/") and name != "records/index.html")
        with self.browser.new_context(viewport={"width": 1440, "height": 900}) as context:
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
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
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
            page = context.new_page()
            page.goto(self.origin + "/" + name)
            self.assertTrue(page.locator(".profile-kind").evaluate("node => Boolean(node.compareDocumentPosition(node.parentElement.querySelector('h1')) & Node.DOCUMENT_POSITION_FOLLOWING)"))
            self.assertLessEqual(page.locator(".profile-kind").bounding_box()["y"] + page.locator(".profile-kind").bounding_box()["height"], page.locator(".profile-id").bounding_box()["y"])

    def test_browser_r2_flow_reserve_with_full_styles(self):
        for width, reserve in ((1440, "948px"), (1000, "1288px")):
            with self.browser.new_context(viewport={"width": width, "height": 900}) as context:
                held = []
                def route_request(route):
                    if route.request.url.startswith(self.origin + "/graph-data/"):
                        held.append(route)
                    elif route.request.url.startswith(self.origin + "/"):
                        route.continue_()
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
        with self.browser.new_context(java_script_enabled=True) as context:
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(self.origin + "/") else route.abort())
            page = context.new_page()
            for width in (320, 390, 480, 768, 1024, 1440, 1920):
                page.set_viewport_size({"width": width, "height": 900})
                for name in self.template_pages():
                    page.goto(self.origin + "/" + name)
                    page.wait_for_load_state("networkidle")
                    page.evaluate("document.fonts.ready")
                    with self.subTest(width=width, page=name):
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

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
