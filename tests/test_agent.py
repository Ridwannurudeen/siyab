import ast
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import agent, base58, canon, ed25519, rpc, signer

ROOT = Path(__file__).resolve().parent.parent
AGENT_SOURCE = ROOT / "gate" / "agent.py"
GATE = "gate-agent.testnet"
TOKEN = "token.gate-agent.testnet"
AGENT_PK = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED)
NOW_S = 1793534400
POLICY = {"label": "agent 1", "enabled": True, "allowlist": ["vendor-shop.testnet"],
          "caps": [{"asset": {"Ft": TOKEN}, "per_call": "5000000", "window_cap": "20000000", "window_ms": 86400000,
                    "window_start_ms": 0, "spent": "0"}], "unknown_caps": None}
SUCCESS = {"transaction_hash": "9ZtqWvdcGfCVt8cDcWbMEhX1RZGqVfpY3iKEY4mZnKqR", "status": {"SuccessValue": ""},
           "logs": ['EVENT_JSON:{"standard":"shieldbot-near","event":"transfer_scheduled"}'], "rpc_error": None}
PANIC = {"transaction_hash": "4h3Qm7Lw9Xp1KcV8sNzR2tYbE6uJfA5dGiHoP3WqTnMk",
         "status": {"Failure": {"ActionError": {"index": 0, "kind": {"FunctionCallError": {
             "ExecutionError": "Smart contract panicked: REFUSED:no_verdict"}}}}}, "logs": [], "rpc_error": None}
REJECTED = {"transaction_hash": "7pQwErTyUiOpAsDfGhJkLzXcVbNm12345678abcdEFGH", "status": None, "logs": [],
            "rpc_error": {"name": "HANDLER_ERROR", "cause": {"name": "INVALID_TRANSACTION"},
                          "data": {"TxExecutionError": {"InvalidTxError": {"InvalidAccessKeyError": {
                              "MethodNameMismatch": {"method_name": "on_transfer"}}}}}}}


class Transport:
    """Records each action and answers with the next outcome."""

    def __init__(self, *outcomes):
        self.sent, self.outcomes = [], list(outcomes)

    def __call__(self, sent):
        self.sent.append(json.loads(json.dumps(sent)))
        return self.outcomes.pop(0) if self.outcomes else PANIC


class Client:
    def __init__(self, policy=POLICY):
        self.policy, self.calls = policy, []

    def call_function(self, contract, method, args):
        self.calls.append((contract, method, args))
        return self.policy


def judged(outcome="ALLOW", verdict_signer=None):
    """A gate.verdict.request result for the vendor transfer."""
    verdict_object = None
    if outcome == "ALLOW":
        verdict_object = {"literal": "ALLOW", "issued_ms": NOW_S * 1000, "expires_ms": NOW_S * 1000 + 300000,
                          "verdict_id": "ab" * 16, "evidence_hash": "cd" * 32, "key_id": "cur", "signature": "ef" * 64}
    return {"outcome": outcome, "verdict": verdict_object, "checks": [
        {"id": "receiver_exists", "status": "pass", "detail": "vendor-shop.testnet exists"}],
        "reasons": [], "evidence_hash": "cd" * 32, "latency_ms": 12}


class AgentCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch("urllib.request.urlopen", side_effect=AssertionError("the test opened the network"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lines = []

    def make(self, *outcomes, policy=POLICY, verifier=signer.TestSigner):
        self.transport, self.client = Transport(*outcomes), Client(policy)
        return agent.Agent(gate=GATE, public_hex=AGENT_PK.hex(), public_key=base58.encode_key(AGENT_PK),
                           transport=self.transport, clients=(self.client, None, None), verifier=verifier,
                           out_dir=self.dir / "out", clock=lambda: NOW_S)

    def request(self, receiver="vendor-shop.testnet", amount="1000000"):
        return agent.transfer_request("ft", TOKEN, receiver, amount, "invoice 1", gate=GATE,
                                      agent_pk_hex=AGENT_PK.hex())

    def written(self, name):
        return json.loads((self.dir / "out" / f"{name}.json").read_text(encoding="utf-8"))


class PayTest(AgentCase):
    def test_pay_sends_the_judged_verdict_untouched(self):
        context = self.make(SUCCESS)
        result = judged()
        with mock.patch("gate.verdict.request", return_value=result) as service:
            self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 0)
        kwargs = service.call_args.kwargs
        self.assertEqual((kwargs["gate"], kwargs["agent_pk_hex"], kwargs["policy"]), (GATE, AGENT_PK.hex(), POLICY))
        self.assertIsInstance(kwargs["signer"], signer.TestSigner)
        self.assertEqual(self.client.calls, [(GATE, "get_agent", {"public_key": base58.encode_key(AGENT_PK)})])
        self.assertEqual(self.transport.sent, [{
            "receiver_id": GATE, "method_name": "request_transfer", "gas": 30 * 10**12, "deposit": 0,
            "args": {"request": {"kind": "ft", "token_id": TOKEN, "receiver_id": "vendor-shop.testnet",
                                 "amount": "1000000", "memo": "invoice 1"}, "verdict": result["verdict"]}}])
        self.assertIn(f"transaction: {SUCCESS['transaction_hash']}", self.lines)
        self.assertIn(f"explorer: https://testnet.nearblocks.io/txns/{SUCCESS['transaction_hash']}", self.lines)
        self.assertIn(f"log: {SUCCESS['logs'][0]}", self.lines)
        self.assertIn('status: {"SuccessValue": ""}', self.lines)
        record = self.written(f"pay-{'ab' * 16}")
        self.assertTrue(record["submitted"])
        self.assertEqual(record["sent"]["args"]["verdict"], result["verdict"])
        self.assertEqual(record["outcome"], SUCCESS)

    def test_pay_sends_a_verdict_file_untouched(self):
        context = self.make(SUCCESS)
        verdict_object = judged()["verdict"]
        path = self.dir / "allow.verdict.json"
        path.write_text(json.dumps(verdict_object, indent=2), encoding="utf-8")
        with mock.patch("gate.verdict.request", side_effect=AssertionError("judged again")):
            self.assertEqual(agent.pay(context, self.request(), str(path), out=self.lines.append), 0)
        self.assertEqual(self.transport.sent[0]["args"]["verdict"], verdict_object)

    def test_pay_sends_nothing_on_deny_or_unknown(self):
        for outcome in ("DENY", "UNKNOWN"):
            with self.subTest(outcome=outcome), mock.patch("gate.verdict.request", return_value=judged(outcome)):
                context = self.make()
                self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 1)
                self.assertEqual(self.transport.sent, [])
                self.assertIn(f"not sent: the verdict is {outcome}, and the Gate pays only on a signed ALLOW",
                              self.lines)
                self.assertFalse(self.written(f"pay-{'cd' * 32}")["submitted"])
        unsigned = self.dir / "unknown.verdict.json"
        unsigned.write_text(json.dumps({"literal": "UNKNOWN", "evidence_hash": "cd" * 32}), encoding="utf-8")
        context = self.make()
        self.assertEqual(agent.pay(context, self.request(), str(unsigned), out=self.lines.append), 1)
        self.assertEqual(self.transport.sent, [])
        allow = judged()["verdict"]
        broken = {
            "an ALLOW without a signature": {key: value for key, value in allow.items() if key != "signature"},
            "a short signature": {**allow, "signature": "ef" * 32},
            "an upper case signature": {**allow, "signature": "EF" * 64},
            "an extra key": {**allow, "note": "x"},
            "a verdict id that is a path": {**allow, "verdict_id": "../../elsewhere"},
            "not an object": ["ALLOW"],
        }
        for name, body in broken.items():
            with self.subTest(case=name):
                self.lines = []
                path = self.dir / "broken.verdict.json"
                path.write_text(json.dumps(body), encoding="utf-8")
                context = self.make()
                self.assertEqual(agent.pay(context, self.request(), str(path), out=self.lines.append), 1)
                self.assertEqual(self.transport.sent, [])
                self.assertTrue(any(line.startswith("not sent: ") for line in self.lines))
        self.assertFalse(self.written("pay-not-sent")["submitted"])
        unsigned.write_text(json.dumps({"literal": "DENY"}), encoding="utf-8")
        (self.dir / "out" / "pay-not-sent.json").unlink()
        self.assertEqual(agent.pay(self.make(), self.request(), str(unsigned), out=self.lines.append), 1)
        self.assertFalse(self.written("pay-not-sent")["submitted"])
        self.assertFalse((self.dir / "out" / "pay-None.json").exists())

    def failed_after_scheduling(self, restored):
        event = json.dumps({"standard": "shieldbot-near", "version": "1.0.0", "event": "transfer_failed",
                            "data": [{"request_id": 0, "restored": restored}]}, separators=(",", ":"))
        outcome = {**SUCCESS, "logs": SUCCESS["logs"] + [f"EVENT_JSON:{event}"]}
        self.lines = []
        with mock.patch("gate.verdict.request", return_value=judged()):
            self.assertEqual(agent.pay(self.make(outcome), self.request(), out=self.lines.append), 1)
        return self.lines

    def test_a_transfer_that_failed_after_scheduling_exits_1(self):
        lines = self.failed_after_scheduling(True)
        self.assertIn("the transfer failed after it was scheduled: the Gate restored the budget, the verdict stays "
                      "used, and the payment needs a fresh ALLOW", lines)
        self.assertFalse(any("nothing was restored" in line for line in lines))

    def test_a_failed_transfer_that_was_not_restored_says_so(self):
        lines = self.failed_after_scheduling(False)
        self.assertIn("the transfer failed after it was scheduled: the agent or its cap changed before the callback, "
                      "so nothing was restored, the verdict stays used, and the payment needs a fresh ALLOW", lines)
        self.assertFalse(any("restored the budget" in line for line in lines))

    def test_a_failed_receipt_exits_1(self):
        with mock.patch("gate.verdict.request", return_value=judged()):
            self.assertEqual(agent.pay(self.make(PANIC), self.request(), out=self.lines.append), 1)

    def test_an_unregistered_agent_key_is_refused_before_judging(self):
        with mock.patch("gate.verdict.request", side_effect=AssertionError("judged")), \
                self.assertRaises(agent.AgentError):
            agent.pay(self.make(policy=None), self.request(), out=self.lines.append)


