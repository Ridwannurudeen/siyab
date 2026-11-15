import hashlib
import html
import json
import re
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from intel import labels, render, trace
from tests import test_design_system


SECTIONS = ("SIYAB", "THE PROBLEM", "HOW IT WORKS", "RECORDED INCIDENT", "CAPABILITIES", "LIMITS", "PRINCIPLES", "START HERE")
COPY = (
    "Security intelligence for NEAR, with a source on every row.",
    "Sourced labels, incident records and payment checks for people, apps and AI agents on NEAR.",
    "Siyab checks the receiver, token, amount and memo of a payment before it is sent, and shows the evidence behind every answer. For AI agents, a Gate contract on testnet holds the agent's funds and pays only on a signed ALLOW bound to that exact transfer.",
    "Recorded mainnet investigation. 1,708 recorded transfers. Depth reached: 2 of 3. Stopped while reading an account 1 hop from the subject.",
    "What if an invoice is poisoned?",
    "One recorded case:",
    "A scripted agent follows an instruction planted in a vendor message and asks to pay an account named in a public incident report. The verdict service answers DENY. The agent submits anyway, without a verdict, and the Gate refuses the transfer.",
    "Changed payment details.", "A lookalike receiver.", "A receiver with an incident label.", "A check that cannot run.",
    "Nothing was signed.", "REFUSED:no_verdict",
    "A scripted agent stands in for a prompt injected model. No model runs.",
    "Check. Enforce. Verify.",
    "Judge the receiver, token, amount and memo before a person, an app or an agent pays. A check that cannot run reads Unknown.",
    "The agent’s function call key can only request a transfer from the Gate. The Gate requires a signed ALLOW and enforces the owner’s payment policy.",
    "Compare a registry record with its evidence hash.",
    "Historical example, not a current network response. Hash equality does not verify a signature or establish the truth of the evidence sources.",
    "Rhea Lend margin trading route exploit.",
    "Recorded outbound transfers from the collector account named by rekt.news. These rows do not establish loss or causal attribution.",
    "Recorded transfers. Gross transfer value is not loss.",
    "Explore the evidence.",
    "Finalized account state, indexed activity and the evidence behind each observation.",
    "Recorded transfers with transaction hashes and blocks.",
    "Account links with their source and confidence. Each edge names its heuristic and provenance.",
    "Follow finalized mainnet blocks, with the source block behind each event.",
    "Issuer status reads and action history.",
    "Accounts from on chain registries, with a source on each assertion.",
    "What these reads mean.",
    "Payment destination intelligence, without transaction or calldata simulation.",
    "The Gate demonstrations run on testnet. After the deadline, on 15 Nov 2026, siyabgate.near hosted a one-off mainnet Gate run (code_hash 5EmM2UQRYcvaNvJX6jfcgtNS7Mm8ZPjZhwK1J8bMXrcu): a labelled collector request was refused as REFUSED:no_verdict with nothing spent, and a signed ALLOW paid 0.01 NEAR to an allowlisted payee. The Gate was then paused and the agent revoked. No Gate is in service on mainnet; siyabgate.near is paused with its agent key revoked. This run is outside the hackathon-submission tag.",
    "Inferred attributions remain hypotheses and never add a subject to the refusal set.",
    "Live API checks are read only and never sign.", "Unknown never passes.",
    "Every label has a source. Every verdict has evidence. Unknown never passes.",
    "Investigate an account.", "Enter a NEAR account to read mainnet data. No payment is signed or sent.",
    "Data: NearBlocks. Blocks: mainnet.neardata.xyz. Transfers: FastNEAR.",
)
FORBIDDEN = (
    "Transaction simulation or calldata analysis.", "Mainnet Gate deployment or enforcement.",
    "Guaranteed safety", "compre" + "hensive prompt injection protection", "guaranteed safe",
    "Registry verification as a prerequisite enforced by the Gate.",
    "Hash equality as proof of source truth", "signature validity or registry consultation",
    "Complete fund tracing", "transfer value presented as loss", "Inferred ownership presented as fact.",
    "Third party adoption", "endorsed integrations", "partners", "testimonials", "pricing",
    "A TEE", "NEAR browser extension", "interactive Telegram scanner",
    "General latency guarantees", "receiver success rates", "100" + "% safe",
)


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if not self.in_script:
            self.parts.append(data)


class LandingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network read")):
            cls.page = render.index_page(render._incidents(), render._flows(), render._clusters(), labels.load())
        cls.main = re.search(r'<main[^>]*>.(.*?)</main>', cls.page, re.S).group(1)
        parser = Text()
        parser.feed(cls.page)
        cls.text = re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts)))

    def test_sections_and_labels_follow_the_approved_order(self):
        self.assertEqual(re.findall(r'class="landing-label">\(\d{2}\)  ([^<]+)', self.main), list(SECTIONS))
        self.assertEqual(re.findall(r'class="landing-label">(\(\d{2}\))', self.main), [f"({number:02})" for number in range(1, 9)])

    def test_approved_copy_is_complete_without_scripts(self):
        for line in COPY:
            with self.subTest(line=line):
                self.assertIn(line, self.text)
        self.assertNotIn('<script', self.main)
        for forbidden in FORBIDDEN:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden.lower(), self.text.lower())
        self.assertNotRegex(self.text, r"[\u2013\u2014]")

    def test_problem_story_is_labeled_as_one_recorded_case(self):
        self.assertIn('<p>One recorded case:</p><p class="landing-statement">', self.main)

    def test_recorded_check_uses_the_committed_judgement_and_sources(self):
        subject = dict(render._flows())["rhea-collector"]["subject"]
        record = json.loads((render.ROOT / "fixtures/receivers/judgements" / (subject + ".json")).read_text(encoding="utf-8"))
        panel = self.main.split('id="landing-check"', 1)[1].split('</article>', 1)[0]
        self.assertIn(record["verdict"], panel)
        self.assertIn('class="verdict deny">DENY<', panel)
        self.assertNotIn("label store Recorded label store", panel)
        self.assertIn(subject[:8] + "..." + subject[-8:], panel)
        self.assertIn('data-account="' + subject + '"', panel)
        for reason in record["reasons"]:
            self.assertIn(html.escape(reason, quote=True), panel)
        for check in record["checks"]:
            if check["status"] == "signal":
                self.assertIn(check["source"]["provider"], panel)
        self.assertIn('https://rekt.news/rhea-finance-rekt', panel)
        self.assertIn('Recorded', panel)

    def test_recorded_check_rejects_a_judgement_other_than_deny(self):
        subject = dict(render._flows())["rhea-collector"]["subject"]
        path = render.ROOT / "fixtures/receivers/judgements" / (subject + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        original = Path.read_text
        for verdict in ("ALLOW", "UNKNOWN"):
            with self.subTest(verdict=verdict):
                changed = dict(record, verdict=verdict)
                def read(candidate, *args, **kwargs):
                    return json.dumps(changed) if candidate == path else original(candidate, *args, **kwargs)
                with mock.patch.object(Path, "read_text", read), self.assertRaisesRegex(ValueError, "recorded.*DENY|Recorded.*DENY"):
                    render.index_page(render._incidents(), render._flows(), render._clusters(), labels.load())

    def test_recorded_check_amount_comes_from_the_request(self):
        subject = dict(render._flows())["rhea-collector"]["subject"]
        path = render.ROOT / "fixtures/receivers/judgements" / (subject + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["request"]["amount"] = "1250000000000000000000000"
        original = Path.read_text
        def read(candidate, *args, **kwargs):
            return json.dumps(record) if candidate == path else original(candidate, *args, **kwargs)
        with mock.patch.object(Path, "read_text", read):
            page = render.index_page(render._incidents(), render._flows(), render._clusters(), labels.load())
        panel = page.split('id="landing-check"', 1)[1].split('</article>', 1)[0]
        self.assertIn("Unsigned receiver judgement for 1.25 NEAR.", panel)

    def test_recorded_check_links_only_https_incidents_and_keeps_rpc_as_text(self):
        store = labels.load()
        for url in ("https://incident.example/report", "http://incident.example/report", "//incident.example/report"):
            with self.subTest(url=url):
                assertions = [replace(row, source=dict(row.source, url=url))
                              if row.category == "incident" and row.source.get("url") else row
                              for row in store.assertions]
                with mock.patch.object(labels, "load", return_value=labels.Store(assertions, store.sha256)):
                    page = render.index_page(render._incidents(), render._flows(), render._clusters(), store)
                panel = page.split('id="landing-check"', 1)[1].split('</article>', 1)[0]
                reasons = panel.split('<ul class="landing-check-reasons">', 1)[1].split('</ul>', 1)[0]
                self.assertIn("incident.example", reasons)
                self.assertEqual('href="' + url + '"' in reasons, url.startswith("https://"))
                issuer = next(row for row in assertions if row.source.get("method") == "get_blacklist_status"
                              and row.category == "watch" and urlsplit(row.source["rpc"]).hostname in reasons)
                self.assertIn(urlsplit(issuer.source["rpc"]).hostname, reasons)
                self.assertNotIn('href="' + issuer.source["rpc"] + '"', reasons)

    def test_recorded_check_keeps_its_sources_with_a_different_page_store(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network read")):
            page = render.index_page(render._incidents(), render._flows(), render._clusters(), labels.Store([], "0" * 64))
        self.assertIn('https://rekt.news/rhea-finance-rekt', page)

    def test_hero_copy_and_graph_have_separate_grid_areas(self):
        css = (render.ROOT / "intel/ui/site.css").read_text(encoding="utf-8")
        styles = {selector: dict(re.findall(r"([a-z-]+):([^;]+)", body))
                  for selector, body, context in test_design_system.rules(css) if not context}
        self.assertEqual(styles[".landing-hero"]["display"], "grid")
        self.assertEqual(styles[".landing-hero-copy"]["grid-area"], "copy")
        self.assertEqual(styles[".landing-graph-svg"]["grid-area"], "graph")
        self.assertIn('"copy graph"', styles[".landing-hero"]["grid-template-areas"])
        self.assertNotEqual(styles[".landing-graph-svg"].get("position"), "absolute")
        self.assertRegex(self.main, r'<section class="landing-hero"[^>]*><svg[^>]*class="landing-graph-svg"')
        self.assertRegex(self.main, r'</svg><div class="landing-hero-copy"><div class="page-head">')

    def test_counts_come_from_the_counts_command(self):
        line = next(line for line in render._counts_lines() if line.startswith("Label store:"))
        expected = re.sub(r"\d{4,}", lambda match: f"{int(match.group()):,}", line.split(" (")[0] + ".")
        self.assertRegex(line, r"\d{4,}")
        self.assertIn(expected, self.text)

    def test_recorded_transfer_excerpt_retains_source_evidence(self):
        document = dict(render._flows())["rhea-collector"]
        for amount, when in (("74,000", "2026-04-15 07:12 UTC"), ("95,000", "2026-04-15 07:13 UTC")):
            row = next(hop for hop in document["hops"] if render._transfer_amount(hop) == amount and trace.utc_minute(hop["block_timestamp"]) == when)
            for value in (row["transaction_hash"], row["receipt_id"], row["asset_id"], when):
                self.assertIn(value, self.main)
            self.assertIn('https://nearblocks.io/txns/' + row["transaction_hash"], self.main)
            self.assertIn(f'{amount} units of', self.text)

    def test_unknown_decimals_do_not_repeat_units(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network read")), \
                mock.patch.object(trace, "asset_decimals", return_value=None):
            page = render.index_page(render._incidents(), render._flows(), render._clusters(), labels.load())
        self.assertNotIn("units units", page)
        self.assertNotIn("unit units", page)
        self.assertRegex(page, r"\d base units? of <q")

    def test_verification_values_are_computed_from_the_saved_record(self):
        record = json.loads(render.VERIFY_RECORD.read_text(encoding="utf-8"))
        _, subject, evidence_hash = render.verify_example()
        compared = hashlib.sha256((render.EVIDENCE_DIR / (evidence_hash + ".json")).read_bytes()).hexdigest()
        for value in (subject, record["verdict"], "2026-11-03 14:30:41 UTC", "Equal" if compared == evidence_hash else "Different"):
            self.assertIn(value, self.main)
        changed = dict(record, evidence_hash="0" * 64)
        original = Path.read_text
        def read(path, *args, **kwargs):
            return json.dumps(changed) if path == render.VERIFY_RECORD else original(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", read), mock.patch.object(render, "verify_example", return_value=("registry.testnet", subject, evidence_hash)):
            page = render.index_page(render._incidents(), render._flows(), render._clusters(), labels.load())
        self.assertIn("Different", page)

    def test_routes_keep_graph_and_account_lookup_separate(self):
        self.assertRegex(self.main, r'href="/explore.html">Investigate</a>')
        self.assertIn('action="/investigate.html" method="get"', self.main)
        self.assertIn('name="account"', self.main)
        self.assertIn('href="/verify.html"', self.main)
        for title in ("Siyab", "Explore", "Investigate account", "Incident record"):
            page = render.page(title, "<h1>Example</h1>", record=title == "Incident record")
            self.assertRegex(page, r'href="index.html"[^>]*>Home</a><a href="explore.html"[^>]*>Explore</a>')
            self.assertNotIn('index.html#tools', page)
            self.assertNotIn('index.html#search', page)

    def test_explore_preserves_the_old_workspace_content_and_scripts(self):
        page = render.explore_page(render._incidents(), render._flows(), render._clusters(), labels.load())
        main = re.search(r'<main[^>]*>\n(.*?)\n</main>', page, re.S).group(1)
        main = re.sub(r'<article class="analytics-panel"><div class="analytics-panel-head"><h2>Label store provenance</h2>.*?</article>', '', main, flags=re.S)
        main = main.replace('/index.html', '/explore.html')
        expected = "b1dc3711be51f7608278c262b87af4b81" + "943867bd544a3007e8fa7d2d3ce11ee"
        self.assertEqual(hashlib.sha256(main.encode("utf-8")).hexdigest(), expected)


class LandingBrowserTest(unittest.TestCase):
    setUpClass = classmethod(test_design_system.DesignSystemTest.setUpClass.__func__)
    local_route = test_design_system.DesignSystemTest.local_route

    def setUp(self):
        if self.browser is None:
            self.skipTest(self.browser_reason)

    def test_warning_display_preserves_api_outcome_and_downloaded_evidence(self):
        data = {"outcome": "ASK", "action": "ask", "verdict": None, "signing": "never",
                "evidence_hash": "a" * 64, "evidence": "{}", "freshness": {"block_height": 1},
                "latency_ms": 1, "reasons": ["A check could not run."],
                "checks": [{"name": "Account age", "status": "unknown", "detail": "Unavailable."}]}
        context = self.browser.new_context(accept_downloads=True)
        self.addCleanup(context.close)
        context.route("**/*", self.local_route)
        page = context.new_page()
        for width in (1440, 375):
            for outcome, action, status, expected, css in (
                    ("ASK", "ask", "unknown", "WARN", "ask"),
                    ("ALLOW", "ask", "pass", "WARN", "ask"),
                    ("ALLOW", "sign", "pass", "ALLOW", "allow"),
                    ("DENY", "refuse", "signal", "DENY", "deny")):
                with self.subTest(width=width, outcome=outcome, action=action):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.goto(self.origin + "/index.html")
                    data["outcome"], data["action"] = outcome, action
                    data["checks"][0]["status"] = status
                    result = page.evaluate("""data => {
                      const freeze = value => {
                        if (value && typeof value === 'object') {
                          Object.values(value).forEach(freeze);
                          Object.freeze(value);
                        }
                      };
                      freeze(data);
                      const panel = document.getElementById('landing-check-result');
                      const token = window.SiyabChecks.beginRequest(panel);
                      return {rendered: window.SiyabChecks.renderVerdict(panel, data, false, token), data};
                    }""", data)
                    self.assertTrue(result["rendered"])
                    self.assertEqual(result["data"], data)
                    panel = page.locator("#landing-check-result")
                    self.assertEqual(panel.locator(".verdict").inner_text(), expected)
                    self.assertEqual(panel.locator(".verdict").get_attribute("class"), "verdict " + css)
                    self.assertIn("Action: " + ("warn" if action == "ask" else action), panel.inner_text())
                    self.assertNotIn("ASK", panel.inner_text())
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    with page.expect_download() as download:
                        panel.get_by_role("button", name="Download evidence").click()
                    self.assertEqual(json.loads(Path(download.value.path()).read_text(encoding="utf-8")), data)

    def test_profile_warning_display_preserves_api_response(self):
        data = {"outcome": "ASK", "action": "ask", "verdict": None, "signing": "never",
                "evidence_hash": "a" * 64, "freshness": {"block_height": 1},
                "reasons": ["A check could not run."],
                "checks": [{"name": "Account age", "status": "unknown", "detail": "Unavailable."}]}
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route("**/*", self.local_route)
        context.route("**/live/verdict?*", lambda route: route.fulfill(content_type="application/json", body=json.dumps(data)))
        page = context.new_page()
        path = next(name for name in self.pages if name.startswith("accounts/"))
        for width in (1440, 375):
            with self.subTest(width=width):
                page.set_viewport_size({"width": width, "height": 900})
                page.goto(self.origin + "/" + path)
                with page.expect_response("**/live/verdict?*") as response:
                    page.locator("[data-profile-payment]").click()
                page.wait_for_function("document.querySelector('[data-profile-live-result]').getAttribute('aria-busy') === 'false'")
                self.assertEqual(response.value.json(), data)
                output = page.locator("[data-profile-live-result]")
                self.assertEqual(output.locator(".profile-outcome").inner_text(), "WARN")
                self.assertEqual(output.locator(".profile-outcome").get_attribute("class"), "profile-outcome ask")
                self.assertNotIn("ASK", output.inner_text())
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    def test_recorded_catch_is_in_the_first_viewport_without_scripts(self):
        for width, height in ((1366, 768), (390, 844)):
            with self.subTest(width=width):
                context = self.browser.new_context(viewport={"width": width, "height": height}, java_script_enabled=False)
                self.addCleanup(context.close)
                context.route("**/*", self.local_route)
                page = context.new_page()
                page.goto(self.origin + "/index.html")
                page.evaluate("document.fonts.ready")
                for selector in ("#landing-check h2", ".landing-check-reasons", "#landing-check-live"):
                    box = page.locator(selector).bounding_box()
                    self.assertGreaterEqual(box["y"], 0)
                    self.assertLessEqual(box["y"] + box["height"], height)

    def test_live_check_is_on_demand_and_retains_the_record_on_failure(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        requests = []
        def route(request):
            if "/live/account-check/" in request.request.url:
                requests.append(request.request.url)
                request.fulfill(status=503, content_type="application/json", body='{"message":"Unavailable"}')
            else:
                self.local_route(request)
        context.route("**/*", route)
        page = context.new_page()
        page.goto(self.origin + "/index.html")
        page.wait_for_load_state("networkidle")
        self.assertEqual(requests, [])
        original = page.locator("#landing-check-record").inner_text()
        page.locator("#landing-check-live").click()
        page.wait_for_function('document.getElementById("landing-check-status").textContent.includes("failed")')
        self.assertEqual(len(requests), 1)
        self.assertEqual(page.locator("#landing-check-record").inner_text(), original)
        self.assertFalse(page.locator("#landing-check-live").is_disabled())
        self.assertNotIn("ALLOW", page.locator("#landing-check-result").inner_text())
        self.assertIn("did not complete", page.locator("#landing-check-result").inner_text())
        subject = dict(render._flows())["rhea-collector"]["subject"]
        record = json.loads((render.ROOT / "fixtures/receivers/judgements" / (subject + ".json")).read_text(encoding="utf-8"))
        live = {"outcome": record["verdict"], "action": "refuse", "verdict": None, "signing": "never",
                "evidence_hash": "a" * 64, "freshness": record["observed"], "latency_ms": 1,
                "reasons": record["reasons"], "checks": [dict(name=check["id"], status=check["status"], detail=check["detail"])
                                                          for check in record["checks"]]}
        for status, content_type, body, expected in (
                (404, "text/html", "Unavailable", "failed"),
                (429, "application/json", '{}', "failed"),
                (0, "", "", "failed"),
                (200, "application/json", '{', "failed"),
                (200, "application/json", 'null', "failed"),
                (200, "application/json", '{"outcome":"ALLOW"}', "failed"),
                (200, "application/json", json.dumps(live), "returned")):
            with self.subTest(status=status, expected=expected):
                context.route("**/live/account-check/*", lambda request: request.fulfill(status=status, content_type=content_type, body=body)
                              if status else request.abort())
                page.locator("#landing-check-live").click()
                page.wait_for_function("text => document.getElementById('landing-check-status').textContent.includes(text)", arg=expected)
                self.assertEqual(page.locator("#landing-check-record").inner_text(), original)
                self.assertFalse(page.locator("#landing-check-live").is_disabled())
                if expected == "failed":
                    self.assertNotIn("ALLOW", page.locator("#landing-check-result").inner_text())
                    if status == 404:
                        self.assertIn("not available on this copy", page.locator("#landing-check-result").inner_text())
                    else:
                        self.assertIn("did not complete", page.locator("#landing-check-result").inner_text())
                        self.assertNotIn("not available on this copy", page.locator("#landing-check-result").inner_text())

    def test_live_check_handles_a_thrown_renderer_and_names_the_rendered_record(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.add_init_script("""Object.defineProperty(window, 'SiyabChecks', {
          configurable: true,
          set(value) {
            value.renderVerdict = () => { throw new Error('Renderer failed'); };
            Object.defineProperty(window, 'SiyabChecks', {value, configurable: true});
          }
        });""")
        def route(request):
            if request.request.url == self.origin + "/index.html":
                document = self.pages["index.html"].replace('class="verdict deny">DENY<', 'class="verdict deny">RECORDED TEST<', 1)
                request.fulfill(content_type="text/html", body=document)
            elif "/live/account-check/" in request.request.url:
                request.fulfill(content_type="application/json", body='{}')
            else:
                self.local_route(request)
        context.route("**/*", route)
        page = context.new_page()
        page.goto(self.origin + "/index.html")
        original = page.locator("#landing-check-record").inner_text()
        page.locator("#landing-check-live").click()
        page.wait_for_function('document.getElementById("landing-check-status").textContent.includes("failed")')
        self.assertIn("The recorded RECORDED TEST is retained above", page.locator("#landing-check-status").inner_text())
        self.assertFalse(page.locator("#landing-check-live").is_disabled())
        self.assertEqual(page.locator("#landing-check-record").inner_text(), original)
        self.assertNotIn("ALLOW", page.locator("#landing-check-result").inner_text())
        self.assertIn("did not complete", page.locator("#landing-check-result").inner_text())

    def test_live_check_aborts_a_route_that_never_responds(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        requests = []
        def route(request):
            if "/live/account-check/" in request.request.url:
                requests.append(request)
            else:
                self.local_route(request)
        context.route("**/*", route)
        page = context.new_page()
        page.goto(self.origin + "/index.html")
        original = page.locator("#landing-check-record").inner_text()
        page.evaluate("""() => {
          window.landingReadStarted = 0;
          window.landingReadAborted = 0;
          const originalFetch = window.fetch;
          window.fetch = (path, options) => {
            if (path.startsWith('/live/account-check/')) {
              window.landingReadStarted = performance.now();
              options.signal.addEventListener('abort', () => { window.landingReadAborted = performance.now(); });
            }
            return originalFetch(path, options);
          };
        }""")
        page.locator("#landing-check-live").click()
        page.wait_for_function('document.getElementById("landing-check-status").textContent.includes("failed")', timeout=14000)
        self.assertEqual(len(requests), 1)
        requests[0].abort()
        elapsed = page.evaluate("landingReadAborted - landingReadStarted")
        self.assertGreaterEqual(elapsed, 11900)
        self.assertLess(elapsed, 13000)
        self.assertFalse(page.locator("#landing-check-live").is_disabled())
        self.assertEqual(page.locator("#landing-check-record").inner_text(), original)
        self.assertNotIn("ALLOW", page.locator("#landing-check-result").inner_text())
        self.assertIn("did not complete", page.locator("#landing-check-result").inner_text())
        self.assertNotIn("not available on this copy", page.locator("#landing-check-result").inner_text())

    def test_both_themes_reflow_and_phone_targets_without_scripts(self):
        for width in (320, 390, 768, 1024, 1440):
            for theme in ("dark", "light"):
                with self.subTest(width=width, theme=theme):
                    context = self.browser.new_context(viewport={"width": width, "height": 900}, java_script_enabled=False)
                    self.addCleanup(context.close)
                    context.route("**/*", self.local_route)
                    page = context.new_page()
                    page.goto(self.origin + "/index.html")
                    page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                    page.evaluate("document.fonts.ready")
                    self.assertEqual(page.locator('.landing-label').count(), 8)
                    self.assertIn(COPY[0], re.sub(r'\s+', ' ', page.locator('main').inner_text()))
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    if width <= 390:
                        small = page.locator('main a, main button, main input, main summary, header a, header button, header summary').evaluate_all("nodes => nodes.filter(node => { const r = node.getBoundingClientRect(); return r.width && r.height && (r.width < 44 || r.height < 44); }).map(node => node.outerHTML)")
                        self.assertEqual(small, [])
                    context.close()

    def test_recorded_evidence_aligns_on_desktop_and_has_phone_labels(self):
        context = self.browser.new_context(reduced_motion="reduce")
        self.addCleanup(context.close)
        context.route("**/*", self.local_route)
        page = context.new_page()
        for width in (1024, 1440, 1920, 390):
            with self.subTest(width=width):
                page.set_viewport_size({"width": width, "height": 900})
                page.goto(self.origin + "/index.html")
                page.evaluate("document.fonts.ready")
                if width > 390:
                    centers = page.locator(".landing-node q, .landing-inspector > details > summary").evaluate_all("nodes => nodes.map(node => {const r=document.createRange();r.selectNodeContents(node);const b=r.getBoundingClientRect();return b.top+b.height/2})")
                    self.assertLessEqual(max(centers) - min(centers), 2, centers)
                else:
                    labels = page.locator(".landing-node, .landing-inspector").evaluate_all("nodes => nodes.map(node => getComputedStyle(node, '::before').content)")
                    self.assertEqual(labels, ['"From"', '"To"', '"To"', '"Recorded time"'])
                    positions = page.locator(".landing-node").evaluate_all("nodes => nodes.map(node => node.getBoundingClientRect().top)")
                    self.assertEqual(len(set(positions)), 3)

    def test_section_spacing_uses_the_shared_landing_token(self):
        context = self.browser.new_context(reduced_motion="reduce")
        self.addCleanup(context.close)
        context.route("**/*", self.local_route)
        page = context.new_page()
        for width, padding in ((1440, "64px"), (390, "36px")):
            with self.subTest(width=width):
                page.set_viewport_size({"width": width, "height": 900})
                page.goto(self.origin + "/index.html")
                values = page.locator(".landing-section").evaluate_all("nodes => nodes.map(node => {const s=getComputedStyle(node);return [s.paddingTop,s.paddingBottom]})")
                self.assertEqual(values, [[padding, padding]] * 7)

    def test_phone_transfer_rows_contain_their_evidence(self):
        for width in (320, 390):
            context = self.browser.new_context(viewport={"width": width, "height": 900})
            self.addCleanup(context.close)
            context.route("**/*", self.local_route)
            page = context.new_page()
            page.goto(self.origin + "/index.html")
            for expanded in (False, True):
                with self.subTest(width=width, expanded=expanded):
                    page.locator('.landing-transfers details').evaluate_all("nodes => nodes.forEach(node => node.open = " + str(expanded).lower() + ")")
                    rows = page.locator('.landing-transfers tbody tr').evaluate_all("nodes => nodes.map(node => ({bottom: node.getBoundingClientRect().bottom, cellBottom: Math.max(...Array.from(node.cells, cell => cell.getBoundingClientRect().bottom))}))")
                    self.assertTrue(all(row["bottom"] >= row["cellBottom"] for row in rows), rows)
            context.close()


if __name__ == "__main__":
    unittest.main()
