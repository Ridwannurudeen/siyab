import json
import os
import shlex
import subprocess
import unittest
from pathlib import Path

from tests.test_scripts import BASH, ScriptTest, cli_answer

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS = {
    "gate": "gate.testnet",
    "registry": "registry.gate.testnet",
    "reports": "reports.gate.testnet",
}
FAKE_NEAR = r"""#!/bin/sh
for arg in "$@"; do printf '%s\n' "$arg"; done >> "$NEAR_LOG"
printf -- '---\n' >> "$NEAR_LOG"
if [ "$2" = "view-account-summary" ]; then
  printf 'code_hash: recorded\n'
  exit 0
fi
if [ "$2" = "deploy" ]; then
  printf 'Transaction ID: 4QkqgW9n6rN8yP7LhXmVv3dEzC2bA1uT5sR6qJ8kH9gS\n'
  exit 0
fi
if [ "$3" = "as-read-only" ] && [ "$5" = "version" ]; then
  cat "$FAKE_DIR/version.txt"
  exit 0
fi
printf 'unexpected request\n' >&2
exit 42
"""


def bash_path(path):
    """The path as the bash on PATH reads it: /c/... for a Windows drive path."""
    path = os.path.abspath(path)
    drive, tail = os.path.splitdrive(path)
    if drive:
        return f"/{drive[0].lower()}{tail.replace(os.sep, '/')}"
    return path


class RedeployTest(ScriptTest):
    def setUp(self):
        super().setUp()
        wasm = Path(self.tempdir.name) / "candidate.wasm"
        wasm.write_bytes(b"\x00asm\x01\x00\x00\x00")
        self.wasm = bash_path(wasm)
        self.env = {**self.env, "GATE_STATE_FROZEN": "1", "DRY_RUN": "1"}
        for name in ("EXPECT_VERSION", "EXPECT_CODE_HASH", "REPORTS_ACCOUNT_ID"):
            self.env.pop(name, None)

    def redeploy(self, *args, **extra):
        return self.run_script("redeploy.sh", *args, env={**self.env, **extra})

    def lines(self, stdout, prefix):
        return [shlex.split(line[2:]) for line in stdout.splitlines() if line.startswith(f"+ {prefix}")]