class NoBypassTest(unittest.TestCase):
    def functions(self):
        tree = ast.parse(AGENT_SOURCE.read_text(encoding="utf-8"))
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[node.name] = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        return found

    def reachable(self, start, functions):
        seen, todo = set(), [start]
        while todo:
            name = todo.pop()
            if name in seen or name not in functions:
                continue
            seen.add(name)
            todo.extend(functions[name])
        return seen

    def test_pay_cannot_reach_the_attack_signer_or_an_attack(self):
        functions = self.functions()
        from_pay = self.reachable("pay", functions)
        self.assertIn("_judge", from_pay)
        self.assertIn("_submit", from_pay)
        for name in ("_attack_signer", "_attack_actions", "_attack_transfer", "_attack_evidence", "attack"):
            self.assertNotIn(name, from_pay)
        self.assertNotIn("os", set().union(*(functions[name] for name in from_pay)))
        self.assertIn("os", functions["live_signer"])
        users = {name for name, names in functions.items() if "_attack_signer" in names}
        self.assertEqual(users, {"_attack_actions"})
        self.assertEqual({name for name in functions if self.reachable(name, functions) & {"_attack_actions"}},
                         {"_attack_actions", "attack"})

    def test_the_source_has_no_bypass_word_and_pay_has_no_bypass_option(self):
        self.assertNotIn("ignore", AGENT_SOURCE.read_text(encoding="utf-8").lower())
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            gate_main.main(["pay", "--help"])
        options = sorted({word.strip("[],") for word in output.getvalue().split() if word.startswith(("--", "[--"))})
        self.assertEqual(options, ["--amount", "--help", "--kind", "--memo", "--out", "--receiver", "--token",
                                   "--verdict-file", "--via"])


