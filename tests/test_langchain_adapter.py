import importlib.util
import json
import unittest
import urllib.error
from pathlib import Path

from integrations.langchain.adapter import decide_account_check, decide_payment, preflight, tool_result

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "integrations" / "langchain" / "fixtures"
COLLECTOR = "31ac7a2705a0686ff427b1a52d3ffd1fcfaa4b1f3cb3e83a0f767494e724a540"
AMOUNT = "1000000000000000000000000"


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


class LangChainAdapterTest(unittest.TestCase):
    def test_recorded_answers(self):
        allow = decide_payment(load("verdict-allow.json"), "intents.near", AMOUNT)
        self.assertEqual(allow["decision"], "allow")
        deny = decide_payment(load("verdict-deny.json"), COLLECTOR, AMOUNT)
        self.assertEqual(deny["decision"], "deny")
        self.assertTrue(deny["reasons"])
        ask = decide_account_check(load("account-check-ask.json"), "hello.near")
        self.assertEqual(ask["decision"], "ask")
        self.assertIn("full access key", " ".join(ask["reasons"]))

    def test_the_answer_must_bind_to_the_transfer(self):
        allow = load("verdict-allow.json")
        self.assertEqual(decide_payment(allow, "another.near", AMOUNT)["decision"], "ask")
        self.assertEqual(decide_payment(allow, "intents.near", "1")["decision"], "ask")
        self.assertEqual(decide_payment(allow, "intents.near", AMOUNT, token="wrap.near")["decision"], "ask")
        self.assertEqual(decide_account_check(allow, "intents.near")["decision"], "ask")

    def test_an_edited_answer_is_ask(self):
        allow = load("verdict-allow.json")
        edited = dict(allow, outcome="DENY")
        self.assertEqual(decide_payment(edited, "intents.near", AMOUNT)["decision"], "ask")
        no_evidence = dict(allow)
        del no_evidence["evidence"]
        self.assertEqual(decide_payment(no_evidence, "intents.near", AMOUNT)["decision"], "ask")

    def test_allow_must_be_complete(self):
        allow = load("verdict-allow.json")
        self.assertEqual(decide_payment(dict(allow, action="ask"), "intents.near", AMOUNT)["decision"], "ask")
        self.assertEqual(decide_payment(dict(allow, signing="sometimes"), "intents.near", AMOUNT)["decision"], "ask")
        broken = json.loads(json.dumps(allow))
        broken["checks"][0]["status"] = "unknown"
        self.assertEqual(decide_payment(broken, "intents.near", AMOUNT)["decision"], "ask")
        self.assertEqual(decide_payment(None, "intents.near", AMOUNT)["decision"], "ask")
        self.assertEqual(decide_payment(allow, "", AMOUNT)["decision"], "ask")

    def test_preflight_maps_http_failures_to_ask(self):
        def unavailable(url, timeout):
            raise urllib.error.HTTPError(url, 503, "busy", {}, None)

        decision = preflight(receiver="intents.near", amount=AMOUNT, opener=unavailable)
        self.assertEqual(decision["decision"], "ask")
        self.assertIn("503", decision["reasons"][0])

        def offline(url, timeout):
            raise urllib.error.URLError("offline")

        self.assertEqual(preflight(receiver="intents.near", amount=AMOUNT, opener=offline)["decision"], "ask")

    def test_preflight_reads_the_recorded_answer(self):
        def allow(url, timeout):
            self.assertIn("/live/verdict?", url)
            return FakeResponse(load("verdict-allow.json"))

        self.assertEqual(preflight(receiver="intents.near", amount=AMOUNT, opener=allow)["decision"], "allow")

        def account(url, timeout):
            self.assertTrue(url.endswith("/live/account-check/hello.near"))
            return FakeResponse(load("account-check-ask.json"))

        self.assertEqual(preflight(receiver="hello.near", opener=account)["decision"], "ask")

    def test_tool_result_wording(self):
        self.assertTrue(tool_result({"decision": "deny", "reasons": ["A signal."]}).startswith("DENY: do not send"))
        self.assertIn("person must decide", tool_result({"decision": "ask", "reasons": []}))
        self.assertIn("not permission to pay", tool_result({"decision": "allow", "reasons": []}))

    @unittest.skipUnless(importlib.util.find_spec("langchain_core"), "langchain-core is unavailable")
    def test_the_langchain_tool_wraps_the_check(self):
        from integrations.langchain.adapter import as_tool

        check = as_tool()
        self.assertEqual(check.name, "siyab_check_payment")


if __name__ == "__main__":
    unittest.main()
