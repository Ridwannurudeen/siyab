"""The policy pages: the response times have one source, and the pages agree with it.

docs/REPORTING.md and docs/INCIDENTS.md are policy, not code paths, so the rule is an explicit
exemption rather than a claims row: what the pages promise is checked here against
intel/service_times.json, and nothing here asserts that a promise has been met.
"""

import json
import re
import unittest
from pathlib import Path

from intel import records

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
TIMES = json.loads((ROOT / "intel" / "service_times.json").read_text(encoding="utf-8"))
REPORTING = (DOCS / "REPORTING.md").read_text(encoding="utf-8")
INCIDENTS = (DOCS / "INCIDENTS.md").read_text(encoding="utf-8")


def flat(text):
    """One line, so a phrase is not broken by the wrap."""
    return " ".join(text.split())

REQUIRED_KEYS = ("hours", "acknowledge_report", "decide_report", "acknowledge_appeal",
                 "decide_appeal", "urgent_block_acknowledge", "measured", "published")


class TimesTest(unittest.TestCase):
    def test_the_time_file_holds_every_required_key_and_nothing_unknown(self):
        self.assertEqual(sorted(set(TIMES) - {"version"}), sorted(REQUIRED_KEYS))

    def test_every_published_time_appears_verbatim_in_the_policy_page(self):
        for key in REQUIRED_KEYS:
            with self.subTest(key=key):
                self.assertIn(TIMES[key], flat(REPORTING))

    def test_the_policy_page_has_no_time_the_file_does_not_hold(self):
        for value in re.findall(r"\b\d+\s+business (?:day|days|hours?)\b", REPORTING):
            with self.subTest(value=value):
                self.assertIn(value, TIMES.values())

    def test_the_policy_page_says_it_is_policy(self):
        self.assertIn("This document is policy, not a description of code", flat(REPORTING))
        self.assertIn("This document is policy and process", flat(INCIDENTS))

    def test_the_policy_page_calls_a_verdict_advisory_and_never_a_guarantee(self):
        self.assertIn("advisory", flat(REPORTING))
        self.assertIn("an assurance that we will catch", flat(REPORTING))
        self.assertNotIn("guarantees that", REPORTING)


class AgreementTest(unittest.TestCase):
    def test_both_pages_use_the_states_the_model_defines(self):
        for page, text in (("REPORTING.md", REPORTING), ("INCIDENTS.md", INCIDENTS)):
            for state in records.STATES:
                with self.subTest(page=page, state=state):
                    self.assertIn(f"`{state}`", flat(text))

    def test_the_pages_name_the_kinds_and_outcomes_from_the_shared_file(self):
        for value in records.KINDS + records.OUTCOMES:
            with self.subTest(value=value):
                self.assertIn(f"`{value}`", flat(REPORTING + INCIDENTS))

    def test_the_kind_that_only_asks_is_named_as_such(self):
        self.assertIn("`social` only asks", flat(REPORTING))
        self.assertIn("`social`", flat(INCIDENTS))

    def test_a_contested_report_is_not_a_switch(self):
        self.assertIn("A dispute is not a switch", flat(REPORTING))


class SubjectTest(unittest.TestCase):
    def test_both_pages_say_a_subject_is_never_a_victim(self):
        self.assertIn("Subjects are not victims", flat(INCIDENTS))
        self.assertIn("never holds the victim", flat(INCIDENTS))
        self.assertIn("subject", REPORTING)

    def test_the_incident_page_forbids_an_unverified_attribution(self):
        self.assertIn("unverified", flat(INCIDENTS).lower())
        self.assertIn("we do not publish a subject", flat(INCIDENTS))

    def test_the_policy_says_a_refusal_is_not_a_claim_of_crime(self):
        self.assertIn("not a claim that the payee has committed a crime", flat(REPORTING))
        self.assertIn("as proof that the other side was at fault", flat(INCIDENTS))


class CorrectionTest(unittest.TestCase):
    def test_the_correction_rule_is_supersede_and_never_edit(self):
        self.assertIn("supersedes", flat(REPORTING))
        self.assertIn("supersedes", flat(INCIDENTS))
        self.assertIn("Nothing is edited", flat(INCIDENTS))

    def test_the_pages_require_the_reason_to_be_readable(self):
        self.assertIn("readable reason", flat(REPORTING))
        self.assertIn("readable reasons", flat(INCIDENTS))

    def test_the_takedown_section_refuses_a_quiet_delete(self):
        self.assertIn("quietly drop a record", flat(REPORTING))
