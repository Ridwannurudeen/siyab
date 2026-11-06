"""The platform mapping: our three answers in NEAR's words, and the page that states them.

The rule this file protects: a verdict must not be presented as an integration we have not built.
The mapping is a translation, the page names what is missing, and both have to agree.
"""

import unittest
from pathlib import Path

from gate import platform
from intel import records

ROOT = Path(__file__).resolve().parent.parent


class DecisionTest(unittest.TestCase):
    def test_the_three_literals_map_to_the_tracks_words(self):
        self.assertEqual(platform.decide("ALLOW"), "allow")
        self.assertEqual(platform.decide("UNKNOWN"), "warn")
        self.assertEqual(platform.decide("DENY"), "block")

    def test_a_literal_that_is_not_ours_has_no_decision(self):
        for literal in ("ALLOWED", "", None, "deny", 7):
            with self.subTest(literal=literal):
                self.assertIsNone(platform.decide(literal))

    def test_every_decision_names_a_platform_field(self):
        for _, literal in platform.ROWS:
            with self.subTest(literal=literal):
                self.assertIn(platform.view(literal)["decision"], platform.POLICY)

    def test_a_closed_record_has_no_effect(self):
        self.assertEqual(platform.view(None)["decision"], "no effect")

    def test_the_table_lists_four_rows_in_order(self):
        self.assertEqual([row["decision"] for row in platform.table()],
                         ["allow", "warn", "block", "no effect"])


class AgreementTest(unittest.TestCase):
    def test_the_view_never_disagrees_with_the_one_function_that_judges(self):
        clean = {"accepted": 0, "pending": 0, "contested": 0, "latest_accepted_id": None,
                 "latest_accepted_kind": None, "deny": False}
        deny_row = {"accepted": 1, "pending": 0, "contested": 0, "latest_accepted_id": 7,
                    "latest_accepted_kind": "exploit", "deny": True}
        social_row = {"accepted": 1, "pending": 0, "contested": 0, "latest_accepted_id": 7,
                      "latest_accepted_kind": "social", "deny": False}
        contested_row = {"accepted": 1, "pending": 0, "contested": 1, "latest_accepted_id": 9,
                         "latest_accepted_kind": "phishing", "deny": False}
        ask_row = {"accepted": 0, "pending": 1, "contested": 0, "latest_accepted_id": None,
                   "latest_accepted_kind": None, "deny": False}
        cases = [
            (clean, False, "no effect"),
            (deny_row, False, "block"),
            (deny_row, True, "block"),
            (social_row, False, "warn"),
            (social_row, True, "no effect"),
            (ask_row, False, "warn"),
            (ask_row, True, "no effect"),
            (contested_row, False, "warn"),
            (contested_row, True, "warn"),
            (None, False, "warn"),
        ]
        for summary, allowlisted, expected in cases:
            with self.subTest(summary=summary, allowlisted=allowlisted):
                view = platform.for_summary(summary, allowlisted=allowlisted)
                self.assertEqual(view["decision"], expected)
                self.assertEqual(view["decision"],
                                 platform.decide(records.verdict_for(summary, allowlisted=allowlisted))
                                 or "no effect")


class PageTest(unittest.TestCase):
    def setUp(self):
        self.page = (ROOT / "docs" / "PLATFORM.md").read_text(encoding="utf-8")
        self.flat = " ".join(self.page.split())

    def table_rows(self):
        """The three-column mapping rows of the page, as cells, in the order they are printed."""
        rows = []
        for line in self.page.splitlines():
            line = line.strip()
            if not line.startswith("|") or not line.endswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) == 3 and cells[1] in platform.POLICY:
                rows.append(cells)
        return rows

    def test_each_row_binds_its_verdict_to_its_own_policy_line(self):
        """A page that merely contains every sentence is not a table: each row must carry its own."""
        rows = self.table_rows()
        self.assertEqual([cells[1] for cells in rows], [decision for decision, _ in platform.ROWS])
        for verdict_cell, decision, policy_cell in rows:
            with self.subTest(decision=decision):
                literal = dict(platform.ROWS)[decision]
                expected = f"`{literal}`" if literal else "no verdict"
                self.assertEqual(verdict_cell, expected)
                self.assertEqual(policy_cell, platform.POLICY[decision])

    def test_the_page_keeps_the_policy_lines_and_the_source(self):
        for decision in platform.POLICY.values():
            with self.subTest(decision=decision):
                self.assertIn(decision, self.flat)
        self.assertIn(platform.SOURCE, self.flat)

    def test_the_page_names_what_is_not_built(self):
        for words in ("not built", "OutLayer", "attestation"):
            with self.subTest(words=words):
                self.assertIn(words, self.flat)

    def test_the_page_does_not_claim_a_tee(self):
        self.assertNotIn("we run in a TEE", self.flat)
        self.assertIn("does not run in a TEE", self.flat)
