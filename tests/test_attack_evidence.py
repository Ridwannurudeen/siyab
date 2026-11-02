import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ATTACKS = ROOT / "docs" / "ATTACKS.md"
CLAIMS = ROOT / "docs" / "CLAIMS.md"
ATTACK_FIXTURES = (
    "01-no-verdict.json",
    "02-forged-signature.json",
    "03-replay.json",
    "03-expired.json",
    "04-over-cap.json",
    "05-callback.json",
    "06-token-direct.json",
    "07-service-down.json",
    "08-one-yocto.json",
)


def table_rows(document, header="| # | Attack and command |"):
    rows = []
    in_table = False
    for line in document.splitlines():
        if line.startswith(header):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
        elif in_table:
            break
    return rows


def declared_functions(path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        tree = ast.parse(text)
        found = set()

        def visit(node, prefix=""):
            for child in getattr(node, "body", ()):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = f"{prefix}{child.name}"
                    found.add(name)
                    visit(child, f"{name}.")
                elif isinstance(child, ast.ClassDef):
                    visit(child, f"{prefix}{child.name}.")

        visit(tree)
        return found
    return set(re.findall(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)", text))


def unbuilt_limit_rows():
    rows = table_rows(CLAIMS.read_text(encoding="utf-8"), "| Claim | File:function |")
    return tuple(row[0] for row in rows if "not built" in row[0].lower())


class AttackEvidenceTest(unittest.TestCase):
    def test_every_attack_row_names_existing_fixtures_and_the_expected_result(self):
        document = ATTACKS.read_text(encoding="utf-8")
        rows = table_rows(document)
        self.assertEqual(len(rows), 9)
        for row in rows[:8]:
            with self.subTest(attack=row[1]):
                self.assertIn("python -m gate attack ", row[1])
                fixture_names = re.findall(r"`(fixtures/[^`]+\.json)`", row[5])
                self.assertTrue(fixture_names)
                expected = re.findall(r"`([^`]+)`", row[4])
                payloads = []
                for name in fixture_names:
                    path = ROOT / name
                    self.assertTrue(path.is_file(), name)
                    payloads.append(path.read_text(encoding="utf-8"))
                if "fixtures/rpc/attacks/" in " ".join(fixture_names):
                    combined = " ".join(payloads)
                    self.assertTrue(all(value in combined for value in expected), row[1])

        evm_row = rows[8]
        self.assertEqual(evm_row[0], "preflight")
        self.assertIn("preflight, no transaction on any chain", evm_row[1])
        intent_files = list((ROOT / "fixtures" / "intents").glob("*.json"))
        goplus_files = list((ROOT / "fixtures" / "goplus").glob("*.json"))
        self.assertEqual(len(intent_files), 1)
        self.assertTrue(goplus_files)
        intent = json.loads(intent_files[0].read_text(encoding="utf-8"))
        goplus_responses = [json.loads(path.read_text(encoding="utf-8"))["response"]["result"]
                            for path in goplus_files]
        self.assertIs(intent["response"]["quoteRequest"]["dry"], True)
        self.assertTrue(any(response.get("stealing_attack") == "1" for response in goplus_responses))

    def test_protocol_rejections_have_rpc_errors_and_no_explorer_links(self):
        document = ATTACKS.read_text(encoding="utf-8")
        rows = table_rows(document)
        protocol_rows = (rows[4], rows[5], rows[7])
        expected_values = ("MethodNameMismatch", "ReceiverMismatch", "DepositWithFunctionCall")
        for row, expected in zip(protocol_rows, expected_values):
            with self.subTest(protocol=expected):
                self.assertIn("refused by the protocol (transaction rejected), not by the contract", row[3])
                self.assertEqual(row[6], "No receipt, no explorer link")
                fixture = ROOT / re.search(r"`(fixtures/[^`]+\.json)`", row[5]).group(1)
                self.assertIn(expected, fixture.read_text(encoding="utf-8"))

    def test_refusal_count_matches_records_without_counting_the_replay_baseline(self):
        document = ATTACKS.read_text(encoding="utf-8")
        total = 0
        refusals = 0
        bypasses = 0
        for name in ATTACK_FIXTURES:
            payload = json.loads((ROOT / "fixtures" / "rpc" / "attacks" / name).read_text(encoding="utf-8"))
            self.assertIs(payload["refused"], True)
            for index, submission in enumerate(payload["submissions"]):
                is_replay_baseline = name == "03-replay.json" and index == 0
                if is_replay_baseline:
                    self.assertEqual(submission["outcome"]["status"], {"SuccessValue": ""})
                    self.assertIn("first send is a real payment", " ".join(payload["notes"]))
                    continue
                total += 1
                outcome = submission["outcome"]
                failed = (outcome.get("status") or {}).get("Failure") is not None or outcome.get("rpc_error") is not None
                refusals += int(failed)
                bypasses += int(not failed)
                self.assertTrue(failed, name)
        self.assertEqual(total, 9)
        self.assertEqual(refusals, 9)
        self.assertEqual(bypasses, 0)
        self.assertIn("9 attempts, 9 refusals, 0 bypasses", document)

    def test_all_claim_rows_resolve_to_code_and_named_tests(self):
        rows = table_rows(CLAIMS.read_text(encoding="utf-8"), "| Claim | File:function |")
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(claim=row[0]):
                source = row[1].strip("`")
                path_text, function = source.rsplit(":", 1)
                path = ROOT / path_text
                self.assertTrue(path.is_file(), path_text)
                self.assertIn(function, declared_functions(path), source)
                test_text = row[2].strip("`")
                test_path_text, test_name = test_text.split("::", 1)
                test_path = ROOT / test_path_text
                self.assertTrue(test_path.is_file(), test_path_text)
                expected_name = test_name.split("::")[-1] if test_path.suffix == ".rs" else test_name
                self.assertIn(expected_name, declared_functions(test_path), test_text)
                evidence = [value.strip().strip("`") for value in row[3].split(",")]
                for item in evidence:
                    self.assertTrue((ROOT / item).exists() or item == "offline", item)
                self.assertIn(row[4], {"yes", "no"})

    def test_unbuilt_surfaces_are_stated_as_limits(self):
        self.assertEqual(set(unbuilt_limit_rows()), {
            "A TEE is not built.",
            "An OutLayer approver is not built.",
            "A browser extension for NEAR is not built.",
            "A Telegram bot for NEAR is not built.",
        })


if __name__ == "__main__":
    unittest.main()