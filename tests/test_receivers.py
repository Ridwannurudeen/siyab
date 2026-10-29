import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import __main__ as intel_main
from intel import labels

ROOT = Path(__file__).resolve().parent.parent
SET_PATH = ROOT / "fixtures" / "receivers" / "set.json"
JUDGEMENTS = ROOT / "fixtures" / "receivers" / "judgements"
VERDICTS = ("ALLOW", "DENY", "UNKNOWN")


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def load_counts():
    spec = importlib.util.spec_from_file_location("counts", ROOT / "scripts" / "counts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReceiverSetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(SET_PATH.read_text(encoding="utf-8"))
        cls.subjects = [entry["subject"] for entry in cls.document["subjects"]]
        cls.store = labels.load()

    def test_the_set_holds_40_to_60_distinct_subjects_each_with_a_reason(self):
        self.assertTrue(40 <= len(self.subjects) <= 60, len(self.subjects))
        self.assertEqual(len(set(self.subjects)), len(self.subjects))
        for entry in self.document["subjects"]:
            with self.subTest(subject=entry["subject"]):
                self.assertTrue(entry["why"])
                self.assertIn(entry["as"], ("token", "receiver"))
                target = entry["request"]["token_id"] if entry["as"] == "token" else entry["request"]["receiver_id"]
                self.assertEqual(target, entry["subject"])

    def test_the_sources_are_the_public_accounts_named_in_the_code(self):
        self.assertEqual([(s["account"], s["role"]) for s in self.document["sources"]],
                         list(intel_main.RECEIVER_SOURCES))
        for source in self.document["sources"]:
            for key in source["requests"]:
                self.assertTrue((intel_main.FASTNEAR_DIR / f"{key}.json").is_file(), key)
        counterparties = [e for e in self.document["subjects"] if "counterparties of" in e["why"]]
        for account, _ in intel_main.RECEIVER_SOURCES:
            named = [e for e in counterparties if e["why"].endswith(f"of {account} in the FastNEAR Transfers API")]
            self.assertTrue(0 < len(named) <= intel_main.COUNTERPARTIES, account)

    def test_the_rhea_accounts_and_ten_catalog_tokens_are_there(self):
        rhea = json.loads(intel_main.RHEA_INCIDENT.read_text(encoding="utf-8"))
        for account in rhea["accounts"]:
            if account["chain"] == "near":
                self.assertIn(account["subject"], self.subjects)
        catalog = [e for e in self.document["subjects"] if e["why"].startswith("a catalog_token NEAR account")]
        self.assertEqual(len(catalog), intel_main.CATALOG_SUBJECTS)
        accounts = intel_main.catalog_accounts(self.store)
        for entry in catalog:
            self.assertIn(entry["subject"], accounts)
            self.assertEqual(entry["as"], "token")

    def test_no_implicit_account_unless_a_catalog_token_or_named_by_an_incident_page(self):
        catalog = set(intel_main.catalog_accounts(self.store))
        named = {account["subject"] for path in (ROOT / "intel" / "incidents").glob("*.json")
                 for account in json.loads(path.read_text(encoding="utf-8"))["accounts"]}
        for subject in self.subjects:
            if intel_main.IMPLICIT.fullmatch(subject):
                with self.subTest(subject=subject):
                    self.assertTrue(subject in catalog or subject in named)
        for entry in self.document["subjects"]:
            if "counterparties of" in entry["why"] and intel_main.IMPLICIT.fullmatch(entry["subject"]):
                self.assertIn(entry["subject"], catalog)

    def test_at_most_30_receivers_had_their_age_recorded(self):
        aged = {e["request"]["receiver_id"] for e in self.document["subjects"] if e["age_recorded"]}
        self.assertLessEqual(len(aged), intel_main.AGE_SUBJECTS)

    def test_the_set_file_and_the_judgement_files_agree(self):
        files = sorted(path.stem for path in JUDGEMENTS.glob("*.json"))
        self.assertEqual(files, sorted(self.subjects))

    def test_no_judgement_crashed(self):
        for subject in self.subjects:
            with self.subTest(subject=subject):
                judgement = json.loads((JUDGEMENTS / f"{subject}.json").read_text(encoding="utf-8"))
                self.assertIn(judgement["verdict"], VERDICTS)
                self.assertEqual(judgement["subject"], subject)
                self.assertTrue(judgement["checks"])
                self.assertTrue(judgement["reasons"])
                statuses = {check["status"] for check in judgement["checks"]}
                expected = "DENY" if "signal" in statuses else "UNKNOWN" if "unknown" in statuses else "ALLOW"
                self.assertEqual(judgement["verdict"], expected)

    def test_a_replay_rejudges_every_subject_to_the_committed_files_and_the_counts_line(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("urllib.request.urlopen", side_effect=no_network):
            results = intel_main.judge_receivers(self.document, self.store, Path(tmp))
            for result in results:
                committed = (JUDGEMENTS / f"{result['subject']}.json").read_bytes()
                self.assertEqual((Path(tmp) / "judgements" / f"{result['subject']}.json").read_bytes(), committed)
        verdicts = [result["verdict"] for result in results]
        recount = (f"On {len(verdicts)} recorded mainnet receivers the rules returned {verdicts.count('ALLOW')} "
                   f"ALLOW, {verdicts.count('DENY')} DENY, {verdicts.count('UNKNOWN')} Unknown.")
        self.assertEqual(load_counts().receivers_line(), recount)

    def test_a_receiver_without_a_recorded_keys_answer_reads_unknown_on_age(self):
        for entry in self.document["subjects"]:
            receiver = entry["request"]["receiver_id"]
            key = rpc.fixture_key(f"{rpc.NEARBLOCKS_URL}/v1/account/{receiver}/keys", "GET", None)
            if (intel_main.RECEIVER_RPC_DIR / f"{key}.json").exists():
                continue
            with self.subTest(subject=entry["subject"]):
                judgement = json.loads((JUDGEMENTS / f"{entry['subject']}.json").read_text(encoding="utf-8"))
                checks = {check["id"]: check for check in judgement["checks"]}
                self.assertEqual(checks["account_age"]["status"], "unknown")

if __name__ == "__main__":
    unittest.main()