class AttackTest(AgentCase):
    def run_attack(self, name, *outcomes, receiver="vendor-shop.testnet", amount="1000000"):
        context = self.make(*outcomes)
        with mock.patch("gate.verdict.request", return_value=judged()):
            code = agent.attack(name, context, self.request(receiver, amount), out=self.lines.append)
        return code, self.transport.sent

    def shape(self, sent):
        return sent["receiver_id"], sent["method_name"], sent["deposit"], sent["gas"]

    def test_each_attack_has_its_transaction_shape(self):
        request_transfer = (GATE, "request_transfer", 0, 30 * 10**12)
        rows = {
            "no-verdict": [request_transfer],
            "forged-signature": [request_transfer],
            "replay": [request_transfer, request_transfer],
            "expired": [request_transfer],
            "over-cap": [request_transfer],
            "callback": [(GATE, "on_transfer", 0, 30 * 10**12)],
            "token-direct": [(TOKEN, "ft_transfer", 0, 30 * 10**12)],
            "service-down": [request_transfer],
            "one-yocto": [(GATE, "request_transfer", 1, 30 * 10**12)],
            "unknown-payee": [request_transfer],
        }
        self.assertEqual(sorted(rows), sorted(agent.ATTACKS))
        for name, expected in rows.items():
            with self.subTest(attack=name):
                self.lines = []
                code, sent = self.run_attack(name, SUCCESS if name == "replay" else PANIC, PANIC)
                self.assertEqual([self.shape(s) for s in sent], expected)
                self.assertEqual(code, 0)
                record = self.written(name)
                self.assertTrue(record["refused"])
                self.assertEqual([s["sent"] for s in record["submissions"]], sent)
                self.assertIn(f"{name}: refused", self.lines)

    def test_the_verdicts_each_attack_carries(self):
        request = self.request()
        _, sent = self.run_attack("no-verdict")
        self.assertEqual(sent[0]["args"], {"request": request, "verdict": None})
        _, sent = self.run_attack("service-down")
        self.assertIsNone(sent[0]["args"]["verdict"])
        for name in ("over-cap", "unknown-payee", "one-yocto"):
            _, sent = self.run_attack(name)
            self.assertEqual(sent[0]["args"], {"request": request, "verdict": judged()["verdict"]})
        _, sent = self.run_attack("replay", SUCCESS, PANIC)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(sent[0]["args"]["verdict"], judged()["verdict"])
        _, sent = self.run_attack("callback", REJECTED)
        self.assertEqual(sent[0]["args"], {"request_id": 0})
        _, sent = self.run_attack("token-direct", REJECTED)
        self.assertEqual(sent[0]["args"], {"receiver_id": "vendor-shop.testnet", "amount": "1000000",
                                           "memo": "invoice 1"})

    def verify(self, verdict_object, request, public):
        message = canon.canonical_message(
            gate=GATE, agent_pk_hex=AGENT_PK.hex(), kind=request["kind"], token_id=request["token_id"],
            receiver_id=request["receiver_id"], amount=request["amount"], memo=request["memo"],
            issued_ms=verdict_object["issued_ms"], expires_ms=verdict_object["expires_ms"],
            verdict_id=verdict_object["verdict_id"], evidence_sha256=verdict_object["evidence_hash"],
            key_id=verdict_object["key_id"])
        return ed25519.verify(public, canon.digest(message), bytes.fromhex(verdict_object["signature"]))

    def test_the_forged_signature_is_the_test_key_and_well_shaped(self):
        live = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED)
        context = self.make(PANIC, verifier=lambda: self.fail("the live key signed a forged verdict"))
        agent.attack("forged-signature", context, self.request(), out=self.lines.append)
        forged = self.transport.sent[0]["args"]["verdict"]
        self.assertEqual(sorted(forged), sorted(judged()["verdict"]))
        self.assertEqual(forged["expires_ms"] - forged["issued_ms"], 300000)
        self.assertGreater(forged["expires_ms"], NOW_S * 1000)
        test_key = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED)
        self.assertTrue(self.verify(forged, self.request(), test_key))
        self.assertFalse(self.verify(forged, self.request(), live))

    def test_the_expired_verdict_is_a_day_old_and_signed_by_the_live_key(self):
        _, sent = self.run_attack("expired")
        old = sent[0]["args"]["verdict"]
        self.assertEqual(old["expires_ms"], NOW_S * 1000 - 24 * 60 * 60 * 1000)
        self.assertEqual(old["expires_ms"] - old["issued_ms"], 300000)
        self.assertTrue(self.verify(old, self.request(), ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED)))

    def test_a_protocol_rejection_is_printed_and_kept(self):
        code, _ = self.run_attack("callback", REJECTED)
        self.assertEqual(code, 0)
        self.assertFalse(any(line.startswith(("explorer:", "transaction:")) for line in self.lines))
        self.assertIn(f"rejected, no receipt: {json.dumps(REJECTED['rpc_error'])}", self.lines)
        self.assertEqual(self.written("callback")["submissions"][0]["outcome"]["rpc_error"], REJECTED["rpc_error"])

    def test_an_attack_that_goes_through_is_reported_and_exits_1(self):
        code, _ = self.run_attack("over-cap", SUCCESS)
        self.assertEqual(code, 1)
        self.assertIn("over-cap: NOT REFUSED", self.lines)
        self.assertFalse(self.written("over-cap")["refused"])
        code, _ = self.run_attack("replay", SUCCESS, SUCCESS)
        self.assertEqual(code, 1)

    def test_an_attack_needing_an_allow_stops_on_deny(self):
        context = self.make()
        with mock.patch("gate.verdict.request", return_value=judged("DENY")), self.assertRaises(agent.AgentError):
            agent.attack("over-cap", context, self.request(), out=self.lines.append)
        self.assertEqual(self.transport.sent, [])


