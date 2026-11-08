import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import demo, evidence, rpc, signer, verdict
from tests.test_rules import ACCOUNT_HEIGHT, DAY_NS, KEY_HEIGHT, KEY_NONCE, OLD_NS, STATE_HEIGHT, RulesCase, request

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "fixtures" / "evidence"
TIER2_RUNS = ROOT / "fixtures" / "latency" / "verdict-runs-tier2.json"
# Recorded evidence documents: four live tier 1 ALLOW verdicts for the allowlisted vendor, the tier 2 ALLOW for
# examples.testnet whose account age NearBlocks dated, the lookalike DENY whose account age could not be read, and
# the aged payee DENY of 1 Nov, dated by a code path the rules no longer run.
TIER1_DOCUMENTS = [next(EVIDENCE.glob(f"{prefix}*.json"))
                   for prefix in ("4b99f7ed", "5cd25837", "7e77187d", "cfca1a63")]
TIER2_DOCUMENT = next(EVIDENCE.glob("9471fea5*.json"))
LOOKALIKE_DOCUMENT = next(EVIDENCE.glob("c5d02152*.json"))
SUPERSEDED_DOCUMENT = next(EVIDENCE.glob("ff5a89a2*.json"))
POLICY = {"allowlist": ["kestrelridge-shop.testnet"], "caps": []}


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def answering(*answers):
    """A stand in for gate.verdict.request that answers each call with the next recorded evidence document, in the
    shape gate.verdict.request returns, or raises the next answer when it is an exception; and its calls."""
    queue, calls = list(answers), []

    def request_stub(kind, token, receiver, amount, memo, **options):
        calls.append(dict(options, kind=kind, token=token, receiver=receiver, amount=amount, memo=memo))
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        document = read(answer)
        return {"outcome": document["verdict"], "checks": document["checks"], "reasons": document["reasons"],
                "evidence_hash": Path(answer).stem, "evidence_path": str(answer), "latency_ms": document["latency_ms"]}

    return request_stub, calls


