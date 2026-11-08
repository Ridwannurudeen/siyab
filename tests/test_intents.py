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
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import evidence, intents, rpc
from intel import sanctions

USDC_BASE_ASSET = "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
DEMO_RECIPIENT = "0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107"
DEPOSIT_ADDRESS = "deposit.near"
ONE_NEAR = "1000000000000000000000000"
CLOCK = datetime(2026, 11, 3, 12, 0, tzinfo=UTC).timestamp()
FIREWALL_URL = "https://api.example"


def no_network(*args, **kwargs):
    raise AssertionError("the test opened the network")


def reply(payload):
    handle = mock.MagicMock()
    handle.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return handle


def judge(recipient=DEMO_RECIPIENT, destination=USDC_BASE_ASSET, **options):
    return intents.judge_quote("nep141:wrap.near", destination, ONE_NEAR, recipient, clock=lambda: CLOCK,
                               **{"mode": "replay", **options})


def detail(result, check_id):
    return next(c["detail"] for c in result["checks"] if c["id"] == check_id)


def statuses(result):
    return {check["id"]: check["status"] for check in result["checks"]}


class Case(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("urllib.request.urlopen", side_effect=no_network)
        self.urlopen = patcher.start()
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

    def quote_for(self, root, recipient, amount_out="4398699", timestamp="2026-11-03T11:59:59Z"):
        body = intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, recipient, "DESTINATION_CHAIN",
                                  intents.RECORDED_DEADLINE)
        response = {"quote": {"amountOut": amount_out}, "quoteRequest": dict(body), "timestamp": timestamp}
        self.write(root, "intents", intents.ONECLICK_URL + "/v0/quote", "POST", rpc.body_params(body), response)

    def quote_for_destination(self, root, recipient, destination):
        body = intents.quote_body("nep141:wrap.near", destination, ONE_NEAR, recipient, "DESTINATION_CHAIN",
                                  intents.RECORDED_DEADLINE)
        response = {"quote": {"amountOut": "4398699"}, "quoteRequest": dict(body),
                    "timestamp": "2026-11-03T11:59:59Z"}
        self.write(root, "intents", intents.ONECLICK_URL + "/v0/quote", "POST", rpc.body_params(body), response)

    def status_for(self, root, deposit_address, quote_request):
        response = {"quoteResponse": {"quoteRequest": quote_request}}
        self.write(root, "intents", intents.ONECLICK_URL + "/v0/status", "GET",
                   {"depositAddress": deposit_address}, response)

    def firewall_says(self, root, classification, recipient=USDC_BASE, value="4398699"):
        body = {"from": intents.INTENTS_EVM_TREASURY, "to": recipient, "value": value, "data": "0x", "chainId": 8453}
        self.write(root, "evm", FIREWALL_URL + "/api/firewall", "POST", rpc.body_params(body),
                   {"classification": classification, "risk_score": 1, "evidence_hash": "0xab",
                    "evidence_url": FIREWALL_URL + "/evidence/0xab"})

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
                                            "destination_supported": "pass", "recipient_theft_label": "signal",
                                            "recipient_code": "pass", "ofac_sanctioned": "pass"})
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
        self.assertEqual(first["evidence_hash"], base64.b64decode("G4F0s9SH8j2yor2ioSob1r7vwJHKbsQnSeqbbfqLsMo=",
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
                                   "--deposit-address", "other.near"])
        self.assertEqual(code, 1)
        bind.assert_called_once()
        self.assertIn("originAsset", errors.getvalue())
        self.assertNotIn("verdict:", output.getvalue())


