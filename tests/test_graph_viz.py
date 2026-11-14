import importlib.util
import re
import shutil
import subprocess
import unittest
from collections import Counter
from pathlib import Path

from intel import render, trace


ROOT = Path(__file__).resolve().parent.parent


class GraphVizTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_exact_values_time_filtering_view_parameters_and_modified_wheel_zoom(self):
        result = subprocess.run(
            [shutil.which("node"), "tests/graph_viz_runtime.cjs"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Browser runtime is unavailable")
    def test_flow_graph_reserves_desktop_height_and_opens_phone_list(self):
        from playwright.sync_api import sync_playwright
        from intel import graph

        payload = graph.flow_payload(render._flows()[0][1], {})
        styles = "\n".join((ROOT / "intel/ui" / name).read_text(encoding="utf-8") for name in ("site.css", "viz.css"))
        styles = re.sub(r"@font-face\s*\{[^}]*\}", "", styles)
        numbers = (ROOT / "intel/ui/number-format.js").read_text(encoding="utf-8")
        graphs = (ROOT / "intel/ui/graphs.js").read_text(encoding="utf-8")
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                for width in (1440, 1000, 390):
                    with self.subTest(width=width):
                        page = browser.new_page(viewport={"width": width, "height": 900})
                        page.route("**/*", lambda route: route.abort())
                        page.set_content('<style>' + styles + '</style><main class="app-content"><section class="siyab-graph" data-graph-kind="flow"><div class="graph-loading"><h2>Recorded investigation</h2></div></section><section id="transfers"><h2>Transfers</h2></section></main>')
                        before = page.locator("#transfers").evaluate("node => node.getBoundingClientRect().top")
                        page.add_script_tag(content=numbers)
                        page.add_script_tag(content=graphs)
                        page.evaluate("payload => { const section = document.querySelector('.siyab-graph'); const script = document.createElement('script'); script.type = 'application/json'; script.textContent = JSON.stringify(payload); section.append(script); return window.SiyabGraph.mount(section); }", payload)
                        after = page.locator("#transfers").evaluate("node => node.getBoundingClientRect().top")
                        if width > 760:
                            self.assertLessEqual(abs(after - before), 1, f"Transfers moved from {before} to {after} after graph loading at {width}px")
                        if width <= 760:
                            self.assertTrue(page.locator(".graph-list").evaluate("node => node.open"))
                            self.assertEqual(page.locator('.graph-mode-controls button[aria-pressed="true"]').inner_text(), "List")
                            self.assertEqual(page.locator(".siyab-graph").evaluate("node => getComputedStyle(node).minHeight"), "0px")
                        page.close()
            finally:
                browser.close()

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Browser runtime is unavailable")
    def test_transfer_inspector_labels_provider_float_and_formats_usd(self):
        from playwright.sync_api import sync_playwright
        from intel import graph

        document = render._flows()[0][1]
        payload = graph.flow_payload(document, {})
        record = payload["records"][0]
        self.assertEqual(record["from"], payload["root"])
        record.update(amount="100000000000000000000000", amount_decimals=24,
                      human_amount="0.09999999999999999")
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                for width in (1440, 375):
                    for usd, expected in (("1234567.129", "1,234,567.13"),
                                          ("0.09999999999999999", "0.10"), ("4747932.6", "4,747,932.60"), (None, "Unknown")):
                        with self.subTest(width=width, usd=usd):
                            record["usd_amount"] = usd
                            page = browser.new_page(viewport={"width": width, "height": 900})
                            page.route("**/*", lambda route: route.abort())
                            page.set_content('<section class="siyab-graph"></section>')
                            for name in ("number-format.js", "graphs.js"):
                                page.add_script_tag(content=(ROOT / "intel/ui" / name).read_text(encoding="utf-8"))
                            page.evaluate("payload => { const section = document.querySelector('.siyab-graph'); const script = document.createElement('script'); script.type = 'application/json'; script.textContent = JSON.stringify(payload); section.append(script); return window.SiyabGraph.mount(section); }", payload)
                            details = page.locator(".graph-record").first
                            details.locator("summary").click()
                            self.assertTrue(details.evaluate("node => node.open"))
                            self.assertEqual(details.locator("summary").inner_text(), f"0.1 \u00b7 {record['asset_id']}")
                            values = details.locator("dl").first.evaluate("node => Object.fromEntries([...node.querySelectorAll('dt')].map(label => [label.textContent, label.nextElementSibling.textContent]))")
                            self.assertEqual(values["USD at provider"], expected)
                            self.assertEqual(values.get("Provider float as recorded"), record["human_amount"])
                            self.assertNotIn("Human amount as recorded", values)
                            self.assertEqual(values["Amount (base units)"], record["amount"])
                            page.close()
            finally:
                browser.close()

    def test_graph_sections_identify_their_payload_kind(self):
        from intel import graph

        for payload in (graph.flow_payload(render._flows()[0][1], {}),
                        graph.cluster_payload(render._clusters()[0][1], {})):
            with self.subTest(kind=payload["kind"]):
                self.assertIn(f'data-graph-kind="{payload["kind"]}"', render._graph_section(payload))

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Browser runtime is unavailable")
    def test_cluster_graph_has_no_reserved_height(self):
        from playwright.sync_api import sync_playwright
        from intel import graph

        payload = graph.cluster_payload(render._clusters()[0][1], {})
        styles = "\n".join((ROOT / "intel/ui" / name).read_text(encoding="utf-8") for name in ("site.css", "viz.css"))
        styles = re.sub(r"@font-face\s*\{[^}]*\}", "", styles)
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                for width in (1440, 1000):
                    with self.subTest(width=width):
                        page = browser.new_page(viewport={"width": width, "height": 900})
                        page.route("**/*", lambda route: route.abort())
                        page.set_content('<style>' + styles + '</style><main class="app-content"><section class="siyab-graph" data-graph-kind="cluster"></section></main>')
                        for name in ("number-format.js", "graphs.js"):
                            page.add_script_tag(content=(ROOT / "intel/ui" / name).read_text(encoding="utf-8"))
                        page.evaluate("payload => { const section = document.querySelector('.siyab-graph'); const script = document.createElement('script'); script.type = 'application/json'; script.textContent = JSON.stringify(payload); section.append(script); return window.SiyabGraph.mount(section); }", payload)
                        section = page.locator(".siyab-graph")
                        self.assertEqual(section.evaluate("node => getComputedStyle(node).minHeight"), "0px")
                        reserved = section.evaluate("node => { const before = node.getBoundingClientRect().height; node.style.minHeight = '0'; return before - node.getBoundingClientRect().height; }")
                        self.assertLessEqual(abs(reserved), 1)
                        page.close()
            finally:
                browser.close()

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "Browser runtime is unavailable")
    def test_phone_transfer_anchor_realigns_once_unless_reader_moves(self):
        from playwright.sync_api import sync_playwright
        from intel import graph

        payload = graph.flow_payload(render._flows()[0][1], {})
        styles = "\n".join((ROOT / "intel/ui" / name).read_text(encoding="utf-8") for name in ("site.css", "viz.css"))
        styles = re.sub(r"@font-face\s*\{[^}]*\}", "", styles)
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                for reader_moves in (False, True):
                    with self.subTest(reader_moves=reader_moves):
                        page = browser.new_page(viewport={"width": 390, "height": 844})
                        html = '<style>' + styles + '</style><main class="app-content"><section class="siyab-graph"><div class="graph-loading"><h2>Recorded investigation</h2></div></section><section id="transfers"><h2>Transfers</h2></section><div style="height:2000px"></div></main>'
                        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
                        page.goto("https://siyab.invalid/flows/example.html#transfers")
                        page.evaluate("() => { window.fetch = () => new Promise(resolve => { window.finishGraph = payload => resolve({ok:true, headers:{get:()=>'application/json'}, json:async()=>payload}); }); const section = document.querySelector('.siyab-graph'); section.setAttribute('data-graph-src', '/graph-data/' + 'a'.repeat(64) + '.json'); window.anchorCalls = 0; const target = document.getElementById('transfers'); const original = target.scrollIntoView.bind(target); target.scrollIntoView = (...args) => { window.anchorCalls++; original(...args); }; }")
                        for name in ("number-format.js", "graphs.js"):
                            page.add_script_tag(content=(ROOT / "intel/ui" / name).read_text(encoding="utf-8"))
                        page.evaluate("() => { window.graphLoad = window.SiyabGraph.mount(document.querySelector('.siyab-graph')); }")
                        if reader_moves:
                            page.mouse.wheel(0, -120)
                        page.evaluate("payload => { window.finishGraph(payload); return window.graphLoad; }", payload)
                        self.assertEqual(page.evaluate("window.anchorCalls"), 0 if reader_moves else 1)
                        if not reader_moves:
                            page.wait_for_function("Math.abs(document.getElementById('transfers').getBoundingClientRect().top - parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop)) < 2")
                            page.evaluate("() => window.SiyabGraph.mount(document.querySelector('.siyab-graph'))")
                            self.assertEqual(page.evaluate("window.anchorCalls"), 1)
                        page.close()
            finally:
                browser.close()

    def test_evidence_pagination_retains_every_transfer_once_and_full_context(self):
        name, document = render._flows()[0]
        counts = Counter()
        number_of_pages = (len(document["hops"]) + 49) // 50
        for number in range(1, number_of_pages + 1):
            page = render.flow_page(document, {}, evidence_url=f"/flows/{name}.html", page_number=number)
            cards = re.findall(r'<div class="card flow-transfer">(.*?)</div>', page, re.DOTALL)
            self.assertLessEqual(len(cards), 50)
            self.assertIn(f"Page {number} of {number_of_pages}", page)
            self.assertIn(document["fetched_at"], page)
            self.assertIn("#transfers", page)
            self.assertIn("Shares of the gross outbound transfer value", page)
            for card in cards:
                match = re.search(r'transaction ([^<]+)<br>receipt ([^<]+)', card)
                self.assertIsNotNone(match)
                counts[match.groups()] += 1
        expected = Counter((hop["transaction_hash"], hop["receipt_id"]) for hop in document["hops"])
        self.assertEqual(counts, expected)
        self.assertEqual(sum(counts.values()), len(document["hops"]))
        self.assertEqual(len(trace.hop_depths(document)), len(document["hops"]))
        with self.assertRaises(ValueError):
            render.flow_page(document, {}, evidence_url=f"/flows/{name}.html", page_number=number_of_pages + 1)

    def test_timeline_exposes_exact_intervals_and_keyboard_readouts(self):
        document = render._flows()[0][1]
        chart = render._flow_analytics(document, {})
        bins = render._flow_timeline(document)
        self.assertEqual(chart.count('data-bin-index='), len(bins))
        self.assertIn('class="chart-readout" role="status"', chart)
        self.assertIn('data-chart-inspect', chart)
        for row in bins:
            self.assertIn(f'data-chart-start="{row["from_ns"]}" data-chart-end="{row["to_ns"]}"', chart)
            self.assertIn(f'data-chart-count="{row["count"]}"', chart)
        self.assertIn('Full recorded dataset, not the current graph filter.', chart)
