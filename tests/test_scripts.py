import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash is required for script tests")
class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        bin_dir = Path(self.tempdir.name) / "bin"
        bin_dir.mkdir()
        python3 = bin_dir / "python3"
        executable = os.path.abspath(sys.executable)
        drive, tail = os.path.splitdrive(executable)
        if drive:
            executable = f"/{drive[0].lower()}{tail.replace(os.sep, '/')}"
        python3.write_text(f'#!/bin/sh\nexec "{executable}" "$@"\n', encoding="utf-8")
        python3.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": os.pathsep.join((str(bin_dir), str(Path(BASH).parent))),
            "OWNER_ACCOUNT_ID": "owner.testnet",
            "GATE_ACCOUNT_ID": "gate.testnet",
            "NEAR_KEYS_DIR": str(Path(self.tempdir.name) / "keys"),
            "COLD_ACCOUNT_ID": "cold.testnet",
            "NETWORK": "testnet",
        }
        self.assertIsNone(shutil.which("near", path=self.env["PATH"]))

    def run_script(self, script, *args, env=None):
        return subprocess.run(
            [BASH, f"scripts/{script}", *args],
            capture_output=True,
            cwd=ROOT,
            env=self.env if env is None else env,
            timeout=30,
            check=False,
            text=True,
        )

    def command(self, stdout, method):
        line = next(line for line in stdout.splitlines() if f" {method} " in line)
        return shlex.split(line[2:])


class KillSwitchTest(ScriptTest):
    def test_dry_run_prints_the_three_owner_calls_in_order_and_sends_nothing(self):
        agent_pub = "ed25519:11111111111111111111111111111111"
        process = self.run_script("kill_switch.sh", agent_pub, "--dry-run")
        self.assertEqual(process.returncode, 0, process.stderr)
        expected = (
            "+ near contract call-function as-transaction gate.testnet set_paused",
            "+ near contract call-function as-transaction gate.testnet revoke_agent",
            "+ near contract call-function as-read-only token.gate.testnet ft_balance_of",
            "+ near contract call-function as-read-only token.gate.testnet storage_balance_of",
            "+ near contract call-function as-transaction gate.testnet withdraw",
        )
        positions = [process.stdout.index(command) for command in expected]
        self.assertEqual(positions, sorted(positions))
        transaction_lines = [line for line in process.stdout.splitlines() if "as-transaction" in line]
        self.assertEqual(len(transaction_lines), 3)
        self.assertTrue(all("attached-deposit 1\\ yoctoNEAR" in line for line in transaction_lines))
        withdraw_args = self.command(process.stdout, "withdraw")
        json_args = withdraw_args[withdraw_args.index("json-args") + 1]
        self.assertEqual(
            json.loads(json_args),
            {
                "asset": {"Ft": "token.gate.testnet"},
                "receiver_id": "cold.testnet",
                "amount": "<ft_balance_of of the Gate>",
            },
        )
        for line in process.stdout.splitlines():
            if line.startswith("+ "):
                self.assertNotIn('"private_key"', line)
                self.assertNotIn('"secret_key"', line)
                self.assertNotRegex(line, r"ed25519:[1-9A-HJ-NP-Za-km-z]{80,}")
        self.assertIn("The Gate NEAR balance stays in place", process.stdout)

    def test_missing_cold_account_or_wrong_arity_exits_2(self):
        env = {**self.env, "COLD_ACCOUNT_ID": ""}
        missing = self.run_script("kill_switch.sh", "agent", "--dry-run", env=env)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("COLD_ACCOUNT_ID", missing.stderr)

        wrong_arity = self.run_script("kill_switch.sh", "agent", "another-agent", "--dry-run")
        self.assertEqual(wrong_arity.returncode, 2)
        self.assertIn("usage: kill_switch.sh <agent_pub> [--dry-run]", wrong_arity.stderr)

    def test_mainnet_is_refused(self):
        env = {**self.env, "NETWORK": "mainnet"}
        process = self.run_script("kill_switch.sh", "agent", "--dry-run", env=env)
        self.assertEqual(process.returncode, 2)
        self.assertIn("NETWORK must be testnet", process.stderr)


