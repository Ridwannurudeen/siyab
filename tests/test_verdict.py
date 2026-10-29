import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import base58, canon, ed25519, evidence, rpc, signer, verdict
from intel import labels

try:
    import nacl.signing
except ImportError:
    nacl = None

AGENT = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex()
ALLOW_POLICY = {"allowlist": ["intents.near"], "caps": [{"asset": {"Ft": "wrap.near"}}]}
VERDICT_KEYS = ["literal", "issued_ms", "expires_ms", "verdict_id", "evidence_hash", "key_id", "signature"]


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def write_key_file(path, seed, public=None):
    """A near-cli style key file for a seed; public overrides the public half."""
    public = public or ed25519.public_from_seed(seed)
    document = {"account_id": "test-verifier.testnet", "public_key": base58.encode_key(public)}
    document["secret_key"] = base58.encode_key(seed + ed25519.public_from_seed(seed))
    Path(path).write_text(json.dumps(document), encoding="utf-8")


class VerdictCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch("urllib.request.urlopen", side_effect=no_network)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = labels.load()

    def request(self, kind="ft", token="wrap.near", receiver="intents.near", amount="1000", memo=None,
                policy=ALLOW_POLICY, verdict_signer=None, ttl_ms=verdict.DEFAULT_TTL_MS):
        return verdict.request(kind, token, receiver, amount, memo, gate="gate.near", agent_pk_hex=AGENT,
                               policy=policy, store=self.store, rpc=rpc.Client(rpc.MAINNET_RPC_URL, "replay"),
                               nearblocks=rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http"),
                               signer=verdict_signer or signer.TestSigner(), ttl_ms=ttl_ms,
                               evidence_dir=self.dir)


class AllowTest(VerdictCase):
    def test_the_verdict_object_has_the_interface_shape(self):
        result = self.request()
        self.assertEqual(result["outcome"], "ALLOW")
        verdict_object = result["verdict"]
        self.assertEqual(list(verdict_object), VERDICT_KEYS)
        self.assertEqual((verdict_object["literal"], verdict_object["key_id"]), ("ALLOW", "cur"))
        for field in ("issued_ms", "expires_ms"):
            self.assertIs(type(verdict_object[field]), int)
        self.assertEqual(verdict_object["expires_ms"] - verdict_object["issued_ms"], 300000)
        self.assertRegex(verdict_object["verdict_id"], r"\A[0-9a-f]{32}\Z")
        self.assertRegex(verdict_object["evidence_hash"], r"\A[0-9a-f]{64}\Z")
        self.assertRegex(verdict_object["signature"], r"\A[0-9a-f]{128}\Z")
        self.assertEqual(result["request"], {"kind": "ft", "token_id": "wrap.near", "receiver_id": "intents.near",
                                             "amount": "1000", "memo": None})

    def test_the_signature_verifies_over_the_rebuilt_message(self):
        result = self.request(memo="invoice 7", amount=1000)
        transfer, verdict_object = result["request"], result["verdict"]
        message = canon.canonical_message(
            gate="gate.near", agent_pk_hex=AGENT, kind=transfer["kind"], token_id=transfer["token_id"],
            receiver_id=transfer["receiver_id"], amount=transfer["amount"], memo=transfer["memo"],
            issued_ms=verdict_object["issued_ms"], expires_ms=verdict_object["expires_ms"],
            verdict_id=verdict_object["verdict_id"], evidence_sha256=verdict_object["evidence_hash"],
            key_id=verdict_object["key_id"])
        self.assertEqual(message.decode(), result["message"])
        public = bytes.fromhex(signer.TestSigner().public_key_hex)
        self.assertTrue(ed25519.verify(public, canon.digest(message), bytes.fromhex(verdict_object["signature"])))

    def test_the_evidence_document_is_written_under_its_hash(self):
        result = self.request()
        path = Path(result["evidence_path"])
        self.assertEqual(path, self.dir / f"{result['evidence_hash']}.json")
        self.assertTrue(evidence.check_file(path))
        document = json.loads(path.read_bytes())
        self.assertEqual(document["verdict_id"], result["verdict"]["verdict_id"])
        self.assertEqual(document["verdict"], "ALLOW")
        self.assertEqual(document["subject"]["amount"], "1000")
        self.assertIs(type(document["latency_ms"]), int)

    def test_previous_key_and_ttl(self):
        result = self.request(verdict_signer=signer.TestSigner("prev"), ttl_ms=60000)
        self.assertTrue(result["message"].endswith("|prev|ALLOW"))
        self.assertEqual(result["verdict"]["expires_ms"] - result["verdict"]["issued_ms"], 60000)

    def test_each_allow_draws_a_new_verdict_id(self):
        self.assertNotEqual(self.request()["verdict"]["verdict_id"], self.request()["verdict"]["verdict_id"])


