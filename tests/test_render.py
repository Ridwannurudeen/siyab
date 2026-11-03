import contextlib
import html
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intel import __main__ as intel_main
from intel import labels, render, trace

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "demo" / "site"
# COMMON rule 3: the hygiene phrases plus the words ShieldBot never writes in its own text.
EXTRA_WORDS = re.compile(r"go to|guarantee|secure|safe|\bshield\b", re.IGNORECASE)
BARRED = re.compile(r"scam|clone|fake|drainer", re.IGNORECASE)
DASHES = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")
QUOTED = re.compile(r'<blockquote class="data"[^>]*>.*?</blockquote>|<q class="data">.*?</q>', re.DOTALL)
LINK = re.compile(r'(?:href|src)="([^"]*)"')


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


def own_text(page):
    """The visible text of a page with every quoted source string removed."""
    text = QUOTED.sub(" ", page)
    text = re.sub(r"<style>.*?</style>", " ", text, flags=re.DOTALL)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with mock.patch("urllib.request.urlopen", side_effect=no_network):
            cls.written = render.render(cls.tmp.name)
        cls.out = Path(cls.tmp.name)
        cls.pages = {name: (cls.out / name).read_text(encoding="utf-8") for name in cls.written}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_page_is_written(self):
        incidents = [p.stem for p in (ROOT / "intel" / "incidents").glob("*.json")]
        flows = [p.stem for p in (ROOT / "intel" / "flows").glob("*.json")]
        expected = (["index.html", "feed.html", "verify.html"] + [f"incidents/{i}.html" for i in incidents]
                    + [f"entities/{name}.html" for name, _, _ in render.ENTITY_SETS] + [f"flows/{f}.html" for f in flows])
        self.assertEqual(sorted(self.written), sorted(expected))

    def test_every_page_is_noindex_with_the_footer_and_no_external_script(self):
        for name, page in self.pages.items():
            with self.subTest(page=name):
                self.assertTrue(page.startswith("<!doctype html>"))
                self.assertIn('<meta name="robots" content="noindex">', page)
                self.assertIn("Data: NearBlocks", page)
                self.assertNotIn("<script src", page)
                self.assertNotIn('rel="stylesheet"', page)
                self.assertEqual(page.count("<style>"), 1)

    def test_no_forbidden_or_barred_word_outside_quoted_source_data(self):
        for name, page in self.pages.items():
            with self.subTest(page=name):
                text = own_text(page)
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
                    target = ((self.out / name).parent / html.unescape(link)).resolve()
                    self.assertIn(root, target.parents)
                    self.assertTrue(target.is_file())

    def test_the_index_carries_the_counts_lines(self):
        for line in render._counts_lines():
            self.assertIn(f"<p>{html.escape(line)}</p>", self.pages["index.html"])

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
            self.assertIn(f"{entry['subject']} as {entry['as']}", page)
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
            self.assertIn(f'<td class="mono">{html.escape(bucket["to"])}</td><td>{html.escape(bucket["kind"])}</td>'
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

    def test_the_entity_pages_list_every_current_row_of_their_set(self):
        store = labels.load()
        for name, _, categories in render.ENTITY_SETS:
            rows = [a for a in store.assertions if a.category in categories and a.superseded_by is None]
            with self.subTest(set=name):
                self.assertTrue(rows)
                self.assertEqual(self.pages[f"entities/{name}.html"].count("<tr><td"), len(rows))

    def test_the_verify_page_reads_the_testnet_rpc_only(self):
        page = self.pages["verify.html"]
        self.assertIn('fetch("https://test.rpc.fastnear.com"', page)
        self.assertIn('method_name: "latest"', page)
        self.assertNotIn("shieldbotsecurity", page)
        self.assertEqual(re.findall(r"https://[a-z.]+", page.split("<script>", 1)[1]), ["https://test.rpc.fastnear.com"])

    def test_the_committed_site_equals_a_fresh_render(self):
        committed = sorted(p.relative_to(SITE).as_posix() for p in SITE.rglob("*.html"))
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
