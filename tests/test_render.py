import contextlib
import hashlib
import html
import importlib.util
import io
import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from intel import __main__ as intel_main
from intel import cluster, feed, issuers, labels, records, render, trace

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "demo" / "site"
ROBOTS = '<meta name="robots" content="noindex">\n'
# COMMON rule 3: the hygiene phrases plus the words ShieldBot never writes in its own text.
EXTRA_WORDS = re.compile(r"go to|guarantee|secure|safe|\bshield\b", re.IGNORECASE)
BARRED = re.compile(r"scam|clone|fake|drainer", re.IGNORECASE)
DASHES = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")
QUOTED = re.compile(r'<blockquote class="data"[^>]*>.*?</blockquote>|<q class="data">.*?</q>', re.DOTALL)
LINK = re.compile(r'(?:href|src)="([^"]*)"')


def page_name(key):
    """A page name as the render names it, computed here apart from intel/render.py."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


def load_hygiene():
    spec = importlib.util.spec_from_file_location("check_hygiene", ROOT / "scripts" / "check_hygiene.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PHRASES = load_hygiene().PHRASES


def forbidden(text):
    return PHRASES.search(text) or EXTRA_WORDS.search(text)


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def make_row(**fields):
    """A valid label store row: an onchain infrastructure row unless fields say otherwise."""
    row = {"entity": "test entity", "subject": "relay.example.near", "chain": "near", "label": "a relayer contract",
           "category": "infrastructure", "provenance": "onchain",
           "source": {"rpc": "https://free.rpc.fastnear.com", "method": "view_account",
                      "args": {"account_id": "relay.example.near"}, "block": 1},
           "observed_at": "2026-11-03T00:00:00Z", "confidence": 1, "rule_id": None, "superseded_by": None,
           "note": "a test row"}
    row.update(fields)
    row["id"] = labels.assertion_id(row)
    return labels.validate(row)


def own_text(page):
    """The visible text of a page with every quoted source string removed."""
    text = QUOTED.sub(" ", page)
    text = re.sub(r"<style>.*?</style>", " ", text, flags=re.DOTALL)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


class RecordFieldParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}
        self.path = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        path = dict(attrs).get("data-record-path")
        if path is not None:
            self.path = path
            self.parts = []

    def handle_data(self, data):
        if self.path is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "span" and self.path is not None:
            self.values[self.path] = "".join(self.parts)
            self.path = None
            self.parts = []


def record_values(value, path=""):
    if isinstance(value, dict):
        if not value:
            return {path: "{}"}
        return {child_path: text for key, child in value.items()
                for child_path, text in record_values(child, f"{path}.{key}" if path else key).items()}
    if isinstance(value, list):
        if not value:
            return {path: "[]"}
        return {child_path: text for index, child in enumerate(value)
                for child_path, text in record_values(child, f"{path}[{index}]").items()}
    if value is None:
        return {path: "null"}
    if isinstance(value, bool):
        return {path: "true" if value else "false"}
    return {path: str(value)}


def write_decision_record(directory, text):
    source = next(path for path in sorted((ROOT / "fixtures" / "records").glob("*.json"))
                  if path.name != "body-vector.json" and records.read(path)["supersedes"] is None)
    record = records.read(source)
    record.pop("record_id")
    record["reports"] = [{"network": "testnet", "contract": "reports.synthetic.testnet", "report_id": 0,
                          "body_sha256": "b" * 64}]
    record["reasons"] = [{"report_id": 0, "dispute_id": None,
                          "reason_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}]
    record["timeline"] = [
        {"at": "2026-11-07T05:17:40Z", "event": "moderated",
         "source": {"type": "chain", "network": "testnet", "block": 1,
                    "transaction": "synthetic-moderation-transaction"}},
        {"at": "2026-11-07T05:17:45Z", "event": "recorded", "source": {"type": "record"}},
    ]
    return records.write(record, directory)


class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            cls.written = render.render(cls.tmp.name)
        cls.out = Path(cls.tmp.name)
        cls.pages = {name: (cls.out / name).read_text(encoding="utf-8") for name in cls.written if name.endswith(".html")}
        cls.subjects = {a.subject for a in labels.load().assertions if a.superseded_by is None}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def linked(self, subject):
        """The escaped subject, linked to its account page when it has a current row."""
        text = html.escape(subject)
        return f'<a href="../accounts/{page_name(subject)}.html">{text}</a>' if subject in self.subjects else text

    def test_every_page_is_written(self):
        incidents = [p.stem for p in (ROOT / "intel" / "incidents").glob("*.json")]
        flows = [p.stem for p in (ROOT / "intel" / "flows").glob("*.json")]
        clusters = [p.stem for p in (ROOT / "intel" / "clusters").glob("*.json")]
        record_documents = [records.read(path) for path in sorted((ROOT / "fixtures" / "records").glob("*.json"))
                            if path.name != "body-vector.json"]
        entities = [json.loads(line)["id"] for line in (ROOT / "intel" / "entities.jsonl").read_text(
            encoding="utf-8").splitlines()]
        expected = (["index.html", "feed.html", "verify.html", "404.html", "issuers.html"]
                    + ["top-receivers.html"]
                    + [f"incidents/{i}.html" for i in incidents]
                    + [f"entities/{name}.html" for name, _, _ in render.ENTITY_SETS] + [f"flows/{f}.html" for f in flows]
                    + [f"clusters/{c}.html" for c in clusters]
                    + ["records/index.html"] + [f"records/{record['record_id']}.html" for record in record_documents]
                    + [f"entities/{page_name(entity)}.html" for entity in entities]
                    + [f"accounts/{page_name(subject)}.html" for subject in self.subjects]
                    + ["search.json", "style.css"])
        self.assertEqual(sorted(self.written), sorted(expected))

    def test_record_pages_show_each_stored_field_value(self):
        paths = sorted((ROOT / "fixtures" / "records").glob("*.json"))
        index = self.pages["records/index.html"]
        for path in (path for path in paths if path.name != "body-vector.json"):
            record = records.read(path)
            page = self.pages[f"records/{record['record_id']}.html"]
            parser = RecordFieldParser()
            parser.feed(page)
            with self.subTest(record=record["record_id"]):
                self.assertEqual(parser.values, record_values(record))
                for field in record:
                    self.assertIn(f"<dt>{field}</dt>", page)
                self.assertIn("python -m intel record-check", page)
                self.assertIn(f"fixtures/records/{record['record_id']}.json", page)
                self.assertIn(f'href="{record["record_id"]}.html">{record["record_id"]}</a>', index)
        self.assertIn('href="records/index.html"', self.pages["index.html"])

    def test_committed_record_pages_render_identically_twice(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("urllib.request.urlopen", side_effect=no_network):
            names = [name for name in render.render(tmp) if name.startswith("records/")]
            first = {name: (Path(tmp) / name).read_bytes() for name in names}
            again = [name for name in render.render(tmp) if name.startswith("records/")]
            second = {name: (Path(tmp) / name).read_bytes() for name in again}
        self.assertEqual(again, names)
        self.assertEqual(second, first)

    def test_record_text_is_escaped_and_keeps_the_page_inert(self):
        text = '<img src=x onerror="alert(1)"> & source text'
        with tempfile.TemporaryDirectory() as tmp, mock.patch("urllib.request.urlopen", side_effect=no_network):
            records_dir = Path(tmp) / "records"
            record_path = write_decision_record(records_dir, text)
            with mock.patch.object(render, "RECORDS_DIR", records_dir):
                written = render.render(Path(tmp) / "site")
            record = records.read(record_path)
            page = (Path(tmp) / "site" / f"records/{record_path.stem}.html").read_text(encoding="utf-8")
        self.assertIn(html.escape(text), page)
        self.assertNotIn(text, page)
        self.assertNotIn("<script", page)
        self.assertIn(f"records/{record_path.stem}.html", written)
        parser = RecordFieldParser()
        parser.feed(page)
        self.assertEqual(parser.values, record_values(record))

    def test_invalid_record_is_skipped_with_its_reason(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("urllib.request.urlopen", side_effect=no_network):
            records_dir = Path(tmp) / "records"
            good_path = write_decision_record(records_dir, "Synthetic moderation reason.")
            invalid_path = records_dir / "invalid.json"
            invalid_path.write_text("{}", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(render, "RECORDS_DIR", records_dir), contextlib.redirect_stdout(output):
                written = render.render(Path(tmp) / "site")
            site = Path(tmp) / "site"
        self.assertIn(f"skipped incident record {invalid_path}", output.getvalue())
        self.assertIn("record: exactly the fields", output.getvalue())
        self.assertIn(f"records/{good_path.stem}.html", written)
        self.assertNotIn("records/invalid.html", written)
        self.assertFalse((site / "records/invalid.html").exists())

    def test_records_build_when_network_access_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("urllib.request.urlopen", side_effect=no_network):
            written = render.render(tmp)
        self.assertIn("records/index.html", written)

    def test_search_index_covers_every_subject_and_entity(self):
        search = json.loads((self.out / "search.json").read_text(encoding="utf-8"))
        store = labels.load()
        current_subjects = {a.subject for a in store.assertions if a.superseded_by is None}
        self.assertEqual({entry["subject"] for entry in search["accounts"]}, current_subjects)
        entities = [json.loads(line) for line in (ROOT / "intel" / "entities.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({entry["id"] for entry in search["entities"]}, {entity["id"] for entity in entities})
        self.assertIn("account-form", self.pages["index.html"])
        self.assertIn("<script>", self.pages["index.html"])
        self.assertNotIn("<script src", self.pages["index.html"])
        for entry in search["accounts"]:
            with self.subTest(subject=entry["subject"]):
                self.assertTrue(entry["label_sources"])
                self.assertTrue(all(row["source"] for row in entry["label_sources"]))

    def test_index_has_the_account_search_tools_and_proven_run_logs(self):
        page = self.pages["index.html"]
        for text in ("Paste any NEAR account", "Can I pay this?", "Is this account safe to use?",
                     "Where is this swap really going?", "Proven on testnet", "Not in the label store."):
            with self.subTest(text=text):
                self.assertIn(text, page)
        for path in ("fixtures/runs/live-beat-20261108T111313Z.txt",
                     "fixtures/runs/kill-switch-drill-2026-11-08.txt",
                     "fixtures/runs/recorder-rotation-2026-11-08.txt",
                     "fixtures/runs/registry-readback-20261107.txt"):
            with self.subTest(path=path):
                self.assertIn(path, page)
                self.assertIn(f"{render.GITHUB_REPO}/blob/main/{path}", page)
        self.assertIn('href="top-receivers.html"', page)
        self.assertIn("One team moderator today", page)

    def test_generated_index_shows_sources_and_fails_live_checks_closed(self):
        page = self.pages["index.html"]
        search = json.loads((self.out / "search.json").read_text(encoding="utf-8"))
        entry = search["accounts"][0]
        self.assertTrue(entry["label_sources"])
        self.assertTrue(entry["label_sources"][0]["source"])
        self.assertIn("label_sources", page)
        self.assertIn("source.url.startsWith(\"https://\")", page)
        self.assertIn("link.href = `/${entry.url}`", page)
        self.assertIn('const liveUnavailable = "Live checks are not available on this copy"', page)
        self.assertIn("response.status === 404", page)
        self.assertIn('!contentType.includes("application/json")', page)
        self.assertIn("catch (error)", page)
        self.assertIn('data.outcome', page)
        self.assertIn('"ALLOW", "ASK", "DENY"', page)
        self.assertIn('["pass", "signal", "unknown"].includes(check.status.trim().toLowerCase())', page)
        self.assertIn('data.outcome !== "ALLOW" || data.checks.every(check => check.status.trim().toLowerCase() === "pass")', page)

    def test_allow_ask_requires_a_person_and_uses_neutral_style(self):
        page = self.pages["index.html"]
        is_green = page[page.index("const isGreen ="):page.index("const displayOutcome =")]
        self.assertIn('data.outcome === "ALLOW" && data.action === "sign"', is_green)
        self.assertIn("data.evidence_hash.trim().length > 0", is_green)
        self.assertIn("data.freshness !== null", is_green)
        self.assertIn('data.checks.every(check => check.status.trim().toLowerCase() === "pass")', page)
        self.assertIn('const displayOutcome = data.outcome === "ALLOW" && !isGreen ? "ASK" : data.outcome;', page)
        self.assertIn('`verdict ${isGreen ? "allow" : displayOutcome.toLowerCase()}`', page)
        self.assertIn('if (data.outcome === "ALLOW" && data.action === "ask")', page)
        self.assertIn("A person must decide before this payment can proceed.", page)

    def test_live_panel_requests_ignore_stale_responses(self):
        page = self.pages["index.html"]
        self.assertIn("const requestTokens = new WeakMap();", page)
        self.assertIn("const beginRequest = panel => {", page)
        self.assertIn("const isCurrentRequest = (panel, token) => requestTokens.get(panel) === token;", page)
        self.assertIn("if (!isCurrentRequest(panel, token)) return null;", page)
        self.assertIn("readJson(verdictUrl, verdictPanel, verdictToken)", page)
        self.assertIn("readJson(`/live/scan/${encodeURIComponent(subject)}`, scanPanel, scanToken)", page)
        self.assertIn("if (verdict && isCurrentRequest(verdictPanel, verdictToken))", page)
        self.assertIn("if (scan && isCurrentRequest(scanPanel, scanToken))", page)
        self.assertIn("readJson(route(values), panel, requestToken)", page)
        self.assertIn("readJson(`/live/scan/${encodeURIComponent(subject)}`, panel, requestToken)", page)
        self.assertIn('readJson("/live/reports/recent", recentPanel, recentToken)', page)
        self.assertIn("readJson(`/live/reports?${new URLSearchParams({subject})}`, panel, requestToken)", page)

    def test_rendered_text_and_fill_pairs_meet_contrast_in_both_themes(self):
        stylesheet = (self.out / "style.css").read_text(encoding="utf-8")
        self.assertIn("a:hover{color:rgb(var(--accent))}", stylesheet)
        self.assertIn(".lookup-card button:hover,.primary:hover{color:white}", stylesheet)
        self.assertNotRegex(stylesheet, r"(?<!border-)color:rgb\(var\(--accent-fill\)\)")
        theme_blocks = (
            ("dark", re.search(r":root\{(.*?)\}", stylesheet, re.DOTALL).group(1)),
            ("light", re.search(r':root\[data-theme="light"\]\{(.*?)\}', stylesheet, re.DOTALL).group(1)),
        )
        text_surfaces = {
            "--ink": ("--ground", "--panel", "--ground-alt"),
            "--secondary": ("--ground", "--panel", "--ground-alt"),
            "--accent": ("--ground", "--panel"),
            "--allow-text": ("--ground", "--panel"),
            "--unknown": ("--ground", "--panel"),
            "--block-text": ("--ground", "--panel"),
        }

        def luminance(rgb):
            channels = []
            for channel in rgb:
                value = channel / 255
                channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
            return sum(weight * channel for weight, channel in zip((0.2126, 0.7152, 0.0722), channels))

        def contrast(foreground, background):
            high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
            return (high + 0.05) / (low + 0.05)

        for theme, block in theme_blocks:
            colors = {name: tuple(map(int, value.split()))
                      for name, value in re.findall(r"(--[\w-]+):(\d+ \d+ \d+)", block)}
            for foreground, surfaces in text_surfaces.items():
                for surface in surfaces:
                    with self.subTest(theme=theme, foreground=foreground, surface=surface):
                        self.assertGreaterEqual(contrast(colors[foreground], colors[surface]), 4.5)
            with self.subTest(theme=theme, foreground="white", surface="--accent-fill"):
                self.assertGreaterEqual(contrast((255, 255, 255), colors["--accent-fill"]), 4.5)

    def test_index_has_no_unexpected_host_or_remote_script_or_font(self):
        page = self.pages["index.html"]
        stylesheet = (self.out / "style.css").read_text(encoding="utf-8")
        urls = [link for link in LINK.findall(page) if link.startswith(("http://", "https://"))]
        self.assertTrue(urls)
        self.assertTrue(all(url.startswith(f"{render.GITHUB_REPO}/blob/main/") for url in urls))
        self.assertNotRegex(page, r"<script[^>]+src=")
        self.assertNotRegex(page, r"<link[^>]+(?:Geist|Plex|fonts\.googleapis)")
        self.assertNotIn("@import", stylesheet)
        self.assertNotRegex(stylesheet, r"url\(\s*['\"]?https?://")
        self.assertIn("--ground:11 11 10", stylesheet)
        self.assertIn('id="theme-toggle"', page)

    def test_top_receivers_page_has_an_honest_empty_state_and_renders_judgements(self):
        with tempfile.TemporaryDirectory() as tmp:
            judgement_dir = Path(tmp) / "judgements"
            empty = render.top_receivers_page(judgement_dir, Path(tmp) / "ranking.json")
            self.assertIn("Scan in progress, not yet published.", empty)
            judgement_dir.mkdir()
            (judgement_dir / "receiver.near.json").write_text(json.dumps({
                "subject": "receiver.near", "verdict": "DENY", "reasons": ["A cited reason."],
            }), encoding="utf-8")
            populated = render.top_receivers_page(judgement_dir, Path(tmp) / "ranking.json")
        self.assertIn("receiver.near", populated)
        self.assertIn("DENY", populated)
        self.assertIn("A cited reason.", populated)

    def test_every_search_link_is_a_page_inside_the_site(self):
        search = json.loads((self.out / "search.json").read_text(encoding="utf-8"))
        root = self.out.resolve()
        for entry, key, folder in ([(entry, entry["subject"], "accounts") for entry in search["accounts"]]
                                   + [(entry, entry["id"], "entities") for entry in search["entities"]]):
            with self.subTest(url=entry["url"]):
                self.assertEqual(entry["url"], f"{folder}/{page_name(key)}.html")
                target = (self.out / entry["url"]).resolve()
                self.assertIn(root, target.parents)
                self.assertTrue(target.is_file())

    def test_pages_that_name_a_subject_link_its_account_page(self):
        document = json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_text(encoding="utf-8"))
        named = [account["subject"] for account in document["accounts"] if account["subject"] in self.subjects]
        self.assertTrue(named)
        for subject in named:
            self.assertIn(self.linked(subject), self.pages["incidents/rhea.html"])
        store = labels.load()
        for name, _, categories in render.ENTITY_SETS:
            for row in (a for a in store.assertions if a.category in categories and a.superseded_by is None):
                with self.subTest(set=name, subject=row.subject):
                    self.assertIn(f'<a href="../accounts/{page_name(row.subject)}.html"><q class="data">'
                                  f'{html.escape(row.subject)}</q></a>', self.pages[f"entities/{name}.html"])
        entity = json.loads((ROOT / "intel" / "entities.jsonl").read_text(encoding="utf-8").splitlines()[0])
        member = entity["members"][0]
        self.assertIn(member, self.subjects)
        self.assertIn(f'<a href="../accounts/{page_name(member)}.html"><q class="data">{html.escape(member)}</q></a>',
                      self.pages[f"entities/{page_name(entity['id'])}.html"])

    def test_every_page_is_noindex_with_the_footer_and_no_external_script(self):
        for name, page in self.pages.items():
            with self.subTest(page=name):
                self.assertTrue(page.startswith("<!doctype html>"))
                self.assertIn('<meta name="robots" content="noindex">', page)
                self.assertIn("Data: NearBlocks", page)
                self.assertNotIn("<script src", page)
                self.assertIn('rel="stylesheet" href="/style.css"', page)
                self.assertNotIn("<style>", page)

    def test_no_forbidden_or_barred_word_outside_quoted_source_data(self):
        for name, page in self.pages.items():
            with self.subTest(page=name):
                text = own_text(page)
                if name == "index.html":
                    text = text.replace(render.HEADLINE, "")
                    text = text.replace("Is this account safe to use?", "")
                self.assertIsNone(forbidden(text), forbidden(text))
                self.assertIsNone(BARRED.search(text), BARRED.search(text))
                self.assertIsNone(DASHES.search(page))

    def test_the_word_check_sees_authored_text(self):
        self.assertTrue(forbidden(own_text('<p>this is safe</p><q class="data">x</q>')))
        self.assertIsNone(forbidden(own_text('<p>symbol <q class="data">SAFE</q></p>')))
        self.assertTrue(forbidden(own_text("<p>" + "trust" + "less</p>")))
        self.assertTrue(BARRED.search(own_text('<blockquote class="other">a fake</blockquote>')))

    def test_an_escaped_quote_stays_inside_the_quoted_data(self):
        bs = chr(92)
        source = f'ft_metadata fails on the contract: "wasm {bs}"panicked{bs}" at {bs}{bs}src" and our words'
        marked = render.data_text(source)
        self.assertEqual(marked, 'ft_metadata fails on the contract: <q class="data">wasm &quot;panicked&quot; at '
                         + bs + "src</q> and our words")
        self.assertEqual(own_text(marked).split(), ["ft_metadata", "fails", "on", "the", "contract:", "and", "our",
                                                    "words"])

    def test_no_escaped_source_text_leaks_into_the_own_text_of_an_incident_page(self):
        for name, page in self.pages.items():
            if name.startswith("incidents/"):
                with self.subTest(page=name):
                    self.assertNotIn(chr(92), own_text(page))

    def test_every_local_link_resolves_inside_the_site(self):
        root = self.out.resolve()
        for name, page in self.pages.items():
            for link in LINK.findall(page):
                if link.startswith("https://"):
                    continue
                with self.subTest(page=name, link=link):
                    # A root absolute href resolves against the site root, any other against the page's directory.
                    rooted = link.startswith("/") and not link.startswith("//")
                    base = self.out if rooted else (self.out / name).parent
                    target = (base / html.unescape(link[1:] if rooted else link)).resolve()
                    self.assertIn(root, target.parents)
                    self.assertTrue(target.is_file())

    def test_the_index_carries_the_counts_lines(self):
        for line in render._counts_lines():
            self.assertIn(f"<p>{html.escape(line)}</p>", self.pages["index.html"])

    def test_the_index_says_what_the_counts_are_of_between_the_h1_and_the_counts_card(self):
        page = self.pages["index.html"]
        sentence = f"<p>{html.escape(render.COUNTS_OF)}</p>"
        self.assertLess(page.index("</h1>"), page.index(sentence))
        self.assertLess(page.index(sentence), page.index('<div class="card">'))
        tier2 = (ROOT / "fixtures" / "latency" / "verdict-runs-tier2.json").is_file()
        fallback = (ROOT / "fixtures" / "latency" / "verdict-runs-tier2-fallback.json").is_file()
        self.assertEqual(len(render._counts_lines()), 7 + tier2 + fallback)
        for words in ("recorded mainnet receivers the rules judged", "by cohort", "label store", "provenance class",
                      "incident pages", "feed", "exit traces", "verdict latency runs", "payee off the allowlist"):
            self.assertIn(words, render.COUNTS_OF)

    def test_the_feed_page_names_a_recorded_window_with_its_date_and_the_follower_snapshot(self):
        state = json.loads(feed.STATE_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("mode", state)
        window = (f"<p>Recorded window: blocks {state['start_height']} to {state['last_height']}, read on "
                  f"{state['start_time'][:10]} UTC.")
        snapshot = json.loads(feed.SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertIn(f"{window} The follower snapshot <code>intel/feed/follow-snapshot.json</code> covers "
                      f"{snapshot['blocks']} blocks from height {snapshot['start_height']} to height "
                      f"{snapshot['last_height']}.</p>", self.pages["feed.html"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            following = dict(state, mode="follow", started_at=state["start_time"], lag_blocks=0, requests=0,
                             request_day=state["start_time"][:10])
            path.write_text(json.dumps(following), encoding="utf-8")
            page = render.feed_page(path, Path(tmp) / "events")
            with mock.patch.object(feed, "SNAPSHOT_PATH", Path(tmp) / "missing.json"):
                self.assertIn(f"{window}</p>", render.feed_page())
        self.assertNotIn("Recorded window", page)
        self.assertNotIn("follower snapshot", page)
        for line in feed.stats_lines(following):
            self.assertIn(f"<li>{html.escape(line)}</li>", page)
        event = {"signal": "new receiver", "observed": {"block_height": 123}, "detail": "follow event",
                 "receipt_id": "receipt", "tx_hash": "transaction"}
        supplied = render.feed_page(state=following, event_rows=[(event, "event-id")])
        for line in feed.stats_lines(following):
            self.assertIn(f"<li>{html.escape(line)}</li>", supplied)
        self.assertIn("follow event", supplied)
        self.assertIn("at block 123", supplied)

    def test_the_feed_page_renders_several_events_from_disk_by_block_height_then_hash(self):
        state = json.loads(feed.STATE_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path, events_dir = Path(tmp) / "state.json", Path(tmp) / "events"
            path.write_text(json.dumps(state), encoding="utf-8")
            events_dir.mkdir()
            for stem, height in (("d", 20), ("b", 20), ("a", 30), ("c", 10)):
                event = {"signal": "new receiver", "observed": {"block_height": height}, "detail": f"event {stem}",
                         "receipt_id": "receipt", "tx_hash": "transaction"}
                (events_dir / f"{stem}.json").write_text(json.dumps(event), encoding="utf-8")
            page = render.feed_page(path, events_dir)
        self.assertEqual(page.count('<div class="card">'), 4)
        positions = [page.index(f"<p>event {stem}</p>") for stem in "cbda"]
        self.assertEqual(positions, sorted(positions))

    def test_the_feed_page_states_the_total_the_events_were_selected_from(self):
        state = dict(json.loads(feed.STATE_PATH.read_text(encoding="utf-8")), mode="follow",
                     started_at="2026-11-03T12:00:00Z", lag_blocks=0)
        rows = [({"signal": "new receiver", "observed": {"block_height": height}, "detail": "event",
                  "receipt_id": "receipt", "tx_hash": "transaction"}, f"event-{height}") for height in (1, 2, 3)]
        self.assertIn("<p>3 events in total.</p>", render.feed_page(state=state, event_rows=rows, total=3))
        self.assertIn("<p>1 event in total.</p>", render.feed_page(state=state, event_rows=rows[2:], total=1))
        self.assertIn("<p>7 events in total; the 3 most recent by block height are shown.</p>",
                      render.feed_page(state=state, event_rows=rows, total=7))
        self.assertNotIn("in total", render.feed_page(state=state, event_rows=rows))

    def test_an_event_card_renders_each_field_its_document_lacks_as_unknown(self):
        state = dict(json.loads(feed.STATE_PATH.read_text(encoding="utf-8")), mode="follow",
                     started_at="2026-11-03T12:00:00Z", lag_blocks=0)
        transfer = {"signal": "labelled_account_large_transfer", "observed": {"block_height": 77},
                    "detail": "transfer event", "receipt_id": "receipt", "transaction_id": "transaction",
                    "usd_amount": "1500", "direction": "outbound", "subject": "subject.near",
                    "other_account_id": "other.near", "asset_id": "nep141:wrap.near", "block_height": 77}
        page = render.feed_page(state=state, event_rows=[(transfer, "event-77"), ({"tx_hash": None}, "event-empty")],
                                total=2)
        self.assertIn("<strong>labelled_account_large_transfer</strong> at block 77</p><p>transfer event</p>"
                      '<p class="mono">receipt receipt<br>transaction unknown<br>evidence event-77</p>', page)
        self.assertIn("<strong>unknown</strong> at block unknown</p><p>unknown</p>"
                      '<p class="mono">receipt unknown<br>transaction unknown<br>evidence event-empty</p>', page)

    def test_the_static_feed_page_skips_and_counts_an_unreadable_event_file(self):
        state = json.loads(feed.STATE_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            path, events_dir = Path(tmp) / "state.json", Path(tmp) / "events"
            path.write_text(json.dumps(state), encoding="utf-8")
            events_dir.mkdir()
            (events_dir / "torn.json").write_text('{"signal": "new rec', encoding="utf-8")
            page = render.feed_page(path, events_dir)
        self.assertIn("<p>1 event file could not be read.</p>", page)
        self.assertNotIn("torn", page)
        self.assertNotIn("No signal fired", page)

    def test_the_index_headline_is_the_requested_account_sentence(self):
        self.assertIn(f'<h1 id="page-title">{html.escape(render.HEADLINE)}</h1>', self.pages["index.html"])
        self.assertIn("checks whether an account is safe to pay", self.pages["index.html"])

    def test_the_verify_page_inputs_carry_placeholders_from_committed_records(self):
        record = json.loads((ROOT / "fixtures" / "registry" / "testnet-latest-kestrelridge-shop.json")
                            .read_text(encoding="utf-8"))
        evidence_path = ROOT / "fixtures" / "evidence" / f"{record['evidence_hash']}.json"
        self.assertEqual(hashlib.sha256(evidence_path.read_bytes()).hexdigest(), record["evidence_hash"])
        evidence = json.loads(evidence_path.read_bytes())
        testnet = (ROOT / "docs" / "TESTNET.md").read_text(encoding="utf-8")
        registry = re.search(r"^\| Registry \| `([^`]+)` \|", testnet, re.MULTILINE).group(1)
        page = self.pages["verify.html"]
        for input_id, value in (("registry", registry), ("subject", evidence["subject"]["receiver"]),
                                ("hash", record["evidence_hash"])):
            with self.subTest(input=input_id):
                self.assertIn(f'<input id="{input_id}" autocomplete="off" placeholder="{html.escape(value)}">', page)
        self.assertIn("<code>fixtures/registry/testnet-latest-kestrelridge-shop.json</code>", page)

    def test_the_incident_page_shows_every_part(self):
        document = json.loads((ROOT / "intel" / "incidents" / "rhea.json").read_text(encoding="utf-8"))
        page = self.pages["incidents/rhea.html"]
        for source in document["sources"]:
            self.assertIn(source["content_sha256"], page)
        for account in document["accounts"]:
            self.assertIn(account["subject"], page)
            self.assertIn(html.escape(account["quote"]["text"]), page)
        for item in document["not_public"]:
            self.assertIn(html.escape(item), page)
        for entry in document["rules_today"]:
            self.assertIn(f"{self.linked(entry['subject'])} as {entry['as']}", page)
        self.assertIn(document["code_hash_as_reported"]["value"], page)

    def test_the_flow_page_has_the_summary_the_shares_and_one_card_per_hop(self):
        document = json.loads((ROOT / "intel" / "flows" / "rhea-collector.json").read_text(encoding="utf-8"))
        page = self.pages["flows/rhea-collector.html"]
        self.assertEqual(page.count('<div class="card">'), len(document["hops"]))
        summary = page.index('<div class="card summary">')
        self.assertLess(summary, page.index("<table>"))
        self.assertLess(page.index("<table>"), page.index('<div class="card state">'))
        self.assertLess(page.index('<div class="card state">'), page.index('<div class="card">'))
        self.assertIn(render.data_text(trace.summary(document)), page)
        self.assertIn(f"stopped while reading {document['completeness']['account']} (1 hop from the subject): the page "
                      "budget of 150 requests", page)
        self.assertIn("<h2>Shares of the gross outbound transfer value</h2>", page)
        self.assertIn("<p>Shares are of the subject's gross outbound transfer value; deeper hops are shown and not re "
                      f"summed.</p><p>{html.escape(trace.gross_sentence(document))}</p>", page)
        self.assertIn("older outbound rows are outside it", page)
        for bucket in document["shares"]["buckets"]:
            self.assertIn(f'<td class="mono">{self.linked(bucket["to"])}</td><td>{html.escape(bucket["kind"])}</td>'
                          f'<td>{trace.usd_text(bucket["usd"])}</td><td>{bucket["count"]}</td><td>{bucket["percent"]}</td>', page)
        self.assertIn('banned by the issuer, get_blacklist_status answers <q class="data">Banned</q>', page)
        for read in document["state_at_block"]:
            self.assertIn(f"{read['method']} at block {read['block']}", page)
        depths = trace.hop_depths(document)
        for depth in sorted(set(depths)):
            self.assertIn(f"<h2>Depth {depth}: {depths.count(depth)} hops</h2>", page)
        self.assertNotIn("hops to depth", page)
        for hop in document["hops"]:
            self.assertIn(hop["transaction_hash"], page)
            self.assertIn(f"block {hop['block']}", page)
        self.assertIn(f"depth {document['depth_reached']} of {document['depth']}, "
                      f"{trace.stop_state(document['completeness'])}", self.pages["index.html"])

    def test_the_cluster_page_shows_every_edge_with_its_provenance_and_the_hypothesis_heading_for_inferred_edges(self):
        paths = sorted((ROOT / "intel" / "clusters").glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            document = json.loads(path.read_text(encoding="utf-8"))
            page = self.pages[f"clusters/{path.stem}.html"]
            facts = {fact["id"]: fact for fact in document["facts"]}
            chain = [edge for edge in document["edges"] if edge["provenance"] != "inferred"]
            inferred = [edge for edge in document["edges"] if edge["provenance"] == "inferred"]
            with self.subTest(cluster=path.stem):
                self.assertTrue(chain and inferred)
                heading = page.index("<h2>Attribution hypothesis</h2>")
                self.assertEqual(page[:heading].count('<div class="card edge">'), len(chain))
                self.assertEqual(page[heading:].count('<div class="card edge">'), len(inferred))
                cards = page.split('<div class="card edge">')[1:]
                for edge, card in zip(chain + inferred, cards):
                    self.assertIn(f"<strong>{edge['rule']}</strong>, provenance {edge['provenance']}", card)
                    self.assertIn(f'<p class="mono">{self.linked(edge["from"])} to {self.linked(edge["to"])}</p>', card)
                for edge, card in zip(chain, cards):
                    for side in ("from", "to"):
                        read = edge["source"][side]
                        args = html.escape(json.dumps(read["args"], ensure_ascii=False))
                        self.assertIn(f"{read['method']} {args} at block {read['block']} from {read['rpc']}", card)
                for edge, card in zip(inferred, cards[len(chain):]):
                    self.assertIn(f"confidence {edge['confidence']}", card)
                    for fact_id in edge["inputs"]:
                        self.assertIn(f"fact {fact_id}", card)
                        self.assertIn(f"at block {facts[fact_id]['source']['block']}", card)
                precision = page.index("<h2>Precision</h2>")
                self.assertLess(heading, precision)
                for line in document["precision"].values():
                    self.assertIn(f"<li>{html.escape(line)}</li>", page[precision:])
                self.assertLess(page.index("<h2>Seeds</h2>"), page.index('<div class="card edge">'))
                for seed in document["seeds"]:
                    self.assertIn(f'<li class="mono">{self.linked(seed)}</li>', page)
                depositors = document["depositors"]
                self.assertIn(f"{depositors['count']} distinct depositors of", page)
                self.assertIn(f"{depositors['implicit_count']} of them implicit accounts", page)
                self.assertIn(f'href="clusters/{path.stem}.html"', self.pages["index.html"])

    def test_the_issuer_page_lists_every_action_with_its_block(self):
        page = self.pages["issuers.html"]
        status = labels.load(issuers.STATUS_PATH).assertions
        history = labels.load(issuers.HISTORY_PATH).assertions
        self.assertTrue(status and history)
        status_part = page[page.index("<h2>Banned by the issuer</h2>"):page.index("<h2>Issuer action history</h2>")]
        history_part = page[page.index("<h2>Issuer action history</h2>"):]
        for row in status:
            with self.subTest(status=row.id):
                card = status_part[status_part.index(f'<p class="mono">{html.escape(row.subject)}</p>'):]
                card = card[:card.index("</div>")]
                self.assertIn(render.data_text(row.label), card)
                self.assertIn(f"at block {row.source['block']}", card)
                self.assertIn(f"row {row.id}", card)
        for row in history:
            with self.subTest(history=row.id):
                card = history_part[history_part.index(f"row {row.id}") - 400:history_part.index(f"row {row.id}")]
                transaction = row.source["args"]["tx_hashes"][0]
                self.assertIn(f"block {row.source['block']}<br>transaction {transaction}", card)
                self.assertIn(render.data_text(row.label), history_part)
                self.assertIn(render.data_text(row.note), history_part)
        coverage = issuers.history_coverage()
        actions = coverage["actions"]
        self.assertEqual(coverage["transactions"], coverage["txs_count"])
        self.assertIn(f"All {coverage['transactions']} function call transactions the Transactions API lists for "
                      f'<q class="data">{html.escape(issuers._owner(issuers.FIXTURE_DIR))}</q>', history_part)
        self.assertIn(f"as receiver, transaction blocks {coverage['tx_block_min']} to {coverage['tx_block_max']}, were "
                      f"read; their receipts carried {sum(actions.values())} successful issuer actions on "
                      f"usdt.tether-token.near: {actions['add_to_blacklist']} add_to_blacklist and "
                      f"{actions['destroy_black_funds']} destroy_black_funds, no remove_from_blacklist.", history_part)
        observations = issuers._account_observations(issuers.FIXTURE_DIR)
        named = {row.subject for row in history}
        missing = [subject for subject in named if not observations[subject]["exists"]]
        self.assertIn(f"{len(missing)} of the {len(named)} account id strings do not exist on chain at block "
                      f"{observations[missing[0]]['block']}.", history_part)
        reads = issuers.reads()
        self.assertIn(f"{len(reads)} status reads over {len({read['subject'] for read in reads})} account ids",
                      page[:page.index("<h2>Banned by the issuer</h2>")])
        own = own_text(page)
        for words in ("every action", "ever took", "frozen", "blocked"):
            self.assertNotIn(words, own.lower())
        self.assertIsNone(re.search(r"\bUSD\b|\$", own))
        self.assertIn('href="issuers.html"', self.pages["index.html"])

    def test_the_issuers_command_hands_every_argument_to_the_module_main(self):
        with mock.patch.object(issuers, "main", return_value=1) as module_main:
            self.assertEqual(intel_main.main(["issuers", "rows"]), 1)
            self.assertEqual(intel_main.main(["issuers", "--help"]), 1)
        self.assertEqual(module_main.call_args_list, [mock.call(["rows"]), mock.call(["--help"])])

    def test_the_cluster_command_hands_every_argument_to_the_module_main(self):
        with mock.patch.object(cluster, "main", return_value=2) as module_main:
            self.assertEqual(intel_main.main(["cluster", "--subject", "x.near"]), 2)
            self.assertEqual(intel_main.main(["cluster", "a.near", "b.near", "--name", "pair", "--record"]), 2)
            self.assertEqual(intel_main.main(["cluster"]), 2)
        self.assertEqual(module_main.call_args_list, [mock.call(["--subject", "x.near"]),
                                                      mock.call(["a.near", "b.near", "--name", "pair", "--record"]),
                                                      mock.call([])])

    def test_indexable_drops_the_robots_meta_and_the_default_keeps_it(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(intel_main.main(["render", "--out", tmp, "--indexable"]), 0)
            indexable = {name: (Path(tmp) / name).read_text(encoding="utf-8") for name in self.written}
        for name in (page for page in self.written if page.endswith(".html")):
            with self.subTest(page=name):
                self.assertIn(ROBOTS, self.pages[name])
                self.assertIn(ROBOTS, (SITE / name).read_text(encoding="utf-8"))
                self.assertNotIn('name="robots"', indexable[name])
                self.assertEqual(indexable[name], self.pages[name].replace(ROBOTS, ""))

    def test_inferred_rows_render_as_hypotheses_on_the_entity_pages(self):
        stated = make_row()
        hypothesis = make_row(subject="relay2.example.near", label="a relayer contract by the same operator",
                              provenance="inferred", rule_id="shared_operator_test", confidence=0.6,
                              source={"rule_id": "shared_operator_test", "inputs": [stated.id]},
                              note="derived from the onchain assertion in source.inputs")
        page = render.entity_page("Infrastructure", ("infrastructure",), labels.Store([hypothesis, stated], "0" * 64))
        heading = page.index("<h2>Attribution hypothesis</h2>")
        self.assertEqual(page.count("<tr><td"), 2)
        self.assertLess(page.index(stated.subject), heading)
        hypothesis_part = page[heading:]
        self.assertNotIn(stated.subject + "<", hypothesis_part)
        row = hypothesis_part[hypothesis_part.index("relay2.example.near"):]
        for value in ("shared_operator_test", stated.id, "0.6", "a relayer contract by the same operator"):
            self.assertIn(value, row)
        store = labels.load()
        for name, _, categories in render.ENTITY_SETS:
            with self.subTest(set=name):
                inferred = [a for a in store.assertions if a.category in categories and a.superseded_by is None
                            and a.provenance == "inferred"]
                self.assertEqual("<h2>Attribution hypothesis</h2>" in self.pages[f"entities/{name}.html"], bool(inferred))

    def test_the_entity_pages_list_every_current_row_of_their_set(self):
        store = labels.load()
        for name, _, categories in render.ENTITY_SETS:
            rows = [a for a in store.assertions if a.category in categories and a.superseded_by is None]
            with self.subTest(set=name):
                self.assertTrue(rows)
                self.assertEqual(self.pages[f"entities/{name}.html"].count("<tr><td"), len(rows))

    def test_no_account_page_for_a_subject_without_a_row(self):
        subject = "no-current-row.example.near"
        self.assertFalse(any(a.subject == subject and a.superseded_by is None for a in labels.load().assertions))
        self.assertNotIn(f"accounts/{subject}.html", self.written)
        replacement = make_row(subject="relay2.example.near")
        superseded = make_row(superseded_by=replacement.id)
        with tempfile.TemporaryDirectory() as tmp, mock.patch("urllib.request.urlopen", side_effect=no_network):
            written = render.render(tmp, store=labels.Store([superseded, replacement], "0" * 64))
            search = json.loads((Path(tmp) / "search.json").read_text(encoding="utf-8"))
            self.assertFalse((Path(tmp) / f"accounts/{page_name(superseded.subject)}.html").exists())
        self.assertNotIn(f"accounts/{page_name(superseded.subject)}.html", written)
        self.assertIn(f"accounts/{page_name(replacement.subject)}.html", written)
        self.assertEqual([entry["subject"] for entry in search["accounts"]], [replacement.subject])

    def test_an_inferred_row_renders_under_the_hypothesis_heading_on_its_account_page(self):
        store = labels.load()
        by_id = {a.id: a for a in store.assertions}
        inferred = [a for a in store.assertions if a.provenance == "inferred" and a.superseded_by is None]
        self.assertIn("fake_token_shared_hash", {row.rule_id for row in inferred})
        for row in inferred:
            with self.subTest(row=row.id):
                page = self.pages[f"accounts/{page_name(row.subject)}.html"]
                heading = page.index("<h2>Attribution hypothesis</h2>")
                plain, hypothesis = page[:heading], page[heading:]
                stated = [a for a in store.assertions if a.subject == row.subject and a.superseded_by is None
                          and a.provenance != "inferred"]
                self.assertEqual(plain.count("<tr><td>"), len(stated))
                self.assertNotIn("<td>inferred</td>", plain)
                self.assertIn(render.data_text(row.label), hypothesis)
                self.assertIn(f"<td>{row.confidence}</td>", hypothesis)
                self.assertIn(f'<td class="mono"><q class="data">{row.rule_id}</q></td>', hypothesis)
                self.assertNotIn(row.rule_id, plain)
                for input_id in row.source["inputs"]:
                    self.assertIn(f'<a href="../accounts/{page_name(by_id[input_id].subject)}.html">{input_id}</a>',
                                  hypothesis)

    def test_the_entity_page_quotes_the_rule_id_of_an_inferred_row(self):
        fact = make_row()
        row = make_row(subject="treasury.example.near", category="treasury", provenance="inferred",
                       source={"rule_id": "a_test_rule", "inputs": [fact.id]}, rule_id="a_test_rule", confidence=0.9)
        page = render.entity_page("Treasuries", ("treasury",), labels.Store([fact, row], "0" * 64))
        heading = page.index("<h2>Attribution hypothesis</h2>")
        self.assertIn('<td class="mono"><q class="data">a_test_rule</q></td>', page[heading:])
        self.assertNotIn("a_test_rule", own_text(page))

    def test_the_account_page_shows_the_chain_of_every_row(self):
        chains = {}
        for a in labels.load().assertions:
            if a.superseded_by is None:
                chains.setdefault(a.subject, []).append(a.chain)
        subject = max(chains, key=lambda key: len(set(chains[key])))
        self.assertGreaterEqual(len(set(chains[subject])), 12)
        page = self.pages[f"accounts/{page_name(subject)}.html"]
        self.assertIn("<th>Chain</th>", page)
        self.assertEqual(sorted(re.findall(r"<tr><td>([a-z0-9]+)</td>", page)), sorted(chains[subject]))

    def test_the_account_page_lists_each_trace_edge_once_with_its_count(self):
        subject, counts = "intents.near", {}
        for path in sorted((ROOT / "intel" / "flows").glob("*.json")):
            for hop in json.loads(path.read_text(encoding="utf-8"))["hops"]:
                if subject in (hop["from"], hop["to"]):
                    key = (path.stem, hop["from"], hop["to"])
                    counts[key] = counts.get(key, 0) + 1
        self.assertTrue(any(count > 1 for count in counts.values()))
        trace_edges = re.compile(r"<h2>Trace edges</h2>(.*?)<h2>", re.DOTALL)
        items = re.findall(r"<li>.*?</li>", trace_edges.search(self.pages[f"accounts/{page_name(subject)}.html"]).group(1))
        expected = [f'<li><a href="/flows/{name}.html">Committed trace edge{"s" if count > 1 else ""}: '
                    f'<q class="data">{html.escape(source)}</q> to <q class="data">{html.escape(target)}</q>'
                    f'{f", {count} edges" if count > 1 else ""}</a></li>' for (name, source, target), count in counts.items()]
        self.assertEqual(items, expected)
        for name, page in self.pages.items():
            if name.startswith("accounts/"):
                with self.subTest(page=name):
                    items = re.findall(r"<li>.*?</li>", trace_edges.search(page).group(1))
                    self.assertEqual(len(items), len(set(items)))

    def test_the_entity_page_lists_its_members_with_the_registry_read(self):
        entity = json.loads((ROOT / "intel" / "entities.jsonl").read_text(encoding="utf-8").splitlines()[0])
        page = self.pages[f"entities/{entity['id']}.html"]
        self.assertIn("<h2>Members</h2>", page)
        self.assertIn(f'<q class="data">{html.escape(entity["members"][0])}</q>', page)
        self.assertIn(entity["source"]["method"], page)
        self.assertIn(str(entity["source"]["block"]), page)

    def test_the_verify_page_reads_the_testnet_rpc_only(self):
        page = self.pages["verify.html"]
        self.assertIn('fetch("https://test.rpc.fastnear.com"', page)
        self.assertIn('method_name: "latest"', page)
        self.assertNotIn("shieldbotsecurity", page)
        self.assertEqual(re.findall(r"https://[a-z.]+", page.split("<script>", 1)[1]), ["https://test.rpc.fastnear.com"])

    def test_the_committed_site_equals_a_fresh_render(self):
        committed = sorted(p.relative_to(SITE).as_posix() for p in SITE.rglob("*") if p.is_file())
        self.assertEqual(committed, sorted(self.written))
        for name in self.written:
            with self.subTest(page=name):
                self.assertEqual((SITE / name).read_bytes(), (self.out / name).read_bytes())

    def test_the_command_writes_the_pages(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(intel_main.main(["render", "--out", tmp]), 0)
            self.assertTrue((Path(tmp) / "index.html").is_file())


if __name__ == "__main__":
    unittest.main()