class NearCliTest(unittest.TestCase):
    def test_the_command_line_follows_the_recorded_form(self):
        calls = []

        def run(argv, **options):
            calls.append((argv, options))
            return mock.Mock(returncode=0, stdout="Transaction ID: 9ZtqWvdcGfCVt8cDcWbMEhX1RZGqVfpY3iKEY4mZnKqR\n"
                                                  'Logs:\n  EVENT_JSON:{"event":"transfer_scheduled"}\n', stderr="")

        transport = agent.NearCliTransport(GATE, "/keys/agent.json", run=run)
        outcome = transport(agent.action(GATE, "request_transfer", {"request": {"kind": "near"}, "verdict": None}, 1))
        argv, options = calls[0]
        self.assertEqual(argv, ["near", "contract", "call-function", "as-transaction", GATE, "request_transfer",
                                "json-args", '{"request":{"kind":"near"},"verdict":null}', "prepaid-gas", "30 Tgas",
                                "attached-deposit", "1 yoctoNEAR", "sign-as", GATE, "network-config", "testnet",
                                "sign-with-access-key-file", "/keys/agent.json", "send"])
        self.assertNotIn("shell", options)
        self.assertEqual(outcome["transaction_hash"], "9ZtqWvdcGfCVt8cDcWbMEhX1RZGqVfpY3iKEY4mZnKqR")
        self.assertEqual(outcome["logs"], ['EVENT_JSON:{"event":"transfer_scheduled"}'])
        self.assertEqual(transport.argv(agent.action(GATE, "on_transfer", {}))[11], "0 NEAR")

    def test_a_missing_near_cli_is_an_agent_error(self):
        transport = agent.NearCliTransport(GATE, "/keys/agent.json", run=mock.Mock(side_effect=FileNotFoundError))
        with self.assertRaises(agent.AgentError):
            transport(agent.action(GATE, "on_transfer", {}))


