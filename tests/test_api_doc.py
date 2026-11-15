"""Contract checks for docs/API.md.

Tests pin response field names/order against an offline verdict.request; check ids against literal evidence.check
calls; Kind rows against emitted near/ft check ids; Tier rows against TIER2_CHECKS and allowlisted/off-allowlist
ft calls; issuer_blacklist only when an issuer table is passed and community_report only when reports_account is
set; CLI/MCP/HTTP issuer selection as issuers_for("mainnet") for Gate ids not ending .testnet and None for
.testnet; outcome/action rows as ALLOW to ask when ask is nonempty, otherwise sign, DENY to refuse, and UNKNOWN
to ask; fixture-backed ALLOW and DENY examples after shortening 64-hex values; and an UNKNOWN example section
that cites no fixture while committed UNKNOWN fixtures are exactly top receiver judgements with that verdict.

The pass, signal and unknown check-table meanings are documentation checked by review, not by a test.
"""

import ast
import inspect
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gate import __main__ as gate_main
from gate import account_check
from gate import http as endpoint
from gate import rules
from mcp import server as mcp_server
from tests.test_verdict import AGENT, VerdictCase

ROOT = Path(__file__).resolve().parent.parent
API_DOC = ROOT / "docs" / "API.md"
RULES = ROOT / "gate" / "rules.py"
FRESHNESS = ROOT / "gate" / "freshness.py"
HEX64 = re.compile(r"[0-9a-f]{64}")
OUTCOMES = ("ALLOW", "DENY", "UNKNOWN")
CHECK_COLUMNS = ("Kind", "Tier", "Enabled by", "Network", "pass", "signal", "unknown")


def section(text, heading):
    """The lines under heading up to the next heading of the same or a higher level."""
    level = heading.split(" ", 1)[0]
    lines = text.splitlines()
    start = lines.index(heading) + 1
    for end in range(start, len(lines)):
        mark = lines[end].split(" ", 1)[0]
        if mark and set(mark) == {"#"} and len(mark) <= len(level):
            return lines[start:end]
    return lines[start:]


def table_rows(lines):
    """The cells of each body row of the first table in lines whose first cell is a backticked name."""
    rows = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("| `") and re.fullmatch(r"`[a-z_]+`", cells[0]):
            rows.append([cells[0].strip("`")] + cells[1:])
    return rows


def doc_fields(text):
    return [row[0] for row in table_rows(section(text, "### Fields"))]


def doc_checks(text):
    """{check id: tier} from the check table."""
    return {row[0]: row[2] for row in table_rows(section(text, "### Check table"))}


def doc_check_rows(text):
    """{check id: {column: cell}} for the documented check table."""
    return {row[0]: dict(zip(CHECK_COLUMNS, row[1:]))
            for row in table_rows(section(text, "### Check table"))}


