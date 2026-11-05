"""The incident and attempt record: the rules of docs/RECORD.md, over the examples in fixtures/records/.

The safety rules: an accepted report denies only for the five kinds that describe an exploit of
code, an accepted social report asks instead, pending and contested only ask, an allowlisted payee
is not stalled by an ask, and anything unreadable is never a pass.
"""

import copy
import json
import unittest
from pathlib import Path

from intel import records

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = sorted(path for path in (ROOT / "fixtures" / "records").glob("*.json")
                 if "record_id" in json.loads(path.read_text(encoding="utf-8")))
VECTOR = ROOT / "fixtures" / "records" / "report-body-vector.json"


def load(name):
    return json.loads((ROOT / "fixtures" / "records" / name).read_text(encoding="utf-8"))


CLEAN = {"accepted": 0, "pending": 0, "contested": 0, "latest_accepted_id": None}


def row(accepted=0, pending=0, contested=0, latest="0" * 64):
    return {"accepted": accepted, "pending": pending, "contested": contested,
            "latest_accepted_id": latest if accepted else None}


class FixtureTest(unittest.TestCase):
    def test_the_examples_are_valid_records(self):
        self.assertEqual(len(FIXTURES), 4)
        for path in FIXTURES:
            with self.subTest(path=path.name):
                record = json.loads(path.read_text(encoding="utf-8"))
                self.assertIs(records.validate(record), record)

    def test_every_example_carries_evidence_and_a_readable_moderation_reason(self):
        for path in FIXTURES:
            record = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path=path.name):
                self.assertTrue(record["evidence"])
                for entry in record["evidence"]:
                    self.assertTrue(entry["url"].startswith("https://"))
                    self.assertRegex(entry["fetched_at"], r"Z$")
                for entry in record["moderation"]:
                    self.assertTrue(entry["reason"].strip())


