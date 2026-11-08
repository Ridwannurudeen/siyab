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
        self.assertEqual(set(self.times["hours"]), {"acknowledge", "correction", "decide", "dispute_decision"})
        for step, hours in self.times["hours"].items():
            with self.subTest(step=step):
                self.assertIs(type(hours), int)
                self.assertGreater(hours, 0)

    def test_the_correction_clock_names_confirmation_and_publication(self):
        row = next(line for line in self.policy.splitlines() if line.startswith("| `correction` |"))
        self.assertIn("72 hours", row)
        self.assertIn("dated `docs/LOG.md` line that records confirmation", row)
        self.assertIn("correcting transaction or commit", row)

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

    def test_record_page_names_its_validator_and_renderer_and_keeps_the_store_unimplemented(self):
        body = self.pages["RECORD"].split("\n\n", maxsplit=1)[1]
        first_paragraph = body.split("\n\n", maxsplit=1)[0]
        self.assertIn("format specification", first_paragraph)
        self.assertIn("validator, reader and writer are in `intel/records.py`", first_paragraph)
        self.assertIn("`python -m intel record-check`", first_paragraph)
        self.assertIn("`python -m intel render --out demo/site` writes valid incident records", first_paragraph)
        self.assertIn("no record store exists", first_paragraph)
        self.assertIn("two committed records are synthetic", first_paragraph)


    def test_report_docs_do_not_repeat_pre_merge_claims(self):
        docs = ROOT / "docs"
        stale_intel = re.compile(
            r"(?:not on main)[\s\S]{0,120}intel/reports\.py|"
            r"intel/reports\.py[\s\S]{0,120}(?:not on main)",
            re.IGNORECASE,
        )
        stale_signer = re.compile(
            r"(?:signs as `OWNER_ACCOUNT_ID`)[\s\S]{0,120}report\.sh|"
            r"report\.sh[\s\S]{0,120}(?:signs as `OWNER_ACCOUNT_ID`)",
            re.IGNORECASE,
        )
        for path in sorted(docs.rglob("*.md")):
            if path in {docs / "LOG.md", docs / "DECISIONS.md"}:
                continue
            page = path.read_text(encoding="utf-8")
            with self.subTest(page=path.relative_to(ROOT)):
                self.assertNotIn("until the rules hook merges", page.casefold())
                self.assertIsNone(stale_intel.search(page))
                self.assertIsNone(stale_signer.search(page))

    def test_report_pages_say_pending_is_a_count_only(self):
        paths = (
            "README.md",
            "docs/CLAIMS.md",
            "docs/INCIDENTS.md",
            "docs/LADDER.md",
            "docs/REPORTING.md",
            "docs/REPORTS.md",
            "docs/TESTNET.md",
            "docs/VERDICT-RECORD.md",
            "docs/spec/report-v1.md",
        )
        for relative in paths:
            page = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(page=relative):
                self.assertRegex(page, r"(?i)pending[^\n]{0,120}count only")

    def test_reports_docs_match_the_deployed_reports_version(self):
        build_record = (ROOT / "res" / "BUILD-reports.md").read_text(
            encoding="utf-8"
        )
        # While a new build waits for its deploy, the Deployed line says pending and the code on testnet is
        # the one the "Previous deploy" line names.
        if re.search(r"^Deployed: pending", build_record, re.MULTILINE):
            deployed_match = re.search(
                r"^Previous deploy, version ([0-9]+(?:\.[0-9]+){2}) ",
                build_record,
                re.MULTILINE,
            )
        else:
            deployed_match = re.search(
                r"^Deployed:.*\bversion ([0-9]+(?:\.[0-9]+){2}),",
                build_record,
                re.MULTILINE,
            )
        self.assertIsNotNone(deployed_match)
        deployed_version = deployed_match.group(1)

        claim = re.compile(
            r"testnet runs deployed Reports version \x60([^\x60]+)\x60",
            re.IGNORECASE,
        )
        paths = (
            "docs/REPORTING.md",
            "docs/REPORTS.md",
            "docs/RUNBOOK.md",
            "docs/UPGRADES.md",
            "docs/spec/report-v1.md",
        )
        for relative in paths:
            page = (ROOT / relative).read_text(encoding="utf-8")
            versions = claim.findall(page)
            with self.subTest(page=relative):
                self.assertTrue(versions)
                self.assertEqual(versions, [deployed_version] * len(versions))

        testnet = (ROOT / "docs" / "TESTNET.md").read_text(encoding="utf-8")
        reports_row = re.search(
            r"^\| Reports \|[^\n]*?\bversion ([0-9]+(?:\.[0-9]+){2})",
            testnet,
            re.MULTILINE,
        )
        self.assertIsNotNone(reports_row)
        self.assertEqual(reports_row.group(1), deployed_version)

    def test_report_spec_names_the_manifest_source_version(self):
        manifest = (ROOT / "contracts" / "reports" / "Cargo.toml").read_text(
            encoding="utf-8"
        )
        version_match = re.search(
            r'^version\s*=\s*"([^"]+)"$', manifest, re.MULTILINE
        )
        self.assertIsNotNone(version_match)
        spec = (ROOT / "docs" / "spec" / "report-v1.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"source crate version `{version_match.group(1)}`", spec)
        self.assertIn("contracts/reports/Cargo.toml", spec)


if __name__ == "__main__":
    unittest.main()
