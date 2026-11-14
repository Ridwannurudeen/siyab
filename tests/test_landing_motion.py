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

    def test_graph_edges_keep_their_recorded_depth(self):
        payload = graph.flow_payload(dict(self.flows)["rhea-collector"], render.account_pages(self.store))
        depths = {record["id"]: record["depth"] for record in payload["records"]}
        edges = {edge["id"]: edge for edge in payload["edges"]}
        paths = re.findall(r'<path class="landing-edge[^"]*" data-edge-id="([^"]+)" data-depth="(\d+)"', self.page)
        self.assertGreater(len(paths), 2)
        self.assertEqual({depth for _, depth in paths}, {"1", "2"})
        for edge_id, depth in paths:
            self.assertEqual(int(depth), min(depths[record] for record in edges[edge_id]["record_ids"]))

    def test_replay_is_a_script_enabled_button_under_the_counter(self):
        block = re.search(r'<div class="landing-counter-block">(.*?)</div>', self.page, re.S).group(1)
        self.assertIn('<button type="button" class="landing-replay" hidden>Replay trace</button>', block)
        self.assertLess(block.index('class="landing-counter"'), block.index('class="landing-replay"'))

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
            self.assertLessEqual(set(properties), {"opacity", "transform", "stroke-dashoffset"})
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
        for mode in ("no-script", "reduce", "no-observer", "hidden"):
            with self.subTest(mode=mode):
                options = {"java_script_enabled": False} if mode == "no-script" else {"reduced_motion": "reduce"} if mode == "reduce" else {}
                context, page = self.open_page(**options)
                if mode == "no-observer":
                    context.add_init_script("window.IntersectionObserver = undefined")
                if mode == "hidden":
                    context.add_init_script("Object.defineProperty(document, 'hidden', {get:()=>true})")
                page.goto(self.origin + "/index.html")
                self.assertEqual(page.locator(".landing-motion-ready").count(), 0)
                self.assert_final(page)
                self.assertFalse(page.get_by_role("button", name="Replay trace").is_visible())
                context.close()

    def test_sequences_finish_once_by_their_deadlines(self):
        context, page = self.open_page(reduced_motion="no-preference")
        context.add_init_script("window.landingStarts=[];document.addEventListener('animationstart', event => window.landingStarts.push({node:event.target,name:event.animationName}))")
        page.goto(self.origin + "/index.html")
        self.assertEqual(page.locator(".landing-motion-ready").count(), 1)
        self.assertEqual(page.locator('[data-landing-sequence="pipeline"].is-running, [data-landing-sequence="timeline"].is-running').count(), 0)
        for name in SEQUENCES:
            sequence = page.locator('[data-landing-sequence="' + name + '"]')
            sequence.evaluate("node => node.scrollIntoView({block:'center',behavior:'instant'})")
            page.wait_for_function("name => document.querySelector('[data-landing-sequence=\"' + name + '\"]').classList.contains('is-complete')", arg=name, timeout=5000)
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

    def test_observer_requires_35_percent_and_unobserves_each_sequence(self):
        context, page = self.open_page(reduced_motion="no-preference")
        context.add_init_script("""window.IntersectionObserver = class {
            constructor(callback, options) { window.landingObserver=this; this.callback=callback; this.options=options; this.targets=new Set(); }
            observe(node) { this.targets.add(node); }
            unobserve(node) { this.targets.delete(node); }
            disconnect() { this.targets.clear(); }
        };""")
        page.goto(self.origin + "/index.html")
        self.assertEqual(page.evaluate("landingObserver.options.threshold"), 0.35)
        self.assertEqual(page.locator(".landing .is-running").count(), 0)
        self.assertEqual(page.locator(".landing-counter").inner_text(), "0")
        self.assertEqual(page.locator(".landing-edge, .landing-connector path").evaluate_all("nodes => nodes.filter(node => getComputedStyle(node).strokeDashoffset !== '1px').length"), 0)
        self.assertEqual(page.locator(".landing-timeline-rail").evaluate("node => getComputedStyle(node).transform"), "matrix(1, 0, 0, 0, 0, 0)")
        for name in SEQUENCES:
            for ratio in (0.34, 0.35):
                page.evaluate("([name,ratio]) => landingObserver.callback([{target:document.querySelector('[data-landing-sequence=\"' + name + '\"]'),isIntersecting:true,intersectionRatio:ratio}])", [name, ratio])
                self.assertEqual(page.locator('[data-landing-sequence="' + name + '"].is-running').count(), int(ratio == 0.35))
            self.assertFalse(page.evaluate("name => landingObserver.targets.has(document.querySelector('[data-landing-sequence=\"' + name + '\"]'))", name))
            timings = page.locator('[data-landing-sequence="' + name + '"]').evaluate("node => node.getAnimations({subtree:true}).map(animation => ({...animation.effect.getTiming(),easing:getComputedStyle(animation.effect.target).animationTimingFunction,depth:animation.effect.target.dataset.depth,highlight:animation.effect.target.classList.contains('is-highlight')}))")
            self.assertTrue(timings)
            for timing in timings:
                self.assertEqual(timing["easing"], "cubic-bezier(0.65, 0, 0.35, 1)")
                self.assertEqual(timing["iterations"], 1)
                duration = (1200 if timing["highlight"] else 2200) if name == "graph" else 700 if name == "pipeline" else 1000
                delay = (900 if timing["highlight"] else 500 if timing["depth"] == "2" else 0) if name == "graph" else 0
                self.assertEqual(timing["duration"], duration)
                self.assertEqual(timing["delay"], delay)
        context.close()

    def test_counter_easing_end_value_and_frame_cleanup(self):
        context, page = self.open_page(reduced_motion="no-preference")
        context.add_init_script("""window.landingFrames=new Map(); window.landingClock=0;
            performance.now=()=>landingClock;
            window.requestAnimationFrame=callback=>{const id=++window.landingClock;landingFrames.set(id,callback);return id;};
            window.cancelAnimationFrame=id=>landingFrames.delete(id);""")
        page.goto(self.origin + "/index.html")
        page.wait_for_function("document.querySelector('[data-landing-sequence=graph]').classList.contains('is-running')")
        counter = page.locator(".landing-counter")
        self.assertEqual(counter.inner_text(), "0")
        width = counter.bounding_box()["width"]
        page.evaluate("for(const [id,callback] of landingFrames){landingFrames.delete(id);callback(1100);break;}")
        self.assertAlmostEqual(int(counter.inner_text().replace(',', '')), 1708 * 0.5, delta=2)
        self.assertAlmostEqual(counter.bounding_box()["width"], width, delta=0.1)
        page.evaluate("for(const [id,callback] of landingFrames){landingFrames.delete(id);callback(2200);break;}")
        self.assertEqual(counter.inner_text(), "1,708")
        self.assertAlmostEqual(counter.bounding_box()["width"], width, delta=0.1)
        self.assertEqual(page.evaluate("landingFrames.size"), 0)
        context.close()

    def test_replay_restarts_only_the_hero_from_keyboard(self):
        context, page = self.open_page(reduced_motion="no-preference")
        page.goto(self.origin + "/index.html")
        page.wait_for_function("document.querySelector('[data-landing-sequence=graph]').classList.contains('is-complete')", timeout=5000)
        replay = page.get_by_role("button", name="Replay trace")
        replay.focus()
        self.assertEqual(replay.evaluate("node => getComputedStyle(node).outlineStyle"), "solid")
        self.assertTrue(replay.evaluate("node => node.matches(':focus-visible') && node.tabIndex === 0"))
        replay.press("Enter")
        self.assertEqual(page.locator('[data-landing-sequence="graph"].is-running').count(), 1)
        self.assertLess(int(page.locator(".landing-counter").inner_text().replace(',', '')), 1708)
        self.assertEqual(page.locator('[data-landing-sequence="pipeline"].is-running, [data-landing-sequence="timeline"].is-running').count(), 0)
        page.wait_for_function("document.querySelector('[data-landing-sequence=graph]').classList.contains('is-complete')", timeout=5000)
        self.assertEqual(page.locator(".landing-counter").inner_text(), "1,708")
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertGreaterEqual(replay.bounding_box()["height"], 44)
        context.close()

    def test_preference_changes_finish_pending_sequences_and_hide_replay(self):
        context, page = self.open_page(reduced_motion="no-preference")
        page.goto(self.origin + "/index.html")
        page.wait_for_timeout(300)
        page.emulate_media(reduced_motion="reduce")
        page.wait_for_function("!document.querySelector('.landing-motion-ready')")
        self.assert_final(page)
        self.assertFalse(page.get_by_role("button", name="Replay trace").is_visible())
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_timeout(100)
        self.assert_final(page)
        self.assertTrue(page.get_by_role("button", name="Replay trace").is_visible())
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
        self.assertFalse(page.get_by_role("button", name="Replay trace").is_visible())
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
                    for label, path in (("Explore", "/explore.html"), ("Account lookup", "/investigate.html"), ("Incidents", "/explore.html#incidents"), ("Verify", "/verify.html")):
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