class PyNearTest(unittest.TestCase):
    def setUp(self):
        try:
            import py_near.exceptions.provider
        except ImportError:
            reason = "py-near is not installed; it runs from the venv ~/.venvs/near"
            print(f"skipped: {reason}")
            self.skipTest(reason)
        self.rejected = py_near.exceptions.provider.InvalidAccessKeyError
        seed = ed25519.TEST_AGENT_SEED
        self.transport = agent.PyNearTransport(GATE, base58.encode_key(seed + AGENT_PK), "http://127.0.0.1:9")

    def test_py_near_reads_the_secret_as_the_agent_key(self):
        self.assertEqual(self.transport._account._provider.timeout, rpc.TIMEOUT_S)
        self.assertEqual(list(self.transport._account._signer_by_pk), [base58.encode(AGENT_PK)])

    def test_the_outcome_of_a_sent_and_a_rejected_transaction(self):
        sent = agent.action(GATE, "request_transfer", {"request": {}, "verdict": None})
        result = mock.Mock(status={"SuccessValue": ""}, logs=["EVENT_JSON:{}"])
        result.transaction.hash = SUCCESS["transaction_hash"]
        account = self.transport._account
        with mock.patch.object(account, "startup", mock.AsyncMock()), \
                mock.patch.object(account, "function_call", mock.AsyncMock(return_value=result)) as call:
            outcome = self.transport(sent)
        call.assert_awaited_once_with(GATE, "request_transfer", {"request": {}, "verdict": None}, gas=30 * 10**12,
                                      amount=0)
        self.assertEqual(outcome, {"transaction_hash": SUCCESS["transaction_hash"], "status": {"SuccessValue": ""},
                                   "logs": ["EVENT_JSON:{}"], "rpc_error": None})
        error = self.rejected({}, error_json=REJECTED["rpc_error"])
        error.trx_hash = REJECTED["transaction_hash"]
        with mock.patch.object(account, "startup", mock.AsyncMock()), \
                mock.patch.object(account, "function_call", mock.AsyncMock(side_effect=error)):
            outcome = self.transport(sent)
        self.assertEqual(outcome, {"transaction_hash": REJECTED["transaction_hash"], "status": None, "logs": [],
                                   "rpc_error": REJECTED["rpc_error"]})


class CommandTest(unittest.TestCase):
    def run_command(self, argv, environment):
        errors = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True), contextlib.redirect_stderr(errors), \
                contextlib.redirect_stdout(io.StringIO()):
            code = gate_main.main(argv)
        return code, errors.getvalue()

    def test_the_command_needs_a_testnet_gate_and_an_agent_key(self):
        argv = ["pay", "--kind", "near", "--receiver", "vendor-shop.testnet", "--amount", "1", "--out", "unused"]
        self.assertEqual(self.run_command(argv, {}), (2, "not sent: set GATE_ACCOUNT_ID and AGENT_KEY_FILE\n"))
        code, errors = self.run_command(argv, {"GATE_ACCOUNT_ID": "gate.near", "AGENT_KEY_FILE": "missing.json"})
        self.assertEqual(code, 2)
        self.assertEqual(errors, "not sent: the agent sends on testnet only, and gate.near is not a .testnet account\n")
        code, errors = self.run_command(["attack", "callback", *argv[1:]],
                                        {"GATE_ACCOUNT_ID": GATE, "AGENT_KEY_FILE": "missing.json"})
        self.assertEqual(code, 2)
        self.assertIn("cannot read a key file", errors)

    def test_pay_in_replay_mode_says_it_reads_the_gate_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "agent.json"
            seed = ed25519.TEST_AGENT_SEED
            document = {"account_id": GATE, "public_key": base58.encode_key(AGENT_PK)}
            document["secret_key"] = base58.encode_key(seed + AGENT_PK)
            key_file.write_text(json.dumps(document), encoding="utf-8")
            argv = ["pay", "--kind", "near", "--receiver", "vendor-shop.testnet", "--amount", "1", "--via", "near-cli",
                    "--out", str(Path(tmp) / "out")]
            with mock.patch("gate.agent.live_signer", return_value=signer.TestSigner()):
                code, errors = self.run_command(argv, {"GATE_ACCOUNT_ID": GATE, "AGENT_KEY_FILE": str(key_file)})
        self.assertEqual(code, 2)
        self.assertEqual(errors, "not sent: pay reads the Gate live: set GATE_FIXTURES_MODE=record or off\n")


if __name__ == "__main__":
    unittest.main()
