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


CLEAN = {"accepted": 0, "pending": 0, "contested": 0, "latest_accepted_id": None,
         "deny": False}


def row(accepted=0, pending=0, contested=0, latest="0" * 64, deny=False):
    return {"accepted": accepted, "pending": pending, "contested": contested,
            "latest_accepted_id": latest if accepted else None, "deny": deny}


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
        self.assertEqual(records.verdict_for(row(accepted=1, deny=True)), "DENY")

    def test_an_accepted_social_report_asks(self):
        self.assertEqual(records.verdict_for(row(accepted=1)), "UNKNOWN")

    def test_pending_and_contested_without_a_deny_kind_only_ask(self):
        self.assertEqual(records.verdict_for(row(pending=1)), "UNKNOWN")
        self.assertEqual(records.verdict_for(row(contested=1)), "UNKNOWN")

    def test_a_subject_with_nothing_recorded_has_no_label(self):
        self.assertIsNone(records.verdict_for(CLEAN))

    def test_an_ask_has_no_effect_on_an_allowlisted_payee(self):
        for summary_row in (row(pending=1), row(accepted=1)):
            with self.subTest(summary=summary_row):
                self.assertIsNone(records.verdict_for(summary_row, allowlisted=True))

    def test_a_contested_report_keeps_its_deny_and_the_allowlist_does_not_lift_it(self):
        self.assertEqual(records.verdict_for(row(contested=1, deny=True)), "DENY")
        self.assertEqual(records.verdict_for(row(contested=1, deny=True), allowlisted=True), "DENY")

    def test_a_contested_social_report_asks_and_the_allowlist_does_not_lift_it(self):
        self.assertEqual(records.verdict_for(row(contested=1)), "UNKNOWN")
        self.assertEqual(records.verdict_for(row(contested=1), allowlisted=True), "UNKNOWN")

    def test_deny_beats_unknown_which_beats_no_effect(self):
        self.assertEqual(records.verdict_for(row(accepted=1, pending=3, deny=True)), "DENY")
        self.assertEqual(records.verdict_for(row(contested=1, pending=3, deny=True)), "DENY")
        self.assertEqual(records.verdict_for(row(accepted=1, pending=3)), "UNKNOWN")
        self.assertEqual(records.verdict_for(row(pending=3)), "UNKNOWN")
        self.assertIsNone(records.verdict_for(CLEAN))

    def test_a_deny_still_applies_to_an_allowlisted_payee(self):
        self.assertEqual(records.verdict_for(row(accepted=1, deny=True), allowlisted=True), "DENY")

    def test_an_unreadable_summary_is_never_a_pass_and_the_allowlist_does_not_forgive_it(self):
        broken = [None, {}, CLEAN | {"extra": 1}, row(accepted=-1), row(accepted=True),
                  row(accepted=1, latest="abc"), row(accepted=1, latest=None), row(pending="1"),
                  row(deny="yes"), row(deny=0), row(deny=None), row(deny=True)]
        for summary_row in broken:
            with self.subTest(summary=summary_row):
                self.assertEqual(records.verdict_for(summary_row), "UNKNOWN")
                self.assertEqual(records.verdict_for(summary_row, allowlisted=True), "UNKNOWN")

    def test_a_summary_whose_latest_id_disagrees_with_its_accepted_count_never_passes(self):
        """An allowlisted payee must not pass on a summary the contract could not have produced."""
        disagreed = [{"accepted": 1, "pending": 0, "contested": 0,
                      "latest_accepted_id": None, "deny": False},
                     {"accepted": 0, "pending": 0, "contested": 0,
                      "latest_accepted_id": "a" * 64, "deny": False},
                     {"accepted": 1, "pending": 0, "contested": 0,
                      "latest_accepted_id": None, "deny": True}]
        for summary_row in disagreed:
            with self.subTest(summary=summary_row):
                self.assertEqual(records.verdict_for(summary_row), "UNKNOWN")
                self.assertEqual(records.verdict_for(summary_row, allowlisted=True), "UNKNOWN")

    def test_a_summary_with_several_accepted_records_and_their_id_still_reads(self):
        """The count check rejects only a disagreement, not a count above one."""
        self.assertEqual(records.verdict_for(row(accepted=3)), "UNKNOWN")
        self.assertIsNone(records.verdict_for(row(accepted=3), allowlisted=True))

    def test_the_summary_counts_follow_the_records_held(self):
        holdings = [load("injected-collector.json"), load("lookalike-pending.json"),
                    load("lookalike-contested.json"), load("social-accepted.json")]
        built = records.summary(holdings)
        self.assertEqual(built["accepted"], 2)
        self.assertEqual(built["pending"], 1)
        self.assertEqual(built["contested"], 1)
        self.assertTrue(built["deny"])
        self.assertIn(built["latest_accepted_id"],
                      {record["record_id"] for record in holdings if record["state"] == "accepted"})

    def test_an_earlier_deny_kind_survives_a_later_accepted_social_report(self):
        """The case the ladder exists for: the latest accepted record is not the whole story."""
        exploit = load("injected-collector.json")
        social = copy.deepcopy(load("social-accepted.json"))
        social["reports"] = [{"seq": max(entry["seq"] for entry in exploit["reports"]) + 5,
                              "sha256": social["reports"][0]["sha256"]}]
        social["record_id"] = records.record_id(social)
        built = records.summary([exploit, social])
        self.assertEqual(built["latest_accepted_id"], social["record_id"])
        self.assertEqual(built["accepted"], 2)
        self.assertTrue(built["deny"])
        self.assertEqual(records.verdict_for(built), "DENY")

    def test_a_subject_with_only_social_reports_asks(self):
        social = load("social-accepted.json")
        built = records.summary([social])
        self.assertFalse(built["deny"])
        self.assertEqual(records.verdict_for(built), "UNKNOWN")

    def test_a_deny_kind_that_is_not_accepted_or_contested_does_not_set_the_bit(self):
        for state in ("pending", "rejected", "withdrawn", "overturned"):
            record = copy.deepcopy(load("injected-collector.json"))
            record["state"] = state
            record["record_id"] = records.record_id(record)
            with self.subTest(state=state):
                built = records.summary([record])
                self.assertFalse(built["deny"])
                self.assertNotEqual(records.verdict_for(built), "DENY")

    def test_a_contested_record_holds_only_the_deny_it_had_when_accepted(self):
        """A dispute preserves an answer. It cannot create the block it was filed against."""
        source = load("lookalike-contested.json")
        accepted, contested = source["moderation"]
        times = ("2026-11-05T15:05:00Z", "2026-11-05T15:10:00Z")
        for decisions, expected in ((("accepted", "contested"), True),
                                    (("rejected", "contested"), False),
                                    (("contested",), False)):
            with self.subTest(decisions=decisions):
                record = copy.deepcopy(source)
                record["moderation"] = [
                    {"decision": decision, "decided_at": times[index],
                     "reason_sha256": (accepted if decision == "accepted"
                                       else contested)["reason_sha256"],
                     "reason": f"a {decision} decision"}
                    for index, decision in enumerate(decisions)]
                record["record_id"] = records.record_id(record)
                built = records.summary([record])
                self.assertEqual(built["contested"], 1)
                self.assertIs(built["deny"], expected)
                self.assertEqual(records.verdict_for(built), "DENY" if expected else "UNKNOWN")

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

    def test_the_url_limit_is_measured_in_bytes_not_characters(self):
        """512 multibyte characters are 1024 bytes, and 512 of those characters must be refused."""
        edited = copy.deepcopy(self.record)
        edited["evidence"][0]["url"] = "https://example.com/" + "é" * 250
        self.assertLessEqual(len(edited["evidence"][0]["url"]), 512)
        self.assertGreater(len(edited["evidence"][0]["url"].encode("utf-8")), 512)
        self.assert_refused(self.reseal(edited), "evidence url")

    def test_a_url_of_exactly_512_bytes_is_accepted(self):
        edited = copy.deepcopy(self.record)
        url = "https://example.com/" + "a" * (512 - len("https://example.com/"))
        self.assertEqual(len(url.encode("utf-8")), 512)
        edited["evidence"][0]["url"] = url
        self.assertIs(records.validate(self.reseal(edited)), edited)

    def test_a_page_source_url_obeys_the_same_byte_limit(self):
        edited = copy.deepcopy(self.record)
        edited["timeline"] = edited["timeline"] + [
            {"event": "first_public_report", "at": "2026-11-05T15:30:00Z",
             "source": {"type": "page", "ref": "https://example.com/" + "é" * 250}}]
        self.assertLessEqual(len(edited["timeline"][-1]["source"]["ref"]), 512)
        self.assert_refused(self.reseal(edited), "source ref")

    def test_a_report_body_url_obeys_the_same_byte_limit(self):
        body = json.loads(VECTOR.read_text(encoding="utf-8"))["report"]
        body["evidence_url"] = "https://example.com/" + "é" * 250
        with self.assertRaises(records.RecordError) as caught:
            records.report_body(body)
        self.assertIn("evidence_url", str(caught.exception))

    def test_a_decision_that_is_not_in_the_timeline_is_refused(self):
        edited = copy.deepcopy(self.record)
        del edited["timeline"][1]
        self.assert_refused(self.reseal(edited), "decision at")

    def test_a_state_that_disagrees_with_the_last_decision_is_refused(self):
        edited = copy.deepcopy(self.record)
        edited["state"] = "accepted"
        self.assert_refused(self.reseal(edited), "not the last decision")

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
                         ("accepted", "pending", "contested", "latest_accepted_id", "deny"))

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
