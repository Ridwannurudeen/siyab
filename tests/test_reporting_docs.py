"""docs/REPORTING.md and docs/service_times.json: the published service times are one set of numbers."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "docs" / "REPORTING.md"
TIMES = ROOT / "docs" / "service_times.json"
ROW = re.compile(r"^\| `([a-z_]+)` \| ([0-9]+) hours \|", re.MULTILINE)
HOURS = re.compile(r"\b([0-9]+) hours?\b")
PROSE_TIME = re.compile(r"`([a-z_]+)` step is ([0-9]+) hours")


def prose_times_match(policy, expected):
    prose = "\n".join(line for line in policy.splitlines() if not line.lstrip().startswith("|"))
    pairs = PROSE_TIME.findall(prose)
    return (
        len(HOURS.findall(prose)) == len(pairs)
        and len(pairs) == len(expected)
        and {step: int(hours) for step, hours in pairs} == expected
    )


class ServiceTimesTest(unittest.TestCase):
    def setUp(self):
        self.policy = POLICY.read_text(encoding="utf-8")
        self.times = json.loads(TIMES.read_text(encoding="utf-8"))

    def test_the_json_parses_with_whole_hours(self):
        self.assertEqual(set(self.times), {"version", "policy", "hours"})
        self.assertEqual(self.times["version"], 1)
        self.assertEqual(self.times["policy"], "docs/REPORTING.md")
        self.assertEqual(set(self.times["hours"]), {"acknowledge", "decide", "dispute_decision"})
        for step, hours in self.times["hours"].items():
            with self.subTest(step=step):
                self.assertIs(type(hours), int)
                self.assertGreater(hours, 0)

    def test_the_policy_table_equals_the_json(self):
        table = {step: int(hours) for step, hours in ROW.findall(self.policy)}
        self.assertEqual(table, self.times["hours"])

    def test_every_time_the_policy_states_is_a_json_value(self):
        stated = {int(hours) for hours in HOURS.findall(self.policy)}
        self.assertEqual(stated, set(self.times["hours"].values()))

    def test_each_prose_service_time_matches_its_json_step(self):
        self.assertTrue(prose_times_match(self.policy, self.times["hours"]))

    def test_a_prose_time_assigned_to_the_wrong_step_fails(self):
        fixture = (
            "The `acknowledge` step is 72 hours; "
            "the `decide` step is 72 hours; "
            "the `dispute_decision` step is 168 hours."
        )
        self.assertFalse(prose_times_match(fixture, self.times["hours"]))

    def test_the_policy_cites_the_json(self):
        self.assertIn("`docs/service_times.json`", self.policy)


class ReportClaimsTest(unittest.TestCase):
    def setUp(self):
        self.contract = (ROOT / "contracts" / "reports" / "src" / "lib.rs").read_text(encoding="utf-8")
        self.pages = {
            "REPORTING": POLICY.read_text(encoding="utf-8"),
            "INCIDENTS": (ROOT / "docs" / "INCIDENTS.md").read_text(encoding="utf-8"),
            "RECORD": (ROOT / "docs" / "RECORD.md").read_text(encoding="utf-8"),
            "LADDER": (ROOT / "docs" / "LADDER.md").read_text(encoding="utf-8"),
            "REPORTS": (ROOT / "docs" / "REPORTS.md").read_text(encoding="utf-8"),
        }

    def test_verdict_pages_name_the_reports_switch_used_by_the_code(self):
        for name in ("REPORTING", "INCIDENTS", "LADDER"):
            with self.subTest(page=name):
                self.assertIn("REPORTS_ACCOUNT_ID", self.pages[name])

        rules = (ROOT / "gate" / "rules.py").read_text(encoding="utf-8")
        self.assertIn("if reports_account:", rules)
        self.assertIn('"subject_summary"', rules)
        self.assertIn("reports.effect", rules)
        for path in (ROOT / "gate" / "__main__.py", ROOT / "mcp" / "server.py"):
            with self.subTest(source=path.name):
                self.assertIn('os.environ.get("REPORTS_ACCOUNT_ID")', path.read_text(encoding="utf-8"))
        agent = (ROOT / "gate" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("reports_account=reports_account", agent)

    def test_documented_deposit_equals_the_contract_constant(self):
        match = re.search(r"^pub const REPORT_DEPOSIT: u128 = ([0-9_]+);$", self.contract, re.MULTILINE)
        self.assertIsNotNone(match)
        deposit = int(match.group(1).replace("_", ""))
        whole, fraction = divmod(deposit, 10**24)
        near_amount = f"{whole}.{fraction:024d}".rstrip("0")

        for name in ("REPORTING", "LADDER", "REPORTS"):
            with self.subTest(page=name):
                self.assertIn(near_amount, self.pages[name])
        for name in ("REPORTING", "REPORTS"):
            with self.subTest(page=name):
                self.assertIn(str(deposit), self.pages[name])

    def test_every_documented_reports_refusal_exists_in_the_contract(self):
        documented = {
            code
            for page in self.pages.values()
            for code in re.findall(r"REFUSED:([a-z_]+)", page)
        }
        implemented = set(re.findall(r"REFUSED:([a-z_]+)", self.contract))
        self.assertTrue(documented)
        self.assertEqual(documented - implemented, set())

    def test_report_pages_do_not_claim_reports_change_no_verdict(self):
        for name, page in self.pages.items():
            with self.subTest(page=name):
                self.assertNotRegex(page.lower(), r"\breports? changes no verdict\b")
                self.assertNotIn("until that rules hook merges", page.lower())

    def test_incidents_page_documents_the_current_report_read(self):
        incidents = self.pages["INCIDENTS"]
        for claim in ("REPORTS_ACCOUNT_ID", "subject_summary", "gate/rules.py", "intel/reports.py"):
            with self.subTest(claim=claim):
                self.assertIn(claim, incidents)

    def test_record_page_marks_the_format_as_unimplemented_in_its_first_paragraph(self):
        body = self.pages["RECORD"].split("\n\n", maxsplit=1)[1]
        first_paragraph = body.split("\n\n", maxsplit=1)[0]
        self.assertIn("format specification", first_paragraph)
        self.assertIn("no validator, store, reader, writer or renderer for it yet", first_paragraph)


if __name__ == "__main__":
    unittest.main()