class RecordLatencyTest(unittest.TestCase):
    def setUp(self):
        self.recorder = load("record_latency")
        self.counts = load("counts")
        self.live_setup = self.recorder.live_setup
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.out = self.tmp / "verdict-runs.json"
        self.clients = (mock.Mock(name="rpc"), mock.Mock(name="nearblocks"), None, mock.Mock(name="archival"))
        environment = mock.patch.dict(os.environ, {"VERIFIER_KEY_FILE": str(self.tmp / "no-such-key.json")})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("REPORTS_ACCOUNT_ID", "GATE_FIXTURES_MODE"):
            os.environ.pop(name, None)
        network = AssertionError("the test opened the network")
        for patcher in (mock.patch.object(self.recorder, "live_setup", return_value=(self.clients, POLICY)),
                        mock.patch("urllib.request.urlopen", side_effect=network)):
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = mock.patch.object(self.recorder.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def run_main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.recorder.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def record(self, receiver, *answers, replace=False):
        request_stub, calls = answering(*answers)
        with mock.patch.object(verdict, "request", side_effect=request_stub):
            code, stdout, stderr = self.run_main("--receiver", receiver, "--runs", str(len(answers)), "--out",
                                                 str(self.out), *(["--replace"] if replace else []))
        return code, stdout, stderr, calls

    def test_the_fixture_has_the_committed_shape_and_counts_reads_the_line_it_prints(self):
        code, stdout, stderr, _ = self.record("kestrelridge-shop.testnet", *TIER1_DOCUMENTS)
        self.assertEqual(code, 0, stderr)
        rows = read(self.out)
        self.assertEqual(rows, [{"receiver": "kestrelridge-shop.testnet", "run": run, "verdict": "ALLOW",
                                 "latency_ms": read(path)["latency_ms"], "path": "tier 1"}
                                for run, path in enumerate(TIER1_DOCUMENTS, start=1)])
        committed = read(TIER2_RUNS)
        self.assertEqual([list(row) for row in rows], [list(committed[0])] * len(rows))
        self.assertEqual(TIER2_RUNS.read_bytes(), (json.dumps(committed, indent=2) + "\n").encode("utf-8"))
        self.assertEqual(self.out.read_bytes(), (json.dumps(rows, indent=2) + "\n").encode("utf-8"))
        line = self.counts.latency_line(self.out)
        self.assertEqual(line, "Latency: p50 1794 ms, p95 2351 ms over 4 verdict runs.")
        self.assertIn(line, stdout.splitlines())
        self.assertEqual(self.sleep.call_args_list, [mock.call(self.recorder.RUN_GAP_S)] * 3)

    def test_a_payee_off_the_allowlist_gets_the_committed_path_and_the_second_line(self):
        code, stdout, stderr, _ = self.record("examples.testnet", TIER2_DOCUMENT, TIER2_DOCUMENT)
        self.assertEqual(code, 0, stderr)
        rows = read(self.out)
        self.assertEqual([row["path"] for row in rows], ["tier 1 and 2, account age from the NearBlocks key creation time"] * 2)
        self.assertEqual([row["latency_ms"] for row in rows], [read(TIER2_DOCUMENT)["latency_ms"]] * 2)
        line = self.counts.tier2_latency_line(self.out)
        self.assertEqual(line, "Latency, payee off the allowlist: p50 13477 ms, p95 13477 ms over 2 verdict runs.")
        self.assertIn(line, stdout.splitlines())
        self.assertNotIn(self.counts.latency_line(self.out), stdout.splitlines())

    def test_every_run_is_the_verdict_service_signing_with_the_test_key_only(self):
        code, _, stderr, calls = self.record("kestrelridge-shop.testnet", *TIER1_DOCUMENTS[:2])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIsInstance(call["signer"], signer.TestSigner)
            self.assertNotIn("sign_actions", call)
            self.assertEqual((call["kind"], call["token"], call["receiver"], call["amount"], call["memo"]),
                             ("ft", demo.TOKEN, "kestrelridge-shop.testnet", demo.AMOUNT, demo.MEMO))
            self.assertEqual((call["gate"], call["policy"], call["issuers"], call["reports_account"]),
                             (demo.GATE, POLICY, None, None))
            self.assertEqual((call["rpc"], call["nearblocks"], call["injection_scanner"], call["archival"]),
                             self.clients)
        self.assertEqual(calls[0]["evidence_dir"], calls[1]["evidence_dir"])
        self.assertNotEqual(Path(calls[0]["evidence_dir"]).resolve(), EVIDENCE.resolve())

    def test_an_existing_fixture_is_overwritten_only_with_replace(self):
        self.out.write_bytes(b"[]\n")
        code, _, stderr, calls = self.record("kestrelridge-shop.testnet", TIER1_DOCUMENTS[0])
        self.assertEqual(code, 2)
        self.assertIn("pass --replace", stderr)
        self.assertEqual(calls, [])
        self.assertEqual(self.out.read_bytes(), b"[]\n")
        code, _, stderr, calls = self.record("kestrelridge-shop.testnet", TIER1_DOCUMENTS[0], replace=True)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(read(self.out)), 1)

    def test_a_failed_run_writes_nothing(self):
        refused = rpc.HttpError(429, rpc.NEARBLOCKS_TESTNET_URL + "/v1/account/examples.testnet/keys")
        code, stdout, stderr, _ = self.record("examples.testnet", TIER2_DOCUMENT, refused)
        self.assertEqual(code, 1)
        self.assertIn("run 2: HttpError: HTTP 429", stderr)
        self.assertIn("nothing written", stderr)
        self.assertFalse(self.out.exists())
        self.assertNotIn("Latency", stdout)
        unknown = read(TIER2_DOCUMENT)
        unknown["verdict"] = "UNKNOWN"
        unknown["checks"] = [dict(check, status="unknown", detail="could not read the account keys: HTTP 429")
                             if check["id"] == "account_age" else check for check in unknown["checks"]]
        unknown_path = evidence.write(unknown, self.tmp / "evidence")
        self.out.write_bytes(b"[]\n")
        code, _, stderr, _ = self.record("examples.testnet", TIER2_DOCUMENT, unknown_path, replace=True)
        self.assertEqual(code, 1)
        self.assertIn("run 2: UNKNOWN, a check could not run (account_age: could not read the account keys: HTTP 429)",
                      stderr)
        self.assertEqual(self.out.read_bytes(), b"[]\n")
        code, _, stderr, _ = self.record("lindenvale.testnet", SUPERSEDED_DOCUMENT, replace=True)
        self.assertEqual(code, 1)
        self.assertIn("run 1: account_age names no source that dated the account", stderr)
        self.assertEqual(self.out.read_bytes(), b"[]\n")

    def test_a_get_agent_read_that_fails_writes_nothing(self):
        with mock.patch.object(self.recorder, "live_setup", side_effect=rpc.HttpError(503, rpc.TESTNET_RPC_URL)):
            code, _, stderr, calls = self.record("examples.testnet", TIER2_DOCUMENT)
        self.assertEqual(code, 1)
        self.assertIn("nothing written", stderr)
        self.assertEqual(calls, [])
        self.assertFalse(self.out.exists())

    def test_live_setup_runs_only_in_mode_off_with_a_registered_agent_key(self):
        for mode in (None, "replay", "record"):
            environment = {} if mode is None else {"GATE_FIXTURES_MODE": mode}
            with self.subTest(mode=mode), mock.patch.dict(os.environ, environment):
                with self.assertRaisesRegex(self.recorder.Refused, "GATE_FIXTURES_MODE=off"):
                    self.live_setup()
        client = mock.Mock(name="rpc")
        clients = (client, mock.Mock(name="nearblocks"), None, mock.Mock(name="archival"))
        with mock.patch.dict(os.environ, {"GATE_FIXTURES_MODE": "off"}), \
                mock.patch.object(self.recorder, "_clients", return_value=clients) as build:
            client.call_function.return_value = POLICY
            self.assertEqual(self.live_setup(), (clients, POLICY))
            build.assert_called_once_with(demo.GATE)
            client.call_function.assert_called_once_with(demo.GATE, "get_agent", {"public_key": demo.AGENT_KEY})
            client.call_function.return_value = None
            with self.assertRaisesRegex(self.recorder.Refused, "is not registered on"):
                self.live_setup()

    def test_mode_replay_is_refused_before_any_run(self):
        with mock.patch.object(self.recorder, "live_setup", self.live_setup):
            code, _, stderr, calls = self.record("examples.testnet", TIER2_DOCUMENT)
        self.assertEqual(code, 2)
        self.assertIn("GATE_FIXTURES_MODE=off", stderr)
        self.assertEqual(calls, [])
        self.assertFalse(self.out.exists())


