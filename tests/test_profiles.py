import html
import json
import re
import copy
import shutil
import subprocess
import unittest
from pathlib import Path

from intel import issuers, labels, render


class ProfilesTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_live_check_boundaries(self):
        result = subprocess.run([shutil.which("node"), "tests/profiles_runtime.cjs"],
                                cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @classmethod
    def setUpClass(cls):
        cls.store = labels.load()
        cls.accounts = render.account_pages(cls.store)
        cls.flows = render._flows()
        cls.incidents = render._incidents()

    def test_account_exposes_bounded_recorded_activity_without_inventing_live_results(self):
        subject = self.flows[0][1]["subject"]
        document = render.account_page(subject, self.store, self.accounts, self.incidents, self.flows, [])
        self.assertIn('data-profile-activity', document)
        self.assertIn('Check 1 NEAR payment', document)
        self.assertIn('Payment result not checked', document)
        self.assertIn('Current balance unavailable', document)
        self.assertEqual(document.count('data-profile-transfer-row'), 20)
        self.assertIn('Recorded counterparties', document)
        self.assertIn('<summary>Source records and attribution evidence</summary>', document)
        self.assertIn('data-watch-account=', document)
        self.assertIn('data-save-investigation', document)

    def test_incident_timeline_is_evidence_chronology_and_preserves_quotes(self):
        incident = next(document for document in self.incidents if document["id"] == "rhea")
        document = render.incident_page(incident, self.accounts)
        self.assertIn('Evidence timeline', document)
        self.assertIn('Recorded transfer route', document)
        for source in incident["sources"]:
            self.assertIn(source["content_sha256"], document)
        for account in incident["accounts"]:
            quote = html.escape(account["quote"]["text"])
            self.assertIn(quote, re.sub(r'<span class="data-id">([0-9a-f]{64})</span>', r'\1', document))
            for identifier in re.findall(r"[0-9a-f]{64}", quote):
                self.assertIn(f'<span class="data-id">{identifier}</span>', document)
        for line in incident["not_public"]:
            self.assertIn(html.escape(line), document)

    def test_issuer_tables_keep_observation_dates_and_coverage(self):
        document = render.issuers_page()
        self.assertIn('data-profile-table="issuer-status"', document)
        self.assertIn('data-profile-table="issuer-history"', document)
        self.assertIn('752', document)
        for row in labels.load(issuers.STATUS_PATH).assertions:
            self.assertIn(row.id, document)
            self.assertIn(row.observed_at, document)
        self.assertIn(render.data_text(render._history_coverage(labels.load(issuers.HISTORY_PATH).assertions)), document)

    def test_registry_members_are_labelled_searchable_records(self):
        entity = json.loads((render.ROOT / "intel/entities.jsonl").read_text(encoding="utf-8").splitlines()[0])
        document = render.registry_entity_page(entity, self.accounts)
        self.assertIn('data-profile-table="registry-members"', document)
        self.assertIn('Search members', document)
        self.assertIn(entity["observed_at"], document)
        self.assertIn(entity["members"][0], document)

    def test_transfer_display_uses_base_units_and_known_asset_decimals(self):
        row = render._profile_transfer_rows(self.flows[0][1]["subject"], self.flows)[0]
        for asset, amount, human, expected in (
                ("native:near", "100000000000000000000000", 0.09999999999999999, "0.1"),
                ("native:near", "320982299467252467848273181", 320.98229946725246, "320.9823"),
                ("native:near", "1000000000000000000020000", 1.0, "1"),
                ("nep141:usdt.tether-token.near", "1564700999000", 1564700.9989999998, "1,564,700.999"),
                ("nep245:intents.near:nep141:usdt.tether-token.near", "100000", 0.09999999999999999, "0.1"),
                ("native:near", "1", None, "&lt;0.0001"),
                ("nep141:unknown.example.near", "42", 0.42, "42 base units")):
            with self.subTest(asset=asset, amount=amount):
                item = dict(row, asset_id=asset, amount=amount, human_amount=human)
                cells = render._profile_transfer_cells(item["from"], item, self.accounts)
                self.assertIn(f'<span class="profile-value">{expected}</span>', cells[3])
                self.assertIn(f'Base units {amount}<br>', cells[5])

    def test_recorded_route_and_transfer_cards_use_exact_amount_display(self):
        incident = next(document for document in self.incidents if document["id"] == "rhea")
        flow = copy.deepcopy(self.flows[0][1])
        item = next(hop for hop in flow["hops"] if hop["from"] == flow["subject"])
        item.update(asset_id="native:near", amount="100000000000000000000000", human_amount=0.09999999999999999)
        from unittest import mock
        with mock.patch.object(render, "_flows", return_value=[("recorded", flow)]):
            page = render.incident_page(incident, self.accounts)
        self.assertIn('<span>0.1<br><q class="data">native:near</q>', page)
        card = render._hop_card(item, self.accounts)
        self.assertIn('<p>0.1 units, from recorded base units.</p>', card)
        self.assertNotIn('Provider amounts', card)
        self.assertNotIn(str(item["human_amount"]), card)
        self.assertIn(item["amount"] + ' base units', card)
        item["usd_amount"] = 0.09999999999999999
        card = render._hop_card(item, self.accounts)
        self.assertIn(', 0.1 USD as the Transfers API values it', card)
        self.assertNotIn(str(item["usd_amount"]), card)
        self.assertNotIn('USD as recorded by the provider', card)

    def test_flow_card_rounded_amount_names_recorded_base_units(self):
        item = dict(self.flows[0][1]["hops"][0], asset_id="native:near", amount="1", human_amount=None)
        card = render._hop_card(item, self.accounts)
        self.assertIn('<p>&lt;0.0001 units, from recorded base units.</p>', card)
        self.assertIn('1 base unit of', card)
        self.assertNotIn('1 base units', card)

    def test_flow_card_unknown_decimals_explains_base_units(self):
        item = dict(self.flows[0][1]["hops"][0], asset_id="nep141:unknown.example.near",
                    amount="42", human_amount=0.42)
        card = render._hop_card(item, self.accounts)
        self.assertIn('<p>42 base units (decimals not recorded).</p>', card)
        self.assertNotIn('from recorded base units', card)
        self.assertNotIn('0.42', card)
        self.assertNotIn('Provider amounts', card)

    def test_missing_transfer_values_remain_unknown_and_keep_exact_base_units(self):
        flow = copy.deepcopy(self.flows[0][1])
        subject = flow["subject"]
        latest = max((hop for hop in flow["hops"] if subject in (hop["from"], hop["to"])), key=lambda row: row["block_timestamp"])
        latest["human_amount"] = None
        latest["usd_amount"] = None
        latest["asset_id"] = "nep141:unknown.example.near"
        document = render.account_page(subject, self.store, self.accounts, self.incidents, [(self.flows[0][0], flow)], [])
        self.assertIn(f'{latest["amount"]} base units', document)
        self.assertIn('<span class="profile-value">Unavailable</span>', document)
        self.assertIn('unavailable values are not zero', document)


if __name__ == "__main__":
    unittest.main()
