import html
import re
import unittest
from unittest import mock

from intel import graph, labels, render
from tests import test_design_system
from tests.test_landing import Text


DISCLAIMER = ("Historical example, not a current network response. Hash equality does not verify "
              "a signature or establish the truth of the evidence sources.")
SEQUENCES = ("graph", "pipeline", "timeline")


class LandingMotionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flows = render._flows()
        cls.store = labels.load()
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network read")):
            cls.page = render.index_page(render._incidents(), cls.flows, render._clusters(), cls.store)
        cls.css = (render.ROOT / "intel/ui/site.css").read_text(encoding="utf-8")
        parser = Text()
        parser.feed(cls.page)
        cls.text = html.unescape(" ".join(parser.parts))

    def test_landing_has_its_own_frame_and_navigation(self):
        self.assertRegex(self.page, r'<body[^>]*class="[^"]*landing-page')
        self.assertNotRegex(self.page, r'<aside[^>]*class="[^"]*(?:app-sidebar|sidebar)')
        for label in ("Explore", "Account lookup", "Incidents", "Verify", "Menu"):
            self.assertIn(label, self.page)
        for href in ("/explore.html", "/investigate.html", "/verify.html"):
            self.assertIn('href="' + href + '"', self.page)
        self.assertIn('id="theme-toggle"', self.page)
        self.assertIn('id="global-search"', self.page)

    def test_recorded_graph_uses_real_nodes_and_edges_and_is_deterministic(self):
        match = re.search(r'<svg[^>]*class="[^"]*landing-graph-svg[^>]*>.*?</svg>', self.page, re.S)
        self.assertIsNotNone(match)
        svg = match.group(0)
        payload = graph.flow_payload(dict(self.flows)["rhea-collector"], render.account_pages(self.store))
        nodes = re.findall(r'data-node-id="([^"]+)"', svg)
        edges = re.findall(r'data-edge-id="([^"]+)"', svg)
        self.assertGreater(len(nodes), 3)
        self.assertLessEqual(len(nodes), 41)
        self.assertGreater(len(edges), 2)
        self.assertLessEqual(len(edges), 60)
        self.assertEqual(len(nodes), len(set(nodes)))
        self.assertEqual(len(edges), len(set(edges)))
        self.assertIn(payload["root"], nodes)
        self.assertLessEqual(set(nodes), {node["id"] for node in payload["nodes"]})
        selected = {edge["id"]: edge for edge in payload["edges"]}
        self.assertLessEqual(set(edges), set(selected))
        for edge_id in edges:
            self.assertIn(selected[edge_id]["from"], nodes)
            self.assertIn(selected[edge_id]["to"], nodes)
        rebuilt = render.index_page(render._incidents(), self.flows, render._clusters(), self.store)
        self.assertEqual(svg, re.search(r'<svg[^>]*class="[^"]*landing-graph-svg[^>]*>.*?</svg>', rebuilt, re.S).group(0))

    def test_counter_and_status_share_the_recorded_transfer_count(self):
        count = len(dict(self.flows)["rhea-collector"]["hops"])
        counter = re.search(r'class="[^"]*landing-counter[^>]*>(.*?)</', self.page, re.S)
        self.assertIsNotNone(counter)
        self.assertEqual(re.sub(r'<[^>]+>', '', counter.group(1)).strip(), f"{count:,}")
        self.assertIn(f"{count:,} recorded transfers.", self.text)
        self.assertIn("RECORDED TRANSFERS", self.text)

    def test_disclaimer_appears_once_and_limits_keep_five_items(self):
        self.assertEqual(self.text.count(DISCLAIMER), 1)
        limits = re.search(r'<ul class="landing-limits">(.*?)</ul>', self.page, re.S)
        self.assertEqual(limits.group(1).count("<li>"), 5)

    def test_display_role_and_motion_tokens_are_defined(self):
        design = (render.ROOT / "DESIGN.md").read_text(encoding="utf-8")
        self.assertIn("display", design.lower())
        self.assertIn("clamp(38px,4.7vw,68px)", self.css.replace(" ", ""))
        self.assertIn("text-wrap:balance", self.css.replace(" ", ""))
        self.assertRegex(self.css, r'--[a-z-]*display[a-z-]*:')
        self.assertRegex(self.css, r'--landing-[a-z-]+:[^;}]*cubic-bezier')
        self.assertRegex(self.css, r'--landing-[a-z-]+:[^;}]*[0-9]ms')
        for name in SEQUENCES:
            self.assertIn('data-landing-sequence="' + name + '"', self.page)

    def test_landing_keyframes_only_animate_allowed_properties(self):
        found = 0
        for selector, body, contexts in test_design_system.rules(self.css):
            if not any(context.startswith("@keyframes") and "landing" in context for context in contexts):
                continue
            found += 1
            properties = re.findall(r'(?:^|;)\s*([a-z-]+)\s*:', body)
            self.assertLessEqual(set(properties), {"opacity", "transform", "stroke-dashoffset", "clip-path"})
        self.assertGreater(found, 0)
        for selector, body, contexts in test_design_system.rules(self.css):
            if "landing" in selector and "animation:" in body:
                self.assertIn("var(--landing-", body)
                self.assertNotIn("infinite", body)


