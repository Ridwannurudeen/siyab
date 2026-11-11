"""The EVM leg over recorded fixtures. The committed 1Click quote fixture keeps every field but the
quote's ed25519 signature, which is replaced by a note, and the firewall fixture has its em dashes
written as hyphens: both would trip the repository hygiene hook."""

import base64
import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import evidence, intents, rpc
from intel import sanctions
from mcp import server as mcp_server

USDC_BASE_ASSET = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
DEMO_RECIPIENT = "0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107"
DEPOSIT_ADDRESS = "deposit.near"
ONE_NEAR = "1000000000000000000000000"
CLOCK = datetime(2026, 11, 3, 12, 0, tzinfo=UTC).timestamp()
FIREWALL_URL = "https://api.example"
# The deadline the synthetic dry quotes are judged with: ten minutes after CLOCK.
DEADLINE = "2026-11-03T12:10:00Z"
# The deadline of a real quote in the synthetic status answers: ten minutes after CLOCK, with milliseconds.
STATUS_DEADLINE = "2026-11-03T12:10:00.000Z"
# A status_for value written as an explicit JSON null; a None value leaves its key out.
JSON_NULL = object()
# No recorded source lists the 1Click status values, so the leg accepts none. The synthetic status
# answers carry WAITING, which every test but those of ACCEPTED_STATUSES accepts in their place.
ACCEPTED_STATUSES = intents.DEPOSIT_STATUSES
WAITING = "SYNTHETIC_WAITING_STATUS"
PAYER = "payer.near"
NAMED_FEES = [{"recipient": "fees.near", "fee": 20}]
# A lone surrogate: valid JSON as the escape \ud800, which json.loads reads as text with no UTF-8 form.
SURROGATE = "\ud800"
# The recorded 1Click dry quote: the one committed intents fixture answering /v0/quote.
[RECORDED_QUOTE] = [path for path in (intents.FIXTURES_ROOT / "intents").glob("*.json")
                    if json.loads(path.read_text(encoding="utf-8"))["url"].endswith("/v0/quote")]
# The app fee 1Click added by itself to that recorded dry echo, read from the fixture, not the data file.
[SERVICE_FEE] = json.loads(RECORDED_QUOTE.read_text(encoding="utf-8"))["response"]["quoteRequest"]["appFees"]


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def reply(payload):
    handle = mock.MagicMock()
    handle.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return handle


def judge(recipient=DEMO_RECIPIENT, destination=USDC_BASE_ASSET, **options):
    """judge_quote at CLOCK: over the committed fixtures with the recorded deadline, over a synthetic
    fixtures root with DEADLINE, unless options name another deadline."""
    deadline = DEADLINE if "fixtures_root" in options else intents.RECORDED_DEADLINE
    return intents.judge_quote("nep141:wrap.near", destination, ONE_NEAR, recipient, clock=lambda: CLOCK,
                               **{"mode": "replay", "deadline": deadline, **options})


def verify(answer, quote_request, clock=CLOCK):
    intents.verify_binding(answer, quote_request, clock=lambda: clock)


def rehashed(answer, **judged):
    """A copy of answer whose judged quote request has judged applied, a None value removing the key,
    with its evidence hash recomputed: a forgery verify_binding cannot tell from an intact answer."""
    forged = json.loads(json.dumps(answer))
    request = dict(forged["document"]["subject"]["quote_request"], **judged)
    forged["document"]["subject"]["quote_request"] = {key: value for key, value in request.items()
                                                      if value is not None}
    forged["evidence_hash"] = evidence.evidence_hash(forged["document"])
    return forged


def detail(result, check_id):
    return next(c["detail"] for c in result["checks"] if c["id"] == check_id)


def statuses(result):
    return {check["id"]: check["status"] for check in result["checks"]}