class RedeployDryRunTest(RedeployTest):
    def test_bash_parses_the_script(self):
        process = subprocess.run([BASH, "-n", "scripts/redeploy.sh"], capture_output=True, cwd=ROOT,
                                 env=self.env, timeout=30, check=False, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_each_contract_deploys_without_an_init_call_by_default(self):
        for contract, account in ACCOUNTS.items():
            with self.subTest(contract=contract):
                process = self.redeploy(contract, self.wasm)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(self.lines(process.stdout, "near contract deploy"), [[
                    "near", "contract", "deploy", account, "use-file", self.wasm, "without-init-call",
                    "network-config", "testnet", "sign-with-access-key-file",
                    f'{self.env["NEAR_KEYS_DIR"]}/{account}.json', "send",
                ]])

    def test_migrate_deploys_with_an_init_call_to_migrate_with_empty_args(self):
        for contract, account in ACCOUNTS.items():
            with self.subTest(contract=contract):
                process = self.redeploy(contract, self.wasm, "--migrate")
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(self.lines(process.stdout, "near contract deploy"), [[
                    "near", "contract", "deploy", account, "use-file", self.wasm, "with-init-call",
                    "migrate", "json-args", "{}", "prepaid-gas", "100.0 Tgas", "attached-deposit", "0 NEAR",
                    "network-config", "testnet", "sign-with-access-key-file",
                    f'{self.env["NEAR_KEYS_DIR"]}/{account}.json', "send",
                ]])
                self.assertNotIn("without-init-call", process.stdout)

    def test_gate_and_registry_read_the_version_before_and_after_the_deploy(self):
        for contract in ("gate", "registry"):
            with self.subTest(contract=contract):
                process = self.redeploy(contract, self.wasm)
                self.assertEqual(process.returncode, 0, process.stderr)
                reads = self.lines(process.stdout, "near contract call-function as-read-only")
                self.assertEqual([read[4:7] for read in reads], [[ACCOUNTS[contract], "version", "json-args"]] * 2)
                lines = process.stdout.splitlines()
                deploy = next(i for i, line in enumerate(lines) if line.startswith("+ near contract deploy"))
                reads_at = [i for i, line in enumerate(lines) if " as-read-only " in line]
                self.assertTrue(reads_at[0] < deploy < reads_at[1])

    def test_reports_reads_no_version(self):
        process = self.redeploy("reports", self.wasm)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(self.lines(process.stdout, "near contract call-function"), [])
        self.assertIn("version before: not read, the reports contract has no version view", process.stdout)

    def test_expected_values_are_printed_as_planned_assertions(self):
        for contract in ("gate", "registry"):
            for mode in ((), ("--migrate",)):
                with self.subTest(contract=contract, mode=mode):
                    process = self.redeploy(contract, self.wasm, *mode,
                                            EXPECT_VERSION="0.2.0", EXPECT_CODE_HASH="Expected1111")
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertIn("would assert: version after equals 0.2.0", process.stdout)
                    self.assertIn(f"would assert: code_hash of {ACCOUNTS[contract]} equals Expected1111",
                                  process.stdout)
        for mode in ((), ("--migrate",)):
            with self.subTest(contract="reports", mode=mode):
                process = self.redeploy("reports", self.wasm, *mode, EXPECT_CODE_HASH="Expected1111")
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertIn("would assert: code_hash of reports.gate.testnet equals Expected1111",
                              process.stdout)
                self.assertNotIn("would assert: version", process.stdout)

    def test_no_assertion_is_planned_without_expected_values(self):
        for contract in ACCOUNTS:
            for mode in ((), ("--migrate",)):
                with self.subTest(contract=contract, mode=mode):
                    process = self.redeploy(contract, self.wasm, *mode)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertNotIn("would assert", process.stdout)

    def test_reports_refuses_an_expected_version(self):
        process = self.redeploy("reports", self.wasm, EXPECT_VERSION="0.1.0")
        self.assertEqual(process.returncode, 2)
        self.assertIn("EXPECT_VERSION cannot be asserted: the reports contract has no version view",
                      process.stderr)
        self.assertEqual(process.stdout, "")

    def test_reports_account_can_be_named(self):
        process = self.redeploy("reports", self.wasm, REPORTS_ACCOUNT_ID="moderated.testnet")
        self.assertEqual(process.returncode, 0, process.stderr)
        deploy = self.lines(process.stdout, "near contract deploy")[0]
        self.assertEqual(deploy[3], "moderated.testnet")
        self.assertEqual(deploy[deploy.index("sign-with-access-key-file") + 1],
                         f'{self.env["NEAR_KEYS_DIR"]}/moderated.testnet.json')

    def test_frozen_state_guard_and_arguments_are_kept(self):
        env = {**self.env}
        env.pop("GATE_STATE_FROZEN")
        process = self.run_script("redeploy.sh", "gate", self.wasm, env=env)
        self.assertEqual(process.returncode, 2)
        self.assertIn("GATE_STATE_FROZEN=1 is required after 4 Nov", process.stderr)

        process = self.redeploy("token", self.wasm)
        self.assertEqual(process.returncode, 2)
        self.assertIn("contract must be gate, registry or reports", process.stderr)

        process = self.redeploy("gate", "--migrate")
        self.assertEqual(process.returncode, 2)
        self.assertIn("usage: redeploy.sh <gate|registry|reports> <wasm> [--migrate]", process.stderr)


class RedeployLiveTest(RedeployTest):
    def setUp(self):
        super().setUp()
        fake = Path(self.tempdir.name) / "fake"
        fake.mkdir()
        bin_dir = Path(self.tempdir.name) / "bin"
        (bin_dir / "near").write_bytes(FAKE_NEAR.encode("utf-8"))
        (bin_dir / "near").chmod(0o755)
        self.fake = fake
        self.log = Path(self.tempdir.name) / "near.log"
        self.env = {**self.env, "FAKE_DIR": str(fake), "NEAR_LOG": str(self.log), "DRY_RUN": "0"}
        self.answer_version('"0.2.0"')

    def answer_version(self, value):
        (self.fake / "version.txt").write_bytes(
            cli_answer(value, "gate.testnet", "version", {}).encode("utf-8"))

    def calls(self):
        records = self.log.read_text(encoding="utf-8").split("---\n")
        return [record.splitlines() for record in records if record.strip()]

    def test_a_matching_version_passes_after_the_migration_is_sent(self):
        process = self.redeploy("gate", self.wasm, "--migrate", EXPECT_VERSION="0.2.0")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("asserted: version after equals 0.2.0", process.stdout)
        deploys = [call for call in self.calls() if call[1] == "deploy"]
        self.assertEqual(len(deploys), 1)
        self.assertEqual(deploys[0][deploys[0].index("with-init-call") + 1:][:3], ["migrate", "json-args", "{}"])

    def test_a_different_version_exits_1(self):
        self.answer_version('"0.1.0"')
        process = self.redeploy("gate", self.wasm, EXPECT_VERSION="0.2.0")
        self.assertEqual(process.returncode, 1)
        self.assertIn("version after is 0.1.0, expected 0.2.0", process.stderr)
        self.assertNotIn("asserted:", process.stdout)

    def test_an_unreadable_version_exits_1(self):
        (self.fake / "version.txt").write_text("Empty return value\n", encoding="utf-8")
        process = self.redeploy("gate", self.wasm, EXPECT_VERSION="0.2.0")
        self.assertEqual(process.returncode, 1)
        self.assertIn("version after could not be read, expected 0.2.0", process.stderr)


if __name__ == "__main__":
    unittest.main()