class PathTest(RulesCase):
    """path is read from the checks of the evidence: recorded documents, and judgements of the rules over replayed
    fixtures for the sources no recorded document shows."""

    def setUp(self):
        super().setUp()
        self.recorder = load("record_latency")

    def test_recorded_documents(self):
        self.assertEqual([self.recorder.path_of(read(path)["checks"]) for path in TIER1_DOCUMENTS], ["tier 1"] * 4)
        self.assertEqual(self.recorder.path_of(read(TIER2_DOCUMENT)["checks"]), "tier 1 and 2, account age from the NearBlocks key creation time")
        self.assertIsNone(self.recorder.path_of(read(LOOKALIKE_DOCUMENT)["checks"]))
        self.assertIsNone(self.recorder.path_of(read(SUPERSEDED_DOCUMENT)["checks"]))

    def test_a_young_account_dated_by_nearblocks_is_deny_with_the_nearblocks_path(self):
        self.stranger("young.testnet", age_ns=2 * DAY_NS)
        judgement = self.judge(request("young.testnet"))
        self.assertEqual(judgement.verdict, "DENY")
        self.assertEqual(self.recorder.path_of(judgement.checks), self.recorder.NEARBLOCKS_AGE)

    def test_an_account_dated_by_its_oldest_access_key_nonce(self):
        self.fx.account("stranger.testnet")
        self.token_ok("stranger.testnet")
        self.fx.keys("stranger.testnet")
        self.fx.access_keys("stranger.testnet", KEY_NONCE)
        self.fx.block_at(KEY_HEIGHT, OLD_NS)
        judgement = self.judge(request("stranger.testnet"), archival=self.archival)
        self.assertEqual(judgement.verdict, "ALLOW")
        self.assertEqual(self.recorder.path_of(judgement.checks),
                         "tier 1 and 2, account age from the access key nonce")

    def test_an_account_dated_by_its_archival_state(self):
        self.fx.account("pool.testnet", code_hash="SomeCodeHash", block_height=ACCOUNT_HEIGHT)
        self.token_ok("pool.testnet")
        self.fx.keys("pool.testnet")
        self.fx.method_missing("pool.testnet", "contract_source_metadata", {})
        self.fx.access_keys("pool.testnet")
        self.fx.block_at(STATE_HEIGHT, OLD_NS)
        self.fx.account_at("pool.testnet", STATE_HEIGHT, code_hash="SomeCodeHash")
        judgement = self.judge(request("pool.testnet"), archival=self.archival)
        self.assertEqual(judgement.verdict, "ALLOW")
        self.assertEqual(self.recorder.path_of(judgement.checks),
                         "tier 1 and 2, account age from the archival account state")


if __name__ == "__main__":
    unittest.main()