# The read only answers as near-cli-rs 0.30.1 prints them on testnet (recorded 5 Nov 2026): a Logs box,
# the return value (one line, or an object pretty printed over several lines), then the console command.
def cli_answer(value, method, account):
    return (
        "\u251c  Logs:\n\u2502    No logs\x1b[0m\nFunction execution return value (printed to stdout):\n"
        f"{value}\n\n\nHere is your console command if you need to script it or re-run:\n"
        f"    \x1b[33mnear contract call-function as-read-only token.gate.testnet {method} json-args "
        f"'{{\"account_id\":\"{account}\"}}' network-config testnet now\x1b[39m\n\n"
    )


FAKE_NEAR = """#!/bin/sh
for arg in "$@"; do printf '%s\\n' "$arg"; done >> "$NEAR_LOG"
printf -- '---\\n' >> "$NEAR_LOG"
case "$*" in
  *as-read-only*ft_balance_of*) cat "$FAKE_DIR/ft_balance_of.txt" ;;
  *as-read-only*storage_balance_of*) cat "$FAKE_DIR/storage_balance_of.txt" ;;
  *as-transaction*) printf 'Transaction ID: 4QkqgW9n6rN8yP7LhXmVv3dEzC2bA1uT5sR6qJ8kH9gS\\n' ;;
  *view-account-summary*) printf 'code_hash: recorded\\n' ;;
esac
"""


class KillSwitchLiveTest(ScriptTest):
    def setUp(self):
        super().setUp()
        fake = Path(self.tempdir.name) / "fake"
        fake.mkdir()
        bin_dir = Path(self.tempdir.name) / "bin"
        (bin_dir / "near").write_bytes(FAKE_NEAR.encode("utf-8"))
        (bin_dir / "near").chmod(0o755)
        (fake / "ft_balance_of.txt").write_bytes(
            cli_answer('"999940000000"', "ft_balance_of", "gate.testnet").encode("utf-8"))
        self.fake = fake
        self.log = Path(self.tempdir.name) / "near.log"
        self.env = {**self.env, "FAKE_DIR": str(fake), "NEAR_LOG": str(self.log), "DRY_RUN": "0"}

    def calls(self):
        records = self.log.read_text(encoding="utf-8").split("---\n")
        return [record.splitlines() for record in records if record.strip()]

    def test_a_registered_cold_account_gets_the_whole_token_balance(self):
        registered = '{\n  "available": "0",\n  "total": "1290000000000000000000"\n}'
        (self.fake / "storage_balance_of.txt").write_bytes(
            cli_answer(registered, "storage_balance_of", "cold.testnet").encode("utf-8"))
        process = self.run_script("kill_switch.sh", "ed25519:11111111111111111111111111111111")
        self.assertEqual(process.returncode, 0, process.stderr)
        sent = [call for call in self.calls() if "as-transaction" in call]
        self.assertEqual([call[call.index("as-transaction") + 2] for call in sent],
                         ["set_paused", "revoke_agent", "withdraw"])
        withdraw = sent[2]
        self.assertEqual(json.loads(withdraw[withdraw.index("json-args") + 1]), {
            "asset": {"Ft": "token.gate.testnet"},
            "receiver_id": "cold.testnet",
            "amount": "999940000000",
        })
        self.assertIn("The Gate NEAR balance stays in place", process.stdout)

    def test_an_unregistered_cold_account_stops_before_the_withdraw(self):
        (self.fake / "storage_balance_of.txt").write_bytes(
            cli_answer("null", "storage_balance_of", "cold.testnet").encode("utf-8"))
        process = self.run_script("kill_switch.sh", "ed25519:11111111111111111111111111111111")
        self.assertEqual(process.returncode, 3)
        self.assertIn("the cold account cold.testnet is not registered on token.gate.testnet", process.stderr)
        sent = [call[call.index("as-transaction") + 2] for call in self.calls() if "as-transaction" in call]
        self.assertEqual(sent, ["set_paused", "revoke_agent"])


class SetDenylistTest(ScriptTest):
    def test_dry_run_prints_set_denylist_with_the_accounts(self):
        accounts = ["vendor.testnet", "cold.testnet"]
        process = self.run_script("set_denylist.sh", ",".join(accounts), "--dry-run")
        self.assertEqual(process.returncode, 0, process.stderr)
        args = self.command(process.stdout, "set_denylist")
        json_args = args[args.index("json-args") + 1]
        self.assertEqual(json.loads(json_args), {"accounts": accounts})


if __name__ == "__main__":
    unittest.main()
