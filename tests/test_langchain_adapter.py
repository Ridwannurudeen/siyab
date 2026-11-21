import importlib.util
import json
import unittest
import urllib.error
from pathlib import Path

from integrations.langchain.adapter import (
    ConfirmationRequired,
    PaymentRefused,
    decide_account_check,
    decide_payment,
    guard_payment,
    preflight,
    tool_result,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "integrations" / "langchain" / "fixtures"
COLLECTOR = "31ac7a2705a0686ff427b1a52d3ffd1fcfaa4b1f3cb3e83a0f767494e724a540"
AMOUNT = "1000000000000000000000000"
TOKEN = "usdt.tether-token.near"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RecordingOpener:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


class RecordedAnswerTest(unittest.TestCase):
    def test_near_answers(self):
        self.assertEqual(decide_payment(load("verdict-allow.json"), "intents.near", AMOUNT)["decision"], "allow")
        self.assertEqual(decide_payment(load("verdict-deny.json"), COLLECTOR, AMOUNT)["decision"], "deny")
        self.assertEqual(decide_account_check(load("account-check-ask.json"), "hello.near")["decision"], "ask")

    def test_token_answers_use_the_ft_kind(self):
        self.assertEqual(decide_payment(load("verdict-token-allow.json"), "intents.near", "1000000", TOKEN)["decision"],
                         "allow")
        self.assertEqual(decide_payment(load("verdict-token-deny.json"), "hello.near", "1000000", TOKEN)["decision"],
                         "deny")
        # The same answer must not pass as a NEAR transfer.
        self.assertEqual(decide_payment(load("verdict-token-allow.json"), "intents.near", "1000000")["decision"], "ask")

    def test_a_deny_is_honoured_before_binding(self):
        deny = load("verdict-token-deny.json")
        self.assertEqual(decide_payment(deny, "someone-else.near", "1", TOKEN)["decision"], "deny")
        self.assertEqual(decide_payment({"outcome": "DENY", "reasons": ["A signal."]}, "x.near", "1")["decision"],
                         "deny")

    def test_an_edited_answer_is_ask(self):
        allow = load("verdict-allow.json")
        edited = dict(allow, outcome="ALLOW", reasons=["Every check ran."])
        edited["evidence_hash"] = "0" * 64
        self.assertEqual(decide_payment(edited, "intents.near", AMOUNT)["decision"], "ask")
        no_evidence = dict(allow)
        del no_evidence["evidence"]
        self.assertEqual(decide_payment(no_evidence, "intents.near", AMOUNT)["decision"], "ask")

    def test_reasons_and_checks_come_from_the_evidence(self):
        allow = load("verdict-allow.json")
        tampered = dict(allow, reasons=["A made up top level reason."])
        decision = decide_payment(tampered, "intents.near", AMOUNT)
        self.assertEqual(decision["decision"], "allow")
        self.assertIn("Every check ran", " ".join(decision["reasons"]))
        self.assertNotIn("made up", " ".join(decision["reasons"]))

    def test_an_unknown_outcome_is_ask(self):
        self.assertEqual(decide_payment({"outcome": "MAYBE"}, "a.near", "1")["decision"], "ask")
        self.assertEqual(decide_payment({"verdict": "ALLOW"}, "a.near", "1")["decision"], "ask")

    def test_allow_must_be_complete(self):
        allow = load("verdict-allow.json")
        self.assertEqual(decide_payment(dict(allow, action="ask"), "intents.near", AMOUNT)["decision"], "ask")
        self.assertEqual(decide_payment(dict(allow, signing="sometimes"), "intents.near", AMOUNT)["decision"], "ask")
        broken = json.loads(json.dumps(allow))
        broken["checks"][0]["status"] = "unknown"
        evidence = json.loads(broken["evidence"])
        evidence["checks"][0]["status"] = "unknown"
        import hashlib
        broken["evidence"] = json.dumps(evidence)
        broken["evidence_hash"] = hashlib.sha256(broken["evidence"].encode()).hexdigest()
        self.assertEqual(decide_payment(broken, "intents.near", AMOUNT)["decision"], "ask")


class PreflightTest(unittest.TestCase):
    def test_inputs_are_validated_before_the_network(self):
        opener = RecordingOpener(load("verdict-allow.json"))
        self.assertEqual(preflight(receiver="Not An Account", amount=AMOUNT, opener=opener)["decision"], "ask")
        self.assertEqual(preflight(receiver="a.near", amount="0", opener=opener)["decision"], "ask")
        self.assertEqual(preflight(receiver="a.near", amount="1", token="bad token", opener=opener)["decision"], "ask")
        self.assertEqual(opener.urls, [])

    def test_every_failure_becomes_ask(self):
        self.assertEqual(preflight(receiver="a.near", amount="1",
                                   opener=RecordingOpener(error=urllib.error.HTTPError("u", 429, "busy", {}, None)))
                         ["decision"], "ask")
        self.assertEqual(preflight(receiver="a.near", amount="1",
                                   opener=RecordingOpener(error=TimeoutError()))["decision"], "ask")
        self.assertEqual(preflight(receiver="a.near", amount="1",
                                   opener=RecordingOpener(error=RuntimeError("boom")))["decision"], "ask")

    def test_a_malformed_body_becomes_ask(self):
        class BadResponse:
            def read(self):
                return b"{not json"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        self.assertEqual(preflight(receiver="a.near", amount="1", opener=lambda url, timeout: BadResponse())
                         ["decision"], "ask")

    def test_a_recorded_answer_is_read(self):
        opener = RecordingOpener(load("verdict-token-allow.json"))
        decision = preflight(receiver="intents.near", amount="1000000", token=TOKEN, opener=opener)
        self.assertEqual(decision["decision"], "allow")
        self.assertIn("/live/verdict?", opener.urls[0])

        account = RecordingOpener(load("account-check-ask.json"))
        self.assertEqual(preflight(receiver="hello.near", opener=account)["decision"], "ask")
        self.assertTrue(account.urls[0].endswith("/live/account-check/hello.near"))

    def test_arguments_are_keyword_only(self):
        with self.assertRaises(TypeError):
            preflight("a.near", "1")  # noqa: E501


class ToolResultTest(unittest.TestCase):
    def test_wording(self):
        self.assertTrue(tool_result({"decision": "deny", "reasons": ["A signal."]}).startswith("DENY: do not send"))
        self.assertIn("person must decide", tool_result({"decision": "ask", "reasons": []}))
        self.assertIn("not permission to pay", tool_result({"decision": "allow", "reasons": []}))


class GuardTest(unittest.TestCase):
    def test_deny_blocks_the_send(self):
        called = []
        with self.assertRaises(PaymentRefused):
            guard_payment(send=lambda: called.append("sent"), receiver=COLLECTOR, amount=AMOUNT,
                          opener=RecordingOpener(load("verdict-deny.json")))
        self.assertEqual(called, [])

    def test_ask_needs_a_confirmation(self):
        called = []
        ask = load("account-check-ask.json")
        with self.assertRaises(ConfirmationRequired):
            guard_payment(send=lambda: called.append("sent"), receiver="hello.near", amount="1",
                          opener=RecordingOpener(ask))
        self.assertEqual(called, [])
        with self.assertRaises(ConfirmationRequired):
            guard_payment(send=lambda: called.append("sent"), receiver="hello.near", amount="1",
                          confirm=lambda decision: False, opener=RecordingOpener(ask))
        self.assertEqual(called, [])

    def test_allow_sends(self):
        called = []

        def send():
            called.append("sent")
            return "sent"

        self.assertEqual(guard_payment(send=send, receiver="intents.near", amount=AMOUNT,
                                       opener=RecordingOpener(load("verdict-allow.json"))), "sent")
        self.assertEqual(called, ["sent"])

    @unittest.skipUnless(importlib.util.find_spec("langchain_core"), "langchain-core is unavailable")
    def test_the_langchain_tool_wraps_the_check(self):
        from integrations.langchain.adapter import as_tool

        self.assertEqual(as_tool().name, "siyab_check_payment")


if __name__ == "__main__":
    unittest.main()
