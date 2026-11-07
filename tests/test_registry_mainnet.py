import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

try:
    import pty
except ImportError:
    pty = None

from gate import rpc
from tests.test_redeploy import bash_path
from tests.test_scripts import BASH, ScriptTest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = "deploy_registry_mainnet.sh"
REGISTRY = "registry.example.near"
OWNER = "owner.example.near"
RECORDER = "recorder.example.near"
TRIP_NEAR = """#!/bin/sh
printf '%s\\n' "$@" > "$NEAR_MARKER"
exit 99
"""


class RegistryMainnetTest(ScriptTest):
    def setUp(self):
        super().setUp()
        bin_dir = Path(self.tempdir.name) / "bin"
        (bin_dir / "near").write_bytes(TRIP_NEAR.encode("utf-8"))
        (bin_dir / "near").chmod(0o755)
        self.marker = Path(self.tempdir.name) / "near-called"
        self.allow_near_calls = False
        self.addCleanup(self.assert_near_not_called)
        self.wasm = bash_path(ROOT / "res" / "registry.wasm")
        self.env = {**self.env, "NETWORK": "mainnet", "MAINNET_DEPLOY_OK": "1", "DRY_RUN": "1",
                    "NEAR_MARKER": bash_path(self.marker)}
        for name in ("REDEPLOY", "EXPECT_VERSION", "EXPECT_CODE_HASH"):
            self.env.pop(name, None)

    def assert_near_not_called(self):
        if not self.allow_near_calls:
            self.assertFalse(self.marker.exists(), "near was invoked")

    def deploy(self, *args, **extra):
        return self.run_script(SCRIPT, *args, env={**self.env, **extra})

    def deploy_default(self, **extra):
        return self.deploy(REGISTRY, OWNER, RECORDER, self.wasm, **extra)

    def commands(self, stdout, prefix):
        return [shlex.split(line[2:]) for line in stdout.splitlines() if line.startswith(f"+ {prefix}")]

    def recorded(self):
        sha256 = (ROOT / "res" / "registry.wasm.sha256").read_text(encoding="utf-8").split()[0]
        lines = subprocess.run([sys.executable, "-m", "gate", "codehash", str(ROOT / "res" / "registry.wasm")],
                               capture_output=True, cwd=ROOT, check=True, text=True).stdout.splitlines()
        code_hash = next(line.split(": ", 1)[1] for line in lines if line.startswith("code_hash: "))
        return sha256, code_hash

    def deploy_with_build_record(self, build_record):
        root = Path(self.tempdir.name) / "isolated-root"
        scripts = root / "scripts"
        res = root / "res"
        scripts.mkdir(parents=True)
        res.mkdir()
        shutil.copyfile(ROOT / "scripts" / SCRIPT, scripts / SCRIPT)
        shutil.copyfile(ROOT / "scripts" / "lib.sh", scripts / "lib.sh")
        shutil.copyfile(ROOT / "res" / "registry.wasm.sha256", res / "registry.wasm.sha256")
        (res / "BUILD-registry.md").write_text(build_record, encoding="utf-8")
        env = {**self.env}
        pythonpath = [str(ROOT)]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        return subprocess.run(
            [BASH, bash_path(scripts / SCRIPT), REGISTRY, OWNER, RECORDER, self.wasm],
            capture_output=True, cwd=root, env=env, timeout=30, check=False, text=True,
        )

    def live_environment(self):
        env = {**self.env}
        env.pop("DRY_RUN")
        stub_dir = Path(self.tempdir.name) / "rpc_stub"
        stub_dir.mkdir()
        _, code_hash = self.recorded()
        (stub_dir / "sitecustomize.py").write_text(
            "from gate import rpc\n"
            f"rpc.Client.code_hash = lambda self, account: {code_hash!r}\n",
            encoding="utf-8",
        )
        # sitecustomize runs before python -m puts the working directory on sys.path, so the stub
        # can import gate only when the repository root is on PYTHONPATH.
        pythonpath = [str(stub_dir), str(ROOT)]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        return env

    def run_with_tty(self, confirmation, live_env=None):
        live_env = self.live_environment() if live_env is None else live_env
        pid, tty_fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(ROOT)
                os.execve(BASH, [BASH, f"scripts/{SCRIPT}", REGISTRY, OWNER, RECORDER, self.wasm], live_env)
            except OSError as error:
                os.write(2, f"could not start deploy script: {error}\n".encode())
                os._exit(127)

        output = bytearray()
        sent = False
        timed_out = False
        deadline = time.monotonic() + 30
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    os.kill(pid, signal.SIGKILL)
                    break
                readable, _, _ = select.select([tty_fd], [], [], remaining)
                if not readable:
                    timed_out = True
                    os.kill(pid, signal.SIGKILL)
                    break
                try:
                    chunk = os.read(tty_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if not sent and b"type the registry account name to continue:" in output:
                    os.write(tty_fd, f"{confirmation}\n".encode())
                    sent = True
        finally:
            _, status = os.waitpid(pid, 0)
            os.close(tty_fd)
        if timed_out:
            self.fail("deploy script did not finish within 30 seconds")
        if not sent:
            self.fail(f"deploy script did not reach the confirmation prompt: {output.decode(errors='replace')}")
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace")


class RegistryMainnetGuardTest(RegistryMainnetTest):
    def test_bash_parses_the_script(self):
        process = subprocess.run([BASH, "-n", f"scripts/{SCRIPT}"], capture_output=True, cwd=ROOT,
                                 env=self.env, timeout=30, check=False, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_the_trip_wire_near_fails_when_called(self):
        process = subprocess.run([BASH, "-c", "near contract deploy"], capture_output=True, cwd=ROOT,
                                 env=self.env, timeout=30, check=False, text=True)
        self.assertEqual(process.returncode, 99)
        self.assertTrue(self.marker.exists())
        self.marker.unlink()

    def test_refuses_without_network_mainnet(self):
        for network in (None, "testnet"):
            with self.subTest(network=network):
                env = {**self.env}
                env.pop("NETWORK")
                if network:
                    env["NETWORK"] = network
                process = self.run_script(SCRIPT, REGISTRY, OWNER, RECORDER, self.wasm, env=env)
                self.assertEqual(process.returncode, 2)
                self.assertIn("NETWORK must be mainnet for this script", process.stderr)
                self.assertEqual(process.stdout, "")

    def test_refuses_without_mainnet_deploy_ok(self):
        for value in (None, "0", "yes"):
            with self.subTest(value=value):
                env = {**self.env}
                env.pop("MAINNET_DEPLOY_OK")
                if value is not None:
                    env["MAINNET_DEPLOY_OK"] = value
                process = self.run_script(SCRIPT, REGISTRY, OWNER, RECORDER, self.wasm, env=env)
                self.assertEqual(process.returncode, 2)
                self.assertIn("MAINNET_DEPLOY_OK=1 is required", process.stderr)
                self.assertEqual(process.stdout, "")

    def test_refuses_without_near_keys_dir(self):
        env = {**self.env}
        env.pop("NEAR_KEYS_DIR")
        process = self.run_script(SCRIPT, REGISTRY, OWNER, RECORDER, self.wasm, env=env)
        self.assertEqual(process.returncode, 2)
        self.assertIn("missing required environment variable: NEAR_KEYS_DIR", process.stderr)
        self.assertEqual(process.stdout, "")

    def test_refuses_a_build_record_without_a_40_hex_source_commit(self):
        process = self.deploy_with_build_record("Source commit: not-a-commit\n")
        self.assertEqual(process.returncode, 2)
        self.assertIn("res/BUILD-registry.md has no 40 hex Source commit line", process.stderr)
        self.assertNotIn("near contract deploy", process.stdout)

    def test_refuses_a_wasm_whose_sha256_does_not_match(self):
        other = Path(self.tempdir.name) / "other.wasm"
        other.write_bytes(b"\x00asm\x01\x00\x00\x00")
        process = self.deploy(REGISTRY, OWNER, RECORDER, bash_path(other))
        self.assertEqual(process.returncode, 2)
        self.assertIn("does not equal res/registry.wasm.sha256", process.stderr)
        self.assertNotIn("+ near", process.stdout)

    def test_refuses_a_missing_code_hash_from_codehash_command(self):
        sha256, _ = self.recorded()
        python3 = Path(self.tempdir.name) / "bin" / "python3"
        wrapper = python3.read_text(encoding="utf-8")
        shim = (
            'if [ "$1" = "-m" ] && [ "$2" = "gate" ] && [ "$3" = "codehash" ]; then\n'
            f"  printf 'sha256: %s\\n' '{sha256}'\n"
            "  exit 0\n"
            "fi\n"
        )
        python3.write_text(wrapper.replace("exec ", shim + "exec ", 1), encoding="utf-8")
        process = self.deploy_default()
        self.assertEqual(process.returncode, 2)
        self.assertIn("invalid code_hash from python -m gate codehash", process.stderr)
        self.assertNotIn("near contract deploy", process.stdout)

    def test_refuses_a_nonempty_malformed_code_hash_from_codehash_command(self):
        sha256, _ = self.recorded()
        python3 = Path(self.tempdir.name) / "bin" / "python3"
        wrapper = python3.read_text(encoding="utf-8")
        for code_hash in ("not-base58!", "2"):
            with self.subTest(code_hash=code_hash):
                shim = (
                    'if [ "$1" = "-m" ] && [ "$2" = "gate" ] && [ "$3" = "codehash" ]; then\n'
                    f"  printf 'sha256: %s\\ncode_hash: %s\\n' '{sha256}' '{code_hash}'\n"
                    "  exit 0\n"
                    "fi\n"
                )
                python3.write_text(wrapper.replace("exec ", shim + "exec ", 1), encoding="utf-8")
                process = self.deploy_default()
                self.assertEqual(process.returncode, 2)
                self.assertIn("invalid code_hash from python -m gate codehash", process.stderr)
                self.assertNotIn("near contract deploy", process.stdout)

    def test_live_guard_refuses_existing_code_without_redeploy(self):
        if pty is None or os.name != "posix":
            self.skipTest("requires POSIX pty support to answer the script's /dev/tty confirmation")
        returncode, output = self.run_with_tty(REGISTRY)
        self.assertEqual(returncode, 2, output)
        self.assertIn(f"{REGISTRY} already holds code; set REDEPLOY=1", output)
        self.assert_near_not_called()

    @unittest.skipIf(os.name == "nt", "Git Bash /dev/tty cannot be isolated reliably from Windows consoles")
    def test_live_guard_refuses_without_a_terminal(self):
        env = self.live_environment()
        process = subprocess.run(
            [BASH, f"scripts/{SCRIPT}", REGISTRY, OWNER, RECORDER, self.wasm],
            capture_output=True,
            cwd=ROOT,
            env=env,
            timeout=30,
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn("type the registry account name to continue", process.stdout)
        self.assertIn("/dev/tty", process.stderr)
        self.assert_near_not_called()

    def test_live_guard_refuses_wrong_confirmation(self):
        if pty is None or os.name != "posix":
            self.skipTest("requires POSIX pty support to send wrong /dev/tty confirmation text")
        returncode, output = self.run_with_tty("wrong-account.near")
        self.assertEqual(returncode, 2, output)
        self.assertIn(f"confirmation does not match {REGISTRY}, nothing was sent", output)
        self.assert_near_not_called()

    def test_refuses_a_missing_wasm(self):
        process = self.deploy(REGISTRY, OWNER, RECORDER, bash_path(Path(self.tempdir.name) / "absent.wasm"))
        self.assertEqual(process.returncode, 2)
        self.assertIn("wasm file not found", process.stderr)

    def test_refuses_recorder_equal_to_owner(self):
        process = self.deploy(REGISTRY, OWNER, OWNER, self.wasm)
        self.assertEqual(process.returncode, 2)
        self.assertIn("the recorder account must differ from the owner account", process.stderr)
        self.assertEqual(process.stdout, "")

    def test_refuses_implicit_and_non_near_accounts(self):
        for position in range(3):
            for bad in ("a" * 64, "registry.example.testnet", "near", "Registry.example.near",
                        "registry..near", "-registry.near", "registry.near.", "a" * 60 + ".near"):
                with self.subTest(position=position, account=bad):
                    accounts = [REGISTRY, OWNER, RECORDER]
                    accounts[position] = bad
                    process = self.deploy(*accounts, self.wasm)
                    self.assertEqual(process.returncode, 2)
                    self.assertIn(f"not a named mainnet account ending in .near: {bad}", process.stderr)
                    self.assertEqual(process.stdout, "")

    def test_refuses_wrong_arity(self):
        process = self.deploy(REGISTRY, OWNER, RECORDER)
        self.assertEqual(process.returncode, 2)
        self.assertIn("usage: deploy_registry_mainnet.sh <registry_account> <owner_account> "
                      "<recorder_account> <wasm>", process.stderr)

    def test_redeploy_requires_both_expected_values(self):
        for extra in ({}, {"EXPECT_VERSION": "0.1.0"}, {"EXPECT_CODE_HASH": "Expected1111"}):
            with self.subTest(extra=extra):
                process = self.deploy_default(REDEPLOY="1", **extra)
                self.assertEqual(process.returncode, 2)
                self.assertIn("missing required environment variable: EXPECT_", process.stderr)


class RegistryMainnetDryRunTest(RegistryMainnetTest):
    def test_dry_run_prints_the_exact_deploy_line(self):
        process = self.deploy_default()
        self.assertEqual(process.returncode, 0, process.stderr)
        key_path = f'{self.env["NEAR_KEYS_DIR"]}/{REGISTRY}.json'
        args = json.dumps({"owner_id": OWNER, "recorder_id": RECORDER}, separators=(",", ":"))
        self.assertEqual(self.commands(process.stdout, "near contract deploy"), [[
            "near", "contract", "deploy", REGISTRY, "use-file", self.wasm, "with-init-call", "new",
            "json-args", args, "prepaid-gas", "100.0 Tgas", "attached-deposit", "0 NEAR",
            "network-config", "mainnet", "sign-with-access-key-file", key_path, "send",
        ]])

    def test_dry_run_prints_provenance_and_the_read_back_assertions(self):
        sha256, code_hash = self.recorded()
        process = self.deploy_default()
        self.assertEqual(process.returncode, 0, process.stderr)
        source_commit = next(line.split(": ", 1)[1] for line in
                             (ROOT / "res" / "BUILD-registry.md").read_text(encoding="utf-8").splitlines()
                             if line.startswith("Source commit: "))
        self.assertIn(f"source commit: {source_commit}", process.stdout)
        self.assertIn(f"wasm sha256: {sha256} (equals res/registry.wasm.sha256)", process.stdout)
        self.assertIn(f"expected code_hash: {code_hash}", process.stdout)
        self.assertIn(f"would refuse if {REGISTRY} holds code, since REDEPLOY is not 1", process.stdout)
        reads = self.commands(process.stdout, "near contract call-function as-read-only")
        self.assertEqual([read[4:7] for read in reads],
                         [[REGISTRY, "get_recorder", "json-args"], [REGISTRY, "version", "json-args"]])
        self.assertIn(f"would assert: get_recorder of {REGISTRY} equals {RECORDER}", process.stdout)
        self.assertIn(f"would assert: code_hash of {REGISTRY} equals {code_hash}", process.stdout)
        self.assertIn("owner: not read back, the registry has no owner view", process.stdout)
        self.assertIn("  network: mainnet", process.stdout)
        lines = process.stdout.splitlines()
        deploy = next(i for i, line in enumerate(lines) if line.startswith("+ near contract deploy"))
        check = next(i for i, line in enumerate(lines) if line.startswith("would check: code_hash"))
        first_read = next(i for i, line in enumerate(lines) if " as-read-only " in line)
        self.assertTrue(check < deploy < first_read)

    def test_dry_run_redeploy_deploys_without_an_init_call(self):
        _, code_hash = self.recorded()
        process = self.deploy_default(REDEPLOY="1", EXPECT_VERSION="0.2.0", EXPECT_CODE_HASH=code_hash)
        self.assertEqual(process.returncode, 0, process.stderr)
        key_path = f'{self.env["NEAR_KEYS_DIR"]}/{REGISTRY}.json'
        self.assertEqual(self.commands(process.stdout, "near contract deploy"), [[
            "near", "contract", "deploy", REGISTRY, "use-file", self.wasm, "without-init-call",
            "network-config", "mainnet", "sign-with-access-key-file", key_path, "send",
        ]])
        self.assertIn(f"would assert: version of {REGISTRY} equals 0.2.0", process.stdout)
        self.assertIn(f"would assert: code_hash of {REGISTRY} equals {code_hash}", process.stdout)
        self.assertNotIn("with-init-call", process.stdout)

    def test_redeploy_refuses_an_expected_code_hash_that_differs_from_the_wasm(self):
        _, code_hash = self.recorded()
        process = self.deploy_default(REDEPLOY="1", EXPECT_VERSION="0.2.0", EXPECT_CODE_HASH="Expected1111")
        self.assertEqual(process.returncode, 2)
        self.assertIn(f"EXPECT_CODE_HASH Expected1111 does not equal the code_hash of {self.wasm}, {code_hash}",
                      process.stderr)
        self.assertNotIn("near contract deploy", process.stdout)
        self.assertNotIn("would check: code_hash", process.stdout)

    def test_dry_run_needs_no_confirmation(self):
        process = subprocess.run([BASH, f"scripts/{SCRIPT}", REGISTRY, OWNER, RECORDER, self.wasm],
                                 capture_output=True, cwd=ROOT, env=self.env, timeout=30, check=False,
                                 text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn("type the registry account name", process.stdout)

    def test_dry_run_flag_works_like_the_variable(self):
        env = {**self.env}
        env.pop("DRY_RUN")
        process = self.run_script(SCRIPT, REGISTRY, OWNER, RECORDER, self.wasm, "--dry-run", env=env)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("+ near contract deploy", process.stdout)


REGISTRY_NEAR = r"""#!/bin/sh
printf '%s\n' "$*" >> "$REGISTRY_NEAR_LOG"
if [ "$2" = "view-account-summary" ]; then
  printf 'account summary\n'
  exit 0
fi
if [ "$2" = "deploy" ]; then
  printf 'Transaction ID: 4QkqgW9n6rN8yP7LhXmVv3dEzC2bA1uT5sR6qJ8kH9gS\n'
  exit 0
fi
if [ "$3" = "as-read-only" ]; then
  case "$5" in
    get_recorder) value=$RECORDER_VALUE ;;
    version) value=$VERSION_VALUE ;;
    *) printf 'unexpected read request\n' >&2; exit 42 ;;
  esac
  printf 'Function execution return value (printed to stdout):\n%s\n\n\n' "$value"
  printf 'Here is your console command if you need to script it or re-run:\n'
  exit 0
fi
printf 'unexpected request\n' >&2
exit 42
"""


class RegistryMainnetLiveAssertionTest(RegistryMainnetTest):
    def setUp(self):
        super().setUp()
        self.allow_near_calls = True
        bin_dir = Path(self.tempdir.name) / "bin"
        near = bin_dir / "near"
        near.write_text(REGISTRY_NEAR, encoding="utf-8")
        near.chmod(0o755)
        sleep = bin_dir / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        sleep.chmod(0o755)
        self.near_log = Path(self.tempdir.name) / "registry-near.log"
        self.rpc_marker = Path(self.tempdir.name) / "rpc-read"
        self.expected_code_hash = self.recorded()[1]
        self.env = {
            **self.env,
            "REGISTRY_NEAR_LOG": bash_path(self.near_log),
            "CODEHASH_MARKER": bash_path(self.rpc_marker),
            "INITIAL_CODE_HASH": rpc.EMPTY_CODE_HASH,
            "POST_CODE_HASH": self.expected_code_hash,
            "RECORDER_VALUE": json.dumps(RECORDER),
            "VERSION_VALUE": json.dumps("0.1.0"),
            "DRY_RUN": "0",
        }

    def set_code_hash_rpc(self, initial=None, after=None, missing=False, live_env=None):
        live_env = self.live_environment() if live_env is None else live_env
        sitecustomize = Path(live_env["PYTHONPATH"].split(os.pathsep)[0]) / "sitecustomize.py"
        if missing:
            code = (
                "from gate import rpc\n"
                "def missing(self, account):\n"
                "    raise rpc.RpcError({'name': 'HANDLER_ERROR', "
                "'cause': {'name': 'UNKNOWN_ACCOUNT'}})\n"
                "rpc.Client.code_hash = missing\n"
            )
        else:
            if self.rpc_marker.exists():
                self.rpc_marker.unlink()
            live_env["INITIAL_CODE_HASH"] = initial
            live_env["POST_CODE_HASH"] = after
            code = (
                "import os\n"
                "from pathlib import Path\n"
                "from gate import rpc\n"
                "marker = Path(os.environ['CODEHASH_MARKER'])\n"
                "def code_hash(self, account):\n"
                "    if not marker.exists():\n"
                "        marker.write_text('initial', encoding='utf-8')\n"
                "        return os.environ['INITIAL_CODE_HASH']\n"
                "    return os.environ['POST_CODE_HASH']\n"
                "rpc.Client.code_hash = code_hash\n"
            )
        sitecustomize.write_text(code, encoding="utf-8")
        return live_env

    def run_live(self, confirmation=REGISTRY, initial=None, after=None, missing=False, **environment):
        if pty is None or os.name != "posix":
            self.skipTest("requires POSIX pty support to answer the script's /dev/tty confirmation")
        env = self.live_environment()
        env.update(self.env)
        env.update(environment)
        env = self.set_code_hash_rpc(initial=initial, after=after, missing=missing, live_env=env)
        return self.run_with_tty(confirmation, env)

    def test_live_first_deploy_asserts_recorder_and_code_hash(self):
        returncode, output = self.run_live(initial=rpc.EMPTY_CODE_HASH, after=self.expected_code_hash)
        self.assertEqual(returncode, 0, output)
        self.assertIn(f"asserted: get_recorder of {REGISTRY} equals {RECORDER}", output)
        self.assertIn(f"asserted: code_hash of {REGISTRY} equals {self.expected_code_hash}", output)
        self.assertIn("contract deploy", self.near_log.read_text(encoding="utf-8"))

    def test_live_first_deploy_refuses_a_different_recorder(self):
        self.env["RECORDER_VALUE"] = json.dumps("wrong.example.near")
        returncode, output = self.run_live(initial=rpc.EMPTY_CODE_HASH, after=self.expected_code_hash)
        self.assertEqual(returncode, 1, output)
        self.assertIn(f"get_recorder of {REGISTRY} is wrong.example.near, expected {RECORDER}", output)
        self.assertNotIn(f"asserted: code_hash of {REGISTRY}", output)

    def test_live_redeploy_refuses_an_empty_registry(self):
        returncode, output = self.run_live(
            initial=rpc.EMPTY_CODE_HASH, after=self.expected_code_hash, REDEPLOY="1",
            EXPECT_VERSION="0.1.0", EXPECT_CODE_HASH=self.expected_code_hash,
        )
        self.assertEqual(returncode, 2, output)
        self.assertIn(f"{REGISTRY} holds no code, so there is nothing to redeploy", output)
        self.assertFalse(self.near_log.exists())

    def test_live_redeploy_refuses_a_version_mismatch(self):
        returncode, output = self.run_live(
            initial="existing-code-hash", after=self.expected_code_hash, REDEPLOY="1",
            EXPECT_VERSION="0.2.0", EXPECT_CODE_HASH=self.expected_code_hash,
        )
        self.assertEqual(returncode, 1, output)
        self.assertIn(f"version of {REGISTRY} is 0.1.0, expected 0.2.0", output)

    def test_live_first_deploy_refuses_a_code_hash_mismatch(self):
        returncode, output = self.run_live(initial=rpc.EMPTY_CODE_HASH, after="different-code-hash")
        self.assertEqual(returncode, 1, output)
        self.assertIn(
            f"code_hash of {REGISTRY} is different-code-hash, expected {self.expected_code_hash}",
            output,
        )

    def test_live_deploy_refuses_a_missing_registry_account(self):
        if pty is None or os.name != "posix":
            self.skipTest("requires POSIX pty support to answer the script's /dev/tty confirmation")
        env = self.set_code_hash_rpc(missing=True)
        returncode, output = self.run_with_tty(REGISTRY, env)
        self.assertEqual(returncode, 2, output)
        self.assertIn(f"registry account {REGISTRY} not found on mainnet", output)
        self.assertFalse(self.near_log.exists())


class RegistryMainnetCodeHashReadTest(unittest.TestCase):
    """The inline python that reads the live code hash, run offline with the RPC client replaced."""

    def snippet(self):
        script = (ROOT / "scripts" / SCRIPT).read_text(encoding="utf-8")
        body = script.split("read_code_hash() {\n  python3 - \"$1\" <<'PY'\n", 1)[1]
        return body.split("\nPY\n", 1)[0]

    def read(self, fake):
        code = f"from gate import rpc\nrpc.Client.code_hash = {fake}\n" + self.snippet()
        return subprocess.run([sys.executable, "-c", code, REGISTRY], capture_output=True, cwd=ROOT,
                              timeout=30, check=False, text=True)

    def test_an_unknown_account_stops_with_one_line(self):
        process = self.read("lambda self, account: (_ for _ in ()).throw(rpc.RpcError("
                            "{'name': 'HANDLER_ERROR', 'cause': {'name': 'UNKNOWN_ACCOUNT'}}))")
        self.assertEqual(process.returncode, 2)
        self.assertEqual(process.stderr, f"registry account {REGISTRY} not found on mainnet\n")
        self.assertEqual(process.stdout, "")

    def test_another_rpc_error_stops_with_one_line(self):
        process = self.read("lambda self, account: (_ for _ in ()).throw(rpc.RpcError("
                            "{'name': 'HANDLER_ERROR', 'cause': {'name': 'TIMEOUT_ERROR'}}))")
        self.assertEqual(process.returncode, 2)
        self.assertEqual(len(process.stderr.splitlines()), 1, process.stderr)
        self.assertIn(f"view_account of {REGISTRY} failed: ", process.stderr)
        self.assertNotIn("Traceback", process.stderr)

    def test_a_code_hash_is_printed(self):
        process = self.read("lambda self, account: rpc.EMPTY_CODE_HASH")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), "11111111111111111111111111111111")


if __name__ == "__main__":
    unittest.main()
