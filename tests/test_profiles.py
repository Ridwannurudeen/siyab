import html
import json
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
            self.assertIn(html.escape(account["quote"]["text"]), document)
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

    def test_missing_transfer_values_remain_unknown_and_keep_exact_base_units(self):
        flow = copy.deepcopy(self.flows[0][1])
        subject = flow["subject"]
        latest = max((hop for hop in flow["hops"] if subject in (hop["from"], hop["to"])), key=lambda row: row["block_timestamp"])
        latest["human_amount"] = None
        latest["usd_amount"] = None
        document = render.account_page(subject, self.store, self.accounts, self.incidents, [(self.flows[0][0], flow)], [])
        self.assertIn(f'{latest["amount"]} base units', document)
        self.assertIn('<span class="profile-value">Unavailable</span>', document)
        self.assertIn('unavailable values are not zero', document)


if __name__ == "__main__":
    unittest.main()