class Case(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("urllib.request.urlopen", side_effect=no_network)
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("gate.intents.DEPOSIT_STATUSES", (WAITING,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def root(self, *copy):
        """A fixtures root holding copies of the committed fixture directories named in copy."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name in ("intents", "goplus", "evm"):
            if name in copy:
                shutil.copytree(intents.FIXTURES_ROOT / name, root / name)
            else:
                (root / name).mkdir()
        return root

    def write(self, root, directory, url, method, params, response):
        key = rpc.fixture_key(url, method, params)
        fixture = {"request": {"method": method, "params": params}, "response": response, "url": url,
                   "fetched_at": "2026-11-03T11:00:00Z", "synthetic": True}
        (root / directory / f"{key}.json").write_text(json.dumps(fixture), encoding="utf-8")

    def quote_for(self, root, recipient, amount_out="4398699", timestamp="2026-11-03T11:59:59Z", echo=None,
                  deadline=DEADLINE, answer=None, **options):
        """A dry quote answer for the request options make, with echo applied to its quoteRequest and
        answer to its top level."""
        body = intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, recipient, "DESTINATION_CHAIN",
                                  deadline, **options)
        response = {"quote": {"amountOut": amount_out, "minAmountOut": amount_out},
                    "quoteRequest": dict(body, **(echo or {})), "timestamp": timestamp, "correlationId": "c",
                    **(answer or {})}
        self.write(root, "intents", intents.ONECLICK_URL + "/v0/quote", "POST", rpc.body_params(body), response)

    def quote_for_destination(self, root, recipient, destination):
        body = intents.quote_body("nep141:wrap.near", destination, ONE_NEAR, recipient, "DESTINATION_CHAIN",
                                  DEADLINE)
        response = {"quote": {"amountOut": "4398699", "minAmountOut": "4354712"}, "quoteRequest": dict(body),
                    "timestamp": "2026-11-03T11:59:59Z", "correlationId": "c"}
        self.write(root, "intents", intents.ONECLICK_URL + "/v0/quote", "POST", rpc.body_params(body), response)

    def status_for(self, root, deposit_address, quote_request, lifecycle=WAITING, **real):
        """A 1Click status answer for deposit_address with status lifecycle whose quoteResponse carries
        quote_request and a fresh real quote for deposit_address, deadline STATUS_DEADLINE and timestamp
        11:59:59Z, with real applied to the quoteResponse, a None value removing a key and JSON_NULL
        writing null."""
        response = dict({"quoteRequest": quote_request,
                         "quote": {"deadline": STATUS_DEADLINE, "depositAddress": deposit_address},
                         "timestamp": "2026-11-03T11:59:59Z"}, **real)
        answer = {"quoteResponse": {key: None if value is JSON_NULL else value for key, value in response.items()
                                    if value is not None}, "status": lifecycle}
        self.write(root, "intents", intents.ONECLICK_URL + "/v0/status", "GET",
                   {"depositAddress": deposit_address},
                   {key: None if value is JSON_NULL else value for key, value in answer.items() if value is not None})

    def firewall_says(self, root, classification, recipient=USDC_BASE, value="4398699", **scan):
        """A complete firewall scan unless scan sets status, partial or failed_sources; None leaves a key out."""
        body = {"from": intents.INTENTS_EVM_TREASURY, "to": recipient, "value": value, "data": "0x", "chainId": 8453}
        answer = {"status": "ok", "partial": False, "failed_sources": [], "classification": classification,
                  "risk_score": 1, "evidence_hash": "0xab", "evidence_url": FIREWALL_URL + "/evidence/0xab"}
        answer.update(scan)
        self.write(root, "evm", FIREWALL_URL + "/api/firewall", "POST", rpc.body_params(body),
                   {key: value for key, value in answer.items() if value is not None})

    def goplus_says(self, root, address, body):
        self.write(root, "goplus", f"{intents.GOPLUS_URL}/api/v1/address_security/{address}", "GET", None, body)


class DryQuoteTest(Case):
    def test_the_body_always_carries_dry_true(self):
        for recipient_type in ("DESTINATION_CHAIN", "INTENTS"):
            body = intents.quote_body("a", "b", "1", "r", recipient_type, "d")
            self.assertIs(body["dry"], True)
        client = rpc.Client("https://quote.example", "off", kind="http")
        with mock.patch("urllib.request.urlopen", return_value=reply({"ok": True})) as opened:
            intents.dry_quote("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, DEMO_RECIPIENT, client=client)
        sent = json.loads(opened.call_args.args[0].data)
        self.assertIs(sent["dry"], True)
        self.assertEqual(opened.call_args.args[0].full_url, "https://quote.example/v0/quote")
        for field in ("swapType", "slippageTolerance", "originAsset", "depositType", "destinationAsset", "amount",
                      "refundTo", "refundType", "recipient", "recipientType", "deadline"):
            self.assertIn(field, sent)

    def test_a_non_dry_call_raises_before_urlopen(self):
        client = rpc.Client("https://quote.example", "off", kind="http")
        for dry in (False, None, 1, "true"):
            with self.subTest(dry=dry), self.assertRaises(ValueError):
                intents.dry_quote("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, DEMO_RECIPIENT, client=client,
                                  dry=dry)
        self.urlopen.assert_not_called()

    def test_judge_quote_always_passes_dry_true(self):
        original = intents.dry_quote

        def dry_only(*args, **kwargs):
            self.assertIs(kwargs.get("dry"), True)
            return original(*args, **kwargs)

        with mock.patch("gate.intents.dry_quote", side_effect=dry_only) as dry_quote:
            judge()
        dry_quote.assert_called_once()

    def test_the_recorded_quote_was_dry(self):
        [path] = [p for p in (intents.FIXTURES_ROOT / "intents").glob("*.json")
                  if json.loads(p.read_text(encoding="utf-8"))["url"].endswith("/v0/quote")]
        fixture = json.loads(path.read_text(encoding="utf-8"))
        self.assertIs(fixture["response"]["quoteRequest"]["dry"], True)
        self.assertNotIn("depositAddress", fixture["response"]["quote"])


class DemoRecipientTest(Case):
    def test_the_demo_recipient_is_deny_with_the_labels_and_the_fetch_time(self):
        result = judge()
        self.assertEqual(result["verdict"], "DENY")
        self.assertEqual(statuses(result), {"quote_dry": "pass", "quote_age": "unknown",
                                            "quote_deadline": "unknown", "destination_supported": "pass",
                                            "recipient_theft_label": "signal", "recipient_code": "pass",
                                            "ofac_sanctioned": "pass"})
        [reason] = result["reasons"]
        self.assertIn("stealing_attack", reason)
        self.assertIn('"SlowMist,BlockSec"', reason)
        fetched = next(c for c in result["checks"] if c["id"] == "recipient_theft_label")["source"]["fetched_at"]
        self.assertRegex(fetched, r"^2026-10-29T")
        self.assertIn(f"fetched {fetched}", reason)
        self.assertEqual(result["document"]["kind"], "intents")
        self.assertIsNone(result["document"]["verdict_id"])

    def test_the_evidence_hash_is_stable(self):
        first, second = judge(), judge()
        self.assertEqual(first["evidence_hash"], second["evidence_hash"])
        self.assertEqual(first["evidence_hash"], base64.b64decode("JIglXz2n7amjr+sZK//mTWIxsUDoqTq31yNLlqqtKUc=",
                                                                   validate=True).hex())

    def test_the_command_prints_deny_and_the_closing_line(self):
        output = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output):
            code = gate_main.main(["intents", "--origin", "nep141:wrap.near", "--destination", USDC_BASE_ASSET,
                                   "--amount", ONE_NEAR, "--recipient", DEMO_RECIPIENT])
        lines = output.getvalue().splitlines()
        self.assertEqual(code, 0)
        self.assertIn("verdict: DENY (unsigned preflight)", lines)
        self.assertEqual(lines[-1], "Nothing was signed, nothing was deposited.")
        self.assertTrue(lines[0].startswith("dry quote: "))
        self.assertIn("deposit address not checked: a dry quote has no deposit address", lines)

    def test_the_command_refuses_a_different_deposit_address_with_its_reason(self):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("gate.intents.bind_deposit_address", side_effect=ValueError("originAsset differs")) as bind, \
                mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(errors):
            code = gate_main.main(["intents", "--origin", "nep141:wrap.near", "--destination", USDC_BASE_ASSET,
                                   "--amount", ONE_NEAR, "--recipient", DEMO_RECIPIENT,
                                   "--deposit-address", "other.near", "--refund-to", PAYER])
        self.assertEqual(code, 1)
        bind.assert_called_once()
        self.assertIn("originAsset", errors.getvalue())
        self.assertNotIn("verdict:", output.getvalue())

    def test_the_command_refuses_a_deposit_address_without_a_named_refund_account(self):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("gate.intents.bind_deposit_address", side_effect=AssertionError("bound")), \
                mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(errors):
            code = gate_main.main(["intents", "--origin", "nep141:wrap.near", "--destination", USDC_BASE_ASSET,
                                   "--amount", ONE_NEAR, "--recipient", DEMO_RECIPIENT,
                                   "--deposit-address", "other.near"])
        self.assertEqual(code, 1)
        self.assertIn("--refund-to", errors.getvalue())
        self.assertNotIn("verdict:", output.getvalue())


class BindingTest(Case):
    def recorded_request(self):
        """The quoteRequest the recorded 1Click quote echoes, with the app fee 1Click added to it."""
        return json.loads(RECORDED_QUOTE.read_text(encoding="utf-8"))["response"]["quoteRequest"]

    def later_request(self, **changes):
        """The quoteRequest of the quote an agent would deposit to after the verdict: the recorded one,
        not dry and without the app fee 1Click added to its echo, which no judged request names."""
        request = dict(self.recorded_request(), dry=False, appFees=None)
        request.update(changes)
        return {key: value for key, value in request.items() if value is not None}

    def allowed_request(self, **changes):
        """later_request for the answer allowed() judges: its recipient and DEADLINE, then changes."""
        return self.later_request(**{"recipient": USDC_BASE, "deadline": DEADLINE, **changes})

    def allowed(self, **options):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE, **options)
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL, **options)
        self.assertEqual(result["verdict"], "ALLOW")
        return result

    def test_the_document_records_the_quote_identity(self):
        result = judge()
        self.assertEqual(result["document"]["subject"]["quote"],
                         {"correlation_id": "b1895363-f91c-4b19-add2-29a3cdab9ca0",
                          "timestamp": "2026-10-29T11:48:38.554Z", "amount_out": "4398699",
                          "min_amount_out": "4354712"})
        self.assertNotIn("depositAddress", result["quote"])
        self.assertNotIn("deposit_address", result["document"]["subject"])
        self.assertEqual(result["document"]["subject"]["quote_request"],
                         intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, DEMO_RECIPIENT,
                                            "DESTINATION_CHAIN", intents.RECORDED_DEADLINE))
        self.assertIsNone(judge(fixtures_root=self.root("goplus", "evm"))["document"]["subject"]["quote"])

    def test_a_matching_request_is_bound(self):
        verify(judge(), self.later_request())
        verify(self.allowed(), self.allowed_request(recipient=USDC_BASE.upper().replace("0X", "0x")))

    def test_another_or_a_missing_field_is_refused(self):
        answer = self.allowed()
        for changes in ({"recipient": DEMO_RECIPIENT}, {"recipient": None}, {"amount": "2" + ONE_NEAR[1:]},
                        {"destinationAsset": "nep141:eth.omft.near"}, {"originAsset": None},
                        {"recipientType": "INTENTS"}, {"refundTo": PAYER}, {"refundTo": None},
                        {"refundType": "INTENTS"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verify(answer, self.allowed_request(**{**changes}))
        with self.assertRaises(ValueError):
            verify(answer, None)

    def test_every_other_field_the_request_carries_must_be_the_judged_one(self):
        answer = self.allowed()
        mismatches = (("appFees", [{"recipient": "thief.near", "fee": 5000}]), ("slippageTolerance", 9999),
                      ("slippageTolerance", "50"), ("slippageTolerance", True), ("swapType", "EXACT_OUTPUT"),
                      ("depositType", "INTENTS"),
                      ("virtualChainRecipient", "0x0000000000000000000000000000000000000002"),
                      ("customRecipientMsg", "x"), ("insured", True), ("depositMode", "MEMO"), ("unlistedKey", "x"))
        for field, value in mismatches:
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                verify(answer, self.allowed_request(**{field: value}))
        with self.assertRaisesRegex(ValueError, "appFees"):
            verify(answer, dict(self.recorded_request(), recipient=USDC_BASE, deadline=DEADLINE))
        verify(answer, self.allowed_request(slippageTolerance=50, appFees=[]))
        with self.assertRaisesRegex(ValueError, "swapType"):
            verify(answer, {field: self.allowed_request()[field]
                                            for field in ("originAsset", "destinationAsset", "amount", "recipient",
                                                          "recipientType", "refundTo", "refundType")})

    def test_a_caller_argument_the_schema_does_not_allow_is_refused_before_any_request(self):
        for name, value, field in (("origin_asset", False, "originAsset"), ("origin_asset", 0, "originAsset"),
                                   ("origin_asset", [], "originAsset"), ("origin_asset", {}, "originAsset"),
                                   ("origin_asset", None, "originAsset"), ("destination_asset", 5, "destinationAsset"),
                                   ("destination_asset", None, "destinationAsset"),
                                   ("recipient_type", "", "recipientType"), ("recipient_type", False, "recipientType"),
                                   ("recipient_type", None, "recipientType"),
                                   ("recipient_type", "destination_chain", "recipientType")):
            arguments = {"origin_asset": "nep141:wrap.near", "destination_asset": USDC_BASE_ASSET, "amount": ONE_NEAR,
                         "recipient": USDC_BASE, name: value}
            with self.subTest(**{name: value}), self.assertRaisesRegex(ValueError, f"^{field} "):
                intents.judge_quote(**arguments, mode="off", deadline=DEADLINE)
        self.urlopen.assert_not_called()

    def test_an_echo_field_the_schema_does_not_allow_is_never_allow(self):
        cases = (("connectedWallets", False, "unknown"), ("connectedWallets", [1], "unknown"),
                 ("connectedWallets", None, "unknown"), ("sessionId", {}, "unknown"), ("sessionId", None, "unknown"),
                 ("quoteWaitingTimeMs", "banana", "unknown"), ("quoteWaitingTimeMs", True, "unknown"),
                 ("quoteWaitingTimeMs", float("nan"), "unknown"), ("quoteWaitingTimeMs", float("inf"), "unknown"),
                 ("referral", 5, "unknown"), ("virtualChainRecipient", [], "unknown"),
                 ("virtualChainRecipient", None, "unknown"), ("virtualChainRefundRecipient", [], "unknown"),
                 ("customRecipientMsg", None, "unknown"), ("rebates", "", "unknown"), ("rebates", None, "unknown"),
                 ("insured", None, "signal"), ("originAsset", False, "signal"), ("originAsset", 0, "signal"),
                 ("originAsset", [], "signal"), ("originAsset", {}, "signal"), ("amount", "-5", "signal"),
                 ("amount", 1000, "signal"), ("refundType", "banana", "signal"), ("refundType", None, "signal"))
        for field, value, status in cases:
            with self.subTest(field=field, value=value):
                root = self.root("evm", "goplus")
                self.quote_for(root, USDC_BASE, echo={field: value})
                self.firewall_says(root, "SAFE")
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
                self.assertEqual(statuses(result)["quote_dry"], status)
                self.assertEqual(result["verdict"], "UNKNOWN" if status == "unknown" else "DENY")
                self.assertIn(field, detail(result, "quote_dry"))
        for echo in ({"connectedWallets": [], "sessionId": "", "referral": "", "quoteWaitingTimeMs": 0,
                      "virtualChainRecipient": "", "rebates": []},
                     {"connectedWallets": ["a"], "sessionId": "s", "referral": "wallet", "quoteWaitingTimeMs": 3000.5},
                     {"quoteWaitingTimeMs": 10 ** 400}):
            with self.subTest(echo=echo):
                root = self.root("evm", "goplus")
                self.quote_for(root, USDC_BASE, echo=echo)
                self.firewall_says(root, "SAFE")
                self.assertEqual(judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)["verdict"], "ALLOW")

    def test_an_answer_field_the_schema_does_not_allow_is_unknown(self):
        body = intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE, "DESTINATION_CHAIN",
                                  DEADLINE)
        for field, value in (("minAmountOut", None), ("minAmountOut", "absent"), ("amountOut", "absent"),
                             ("correlationId", None), ("correlationId", "absent"), ("timestamp", None),
                             ("timestamp", "absent")):
            with self.subTest(field=field, value=value):
                root = self.root("evm", "goplus")
                self.firewall_says(root, "SAFE")
                response = {"quote": {"amountOut": "4398699", "minAmountOut": "4354712"},
                            "quoteRequest": dict(body), "correlationId": "c", "timestamp": "2026-11-03T11:59:59Z"}
                holder = response["quote"] if field in ("amountOut", "minAmountOut") else response
                if value == "absent":
                    del holder[field]
                else:
                    holder[field] = value
                self.write(root, "intents", intents.ONECLICK_URL + "/v0/quote", "POST", rpc.body_params(body),
                           response)
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
                self.assertEqual(statuses(result)["quote_dry"], "unknown")
                self.assertIn(field, detail(result, "quote_dry"))
                self.assertEqual(result["verdict"], "UNKNOWN")
                verify(result, self.allowed_request())

    def test_a_later_request_field_the_schema_does_not_allow_is_refused(self):
        answer = self.allowed()
        for field, value in (("connectedWallets", False), ("connectedWallets", [1]), ("connectedWallets", None),
                             ("sessionId", {}), ("sessionId", None), ("quoteWaitingTimeMs", "banana"),
                             ("quoteWaitingTimeMs", True), ("referral", 5), ("virtualChainRecipient", []),
                             ("virtualChainRecipient", None), ("rebates", ""), ("rebates", None),
                             ("customRecipientMsg", None), ("insured", None), ("dry", None), ("dry", "false"),
                             ("originAsset", False), ("originAsset", 0), ("originAsset", []), ("originAsset", {}),
                             ("amount", "-5"), ("refundType", "banana"), ("appFees", None)):
            request = dict(self.allowed_request(), **{field: value})
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                verify(answer, request)
        verify(answer, self.allowed_request(connectedWallets=[], sessionId="", referral="", quoteWaitingTimeMs=0,
                                            virtualChainRecipient="", rebates=[]))

    def test_a_later_request_must_carry_every_judged_field_and_the_judged_deadline(self):
        answer = self.allowed()
        for field, value in (("deadline", "garbage"), ("deadline", "2026-10-29T00:00:00Z"), ("deadline", 5),
                             ("deadline", "2026-11-03T12:10:01.000Z"), ("deadline", None), ("swapType", None),
                             ("depositType", None), ("slippageTolerance", None)):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                verify(answer, self.allowed_request(**{field: value}))
        verify(answer, self.allowed_request(deadline=DEADLINE))
        verify(answer, self.allowed_request(deadline="2026-11-03T13:10:00+01:00"))

    def test_a_stale_quote_is_unknown_with_its_age(self):
        root = self.root("evm", "goplus")
        stale_timestamp = datetime.fromtimestamp(CLOCK - (intents.MAX_QUOTE_AGE_MS + 1) / 1000, UTC).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        self.quote_for(root, DEMO_RECIPIENT, timestamp=stale_timestamp)
        self.goplus_says(root, DEMO_RECIPIENT, {"code": 1, "result": {"data_source": "synthetic"}})
        result = judge(fixtures_root=root)
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertEqual(statuses(result)["quote_age"], "unknown")
        self.assertIn(f"quote age is {intents.MAX_QUOTE_AGE_MS + 1} ms", detail(result, "quote_age"))
        self.assertIn("quote_age could not run", result["reasons"][0])
        verify(result, self.later_request(deadline=DEADLINE))
        with self.assertRaisesRegex(ValueError, "amount"):
            verify(result, self.later_request(amount="2", deadline=DEADLINE))

    def test_a_dry_quote_without_a_deposit_address_still_passes(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE)
        self.goplus_says(root, USDC_BASE, {"code": 1, "result": {"data_source": "synthetic"}})
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(result["verdict"], "ALLOW")
        self.assertEqual(statuses(result)["quote_dry"], "pass")
        self.assertNotIn("depositAddress", result["quote"])

    def test_a_tampered_answer_is_refused(self):
        denied, request = judge(), self.later_request()
        relabelled = dict(denied, verdict="ALLOW")
        moved = json.loads(json.dumps(denied))
        moved["document"]["subject"]["recipient"] = USDC_BASE
        rehashed = dict(denied, evidence_hash="0" * 64)
        for tampered in (relabelled, moved, rehashed, {k: v for k, v in denied.items() if k != "document"}, None):
            with self.subTest(tampered=None if tampered is None else tampered.get("evidence_hash")), \
                    self.assertRaises(ValueError):
                verify(tampered, request)

    def test_a_document_holding_a_float_is_refused_as_a_hash_mismatch(self):
        denied, request = judge(), self.later_request()
        forged = json.loads(json.dumps(denied))
        forged["document"]["subject"]["quote"]["min_amount_out"] = 1.5
        with self.assertRaisesRegex(ValueError, "the evidence hash does not match the answer's document"):
            verify(forged, request)

    def test_a_rehashed_document_whose_subject_is_not_intents_is_refused(self):
        denied, request = judge(), self.later_request()
        forged = json.loads(json.dumps(denied))
        forged["document"]["subject"]["kind"] = "near"
        forged["evidence_hash"] = evidence.evidence_hash(forged["document"])
        with self.assertRaisesRegex(ValueError, "not an intents verdict"):
            verify(forged, request)

    def test_an_echo_that_differs_from_the_request_is_a_signal(self):
        root = self.root("evm", "goplus")
        body = intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE, "DESTINATION_CHAIN",
                                  DEADLINE)
        self.write(root, "intents", intents.ONECLICK_URL + "/v0/quote", "POST", rpc.body_params(body),
                   {"quote": {"amountOut": "4398699", "minAmountOut": "4354712"},
                    "quoteRequest": dict(body, amount="1"), "timestamp": "2026-11-03T11:59:59Z", "correlationId": "c"})
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(statuses(result)["quote_dry"], "signal")
        self.assertEqual(result["verdict"], "DENY")
        self.assertIn(f'amount "1", not "{ONE_NEAR}"', detail(result, "quote_dry"))

    def test_each_sent_field_the_echo_changes_is_a_signal(self):
        for field, value in (("refundTo", "other.near"), ("refundType", "INTENTS"), ("swapType", "EXACT_OUTPUT"),
                             ("slippageTolerance", 500), ("depositType", "INTENTS"),
                             ("deadline", "2026-10-31T00:00:00.000Z"), ("deadline", "not a date"),
                             ("deadline", None)):
            with self.subTest(field=field, value=value):
                root = self.root("evm", "goplus")
                self.quote_for(root, USDC_BASE, echo={field: value})
                self.firewall_says(root, "SAFE")
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
                self.assertEqual(statuses(result)["quote_dry"], "signal")
                self.assertEqual(result["verdict"], "DENY")
                self.assertIn(field, detail(result, "quote_dry"))

    def test_the_deadline_echo_with_milliseconds_is_the_same_instant(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE, echo={"deadline": "2026-11-03T12:10:00.000Z"})
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(statuses(result)["quote_dry"], "pass")
        self.assertEqual(result["verdict"], "ALLOW")

    def test_a_named_refund_account_and_app_fees_are_the_judged_request(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE, refund_to=PAYER, refund_type="INTENTS", app_fees=NAMED_FEES)
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL, refund_to=PAYER,
                       refund_type="INTENTS", app_fees=NAMED_FEES)
        self.assertEqual(result["verdict"], "ALLOW")
        judged = result["document"]["subject"]["quote_request"]
        self.assertEqual((judged["refundTo"], judged["refundType"], judged["appFees"]), (PAYER, "INTENTS", NAMED_FEES))
        self.assertNotIn("appFees", judge()["document"]["subject"]["quote_request"])

    def test_the_echo_must_carry_each_named_app_fee_and_may_add_only_the_recorded_service_fee(self):
        service = SERVICE_FEE
        foreign = {"recipient": "thief.near", "fee": 9000}
        cases = ((NAMED_FEES, [service] + NAMED_FEES, "pass"), (NAMED_FEES, NAMED_FEES + [service], "pass"),
                 (NAMED_FEES, NAMED_FEES, "pass"), (None, [service], "pass"), (None, None, "signal"),
                 (NAMED_FEES, [service], "signal"), (NAMED_FEES, None, "signal"),
                 (NAMED_FEES, NAMED_FEES + [foreign], "signal"), (None, [foreign], "signal"),
                 (None, [service, foreign], "signal"), (None, [service, service], "signal"),
                 (None, [dict(service, fee=21)], "signal"), (None, [dict(service, fee=20.0)], "signal"),
                 (None, [dict(service, fee=True)], "signal"),
                 (None, [dict(service, recipient="service.near")], "signal"),
                 (None, [dict(service, extra=1)], "signal"), (None, "none", "signal"), (None, {}, "signal"))
        for named, echoed, status in cases:
            with self.subTest(named=named, echoed=echoed):
                root = self.root("evm", "goplus")
                self.quote_for(root, USDC_BASE, app_fees=named, echo={"appFees": echoed})
                self.firewall_says(root, "SAFE")
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL, app_fees=named)
                self.assertEqual(statuses(result)["quote_dry"], status)
                self.assertEqual(result["verdict"], "ALLOW" if status == "pass" else "DENY")
                if status == "signal":
                    self.assertIn("appFees", detail(result, "quote_dry"))
                elif isinstance(echoed, list) and service in echoed:
                    self.assertIn(f"the 1Click service fee {intents._json(service)}", detail(result, "quote_dry"))
                else:
                    self.assertIn("no 1Click service fee", detail(result, "quote_dry"))

    def test_an_unknown_key_or_an_unjudged_routing_field_in_the_echo_is_a_signal(self):
        for field, value in (("unlistedKey", "x"),
                             ("virtualChainRecipient", "0x0000000000000000000000000000000000000002"),
                             ("virtualChainRefundRecipient", "0x0000000000000000000000000000000000000002"),
                             ("customRecipientMsg", "x"), ("rebates", [{"recipient": "r.near", "share": 100}]),
                             ("insured", True), ("depositMode", "MEMO"), ("confidentiality", "basic")):
            with self.subTest(field=field):
                root = self.root("evm", "goplus")
                self.quote_for(root, USDC_BASE, echo={field: value})
                self.firewall_says(root, "SAFE")
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
                self.assertEqual(statuses(result)["quote_dry"], "signal")
                self.assertEqual(result["verdict"], "DENY")
                self.assertIn(field, detail(result, "quote_dry"))
                self.assertIsNotNone(result["quote"])

    def test_the_service_fee_is_the_one_the_recorded_echo_adds(self):
        named_none = intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, DEMO_RECIPIENT,
                                        "DESTINATION_CHAIN", intents.RECORDED_DEADLINE)
        self.assertNotIn("appFees", named_none)
        self.assertEqual(RECORDED_QUOTE.stem, rpc.fixture_key(intents.ONECLICK_URL + "/v0/quote", "POST",
                                                              rpc.body_params(named_none)))
        self.assertTrue(intents._whole_fee(SERVICE_FEE))
        data = json.loads(intents.SERVICE_FEE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["service_fee"], SERVICE_FEE)
        self.assertIn(f"fixtures/intents/{RECORDED_QUOTE.name}", data["source"])
        self.assertEqual(intents._service_fee(), (SERVICE_FEE, None))
        self.assertIn(f"the 1Click service fee {intents._json(SERVICE_FEE)}", detail(judge(), "quote_dry"))

    def test_without_a_readable_service_fee_file_one_unnamed_fee_is_unknown_not_a_signal(self):
        foreign = {"recipient": "thief.near", "fee": 9000}
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE, echo={"appFees": [foreign]})
        self.firewall_says(root, "SAFE")
        present = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual((statuses(present)["quote_dry"], present["verdict"]), ("signal", "DENY"))
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "service-fee.json"
            for path, content in ((Path(tmp) / "missing.json", None), (broken, "{"), (broken, '{"other": 1}'),
                                  (broken, '{"service_fee": {"recipient": "x.near", "fee": 20.0}}'),
                                  (broken, '{"service_fee": {"recipient": "", "fee": 20}}'),
                                  (broken, '{"service_fee": [20]}')):
                if content is not None:
                    broken.write_text(content, encoding="utf-8")
                with self.subTest(content=content), mock.patch.object(intents, "SERVICE_FEE_FILE", path):
                    fee, reason = intents._service_fee()
                    self.assertIsNone(fee)
                    self.assertTrue(reason)
                    recorded = judge()
                    self.assertEqual(statuses(recorded)["quote_dry"], "unknown")
                    self.assertIn(intents._json(SERVICE_FEE), detail(recorded, "quote_dry"))
                    self.assertIn("could not be read: ", detail(recorded, "quote_dry"))
                    for named, echoed in ((None, [SERVICE_FEE]), (None, [foreign]),
                                          (NAMED_FEES, NAMED_FEES + [SERVICE_FEE])):
                        root = self.root("evm", "goplus")
                        self.quote_for(root, USDC_BASE, app_fees=named, echo={"appFees": echoed})
                        self.firewall_says(root, "SAFE")
                        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL, app_fees=named)
                        self.assertEqual(statuses(result)["quote_dry"], "unknown")
                        self.assertEqual(result["verdict"], "UNKNOWN")
                        self.assertIn(f"quote_dry could not run: the dry quote echo adds the unnamed app fee "
                                      f"{intents._json(echoed[-1])}", " ".join(result["reasons"]))
                    for named, echo in ((None, {"appFees": [SERVICE_FEE, foreign]}),
                                        (NAMED_FEES, {"appFees": [SERVICE_FEE]}), (None, {"appFees": {}}),
                                        (None, {"appFees": [SERVICE_FEE], "insured": True}), (None, {"appFees": [{}]}),
                                        (None, {"appFees": ["x"]}), (None, {"appFees": [dict(SERVICE_FEE, fee=20.0)]}),
                                        (None, {"appFees": [dict(SERVICE_FEE, extra=1)]})):
                        root = self.root("evm", "goplus")
                        self.quote_for(root, USDC_BASE, app_fees=named, echo=echo)
                        self.firewall_says(root, "SAFE")
                        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL, app_fees=named)
                        self.assertEqual(statuses(result)["quote_dry"], "signal")
                        self.assertEqual(result["verdict"], "DENY")
                    root = self.root("evm", "goplus")
                    self.quote_for(root, USDC_BASE, app_fees=NAMED_FEES)
                    self.firewall_says(root, "SAFE")
                    named = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL, app_fees=NAMED_FEES)
                    self.assertEqual(named["verdict"], "ALLOW")
                    self.assertIn("no 1Click service fee", detail(named, "quote_dry"))

    def test_a_judged_deadline_not_after_the_judge_clock_is_never_allow(self):
        for deadline in (intents.RECORDED_DEADLINE, "2026-11-03T12:00:00Z", "2026-11-03T11:59:59.999Z"):
            with self.subTest(deadline=deadline):
                root = self.root("evm", "goplus")
                self.quote_for(root, USDC_BASE, timestamp="2026-11-03T11:59:59Z", deadline=deadline)
                self.firewall_says(root, "SAFE")
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL, deadline=deadline)
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(statuses(result)["quote_dry"], "pass")
                self.assertEqual(statuses(result)["quote_age"], "pass")
                self.assertEqual(statuses(result)["quote_deadline"], "unknown")
                self.assertIn(f'the judged deadline "{deadline}" is not after 2026-11-03T12:00:00Z',
                              detail(result, "quote_deadline"))
                self.assertIn("quote_deadline could not run", result["reasons"][0])
        for deadline in ("2026-11-04", "2026-11-04T00:00:00", "garbage", None):
            with self.subTest(deadline=deadline), self.assertRaisesRegex(
                    ValueError, "^deadline: not a complete date-time with an explicit zone"):
                judge(USDC_BASE, mode="off", deadline=deadline)
        self.urlopen.assert_not_called()
        allowed = self.allowed()
        self.assertEqual(statuses(allowed)["quote_deadline"], "pass")
        self.assertIn(f'the judged deadline "{DEADLINE}" is after 2026-11-03T12:00:00Z',
                      detail(allowed, "quote_deadline"))

    def test_a_deadline_passing_during_the_recipient_reads_is_never_allow(self):
        deadline = "2026-11-03T12:00:01Z"
        for read in ("goplus_address", "eth_get_code", "firewall"):
            with self.subTest(read=read):
                root = self.root("evm")
                self.quote_for(root, USDC_BASE, deadline=deadline)
                self.goplus_says(root, USDC_BASE, {"code": 1, "result": {"data_source": "synthetic"}})
                self.firewall_says(root, "SAFE")
                now = [CLOCK]
                original = getattr(intents, read)

                def advancing(*args, original=original, **kwargs):
                    now[0] += 2
                    return original(*args, **kwargs)

                with mock.patch.object(intents, read, side_effect=advancing):
                    result = intents.judge_quote("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE,
                                                 mode="replay", fixtures_root=root, firewall_url=FIREWALL_URL,
                                                 clock=lambda: now[0], deadline=deadline)
                self.assertEqual(now[0], CLOCK + 2)
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(statuses(result)["quote_deadline"], "unknown")
                self.assertIn(f'the judged deadline "{deadline}" is not after 2026-11-03T12:00:02Z',
                              detail(result, "quote_deadline"))
                self.assertEqual(result["document"]["observed"]["timestamp_ms"], int((CLOCK + 2) * 1000))
                self.assertEqual([check["id"] for check in result["checks"]].index("quote_deadline"), 2)

    def test_a_quote_aging_past_the_bound_during_the_recipient_reads_is_never_allow(self):
        timestamp = "2026-11-03T11:55:01Z"
        for read in ("goplus_address", "eth_get_code", "firewall"):
            with self.subTest(read=read):
                root = self.root("evm")
                self.quote_for(root, USDC_BASE, timestamp=timestamp)
                self.goplus_says(root, USDC_BASE, {"code": 1, "result": {"data_source": "synthetic"}})
                self.firewall_says(root, "SAFE")
                now = [CLOCK]
                original = getattr(intents, read)

                def advancing(*args, original=original, **kwargs):
                    now[0] += 2
                    return original(*args, **kwargs)

                with mock.patch.object(intents, read, side_effect=advancing):
                    result = intents.judge_quote("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE,
                                                 mode="replay", fixtures_root=root, firewall_url=FIREWALL_URL,
                                                 clock=lambda: now[0], deadline=DEADLINE)
                self.assertEqual(now[0], CLOCK + 2)
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(statuses(result)["quote_age"], "unknown")
                self.assertEqual(statuses(result)["quote_deadline"], "pass")
                self.assertIn(f"quote age is 301000 ms, over {intents.MAX_QUOTE_AGE_MS} ms",
                              detail(result, "quote_age"))
                self.assertEqual([check["id"] for check in result["checks"]][:3],
                                 ["quote_dry", "quote_age", "quote_deadline"])

    def test_verify_binding_refuses_an_allow_whose_deadline_has_passed(self):
        deadline = "2026-11-03T12:04:00Z"
        answer, request = self.allowed(deadline=deadline), self.allowed_request(deadline=deadline)
        verify(answer, request, clock=CLOCK + 239)
        for later in (CLOCK + 240, CLOCK + 600, CLOCK + 86400):
            with self.subTest(later=later), self.assertRaisesRegex(
                    ValueError, f'^the judged deadline "{deadline}" is not after '):
                verify(answer, request, clock=later)
        verify(judge(), self.later_request(), clock=CLOCK + 86400 * 365)

    def test_verify_binding_refuses_an_allow_whose_quote_is_too_old(self):
        answer, request = self.allowed(), self.allowed_request()
        verify(answer, request, clock=CLOCK + 299)
        with self.assertRaisesRegex(ValueError, f"^quote age is 301000 ms, over {intents.MAX_QUOTE_AGE_MS} ms"):
            verify(answer, request, clock=CLOCK + 300)
        for quote in (None, {"timestamp": None}, {"timestamp": "2026-11-03T11:59:59"}, ["x"]):
            forged = json.loads(json.dumps(answer))
            forged["document"]["subject"]["quote"] = quote
            forged["evidence_hash"] = evidence.evidence_hash(forged["document"])
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, "^quote age is unknown"):
                verify(forged, request)

    def test_a_quote_a_fraction_of_a_millisecond_past_the_bound_is_never_allow(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE, timestamp="2026-11-03T11:55:00.000Z")
        self.firewall_says(root, "SAFE")
        options = {"mode": "replay", "fixtures_root": root, "firewall_url": FIREWALL_URL, "deadline": DEADLINE}
        answer = intents.judge_quote("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE, clock=lambda: CLOCK,
                                     **options)
        self.assertEqual(answer["verdict"], "ALLOW")
        self.assertIn(f"quote age is 300000 ms, within {intents.MAX_QUOTE_AGE_MS} ms", detail(answer, "quote_age"))
        late = intents.judge_quote("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE,
                                   clock=lambda: CLOCK + 0.0009, **options)
        self.assertEqual(late["verdict"], "UNKNOWN")
        self.assertEqual(statuses(late)["quote_age"], "unknown")
        self.assertIn(f"quote age is 300001 ms, over {intents.MAX_QUOTE_AGE_MS} ms", detail(late, "quote_age"))
        request = self.allowed_request()
        verify(answer, request)
        with self.assertRaisesRegex(ValueError, f"^quote age is 300001 ms, over {intents.MAX_QUOTE_AGE_MS} ms"):
            verify(answer, request, clock=CLOCK + 0.0009)

    def test_a_quote_timestamp_a_fraction_of_a_millisecond_outside_the_bounds_is_unknown(self):
        for timestamp, expected in (("2026-11-03T12:00:00.0004Z", "quote age is -1 ms because timestamp is in the future"),
                                    ("2026-11-03T11:54:59.9999Z",
                                     f"quote age is 300001 ms, over {intents.MAX_QUOTE_AGE_MS} ms")):
            with self.subTest(timestamp=timestamp):
                root = self.root("evm", "goplus")
                self.quote_for(root, USDC_BASE, timestamp=timestamp)
                self.firewall_says(root, "SAFE")
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(statuses(result)["quote_age"], "unknown")
                self.assertIn(expected, detail(result, "quote_age"))

    def test_a_deadline_without_a_complete_time_and_zone_is_refused(self):
        answer = self.allowed(deadline="2026-11-04T00:00:00Z")
        for deadline in ("2026-11-04", "2026-11-04T00:00:00", "2026-11-04 00:00:00Z", "20261104T000000Z",
                         "2026-11-04T00Z", "2026-11-04T00:00:00z"):
            with self.subTest(deadline=deadline), self.assertRaisesRegex(ValueError, "^deadline"):
                verify(answer, self.allowed_request(deadline=deadline))
        verify(answer, self.allowed_request(deadline="2026-11-04T00:00:00.000Z"))
        verify(answer, self.allowed_request(deadline="2026-11-04T01:00:00+01:00"))
        for text in ("2026-11-04", "2026-11-04T00:00:00", "2026-11-04T00:00", "2026-W45-3T00:00:00Z", None, 5):
            with self.subTest(text=text):
                self.assertIsNone(intents._instant(text))

    def test_a_date_time_component_out_of_range_is_refused_never_normalised(self):
        out_of_range = ("2026-11-03T14:10:00+01:60", "2026-11-03T12:10:00-00:60", "2026-11-03T12:10:00+24:00",
                        "2026-11-03T12:10:00+1:00", "2026-11-03T24:10:00Z", "2026-11-03T12:60:00Z",
                        "2026-11-03T12:10:60Z", "2026-13-03T12:10:00Z", "2026-11-32T12:10:00Z",
                        "2026-02-30T12:10:00Z", "2026-00-03T12:10:00Z", "2026-11-00T12:10:00Z")
        answer = self.allowed()
        for text in out_of_range:
            with self.subTest(text=text):
                self.assertIsNone(intents._instant(text))
                with self.assertRaisesRegex(ValueError, "^deadline: not a complete date-time with an explicit zone"):
                    judge(USDC_BASE, mode="off", deadline=text)
                with self.assertRaisesRegex(ValueError, "^deadline"):
                    verify(answer, self.allowed_request(deadline=text))
                with self.assertRaisesRegex(ValueError, "^deadline"):
                    verify(rehashed(answer, deadline=text), self.allowed_request())
        self.urlopen.assert_not_called()
        for text in ("2026-11-04T12:09:00+23:59", "2026-11-03T00:11:00-11:59", "2026-11-03T13:10:00+01:00"):
            with self.subTest(text=text):
                verify(answer, self.allowed_request(deadline=text))

    def test_a_quote_timestamp_without_a_zone_is_unknown(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE, timestamp="2026-11-03T11:59:59")
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(statuses(result)["quote_age"], "unknown")
        self.assertIn("timestamp is invalid", detail(result, "quote_age"))
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_a_document_without_a_judged_quote_request_predates_request_binding(self):
        for answer, request in ((judge(), self.later_request()), (self.allowed(), self.allowed_request())):
            old = json.loads(json.dumps(answer))
            del old["document"]["subject"]["quote_request"]
            old["evidence_hash"] = evidence.evidence_hash(old["document"])
            with self.subTest(verdict=answer["verdict"]), self.assertRaisesRegex(
                    ValueError, "^the evidence document predates request binding"):
                verify(old, request)

    def test_a_forged_judged_slippage_is_refused_by_name(self):
        answer = self.allowed()
        for value in ("100", None, True, [100]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^the judged slippageTolerance"):
                verify(rehashed(answer, slippageTolerance=value), self.allowed_request())

    def test_a_malformed_refund_account_or_app_fee_is_refused_before_any_request(self):
        for options in ({"refund_to": ""}, {"refund_to": None}, {"refund_type": 1}, {"app_fees": {}},
                        {"app_fees": [{"recipient": "fees.near"}]}, {"app_fees": [{"recipient": "", "fee": 1}]},
                        {"app_fees": [{"recipient": "fees.near", "fee": True}]},
                        {"app_fees": [{"recipient": "fees.near", "fee": -1}]},
                        {"app_fees": [{"recipient": "fees.near", "fee": 1, "extra": 1}]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                judge(USDC_BASE, mode="off", **options)
        self.urlopen.assert_not_called()

    def test_a_malformed_amount_or_refund_type_is_refused_before_any_request(self):
        for amount in ("", "-5", " 1", "1e6", "abc", "0", "01", "1.5", [], True, 1000, None):
            with self.subTest(amount=amount), self.assertRaisesRegex(ValueError, "^amount: "):
                intents.judge_quote("nep141:wrap.near", USDC_BASE_ASSET, amount, USDC_BASE, mode="off",
                                    deadline=DEADLINE)
        for refund_type in ("banana", "origin_chain", " ", "ORIGIN_CHAIN "):
            with self.subTest(refund_type=refund_type), self.assertRaisesRegex(
                    ValueError, "^refund_type: not ORIGIN_CHAIN, INTENTS or CONFIDENTIAL_INTENTS"):
                judge(USDC_BASE, mode="off", refund_type=refund_type)
        self.urlopen.assert_not_called()
        for refund_type in intents.REFUND_TYPES:
            with self.subTest(refund_type=refund_type):
                verify(self.allowed(refund_type=refund_type), self.allowed_request(refundType=refund_type))

    def test_verify_binding_refuses_a_judged_malformed_amount_or_refund_type(self):
        answer = self.allowed()
        for field, value in (("amount", "-5"), ("amount", "0"), ("amount", " 1"), ("amount", 1000),
                             ("amount", True), ("refundType", "banana"), ("refundType", "origin_chain")):
            with self.subTest(field=field, value=value):
                forged = json.loads(json.dumps(answer))
                forged["document"]["subject"]["quote_request"][field] = value
                if field == "amount":
                    forged["document"]["subject"]["amount"] = value
                forged["evidence_hash"] = evidence.evidence_hash(forged["document"])
                with self.assertRaisesRegex(ValueError, "^(amount|refund_type): "):
                    verify(forged, self.allowed_request(**{field: value}))

    def test_a_bool_never_equals_a_number_in_the_binding(self):
        answer = self.allowed()
        with self.assertRaisesRegex(ValueError, "^swapType true is not the judged 1"):
            verify(rehashed(answer, swapType=1), self.allowed_request(swapType=True))
        self.assertFalse(intents._same("slippageTolerance", 1, True))
        self.assertTrue(intents._same("slippageTolerance", 1, 1))


class CallerTest(Case):
    """The CLI and the MCP tool verify the request they judged, the named app fees included."""

    def callers_over(self, status_fees, deadline=DEADLINE, verified_at=CLOCK, timestamp="2026-11-03T11:59:59Z",
                     real=(), lifecycle=WAITING):
        """Fixtures for a clean quote stamped timestamp naming NAMED_FEES and refunding to PAYER, and a
        1Click status lifecycle for DEPOSIT_ADDRESS whose quote request carries status_fees, with real
        applied to its quoteResponse; both callers then replay them at CLOCK with deadline as the
        replayed deadline and verify the answer at verified_at."""
        root = self.fixtures = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE, refund_to=PAYER, app_fees=NAMED_FEES, deadline=deadline, timestamp=timestamp)
        self.goplus_says(root, USDC_BASE, {"code": 1, "result": {"data_source": "synthetic"}})
        self.firewall_says(root, "SAFE")
        request = intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE, "DESTINATION_CHAIN",
                                     STATUS_DEADLINE, refund_to=PAYER, app_fees=status_fees)
        self.status_for(root, DEPOSIT_ADDRESS, dict(request, dry=False), lifecycle, **dict(real))
        judge_quote, bind, verify_binding = intents.judge_quote, intents.bind_deposit_address, intents.verify_binding
        replayed = {"fixtures_root": root, "clock": lambda: CLOCK}
        for patcher in (mock.patch("gate.intents.judge_quote",
                                   side_effect=lambda *a, **k: judge_quote(*a, **k, **replayed)),
                        mock.patch("gate.intents.bind_deposit_address",
                                   side_effect=lambda *a, **k: bind(*a, **k, **replayed)),
                        mock.patch("gate.intents.verify_binding",
                                   side_effect=lambda *a, **k: verify_binding(*a, **{"clock": lambda: verified_at,
                                                                                    **k})),
                        mock.patch("gate.intents.RECORDED_DEADLINE", deadline),
                        mock.patch.dict("os.environ", {"SHIELDBOT_API_URL": FIREWALL_URL}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def command(self, *extra):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = gate_main.main(["intents", "--origin", "nep141:wrap.near", "--destination", USDC_BASE_ASSET,
                                   "--amount", ONE_NEAR, "--recipient", USDC_BASE, "--refund-to", PAYER,
                                   "--app-fees", json.dumps(NAMED_FEES), *extra])
        return code, output.getvalue().splitlines(), errors.getvalue()

    def tool(self, **extra):
        arguments = {"origin_asset": "nep141:wrap.near", "destination_asset": USDC_BASE_ASSET, "amount": ONE_NEAR,
                     "recipient": USDC_BASE, "refund_to": PAYER, "app_fees": NAMED_FEES, **extra}
        return mcp_server._check_intents_quote(arguments, None)

    def test_a_named_app_fee_is_allowed_by_both_callers(self):
        self.callers_over(NAMED_FEES)
        code, lines, errors = self.command()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("verdict: ALLOW (unsigned preflight)", lines)
        self.assertEqual(self.tool()["verdict"], "ALLOW")
        code, lines, errors = self.command("--deposit-address", DEPOSIT_ADDRESS)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("verdict: ALLOW (unsigned preflight)", lines)
        answer = self.tool(deposit_address=DEPOSIT_ADDRESS)
        self.assertEqual(answer["verdict"], "ALLOW")
        self.assertEqual(statuses(answer)["deposit_address_status"], "pass")

    def real_quote_case(self, expected, real, lifecycle=WAITING):
        """Both callers at CLOCK, with a fresh dry quote and the judged deadline DEADLINE, bind a deposit
        address whose status answer has status lifecycle and a quoteResponse carrying real: UNKNOWN with
        expected in its status detail, never ALLOW."""
        self.callers_over(NAMED_FEES, real=real, lifecycle=lifecycle)
        code, lines, errors = self.command("--deposit-address", DEPOSIT_ADDRESS)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("verdict: UNKNOWN (unsigned preflight)", lines)
        self.assertTrue(any(line.startswith("unknown deposit_address_status: ") and expected in line
                            for line in lines))
        answer = self.tool(deposit_address=DEPOSIT_ADDRESS)
        self.assertEqual(answer["verdict"], "UNKNOWN")
        self.assertEqual(statuses(answer)["deposit_address_status"], "unknown")
        self.assertIn(expected, detail(answer, "deposit_address_status"))
        self.assertEqual(self.tool()["verdict"], "ALLOW")

    def test_a_real_quote_past_its_deadline_is_never_allow_for_either_caller(self):
        self.real_quote_case('the real quote deadline "2026-11-03T11:59:00Z" is not after 2026-11-03T12:00:00Z',
                             {"quote": {"deadline": "2026-11-03T11:59:00Z"}})

    def test_a_real_quote_older_than_the_bound_is_never_allow_for_either_caller(self):
        self.real_quote_case(f'the real quote timestamp "2026-11-03T11:54:59Z": quote age is 301000 ms, over '
                             f"{intents.MAX_QUOTE_AGE_MS} ms", {"timestamp": "2026-11-03T11:54:59Z"})

    def test_a_real_quote_for_another_deposit_address_is_never_allow_for_either_caller(self):
        self.real_quote_case(f'the real quote depositAddress "other.near" is not the supplied "{DEPOSIT_ADDRESS}"',
                             {"quote": {"deadline": STATUS_DEADLINE, "depositAddress": "other.near"}})

    def test_a_real_quote_without_its_deposit_address_is_never_allow_for_either_caller(self):
        self.real_quote_case(f'the real quote has no depositAddress, so it is not bound to the supplied '
                             f'"{DEPOSIT_ADDRESS}"', {"quote": {"deadline": STATUS_DEADLINE}})

    def test_a_refunded_status_is_never_allow_for_either_caller(self):
        self.real_quote_case('the status "REFUNDED" is not one this leg accepts as waiting for a deposit', (),
                             "REFUNDED")

    def test_a_success_status_is_never_allow_for_either_caller(self):
        self.real_quote_case('the status "SUCCESS" is not one this leg accepts as waiting for a deposit', (),
                             "SUCCESS")

    def test_a_missing_status_is_never_allow_for_either_caller(self):
        self.real_quote_case("the status answer's status null is missing or not a string", (), None)

    def test_a_malformed_status_is_never_allow_for_either_caller(self):
        self.real_quote_case("the status answer's status 5 is missing or not a string", (), 5)

    def test_no_status_is_accepted_without_a_recorded_list_by_either_caller(self):
        self.assertEqual(ACCEPTED_STATUSES, ())
        with mock.patch("gate.intents.DEPOSIT_STATUSES", ACCEPTED_STATUSES):
            self.real_quote_case(f'the status "{WAITING}" is not one this leg accepts as waiting for a deposit: '
                                 "no recorded source lists the 1Click status values, so none is accepted", ())

    def cp1252_command(self, *extra):
        """command with stdout a strict cp1252 text stream, as a piped or redirected stdout is on Windows."""
        return self.encoded_command("cp1252", *extra)

    def encoded_command(self, encoding, *extra):
        """command with stdout a strict text stream in encoding."""
        output, errors = io.TextIOWrapper(io.BytesIO(), encoding=encoding), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = gate_main.main(["intents", "--origin", "nep141:wrap.near", "--destination", USDC_BASE_ASSET,
                                   "--amount", ONE_NEAR, "--recipient", USDC_BASE, "--refund-to", PAYER,
                                   "--app-fees", json.dumps(NAMED_FEES), *extra])
        output.flush()
        return code, output.buffer.getvalue().decode(encoding).splitlines(), errors.getvalue()

    def served(self, **extra):
        """The parsed replies of serve() to a framed check_intents_quote call with extra arguments and a
        ping after it, read from memory, and the raw reply bytes."""
        arguments = {"origin_asset": "nep141:wrap.near", "destination_asset": USDC_BASE_ASSET, "amount": ONE_NEAR,
                     "recipient": USDC_BASE, "refund_to": PAYER, "app_fees": NAMED_FEES, **extra}
        bodies = [json.dumps(message).encode("utf-8") for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "check_intents_quote", "arguments": arguments}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"})]
        stdin = io.BufferedReader(io.BytesIO(b"".join(b"Content-Length: %d\r\n\r\n" % len(body) + body
                                                      for body in bodies)))
        stdout = io.BytesIO()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mcp_server.serve(stdin, stdout), 0)
        data = rest = stdout.getvalue()
        replies = []
        while rest:
            header, _, rest = rest.partition(b"\r\n\r\n")
            length = int(header.split(b": ")[1])
            replies.append(json.loads(rest[:length]))
            rest = rest[length:]
        return replies, data

    def surrogate_case(self, check_id, write, deposit=False,
                       expected="holds a lone surrogate, text with no UTF-8 form"):
        """After write puts a lone surrogate in one provider answer, every caller answers UNKNOWN with
        check_id unknown and expected in its detail, by default because the answer holds text with no
        UTF-8 form: the command on a utf-8 and a strict cp1252 stdout, the MCP handler, and serve(),
        which still answers the ping after it."""
        write()
        extra = ("--deposit-address", DEPOSIT_ADDRESS) if deposit else ()
        for encoding in ("utf-8", "cp1252"):
            code, lines, errors = self.encoded_command(encoding, *extra)
            self.assertEqual((code, errors), (0, ""))
            self.assertTrue(any(line.startswith(f"unknown {check_id}: ") and expected in line for line in lines),
                            lines)
            self.assertIn("verdict: UNKNOWN (unsigned preflight)", lines)
            self.assertEqual(lines[-1], "Nothing was signed, nothing was deposited.")
        tool = {"deposit_address": DEPOSIT_ADDRESS} if deposit else {}
        answer = self.tool(**tool)
        self.assertEqual(answer["verdict"], "UNKNOWN")
        self.assertEqual(statuses(answer)[check_id], "unknown")
        self.assertIn(expected, detail(answer, check_id))
        json.dumps(answer, ensure_ascii=False).encode("utf-8")
        self.assertEqual(answer["evidence_hash"],
                         hashlib.sha256(evidence.canonical_bytes(answer["document"])).hexdigest())
        replies, data = self.served(**tool)
        data.decode("ascii")
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertFalse(replies[0]["result"]["isError"])
        served = json.loads(replies[0]["result"]["content"][0]["text"])
        self.assertEqual(served["verdict"], "UNKNOWN")
        self.assertIn(expected, detail(served, check_id))
        self.assertEqual(replies[1]["result"], {})

    def test_a_lone_surrogate_in_the_dry_quote_answer_is_unknown_for_every_caller(self):
        self.callers_over(NAMED_FEES)
        clean = {"amountOut": "4398699", "minAmountOut": "4398699"}
        for name, answer, echo in (
                ("amountInFormatted", {"quote": dict(clean, amountInFormatted=SURROGATE)}, None),
                ("correlationId", {"correlationId": SURROGATE}, None),
                ("timestamp", {"timestamp": SURROGATE}, None),
                ("an echoed sessionId", None, {"sessionId": SURROGATE}),
                ("an echoed refundTo", None, {"refundTo": SURROGATE}),
                ("an echoed key", None, {SURROGATE: 1})):
            with self.subTest(field=name):
                self.surrogate_case("quote_dry", lambda: self.quote_for(
                    self.fixtures, USDC_BASE, refund_to=PAYER, app_fees=NAMED_FEES, answer=answer, echo=echo))

    def test_a_lone_surrogate_in_the_status_answer_is_unknown_for_every_caller(self):
        self.callers_over(NAMED_FEES)
        request = dict(intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE,
                                          "DESTINATION_CHAIN", STATUS_DEADLINE, refund_to=PAYER,
                                          app_fees=NAMED_FEES), dry=False)
        for name, lifecycle, real in (
                ("status", SURROGATE, {}),
                ("depositAddress", WAITING, {"quote": {"deadline": STATUS_DEADLINE, "depositAddress": SURROGATE}}),
                ("timestamp", WAITING, {"timestamp": SURROGATE}),
                ("quoteRequest refundTo", WAITING, {"quoteRequest": dict(request, refundTo=SURROGATE)}),
                ("quoteRequest appFees", WAITING,
                 {"quoteRequest": dict(request, appFees=[{"recipient": SURROGATE, "fee": 20}])})):
            with self.subTest(field=name):
                self.surrogate_case("deposit_address_status", lambda: self.status_for(
                    self.fixtures, DEPOSIT_ADDRESS, request, lifecycle, **real), deposit=True)

    def test_a_lone_surrogate_in_the_goplus_answer_is_unknown_for_every_caller(self):
        self.callers_over(NAMED_FEES)
        for name, result in (("data_source", {"data_source": SURROGATE}),
                             ("a theft label", {"data_source": "synthetic", "cybercrime": SURROGATE}),
                             ("a field this leg does not read", {"data_source": "synthetic", "note": SURROGATE})):
            with self.subTest(field=name):
                self.surrogate_case("recipient_theft_label", lambda: self.goplus_says(
                    self.fixtures, USDC_BASE, {"code": 1, "result": result}))

    def test_a_lone_surrogate_in_the_firewall_answer_is_unknown_for_every_caller(self):
        self.callers_over(NAMED_FEES)
        for name, classification, scan in (("classification", SURROGATE, {}),
                                           ("evidence_url", "SAFE", {"evidence_url": SURROGATE}),
                                           ("status", "SAFE", {"status": SURROGATE}),
                                           ("a field this leg does not read", "SAFE", {"note": SURROGATE})):
            with self.subTest(field=name):
                self.surrogate_case("recipient_code", lambda: self.firewall_says(self.fixtures, classification,
                                                                                 **scan))

    def test_a_lone_surrogate_in_a_caller_argument_is_refused_by_every_caller(self):
        self.callers_over(NAMED_FEES)
        request = "the quote request holds a lone surrogate, text with no UTF-8 form"
        address = "the deposit address holds a lone surrogate, text with no UTF-8 form"
        for name, argv, arguments, refusal in (
                ("origin", ("--origin", SURROGATE), {"origin_asset": SURROGATE}, (2, f"invalid request: {request}")),
                ("refund account", ("--refund-to", SURROGATE), {"refund_to": SURROGATE},
                 (2, f"invalid request: {request}")),
                ("app fee recipient", ("--app-fees", json.dumps([{"recipient": SURROGATE, "fee": 20}])),
                 {"app_fees": [{"recipient": SURROGATE, "fee": 20}]}, (2, f"invalid request: {request}")),
                ("deposit address", ("--deposit-address", SURROGATE), {"deposit_address": SURROGATE},
                 (1, f"refused: {address}"))):
            with self.subTest(argument=name):
                for encoding in ("utf-8", "cp1252"):
                    code, lines, errors = self.encoded_command(encoding, *argv)
                    self.assertEqual((code, lines, errors), (refusal[0], [], refusal[1] + "\n"))
                with self.assertRaises(mcp_server.ToolError) as raised:
                    self.tool(**arguments)
                self.assertEqual(str(raised.exception), refusal[1])
                replies, data = self.served(**arguments)
                data.decode("ascii")
                self.assertEqual([reply["id"] for reply in replies], [1, 2])
                self.assertEqual(replies[0]["result"], {"content": [{"type": "text", "text": refusal[1]}],
                                                        "isError": True})
                self.assertEqual(replies[1]["result"], {})
        self.urlopen.assert_not_called()

    def test_a_body_nested_too_deeply_to_parse_is_unknown_for_every_caller(self):
        """Every read live, in mode off: one provider at a time answers a body nested too deeply to parse
        and the others answer from the synthetic fixtures."""
        self.callers_over(NAMED_FEES)
        deep, deep_at = mock.MagicMock(), [None]
        deep.__enter__.return_value.read.return_value = b"[" * 100000

        def opened(request, timeout=None):
            url, _, query = request.full_url.partition("?")
            if deep_at[0] is not None and deep_at[0] in request.full_url:
                return deep
            if request.data is None:
                method, params = "GET", dict(urllib.parse.parse_qsl(query)) or None
            else:
                body = json.loads(request.data)
                method, params = ((body["method"], body["params"]) if "jsonrpc" in body
                                  else ("POST", rpc.body_params(body)))
            [path] = self.fixtures.glob(f"*/{rpc.fixture_key(url, method, params)}.json")
            return reply(json.loads(path.read_text(encoding="utf-8"))["response"])

        self.urlopen.side_effect = opened
        for patcher in (mock.patch("gate.intents.live_deadline", return_value=DEADLINE),
                        mock.patch.dict("os.environ", {"GATE_FIXTURES_MODE": "off"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertEqual(self.tool()["verdict"], "ALLOW")
        self.assertEqual(self.tool(deposit_address=DEPOSIT_ADDRESS)["verdict"], "ALLOW")
        for check_id, url, deposit in (("quote_dry", "/v0/quote", False),
                                       ("deposit_address_status", "/v0/status", True),
                                       ("recipient_theft_label", "/api/v1/address_security/", False),
                                       ("recipient_code", intents.EVM_RPC_URLS["base"], False),
                                       ("recipient_code", "/api/firewall", False)):
            with self.subTest(read=url):
                self.surrogate_case(check_id, lambda: deep_at.__setitem__(0, url), deposit,
                                    expected="MalformedResponse: the body is nested too deeply to parse")

    def test_app_fees_nested_too_deeply_are_refused_by_the_command(self):
        """An --app-fees value nested past what the parser reads, or parsed and then too deep to quote in
        the refusal (a depth near the limit, which moves with the stack), is invalid request, exit 2."""
        self.callers_over(NAMED_FEES)
        for encoding in ("utf-8", "cp1252"):
            self.assertEqual(self.encoded_command(encoding, "--app-fees", "[" * 100000),
                             (2, [], "invalid request: the request is nested too deeply to read\n"))
        for depth in range(2900, 3100):
            with self.subTest(depth=depth):
                code, lines, errors = self.cp1252_command("--app-fees", "[" * depth + "]" * depth)
                self.assertEqual((code, lines), (2, []))
                self.assertTrue(errors.startswith("invalid request: "), errors[:80])
        self.urlopen.assert_not_called()

    def test_a_status_outside_cp1252_is_printed_as_unknown_by_the_command(self):
        self.callers_over(NAMED_FEES, lifecycle="\u4e2d")
        code, lines, errors = self.cp1252_command("--deposit-address", DEPOSIT_ADDRESS)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn('unknown deposit_address_status: the status "\\u4e2d" is not one this leg accepts as waiting '
                      f"for a deposit: only {WAITING} is accepted", lines)
        self.assertIn("verdict: UNKNOWN (unsigned preflight)", lines)
        self.assertEqual(lines[-1], "Nothing was signed, nothing was deposited.")
        self.assertEqual(self.tool(deposit_address=DEPOSIT_ADDRESS)["verdict"], "UNKNOWN")

    def test_a_deposit_address_outside_cp1252_is_printed_as_unknown_by_the_command(self):
        self.callers_over(NAMED_FEES, real={"quote": {"deadline": STATUS_DEADLINE, "depositAddress": "\u4e2d.near"}})
        code, lines, errors = self.cp1252_command("--deposit-address", DEPOSIT_ADDRESS)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn('unknown deposit_address_status: the real quote depositAddress "\\u4e2d.near" is not the '
                      f'supplied "{DEPOSIT_ADDRESS}"', lines)
        self.assertIn("verdict: UNKNOWN (unsigned preflight)", lines)
        self.assertEqual(lines[-1], "Nothing was signed, nothing was deposited.")
        self.assertEqual(self.tool(deposit_address=DEPOSIT_ADDRESS)["verdict"], "UNKNOWN")

    def test_a_goplus_data_source_outside_cp1252_is_printed_by_the_command(self):
        self.callers_over(NAMED_FEES)
        self.goplus_says(self.fixtures, USDC_BASE, {"code": 1, "result": {"data_source": "\u4e2d"}})
        code, lines, errors = self.cp1252_command()
        self.assertEqual((code, errors), (0, ""))
        self.assertTrue(any(line.startswith("pass    recipient_theft_label: ") and 'data_source "\\u4e2d";' in line
                            for line in lines))
        self.assertIn("verdict: ALLOW (unsigned preflight)", lines)
        self.assertEqual(lines[-1], "Nothing was signed, nothing was deposited.")
        self.assertIn('data_source "\u4e2d";', detail(self.tool(), "recipient_theft_label"))
        replies, data = self.served()
        data.decode("ascii")
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertIn('data_source "\u4e2d";', detail(json.loads(replies[0]["result"]["content"][0]["text"]),
                                                      "recipient_theft_label"))

    def test_a_foreign_app_fee_on_the_real_quote_is_refused_by_both_callers(self):
        self.callers_over(NAMED_FEES + [{"recipient": "thief.near", "fee": 9000}])
        code, lines, errors = self.command("--deposit-address", DEPOSIT_ADDRESS)
        self.assertEqual(code, 1)
        self.assertRegex(errors, "^refused: appFees .*thief.near")
        self.assertFalse(any(line.startswith("verdict:") for line in lines))
        with self.assertRaisesRegex(mcp_server.ToolError, "^refused: appFees .*thief.near"):
            self.tool(deposit_address=DEPOSIT_ADDRESS)

    def test_the_recorded_deadline_is_never_allow_for_either_caller(self):
        self.callers_over(NAMED_FEES, deadline=intents.RECORDED_DEADLINE)
        code, lines, errors = self.command()
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("verdict: UNKNOWN (unsigned preflight)", lines)
        self.assertTrue(any(line.startswith("unknown quote_deadline: the judged deadline") for line in lines))
        answer = self.tool()
        self.assertEqual(answer["verdict"], "UNKNOWN")
        self.assertEqual(statuses(answer)["quote_deadline"], "unknown")

    def test_an_allow_past_its_deadline_at_verification_is_refused_by_both_callers(self):
        self.callers_over(NAMED_FEES, verified_at=CLOCK + 600)
        for extra in ((), ("--deposit-address", DEPOSIT_ADDRESS)):
            with self.subTest(extra=extra):
                code, lines, errors = self.command(*extra)
                self.assertEqual(code, 1)
                self.assertRegex(errors, f'^refused: the judged deadline "{DEADLINE}" is not after ')
                self.assertFalse(any(line.startswith("verdict:") for line in lines))
        with self.assertRaisesRegex(mcp_server.ToolError, f'^refused: the judged deadline "{DEADLINE}" is not after '):
            self.tool()

    def test_a_malformed_refund_or_recipient_type_is_never_defaulted_by_either_caller(self):
        self.callers_over(NAMED_FEES)
        self.assertEqual(self.tool()["verdict"], "ALLOW")
        for value in (False, 0, [], {}, "", None):
            for name in ("refund_to", "refund_type"):
                with self.subTest(**{name: value}), self.assertRaisesRegex(
                        mcp_server.ToolError, f"^invalid request: {name}: not a non empty string"):
                    self.tool(**{name: value})
            with self.subTest(recipient_type=value), self.assertRaisesRegex(
                    mcp_server.ToolError, "^invalid request: recipientType .* is not one of DESTINATION_CHAIN"):
                self.tool(recipient_type=value)
        for value in (False, 0, [], {}, None):
            with self.subTest(origin_asset=value), self.assertRaisesRegex(
                    mcp_server.ToolError, "^invalid request: originAsset .* is not a string"):
                self.tool(origin_asset=value)
        answer = self.tool(recipient_type="INTENTS")
        self.assertEqual(answer["verdict"], "UNKNOWN")
        self.assertEqual(statuses(answer)["destination_supported"], "unknown")
        code, lines, errors = self.command("--refund-type", "")
        self.assertEqual(code, 2)
        self.assertRegex(errors, "^invalid request: refund_type: not a non empty string")
        self.assertFalse(any(line.startswith("verdict:") for line in lines))
        code, lines, errors = self.command("--recipient-type", "")
        self.assertEqual(code, 2)
        self.assertRegex(errors, '^invalid request: recipientType "" is not one of DESTINATION_CHAIN')
        self.assertFalse(any(line.startswith("verdict:") for line in lines))

    def test_a_malformed_amount_or_refund_type_is_refused_by_both_callers(self):
        self.callers_over(NAMED_FEES)
        for amount in ("", "-5", " 1", "1e6", "abc", "0", [], True, 1000, None):
            with self.subTest(amount=amount), self.assertRaisesRegex(mcp_server.ToolError,
                                                                     "^invalid request: amount: "):
                self.tool(amount=amount)
        for refund_type in ("banana", "origin_chain", " "):
            with self.subTest(refund_type=refund_type), self.assertRaisesRegex(
                    mcp_server.ToolError, "^invalid request: refund_type: not ORIGIN_CHAIN"):
                self.tool(refund_type=refund_type)
        for extra in (("--amount=-5",), ("--amount", "1e6"), ("--amount", "0"), ("--amount", " 1"),
                      ("--refund-type", "banana"), ("--refund-type", "origin_chain")):
            with self.subTest(extra=extra):
                code, lines, errors = self.command(*extra)
                self.assertEqual(code, 2)
                self.assertRegex(errors, "^invalid request: (amount|refund_type): ")
                self.assertFalse(any(line.startswith("verdict:") for line in lines))

    def advancing_callers(self, read, timestamp, deadline, real=()):
        """callers_over with one clock for judge_quote, bind_deposit_address and verify_binding, starting
        at CLOCK and advanced two seconds by each rpc.Client.get whose url read accepts."""
        judge_quote, bind, verify_binding = intents.judge_quote, intents.bind_deposit_address, intents.verify_binding
        original = rpc.Client.get
        self.callers_over(NAMED_FEES, deadline=deadline, timestamp=timestamp, real=real)
        now, root = [CLOCK], self.fixtures

        def advancing(client, url, *args, **kwargs):
            if read(url):
                now[0] += 2
            return original(client, url, *args, **kwargs)

        for patcher in (mock.patch("gate.intents.judge_quote", side_effect=lambda *a, **k: judge_quote(
                            *a, **k, fixtures_root=root, clock=lambda: now[0])),
                        mock.patch("gate.intents.bind_deposit_address", side_effect=lambda *a, **k: bind(
                            *a, **k, fixtures_root=root, clock=lambda: now[0])),
                        mock.patch("gate.intents.verify_binding", side_effect=lambda *a, **k: verify_binding(
                            *a, **{"clock": lambda: now[0], **k})),
                        mock.patch.object(rpc.Client, "get", advancing)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return now

    def status_read_case(self, timestamp, deadline, expected, real=()):
        """Both callers judge at CLOCK and bind with the clock advanced two seconds by the status read,
        with real applied to the status quoteResponse."""
        now = self.advancing_callers(lambda url: url == "/v0/status", timestamp, deadline, real=real)
        code, lines, errors = self.command("--deposit-address", DEPOSIT_ADDRESS)
        self.assertEqual(now[0], CLOCK + 2)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("verdict: UNKNOWN (unsigned preflight)", lines)
        self.assertTrue(any(line.startswith("unknown deposit_address_status: ") and expected in line
                            for line in lines))
        now[0] = CLOCK
        answer = self.tool(deposit_address=DEPOSIT_ADDRESS)
        self.assertEqual(now[0], CLOCK + 2)
        self.assertEqual(answer["verdict"], "UNKNOWN")
        self.assertIn(expected, detail(answer, "deposit_address_status"))
        now[0] = CLOCK
        self.assertEqual(self.tool()["verdict"], "ALLOW")

    def test_a_quote_aging_past_the_bound_during_the_status_read_is_never_allow_for_either_caller(self):
        self.status_read_case("2026-11-03T11:55:01Z", DEADLINE,
                              f"quote age is 301000 ms, over {intents.MAX_QUOTE_AGE_MS} ms")

    def test_a_judged_deadline_passing_during_the_status_read_is_never_allow_for_either_caller(self):
        self.status_read_case("2026-11-03T11:59:59Z", "2026-11-03T12:00:01Z",
                              'the judged deadline "2026-11-03T12:00:01Z" is not after 2026-11-03T12:00:02Z')

    def test_a_real_quote_deadline_passing_during_the_status_read_is_never_allow_for_either_caller(self):
        self.status_read_case("2026-11-03T11:59:59Z", DEADLINE,
                              'the real quote deadline "2026-11-03T12:00:01Z" is not after 2026-11-03T12:00:02Z',
                              real={"quote": {"deadline": "2026-11-03T12:00:01Z", "depositAddress": DEPOSIT_ADDRESS}})

    def test_a_real_quote_aging_past_the_bound_during_the_status_read_is_never_allow_for_either_caller(self):
        self.status_read_case("2026-11-03T11:59:59Z", DEADLINE,
                              f'the real quote timestamp "2026-11-03T11:55:01Z": quote age is 301000 ms, over '
                              f"{intents.MAX_QUOTE_AGE_MS} ms", real={"timestamp": "2026-11-03T11:55:01Z"})

    def test_a_quote_aging_past_the_bound_during_the_recipient_reads_is_never_allow_for_either_caller(self):
        now = self.advancing_callers(lambda url: url.startswith("/api/v1/address_security/"),
                                     "2026-11-03T11:55:01Z", DEADLINE)
        for extra in ((), ("--deposit-address", DEPOSIT_ADDRESS)):
            with self.subTest(extra=extra):
                now[0] = CLOCK
                code, lines, errors = self.command(*extra)
                self.assertEqual((code, errors), (0, ""))
                self.assertIn("verdict: UNKNOWN (unsigned preflight)", lines)
                self.assertIn(f"unknown quote_age: quote age is 301000 ms, over {intents.MAX_QUOTE_AGE_MS} ms", lines)
                now[0] = CLOCK
                answer = self.tool(**({"deposit_address": DEPOSIT_ADDRESS} if extra else {}))
                self.assertEqual(answer["verdict"], "UNKNOWN")
                self.assertEqual(statuses(answer)["quote_age"], "unknown")

    def test_a_forged_judged_slippage_is_refused_by_both_callers(self):
        forged = rehashed(judge(), slippageTolerance="100")
        errors = io.StringIO()
        with mock.patch("gate.intents.judge_quote", return_value=forged), \
                mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(errors):
            code = gate_main.main(["intents", "--origin", "nep141:wrap.near", "--destination", USDC_BASE_ASSET,
                                   "--amount", ONE_NEAR, "--recipient", DEMO_RECIPIENT])
            with self.assertRaisesRegex(mcp_server.ToolError, "^refused: the judged slippageTolerance"):
                mcp_server._check_intents_quote({"origin_asset": "nep141:wrap.near",
                                                 "destination_asset": USDC_BASE_ASSET, "amount": ONE_NEAR,
                                                 "recipient": DEMO_RECIPIENT}, None)
        self.assertEqual(code, 1)
        self.assertRegex(errors.getvalue(), "^refused: the judged slippageTolerance")


class DepositAddressTest(Case):
    def allowed(self, root, **options):
        self.quote_for(root, USDC_BASE, **options)
        self.goplus_says(root, USDC_BASE, {"code": 1, "result": {"data_source": "synthetic"}})
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL, **options)
        self.assertEqual(result["verdict"], "ALLOW")
        return result

    def status_request(self, result, **changes):
        """The quoteRequest a 1Click status answer echoes for a real quote made from the judged request:
        not dry, a deadline in the future, with changes applied and a None change removing a key."""
        request = dict(result["document"]["subject"]["quote_request"], dry=False, deadline=STATUS_DEADLINE)
        request.update(changes)
        return {key: value for key, value in request.items() if value is not None}

    def bind(self, root, result, quote_request):
        return intents.bind_deposit_address(result, quote_request, DEPOSIT_ADDRESS, fixtures_root=root,
                                            clock=lambda: CLOCK)

    def test_a_matching_status_request_binds_the_supplied_address(self):
        root = self.root("evm", "goplus")
        result = self.allowed(root, refund_to=PAYER)
        quote_request = dict(result["document"]["subject"]["quote_request"])
        status_request = self.status_request(result)
        self.status_for(root, DEPOSIT_ADDRESS, status_request)
        check = self.bind(root, result, quote_request)
        verify(result, quote_request)
        self.assertEqual(check["status"], "pass")
        self.assertEqual(result["verdict"], "ALLOW")
        for named in (f'refundTo "{PAYER}"', 'refundType "ORIGIN_CHAIN"', "slippageTolerance 100",
                      f'deadline "{STATUS_DEADLINE}"', "appFees []",
                      f'real quote deadline "{STATUS_DEADLINE}" in the future',
                      f"quote age is 1000 ms, within {intents.MAX_QUOTE_AGE_MS} ms",
                      f'real quote depositAddress "{DEPOSIT_ADDRESS}", as supplied',
                      f'status "{WAITING}" waiting for a deposit'):
            self.assertIn(named, check["detail"])
        self.assertEqual(statuses(result)["deposit_address_status"], "pass")
        self.assertEqual(result["evidence_hash"], evidence.evidence_hash(result["document"]))
        key = rpc.fixture_key(intents.ONECLICK_URL + "/v0/status", "GET", {"depositAddress": DEPOSIT_ADDRESS})
        fixture = json.loads((root / "intents" / f"{key}.json").read_text(encoding="utf-8"))
        self.assertIs(fixture["synthetic"], True)
        self.assertEqual(fixture["response"], {"quoteResponse": {"quoteRequest": status_request,
                                                                 "quote": {"deadline": STATUS_DEADLINE,
                                                                           "depositAddress": DEPOSIT_ADDRESS},
                                                                 "timestamp": "2026-11-03T11:59:59Z"},
                                               "status": WAITING})

    def test_each_bound_field_mismatch_is_refused_with_its_field(self):
        mismatches = (("originAsset", "other-origin"), ("destinationAsset", "other-destination"), ("amount", "2"),
                      ("recipient", "0x0000000000000000000000000000000000000001"), ("recipientType", "INTENTS"),
                      ("refundTo", "someone-else.near"), ("refundTo", None), ("refundType", "INTENTS"),
                      ("swapType", "EXACT_OUTPUT"), ("depositType", "INTENTS"), ("slippageTolerance", 101),
                      ("slippageTolerance", "50"), ("slippageTolerance", True), ("slippageTolerance", None),
                      ("appFees", NAMED_FEES), ("virtualChainRecipient", "0x0000000000000000000000000000000000000002"),
                      ("virtualChainRefundRecipient", "0x0000000000000000000000000000000000000002"),
                      ("customRecipientMsg", "x"), ("rebates", [{"recipient": "r.near", "share": 100}]),
                      ("insured", True), ("confidentiality", "basic"), ("depositMode", "MEMO"),
                      ("unlistedKey", "x"), ("dry", True), ("dry", None), ("dry", 0), ("dry", "false"))
        for field, value in mismatches:
            with self.subTest(field=field, value=value):
                root = self.root("evm", "goplus")
                result = self.allowed(root, refund_to=PAYER)
                quote_request = dict(result["document"]["subject"]["quote_request"])
                self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result, **{field: value}))
                with self.assertRaisesRegex(ValueError, field):
                    self.bind(root, result, quote_request)

    def test_a_foreign_refund_account_on_a_genuine_quote_is_refused(self):
        root = self.root("evm", "goplus")
        result = self.allowed(root, refund_to=PAYER)
        quote_request = dict(result["document"]["subject"]["quote_request"])
        self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result, refundTo="collector.near"))
        with self.assertRaisesRegex(ValueError, f'refundTo "collector.near" is not the judged "{PAYER}"'):
            self.bind(root, result, quote_request)
        self.assertNotIn("deposit_address_status", statuses(result))

    def test_app_fees_bind_only_when_named_and_equal(self):
        root = self.root("evm", "goplus")
        result = self.allowed(root, refund_to=PAYER, app_fees=NAMED_FEES)
        quote_request = dict(result["document"]["subject"]["quote_request"])
        self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result))
        self.assertEqual(self.bind(root, result, quote_request)["status"], "pass")
        for fees in (None, [], [{"recipient": "fees.near", "fee": 21}], NAMED_FEES * 2):
            with self.subTest(fees=fees):
                root = self.root("evm", "goplus")
                result = self.allowed(root, refund_to=PAYER, app_fees=NAMED_FEES)
                self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result, appFees=fees))
                with self.assertRaisesRegex(ValueError, "appFees"):
                    self.bind(root, result, quote_request)

    def test_a_lower_slippage_and_the_informational_keys_still_bind(self):
        root = self.root("evm", "goplus")
        result = self.allowed(root, refund_to=PAYER)
        quote_request = dict(result["document"]["subject"]["quote_request"])
        self.status_for(root, DEPOSIT_ADDRESS, self.status_request(
            result, slippageTolerance=50, depositMode="SIMPLE", insured=False, confidentiality="public",
            sessionId="s", referral="wallet", connectedWallets=["a"], quoteWaitingTimeMs=0))
        self.assertEqual(self.bind(root, result, quote_request)["status"], "pass")
        self.assertEqual(result["verdict"], "ALLOW")

    def test_a_status_field_the_schema_does_not_allow_is_unknown_or_refused(self):
        for field, value, outcome in (("connectedWallets", False, "unknown"), ("sessionId", {}, "unknown"),
                                      ("quoteWaitingTimeMs", "banana", "unknown"),
                                      ("virtualChainRecipient", [], "unknown"), ("connectedWallets", None, "unknown"),
                                      ("referral", None, "unknown"), ("rebates", "", "unknown"),
                                      ("insured", None, "refused"), ("originAsset", False, "refused"),
                                      ("originAsset", 0, "refused"), ("originAsset", [], "refused"),
                                      ("originAsset", {}, "refused"), ("amount", "-5", "refused"),
                                      ("refundType", "banana", "refused")):
            with self.subTest(field=field, value=value):
                root = self.root("evm", "goplus")
                result = self.allowed(root, refund_to=PAYER)
                quote_request = dict(result["document"]["subject"]["quote_request"])
                self.status_for(root, DEPOSIT_ADDRESS, dict(self.status_request(result), **{field: value}))
                if outcome == "refused":
                    with self.assertRaisesRegex(ValueError, field):
                        self.bind(root, result, quote_request)
                    continue
                check = self.bind(root, result, quote_request)
                self.assertEqual(check["status"], "unknown")
                self.assertIn(field, check["detail"])
                self.assertEqual(result["verdict"], "UNKNOWN")
                verify(result, quote_request)

    def status_read_case(self, timestamp, deadline, expected):
        """Judge at CLOCK, then bind with the clock advanced two seconds by the status read: never ALLOW."""
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE, timestamp=timestamp, deadline=deadline, refund_to=PAYER)
        self.goplus_says(root, USDC_BASE, {"code": 1, "result": {"data_source": "synthetic"}})
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL, deadline=deadline, refund_to=PAYER)
        self.assertEqual(result["verdict"], "ALLOW")
        quote_request = dict(result["document"]["subject"]["quote_request"])
        self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result))
        now = [CLOCK]
        original = rpc.Client.get

        def advancing(client, url, *args, **kwargs):
            if url == "/v0/status":
                now[0] += 2
            return original(client, url, *args, **kwargs)

        with mock.patch.object(rpc.Client, "get", advancing):
            check = intents.bind_deposit_address(result, quote_request, DEPOSIT_ADDRESS, fixtures_root=root,
                                                 clock=lambda: now[0])
        self.assertEqual(now[0], CLOCK + 2)
        self.assertEqual(check["status"], "unknown")
        self.assertIn(expected, check["detail"])
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertEqual(result["evidence_hash"], evidence.evidence_hash(result["document"]))
        verify(result, quote_request, clock=now[0])

    def test_a_quote_aging_past_the_bound_during_the_status_read_is_never_allow(self):
        self.status_read_case("2026-11-03T11:55:01Z", DEADLINE,
                              f"quote age is 301000 ms, over {intents.MAX_QUOTE_AGE_MS} ms")

    def test_a_judged_deadline_passing_during_the_status_read_is_never_allow(self):
        self.status_read_case("2026-11-03T11:59:59Z", "2026-11-03T12:00:01Z",
                              'the judged deadline "2026-11-03T12:00:01Z" is not after 2026-11-03T12:00:02Z')

    def test_a_quote_a_fraction_of_a_millisecond_past_the_bound_at_the_status_read_is_never_allow(self):
        for step, status, verdict in ((0, "pass", "ALLOW"), (0.0009, "unknown", "UNKNOWN")):
            with self.subTest(step=step):
                root = self.root("evm", "goplus")
                self.quote_for(root, USDC_BASE, timestamp="2026-11-03T11:55:00.000Z", refund_to=PAYER)
                self.goplus_says(root, USDC_BASE, {"code": 1, "result": {"data_source": "synthetic"}})
                self.firewall_says(root, "SAFE")
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL, refund_to=PAYER)
                self.assertEqual(result["verdict"], "ALLOW")
                quote_request = dict(result["document"]["subject"]["quote_request"])
                self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result))
                now = [CLOCK]
                original = rpc.Client.get

                def advancing(client, url, *args, step=step, original=original, **kwargs):
                    if url == "/v0/status":
                        now[0] += step
                    return original(client, url, *args, **kwargs)

                with mock.patch.object(rpc.Client, "get", advancing):
                    check = intents.bind_deposit_address(result, quote_request, DEPOSIT_ADDRESS, fixtures_root=root,
                                                         clock=lambda: now[0])
                self.assertEqual(check["status"], status)
                self.assertEqual(result["verdict"], verdict)
                self.assertEqual(result["evidence_hash"], evidence.evidence_hash(result["document"]))
        self.assertIn(f"quote age is 300001 ms, over {intents.MAX_QUOTE_AGE_MS} ms, at the clock read after the "
                      "status lookup", check["detail"])
        result = self.allowed(self.root("evm", "goplus"), refund_to=PAYER)
        quote_request = dict(result["document"]["subject"]["quote_request"])
        with self.assertRaisesRegex(ValueError, f"^quote age is 300001 ms, over {intents.MAX_QUOTE_AGE_MS} ms"):
            intents.bind_deposit_address(result, quote_request, DEPOSIT_ADDRESS, clock=lambda: CLOCK + 299.0009)

    def test_a_past_missing_or_invalid_status_deadline_is_unknown(self):
        for deadline in (intents.RECORDED_DEADLINE, "2026-11-03T12:00:00.000Z", None, "not a date", 5,
                         "2026-11-04", "2026-11-04T00:00:00", "2026-11-03T14:10:00+01:60", "2026-11-03T24:10:00Z"):
            with self.subTest(deadline=deadline):
                root = self.root("evm", "goplus")
                result = self.allowed(root, refund_to=PAYER)
                quote_request = dict(result["document"]["subject"]["quote_request"])
                self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result, deadline=deadline))
                check = self.bind(root, result, quote_request)
                self.assertEqual(check["status"], "unknown")
                self.assertIn("deadline", check["detail"])
                self.assertEqual(result["verdict"], "UNKNOWN")
                verify(result, quote_request)

    def test_a_missing_malformed_or_failing_real_quote_time_is_unknown(self):
        cases = [{"quote": None}, {"quote": JSON_NULL}, {"quote": "x"}, {"quote": []}, {"quote": {}}]
        cases += [{"quote": {"deadline": deadline}}
                  for deadline in (None, 5, [], "", "not a date", "2026-11-04", "2026-11-04T00:00:00",
                                   "2026-11-03T14:10:00+01:60", "2026-11-03T24:10:00Z", "2026-11-03T12:00:00Z",
                                   "2026-11-03T11:59:00Z")]
        cases += [{"timestamp": timestamp} for timestamp in (None, JSON_NULL, 5, [], "", "not a date", "2026-11-03",
                                                             "2026-11-03T11:59:59", "2026-11-03T11:54:59Z",
                                                             "2026-11-03T11:54:59.999Z", "2026-11-03T12:00:01Z")]
        for real in cases:
            with self.subTest(real=real):
                root = self.root("evm", "goplus")
                result = self.allowed(root, refund_to=PAYER)
                quote_request = dict(result["document"]["subject"]["quote_request"])
                self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result), **real)
                check = self.bind(root, result, quote_request)
                self.assertEqual(check["status"], "unknown")
                self.assertIn("real quote", check["detail"])
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(result["evidence_hash"], evidence.evidence_hash(result["document"]))
                verify(result, quote_request)

    def test_a_fresh_real_quote_still_binds(self):
        for real in ({}, {"quote": {"deadline": "2026-11-03T12:00:00.001Z", "depositAddress": DEPOSIT_ADDRESS}},
                     {"quote": {"deadline": "2026-11-03T13:10:00+01:00", "amountOut": "4398699",
                                "depositAddress": DEPOSIT_ADDRESS}},
                     {"timestamp": "2026-11-03T11:55:00Z"}, {"timestamp": "2026-11-03T12:00:00.000Z"}):
            with self.subTest(real=real):
                root = self.root("evm", "goplus")
                result = self.allowed(root, refund_to=PAYER)
                quote_request = dict(result["document"]["subject"]["quote_request"])
                self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result), **real)
                check = self.bind(root, result, quote_request)
                self.assertEqual(check["status"], "pass")
                self.assertIn("real quote deadline", check["detail"])
                self.assertEqual(result["verdict"], "ALLOW")
                verify(result, quote_request)

    def test_a_real_quote_for_another_or_no_deposit_address_is_unknown(self):
        for address in ("other.near", "DEPOSIT.NEAR", DEPOSIT_ADDRESS + " ", "", None, JSON_NULL, 5,
                        [DEPOSIT_ADDRESS], {"address": DEPOSIT_ADDRESS}):
            with self.subTest(address=address):
                root = self.root("evm", "goplus")
                result = self.allowed(root, refund_to=PAYER)
                quote_request = dict(result["document"]["subject"]["quote_request"])
                real = {"deadline": STATUS_DEADLINE, "depositAddress": None if address is JSON_NULL else address}
                if address is None:
                    del real["depositAddress"]
                self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result), quote=real)
                check = self.bind(root, result, quote_request)
                self.assertEqual(check["status"], "unknown")
                self.assertIn("depositAddress", check["detail"])
                self.assertIn(f'the supplied "{DEPOSIT_ADDRESS}"', check["detail"])
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(result["evidence_hash"], evidence.evidence_hash(result["document"]))
                verify(result, quote_request)

    def test_a_status_not_waiting_for_a_deposit_is_unknown(self):
        for lifecycle in ("REFUNDED", "SUCCESS", "PENDING_DEPOSIT", WAITING.lower(), WAITING + " ", "", None,
                          JSON_NULL, 5, True, [WAITING], {"status": WAITING}):
            with self.subTest(lifecycle=lifecycle):
                root = self.root("evm", "goplus")
                result = self.allowed(root, refund_to=PAYER)
                quote_request = dict(result["document"]["subject"]["quote_request"])
                self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result), lifecycle)
                check = self.bind(root, result, quote_request)
                self.assertEqual(check["status"], "unknown")
                self.assertRegex(check["detail"], "^the status (answer's status .* is missing or not a string|"
                                                  ".* is not one this leg accepts as waiting for a deposit)")
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(result["evidence_hash"], evidence.evidence_hash(result["document"]))
                verify(result, quote_request)

    def test_no_status_binds_without_a_recorded_list(self):
        root = self.root("evm", "goplus")
        result = self.allowed(root, refund_to=PAYER)
        quote_request = dict(result["document"]["subject"]["quote_request"])
        self.status_for(root, DEPOSIT_ADDRESS, self.status_request(result))
        with mock.patch("gate.intents.DEPOSIT_STATUSES", ACCEPTED_STATUSES):
            check = self.bind(root, result, quote_request)
        self.assertEqual(check["status"], "unknown")
        self.assertEqual(check["detail"], f'the status "{WAITING}" is not one this leg accepts as waiting for a '
                                          "deposit: no recorded source lists the 1Click status values, so none is "
                                          "accepted")
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_a_404_means_the_address_is_unknown_to_1click(self):
        result = self.allowed(self.root("evm", "goplus"))
        quote_request = dict(result["document"]["subject"]["quote_request"])
        error = urllib.error.HTTPError("https://1click.example/v0/status", 404, "Not Found", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(ValueError, "unknown to 1Click"):
                intents.bind_deposit_address(result, quote_request, DEPOSIT_ADDRESS, mode="off", clock=lambda: CLOCK)

    def test_failed_status_reads_are_unknown_and_refused(self):
        failures = (urllib.error.HTTPError("https://1click.example/v0/status", 401, "Unauthorized", {}, None),
                    urllib.error.HTTPError("https://1click.example/v0/status", 503, "Unavailable", {}, None),
                    OSError("offline"))
        for error in failures:
            with self.subTest(error=type(error).__name__, status=getattr(error, "code", None)):
                result = self.allowed(self.root("evm", "goplus"))
                quote_request = dict(result["document"]["subject"]["quote_request"])
                with mock.patch("urllib.request.urlopen", side_effect=error):
                    check = intents.bind_deposit_address(result, quote_request, DEPOSIT_ADDRESS, mode="off",
                                                         clock=lambda: CLOCK)
                self.assertEqual(check["status"], "unknown")
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(result["evidence_hash"], evidence.evidence_hash(result["document"]))
                verify(result, quote_request)


class FailClosedTest(Case):
    def test_a_goplus_failure_is_unknown_never_allow(self):
        root = self.root("intents", "evm")
        result = judge(fixtures_root=root, deadline=intents.RECORDED_DEADLINE)
        self.assertEqual(statuses(result)["recipient_theft_label"], "unknown")
        self.assertEqual(result["verdict"], "UNKNOWN")
        for body in ({"code": 0, "message": "error", "result": {}}, {"code": 1, "result": {}}, ["x"]):
            with self.subTest(body=body):
                self.goplus_says(root, DEMO_RECIPIENT, body)
                self.assertEqual(judge(fixtures_root=root, deadline=intents.RECORDED_DEADLINE)["verdict"], "UNKNOWN")

    def test_a_goplus_answer_outside_its_recorded_shape_is_unknown_never_allow(self):
        malformed = 'theft labels that are not "0" or "1"'
        cases = (({"code": 1, "result": {"stealing_attack": None}}, "unknown", malformed),
                 ({"code": 1, "result": {"stealing_attack": 1}}, "unknown", malformed),
                 ({"code": 1, "result": {"stealing_attack": 0}}, "unknown", malformed),
                 ({"code": 1, "result": {"stealing_attack": True}}, "unknown", malformed),
                 ({"code": 1, "result": {"stealing_attack": ""}}, "unknown", malformed),
                 ({"code": 1, "result": {"mixer": "yes", "stealing_attack": "0"}}, "unknown", malformed),
                 ({"code": True, "result": {"stealing_attack": "0"}}, "unknown", "code True"),
                 ({"code": 1.0, "result": {"stealing_attack": "0"}}, "unknown", "code 1.0"),
                 ({"code": "1", "result": {"stealing_attack": "0"}}, "unknown", "code '1'"),
                 ({"result": {"stealing_attack": "0"}}, "unknown", "code None"),
                 ({"code": 1, "result": {"stealing_attack": "1", "mixer": None}}, "signal", "stealing_attack"),
                 ({"code": 1, "result": {"stealing_attack": "0"}}, "pass", "none of the"),
                 ({"code": 1, "result": {"data_source": "synthetic"}}, "pass", "none of the"))
        for body, status, named in cases:
            with self.subTest(body=body):
                root = self.root("evm")
                self.quote_for(root, USDC_BASE)
                self.goplus_says(root, USDC_BASE, body)
                self.firewall_says(root, "SAFE")
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
                self.assertEqual(statuses(result)["recipient_theft_label"], status)
                self.assertEqual(result["verdict"], {"unknown": "UNKNOWN", "signal": "DENY", "pass": "ALLOW"}[status])
                self.assertIn(named, detail(result, "recipient_theft_label"))

    def test_an_ofac_listed_evm_recipient_is_denied_with_entry_and_source_evidence(self):
        document = sanctions.load()
        listing = next(listing for listing in document["listings"]
                       if listing["asset"] == "ETH" and listing["address"].startswith("0x"))
        destination = "nep141:eth.omft.near"
        root = self.root("evm")
        self.quote_for_destination(root, listing["address"], destination)
        self.goplus_says(root, listing["address"], {"code": 1, "result": {"data_source": "synthetic"}})
        result = judge(listing["address"], destination=destination, fixtures_root=root)
        self.assertEqual(result["verdict"], "DENY")
        check = next(check for check in result["checks"] if check["id"] == "ofac_sanctioned")
        self.assertEqual(check["status"], "signal")
        match = next(match for match in sanctions.is_sanctioned("eth", listing["address"], document)
                     if match["uid"] == listing["uid"])
        reason = " ".join(result["reasons"])
        for value in (str(match["uid"]), match["name"], *match["programs"], match["publish_date"],
                      match["source_sha256"]):
            self.assertIn(value, reason)
        fixture_source = next(check for check in result["checks"] if check["id"] == "ofac_sanctioned")["source"]
        self.assertTrue(fixture_source["provider"].startswith("fixtures/sanctions/"))
        fixture_path = sanctions.ROOT / fixture_source["provider"]
        self.assertEqual(fixture_source["request_sha256"], hashlib.sha256(fixture_path.read_bytes()).hexdigest())

    def test_eth_listing_is_denied_for_evm_chains_without_published_asset_codes(self):
        document = sanctions.load()
        listing = next(listing for listing in document["listings"]
                       if listing["uid"] == 24003 and listing["asset"] == "ETH")
        for chain in ("base", "op", "pol", "hood"):
            with self.subTest(chain=chain):
                destination = f"nep141:{chain}.omft.near"
                root = self.root("evm")
                self.quote_for_destination(root, listing["address"], destination)
                self.goplus_says(root, listing["address"], {"code": 1, "result": {"data_source": "synthetic"}})

                result = judge(listing["address"], destination=destination, fixtures_root=root)

                self.assertEqual(result["verdict"], "DENY")
                check = next(check for check in result["checks"] if check["id"] == "ofac_sanctioned")
                self.assertEqual(check["status"], "signal")
                self.assertIn("uid 24003", check["detail"])

    def test_an_unlisted_recipient_passes_the_ofac_check(self):
        document = sanctions.load()
        self.assertEqual(sanctions.is_sanctioned("eth", DEMO_RECIPIENT, document), [])
        destination = "nep141:eth.omft.near"
        root = self.root("evm")
        self.quote_for_destination(root, DEMO_RECIPIENT, destination)
        self.goplus_says(root, DEMO_RECIPIENT, {"code": 1, "result": {"data_source": "synthetic"}})
        result = judge(DEMO_RECIPIENT, destination=destination, fixtures_root=root)
        check = next(check for check in result["checks"] if check["id"] == "ofac_sanctioned")
        self.assertEqual(check["status"], "pass")
        self.assertIn(document["publish_date"], check["detail"])
        self.assertIn(str(sanctions.counts(document)["addresses"]), check["detail"])

    def test_a_non_evm_chain_without_an_ofac_code_passes_with_the_list_fact(self):
        document = sanctions.load()
        check = intents._ofac_check("ton", "non-evm-address", None)
        self.assertEqual(check["status"], "pass")
        self.assertIn("publishes no address for chain ton", check["detail"])
        self.assertIn(document["publish_date"], check["detail"])

    def test_a_non_evm_address_uses_only_its_destination_asset_code(self):
        document = sanctions.load()
        xbt_keys = {listing["key"] for listing in document["listings"] if listing["asset"] == "XBT"}
        shared = next(listing for listing in document["listings"]
                      if listing["asset"] == "USDT" and listing["key"] in xbt_keys)

        check = intents._ofac_check("btc", shared["address"], None)

        self.assertEqual(check["status"], "signal")
        self.assertIn("asset XBT address", check["detail"])
        self.assertNotIn("asset USDT address", check["detail"])

    def test_a_missing_ofac_file_is_unknown(self):
        destination = "nep141:eth.omft.near"
        root = self.root("evm")
        self.quote_for_destination(root, DEMO_RECIPIENT, destination)
        self.goplus_says(root, DEMO_RECIPIENT, {"code": 1, "result": {"data_source": "synthetic"}})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sanctions, "FIXTURE_DIR", Path(tmp)):
            result = judge(DEMO_RECIPIENT, destination=destination, fixtures_root=root)
        check = next(check for check in result["checks"] if check["id"] == "ofac_sanctioned")
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertEqual(check["status"], "unknown")
        self.assertIn("no ofac-sdn-addresses", check["detail"])

    def test_an_unsupported_chain_is_unknown(self):
        result = judge(USDC_BASE, destination="nep141:sol.omft.near")
        self.assertEqual(statuses(result)["recipient_theft_label"], "pass")
        self.assertEqual(statuses(result)["destination_supported"], "unknown")
        self.assertEqual(statuses(result)["recipient_code"], "unknown")
        self.assertEqual(result["verdict"], "UNKNOWN")
        result = judge(USDC_BASE, destination="nep141:gnosis-0x4d18815d14fe5c3304e87b3fa18318baa5c23820.omft.near")
        self.assertIn("gnosis is not one of", detail(result, "destination_supported"))
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_a_recipient_that_is_not_an_evm_address_is_refused_before_any_request(self):
        for recipient in ("0xabc?apikey=k", "0x1fcdb04d0c5364fbd92c73ca8af9baa72c26910", "vendor.near",
                          DEMO_RECIPIENT + "/x", None):
            with self.subTest(recipient=recipient), self.assertRaises(ValueError):
                judge(recipient, mode="off")
        self.urlopen.assert_not_called()
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            code = gate_main.main(["intents", "--origin", "nep141:wrap.near", "--destination", USDC_BASE_ASSET,
                                   "--amount", ONE_NEAR, "--recipient", "0xabc?apikey=k"])
        self.assertEqual(code, 2)
        self.assertIn("invalid request", errors.getvalue())

    def test_the_firewall_is_not_called_without_code(self):
        with mock.patch("gate.intents.firewall", side_effect=AssertionError("firewall called")):
            result = judge(firewall_url=FIREWALL_URL)
        self.assertEqual(statuses(result)["recipient_code"], "pass")
        self.assertIn("the firewall is not called", result["checks"][-1]["detail"])

    def test_the_lowest_firewall_band_never_makes_allow_when_goplus_is_unknown(self):
        root = self.root("evm")
        self.quote_for(root, USDC_BASE)
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(statuses(result)["recipient_code"], "pass")
        self.assertIn('classification "SAFE"', result["checks"][-1]["detail"])
        self.assertEqual(statuses(result)["recipient_theft_label"], "unknown")
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_the_firewall_never_appears_in_an_allow_reason(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE)
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(result["verdict"], "ALLOW")
        self.assertTrue(all("firewall" not in reason.lower() and "SAFE" not in reason for reason in result["reasons"]))
        for classification, verdict in (("BLOCK_RECOMMENDED", "DENY"), ("HIGH_RISK", "DENY"), ("CAUTION", "ALLOW"),
                                        ("A_NEW_BAND", "UNKNOWN"), ("", "UNKNOWN")):
            with self.subTest(classification=classification):
                self.firewall_says(root, classification)
                self.assertEqual(judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)["verdict"], verdict)

    def test_an_incomplete_firewall_scan_is_unknown_never_a_pass(self):
        for scan in ({"status": "unknown"}, {"status": None}, {"status": "OK"}, {"partial": True},
                     {"partial": 1}, {"partial": "true"}, {"partial": 0}, {"partial": None},
                     {"failed_sources": ["structural"]}, {"failed_sources": "structural"},
                     {"failed_sources": None}, {"failed_sources": False}, {"failed_sources": 0},
                     {"failed_sources": ""}, {"failed_sources": {}}):
            for classification in ("SAFE", "CAUTION"):
                with self.subTest(scan=scan, classification=classification):
                    root = self.root("evm", "goplus")
                    self.quote_for(root, USDC_BASE)
                    self.firewall_says(root, classification, **scan)
                    result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
                    self.assertEqual(statuses(result)["recipient_code"], "unknown")
                    self.assertEqual(result["verdict"], "UNKNOWN")
                    self.assertIn(f'classification "{classification}"', detail(result, "recipient_code"))
                    self.assertIn("incomplete", detail(result, "recipient_code"))

    def test_a_null_failed_sources_is_an_incomplete_scan(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE)
        body = {"from": intents.INTENTS_EVM_TREASURY, "to": USDC_BASE, "value": "4398699", "data": "0x",
                "chainId": 8453}
        self.write(root, "evm", FIREWALL_URL + "/api/firewall", "POST", rpc.body_params(body),
                   {"status": "ok", "partial": False, "failed_sources": None, "classification": "SAFE"})
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(statuses(result)["recipient_code"], "unknown")
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn("failed_sources null", detail(result, "recipient_code"))

    def test_contract_code_without_a_readable_quote_is_unknown(self):
        result = judge(USDC_BASE, fixtures_root=self.root("evm", "goplus"), firewall_url=FIREWALL_URL)
        self.assertEqual(statuses(result)["quote_dry"], "unknown")
        self.assertEqual(statuses(result)["recipient_code"], "unknown")
        self.assertIn("no amountOut from the quote", detail(result, "recipient_code"))
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_a_quote_field_that_is_not_a_string_is_unknown_never_a_crash(self):
        body = intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE, "DESTINATION_CHAIN",
                                  DEADLINE)
        for field, value in (("amountOut", 1.5), ("amountOut", 4398699), ("amountOut", None),
                             ("minAmountOut", 1.5), ("minAmountOut", 4398699), ("correlationId", 1.5),
                             ("correlationId", ["x"]), ("timestamp", 1.5), ("timestamp", 1762171199),
                             (None, None)):
            with self.subTest(field=field, value=value):
                root = self.root("evm", "goplus")
                self.firewall_says(root, "SAFE")
                response = {"quote": {"amountOut": "4398699", "minAmountOut": "4354712"},
                            "quoteRequest": dict(body), "correlationId": "c", "timestamp": "2026-11-03T11:59:59Z"}
                if field:
                    (response["quote"] if field in ("amountOut", "minAmountOut") else response)[field] = value
                self.write(root, "intents", intents.ONECLICK_URL + "/v0/quote", "POST", rpc.body_params(body),
                           response)
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
                if field is None:
                    self.assertEqual(result["verdict"], "ALLOW")
                    continue
                self.assertEqual(statuses(result)["quote_dry"], "unknown")
                self.assertIn(field, detail(result, "quote_dry"))
                self.assertEqual(result["verdict"], "UNKNOWN")

    def test_an_amount_out_that_is_not_a_decimal_string_is_unknown(self):
        body = intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE, "DESTINATION_CHAIN",
                                  DEADLINE)
        for field, value in [(field, value) for field in ("amountOut", "minAmountOut")
                             for value in ("banana", "-1", " 5", "5 ", "1.5", "0x10", "01")] + [("amountOut", "0")]:
            with self.subTest(field=field, value=value):
                root = self.root("evm", "goplus")
                quote = dict({"amountOut": "4398699", "minAmountOut": "4354712"}, **{field: value})
                self.firewall_says(root, "SAFE", value=quote["amountOut"])
                self.write(root, "intents", intents.ONECLICK_URL + "/v0/quote", "POST", rpc.body_params(body),
                           {"quote": quote, "quoteRequest": dict(body), "timestamp": "2026-11-03T11:59:59Z",
                            "correlationId": "c"})
                result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
                if value == "0":
                    self.assertEqual(result["verdict"], "ALLOW")
                    continue
                self.assertEqual(statuses(result)["quote_dry"], "unknown")
                self.assertIn(f"{field} {json.dumps(value)} is not a decimal string", detail(result, "quote_dry"))
                self.assertEqual(result["verdict"], "UNKNOWN")

    def test_a_signal_from_a_partial_scan_is_still_a_signal(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE)
        self.firewall_says(root, "BLOCK_RECOMMENDED", status="unknown", partial=True, failed_sources=["structural"])
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(statuses(result)["recipient_code"], "signal")
        self.assertEqual(result["verdict"], "DENY")

    def test_an_unreadable_classification_list_is_unknown(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE)
        self.firewall_says(root, "SAFE")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(intents, "FIREWALL_CLASSIFICATIONS", Path(tmp) / "missing.json"):
            result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(statuses(result)["recipient_code"], "unknown")
        self.assertEqual(result["verdict"], "UNKNOWN")

    def test_an_answer_nested_too_deeply_to_encode_is_malformed(self):
        nested = []
        for _ in range(100000):
            nested = [nested]
        with self.assertRaises(rpc.MalformedResponse) as caught:
            intents._encodable({"quote": nested}, "1Click dry quote answer")
        self.assertEqual(str(caught.exception), "the 1Click dry quote answer is nested too deeply to encode")

    def test_the_recorded_contract_case(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE)
        result = judge(USDC_BASE, fixtures_root=root, firewall_url="https://api.shieldbotsecurity.online")
        self.assertEqual(statuses(result)["recipient_code"], "unknown")
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertIn('status "unknown", partial true, failed_sources ["structural"]', result["checks"][-1]["detail"])
        self.assertIn('classification "CAUTION"', result["checks"][-1]["detail"])
        self.assertIn("judged as a native value transfer of amountOut base units", result["checks"][-1]["detail"])
        self.assertIn("evidence_url", result["checks"][-1]["detail"])
        without = judge(USDC_BASE, fixtures_root=root)
        self.assertEqual(statuses(without)["recipient_code"], "unknown")
        self.assertIn("SHIELDBOT_API_URL is unset", without["checks"][-1]["detail"])


class ParseTest(unittest.TestCase):
    def test_parse_destination(self):
        self.assertEqual(intents.parse_destination(USDC_BASE_ASSET), ("base", USDC_BASE))
        self.assertEqual(intents.parse_destination("nep141:wrap.near"), ("near", "wrap.near"))
        self.assertEqual(intents.parse_destination("nep141:eth.omft.near"), ("eth", None))
        for bad in ("nep245:x", "nep141:", "nep141:x", "nep141:base-0x12.omft.near", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                intents.parse_destination(bad)

    def test_the_copied_constants(self):
        self.assertEqual(len(intents.THEFT_LABELS), 13)
        self.assertEqual(intents.THEFT_LABELS[:2], ("phishing_activities", "stealing_attack"))
        self.assertEqual(intents.CHAIN_IDS, {"eth": 1, "bsc": 56, "base": 8453, "arb": 42161, "op": 10,
                                             "pol": 137, "hood": 4663})
        self.assertEqual(set(intents.EVM_RPC_URLS), set(intents.CHAIN_IDS))
        self.assertEqual(intents.FIREWALL_SIGNALS, ("HIGH_RISK", "BLOCK_RECOMMENDED"))


if __name__ == "__main__":
    unittest.main()