class NoSignatureTest(VerdictCase):
    def assert_unsigned(self, result, outcome):
        self.assertEqual(result["outcome"], outcome)
        self.assertIsNone(result["verdict"])
        self.assertIsNone(result["message"])
        document = json.loads(Path(result["evidence_path"]).read_bytes())
        self.assertEqual(document["verdict"], outcome)
        self.assertEqual([document[k] for k in ("issued_ms", "expires_ms", "verdict_id")], [None, None, None])

    def test_deny_is_never_signed(self):
        spy = mock.MagicMock(key_id="cur")
        result = self.request(receiver="no-such-account-example.near",
                              policy={"allowlist": ["no-such-account-example.near"], "caps": []},
                              verdict_signer=spy)
        self.assert_unsigned(result, "DENY")
        spy.sign.assert_not_called()

    def test_unknown_is_never_signed(self):
        spy = mock.MagicMock(key_id="cur")
        result = self.request(kind="near", token=None, receiver="relay.aurora", amount="5",
                              policy={"allowlist": [], "caps": []}, verdict_signer=spy)
        self.assert_unsigned(result, "UNKNOWN")
        spy.sign.assert_not_called()

    def test_an_invalid_request_fails_before_judging_or_writing(self):
        with mock.patch("gate.rules.judge", side_effect=AssertionError("judged")):
            for changes in ({"amount": "0100"}, {"memo": "a" + chr(0xD800)}, {"receiver": "Bad"},
                            {"kind": "near"}, {"token": None}):
                with self.subTest(changes=changes), self.assertRaises(canon.CanonError):
                    self.request(**changes)
        self.assertEqual(list(self.dir.iterdir()), [])


class SignerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.key_file = Path(tmp.name) / "test-verifier.json"
        write_key_file(self.key_file, ed25519.TEST_VERIFIER_SEED)
        self.digests = [hashlib.sha256(text).digest() for text in (b"", b"GATEv1|a", b"x" * 300)]

    @unittest.skipIf(nacl is None, "PyNaCl is not installed; run under ~/.venvs/near/bin/python")
    def test_pynacl_and_pure_python_sign_identically_on_the_test_seed(self):
        live, test = signer.NaClSigner(self.key_file), signer.TestSigner()
        self.assertEqual(live.public_key_hex, test.public_key_hex)
        for digest in self.digests:
            self.assertEqual(live.sign(digest), test.sign(digest))

    def test_pure_python_live_signer_refuses_without_the_variable(self):
        for value in (None, "0", "true", "yes"):
            environment = {} if value is None else {"GATE_ALLOW_PURE_PYTHON_SIGNER": value}
            with self.subTest(value=value), mock.patch.dict(os.environ, environment, clear=True), \
                    self.assertRaises(signer.SignerError):
                signer.PurePythonLiveSigner(self.key_file)
        with mock.patch.dict(os.environ, {"GATE_ALLOW_PURE_PYTHON_SIGNER": "1"}):
            live = signer.PurePythonLiveSigner(self.key_file, key_id="prev")
        self.assertEqual((live.public_key_hex, live.key_id), (signer.TestSigner().public_key_hex, "prev"))
        self.assertEqual(live.sign(self.digests[1]), signer.TestSigner().sign(self.digests[1]))

    def test_a_key_file_whose_halves_disagree_is_refused(self):
        write_key_file(self.key_file, ed25519.TEST_VERIFIER_SEED, public=ed25519.public_from_seed(ed25519.TEST_AGENT_SEED))
        with mock.patch.dict(os.environ, {"GATE_ALLOW_PURE_PYTHON_SIGNER": "1"}), \
                self.assertRaises(signer.SignerError):
            signer.PurePythonLiveSigner(self.key_file)
        self.key_file.write_text("{}", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GATE_ALLOW_PURE_PYTHON_SIGNER": "1"}), \
                self.assertRaises(signer.SignerError):
            signer.PurePythonLiveSigner(self.key_file)

    def test_a_missing_or_unreadable_key_file_raises_signer_error(self):
        self.key_file.write_text("not json", encoding="utf-8")
        for path in (self.key_file.with_name("missing.json"), self.key_file):
            with (self.subTest(path=path.name),
                  mock.patch.dict(os.environ, {"GATE_ALLOW_PURE_PYTHON_SIGNER": "1"}),
                  self.assertRaises(signer.SignerError)):
                signer.PurePythonLiveSigner(path)

    def test_key_id_is_cur_or_prev(self):
        with self.assertRaises(signer.SignerError):
            signer.TestSigner("next")


class CommandTest(VerdictCase):
    def run_command(self, *extra, environment=None):
        output, errors = io.StringIO(), io.StringIO()
        argv = ["verdict", "--kind", "ft", "--token", "wrap.near", "--receiver", "intents.near", "--amount",
                "1000", "--gate", "gate.near", "--evidence-dir", str(self.dir), "--out", str(self.dir), *extra]
        with mock.patch.dict(os.environ, environment or {}, clear=True), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = gate_main.main(argv)
        return code, output.getvalue(), errors.getvalue()

    def test_allow_prints_the_checks_and_writes_the_verdict_object(self):
        code, output, _ = self.run_command("--policy", json.dumps(ALLOW_POLICY), "--memo", "invoice 7")
        self.assertEqual(code, 0)
        lines = output.splitlines()
        self.assertEqual(lines[0], "pass    receiver_exists: intents.near exists")
        self.assertIn("verdict: ALLOW", lines)
        self.assertTrue(any(re.fullmatch(r"latency: \d+ ms", line) for line in lines))
        verdict_object = json.loads(lines[-2])
        self.assertEqual(list(verdict_object), VERDICT_KEYS)
        written = self.dir / f"{verdict_object['verdict_id']}.verdict.json"
        self.assertEqual(json.loads(written.read_text()), verdict_object)
        self.assertIn(f"evidence: {verdict_object['evidence_hash']}", lines)

    def test_unknown_writes_an_unsigned_record(self):
        code, output, _ = self.run_command("--policy", '{"allowlist": [], "caps": []}')
        self.assertEqual(code, 0)
        record = json.loads(output.splitlines()[-2])
        self.assertEqual(record["literal"], "UNKNOWN")
        self.assertNotIn("signature", record)

    def test_bad_input_and_a_missing_live_key_exit_2(self):
        self.assertEqual(self.run_command("--amount", "0100")[0], 2)
        self.assertEqual(self.run_command("--policy", "not json")[0], 2)
        code, _, errors = self.run_command("--live")
        self.assertEqual(code, 2)
        self.assertIn("VERIFIER_KEY_FILE", errors)
        missing = str(self.dir / "missing-key.json")
        code, _, errors = self.run_command("--live", environment={"VERIFIER_KEY_FILE": missing})
        self.assertEqual(code, 2)
        self.assertIn("no signer:", errors)

    def test_help_says_where_the_files_land(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stop:
            gate_main.main(["verdict", "--help"])
        self.assertEqual(stop.exception.code, 0)
        text = " ".join(output.getvalue().split())
        self.assertIn("where the evidence document <hash>.json goes (default fixtures/evidence)", text)
        self.assertIn("where the verdict file goes: <verdict_id>.verdict.json on ALLOW, <evidence hash>.verdict.json "
                      "otherwise (default the current directory)", text)

    def test_without_out_the_verdict_file_lands_in_the_current_directory(self):
        here = self.dir / "here"
        here.mkdir()
        argv = ["verdict", "--kind", "ft", "--token", "wrap.near", "--receiver", "intents.near", "--amount", "1000",
                "--gate", "gate.near", "--evidence-dir", str(self.dir), "--policy", json.dumps(ALLOW_POLICY)]
        previous = os.getcwd()
        os.chdir(here)
        self.addCleanup(os.chdir, previous)
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate_main.main(argv), 0)
        written = sorted(path.name for path in here.iterdir())
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].endswith(".verdict.json"))


if __name__ == "__main__":
    unittest.main()
