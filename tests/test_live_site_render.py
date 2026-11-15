import html
import json
import re
import tempfile
from pathlib import Path
from unittest import mock
import unittest
from datetime import UTC, datetime

from intel import labels, records, render, trace
from tests.test_render import make_row


class LiveSiteRenderTest(unittest.TestCase):
    def test_landing_preview_describes_lookup_destination(self):
        page = render.index_page([], render._flows(), [], labels.load())
        self.assertEqual(page.count("Shown on the lookup page after you investigate an account."), 4)
        self.assertNotIn("Select an account to read this data.", page)

    def test_all_page_kinds_have_distinct_landmark_names_and_no_private_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network read")):
                written = render.render(tmp)
            for name in written:
                if not name.endswith(".html"):
                    continue
                page = (Path(tmp) / name).read_text(encoding="utf-8")
                with self.subTest(page=name):
                    names = re.findall(r'<nav\b[^>]*aria-label="([^"]+)"', page)
                    self.assertEqual(names.count("Primary"), 1)
                    self.assertEqual(names.count("Mobile primary"), 1)
                    links = re.findall(r'<a\b[^>]*href="([^"]+)"', page)
                    private = [url for url in links if re.match(r"https://github[.]com/Ridwannurudeen/(?:siyab|shieldbot-near)/", url, re.IGNORECASE)]
                    self.assertEqual(private, [])

    def test_distribution_uses_singular_row_counts(self):
        document = render._flows()[0][1]
        document["shares"]["buckets"][0]["count"] = 1
        document["shares"]["below_min_usd"]["count"] = 1
        document["shares"]["without_usd"]["count"] = 1
        page = render._flow_analytics(document, {})
        self.assertIn("</span>1 row</span>", page)
        self.assertIn("1 row below", page)
        self.assertIn("1 row without a USD amount", page)
        self.assertNotIn("1 rows", page)

    def test_feed_provider_uses_recorded_url_host(self):
        event = {"source": {"url": "https://blocks.example/v0/block/1"}}
        page = render._feed_panel([(event, "event")])
        self.assertIn("blocks.example<br>Unlinked record", page)
        event["source"]["provider"] = "Recorded provider"
        self.assertIn("Recorded provider<br>Unlinked record", render._feed_panel([(event, "event")]))
        for url in ("not a URL", "https://[broken", "file:///record"):
            with self.subTest(url=url):
                event["source"] = {"url": url}
                self.assertIn("Provider not recorded<br>", render._feed_panel([(event, "event")]))

    def test_feed_health_links_to_raw_feed(self):
        self.assertIn('<a href="/feed">Open raw feed JSON</a>', render._feed_panel())

    def test_receiver_pages_only_publish_aggregate_counts(self):
        explore = render.explore_page([], [], [], labels.load())
        receivers = render.top_receivers_page()
        self.assertIn("Aggregate counts are published.", explore)
        self.assertIn("Only aggregate counts are public on this page.", receivers)
        self.assertNotIn("Judgements are published.", explore)
        for page in (explore, receivers):
            self.assertNotRegex(page, r'href="https://github[.]com/[^\"]*(?:judgements|fixtures/runs|docs/)')

    def test_issuer_table_states_banned_answers_only(self):
        page = render.issuers_page()
        table = page.split('id="issuer-status-section"', 1)[1].split('<section class="profile-section" id="issuer-ledger"', 1)[0]
        self.assertIn("Banned answers only.", table)

    def test_incident_brief_does_not_quote_unnamed_account_record(self):
        document = next(item for item in render._incidents() if not item["accounts"])
        page = render.incident_page(document, {})
        brief = page.split('class="profile-case-brief"', 1)[1].split("</section>", 1)[0]
        self.assertIn("The source snapshots do not name a public account.", brief)
        self.assertNotIn("<blockquote", brief)
        quote = next(value["quote"]["text"] for key, value in document.items() if key.endswith("_as_reported"))
        self.assertEqual(page.count(html.escape(quote)), 1)

    def test_incident_rules_heading_has_recorded_date(self):
        for document in render._incidents():
            page = render.incident_page(document, {})
            at = document.get("rules_today_at_ms")
            date = " on " + datetime.fromtimestamp(at / 1000, UTC).strftime("%Y-%m-%d") if at else ""
            self.assertIn("<h2>Rules as evaluated" + date + "</h2>", page)
            self.assertNotIn("<h2>Rules today</h2>", page)

    def test_incident_onchain_empty_state(self):
        document = next(item for item in render._incidents() if not item["accounts"])
        page = render.incident_page(document, {})
        self.assertIn("<h2>On chain facts</h2>\n<p>No on chain fact is recorded.</p>", page)

    def test_incident_brief_uses_singular_counts(self):
        document = next(item for item in render._incidents() if item["accounts"])
        document["accounts"] = document["accounts"][:1]
        document["chains"] = document["chains"][:1]
        document["sources"] = document["sources"][:1]
        page = render.incident_page(document, {})
        self.assertIn("1 named account across 1 chain; 1 recorded source snapshot.", page)

    def test_workspace_explains_display_pause_scope(self):
        self.assertIn("Pausing the live displays does not stop alert monitoring.", render.workspace_page())

    def test_explore_feed_panel_opens_the_feed_page_and_the_feed_page_opens_raw_json(self):
        explore = render.explore_page([], [], [], labels.load())
        self.assertIn('<h2>Follower health</h2><a href="/feed.html">Open feed</a>', explore)
        self.assertNotIn('href="/feed"', explore)
        feed_page = render.feed_page()
        self.assertIn('<h2>Follower health</h2><a href="/feed">Open raw feed JSON</a>', feed_page)
        self.assertNotIn('<a href="/feed.html">Open feed</a>', feed_page)

    def test_private_record_evidence_is_text_with_its_hash_and_public_sources_are_links(self):
        path = next(path for path in sorted(render.RECORDS_DIR.glob("*.json")) if path.name != "body-vector.json")
        record = records.read(path)
        source = record["evidence"][0]
        page = render.record_index_page([record])
        self.assertIn(html.escape(source["url"]), page)
        self.assertIn(html.escape(source["content_sha256"]), page)
        self.assertNotIn('href="' + html.escape(source["url"]) + '"', page)
        source["url"] = "https://reports.example/incident?a=1&b=2"
        self.assertIn('<a href="' + html.escape(source["url"]) + '">', render.record_index_page([record]))

    def test_explore_formats_label_counts_and_names_the_later_report_record(self):
        page = render.explore_page([], [], [], labels.load())
        counts = labels.load().counts()
        self.assertIn(f'Label store size: {counts["assertions"]:,} assertions over {counts["subjects"]:,} subjects.', page)
        self.assertIn("The later threshold run is recorded in the 9 Nov demonstration above.", page)
        self.assertNotIn("Dated reports updates", page)

    def test_incident_timeline_links_public_publisher_and_escapes_details_once(self):
        document = render._incidents()[0]
        source = document["sources"][0]
        source["publisher"] = "Publisher <record> & report"
        source["url"] = "https://reports.example/incident?a=1&b=2"
        timeline = render.incident_page(document, {}).split('<ol class="profile-timeline">', 1)[1].split("</ol>", 1)[0]
        self.assertIn('<a href="' + html.escape(source["url"]) + '" rel="noopener">' + html.escape(source["publisher"]) + '</a>', timeline)
        self.assertNotIn("&amp;lt;", timeline)
        self.assertNotIn("<record>", timeline)

    def test_top_receiver_deny_explains_combined_signals_and_payment_scope(self):
        page = render.top_receivers_page()
        self.assertIn("Lookalike with code without metadata: 1.</p><p>Other DENY signal groups: 5.</p><p>Each verdict is what an agent's 1 NEAR payment to that receiver would get, not a finding that the account is malicious.</p>", page)

    def test_single_account_activity_and_counterparty_counts(self):
        subject = "account.example.near"
        row = make_row(subject=subject)
        document = render._flows()[0][1]
        hop = dict(document["hops"][0], **{"from": subject, "to": "other.example.near"})
        document["hops"] = [hop]
        document["subject"] = subject
        page = render._profile_account_summary(subject, [row], {}, [("one", document)])
        self.assertIn("Showing 1 of 1 recorded row, newest first.", page)
        self.assertIn("1 of 1 recorded counterparty initially shown.", page)
        self.assertNotIn("1 recorded counterparties", page)

    def test_single_flow_counts_are_singular(self):
        document = render._flows()[0][1]
        document["hops"] = [next(hop for hop in document["hops"] if hop["from"] == document["subject"])]
        document["shares"] = trace.shares(document["subject"], document["hops"], set(), set())
        page = render.flow_page(document, {})
        for text in ("1 root outbound transfer,", "(1 transfer)</option>", "1 root outbound transfer from", "1 recorded transfer in 1 time bin", "Peak interval: 1 root outbound transfer.", "of 1 transfer, grouped", "Depth 1: 1 hop", "1 of these hops appears"):
            with self.subTest(text=text):
                self.assertIn(text, page)
        self.assertIn("across 1 root outbound row valued", page)

    def test_single_entity_and_record_counts_are_singular(self):
        row = make_row()
        store = labels.Store([row], "0" * 64)
        page = render.entity_page("Infrastructure", ("infrastructure",), store)
        for text in ("1 current assertion from", "1 source assertion on this page.", "of 1 assertion."):
            self.assertIn(text, page)
        entity = json.loads((render.ROOT / "intel/entities.jsonl").read_text(encoding="utf-8").splitlines()[0])
        entity["members"] = entity["members"][:1]
        self.assertIn("1 recorded member</p>", render.registry_entity_page(entity, {}))
        path = next(path for path in sorted(render.RECORDS_DIR.glob("*.json")) if path.name != "body-vector.json")
        record = records.read(path)
        record["reports"] = [{"report_id": 0}]
        page = render.record_index_page([record])
        self.assertIn("1 report citation", page)
        self.assertNotIn("1 report citations", page)
        self.assertIn("1 valid immutable record</caption>", page)

    def test_single_landing_and_issuer_read_counts_are_singular(self):
        flows = render._flows()
        name, document = flows[0]
        document["hops"] = [next(hop for hop in document["hops"] if hop["from"] == document["subject"])]
        page = render.index_page([], [(name, document)], [], labels.load())
        self.assertIn("1 recorded transfer.", page)
        read = render.issuers.reads()[0]
        summary = render._status_reads([read])
        for text in ("1 status read over 1 account id:", "1 issuer view at", "1 time."):
            self.assertIn(text, summary)

    def test_single_cluster_and_registry_group_counts_are_singular(self):
        name, document = render._clusters()[0]
        document["seeds"] = document["seeds"][:1]
        document["depositors"]["count"] = 1
        document["depositors"]["implicit_count"] = 1
        page = render.cluster_page(name, document, {})
        for text in ("1 seed, each", "1 distinct depositor of", "1 of them implicit account;"):
            self.assertIn(text, page)
        row = make_row(category="organisation", source={"rpc": "https://rpc.example", "method": "validators", "args": {}, "block": 1})
        page = render.entity_page("Organisations", ("organisation",), labels.Store([row], "0" * 64))
        self.assertIn("1 account in this recorded read", page)
        self.assertIn('1 <span class="stat-connective">assertion from</span> 1 <span class="stat-connective">recorded registry read.</span>', page)

    def test_single_history_coverage_counts_are_singular(self):
        row = make_row()
        coverage = {"transactions": 1, "txs_count": 1, "tx_block_min": 1, "tx_block_max": 1, "actions": {"ban": 1}}
        with mock.patch.object(render.issuers, "history_coverage", return_value=coverage), mock.patch.object(render.issuers, "_owner", return_value="issuer.near"), mock.patch.object(render.issuers, "_account_observations", return_value={row.subject: {"exists": False, "block": 1}}):
            sentence = render._history_coverage([row])
        for text in ("1 function call transaction ", ", was read;", "1 successful issuer action on", "1 account id string does not exist"):
            self.assertIn(text, sentence)

    def test_explore_counts_block_formats_counts_and_says_the_feed_line_is_the_committed_snapshot(self):
        page = render.explore_page([], [], [], labels.load())
        block = page.split("<summary>Committed data counts</summary>", 1)[1].split("</details>", 1)[0]
        counts = labels.load().counts()
        self.assertIn(f"Label store: {counts['assertions']:,} assertions over {counts['subjects']:,} subjects", block)
        self.assertIn("1 exit trace,", block)
        self.assertNotRegex(block, r"\b1 exit traces\b")
        self.assertNotRegex(block, r"\d{4,} (?:blocks|hops|assertions|subjects|ms)\b")
        snapshot = json.loads(render.feed.SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertIn(f"Feed: following since {snapshot['started_at'][:10]}, {snapshot['blocks']:,} blocks", block)
        self.assertIn(f"which ends at {snapshot['last_time']}. Events the running follower raised after it are on the "
                      '<a href="feed.html">feed page</a>, not in this count.', block)
        self.assertEqual(render._counts_display("Flows: 1 exit trace, 1708 hops, from 2026-11-03T05:22:06Z."),
                         "<p>Flows: 1 exit trace, 1,708 hops, from 2026-11-03T05:22:06Z.</p>")

    def test_issuer_scope_names_the_method_behind_each_other_answer(self):
        page = render.issuers_page()
        scope = page.split('id="issuer-status-section"', 1)[1].split('<p class="issuers-scope">', 1)[1].split("</p>", 1)[0]
        self.assertIn("is_blocklisted answered false 380 times", scope)
        self.assertIn('get_blacklist_status answered <q class="data">Allowable</q> 369 times', scope)
        self.assertNotIn("false: 380", scope)

    def test_distribution_threshold_uses_two_decimal_usd(self):
        document = render._flows()[0][1]
        page = render._flow_analytics(document, {})
        self.assertIn("valued at or above 1.00 USD by the Transfers API", page)
        self.assertIn("below 1.00 USD:", page)
        self.assertNotRegex(page, r"\b1\.0 USD")
        self.assertNotRegex(render._shares_table(document["shares"], {}), r"\b1\.0 USD")
        self.assertIn("below 1.00 USD)", trace.summary(document))

    def test_verify_names_the_example_account_instead_of_the_vendor(self):
        page = render.verify_page()
        self.assertNotIn("vendor", page)
        self.assertIn("the latest record for <code>kestrelridge-shop.testnet</code>, the allowlisted demo payee on testnet", page)

    def test_record_pages_share_the_site_header_theme_script_and_search(self):
        record = render.records.read(next(render.RECORDS_DIR.glob("*.json")))
        feed_page = render.page("Feed", "<h1>Feed</h1>", depth=1)
        header = lambda html: html.split("<header", 1)[1].split("</header>", 1)[0].replace(' aria-current="page"', "")
        for page in (render.record_index_page([record]), render.record_page(record)):
            self.assertEqual(header(page), header(feed_page))
            self.assertTrue(page.split("<head>\n", 1)[1].startswith("<script>try{var t=localStorage.getItem('siyab-theme')"))
            self.assertIn('<dialog class="search-dialog" id="global-search"', page)
            self.assertNotIn("record-theme", page)
            self.assertIn('<main class="app-content record-content" id="main-content">', page)

    def test_hop_card_names_one_unit_and_one_base_unit_in_the_singular(self):
        hop = dict(render._flows()[0][1]["hops"][0], amount="1", asset_id="native:near")
        card = render._hop_card(hop, {})
        self.assertIn("1 base unit of", card)
        self.assertNotRegex(card, r"\b1 (?:base )?units\b")
        decimals = trace.asset_decimals(hop["asset_id"])
        hop = dict(hop, amount=str(10 ** decimals))
        card = render._hop_card(hop, {})
        self.assertIn("<p>1 unit, from recorded base units.</p>", card)
        self.assertEqual(render._transfer_amount(dict(hop, asset_id="unknown.near", amount="1")), "1 base unit")
