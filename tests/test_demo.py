import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import demo, intents, rpc, signer

DENIED = ("DENIED. kestrelridge-sh0p.testnet is a lookalike of kestrelridge-shop.testnet. Created less than 7 d ago. "
          "Not on the owner's allowlist. Not registered on the token.")
EVM_DENIED = ("  DENIED. The recipient 0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107 carries the GoPlus theft labels "
              "stealing_attack")
RECORDED_URLS = (rpc.TESTNET_RPC_URL, rpc.ARCHIVAL_TESTNET_RPC_URL, rpc.NEARBLOCKS_TESTNET_URL)
ROOT = Path(__file__).resolve().parent.parent


def no_network(*args, **kwargs):
    raise AssertionError("the demo opened the network")


def check(result, check_id):
    return next(c for c in result["checks"] if c["id"] == check_id)


class DemoTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("urllib.request.urlopen", side_effect=no_network)
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)

    def run_demo(self):
        lines = []
        results = demo.run(out=lines.append)
        self.addCleanup(shutil.rmtree, Path(results[0]["evidence_path"]).parent)
        return results, lines

    def test_four_outcomes(self):
        results, lines = self.run_demo()
        self.assertEqual([r["outcome"] for r in results], ["ALLOW", "DENY", "UNKNOWN", "DENY"])
        self.assertIsNotNone(results[0]["verdict"])
        for result in results[1:]:
            self.assertIsNone(result["verdict"])
        self.assertIn(f"  {DENIED}", lines)
        self.assertIn("  ALLOW, signed with the TEST key (key id cur); signature verified with gate.ed25519: True",
                      lines)
        self.assertIn("  UNKNOWN, not signed: account_age, dust_funded could not run", lines)
        self.assertTrue(any(line.startswith(EVM_DENIED) for line in lines))
        self.assertIn("  Nothing was signed, nothing was deposited.", lines)
        self.assertEqual(sum(line.startswith("  evidence sha256: ") for line in lines), 4)
        self.assertEqual(sum(line.startswith("  latency: ") and line.endswith(" ms") for line in lines), 4)
        self.assertTrue(any(line.startswith("  canonical message: GATEv1|kestrelridge-main.testnet|")
                            for line in lines))
        for result in results:
            path = Path(result["evidence_path"])
            self.assertTrue(path.exists())
            self.assertEqual(path.stem, result["evidence_hash"])
            self.assertIn(f"  evidence sha256: {result['evidence_hash']}", lines)

    def test_the_lookalike_is_dated_by_its_oldest_access_key(self):
        results, _ = self.run_demo()
        age = check(results[1], "account_age")
        self.assertEqual(age["status"], "signal")
        self.assertIn("oldest access key nonce 275346461000000 from test.rpc.fastnear.com: existed by block "
                      "275346462 (2026-10-29T19:57:35Z) from archival-rpc.testnet.fastnear.com, younger than 7 days",
                      age["detail"])
        self.assertEqual(check(results[1], "lookalike")["status"], "signal")
        self.assertEqual(check(results[1], "storage_registered")["status"], "signal")

    def test_the_unknown_is_the_left_out_archival_block(self):
        results, lines = self.run_demo()
        age = check(results[2], "account_age")
        self.assertEqual(age["status"], "unknown")
        self.assertTrue(age["detail"].endswith("archival block_at could not run: no recorded response for the "
                                               "request"))
        key = rpc.fixture_key(rpc.ARCHIVAL_TESTNET_RPC_URL, "block", {"block_id": 275336351})
        self.assertFalse((demo.DEMO_DIR / f"{key}.json").exists())
        self.assertTrue(any("left out of fixtures/demo" in line for line in lines))

    def test_the_policy_is_the_recorded_get_agent_answer(self):
        policy = json.loads((demo.DEMO_DIR / "policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["allowlist"], [demo.VENDOR])
        self.assertEqual(policy["caps"][0]["asset"], {"Ft": demo.TOKEN})

    def test_command_exit_codes(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(gate_main.main(["demo", "--offline"]), 0)
        self.addCleanup(shutil.rmtree, output.getvalue().splitlines()[-1].split(": ", 1)[1])
        self.assertIn(DENIED, output.getvalue())
        with (mock.patch("gate.verdict.request", side_effect=RuntimeError("broken")),
              contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError)):
            demo.main(offline=True)

    def test_the_evidence_directory_is_removed_unless_kept(self):
        for keep in (None, "1"):
            with self.subTest(keep=keep), tempfile.TemporaryDirectory() as tmp:
                env = {key: value for key, value in os.environ.items() if key != "GATE_KEEP_EVIDENCE"}
                env.update({"GATE_FIXTURES_MODE": "replay", "TMPDIR": tmp, "TEMP": tmp, "TMP": tmp})
                if keep:
                    env["GATE_KEEP_EVIDENCE"] = keep
                process = subprocess.run([sys.executable, "-m", "gate", "demo", "--offline"], capture_output=True,
                                         cwd=ROOT, env=env, timeout=120, check=False)
                self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))
                last = process.stdout.decode("utf-8").splitlines()[-1]
                self.assertTrue(last.startswith("Evidence documents, removed at exit unless GATE_KEEP_EVIDENCE=1: "),
                                last)
                directory = Path(last.split(": ", 1)[1])
                self.assertEqual(directory.parent, Path(tmp).resolve())
                if keep:
                    self.assertEqual(os.listdir(tmp), [directory.name])
                    self.assertEqual(len(list(directory.glob("*.json"))), 4)
                else:
                    self.assertEqual(os.listdir(tmp), [])

    def test_live_reads_testnet_and_a_dry_quote_and_writes_no_fixture(self):
        policy = json.loads((demo.DEMO_DIR / "policy.json").read_text(encoding="utf-8"))
        near_results, quotes = [], []

        def fake_request(*args, **kwargs):
            near_results.append((args, kwargs))
            return {"outcome": "DENY", "verdict": None, "checks": [], "reasons": ["Recorded by the test."],
                    "evidence_hash": "0" * 64, "evidence_path": str(Path(kwargs["evidence_dir"]) / "x.json"),
                    "latency_ms": 1, "message": None}

        def fake_quote(*args, **kwargs):
            quotes.append((args, kwargs))
            return {"verdict": "DENY", "checks": [], "reasons": ["Recorded by the test."], "quote": None,
                    "document": {"kind": "intents"}}

        output = io.StringIO()
        with (mock.patch("gate.rpc.Client.call_function", return_value=policy) as view,
              mock.patch("gate.verdict.request", side_effect=fake_request),
              mock.patch("gate.intents.judge_quote", side_effect=fake_quote),
              contextlib.redirect_stdout(output)):
            self.assertEqual(gate_main.main(["demo", "--live"]), 0)
        self.addCleanup(shutil.rmtree, output.getvalue().splitlines()[-1].split(": ", 1)[1])
        self.urlopen.assert_not_called()
        view.assert_called_once_with(demo.GATE, "get_agent", {"public_key": demo.AGENT_KEY})
        self.assertEqual([args[2] for args, _ in near_results], [demo.VENDOR, demo.LOOKALIKE, demo.AGED])
        for _, kwargs in near_results:
            clients = (kwargs["rpc"], kwargs["nearblocks"], kwargs["archival"])
            self.assertEqual([c.url for c in clients], [rpc.TESTNET_RPC_URL, rpc.NEARBLOCKS_TESTNET_URL,
                                                        rpc.ARCHIVAL_TESTNET_RPC_URL])
            self.assertEqual({c.mode for c in clients}, {"off"})
            self.assertIsInstance(kwargs["signer"], signer.TestSigner)
            self.assertEqual(kwargs["policy"], policy)
        [(args, kwargs)] = quotes
        self.assertEqual(args[3], demo.EVM_RECIPIENT)
        self.assertEqual(kwargs["mode"], "off")
        self.assertNotEqual(kwargs["deadline"], intents.RECORDED_DEADLINE)
        self.assertIn("nothing is sent", output.getvalue())

    def test_the_fixtures_are_reduced_recordings_under_their_replay_keys(self):
        paths = sorted(p for p in demo.DEMO_DIR.iterdir()
                       if p.name not in ("policy.json", "injected-message.txt"))
        self.assertEqual(len(paths), 12)
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                fixture = json.loads(text)
                self.assertEqual(sorted(fixture), ["fetched_at", "request", "response", "url"])
                self.assertTrue(fixture["url"].startswith(RECORDED_URLS))
                self.assertTrue(fixture["fetched_at"].startswith(("2026-11-01T", "2026-11-02T")))
                key = rpc.fixture_key(fixture["url"], fixture["request"]["method"], fixture["request"]["params"])
                self.assertEqual(path.stem, key)
                self.assertNotIn("secret", text.lower())
                self.assertNotIn("private", text.lower())
                if fixture["request"]["method"] == "block":
                    self.assertEqual(sorted(fixture["response"]["result"]), ["header"])
                    self.assertEqual(sorted(fixture["response"]["result"]["header"]), ["hash", "height", "timestamp"])


if __name__ == "__main__":
    unittest.main()