def replace_check_cell(text, check_id, column, value):
    """Return a copy of the doc with one check-table cell changed."""
    index = 1 + CHECK_COLUMNS.index(column)
    for line in text.splitlines():
        if line.startswith(f"| `{check_id}` |"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            cells[index] = value
            changed = "| " + " | ".join(cells) + " |"
            return text.replace(line, changed, 1)
    raise AssertionError(f"no check-table row for {check_id}")


def kind_problems(text, observed_by_kind):
    """Check that each row names exactly the kinds for which verdict calls emit it."""
    problems = {}
    for check_id, row in doc_check_rows(text).items():
        observed = {kind for kind, ids in observed_by_kind.items() if check_id in ids}
        documented = {kind.strip() for kind in row["Kind"].split(",")}
        if documented != observed:
            problems[check_id] = (documented, observed)
    return problems


def tier_problems(text, allowlisted_ft_ids, off_allowlist_ft_ids):
    """Check that the documented ft tier sets match the allowlisted and off-list verdict calls."""
    rows = doc_check_rows(text)
    tier_one = {check_id for check_id, row in rows.items()
                if "ft" in {kind.strip() for kind in row["Kind"].split(",")} and row["Tier"] == "1"}
    all_ft = {check_id for check_id, row in rows.items()
              if "ft" in {kind.strip() for kind in row["Kind"].split(",")}}
    problems = {}
    if tier_one != allowlisted_ft_ids:
        problems["tier 1"] = (tier_one, allowlisted_ft_ids)
    if all_ft != off_allowlist_ft_ids:
        problems["tier 1 and 2"] = (all_ft, off_allowlist_ft_ids)
    return problems


def enabled_description_problems(text):
    """Check that optional rows describe the inputs which the verdict calls require."""
    rows = doc_check_rows(text)
    issuer = rows["issuer_blacklist"]["Enabled by"].lower()
    report = rows["community_report"]["Enabled by"].lower()
    problems = {}
    if "caller passes" not in issuer or "issuer table" not in issuer:
        problems["issuer_blacklist"] = issuer
    if not all(value in report for value in ("reports_account_id", "unset", "absent")):
        problems["community_report"] = report
    return problems


def network_description_problems(text):
    """Check that the network cell documents the Gate-based issuer selection."""
    network = doc_check_rows(text)["issuer_blacklist"]["Network"]
    required = ("mainnet Gate only", '`issuers_for("mainnet")`', "not ending `.testnet`", "testnet table is empty",
                "gate/__main__.py", "mcp/server.py", "gate/http.py")
    return [] if all(value in network for value in required) else [network]


def action_rows(text):
    """{outcome: documented action relation} from the Outcome and action table."""
    rows = {}
    for line in section(text, "### Outcome and action"):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        outcome = cells[0].strip("`")
        if len(cells) == 3 and outcome in OUTCOMES:
            rows[outcome] = cells[2]
    return rows


def action_problems(text, results):
    """Check verdict.request outcomes and actions against the documented action rows."""
    rows, problems = action_rows(text), []
    for result in results:
        outcome, action = result["outcome"], result["action"]
        if outcome == "ALLOW":
            expected = "ask" if result["ask"] else "sign"
            relation = "`ask` when `ask` is not empty, else `sign`"
        elif outcome == "DENY":
            expected, relation = "refuse", "`refuse`"
        else:
            expected, relation = "ask", "`ask`"
        if action != expected or rows.get(outcome) != relation:
            problems.append((outcome, action, rows.get(outcome), expected, relation))
    return problems


def rules_check_ids(source):
    """Every check id gate/rules.py can emit: the first argument of each evidence.check call."""
    ids = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "check"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "evidence"):
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                raise ValueError(f"evidence.check at line {node.lineno} has no literal check id")
            ids.add(first.value)
    return ids


def rules_version_problems(source):
    problems = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = node.args.posonlyargs + node.args.args
            defaults = list(zip(positional[len(positional) - len(node.args.defaults):], node.args.defaults))
            defaults += list(zip(node.args.kwonlyargs, node.args.kw_defaults))
            for argument, default in defaults:
                if argument.arg == "rules_version" and default is not None and not (
                        isinstance(default, ast.Constant) and type(default.value) is int and default.value == 1):
                    problems.append(node.lineno)
        if isinstance(node, ast.Call):
            name = (node.func.id if isinstance(node.func, ast.Name)
                    else node.func.attr if isinstance(node.func, ast.Attribute) else None)
            for keyword in node.keywords:
                if ((keyword.arg is None and name in ("judge", "check_account"))
                        or (keyword.arg == "rules_version" and not (
                            isinstance(keyword.value, ast.Name) and keyword.value.id == "rules_version"))):
                    problems.append(node.lineno)
    return problems


