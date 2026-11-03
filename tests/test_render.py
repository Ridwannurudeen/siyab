import contextlib
import hashlib
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
from intel import cluster, feed, issuers, labels, render, trace

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "demo" / "site"
ROBOTS = '<meta name="robots" content="noindex">\n'
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
        clusters = [p.stem for p in (ROOT / "intel" / "clusters").glob("*.json")]
        expected = (["index.html", "feed.html", "verify.html", "404.html", "issuers.html"]
                    + [f"incidents/{i}.html" for i in incidents]
                    + [f"entities/{name}.html" for name, _, _ in render.ENTITY_SETS] + [f"flows/{f}.html" for f in flows]
                    + [f"clusters/{c}.html" for c in clusters])
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
        self.assertEqual(len(render._counts_lines()), 6)
        for words in ("recorded mainnet receivers the rules judged", "label store", "provenance class", "incident pages",
                      "feed", "exit traces", "verdict latency runs"):
            self.assertIn(words, render.COUNTS_OF)

    def test_the_feed_page_names_a_recorded_window_with_its_date_and_says_it_is_not_following(self):
        state = json.loads(feed.STATE_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("mode", state)
        self.assertIn(f"<p>Recorded window: blocks {state['start_height']} to {state['last_height']}, read on "
                      f"{state['start_time'][:10]} UTC. Live following is not running yet.</p>", self.pages["feed.html"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            following = dict(state, mode="follow")
            path.write_text(json.dumps(following), encoding="utf-8")
            page = render.feed_page(path, Path(tmp) / "events")
        self.assertNotIn("Recorded window", page)
        self.assertNotIn("not running", page)
        for line in feed.stats_lines(following):
            self.assertIn(f"<li>{html.escape(line)}</li>", page)

    def test_the_verify_page_inputs_carry_placeholders_from_committed_records(self):
        record = json.loads((ROOT / "fixtures" / "registry" / "testnet-vendor.json").read_text(encoding="utf-8"))
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
                    self.assertIn(f'<p class="mono">{html.escape(edge["from"])} to {html.escape(edge["to"])}</p>', card)
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
                    self.assertIn(f'<li class="mono">{html.escape(seed)}</li>', page)
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
        for name in self.written:
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
