import ast
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import agent, base58, canon, demo, ed25519, rpc, signer

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
    def __init__(self, policy=POLICY, gate_policy=None):
        self.policy, self.gate_policy, self.calls = policy, gate_policy or {"paused": False, "denylist": []}, []

    def call_function(self, contract, method, args):
        self.calls.append((contract, method, args))
        return self.gate_policy if method == "get_policy" else self.policy


def judged(outcome="ALLOW", verdict_signer=None):
    """A gate.verdict.request result for the vendor transfer."""
    verdict_object = None
    if outcome == "ALLOW":
        verdict_object = {"literal": "ALLOW", "issued_ms": NOW_S * 1000, "expires_ms": NOW_S * 1000 + 300000,
                          "verdict_id": "ab" * 16, "evidence_hash": "cd" * 32, "key_id": "cur", "signature": "ef" * 64}
    action = {"ALLOW": "sign", "DENY": "refuse", "UNKNOWN": "ask"}[outcome]
    check_status = "unknown" if outcome == "UNKNOWN" else "pass"
    check_detail = "no recorded response" if outcome == "UNKNOWN" else "vendor-shop.testnet exists"
    checks = [{"id": "receiver_exists", "status": check_status, "detail": check_detail}]
    ask = [f"{check['id']} could not run: {check['detail']}" for check in checks if check["status"] == "unknown"]
    return {"outcome": outcome, "action": action, "ask": ask, "verdict": verdict_object, "checks": checks,
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

    def make(self, *outcomes, policy=POLICY, gate_policy=None, verifier=signer.TestSigner):
        self.transport, self.client = Transport(*outcomes), Client(policy, gate_policy)
        return agent.Agent(gate=GATE, public_hex=AGENT_PK.hex(), public_key=base58.encode_key(AGENT_PK),
                           transport=self.transport, clients=(self.client, None, None, None), verifier=verifier,
                           out_dir=self.dir / "out", clock=lambda: NOW_S)

    def request(self, receiver="vendor-shop.testnet", amount="1000000"):
        return agent.transfer_request("ft", TOKEN, receiver, amount, "invoice 1", gate=GATE,
                                      agent_pk_hex=AGENT_PK.hex())

    def written(self, name):
        return json.loads((self.dir / "out" / f"{name}.json").read_text(encoding="utf-8"))


class ReadPolicyTest(AgentCase):
    def test_read_policy_combines_the_agent_and_gate_views(self):
        context = self.make(gate_policy={"paused": True, "denylist": ["blocked.testnet"]})
        policy = agent.read_policy(context)
        self.assertEqual(policy["gate_paused"], True)
        self.assertEqual(policy["gate_denylist"], ["blocked.testnet"])
        self.assertEqual(policy["enabled"], True)
        self.assertEqual(context.clients[0].calls, [
            (GATE, "get_agent", {"public_key": context.public_key}),
            (GATE, "get_policy", {}),
        ])


class JudgeIssuerTest(AgentCase):
    def run_judge(self, gate, receiver):
        context = self.make()
        context.gate = gate
        client = mock.Mock()
        client.last_source = {"provider": "rpc.example", "request_sha256": None, "fetched_at": None}
        client.view_account.return_value = {"block_height": 100, "code_hash": rpc.EMPTY_CODE_HASH}
        client.call_function.return_value = False
        context.clients = (client, None, None, None)
        transfer = agent.transfer_request("near", None, receiver, "1", None, gate=gate,
                                          agent_pk_hex=AGENT_PK.hex())
        store = mock.Mock(sha256="test-label-store", assertions=[], by_subject=mock.Mock(return_value=[]))
        original_write = agent.verdict.evidence.write

        def write_to_temp(document, directory):
            self.assertEqual(Path(directory), agent.verdict.EVIDENCE_DIR)
            return original_write(document, self.dir / "evidence")

        with (
            mock.patch("gate.agent.labels.load", return_value=store),
            mock.patch("gate.verdict.evidence.write", side_effect=write_to_temp),
        ):
            result = agent._judge(context, transfer, context.verifier(), self.lines.append,
                                  policy={"allowlist": [receiver], "caps": []})
        return result

    def test_mainnet_gate_runs_issuer_blacklist_in_the_judgement(self):
        issuer_table = [{"token": "issuer.near", "method": "is_blacklisted", "args_key": "account_id",
                         "banned": True}]
        with mock.patch("gate.agent.rules.issuers_for", return_value=issuer_table) as issuer_source:
            result = self.run_judge("gate.near", "vendor.near")
        issuer_source.assert_called_once_with("mainnet")
        check = next(check for check in result["checks"] if check["id"] == "issuer_blacklist")
        self.assertEqual(check["status"], "pass")

    def test_testnet_gate_omits_issuer_blacklist_from_the_judgement(self):
        with mock.patch("gate.agent.rules.issuers_for") as issuer_source:
            result = self.run_judge(GATE, "vendor-shop.testnet")
        issuer_source.assert_not_called()
        self.assertNotIn("issuer_blacklist", {check["id"] for check in result["checks"]})


class PayTest(AgentCase):
    def test_pay_sends_the_judged_verdict_untouched(self):
        context = self.make(SUCCESS)
        result = judged()
        with mock.patch("gate.verdict.request", return_value=result) as service:
            self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 0)
        kwargs = service.call_args.kwargs
        self.assertEqual((kwargs["gate"], kwargs["agent_pk_hex"], kwargs["policy"]),
                         (GATE, AGENT_PK.hex(), {**POLICY, "gate_paused": False, "gate_denylist": []}))
        self.assertNotIn("sign_actions", kwargs)
        self.assertIsInstance(kwargs["signer"], signer.TestSigner)
        self.assertEqual(self.client.calls, [
            (GATE, "get_agent", {"public_key": base58.encode_key(AGENT_PK)}),
            (GATE, "get_policy", {}),
        ])
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
        self.assertEqual(context.clients[0].calls, [])

    def test_pay_sends_nothing_on_deny(self):
        with mock.patch("gate.verdict.request", return_value=judged("DENY")):
            context = self.make()
            self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 1)
        self.assertEqual(self.transport.sent, [])
        self.assertIn("not sent: the verdict is DENY, and the Gate pays only on a signed ALLOW", self.lines)
        self.assertFalse(self.written(f"pay-{'cd' * 32}")["submitted"])

    def test_deny_alert_failure_does_not_change_the_final_verdict_or_exit_code(self):
        context = self.make()
        result = judged("DENY")
        original = json.loads(json.dumps(result))
        order = []

        class SlowBrokenSink:
            def send(self, alert):
                order.append(("send", result["outcome"]))
                self.alert = alert
                time.sleep(0.01)
                raise RuntimeError("sink failed")

        sink = SlowBrokenSink()

        def judge(*args, **kwargs):
            order.append(("judge", result["outcome"]))
            return result

        with tempfile.TemporaryDirectory() as tmp:
            url_file = Path(tmp) / "alert-url"
            url_file.write_text("https://alerts.example.test/hook\n", encoding="utf-8")
            environment = {"SHIELDBOT_ALERT_URL_FILE": str(url_file),
                           "SHIELDBOT_ALERT_SECRET_FILE": str(Path(tmp) / "secret")}
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch("gate.verdict.request", side_effect=judge), \
                    mock.patch("intel.alerts.WebhookSink", return_value=sink) as webhook_sink, \
                    self.assertLogs("intel.alerts", level="WARNING"):
                code = agent.pay(context, self.request(), out=self.lines.append)

        self.assertEqual(code, 1)
        self.assertEqual(result, original)
        self.assertEqual(order, [("judge", "DENY"), ("send", "DENY")])
        webhook_sink.assert_called_once_with("https://alerts.example.test/hook", environment["SHIELDBOT_ALERT_SECRET_FILE"])
        self.assertEqual(sink.alert.source, "payment")
        self.assertEqual(sink.alert.severity, "critical")
        self.assertEqual(sink.alert.subject, "vendor-shop.testnet")
        self.assertEqual(sink.alert.evidence_sha256, "cd" * 32)
        self.assertEqual(sink.alert.created_ms, NOW_S * 1000)
        self.assertEqual(self.transport.sent, [])

    def test_an_unknown_that_asks_a_human_raises_a_warn_alert(self):
        context = self.make()
        result = judged("UNKNOWN")
        delivered = []

        class CaptureSink:
            def send(self, alert):
                delivered.append(alert)
                return {"ok": True}

        with mock.patch("gate.verdict.request", return_value=result), \
                mock.patch("intel.alerts.configured_sinks", return_value=[CaptureSink()]):
            self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 3)

        [alert] = delivered
        self.assertEqual(alert.source, "payment")
        self.assertEqual(alert.severity, "warn")
        self.assertEqual(alert.subject, "vendor-shop.testnet")
        self.assertEqual(alert.evidence_sha256, result["evidence_hash"])
        self.assertEqual(self.transport.sent, [])

    def test_unknown_asks_a_human_and_sends_nothing(self):
        context = self.make()
        result = judged("UNKNOWN")
        result["checks"] = [
            {"id": "receiver_exists", "status": "pass", "detail": "other-payee.testnet exists"},
            {"id": "account_age", "status": "unknown", "detail": "no recorded response"},
            {"id": "dust_funded", "status": "unknown", "detail": "no recorded response"},
        ]
        result["ask"] = [f"{check['id']} could not run: {check['detail']}" for check in result["checks"]
                         if check["status"] == "unknown"]
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request("other-payee.testnet"), out=self.lines.append), 3)
        self.assertEqual(self.transport.sent, [])
        self.assertIn("not sent: the verdict is UNKNOWN, and the Gate pays only on a signed ALLOW", self.lines)
        self.assertIn("ask a human: 2 checks could not run: account_age, dust_funded", self.lines)
        allowlist_line = (f"the owner's step, when the payee is known to them: bash scripts/set_allowlist.sh "
                          f"{context.public_key} vendor-shop.testnet,other-payee.testnet")
        self.assertIn(allowlist_line, self.lines)
        listed = allowlist_line.rsplit(" ", 1)[1].split(",")
        self.assertEqual(len(listed), len(set(listed)))
        self.assertIn("scripts/set_allowlist.sh needs OWNER_ACCOUNT_ID, GATE_ACCOUNT_ID, and NEAR_KEYS_DIR", self.lines)
        self.assertIn("nothing is sent until then", self.lines)
        self.assertEqual(context.clients[0].calls, [
            (GATE, "get_agent", {"public_key": context.public_key}),
            (GATE, "get_policy", {}),
        ])
        record = self.written(f"pay-{'cd' * 32}")
        self.assertFalse(record["submitted"])
        self.assertEqual(record["ask"], result["ask"])
        unsigned = self.dir / "unknown.verdict.json"
        unsigned.write_text(json.dumps({"literal": "UNKNOWN", "evidence_hash": "cd" * 32}), encoding="utf-8")
        context = self.make()
        self.lines = []
        self.assertEqual(agent.pay(context, self.request(), str(unsigned), out=self.lines.append), 3)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(context.clients[0].calls, [])
        self.assertIn("ask a human: the verdict file carries no check details", self.lines)
        self.assertIn("nothing is sent until then", self.lines)
        self.assertFalse(any("0 checks could not run" in line for line in self.lines))
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

    def test_an_allow_the_gate_refuses_asks_a_human_and_sends_nothing(self):
        policy = json.loads((ROOT / "fixtures" / "demo" / "policy.json").read_text(encoding="utf-8"))
        context = self.make(policy=policy)
        receiver = "lindenvale.testnet"
        amount = "2000000"
        reason = (f"{receiver} is not on the agent's allowlist and {amount} is over its unknown payee cap of "
                  "1000000 per call, so the Gate refuses the transfer (REFUSED:unknown_payee_cap)")
        result = judged()
        result["action"] = "ask"
        result["ask"] = [reason]
        result["checks"] = [{"id": "receiver_exists", "status": "pass", "detail": f"{receiver} exists"}]
        request = agent.transfer_request("ft", "token.kestrelridge-main.testnet", receiver, amount, None,
                                         gate=GATE, agent_pk_hex=AGENT_PK.hex())
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, request, out=self.lines.append), 3)
        self.assertEqual(self.transport.sent, [])
        self.assertEqual(context.clients[0].calls, [
            (GATE, "get_agent", {"public_key": context.public_key}),
            (GATE, "get_policy", {}),
        ])
        command = (f"the owner's step, when the payee is known to them: bash scripts/set_allowlist.sh "
                   f"{context.public_key} kestrelridge-shop.testnet,{receiver}")
        decision_lines = [line for line in self.lines if line.startswith((
            "not sent: the verdict is ALLOW", "ask a human:", "the owner's step,",
            "scripts/set_allowlist.sh needs", "nothing is sent until then"))]
        self.assertEqual(decision_lines, [
            "not sent: the verdict is ALLOW, and the Gate refuses this transfer as it stands",
            f"ask a human: {reason}",
            command,
            "scripts/set_allowlist.sh needs OWNER_ACCOUNT_ID, GATE_ACCOUNT_ID, and NEAR_KEYS_DIR",
            "nothing is sent until then",
        ])
        record = self.written(f"pay-{'cd' * 32}")
        self.assertFalse(record["submitted"])
        self.assertEqual(record["ask"], [reason])

    def test_an_allow_while_paused_prints_only_the_pause_step(self):
        context = self.make(gate_policy={"paused": True, "denylist": []})
        result = judged()
        result["action"] = "ask"
        result["ask"] = ["the Gate is paused, so it refuses every transfer (REFUSED:paused)"]
        step = ("the owner's step: the Gate is paused; resume it with bash scripts/set_paused.sh false once the "
                "cause is resolved")
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 3)
        self.assertIn(step, self.lines)
        self.assertFalse(any("set_allowlist.sh" in line for line in self.lines))
        self.assertIn("nothing is sent until then", self.lines)
        self.assertEqual(self.transport.sent, [])
        record = self.written(f"pay-{'cd' * 32}")
        self.assertFalse(record["submitted"])
        self.assertEqual(record["ask"], result["ask"])

    def test_an_allow_with_a_disabled_agent_prints_only_the_enable_step(self):
        context = self.make(policy={**POLICY, "enabled": False})
        result = judged()
        result["action"] = "ask"
        result["ask"] = ["the agent is disabled on the Gate, so the Gate refuses the transfer "
                         "(REFUSED:agent_disabled)"]
        step = ("the owner's step: the agent is disabled on the Gate; the owner re-enables it with the Gate's "
                "set_agent_enabled call")
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 3)
        self.assertIn(step, self.lines)
        self.assertFalse(any("set_allowlist.sh" in line for line in self.lines))
        self.assertIn("nothing is sent until then", self.lines)
        self.assertEqual(self.transport.sent, [])
        record = self.written(f"pay-{'cd' * 32}")
        self.assertFalse(record["submitted"])
        self.assertEqual(record["ask"], result["ask"])

    def test_an_allow_for_a_denylisted_payee_prints_only_the_review_step(self):
        context = self.make(gate_policy={"paused": False, "denylist": ["vendor-shop.testnet"]})
        result = judged()
        result["action"] = "ask"
        result["ask"] = ["vendor-shop.testnet is on the Gate's denylist, so the Gate refuses the transfer "
                         "(REFUSED:denylisted)"]
        step = ("the owner's step: the payee is on the Gate's denylist; remove it with scripts/set_denylist.sh only "
                "after the owner has reviewed why it was added")
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 3)
        self.assertIn(step, self.lines)
        self.assertFalse(any("set_allowlist.sh" in line for line in self.lines))
        self.assertIn("nothing is sent until then", self.lines)
        self.assertEqual(self.transport.sent, [])
        record = self.written(f"pay-{'cd' * 32}")
        self.assertFalse(record["submitted"])
        self.assertEqual(record["ask"], result["ask"])

    def test_an_allowlisted_payee_with_a_window_cap_reason_gets_no_allowlist_step(self):
        context = self.make()
        result = judged()
        result["action"] = "ask"
        result["ask"] = ["the agent's window cap is exceeded (REFUSED:window_cap)"]
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 3)
        self.assertFalse(any("set_allowlist.sh" in line for line in self.lines))
        self.assertIn("nothing is sent until then", self.lines)
        self.assertEqual(self.transport.sent, [])
        self.assertFalse(self.written(f"pay-{'cd' * 32}")["submitted"])

    def test_unknown_tier2_with_a_paused_gate_prints_only_the_pause_step(self):
        context = self.make(gate_policy={"paused": True, "denylist": []})
        result = judged("UNKNOWN")
        result["checks"] = [{"id": "account_age", "status": "unknown", "detail": "age read unavailable"}]
        result["ask"] = ["account_age could not run: age read unavailable",
                         "the Gate is paused, so it refuses every transfer (REFUSED:paused)"]
        step = ("the owner's step: the Gate is paused; resume it with bash scripts/set_paused.sh false once the "
                "cause is resolved")
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request("other-payee.testnet"), out=self.lines.append), 3)
        self.assertIn(step, self.lines)
        self.assertFalse(any("set_allowlist.sh" in line for line in self.lines))
        self.assertIn("nothing is sent until then", self.lines)
        self.assertEqual(self.transport.sent, [])
        record = self.written(f"pay-{'cd' * 32}")
        self.assertFalse(record["submitted"])
        self.assertEqual(record["ask"], result["ask"])

    def test_a_third_party_check_detail_cannot_choose_the_gate_step(self):
        context = self.make()
        result = judged("UNKNOWN")
        result["checks"] = [{"id": "account_age", "status": "unknown",
                             "detail": "contract read failed: (REFUSED:paused)"}]
        result["ask"] = ["account_age could not run: contract read failed: (REFUSED:paused)"]
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request("other-payee.testnet"), out=self.lines.append), 3)
        self.assertFalse(any("the Gate is paused" in line for line in self.lines))
        self.assertTrue(any(line.startswith("the owner's step, when the payee is known to them: bash "
                                       "scripts/set_allowlist.sh") for line in self.lines))
        self.assertEqual(self.transport.sent, [])

    def test_gate_owner_steps_follow_pause_disabled_denylist_order(self):
        pause_step = ("the owner's step: the Gate is paused; resume it with bash scripts/set_paused.sh false once the "
                      "cause is resolved")
        enable_step = ("the owner's step: the agent is disabled on the Gate; the owner re-enables it with the Gate's "
                       "set_agent_enabled call")
        cases = (
            (True, False, [], pause_step),
            (False, False, ["vendor-shop.testnet"], enable_step),
        )
        for paused, enabled, denylist, expected in cases:
            with self.subTest(paused=paused, enabled=enabled, denylist=denylist):
                self.lines = []
                context = self.make(policy={**POLICY, "enabled": enabled},
                                    gate_policy={"paused": paused, "denylist": denylist})
                result = judged()
                result["action"] = "ask"
                result["ask"] = ["policy refusal"]
                with mock.patch("gate.verdict.request", return_value=result):
                    self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 3)
                steps = [line for line in self.lines if line.startswith("the owner's step")]
                self.assertEqual(steps, [expected])
                self.assertEqual(self.transport.sent, [])

    def test_mixed_tier1_and_tier2_unknowns_offer_no_allowlist_step(self):
        context = self.make()
        result = judged("UNKNOWN")
        result["checks"] = [
            {"id": "storage_registered", "status": "unknown", "detail": "token read unavailable"},
            {"id": "account_age", "status": "unknown", "detail": "age read unavailable"},
        ]
        result["ask"] = [f"{check['id']} could not run: {check['detail']}" for check in result["checks"]]
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request("other-payee.testnet"), out=self.lines.append), 3)
        self.assertEqual(self.transport.sent, [])
        self.assertIn("the owner's step: allowlisting skips only the tier 2 checks, so pay again when the source of "
                      "storage_registered, account_age answers", self.lines)
        self.assertFalse(any(line.startswith("the owner's step, when the payee is known") for line in self.lines))

    def test_unknown_tier1_check_offers_no_allowlist_step(self):
        context = self.make()
        result = judged("UNKNOWN")
        result["checks"] = [{"id": "storage_registered", "status": "unknown", "detail": "token read unavailable"}]
        result["ask"] = ["storage_registered could not run: token read unavailable"]
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request("other-payee.testnet"), out=self.lines.append), 3)
        self.assertEqual(self.transport.sent, [])
        self.assertIn("the owner's step: allowlisting skips only the tier 2 checks, so pay again when the source "
                      "of storage_registered answers", self.lines)
        self.assertFalse(any(line.startswith("the owner's step, when the payee is known") for line in self.lines))

    def test_a_disabled_agent_asks_and_sends_nothing(self):
        context = self.make(policy={**POLICY, "enabled": False})
        result = judged()
        result["action"] = "ask"
        result["ask"] = ["the agent is disabled on the Gate, so the Gate refuses the transfer "
                         "(REFUSED:agent_disabled)"]
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request(), out=self.lines.append), 3)
        self.assertEqual(self.transport.sent, [])
        self.assertIn("ask a human: the agent is disabled on the Gate, so the Gate refuses the transfer "
                      "(REFUSED:agent_disabled)", self.lines)
        self.assertFalse(self.written(f"pay-{'cd' * 32}")["submitted"])

    def test_an_allowlisted_unknown_does_not_print_an_allowlist_step(self):
        policy = {**POLICY, "allowlist": ["vendor-shop.testnet", "other-payee.testnet"]}
        context = self.make(policy=policy)
        result = judged("UNKNOWN")
        result["checks"] = [{"id": "receiver_exists", "status": "unknown", "detail": "no recorded response"}]
        result["ask"] = ["receiver_exists could not run: no recorded response"]
        with mock.patch("gate.verdict.request", return_value=result):
            self.assertEqual(agent.pay(context, self.request("other-payee.testnet"), out=self.lines.append), 3)
        self.assertFalse(any(line.startswith("the owner's step,") for line in self.lines))
        self.assertEqual(self.transport.sent, [])

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
    def run_attack(self, name, *outcomes, receiver="vendor-shop.testnet", amount="1000000",
                   verdict_outcome="ALLOW"):
        context = self.make(*outcomes)
        with mock.patch("gate.verdict.request", return_value=judged(verdict_outcome)):
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
            "injected": [request_transfer],
        }
        self.assertEqual(sorted(rows), sorted(agent.ATTACKS))
        for name, expected in rows.items():
            with self.subTest(attack=name):
                self.lines = []
                code, sent = self.run_attack(name, SUCCESS if name == "replay" else PANIC, PANIC,
                                             verdict_outcome="DENY" if name == "injected" else "ALLOW")
                self.assertEqual([self.shape(s) for s in sent], expected)
                self.assertEqual(code, 0)
                record = self.written(name)
                self.assertTrue(record["refused"])
                self.assertEqual([s["sent"] for s in record["submissions"]], sent)
                self.assertIn(f"{name}: refused", self.lines)

    def test_injected_sends_one_request_without_a_verdict_after_a_deny(self):
        context = self.make(PANIC)
        result = judged("DENY")
        result["reasons"] = ["the collector carries an incident label"]
        with mock.patch("gate.verdict.request", return_value=result):
            code = agent.attack("injected", context, self.request(), out=self.lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(self.transport.sent[0]["method_name"], "request_transfer")
        self.assertIsNone(self.transport.sent[0]["args"]["verdict"])
        record = self.written("injected")
        self.assertTrue(record["refused"])
        self.assertEqual(record["notes"], ["the verdict service answered DENY: the collector carries an incident label",
                                           "the agent submits without a verdict"])

    def test_injected_stops_when_the_service_allows(self):
        context = self.make()
        with mock.patch("gate.verdict.request", return_value=judged("ALLOW")):
            with self.assertRaisesRegex(agent.AgentError, "not DENY"):
                agent.attack("injected", context, self.request(), out=self.lines.append)
        self.assertEqual(self.transport.sent, [])

    def test_injected_stops_when_the_service_answers_unknown(self):
        context = self.make()
        with mock.patch("gate.verdict.request", return_value=judged("UNKNOWN")):
            with self.assertRaisesRegex(agent.AgentError, "UNKNOWN.*not DENY"):
                agent.attack("injected", context, self.request(), out=self.lines.append)
        self.assertEqual(self.transport.sent, [])

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

    def test_the_attack_harness_requests_a_signature_for_an_allow_ask(self):
        context = self.make(PANIC)
        result = judged()
        result["action"] = "ask"
        result["ask"] = ["the Gate is paused (REFUSED:paused)"]
        with mock.patch("gate.verdict.request", return_value=result) as service:
            code = agent.attack("over-cap", context, self.request(), out=self.lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(service.call_args.kwargs["sign_actions"], ("sign", "ask"))
        self.assertEqual(self.transport.sent[0]["args"]["verdict"], result["verdict"])

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
        account = self.transport._new_account()
        self.assertEqual(account._provider.timeout, rpc.TIMEOUT_S)
        self.assertEqual(list(account._signer_by_pk), [base58.encode(AGENT_PK)])

    def test_the_outcome_of_a_sent_and_a_rejected_transaction(self):
        sent = agent.action(GATE, "request_transfer", {"request": {}, "verdict": None})
        result = mock.Mock(status={"SuccessValue": ""}, logs=["EVENT_JSON:{}"])
        result.transaction.hash = SUCCESS["transaction_hash"]
        from py_near.account import Account
        with mock.patch.object(Account, "startup", mock.AsyncMock()), \
                mock.patch.object(Account, "shutdown", mock.AsyncMock()), \
                mock.patch.object(Account, "function_call", mock.AsyncMock(return_value=result)) as call:
            outcome = self.transport(sent)
        call.assert_awaited_once_with(GATE, "request_transfer", {"request": {}, "verdict": None}, gas=30 * 10**12,
                                      amount=0)
        self.assertEqual(outcome, {"transaction_hash": SUCCESS["transaction_hash"], "status": {"SuccessValue": ""},
                                   "logs": ["EVENT_JSON:{}"], "rpc_error": None})
        error = self.rejected({}, error_json=REJECTED["rpc_error"])
        error.trx_hash = REJECTED["transaction_hash"]
        with mock.patch.object(Account, "startup", mock.AsyncMock()), \
                mock.patch.object(Account, "shutdown", mock.AsyncMock()), \
                mock.patch.object(Account, "function_call", mock.AsyncMock(side_effect=error)):
            outcome = self.transport(sent)
        self.assertEqual(outcome, {"transaction_hash": REJECTED["transaction_hash"], "status": None, "logs": [],
                                   "rpc_error": REJECTED["rpc_error"]})

    def test_each_send_gets_its_own_account_and_closes_it(self):
        # asyncio.run makes a new event loop per send; an account reused across loops kept a session bound to
        # the closed first loop, so the second send of the replay attack never reached the RPC.
        from py_near.account import Account
        sent = agent.action(GATE, "request_transfer", {"request": {}, "verdict": None})
        result = mock.Mock(status={"SuccessValue": ""}, logs=[])
        result.transaction.hash = SUCCESS["transaction_hash"]
        seen = []

        async def startup(account):
            seen.append(account)

        with mock.patch.object(Account, "startup", startup), \
                mock.patch.object(Account, "function_call", mock.AsyncMock(return_value=result)), \
                mock.patch.object(Account, "shutdown", mock.AsyncMock()) as shutdown:
            self.transport(sent)
            self.transport(sent)
        self.assertEqual(len(seen), 2)
        self.assertIsNot(seen[0], seen[1])
        self.assertEqual(shutdown.await_count, 2)

    def test_a_rejection_without_rpc_json_still_records_why(self):
        # py-near 1.2.22 raises InvalidTransactionError("DepositWithFunctionCall") with error_json None when a
        # function call key attaches a deposit; the record must still say it was rejected and why.
        import py_near.exceptions.provider
        from py_near.account import Account
        sent = agent.action(GATE, "request_transfer", {"request": {}, "verdict": None})
        error = py_near.exceptions.provider.InvalidTransactionError("DepositWithFunctionCall")
        error.trx_hash = REJECTED["transaction_hash"]
        with mock.patch.object(Account, "startup", mock.AsyncMock()), \
                mock.patch.object(Account, "function_call", mock.AsyncMock(side_effect=error)), \
                mock.patch.object(Account, "shutdown", mock.AsyncMock()) as shutdown:
            outcome = self.transport(sent)
        self.assertEqual(shutdown.await_count, 1)
        self.assertEqual(outcome["status"], None)
        self.assertEqual(outcome["rpc_error"], {"error": "InvalidTransactionError", "message": "DepositWithFunctionCall"})
        self.assertFalse(agent._succeeded(outcome))


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

    def test_attack_injected_needs_a_message_and_the_others_need_the_transfer_fields(self):
        code, errors = self.run_command(["attack", "injected", "--out", "unused"], {})
        self.assertEqual((code, errors), (2, "not sent: attack injected reads the transfer from --message\n"))
        code, errors = self.run_command(["attack", "no-verdict", "--kind", "ft", "--amount", "1", "--out",
                                         "unused"], {})
        self.assertEqual((code, errors), (2, "not sent: set --kind, --receiver and --amount\n"))
        code, errors = self.run_command(["attack", "injected", "--message", "fixtures/demo/injected-message.txt",
                                         "--receiver", "x", "--out", "unused"], {})
        self.assertEqual((code, errors),
                         (2, "not sent: attack injected reads the transfer from --message; do not pass "
                             "transfer options\n"))
        for option in ("--token", "--memo"):
            with self.subTest(option=option):
                code, errors = self.run_command(["attack", "injected", "--message",
                                                  "fixtures/demo/injected-message.txt", option, "value",
                                                  "--out", "unused"], {})
                self.assertEqual((code, errors),
                                 (2, "not sent: attack injected reads the transfer from --message; do not pass "
                                     "transfer options\n"))

    def test_injected_message_read_and_parse_errors_are_not_tracebacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "agent-public.json"
            key_file.write_text(json.dumps({"public_key": demo.AGENT_KEY}), encoding="utf-8")

            def public_only_key(path):
                public_key = json.loads(Path(path).read_text(encoding="utf-8"))["public_key"]
                return base58.decode_key(public_key).hex(), public_key, None

            environment = {"GATE_ACCOUNT_ID": GATE, "AGENT_KEY_FILE": str(key_file)}
            missing_path = Path(tmp) / "missing-message.txt"
            cases = (
                ("missing file", missing_path, None, "cannot read --message:"),
                ("missing pay-to", Path(tmp) / "missing-pay-to.txt", "From: billing\n\namount: 1\n",
                 "missing pay-to"),
                ("missing amount", Path(tmp) / "missing-amount.txt",
                 "From: billing\n\npay-to: vendor-shop.testnet\n", "missing amount"),
            )
            with mock.patch("gate.agent.agent_key", side_effect=public_only_key), \
                    mock.patch("gate.agent.NearCliTransport") as transport:
                for name, message_path, message, expected in cases:
                    with self.subTest(error=name):
                        if message is not None:
                            message_path.write_text(message, encoding="utf-8")
                        code, errors = self.run_command(
                            ["attack", "injected", "--message", str(message_path), "--via", "near-cli", "--out",
                             str(Path(tmp) / "out")], environment)
                        self.assertEqual(code, 2)
                        self.assertTrue(errors.startswith(f"not sent: {expected}"), errors)
                        self.assertNotIn("Traceback", errors)
                transport.assert_not_called()

    def test_pay_in_default_replay_refuses_without_a_live_agent_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "agent-public.json"
            key_file.write_text(json.dumps({"public_key": demo.AGENT_KEY}), encoding="utf-8")

            def public_only_key(path):
                public_key = json.loads(Path(path).read_text(encoding="utf-8"))["public_key"]
                return base58.decode_key(public_key).hex(), public_key, None

            transport = Transport()
            output, errors = io.StringIO(), io.StringIO()
            argv = ["pay", "--kind", "ft", "--token", "token.kestrelridge-main.testnet", "--receiver",
                    "kestrelridge-shop.testnet", "--amount", "1", "--via", "near-cli", "--out",
                    str(Path(tmp) / "out")]
            environment = {"GATE_ACCOUNT_ID": "kestrelridge-main.testnet", "AGENT_KEY_FILE": str(key_file)}
            with (mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch("gate.agent.agent_key", side_effect=public_only_key),
                  mock.patch("gate.agent.NearCliTransport", return_value=transport),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("replay opened the network")),
                  contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
                self.assertNotIn("GATE_FIXTURES_MODE", os.environ)
                code = gate_main.main(argv)
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(),
                         "not sent: pay reads the Gate live: set GATE_FIXTURES_MODE=record or off\n")
        self.assertEqual(transport.sent, [])

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
