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
AGENT_PUB = "ed25519:11111111111111111111111111111111"


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
            "AGENT_PUB": AGENT_PUB,
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


class MakeAttackReceiverTest(ScriptTest):
    def test_dry_run_uses_the_demo_counterparty_account(self):
        env = {**self.env, "COUNTERPARTY_ACCOUNT_ID": "counterparty.testnet", "DRY_RUN": "1"}
        env.pop("ATTACKER_ACCOUNT_ID", None)
        process = self.run_script("make_attack_receiver.sh", "lookalike", env=env)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        args = self.command(process.stdout, "create-account")
        self.assertEqual(args[args.index("sign-as") + 1], "counterparty.testnet")
        self.assertEqual(
            args[args.index("sign-with-access-key-file") + 1],
            f'{self.env["NEAR_KEYS_DIR"]}/counterparty.testnet.json',
        )

    def test_dry_run_supports_the_legacy_variable_with_one_deprecation_line(self):
        env = {**self.env, "ATTACKER_ACCOUNT_ID": "legacy-counterparty.testnet", "DRY_RUN": "1"}
        env.pop("COUNTERPARTY_ACCOUNT_ID", None)
        process = self.run_script("make_attack_receiver.sh", "lookalike", env=env)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(
            process.stderr,
            "deprecated: ATTACKER_ACCOUNT_ID is deprecated; use COUNTERPARTY_ACCOUNT_ID\n",
        )
        args = self.command(process.stdout, "create-account")
        self.assertEqual(args[args.index("sign-as") + 1], "legacy-counterparty.testnet")
        self.assertEqual(
            args[args.index("sign-with-access-key-file") + 1],
            f'{self.env["NEAR_KEYS_DIR"]}/legacy-counterparty.testnet.json',
        )

    def test_legacy_variable_appears_only_in_compatibility_code_reference_and_this_test(self):
        matches = []
        excluded = {".git", "fixtures", "res"}
        text_suffixes = {".example", ".json", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"}
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            if not path.is_file() or excluded.intersection(relative.parts):
                continue
            if path.suffix.lower() not in text_suffixes:
                continue
            if "ATTACKER_ACCOUNT_ID" in path.read_text(encoding="utf-8"):
                matches.append(relative.as_posix())
        self.assertEqual(sorted(matches), ["env.example", "scripts/lib.sh", "tests/test_scripts.py"])


class KillSwitchTest(ScriptTest):
    def test_dry_run_prints_calls_arguments_reads_and_signer(self):
        process = self.run_script("kill_switch.sh", AGENT_PUB, "--dry-run")
        self.assertEqual(process.returncode, 0, process.stderr)
        expected = (
            "+ near contract call-function as-transaction gate.testnet set_paused",
            "+ near contract call-function as-read-only gate.testnet get_agent",
            "+ near contract call-function as-transaction gate.testnet revoke_agent",
            "+ near contract call-function as-read-only token.gate.testnet ft_balance_of",
            "+ near contract call-function as-read-only token.gate.testnet storage_balance_of",
            "+ near contract call-function as-transaction gate.testnet withdraw",
        )
        positions = [process.stdout.index(command) for command in expected]
        self.assertEqual(positions, sorted(positions))

        for method, expected_args in (
            ("set_paused", {"paused": True}),
            ("revoke_agent", {"public_key": AGENT_PUB}),
        ):
            args = self.command(process.stdout, method)
            self.assertEqual(json.loads(args[args.index("json-args") + 1]), expected_args)

        for method, contract, expected_args in (
            ("get_agent", "gate.testnet", {"public_key": AGENT_PUB}),
            ("ft_balance_of", "token.gate.testnet", {"account_id": "gate.testnet"}),
            ("storage_balance_of", "token.gate.testnet", {"account_id": "cold.testnet"}),
        ):
            args = self.command(process.stdout, method)
            self.assertEqual(args[args.index("as-read-only") + 1], contract)
            self.assertEqual(json.loads(args[args.index("json-args") + 1]), expected_args)

        transaction_lines = [line for line in process.stdout.splitlines() if "as-transaction" in line]
        self.assertEqual(len(transaction_lines), 3)
        for line in transaction_lines:
            args = shlex.split(line[2:])
            self.assertEqual(args[args.index("sign-as") + 1], "owner.testnet")
            self.assertIn("attached-deposit", args)
            self.assertEqual(args[args.index("attached-deposit") + 1], "1 yoctoNEAR")

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

    def test_dry_run_uses_the_owners_key_file(self):
        process = self.run_script("kill_switch.sh", AGENT_PUB, "--dry-run")
        self.assertEqual(process.returncode, 0, process.stderr)
        key_path = f'{self.env["NEAR_KEYS_DIR"]}/owner.testnet.json'
        for method in ("set_paused", "revoke_agent", "withdraw"):
            args = self.command(process.stdout, method)
            self.assertEqual(
                args[args.index("sign-with-access-key-file") + 1], key_path
            )

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
        process = self.run_script("kill_switch.sh", AGENT_PUB, "--dry-run", env=env)
        self.assertEqual(process.returncode, 2)
        self.assertIn("NETWORK must be testnet", process.stderr)


# near-cli-rs 0.30.1 returns the JSON value between these two lines.
def cli_answer(value, contract, method, args):
    json_args = json.dumps(args, separators=(",", ":"))
    return (
        "\u251c  Logs:\n\u2502    No logs\x1b[0m\nFunction execution return value (printed to stdout):\n"
        f"{value}\n\n\nHere is your console command if you need to script it or re-run:\n"
        f"    \x1b[33mnear contract call-function as-read-only {contract} {method} json-args "
        f"'{json_args}' network-config testnet now\x1b[39m\n\n"
    )


FAKE_NEAR = r"""#!/bin/sh
for arg in "$@"; do printf '%s\n' "$arg"; done >> "$NEAR_LOG"
printf -- '---\n' >> "$NEAR_LOG"
if [ "$2" = "view-account-summary" ]; then
  printf 'code_hash: recorded\n'
  exit 0
fi
case "$3" in
  as-read-only)
    contract=$4
    method=$5
    json_args=$7
    if [ "${FAIL_READ_METHOD:-}" = "$method" ]; then
      printf 'simulated read failure\n' >&2
      exit 44
    fi
    expected_agent_args=$(printf '{"public_key":"%s"}' "$AGENT_PUB")
    case "$contract|$method|$json_args" in
      "gate.testnet|get_agent|$expected_agent_args") cat "$FAKE_DIR/get_agent_gate.testnet.txt" ;;
      'token.gate.testnet|ft_balance_of|{"account_id":"gate.testnet"}') cat "$FAKE_DIR/ft_balance_of_gate.testnet.txt" ;;
      'token.gate.testnet|storage_balance_of|{"account_id":"cold.testnet"}') cat "$FAKE_DIR/storage_balance_of_cold.testnet.txt" ;;
      *) printf 'unexpected read request\n' >&2; exit 42 ;;
    esac
    ;;
  as-transaction)
    if [ "${FAIL_METHOD:-}" = "$5" ]; then
      printf 'simulated transaction failure\n' >&2
      exit 1
    fi
    printf 'Transaction ID: 4QkqgW9n6rN8yP7LhXmVv3dEzC2bA1uT5sR6qJ8kH9gS\n'
    ;;
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
        self.fake = fake
        self.log = Path(self.tempdir.name) / "near.log"
        self.env = {
            **self.env,
            "FAKE_DIR": str(fake),
            "NEAR_LOG": str(self.log),
            "DRY_RUN": "0",
        }
        self.write_answer("get_agent", "gate.testnet", "{\n  \"agent_label\": \"registered\"\n}")
        self.write_answer("ft_balance_of", "token.gate.testnet", '"999940000000"')
        self.write_answer(
            "storage_balance_of",
            "token.gate.testnet",
            '{\n  "available": "0",\n  "total": "1290000000000000000000"\n}',
            account="cold.testnet",
        )

    def write_answer(self, method, contract, value, account=None):
        if method == "get_agent":
            args = {"public_key": AGENT_PUB}
            account = "gate.testnet"
            name = f"{method}_{account}.txt"
        else:
            account = account or "gate.testnet"
            args = {"account_id": account}
            name = f"{method}_{account}.txt"
        (self.fake / name).write_bytes(
            cli_answer(value, contract, method, args).encode("utf-8")
        )

    def calls(self):
        if not self.log.exists():
            return []
        records = self.log.read_text(encoding="utf-8").split("---\n")
        return [record.splitlines() for record in records if record.strip()]

    def transactions(self):
        return [call for call in self.calls() if len(call) > 2 and call[2] == "as-transaction"]

    def test_registered_cold_account_gets_the_whole_token_balance(self):
        process = self.run_script("kill_switch.sh", AGENT_PUB)
        self.assertEqual(process.returncode, 0, process.stderr)
        sent = self.transactions()
        self.assertEqual([call[4] for call in sent], ["set_paused", "revoke_agent", "withdraw"])
        for call in sent:
            self.assertEqual(call[call.index("sign-as") + 1], "owner.testnet")
        self.assertEqual(json.loads(sent[0][sent[0].index("json-args") + 1]), {"paused": True})
        self.assertEqual(
            json.loads(sent[1][sent[1].index("json-args") + 1]),
            {"public_key": AGENT_PUB},
        )
        withdraw = sent[2]
        self.assertEqual(json.loads(withdraw[withdraw.index("json-args") + 1]), {
            "asset": {"Ft": "token.gate.testnet"},
            "receiver_id": "cold.testnet",
            "amount": "999940000000",
        })
        self.assertIn("+ near contract call-function as-read-only gate.testnet get_agent", process.stdout)
        self.assertIn("+ near contract call-function as-read-only token.gate.testnet ft_balance_of", process.stdout)
        self.assertIn("+ near contract call-function as-read-only token.gate.testnet storage_balance_of", process.stdout)
        self.assertIn("code_hash: recorded", process.stdout)
        reads = [call for call in self.calls() if len(call) > 2 and call[2] == "as-read-only"]
        self.assertEqual(
            [(call[3], call[4], json.loads(call[call.index("json-args") + 1])) for call in reads],
            [
                ("gate.testnet", "get_agent", {"public_key": AGENT_PUB}),
                ("token.gate.testnet", "ft_balance_of", {"account_id": "gate.testnet"}),
                ("token.gate.testnet", "storage_balance_of", {"account_id": "cold.testnet"}),
            ],
        )
        self.assertIn(
            ["account", "view-account-summary", "gate.testnet", "network-config", "testnet", "now"],
            self.calls(),
        )
        self.assertIn("The Gate NEAR balance stays in place", process.stdout)

    def test_null_agent_skips_revoke_and_still_withdraws(self):
        self.write_answer("get_agent", "gate.testnet", "null")
        process = self.run_script("kill_switch.sh", AGENT_PUB)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn(
            f"warning: no agent entry for {AGENT_PUB} on gate.testnet: revoke_agent already ran or the key is not an agent key; not sent",
            process.stderr,
        )
        self.assertNotIn("no agent entry", process.stdout)
        self.assertEqual([call[4] for call in self.transactions()], ["set_paused", "withdraw"])

    def test_failed_pause_stops_without_state_report_or_later_calls(self):
        process = self.run_script(
            "kill_switch.sh",
            AGENT_PUB,
            env={**self.env, "FAIL_METHOD": "set_paused"},
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("simulated transaction failure", process.stderr)
        self.assertNotIn("Kill switch partial state", process.stderr)
        self.assertEqual([call[4] for call in self.transactions()], ["set_paused"])
        self.assertEqual(len(self.calls()), 1)

    def test_failed_revoke_reports_not_revoked_and_stops_after_revoke(self):
        process = self.run_script(
            "kill_switch.sh",
            AGENT_PUB,
            env={**self.env, "FAIL_METHOD": "revoke_agent"},
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("Agent key revoked: no in this run", process.stderr)
        self.assertEqual(
            [call[4] for call in self.transactions()], ["set_paused", "revoke_agent"]
        )
        calls = self.calls()
        self.assertEqual(calls[-1][4], "revoke_agent")
        self.assertEqual(len(calls), 3)
        manual_line = next(
            line for line in process.stderr.splitlines() if "Manual command" in line
        )
        manual_command = shlex.split(manual_line.split(": ", 1)[1])
        manual_args = json.loads(
            manual_command[manual_command.index("json-args") + 1]
        )
        self.assertEqual(manual_args["amount"], "<balance>")

    def test_non_object_agent_answer_exits_3_without_revoke(self):
        self.write_answer("get_agent", "gate.testnet", '"unexpected"')
        process = self.run_script("kill_switch.sh", AGENT_PUB)
        self.assertEqual(process.returncode, 3)
        self.assertIn("Agent key revoked: no in this run", process.stderr)
        self.assertEqual([call[4] for call in self.transactions()], ["set_paused"])

    def test_unparseable_read_exits_3_without_withdraw(self):
        self.write_answer("get_agent", "gate.testnet", "Empty return value")
        process = self.run_script("kill_switch.sh", AGENT_PUB)
        self.assertEqual(process.returncode, 3)
        self.assertIn("Empty return value", process.stderr)
        self.assertIn("Gate paused: yes", process.stderr)
        self.assertIn("Agent key revoked: no in this run", process.stderr)
        self.assertIn("Token balance moved: no", process.stderr)
        self.assertNotIn("withdraw", [call[4] for call in self.transactions()])

    def test_an_unparseable_balance_keeps_the_placeholder_in_the_manual_command(self):
        self.write_answer("ft_balance_of", "token.gate.testnet", '"abc"')
        process = self.run_script("kill_switch.sh", AGENT_PUB)
        self.assertEqual(process.returncode, 3)
        line = next(line for line in process.stderr.splitlines() if "Manual command" in line)
        args = shlex.split(line.split(": ", 1)[1])
        self.assertEqual(json.loads(args[args.index("json-args") + 1])["amount"], "<balance>")
        self.assertNotIn("withdraw", [call[4] for call in self.transactions()])

    def test_failed_read_preserves_command_exit_and_reports_state(self):
        process = self.run_script(
            "kill_switch.sh",
            AGENT_PUB,
            env={**self.env, "FAIL_READ_METHOD": "get_agent"},
        )
        self.assertEqual(process.returncode, 44)
        self.assertIn("simulated read failure", process.stderr)
        self.assertIn("Gate paused: yes", process.stderr)
        self.assertIn("Agent key revoked: no in this run", process.stderr)
        self.assertIn("Token balance moved: no", process.stderr)
        self.assertNotIn("revoke_agent", [call[4] for call in self.transactions()])

    def test_failed_withdraw_reports_completed_state(self):
        process = self.run_script(
            "kill_switch.sh",
            AGENT_PUB,
            env={**self.env, "FAIL_METHOD": "withdraw"},
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("Gate paused: yes", process.stderr)
        self.assertIn("Agent key revoked: yes", process.stderr)
        self.assertIn("Token balance moved: no", process.stderr)
        self.assertIn("run this script again", process.stderr)
        self.assertIn("near contract call-function as-transaction gate.testnet withdraw", process.stderr)
        manual_line = next(
            line for line in process.stderr.splitlines() if "Manual command" in line
        )
        manual_command = shlex.split(manual_line.split(": ", 1)[1])
        manual_args = json.loads(
            manual_command[manual_command.index("json-args") + 1]
        )
        self.assertEqual(manual_args["amount"], "999940000000")

    def test_zero_balance_skips_withdraw(self):
        self.write_answer("ft_balance_of", "token.gate.testnet", '"0"')
        process = self.run_script("kill_switch.sh", AGENT_PUB)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("the Gate holds no token.gate.testnet; nothing to withdraw", process.stdout)
        self.assertNotIn("withdraw", [call[4] for call in self.transactions()])
        self.assertNotIn("storage_balance_of", [call[4] for call in self.calls() if len(call) > 4 and call[2] == "as-read-only"])

    def test_unregistered_cold_account_stops_before_withdraw(self):
        self.write_answer("storage_balance_of", "token.gate.testnet", "null", account="cold.testnet")
        process = self.run_script("kill_switch.sh", AGENT_PUB)
        self.assertEqual(process.returncode, 3)
        self.assertIn("the cold account cold.testnet is not registered on token.gate.testnet", process.stderr)
        self.assertIn("set_paused and revoke_agent have run", process.stderr)
        self.assertIn("Gate paused: yes", process.stderr)
        self.assertIn("Agent key revoked: yes", process.stderr)
        self.assertIn("Token balance moved: no", process.stderr)
        self.assertEqual([call[4] for call in self.transactions()], ["set_paused", "revoke_agent"])

    def test_unregistered_cold_account_reports_skipped_revoke(self):
        self.write_answer("get_agent", "gate.testnet", "null")
        self.write_answer(
            "storage_balance_of", "token.gate.testnet", "null", account="cold.testnet"
        )
        process = self.run_script("kill_switch.sh", AGENT_PUB)
        self.assertEqual(process.returncode, 3)
        self.assertIn(
            "set_paused has run and revoke_agent was skipped", process.stderr
        )
        self.assertNotIn("revoke_agent have run", process.stderr)
        self.assertEqual([call[4] for call in self.transactions()], ["set_paused"])

    def test_cold_account_equal_to_gate_exits_2_before_any_call(self):
        process = self.run_script(
            "kill_switch.sh",
            AGENT_PUB,
            env={**self.env, "COLD_ACCOUNT_ID": "gate.testnet"},
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("COLD_ACCOUNT_ID must differ from GATE_ACCOUNT_ID", process.stderr)
        self.assertEqual(self.calls(), [])


class SetPausedTest(ScriptTest):
    def test_dry_run_prints_false(self):
        process = self.run_script("set_paused.sh", "false", "--dry-run")
        self.assertEqual(process.returncode, 0, process.stderr)
        args = self.command(process.stdout, "set_paused")
        self.assertEqual(json.loads(args[args.index("json-args") + 1]), {"paused": False})
        self.assertEqual(args[args.index("sign-as") + 1], "owner.testnet")
        self.assertEqual(args[args.index("attached-deposit") + 1], "1 yoctoNEAR")

    def test_rejects_other_pause_values(self):
        process = self.run_script("set_paused.sh", "yes")
        self.assertEqual(process.returncode, 2)
        self.assertIn("usage: set_paused.sh <true|false> [--dry-run]", process.stderr)
        self.assertEqual(process.stdout, "")


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