def shorten(value):
    """The value with every 64 hex run in its strings cut to first8...last8, as the doc prints hashes."""
    if isinstance(value, dict):
        return {key: shorten(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shorten(item) for item in value]
    if isinstance(value, str):
        return HEX64.sub(lambda match: f"{match.group(0)[:8]}...{match.group(0)[-8:]}", value)
    return value


def examples(text):
    """{heading outcome: (cited fixture path or pattern, the JSON block)} from the Examples section."""
    found, outcome, fixture, block, inside = {}, None, None, [], False
    for line in section(text, "## Examples"):
        if line.startswith("### "):
            outcome, fixture = line[4:].strip(), None
        elif line.startswith("Fixture: `"):
            fixture = line.split("`")[1]
        elif line == "```json":
            inside, block = True, []
        elif line == "```" and inside:
            inside = False
            found[outcome] = (fixture, "\n".join(block))
        elif inside:
            block.append(line)
    return found


def fixture_outcome(data):
    """The outcome a fixture records: a tool result's outcome, a response's outcome, or a document's verdict."""
    if isinstance(data.get("result"), dict):
        data = data["result"]
    return data.get("outcome", data.get("verdict"))


def example_problems(text, root):
    """{outcome: what is wrong} for each example that does not match its fixture under root."""
    problems = {}
    for outcome, (fixture, block) in examples(text).items():
        matches = sorted(root.glob(fixture)) if fixture else []
        if outcome not in OUTCOMES:
            problems[outcome] = "the heading is not an outcome"
            continue
        if len(matches) != 1:
            problems[outcome] = f"{fixture} names {len(matches)} files, not one"
            continue
        data = json.loads(matches[0].read_text(encoding="utf-8"))
        if fixture_outcome(data) != outcome:
            problems[outcome] = f"{fixture} records {fixture_outcome(data)}, not {outcome}"
            continue
        recorded = data["result"] if isinstance(data.get("result"), dict) else data
        if json.loads(block) != shorten(recorded):
            problems[outcome] = f"the block is not {fixture} with its hashes shortened"
    return problems


class AskDocumentationTest(unittest.TestCase):
    def test_usage_ceiling_and_versions_match_code(self):
        from intel import ask

        text = "\n".join(section(API_DOC.read_text(encoding="utf-8"), "## Ask Siyab"))
        for phrase in ("at most `2**53`", "output tokens must not exceed the request's `max_tokens`",
                       "canonical complete request's UTF-8 byte length plus 4,096", "highest input rate",
                       "never the reported amount", "after acquiring the database lock"):
            self.assertIn(phrase, text)
        for name in ("ASK_VERSION", "PROMPT_VERSION", "SCHEMA_VERSION"):
            self.assertIn('`' + name + ' = "' + getattr(ask, name) + '"`', text)
        self.assertIn('"ask_version": "' + ask.ASK_VERSION + '"', text)
        self.assertEqual(ask.ASK_VERSION, "13")

    def test_token_quota_refusals_document_attempted_call_reservations(self):
        text = "\n".join(section(API_DOC.read_text(encoding="utf-8"), "## Ask Siyab"))
        for reason in ("user_quota", "team_quota", "spend_cap"):
            row = next(line for line in text.splitlines() if line.startswith("| `" + reason + "` |"))
            self.assertEqual(row.split("|")[3].strip(), "no")
            self.assertEqual(row.split("|")[4].strip(), "none")
            self.assertIn("before the call", row)

    def test_public_ask_requires_sign_in_and_storage_failures_keep_spend(self):
        text = "\n".join(section(API_DOC.read_text(encoding="utf-8"), "## Ask Siyab"))
        self.assertIn("Anonymous reads return `sign_in_required`", text)
        self.assertIn("never read the answer cache", text)
        self.assertNotIn("| `check_failed` |", text)
        self.assertIn("spend after a call remains in the ledger", text)

    def test_question_content_refusals_are_unavailable_answers(self):
        text = "\n".join(section(API_DOC.read_text(encoding="utf-8"), "## Ask Siyab"))
        self.assertIn("Angle brackets or instruction-pattern matches return HTTP 200 `question_refused`", text)
        self.assertNotIn("with no control characters, angle brackets or instruction-pattern match", text)

    def test_selection_schema_and_invalid_output_are_documented(self):
        text = "\n".join(section(API_DOC.read_text(encoding="utf-8"), "## Ask Siyab"))
        for phrase in ("`answerable`, `points` and `gaps`", "`item_id` and `aspect`",
                       "`read`, `unknown` or `would_change`", "at most 6 points", "at most 3 gap codes",
                       "`answerable: false` requires zero points", "`additionalProperties: false`",
                       "unknown id", "duplicate id", "invalid aspect", "`unavailable/invalid_output`",
                       "never cached"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        for code in ("transaction_simulation", "off_chain_identity", "future_state", "intent", "not_covered_by_checks"):
            self.assertIn("`" + code + "`", text)

    def test_invalid_output_documents_cache_and_call_accounting(self):
        text = "\n".join(section(API_DOC.read_text(encoding="utf-8"), "## Ask Siyab"))
        row = next(row for row in table_rows(text.splitlines()) if row[0] == "invalid_output")
        self.assertEqual(row[1], "Invalid cached citations or aspects; response not an object; missing, malformed "
                         "or out-of-bounds usage; missing or non-string `stop_reason` or `text`; "
                         "`stop_reason` other than `end_turn`, `refusal` or `max_tokens`; any selection validation failure")
        self.assertEqual(row[2], "no for invalid cached selections; yes for response failures")
        self.assertEqual(row[3], "none for invalid cached selections; full reservation kept for a non-object "
                         "response or missing, malformed or out-of-bounds usage; otherwise settled from valid usage "
                         "before envelope or selection validation")

    def test_templates_and_evidence_chips_are_documented(self):
        text = "\n".join(section(API_DOC.read_text(encoding="utf-8"), "## Ask Siyab"))
        for phrase in ("Siyab writes every sentence", "selects and orders evidence items", "fixed codes",
                       "never produced or changed by the model", "`TEMPLATES`", "byte-identical to the projection",
                       "`evidence_chip`", "`item_id`", "`fields`", "except `id`", "including uncited checks",
                       "pass records no signal", "signal records a signal",
                       "unknown leaves the check unresolved", "`gate/rules.py`"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_response_example_matches_rendered_chip_shape(self):
        from intel import ask

        text = "\n".join(section(API_DOC.read_text(encoding="utf-8"), "## Ask Siyab"))
        response = "\n".join(section(text, "### Response"))
        example = json.loads(re.search(r"```json\n(.*?)\n```", response, re.S).group(1))
        projection = {"items": [{"id": "check.receiver_exists", "status": "signal",
                                 "detail": "receiver does not exist"}]}
        selection = {"answerable": True, "points": [{"item_id": "check.receiver_exists", "aspect": "read"}],
                     "gaps": []}
        rendered = ask.render_answer(selection, projection)
        for name, value in rendered.items():
            self.assertEqual(example[name], value)

    def test_single_call_reservation_and_cache_timestamp_are_documented(self):
        text = "\n".join(section(API_DOC.read_text(encoding="utf-8"), "## Ask Siyab"))
        self.assertIn("one selection call", text)
        self.assertIn("single call's request bound", text)
        self.assertIn("at most one model call and one reservation", text)
        self.assertIn("Any failure returns unavailable with its reason", text)
        self.assertNotIn("after retry", text)
        self.assertNotIn("call or retry", text)
        self.assertIn("cached `generated_at`", text)
        self.assertNotIn("second checking call", text)
        self.assertNotIn("CHECK_VERSION", text)

    def test_projection_cleaning_names_only_the_removed_link_forms(self):
        text = "\n".join(section(API_DOC.read_text(encoding="utf-8"), "## Ask Siyab"))
        self.assertIn("explicit `http://`, `https://`, `ftp://`, `ws://`, `wss://` and `www.` forms", text)
        self.assertIn("Other link forms are not removed by projection cleaning", text)


class CheckTableTest(unittest.TestCase):
    def setUp(self):
        self.text = API_DOC.read_text(encoding="utf-8")
        self.source = RULES.read_text(encoding="utf-8")

    def test_the_table_and_the_rules_name_the_same_checks(self):
        self.assertEqual(set(doc_checks(self.text)), rules_check_ids(self.source))

    def test_the_tier_2_rows_are_the_rules_tier_2_checks(self):
        tiers = doc_checks(self.text)
        self.assertEqual({check for check, tier in tiers.items() if tier == "2"}, set(rules.TIER2_CHECKS))
        self.assertEqual({tier for tier in tiers.values()}, {"1", "2"})

    def test_the_rules_version_2_checks_stay_out_of_the_table_while_no_caller_runs_version_2(self):
        # The table documents the rule set callers run. A module that passes rules_version other than through its
        # own rules_version parameter fails here; switching version 2 on then needs table rows for these ids,
        # code_changed_recently as Tier 2, and intel/ask.py would_change templates for them.
        ids = rules_check_ids(FRESHNESS.read_text(encoding="utf-8"))
        self.assertEqual(ids, {"code_changed_recently", "label_stale"})
        self.assertLessEqual(set(rules.TIER2_CHECKS_V2), ids)
        self.assertFalse(ids & set(doc_checks(self.text)))
        for function in (rules.judge, account_check.check_account):
            self.assertEqual(inspect.signature(function).parameters["rules_version"].default, 1)
        passed = []
        for path in sorted(ROOT.rglob("*.py")):
            if set(path.relative_to(ROOT).parts) & {"tests", ".git", "__pycache__", "target", ".venv"}:
                continue
            passed += [f"{path.relative_to(ROOT)}:{line}"
                       for line in rules_version_problems(path.read_text(encoding="utf-8"))]
        self.assertEqual(passed, [])

    def test_a_missing_row_or_an_extra_check_is_caught(self):
        dropped = "\n".join(line for line in self.text.splitlines() if not line.startswith("| `lookalike` |"))
        self.assertNotEqual(set(doc_checks(dropped)), rules_check_ids(self.source))
        extra = self.source + '\n\nevidence.check("brand_new_check", "pass", "", None)\n'
        self.assertNotEqual(set(doc_checks(self.text)), rules_check_ids(extra))
        with self.assertRaises(ValueError):
            rules_check_ids(self.source + "\n\nevidence.check(name, 'pass', '', None)\n")

    def test_rules_version_tripwire_catches_forwarding_defaults(self):
        for signature in ("rules_version=2", "*, rules_version=2", "rules_version=2, /",
                          "rules_version=True", "rules_version=1.0"):
            source = f"def forward({signature}):\n    return rules.judge(rules_version=rules_version)\n"
            with self.subTest(signature=signature):
                self.assertTrue(rules_version_problems(source))
        self.assertEqual(rules_version_problems(
            "async def forward(*, rules_version=1):\n"
            "    return check_account(rules_version=rules_version)\n"), [])

    def test_rules_version_tripwire_catches_keyword_expansion(self):
        for function in ("judge", "rules.judge", "check_account", "account_check.check_account"):
            with self.subTest(function=function):
                self.assertTrue(rules_version_problems(f"{function}(**options)"))
        self.assertTrue(rules_version_problems("rules.judge(rules_version=2)"))
        self.assertEqual(rules_version_problems("other(**options)"), [])


class FieldListTest(VerdictCase):
    def test_the_field_list_is_the_keys_verdict_request_returns(self):
        self.assertEqual(doc_fields(API_DOC.read_text(encoding="utf-8")), list(self.request()))

    def test_every_check_an_offline_verdict_emits_is_in_the_table(self):
        table = doc_checks(API_DOC.read_text(encoding="utf-8"))
        for policy in ({"allowlist": ["intents.near"], "caps": [{"asset": {"Ft": "wrap.near"}}]},
                       {"allowlist": [], "caps": []}):
            with self.subTest(policy=policy):
                emitted = {check["id"] for check in self.request(policy=policy)["checks"]}
                self.assertTrue(emitted)
                self.assertLessEqual(emitted, set(table))

    def test_a_missing_or_extra_field_is_caught(self):
        text = API_DOC.read_text(encoding="utf-8")
        keys = list(self.request())
        dropped = "\n".join(line for line in text.splitlines() if not line.startswith("| `evidence_path` |"))
        self.assertNotEqual(doc_fields(dropped), keys)
        extra = text.replace("| `message` |", "| `signing` | string | x | x |\n| `message` |", 1)
        self.assertNotEqual(doc_fields(extra), keys)


class CheckBehaviorTest(VerdictCase):
    def setUp(self):
        super().setUp()
        self.text = API_DOC.read_text(encoding="utf-8")

    def test_kind_and_tier_rows_match_offline_verdict_calls(self):
        receiver = "intents.near"
        issuers = rules.issuers_for("mainnet")
        self.assertTrue(issuers)
        results = {}
        for kind in ("near", "ft"):
            for allowlisted in (True, False):
                policy = {"allowlist": [receiver] if allowlisted else [],
                          "caps": [{"asset": {"Ft": "wrap.near"}}]}
                results[(kind, allowlisted)] = self.request(
                    kind=kind, token="wrap.near" if kind == "ft" else None, receiver=receiver, amount="1000",
                    policy=policy, issuers=issuers, reports_account="reports.gate.near")

        emitted = {
            "near": ({check["id"] for check in results[("near", True)]["checks"]}
                     | {check["id"] for check in results[("near", False)]["checks"]}),
            "ft": ({check["id"] for check in results[("ft", True)]["checks"]}
                   | {check["id"] for check in results[("ft", False)]["checks"]}),
        }
        self.assertEqual(kind_problems(self.text, emitted), {})
        allowlisted_ft = {check["id"] for check in results[("ft", True)]["checks"]}
        off_allowlist_ft = {check["id"] for check in results[("ft", False)]["checks"]}
        self.assertEqual(tier_problems(self.text, allowlisted_ft, off_allowlist_ft), {})
        ft_only = {check_id for check_id, row in doc_check_rows(self.text).items() if row["Kind"] == "ft"}
        self.assertTrue(ft_only)
        for allowlisted in (True, False):
            near_ids = {check["id"] for check in results[("near", allowlisted)]["checks"]}
            self.assertFalse(ft_only & near_ids)

        wrong_kind = replace_check_cell(self.text, "token_in_catalog", "Kind", "near")
        self.assertIn("token_in_catalog", kind_problems(wrong_kind, emitted))
        wrong_tier = replace_check_cell(self.text, "token_in_catalog", "Tier", "2")
        self.assertTrue(tier_problems(wrong_tier, allowlisted_ft, off_allowlist_ft))

    def test_optional_check_rows_match_their_enabling_inputs(self):
        issuer_table = rules.issuers_for("mainnet")
        without_inputs = self.request(kind="near", token=None)
        with_issuers = self.request(kind="near", token=None, issuers=issuer_table)
        with_reports = self.request(kind="near", token=None, reports_account="reports.gate.near")
        ids = [{check["id"] for check in result["checks"]}
               for result in (without_inputs, with_issuers, with_reports)]
        self.assertNotIn("issuer_blacklist", ids[0])
        self.assertIn("issuer_blacklist", ids[1])
        self.assertNotIn("issuer_blacklist", ids[2])
        self.assertNotIn("community_report", ids[0])
        self.assertNotIn("community_report", ids[1])
        self.assertIn("community_report", ids[2])
        self.assertEqual(enabled_description_problems(self.text), {})

        broken = replace_check_cell(self.text, "community_report", "Enabled by", "always")
        self.assertIn("community_report", enabled_description_problems(broken))


class IssuerNetworkTest(VerdictCase):
    def stub_result(self):
        return {"checks": [], "outcome": "ALLOW", "reasons": [], "action": "sign", "ask": [],
                "evidence_hash": "a" * 64, "latency_ms": 0, "verdict": None}

    def cli_issuers(self, gate, issuer_marker):
        args = SimpleNamespace(policy="{}", live=False, gate=gate, kind="near", token=None, receiver="intents.near",
                               amount="1", memo=None, agent_pk=AGENT, evidence_dir=self.dir, out=self.dir)
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(gate_main, "_clients", return_value=(None, None, None, None)), \
                mock.patch.object(gate_main.labels, "load", return_value=object()), \
                mock.patch.object(gate_main.rules, "issuers_for", return_value=issuer_marker) as selection, \
                mock.patch.object(gate_main.verdict, "request", return_value=self.stub_result()) as request:
            self.assertEqual(gate_main._verdict(args), 0)
        passed = request.call_args.kwargs["issuers"]
        if gate.endswith(".testnet"):
            selection.assert_not_called()
        else:
            selection.assert_called_once_with("mainnet")
        return passed

    def mcp_issuers(self, gate, issuer_marker):
        arguments = {"kind": "near", "token": None, "receiver": "intents.near", "amount": "1", "memo": None}
        environment = {"GATE_ACCOUNT_ID": gate, "GATE_POLICY": "{}", "REPORTS_ACCOUNT_ID": ""}
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(mcp_server, "_live_signing", return_value=None), \
                mock.patch.object(mcp_server, "_clients", return_value=(None, None, None, None)), \
                mock.patch.object(mcp_server.labels, "load", return_value=object()), \
                mock.patch.object(mcp_server.rules, "issuers_for", return_value=issuer_marker) as selection, \
                mock.patch.object(mcp_server.verdict, "request", return_value=self.stub_result()) as request:
            mcp_server._check_near_payment(arguments, self.dir)
        passed = request.call_args.kwargs["issuers"]
        if gate.endswith(".testnet"):
            selection.assert_not_called()
        else:
            selection.assert_called_once_with("mainnet")
        return passed

    def http_issuers(self, gate, issuer_marker):
        class ServerStub:
            def __init__(self, address, handler):
                self.handler = handler

        config = {"gate": gate, "policy": {}, "reports_account": None, "rate": 60, "keys": ("stub",),
                  "mode": "replay"}
        with mock.patch.object(endpoint, "_clients", return_value=(None, None, None, None)), \
                mock.patch.object(endpoint, "ThreadingHTTPServer", ServerStub), \
                mock.patch.object(endpoint.rules, "issuers_for", return_value=issuer_marker) as selection:
            server = endpoint.make_server("127.0.0.1", 0, config, self.dir, store=object())
        method = server.handler._verdict
        closure = dict(zip(method.__code__.co_freevars,
                           (cell.cell_contents for cell in method.__closure__ or ())))
        self.assertIn("judge", closure)
        with mock.patch.object(endpoint.verdict, "request", return_value={"evidence_path": "stub.json"}) as request:
            closure["judge"]({"kind": "near", "token": None, "receiver": "intents.near", "amount": "1"})
        passed = request.call_args.kwargs["issuers"]
        if gate.endswith(".testnet"):
            selection.assert_not_called()
        else:
            selection.assert_called_once_with("mainnet")
        return passed

    def test_all_callers_pass_issuers_only_for_a_non_testnet_gate(self):
        callers = {"CLI": self.cli_issuers, "MCP": self.mcp_issuers, "HTTP": self.http_issuers}
        for caller_name, caller in callers.items():
            for gate in ("gate.near", "gate.testnet"):
                with self.subTest(caller=caller_name, gate=gate):
                    issuer_marker = [{"issuer": "marker"}]
                    passed = caller(gate, issuer_marker)
                    self.assertIs(passed, issuer_marker if not gate.endswith(".testnet") else None)

        self.assertEqual(network_description_problems(self.text()), [])
        broken = replace_check_cell(self.text(), "issuer_blacklist", "Network", "any")
        self.assertTrue(network_description_problems(broken))

    def text(self):
        return API_DOC.read_text(encoding="utf-8")


class OutcomeActionTest(VerdictCase):
    def test_actions_match_each_documented_outcome_row(self):
        allowed = self.request()
        allowed_ask = self.request(policy={"allowlist": ["intents.near"], "caps": [], "enabled": False})
        denied = self.request(kind="near", token=None, receiver="no-such-account-example.near", amount="1",
                              policy={"allowlist": ["no-such-account-example.near"], "caps": []})
        unknown = self.request(kind="near", token=None, receiver="relay.aurora", amount="5",
                               policy={"allowlist": [], "caps": []})
        results = (allowed, allowed_ask, denied, unknown)
        self.assertEqual({result["outcome"] for result in results}, set(OUTCOMES))
        self.assertEqual(action_problems(API_DOC.read_text(encoding="utf-8"), results), [])

        broken = API_DOC.read_text(encoding="utf-8").replace(
            "| `DENY` | always empty (`gate/verdict.py:29-30`) | `refuse` |",
            "| `DENY` | always empty (`gate/verdict.py:29-30`) | `sign` |", 1)
        self.assertTrue(action_problems(broken, results))


class ExamplesTest(unittest.TestCase):
    def setUp(self):
        self.text = API_DOC.read_text(encoding="utf-8")

    def test_allow_and_deny_have_examples_and_the_unknown_section_cites_none(self):
        self.assertEqual(sorted(examples(self.text)), ["ALLOW", "DENY"])
        unknown = section(self.text, "### UNKNOWN")
        self.assertTrue(unknown)
        self.assertFalse([line for line in unknown if line.startswith("Fixture: `") or line == "```json"])

    def test_no_committed_fixture_records_unknown_as_the_unknown_section_says(self):
        unknown = []
        for path in sorted((ROOT / "fixtures").rglob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and fixture_outcome(data) == "UNKNOWN":
                unknown.append(path.relative_to(ROOT).as_posix())
        top_judgements = sorted((ROOT / "fixtures" / "receivers" / "top" / "judgements").glob("*.json"))
        top_unknown = [path for path in top_judgements
                       if json.loads(path.read_text(encoding="utf-8"))["verdict"] == "UNKNOWN"]
        self.assertTrue(top_unknown)
        self.assertEqual(unknown, [path.relative_to(ROOT).as_posix() for path in top_unknown])
        self.assertTrue(sorted((ROOT / "fixtures" / "receivers" / "judgements").glob("*.json")))
        self.assertIn("The committed UNKNOWN verdicts are exactly the top receiver judgements whose verdict is "
                      "UNKNOWN, under `fixtures/receivers/top/judgements/`; they reflect incomplete data for real "
                      "mainnet receivers.", "\n".join(section(self.text, "### UNKNOWN")))

    def test_each_example_is_its_fixture_and_the_fixture_records_its_outcome(self):
        self.assertEqual(example_problems(self.text, ROOT), {})

    def test_a_wrong_heading_a_wrong_fixture_or_an_edited_block_is_caught(self):
        swapped = self.text.replace("### ALLOW\n\nFixture: `fixtures/runs", "### DENY\n\nFixture: `fixtures/runs", 1)
        swapped = swapped.replace("### DENY\n\nFixture: `fixtures/evidence", "### ALLOW\n\nFixture: `fixtures/evidence", 1)
        self.assertEqual(set(example_problems(swapped, ROOT)), {"ALLOW", "DENY"})
        edited = self.text.replace('"latency_ms": 4712', '"latency_ms": 4713', 1)
        self.assertEqual(set(example_problems(edited, ROOT)), {"DENY"})
        with tempfile.TemporaryDirectory() as tmp:
            for fixture, _ in examples(self.text).values():
                for path in ROOT.glob(fixture):
                    target = Path(tmp) / path.relative_to(ROOT)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
            self.assertEqual(example_problems(self.text, Path(tmp)), {})
            copy = Path(tmp) / examples(self.text)["ALLOW"][0]
            data = json.loads(copy.read_text(encoding="utf-8"))
            copy.write_text(json.dumps({**data, "result": {**data["result"], "outcome": "UNKNOWN"}}), encoding="utf-8")
            self.assertIn("records UNKNOWN, not ALLOW", example_problems(self.text, Path(tmp))["ALLOW"])


if __name__ == "__main__":
    unittest.main()
