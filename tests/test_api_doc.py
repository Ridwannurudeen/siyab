"""docs/API.md is the consumer contract of gate.verdict.request: its field list, its check table and its examples.

The check ids come from reading gate/rules.py with ast: the first argument of every evidence.check call, which
must be a string literal. The field list is compared with the keys of an offline verdict.request over the replay
fixtures, wired as tests/test_verdict.py wires it. Each example must equal its cited fixture after every 64 hex
value is shortened to its first and last 8 hex, and the fixture must record the outcome the example's heading
names. Every test also proves that its comparison fails on a deliberately broken copy.
"""

import ast
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from gate import rules
from tests.test_verdict import VerdictCase

ROOT = Path(__file__).resolve().parent.parent
API_DOC = ROOT / "docs" / "API.md"
RULES = ROOT / "gate" / "rules.py"
HEX64 = re.compile(r"[0-9a-f]{64}")
OUTCOMES = ("ALLOW", "DENY", "UNKNOWN")


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

    def test_a_missing_row_or_an_extra_check_is_caught(self):
        dropped = "\n".join(line for line in self.text.splitlines() if not line.startswith("| `lookalike` |"))
        self.assertNotEqual(set(doc_checks(dropped)), rules_check_ids(self.source))
        extra = self.source + '\n\nevidence.check("brand_new_check", "pass", "", None)\n'
        self.assertNotEqual(set(doc_checks(self.text)), rules_check_ids(extra))
        with self.assertRaises(ValueError):
            rules_check_ids(self.source + "\n\nevidence.check(name, 'pass', '', None)\n")


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


class ExamplesTest(unittest.TestCase):
    def setUp(self):
        self.text = API_DOC.read_text(encoding="utf-8")

    def test_one_example_per_outcome(self):
        self.assertEqual(sorted(examples(self.text)), sorted(OUTCOMES))

    def test_each_example_is_its_fixture_and_the_fixture_records_its_outcome(self):
        self.assertEqual(example_problems(self.text, ROOT), {})

    def test_a_wrong_heading_a_wrong_fixture_or_an_edited_block_is_caught(self):
        swapped = self.text.replace("### UNKNOWN", "### ALLOW", 1).replace("### ALLOW\n\nFixture: `fixtures/runs",
                                                                           "### UNKNOWN\n\nFixture: `fixtures/runs", 1)
        self.assertEqual(set(example_problems(swapped, ROOT)), {"ALLOW", "UNKNOWN"})
        edited = self.text.replace('"latency_ms": 4712', '"latency_ms": 4713', 1)
        self.assertEqual(set(example_problems(edited, ROOT)), {"DENY"})
        with tempfile.TemporaryDirectory() as tmp:
            for fixture, _ in examples(self.text).values():
                for path in ROOT.glob(fixture):
                    target = Path(tmp) / path.relative_to(ROOT)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
            self.assertEqual(example_problems(self.text, Path(tmp)), {})
            copy = Path(tmp) / "fixtures" / "receivers" / "judgements" / "liver.pool.near.json"
            data = json.loads(copy.read_text(encoding="utf-8"))
            copy.write_text(json.dumps({**data, "verdict": "ALLOW"}), encoding="utf-8")
            self.assertIn("records ALLOW, not UNKNOWN", example_problems(self.text, Path(tmp))["UNKNOWN"])


if __name__ == "__main__":
    unittest.main()