class BindingTest(Case):
    def later_request(self, **changes):
        """The quoteRequest of the quote an agent would deposit to after the verdict: the recorded one,
        not dry."""
        [path] = [p for p in (intents.FIXTURES_ROOT / "intents").glob("*.json")
                  if json.loads(p.read_text(encoding="utf-8"))["url"].endswith("/v0/quote")]
        request = dict(json.loads(path.read_text(encoding="utf-8"))["response"]["quoteRequest"], dry=False)
        request.update(changes)
        return {key: value for key, value in request.items() if value is not None}

    def allowed(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE)
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
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
        intents.verify_binding(judge(), self.later_request())
        intents.verify_binding(self.allowed(), self.later_request(recipient=USDC_BASE.upper().replace("0X", "0x")))

    def test_another_or_a_missing_field_is_refused(self):
        answer = self.allowed()
        for changes in ({"recipient": DEMO_RECIPIENT}, {"recipient": None}, {"amount": "2" + ONE_NEAR[1:]},
                        {"destinationAsset": "nep141:eth.omft.near"}, {"originAsset": None},
                        {"recipientType": "INTENTS"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                intents.verify_binding(answer, self.later_request(**{"recipient": USDC_BASE, **changes}))
        with self.assertRaises(ValueError):
            intents.verify_binding(answer, None)

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
        with self.assertRaisesRegex(ValueError, f"UNKNOWN verdict is refused.*quote age is "
                                    f"{intents.MAX_QUOTE_AGE_MS + 1} ms"):
            intents.verify_binding(result, self.later_request())

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
                intents.verify_binding(tampered, request)

    def test_a_rehashed_document_whose_subject_is_not_intents_is_refused(self):
        denied, request = judge(), self.later_request()
        forged = json.loads(json.dumps(denied))
        forged["document"]["subject"]["kind"] = "near"
        forged["evidence_hash"] = evidence.evidence_hash(forged["document"])
        with self.assertRaisesRegex(ValueError, "not an intents verdict"):
            intents.verify_binding(forged, request)

    def test_an_echo_that_differs_from_the_request_is_a_signal(self):
        root = self.root("evm", "goplus")
        body = intents.quote_body("nep141:wrap.near", USDC_BASE_ASSET, ONE_NEAR, USDC_BASE, "DESTINATION_CHAIN",
                                  intents.RECORDED_DEADLINE)
        self.write(root, "intents", intents.ONECLICK_URL + "/v0/quote", "POST", rpc.body_params(body),
                   {"quote": {"amountOut": "4398699"}, "quoteRequest": dict(body, amount="1")})
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(statuses(result)["quote_dry"], "signal")
        self.assertEqual(result["verdict"], "DENY")
        self.assertIn(f'amount "1", not "{ONE_NEAR}"', detail(result, "quote_dry"))


class DepositAddressTest(Case):
    def allowed(self, root):
        self.quote_for(root, USDC_BASE)
        self.goplus_says(root, USDC_BASE, {"code": 1, "result": {"data_source": "synthetic"}})
        self.firewall_says(root, "SAFE")
        result = judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)
        self.assertEqual(result["verdict"], "ALLOW")
        return result

    def test_a_matching_status_request_binds_the_supplied_address(self):
        root = self.root("evm", "goplus")
        result = self.allowed(root)
        quote_request = dict(result["document"]["subject"]["quote_request"])
        status_request = dict(quote_request, dry=False)
        self.status_for(root, DEPOSIT_ADDRESS, status_request)
        check = intents.bind_deposit_address(result, quote_request, DEPOSIT_ADDRESS, fixtures_root=root)
        intents.verify_binding(result, quote_request)
        self.assertEqual(check["status"], "pass")
        self.assertEqual(statuses(result)["deposit_address_status"], "pass")
        self.assertEqual(result["evidence_hash"], evidence.evidence_hash(result["document"]))
        key = rpc.fixture_key(intents.ONECLICK_URL + "/v0/status", "GET", {"depositAddress": DEPOSIT_ADDRESS})
        fixture = json.loads((root / "intents" / f"{key}.json").read_text(encoding="utf-8"))
        self.assertIs(fixture["synthetic"], True)
        self.assertEqual(fixture["response"], {"quoteResponse": {"quoteRequest": status_request}})

    def test_each_bound_field_mismatch_is_refused_with_its_field(self):
        mismatches = {"originAsset": "other-origin", "destinationAsset": "other-destination", "amount": "2",
                      "recipient": "0x0000000000000000000000000000000000000001", "recipientType": "INTENTS"}
        for field, value in mismatches.items():
            with self.subTest(field=field):
                root = self.root("evm", "goplus")
                result = self.allowed(root)
                quote_request = dict(result["document"]["subject"]["quote_request"])
                status_request = dict(quote_request, dry=False)
                status_request[field] = value
                self.status_for(root, DEPOSIT_ADDRESS, status_request)
                with self.assertRaisesRegex(ValueError, field):
                    intents.bind_deposit_address(result, quote_request, DEPOSIT_ADDRESS, fixtures_root=root)

    def test_a_404_means_the_address_is_unknown_to_1click(self):
        result = self.allowed(self.root("evm", "goplus"))
        quote_request = dict(result["document"]["subject"]["quote_request"])
        error = urllib.error.HTTPError("https://1click.example/v0/status", 404, "Not Found", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(ValueError, "unknown to 1Click"):
                intents.bind_deposit_address(result, quote_request, DEPOSIT_ADDRESS, mode="off")

    def test_failed_status_reads_are_unknown_and_refused(self):
        failures = (urllib.error.HTTPError("https://1click.example/v0/status", 401, "Unauthorized", {}, None),
                    urllib.error.HTTPError("https://1click.example/v0/status", 503, "Unavailable", {}, None),
                    OSError("offline"))
        for error in failures:
            with self.subTest(error=type(error).__name__, status=getattr(error, "code", None)):
                result = self.allowed(self.root("evm", "goplus"))
                quote_request = dict(result["document"]["subject"]["quote_request"])
                with mock.patch("urllib.request.urlopen", side_effect=error):
                    check = intents.bind_deposit_address(result, quote_request, DEPOSIT_ADDRESS, mode="off")
                self.assertEqual(check["status"], "unknown")
                self.assertEqual(result["verdict"], "UNKNOWN")
                with self.assertRaisesRegex(ValueError, "UNKNOWN deposit address status"):
                    intents.verify_binding(result, quote_request)


class FailClosedTest(Case):
    def test_a_goplus_failure_is_unknown_never_allow(self):
        root = self.root("intents", "evm")
        result = judge(fixtures_root=root)
        self.assertEqual(statuses(result)["recipient_theft_label"], "unknown")
        self.assertEqual(result["verdict"], "UNKNOWN")
        for body in ({"code": 0, "message": "error", "result": {}}, {"code": 1, "result": {}}, ["x"]):
            with self.subTest(body=body):
                self.goplus_says(root, DEMO_RECIPIENT, body)
                self.assertEqual(judge(fixtures_root=root)["verdict"], "UNKNOWN")

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
        for classification, verdict in (("BLOCK_RECOMMENDED", "DENY"), ("HIGH_RISK", "DENY"), ("CAUTION", "ALLOW")):
            with self.subTest(classification=classification):
                self.firewall_says(root, classification)
                self.assertEqual(judge(USDC_BASE, fixtures_root=root, firewall_url=FIREWALL_URL)["verdict"], verdict)

    def test_the_recorded_contract_case(self):
        root = self.root("evm", "goplus")
        self.quote_for(root, USDC_BASE)
        result = judge(USDC_BASE, fixtures_root=root, firewall_url="https://api.shieldbotsecurity.online")
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