class LandingMotionBrowserTest(unittest.TestCase):
    setUpClass = classmethod(test_design_system.DesignSystemTest.setUpClass.__func__)
    local_route = test_design_system.DesignSystemTest.local_route

    def setUp(self):
        if self.browser is None:
            self.skipTest(self.browser_reason)

    def open_page(self, **options):
        context = self.browser.new_context(viewport={"width": 1440, "height": 900}, **options)
        self.addCleanup(context.close)
        context.route("**/*", self.local_route)
        return context, context.new_page()

    def assert_final(self, page):
        self.assertEqual(page.locator(".landing-counter").inner_text(), f'{len(dict(render._flows())["rhea-collector"]["hops"]):,}')
        self.assertEqual(page.locator(".landing .is-running").count(), 0)
        self.assertEqual(page.evaluate("document.getAnimations().filter(a => a.playState === 'running').length"), 0)
        unfinished = page.locator('[data-landing-sequence], .landing-graph-svg [data-node-id], .landing-graph-svg [data-edge-id]').evaluate_all("nodes => nodes.filter(node => { const s=getComputedStyle(node); return Number(s.opacity) === 0 || !['0','0px','none'].includes(s.strokeDashoffset) || s.willChange !== 'auto'; }).map(node => node.outerHTML)")
        self.assertEqual(unfinished, [])

    def test_script_free_and_reduced_motion_and_missing_observer_are_final_immediately(self):
        for mode in ("no-script", "reduce", "no-observer"):
            with self.subTest(mode=mode):
                options = {"java_script_enabled": False} if mode == "no-script" else {"reduced_motion": "reduce"} if mode == "reduce" else {}
                context, page = self.open_page(**options)
                if mode == "no-observer":
                    context.add_init_script("window.IntersectionObserver = undefined")
                page.goto(self.origin + "/index.html")
                self.assertEqual(page.locator(".landing-motion-ready").count(), 0)
                self.assert_final(page)
                context.close()

    def test_sequences_finish_once_by_their_deadlines(self):
        context, page = self.open_page(reduced_motion="no-preference")
        context.add_init_script("window.landingStarts=[];document.addEventListener('animationstart', event => window.landingStarts.push({node:event.target,name:event.animationName}))")
        page.goto(self.origin + "/index.html")
        self.assertEqual(page.locator(".landing-motion-ready").count(), 1)
        for name, elapsed in (("graph", 2300), ("pipeline", 300), ("timeline", 1600)):
            page.wait_for_timeout(elapsed)
            self.assertEqual(page.locator('[data-landing-sequence="' + name + '"].is-complete').count(), 1)
        self.assert_final(page)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)
        self.assert_final(page)
        self.assertEqual(page.locator("[data-landing-sequence].is-complete").count(), 3)
        self.assertGreater(page.evaluate("window.landingStarts.length"), 0)
        self.assertFalse(page.evaluate("window.landingStarts.some((entry,index,rows) => rows.slice(0,index).some(prior => prior.node===entry.node && prior.name===entry.name))"))
        context.close()

    def test_display_role_keeps_its_tracking_and_leading_on_phones(self):
        context, page = self.open_page(reduced_motion="reduce")
        for width in (320, 390, 768, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(self.origin + "/index.html")
            style = page.locator("#landing-title").evaluate("node => {const s=getComputedStyle(node);return {size:parseFloat(s.fontSize),line:parseFloat(s.lineHeight),tracking:parseFloat(s.letterSpacing),weight:s.fontWeight,family:s.fontFamily}}")
            with self.subTest(width=width):
                self.assertAlmostEqual(style["size"], max(38, min(width * 0.047, 68)), places=2)
                self.assertAlmostEqual(style["line"], style["size"] * 1.04, places=2)
                self.assertAlmostEqual(style["tracking"], style["size"] * -0.035, places=2)
                self.assertEqual(style["weight"], "600")
                self.assertTrue(style["family"].startswith("Manrope"), style["family"])
        context.close()

    def test_hidden_document_jumps_to_final_state(self):
        context, page = self.open_page(reduced_motion="no-preference")
        page.goto(self.origin + "/index.html")
        page.wait_for_timeout(300)
        page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))")
        self.assert_final(page)
        context.close()

    def test_both_themes_reflow_with_topbar_and_phone_targets(self):
        for theme in ("dark", "light"):
            context, page = self.open_page(reduced_motion="reduce")
            context.add_init_script("localStorage.setItem('siyab-theme', '" + theme + "')")
            for width in (320, 390, 768, 1024, 1440):
                with self.subTest(theme=theme, width=width):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.goto(self.origin + "/index.html")
                    page.evaluate("document.fonts.ready")
                    self.assertEqual(page.locator("aside.app-sidebar, aside.sidebar").count(), 0)
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    menu = page.locator("#mobile-navigation > summary")
                    if menu.is_visible():
                        menu.click()
                    for label, path in (("Explore", "/explore.html"), ("Account lookup", "/investigate.html"), ("Incidents", "/explore.html#explore-title"), ("Verify", "/verify.html")):
                        links = page.locator("header a").filter(has_text=re.compile("^" + label + "$"))
                        visible = [link for link in links.all() if link.is_visible()]
                        self.assertTrue(visible, label)
                        self.assertTrue(all(link.evaluate("node => new URL(node.href).pathname + new URL(node.href).hash") == path for link in visible))
                        target, _, anchor = path.partition("#")
                        document = self.out / target.lstrip("/")
                        self.assertTrue(document.is_file())
                        if anchor:
                            self.assertIn('id="' + anchor + '"', document.read_text(encoding="utf-8"))
                    if width <= 390:
                        small = page.locator("header a,header button,header summary,main a,main input,main button,main summary").evaluate_all("nodes => nodes.filter(node => node.checkVisibility()).filter(node => {const r=node.getBoundingClientRect();return r.width<44 || r.height<44}).map(node=>node.outerHTML)")
                        self.assertEqual(small, [])
                        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
            context.close()


if __name__ == "__main__":
    unittest.main()