class EnumTest(unittest.TestCase):
    def test_the_vocabulary_file_holds_exactly_the_three_lists(self):
        enums = json.loads((ROOT / "intel" / "record_enums.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(enums), ["kinds", "outcomes", "states"])
        self.assertEqual(tuple(enums["kinds"]), records.KINDS)
        self.assertEqual(tuple(enums["outcomes"]), records.OUTCOMES)
        self.assertEqual(tuple(enums["states"]), records.STATES)

    def test_the_values_are_the_agreed_ones(self):
        self.assertEqual(records.KINDS, ("scam", "theft", "phishing", "exploit", "key_compromise", "social"))
        self.assertEqual(records.OUTCOMES, ("loss", "attempt", "near_miss", "unknown"))
        self.assertEqual(records.STATES,
                         ("pending", "accepted", "rejected", "withdrawn", "contested", "overturned"))

    def test_the_two_axes_do_not_overlap(self):
        for word in ("loss", "attempt", "near_miss", "unknown"):
            with self.subTest(word=word):
                self.assertIn(word, records.OUTCOMES)
                self.assertNotIn(word, records.KINDS)

    def test_only_five_kinds_may_deny(self):
        self.assertEqual(records.DENY_KINDS, ("scam", "theft", "phishing", "exploit", "key_compromise"))
        self.assertNotIn("social", records.DENY_KINDS)


class VerdictTest(unittest.TestCase):
    def test_an_accepted_report_denies_for_the_five_kinds(self):
        for kind in records.DENY_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(records.verdict_for(row(accepted=1), kind), "DENY")

    def test_an_accepted_social_report_asks(self):
        self.assertEqual(records.verdict_for(row(accepted=1), "social"), "UNKNOWN")

    def test_pending_and_contested_only_ask(self):
        self.assertEqual(records.verdict_for(row(pending=1)), "UNKNOWN")
        self.assertEqual(records.verdict_for(row(contested=1)), "UNKNOWN")

    def test_a_subject_with_nothing_recorded_has_no_label(self):
        self.assertIsNone(records.verdict_for(CLEAN))

    def test_an_ask_has_no_effect_on_an_allowlisted_payee(self):
        for summary_row in (row(pending=1), row(accepted=1)):
            with self.subTest(summary=summary_row):
                kind = "social" if summary_row["accepted"] else None
                self.assertIsNone(records.verdict_for(summary_row, kind, allowlisted=True))

    def test_a_contested_report_keeps_its_deny_and_the_allowlist_does_not_lift_it(self):
        for kind in records.DENY_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(records.verdict_for(row(contested=1), kind), "DENY")
                self.assertEqual(records.verdict_for(row(contested=1), kind, allowlisted=True), "DENY")

    def test_a_contested_social_report_asks_and_the_allowlist_does_not_lift_it(self):
        self.assertEqual(records.verdict_for(row(contested=1), "social"), "UNKNOWN")
        self.assertEqual(records.verdict_for(row(contested=1), "social", allowlisted=True), "UNKNOWN")

    def test_deny_beats_unknown_which_beats_no_effect(self):
        self.assertEqual(records.verdict_for(row(accepted=1, pending=3), "theft"), "DENY")
        self.assertEqual(records.verdict_for(row(contested=1, pending=3), "theft"), "DENY")
        self.assertEqual(records.verdict_for(row(accepted=1, pending=3), "social"), "UNKNOWN")
        self.assertEqual(records.verdict_for(row(pending=3), None), "UNKNOWN")
        self.assertIsNone(records.verdict_for(CLEAN, "theft"))

    def test_a_deny_still_applies_to_an_allowlisted_payee(self):
        self.assertEqual(records.verdict_for(row(accepted=1), "theft", allowlisted=True), "DENY")

    def test_an_unreadable_summary_is_never_a_pass_and_the_allowlist_does_not_forgive_it(self):
        broken = [None, {}, CLEAN | {"extra": 1}, row(accepted=-1), row(accepted=True),
                  row(accepted=1, latest="abc"), row(pending="1")]
        for summary_row in broken:
            with self.subTest(summary=summary_row):
                self.assertEqual(records.verdict_for(summary_row, "theft"), "UNKNOWN")
                self.assertEqual(records.verdict_for(summary_row, "theft", allowlisted=True), "UNKNOWN")

    def test_an_accepted_summary_without_a_known_kind_is_unreadable(self):
        for kind in (None, "", "rug", "SOCIAL"):
            with self.subTest(kind=kind):
                self.assertEqual(records.verdict_for(row(accepted=1), kind), "UNKNOWN")

    def test_the_summary_counts_follow_the_records_held(self):
        holdings = [load("injected-collector.json"), load("lookalike-pending.json"),
                    load("lookalike-contested.json"), load("social-accepted.json")]
        built = records.summary(holdings)
        self.assertEqual(built["accepted"], 2)
        self.assertEqual(built["pending"], 1)
        self.assertEqual(built["contested"], 1)
        self.assertIn(built["latest_accepted_id"],
                      {record["record_id"] for record in holdings if record["state"] == "accepted"})

    def test_the_latest_accepted_record_is_the_greatest_report_seq(self):
        accepted = [record for record in (load("injected-collector.json"), load("social-accepted.json"))
                    if record["state"] == "accepted"]
        self.assertEqual(records.summary(accepted)["latest_accepted_id"],
                         max(accepted, key=lambda r: max(e["seq"] for e in r["reports"]))["record_id"])

    def test_an_empty_holding_is_a_clean_summary(self):
        self.assertEqual(records.summary([]), CLEAN)


class TransitionTest(unittest.TestCase):
    def test_the_transitions_match_the_spec(self):
        self.assertTrue(records.may_transition("pending", "accepted"))
        self.assertTrue(records.may_transition("accepted", "contested"))
        self.assertTrue(records.may_transition("contested", "overturned"))
        self.assertFalse(records.may_transition("accepted", "pending"))
        self.assertFalse(records.may_transition("withdrawn", "accepted"))
        self.assertFalse(records.may_transition("overturned", "accepted"))

    def test_every_state_is_reachable_and_terminal_where_the_spec_says(self):
        reachable = {after for before in records.TRANSITIONS for after in records.TRANSITIONS[before]}
        self.assertEqual(reachable | {"pending"}, set(records.STATES))
        self.assertEqual([state for state in records.STATES if not records.TRANSITIONS[state]],
                         ["withdrawn", "overturned"])

    def test_there_is_no_corrected_state_because_a_correction_supersedes(self):
        self.assertNotIn("corrected", records.STATES)
        self.assertNotIn("superseded", records.STATES)
        self.assertIn("superseded", records.EVENTS)


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.record = load("lookalike-contested.json")

    def assert_refused(self, record, words):
        with self.assertRaises(records.RecordError) as caught:
            records.validate(record)
        self.assertIn(words, str(caught.exception))

    def reseal(self, record):
        record["record_id"] = records.record_id(record)
        return record

    def test_an_edit_changes_the_id_and_is_refused(self):
        edited = copy.deepcopy(self.record)
        edited["subjects"] = ["another.testnet"]
        self.assert_refused(edited, "record_id")

    def test_a_recomputed_id_accepts_the_edit_and_the_old_bytes_stay_verifiable(self):
        edited = copy.deepcopy(self.record)
        edited["subjects"] = ["another.testnet"]
        self.reseal(edited)
        self.assertIs(records.validate(edited), edited)
        self.assertNotEqual(edited["record_id"], self.record["record_id"])

    def test_a_timeline_that_goes_backwards_is_refused(self):
        edited = copy.deepcopy(self.record)
        edited["timeline"][0]["at"] = "2026-11-05T16:00:00Z"
        self.assert_refused(self.reseal(edited), "append only")

    def test_a_timeline_entry_without_a_source_is_refused(self):
        edited = copy.deepcopy(self.record)
        del edited["timeline"][0]["source"]
        self.assert_refused(self.reseal(edited), "timeline")

    def test_a_record_without_evidence_is_refused(self):
        edited = copy.deepcopy(self.record)
        edited["evidence"] = []
        self.assert_refused(self.reseal(edited), "evidence")

    def test_a_non_https_evidence_url_is_refused(self):
        edited = copy.deepcopy(self.record)
        edited["evidence"][0]["url"] = "http://example.com/evidence"
        self.assert_refused(self.reseal(edited), "evidence url")

    def test_an_over_long_evidence_url_is_refused(self):
        edited = copy.deepcopy(self.record)
        edited["evidence"][0]["url"] = "https://example.com/" + "a" * 600
        self.assert_refused(self.reseal(edited), "evidence url")

    def test_a_moderation_decision_without_readable_text_is_refused(self):
        edited = copy.deepcopy(self.record)
        edited["moderation"][0]["reason"] = "  "
        self.assert_refused(self.reseal(edited), "moderation reason")

    def test_a_moderation_reason_hash_is_required_beside_the_text(self):
        edited = copy.deepcopy(self.record)
        del edited["moderation"][0]["reason_sha256"]
        self.assert_refused(self.reseal(edited), "moderation")

    def test_an_unknown_kind_or_outcome_is_refused(self):
        for field, value in (("kind", "rug"), ("outcome", "loss_and_attempt")):
            with self.subTest(field=field):
                edited = copy.deepcopy(self.record)
                edited[field] = value
                self.assert_refused(self.reseal(edited), field)

    def test_reports_must_be_ordered_and_carry_both_ids(self):
        edited = copy.deepcopy(self.record)
        edited["reports"] = list(reversed(edited["reports"]))
        self.assert_refused(self.reseal(edited), "reports")
        broken = copy.deepcopy(self.record)
        del broken["reports"][0]["seq"]
        self.assert_refused(self.reseal(broken), "reports")

    def test_a_superseding_record_cites_the_earlier_id_and_gets_its_own(self):
        correction = copy.deepcopy(self.record)
        correction["supersedes"] = self.record["record_id"]
        correction["timeline"] = correction["timeline"] + [
            {"event": "superseded", "at": "2026-11-05T16:00:00Z",
             "source": {"type": "internal", "ref": "note-20261105-02"}}]
        self.reseal(correction)
        self.assertIs(records.validate(correction), correction)
        self.assertEqual(correction["supersedes"], self.record["record_id"])
        self.assertNotEqual(correction["record_id"], self.record["record_id"])


class ShapeTest(unittest.TestCase):
    def test_the_fields_are_the_spec_fields_in_order(self):
        self.assertEqual(records.FIELDS, ("version", "record_id", "kind", "outcome", "state", "subjects",
                                          "reports", "evidence", "moderation", "timeline", "supersedes",
                                          "publisher", "observed"))

    def test_the_summary_fields_are_the_bounded_contract_read(self):
        self.assertEqual(records.SUMMARY_FIELDS,
                         ("accepted", "pending", "contested", "latest_accepted_id"))

    def test_the_canonical_bytes_do_not_depend_on_key_order(self):
        record = load("injected-collector.json")
        reordered = {key: record[key] for key in reversed(list(record))}
        self.assertEqual(records.canonical(record), records.canonical(reordered))

    def test_the_committed_line_is_compact_and_keeps_the_field_order(self):
        record = load("injected-collector.json")
        line = records.row_line(record)
        self.assertNotIn('", "', line)
        self.assertNotIn('": ', line)
        self.assertEqual(list(json.loads(line)), list(records.FIELDS))


class ReportBodyTest(unittest.TestCase):
    def setUp(self):
        self.vector = json.loads(VECTOR.read_text(encoding="utf-8"))

    def test_the_shared_vector_reproduces_byte_for_byte(self):
        self.assertEqual(records.report_body(self.vector["report"]).decode("utf-8"),
                         self.vector["canonical"])

    def test_the_shared_vector_hash_is_the_published_one(self):
        self.assertEqual(records.body_sha256(self.vector["report"]), self.vector["sha256"])

    def test_the_body_has_the_six_agreed_fields_and_no_null(self):
        self.assertEqual(tuple(sorted(self.vector["report"])), records.REPORT_BODY_FIELDS)
        for field in records.REPORT_BODY_FIELDS:
            with self.subTest(field=field):
                broken = dict(self.vector["report"])
                broken[field] = None
                with self.assertRaises(records.RecordError):
                    records.report_body(broken)

    def test_a_body_with_an_extra_field_is_refused(self):
        broken = dict(self.vector["report"], memo="hello")
        with self.assertRaises(records.RecordError):
            records.report_body(broken)

    def test_the_body_does_not_import_the_gate(self):
        source = (ROOT / "intel" / "records.py").read_text(encoding="utf-8")
        self.assertNotIn("gate.rules", source)
        self.assertNotIn("gate.verdict", source)
        self.assertNotIn("from gate", source)
        self.assertNotIn("import gate", source)
