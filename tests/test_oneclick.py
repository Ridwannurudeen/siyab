"""Offline settlement boundaries, synthetic chain replies and recorded public signatures."""

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from gate import __main__ as gate_main
from gate import base58, evidence, intents, oneclick, rpc


ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "oneclick"
RECORDED = json.loads((ROOT / "dry-quote-20261121.json").read_text(encoding="utf-8"))
CONTRACT = json.loads((ROOT / "api-contract.json").read_text(encoding="utf-8"))
CLOCK = datetime(2026, 11, 21, 15, 2, 20, tzinfo=UTC).timestamp()
SENDER = "payer.near"
FLOOR = "280000"


def quote():
    answer = copy.deepcopy(RECORDED["response"])
    answer["quoteRequest"]["dry"] = False
    answer["quoteRequest"]["refundTo"] = SENDER
    answer["quote"].update(depositAddress="siyab-synthetic-deposit.near",
                           deadline=answer["quoteRequest"]["deadline"],
                           timeWhenInactive=answer["quoteRequest"]["deadline"])
    return answer


def judgement(response):
    request = dict(response["quoteRequest"], dry=True)
    document = evidence.document(
        subject={"kind": "intents", "origin_asset": request["originAsset"],
                 "destination_asset": request["destinationAsset"], "amount": request["amount"],
                 "recipient": request["recipient"], "recipient_type": request["recipientType"],
                 "quote_request": request,
                 "quote": {"correlation_id": response["correlationId"], "timestamp": response["timestamp"],
                           "amount_out": response["quote"]["amountOut"],
                           "min_amount_out": response["quote"]["minAmountOut"]}},
        checks=[], labels=[], verdict="ALLOW", reasons=[], observed={"timestamp_ms": int(CLOCK * 1000)},
        issued_ms=None, expires_ms=None, verdict_id=None, latency_ms=None)
    document["kind"] = "intents"
    return {"verdict": "ALLOW", "quote": copy.deepcopy(response["quote"]), "checks": [], "reasons": [],
            "document": document, "evidence_hash": evidence.evidence_hash(document)}


def status(response, lifecycle="PENDING_DEPOSIT"):
    return {"correlationId": response["correlationId"], "quoteResponse": copy.deepcopy(response),
            "status": lifecycle, "updatedAt": "2026-11-21T15:02:20Z", "swapDetails": {
                "intentHashes": [], "nearTxHashes": [], "originChainTxHashes": [],
                "destinationChainTxHashes": []}}


TX_HASH = "0x" + "a" * 64
BLOCK_HASH = "0x" + "b" * 64


def success(response, deposit_hash=None):
    answer = status(response, "SUCCESS")
    answer["swapDetails"].update(amountIn=response["quote"]["amountIn"],
                                 depositedAmount=response["quote"]["amountIn"],
                                 amountOut="285000", refundedAmount="0", refundReason=None,
                                 destinationChainTxHashes=[{"hash": TX_HASH, "explorerUrl": ""}])
    if deposit_hash:
        answer["swapDetails"]["originChainTxHashes"] = [{"hash": deposit_hash, "explorerUrl": ""}]
    return answer


def evm_client(response):
    topic = json.loads((ROOT / "erc20-transfer.json").read_text(encoding="utf-8"))["topic"]
    recipient = response["quoteRequest"]["recipient"]
    token = intents.parse_destination(response["quoteRequest"]["destinationAsset"])[1]
    log = {"address": token, "topics": [topic, "0x" + "0" * 64, "0x" + "0" * 24 + recipient[2:]],
           "data": "0x" + format(285000, "064x"), "removed": False, "transactionHash": TX_HASH,
           "blockHash": BLOCK_HASH, "logIndex": "0x0"}
    receipt = {"status": "0x1", "transactionHash": TX_HASH, "blockNumber": "0x64",
               "blockHash": BLOCK_HASH, "logs": [log]}
    replies = {"eth_chainId": "0x2105", "eth_getTransactionReceipt": receipt,
               "eth_getBlockByNumber": {"number": "0x64", "hash": BLOCK_HASH, "timestamp": hex(int(CLOCK))}}
    client = mock.Mock(url="https://synthetic.invalid")
    def call(method, params):
        result = copy.deepcopy(replies[method])
        if result is None:
            raise rpc.MalformedResponse("the JSON-RPC body has no result")
        return result
    client.call.side_effect = call
    return client, replies


def deposit_result(deposit_hash, response, registration=0):
    args = {"receiver_id": response["quote"]["depositAddress"], "amount": response["quote"]["amountIn"]}
    result = {"transaction": {"hash": deposit_hash, "signer_id": SENDER, "receiver_id": "wrap.near",
                             "actions": [{"FunctionCall": {"method_name": "ft_transfer", "deposit": "1",
                                  "gas": oneclick.GAS, "args": base64.b64encode(json.dumps(args).encode()).decode()}}]},
            "final_execution_status": "FINAL", "status": {"SuccessValue": ""}}
    if registration:
        args = {"account_id": response["quote"]["depositAddress"], "registration_only": True}
        result["transaction"]["actions"].insert(0, {"FunctionCall": {
            "method_name": "storage_deposit", "deposit": str(registration), "gas": 30000000000000,
            "args": base64.b64encode(json.dumps(args).encode()).decode()}})
    return result


def transaction_signer():
    return mock.Mock(public_key_hex=bytes(range(32)).hex(), sign=mock.Mock(return_value=bytes(64)))


class SignatureTests(unittest.TestCase):
    def recorded(self):
        record = json.loads((ROOT / "signed-dry-quote-20261121.json").read_text(encoding="utf-8"))
        response = record["response"]
        response["signature"] = "ed25519:" + base58.encode(bytes(record["signature_bytes"]))
        return response

    def test_recorded_public_signature_verifies(self):
        self.assertTrue(oneclick.verify_quote_signature(self.recorded()))

    def test_recorded_allocated_quote_has_a_verified_supported_shape(self):
        record = json.loads((ROOT / "allocated-quote-20261121.json").read_text(encoding="utf-8"))
        response = record["response"]
        response["signature"] = "ed25519:" + base58.encode(bytes(record["signature_bytes"]))
        self.assertTrue(oneclick.verify_quote_signature(response))
        checked, request = oneclick._quote_shape(response)
        self.assertEqual(checked["depositAddress"], response["quote"]["depositAddress"])
        self.assertEqual(request["originAsset"], oneclick.ORIGIN)

    def test_economic_fields_and_timestamp_signature_tampering_refused(self):
        for holder, field, value in (("quote", "amountOut", "1"), ("quote", "minAmountOut", "1"),
                                     ("quoteRequest", "recipient", "0x" + "2" * 40),
                                     ("quoteRequest", "refundTo", "other.near"),
                                     (None, "timestamp", "2026-11-21T15:14:00Z"),
                                     (None, "signature", "ed25519:bad")):
            with self.subTest(holder=holder, field=field):
                response = self.recorded()
                (response[holder] if holder else response)[field] = value
                self.assertFalse(oneclick.verify_quote_signature(response))

    def test_official_sdk_signature_vectors_and_wet_tampering(self):
        record = json.loads((ROOT / "sdk-signature-vectors.json").read_text(encoding="utf-8"))
        with mock.patch("gate.oneclick.MANAGER_PUBLIC_KEY", record["public_key"]):
            for vector in record["vectors"]:
                with self.subTest(vector=vector["name"]):
                    response = copy.deepcopy(vector["response"])
                    response["signature"] = "ed25519:" + base58.encode(bytes(vector["signature_bytes"]))
                    self.assertTrue(oneclick.verify_quote_signature(response))
                    if not response["quoteRequest"]["dry"]:
                        for field, value in (("depositAddress", "other.near"), ("withdrawFee", "999999999")):
                            changed = copy.deepcopy(response)
                            changed["quote"][field] = value
                            self.assertFalse(oneclick.verify_quote_signature(changed))
                    response["quoteRequest"]["appFees"] = [{"recipient": "other.near", "fee": 999}]
                    self.assertTrue(oneclick.verify_quote_signature(response))


class Case(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        patcher = mock.patch("gate.oneclick.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network in offline test"))
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("gate.rpc._open_with_deadline", side_effect=AssertionError("network in offline test"))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("gate.oneclick.verify_quote_signature", return_value=True, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class QuoteTests(Case):
    def test_judge_quote_signature_verifier_failure_is_unknown(self):
        body = RECORDED["request"]["body"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("intents", "goplus", "evm"):
                (root / name).mkdir()
            params = rpc.body_params(body)
            url = intents.ONECLICK_URL + "/v0/quote"
            fixture = {"url": url, "request": {"method": "POST", "params": params},
                       "fetched_at": RECORDED["fetched_at"], "response": RECORDED["response"]}
            (root / "intents" / (rpc.fixture_key(url, "POST", params) + ".json")).write_text(json.dumps(fixture), encoding="utf-8")
            verifier = mock.Mock(return_value=False)
            result = intents.judge_quote(body["originAsset"], body["destinationAsset"], body["amount"], body["recipient"],
                                        mode="replay", fixtures_root=root, clock=lambda: CLOCK, deadline=body["deadline"],
                                        quote_verifier=verifier)
        verifier.assert_called_once_with(RECORDED["response"])
        check = next(check for check in result["checks"] if check["id"] == "quote_dry")
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertEqual(check["status"], "unknown")
        self.assertIn("signature did not verify", check["detail"])
        self.assertIsNone(result["document"]["subject"]["quote"])

    def test_checked_recorded_quote_mutation(self):
        response = quote()
        self.assertEqual(oneclick.check_quote(judgement(response), response, min_amount_out=FLOOR,
                                             clock=lambda: CLOCK), response["quote"])

    def test_owner_floor_is_met_even_if_withdrawal_fee_is_still_due(self):
        response = quote()
        minimum = int(response["quote"]["minAmountOut"]) - int(response["quote"]["withdrawFee"])
        oneclick.check_quote(judgement(response), response, min_amount_out=str(minimum), clock=lambda: CLOCK)
        with self.assertRaisesRegex(ValueError, "owner minimum"):
            oneclick.check_quote(judgement(response), response, min_amount_out=str(minimum + 1), clock=lambda: CLOCK)

    def test_present_malformed_withdrawal_fee_never_passes_owner_floor(self):
        response = quote()
        for fee in (None, True, 0, "", "01", "-1", "1.0", "unavailable"):
            with self.subTest(fee=fee):
                changed = copy.deepcopy(response)
                changed["quote"]["withdrawFee"] = fee
                with self.assertRaises(ValueError):
                    oneclick.check_quote(judgement(response), changed, min_amount_out="1", clock=lambda: CLOCK)

    def test_deposit_address_expiry_can_follow_refund_deadline(self):
        response = quote()
        judged = judgement(response)
        response["quote"]["deadline"] = "2026-11-24T15:12:13.601Z"
        response["quote"]["timeWhenInactive"] = response["quote"]["deadline"]
        self.assertEqual(oneclick.check_quote(judged, response, min_amount_out=FLOOR, clock=lambda: CLOCK), response["quote"])

    def test_refund_deadline_needs_deposit_safety_margin(self):
        response = quote()
        response["quoteRequest"]["deadline"] = datetime.fromtimestamp(CLOCK + 10, UTC).isoformat()
        response["quote"]["deadline"] = "2026-11-24T15:12:13.601Z"
        response["quote"]["timeWhenInactive"] = response["quote"]["deadline"]
        with self.assertRaises(ValueError):
            oneclick.check_quote(judgement(response), response, min_amount_out=FLOOR, clock=lambda: CLOCK)

    def test_deadline_margin_covers_transport_and_clock_skew(self):
        for seconds in (119, 124, 125):
            with self.subTest(seconds=seconds):
                response = quote()
                response["quoteRequest"]["deadline"] = datetime.fromtimestamp(CLOCK + seconds, UTC).isoformat()
                response["quote"]["deadline"] = response["quoteRequest"]["deadline"]
                response["quote"]["timeWhenInactive"] = response["quoteRequest"]["deadline"]
                if seconds < 125:
                    with self.assertRaisesRegex(ValueError, "120 seconds"):
                        oneclick.check_quote(judgement(response), response, min_amount_out=FLOOR, clock=lambda: CLOCK)
                else:
                    oneclick.check_quote(judgement(response), response, min_amount_out=FLOOR, clock=lambda: CLOCK)

    def test_quote_clock_skew_is_bounded_and_read_only_default_stays_strict(self):
        for seconds in (5, 5.001, -300, -305, -305.001):
            with self.subTest(seconds=seconds):
                response = quote()
                response["timestamp"] = datetime.fromtimestamp(CLOCK + seconds, UTC).isoformat()
                judged = judgement(response)
                if seconds > 5 or seconds < -305:
                    with self.assertRaises(ValueError):
                        oneclick.check_quote(judged, response, min_amount_out=FLOOR, clock=lambda: CLOCK)
                else:
                    oneclick.check_quote(judged, response, min_amount_out=FLOOR, clock=lambda: CLOCK)
                if seconds > 0 or seconds < -300:
                    with self.assertRaises(ValueError):
                        intents.verify_binding(judged, judged["document"]["subject"]["quote_request"], clock=lambda: CLOCK)

    def test_dry_floor_allows_only_a_small_tick_and_never_lowers_owner_floor(self):
        response = quote()
        judged = judgement(response)
        minimum = int(response["quote"]["minAmountOut"])
        tick_floor = (minimum * 9975 + 9999) // 10000
        for floor, output, allowed in (("1", tick_floor, True), ("1", tick_floor - 1, False),
                                       (str(minimum), tick_floor, False)):
            with self.subTest(owner_floor=floor, output=output):
                changed = copy.deepcopy(response)
                changed["quote"]["minAmountOut"] = str(output)
                changed["quote"]["amountOut"] = str((output * 10000 + 9899) // 9900)
                if allowed:
                    oneclick.check_quote(judged, changed, min_amount_out=floor, clock=lambda: CLOCK)
                else:
                    with self.assertRaises(ValueError):
                        oneclick.check_quote(judged, changed, min_amount_out=floor, clock=lambda: CLOCK)

    def test_request_rebinding(self):
        mutations = {"recipient": "0x" + "2" * 40, "amount": "200000000000000000000000",
                     "refundTo": "other.near", "destinationAsset": "nep141:wrap.near",
                     "deadline": "2026-11-21T15:13:13Z", "slippageTolerance": 101,
                     "appFees": [{"recipient": "fees.near", "fee": 1}]}
        response = quote()
        judged = judgement(response)
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(response)
                changed["quoteRequest"][field] = value
                with self.assertRaises(ValueError):
                    oneclick.check_quote(judged, changed, min_amount_out=FLOOR, clock=lambda: CLOCK)

    def test_unsafe_quote_fields(self):
        response = quote()
        judged = judgement(response)
        mutations = [("depositAddress", ""), ("depositAddress", "0x" + "1" * 40),
                     ("depositMemo", "memo"), ("amountIn", "999"), ("minAmountIn", "999"),
                     ("amountOut", "279999"), ("minAmountOut", "279999"),
                     ("minAmountOut", "999999"), ("refundFee", "-1"), ("withdrawFee", "garbage"),
                     ("refundFee", "1"), ("withdrawFee", "2401"),
                     ("deadline", "2026-11-21T15:02:00Z"),
                     ("timeWhenInactive", "2026-11-21T15:02:00Z")]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(response)
                changed["quote"][field] = value
                with self.assertRaises(ValueError):
                    oneclick.check_quote(judged, changed, min_amount_out=FLOOR, clock=lambda: CLOCK)

    def test_refuses_stale_dry_and_nonallow(self):
        response = quote()
        for kind in ("stale", "dry", "denied", "unknown", "edited_evidence"):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(response)
                judged = judgement(response)
                now = CLOCK
                if kind == "stale":
                    now += 1200
                elif kind == "dry":
                    changed["quoteRequest"]["dry"] = True
                elif kind in ("denied", "unknown"):
                    judged["verdict"] = judged["document"]["verdict"] = kind.upper()
                    judged["evidence_hash"] = evidence.evidence_hash(judged["document"])
                else:
                    judged["document"]["verdict"] = "DENY"
                with self.assertRaises(ValueError):
                    oneclick.check_quote(judged, changed, min_amount_out=FLOOR, clock=lambda: now)

    def test_missing_required_quote_fields(self):
        response = quote()
        judged = judgement(response)
        for field in ("depositAddress", "amountIn", "minAmountIn", "amountOut", "minAmountOut",
                      "deadline", "refundFee", "withdrawFee"):
            with self.subTest(field=field):
                changed = copy.deepcopy(response)
                del changed["quote"][field]
                with self.assertRaises(ValueError):
                    oneclick.check_quote(judged, changed, min_amount_out=FLOOR, clock=lambda: CLOCK)


class StatusTests(Case):
    def test_status_clock_skew_is_bounded(self):
        response = quote()
        for seconds in (5, 5.001):
            with self.subTest(seconds=seconds):
                answer = status(response)
                answer["updatedAt"] = datetime.fromtimestamp(CLOCK + seconds, UTC).isoformat()
                if seconds <= 5:
                    self.assertEqual(oneclick.check_status(response, answer, clock=lambda: CLOCK), "PENDING_DEPOSIT")
                else:
                    with self.assertRaises(ValueError):
                        oneclick.check_status(response, answer, clock=lambda: CLOCK)

    def test_documented_status_request_normalization(self):
        response = quote()
        for correlation in (None, "status-normalized-correlation"):
            with self.subTest(correlation=correlation):
                answer = status(response)
                nested = answer["quoteResponse"]
                if correlation is None:
                    del nested["correlationId"]
                else:
                    nested["correlationId"] = correlation
                for field in ("virtualChainRecipient", "virtualChainRefundRecipient", "referral"):
                    nested["quoteRequest"][field] = None
                for field in ("insured", "quoteWaitingTimeMs"):
                    nested["quoteRequest"].pop(field, None)
                self.assertEqual(oneclick.check_status(response, answer, clock=lambda: CLOCK), "PENDING_DEPOSIT")

    def test_every_documented_status_has_action(self):
        values = CONTRACT["schemas"]["GetExecutionStatusResponse"]["properties"]["status"]["enum"]
        self.assertEqual(set(oneclick.STATUS_ACTIONS), set(values))
        for value in values:
            with self.subTest(status=value):
                response = quote()
                self.assertEqual(oneclick.check_status(response, status(response, value), clock=lambda: CLOCK), value)

    def test_only_pending_allows_first_deposit(self):
        for value in oneclick.STATUS_ACTIONS:
            with self.subTest(status=value):
                response = quote()
                if value == "PENDING_DEPOSIT":
                    self.assertEqual(oneclick.check_status(response, status(response, value), before_deposit=True,
                                                         clock=lambda: CLOCK), value)
                else:
                    with self.assertRaises(ValueError):
                        oneclick.check_status(response, status(response, value), before_deposit=True,
                                              clock=lambda: CLOCK)

    def test_rebound_and_missing_status(self):
        response = quote()
        mutations = [None, {}, {"status": "SUCCESS"}, status(response, "NEW_STATUS"),
                     json.loads((ROOT / "status-404-20261121.json").read_text(encoding="utf-8"))["response"]]
        for field, value in (("depositAddress", "other.near"), ("amountIn", "1"), ("amountOut", "1"),
                             ("minAmountOut", "1"), ("withdrawFee", "1")):
            changed = status(response)
            changed["quoteResponse"]["quote"][field] = value
            mutations.append(changed)
        changed = status(response)
        changed["quoteResponse"]["quoteRequest"]["recipient"] = "0x" + "2" * 40
        mutations.append(changed)
        for index, changed in enumerate(mutations):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    oneclick.check_status(response, changed, clock=lambda: CLOCK)

    def test_pending_with_existing_funds_or_transactions_cannot_deposit(self):
        response = quote()
        for field, value in (("depositedAmount", "1"), ("amountIn", "1"), ("amountOut", "1"),
                             ("refundedAmount", "1"), ("intentHashes", ["known"]),
                             ("originChainTxHashes", [{"hash": "known", "explorerUrl": ""}])):
            with self.subTest(field=field):
                answer = status(response)
                answer["swapDetails"][field] = value
                with self.assertRaises(ValueError):
                    oneclick.check_status(response, answer, before_deposit=True, clock=lambda: CLOCK)

    def test_status_time_and_detail_shapes_fail_closed(self):
        response = quote()
        mutations = [("updatedAt", "2026-11-21T15:03:00Z"), ("updatedAt", "2026-11-21T15:01:00Z"),
                     ("updatedAt", None), ("swapDetails", None), ("quoteResponse", None), ("correlationId", "")]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                answer = status(response)
                answer[field] = value
                with self.assertRaises(ValueError):
                    oneclick.check_status(response, answer, clock=lambda: CLOCK)


class ReceiptTests(Case):
    def test_receipt_topic_is_the_recorded_erc20_constant(self):
        record = json.loads((ROOT / "erc20-transfer.json").read_text(encoding="utf-8"))
        self.assertEqual(oneclick.TRANSFER_TOPIC, record["verification"]["result"])

    def test_real_client_null_receipt_is_pending_and_other_malformed_replies_refuse(self):
        response = quote()
        client, replies = evm_client(response)
        real = rpc.Client(client.url, "off")
        def exchange(url, method, params, body):
            return {"jsonrpc": "2.0", "id": "gate", "result": copy.deepcopy(replies[method])}
        with mock.patch.object(real, "_exchange", side_effect=exchange):
            replies["eth_getTransactionReceipt"] = None
            with self.assertRaises(oneclick.SettlementPending):
                oneclick.destination_receipt(response, success(response), client=real)
        error = rpc.MalformedResponse("the JSON-RPC body carries both a result and an error")
        def malformed(method, params):
            if method == "eth_getTransactionReceipt":
                raise error
            return copy.deepcopy(replies[method])
        client.call.side_effect = malformed
        with self.assertRaises(rpc.MalformedResponse) as raised:
            oneclick.destination_receipt(response, success(response), client=client)
        self.assertIs(raised.exception, error)

    def test_actual_output_and_recipient_proven_by_finalized_logs(self):
        response = quote()
        client, _ = evm_client(response)
        result = oneclick.destination_receipt(response, success(response), client=client)
        self.assertEqual(result["amount"], "285000")
        self.assertEqual(result["recipient"], response["quoteRequest"]["recipient"])

    def test_recorded_better_fill_reconciles_to_the_exact_receipt(self):
        record = json.loads((ROOT / "status-near-success-20261121.json").read_text(encoding="utf-8"))["response"]
        # Only the recorded economics are reused; the supported EVM route and receipt are synthetic.
        response = quote()
        for field in ("amountOut", "minAmountOut"):
            response["quote"][field] = record["quoteResponse"]["quote"][field]
        response["quote"]["withdrawFee"] = "0"
        settled = success(response)
        settled["swapDetails"]["amountOut"] = record["swapDetails"]["amountOut"]
        self.assertEqual((response["quote"]["amountOut"], settled["swapDetails"]["amountOut"]), ("132894", "133142"))
        client, replies = evm_client(response)
        log = replies["eth_getTransactionReceipt"]["logs"][0]
        log["data"] = "0x" + format(int(settled["swapDetails"]["amountOut"]), "064x")
        self.assertEqual(oneclick.destination_receipt(response, settled, client=client)["amount"], "133142")
        log["data"] = "0x" + format(133141, "064x")
        with self.assertRaisesRegex(ValueError, "receipt amount"):
            oneclick.destination_receipt(response, settled, client=client)

    def test_matching_receipt_below_the_quoted_minimum_is_refused(self):
        response = quote()
        settled = success(response)
        amount = int(response["quote"]["minAmountOut"]) - 1
        settled["swapDetails"]["amountOut"] = str(amount)
        client, replies = evm_client(response)
        replies["eth_getTransactionReceipt"]["logs"][0]["data"] = "0x" + format(amount, "064x")
        with self.assertRaisesRegex(ValueError, "minimum"):
            oneclick.destination_receipt(response, settled, client=client)

    def test_unproven_or_mismatched_receipts(self):
        for mutation in ("chain", "missing", "failed", "hash", "unfinalized", "canonical", "amount",
                         "recipient", "asset", "removed", "duplicate", "debit", "malformed"):
            with self.subTest(mutation=mutation):
                response = quote()
                client, replies = evm_client(response)
                receipt = replies["eth_getTransactionReceipt"]
                log = receipt["logs"][0]
                if mutation == "chain":
                    replies["eth_chainId"] = "0x1"
                elif mutation == "missing":
                    replies["eth_getTransactionReceipt"] = None
                elif mutation == "failed":
                    receipt["status"] = "0x0"
                elif mutation == "hash":
                    receipt["transactionHash"] = "0x" + "c" * 64
                elif mutation == "unfinalized":
                    receipt["blockNumber"] = "0x65"
                elif mutation == "canonical":
                    replies["eth_getBlockByNumber"]["hash"] = "0x" + "c" * 64
                elif mutation == "amount":
                    log["data"] = "0x" + format(284999, "064x")
                elif mutation == "recipient":
                    log["topics"][2] = "0x" + "0" * 24 + "2" * 40
                elif mutation == "asset":
                    log["address"] = "0x" + "2" * 40
                elif mutation == "removed":
                    log["removed"] = True
                elif mutation == "duplicate":
                    receipt["logs"].append(copy.deepcopy(log))
                elif mutation == "debit":
                    debit = copy.deepcopy(log)
                    debit["topics"][1], debit["topics"][2] = debit["topics"][2], debit["topics"][1]
                    debit["logIndex"] = "0x1"
                    receipt["logs"].append(debit)
                else:
                    log["data"] = "0x12"
                with self.assertRaises(ValueError):
                    oneclick.destination_receipt(response, success(response), client=client)

    def test_success_provider_amounts_and_refunds(self):
        response = quote()
        for field, value in (("amountIn", "1"), ("depositedAmount", "1"), ("amountOut", "1"),
                             ("amountOut", "999999"), ("refundedAmount", "1"), ("refundReason", "refund"),
                             ("destinationChainTxHashes", [])):
            with self.subTest(field=field, value=value):
                answer = success(response)
                answer["swapDetails"][field] = value
                client, _ = evm_client(response)
                with self.assertRaises(ValueError):
                    oneclick.destination_receipt(response, answer, client=client)

    def test_receipt_predating_quote_is_refused(self):
        response = quote()
        client, replies = evm_client(response)
        replies["eth_getBlockByNumber"]["timestamp"] = hex(int(CLOCK) - 60)
        with self.assertRaises(ValueError):
            oneclick.destination_receipt(response, success(response), client=client)

    def test_native_output_requires_exact_direct_transfer(self):
        response = quote()
        response["quoteRequest"]["destinationAsset"] = "nep141:base.omft.near"
        for mutation in (None, "recipient", "value", "input", "block"):
            with self.subTest(mutation=mutation):
                client, replies = evm_client(response)
                recipient = response["quoteRequest"]["recipient"]
                replies["eth_getTransactionReceipt"]["to"] = recipient
                tx = {"hash": TX_HASH, "blockHash": BLOCK_HASH, "to": recipient, "input": "0x", "value": hex(285000)}
                replies["eth_getTransactionByHash"] = tx
                if mutation == "recipient":
                    tx["to"] = "0x" + "2" * 40
                elif mutation == "value":
                    tx["value"] = hex(284999)
                elif mutation == "input":
                    tx["input"] = "0x1234"
                elif mutation == "block":
                    tx["blockHash"] = "0x" + "c" * 64
                if mutation is None:
                    self.assertEqual(oneclick.destination_receipt(response, success(response), client=client)["amount"], "285000")
                else:
                    with self.assertRaises(ValueError):
                        oneclick.destination_receipt(response, success(response), client=client)


class SigningTests(Case):
    def funding_client(self):
        client = mock.Mock()
        client.call_function.side_effect = lambda token, method, args: (
            {"total": "1250000000000000000000"} if method == "storage_balance_of" else str(oneclick.MAX_AMOUNT))
        client.view_account.return_value = {"amount": "100000000000000000000000"}
        client.call.return_value = {"permission": "FullAccess", "nonce": 7,
                                    "block_hash": base58.encode(bytes(range(32))), "block_height": 200000000}
        return client

    def test_fresh_receiver_gets_only_capped_registration_before_transfer(self):
        response = quote()
        client = self.funding_client()
        storage = 1250000000000000000000
        def view(token, method, args):
            if method == "storage_balance_of":
                return {"total": str(storage)} if args["account_id"] == SENDER else None
            if method == "storage_balance_bounds":
                return {"min": str(storage), "max": str(storage)}
            return str(oneclick.MAX_AMOUNT)
        client.call_function.side_effect = view
        digest, encoded, registration, reference = oneclick._signed_deposit(SENDER, response, client=client,
                                                                 tx_signer=transaction_signer())
        self.assertEqual(registration, storage)
        self.assertEqual(reference, {"block_hash": base58.encode(bytes(range(32))), "block_height": 200000000,
                                     "public_key": base58.encode_key(bytes(range(32))), "nonce": 8})
        data = base64.b64decode(encoded)[:-65]
        self.assertEqual(base58.decode(digest), hashlib.sha256(data).digest())
        offset = 4 + len(SENDER) + 1 + 32 + 8 + 4 + len("wrap.near") + 32
        count = int.from_bytes(data[offset:offset + 4], "little")
        self.assertEqual(count, 2)
        offset += 4
        calls = []
        for _ in range(count):
            self.assertEqual(data[offset], 2)
            offset += 1
            fields = []
            for _ in range(2):
                length = int.from_bytes(data[offset:offset + 4], "little")
                offset += 4
                fields.append(data[offset:offset + length].decode())
                offset += length
            gas = int.from_bytes(data[offset:offset + 8], "little")
            deposit = int.from_bytes(data[offset + 8:offset + 24], "little")
            offset += 24
            calls.append((fields[0], json.loads(fields[1]), gas, deposit))
        self.assertEqual(offset, len(data))
        self.assertEqual(calls, [
            ("storage_deposit", {"account_id": response["quote"]["depositAddress"], "registration_only": True},
             30000000000000, storage),
            ("ft_transfer", {"receiver_id": response["quote"]["depositAddress"], "amount": response["quote"]["amountIn"]},
             oneclick.GAS, 1)])

    def test_registration_bounds_and_sender_storage_fail_closed(self):
        for bounds in (None, {}, {"min": "0", "max": "0"},
                       {"min": "1250000000000000000001", "max": "1250000000000000000001"},
                       {"min": "1250000000000000000000", "max": None}):
            with self.subTest(bounds=bounds):
                client = self.funding_client()
                def view(token, method, args):
                    if method == "storage_balance_of":
                        return {"total": "1"} if args["account_id"] == SENDER else None
                    if method == "storage_balance_bounds":
                        return bounds
                    return str(oneclick.MAX_AMOUNT)
                client.call_function.side_effect = view
                tx_signer = transaction_signer()
                with self.assertRaises(ValueError):
                    oneclick._signed_deposit(SENDER, quote(), client=client, tx_signer=tx_signer)
                tx_signer.sign.assert_not_called()

    def test_signed_transaction_contains_only_exact_transfer(self):
        response = quote()
        tx_signer = transaction_signer()
        digest, encoded, registration, _ = oneclick._signed_deposit(SENDER, response, client=self.funding_client(), tx_signer=tx_signer)
        self.assertEqual(registration, 0)
        signed = base64.b64decode(encoded)
        self.assertEqual(base58.decode(digest), hashlib.sha256(signed[:-65]).digest())
        self.assertIn(b"ft_transfer", signed)
        self.assertIn(b'"receiver_id":"siyab-synthetic-deposit.near"', signed)
        self.assertIn(response["quote"]["amountIn"].encode(), signed)
        self.assertEqual(signed[-89:-81], oneclick.GAS.to_bytes(8, "little"))
        self.assertEqual(signed[-81:-65], (1).to_bytes(16, "little"))

    def test_funding_and_full_access_required(self):
        for mutation in ("storage", "token", "gas", "permission", "nonce", "block"):
            with self.subTest(mutation=mutation):
                client = self.funding_client()
                if mutation == "storage":
                    client.call_function.side_effect = None
                    client.call_function.return_value = None
                elif mutation == "token":
                    client.call_function.side_effect = lambda token, method, args: ({"total": "1"}
                                                                 if method == "storage_balance_of" else "0")
                elif mutation == "gas":
                    client.view_account.return_value = {"amount": "0"}
                elif mutation == "permission":
                    client.call.return_value["permission"] = {"FunctionCall": {}}
                elif mutation == "nonce":
                    client.call.return_value["nonce"] = True
                else:
                    client.call.return_value["block_hash"] = "bad"
                with self.assertRaises(ValueError):
                    oneclick._signed_deposit(SENDER, quote(), client=client, tx_signer=transaction_signer())

    def test_deposit_result_must_match_finalized_exact_call(self):
        response = quote()
        digest = base58.encode(bytes(range(32)))
        state = {"deposit_hash": digest, "sender": SENDER, "amount": response["quote"]["amountIn"], "quote_response": response}
        for mutation in (None, "hash", "sender", "receiver", "finality", "failure", "extra_action", "method", "gas", "deposit", "args"):
            with self.subTest(mutation=mutation):
                result = deposit_result(digest, response)
                tx = result["transaction"]
                call = tx["actions"][0]["FunctionCall"]
                if mutation == "hash":
                    tx["hash"] = "other"
                elif mutation == "sender":
                    tx["signer_id"] = "other.near"
                elif mutation == "receiver":
                    tx["receiver_id"] = "other.near"
                elif mutation == "finality":
                    result["final_execution_status"] = "EXECUTED_OPTIMISTIC"
                elif mutation == "failure":
                    result["status"] = {"Failure": None}
                elif mutation == "extra_action":
                    tx["actions"].append(copy.deepcopy(tx["actions"][0]))
                elif mutation == "method":
                    call["method_name"] = "ft_transfer_call"
                elif mutation == "gas":
                    call["gas"] += 1
                elif mutation == "deposit":
                    call["deposit"] = "2"
                elif mutation == "args":
                    call["args"] = base64.b64encode(b'{"receiver_id":"other.near","amount":"1"}').decode()
                if mutation is None:
                    oneclick._deposit_result(state, result)
                else:
                    with self.assertRaises(ValueError):
                        oneclick._deposit_result(state, result)

    def test_registration_result_must_match_recorded_receiver_budget_and_args(self):
        response = quote()
        digest = base58.encode(bytes(range(32)))
        state = {"deposit_hash": digest, "sender": SENDER, "amount": response["quote"]["amountIn"],
                 "quote_response": response, "storage_deposit": "1250000000000000000000"}
        expected = {"account_id": response["quote"]["depositAddress"], "registration_only": True}
        for mutation in (None, "budget", "gas", "receiver", "flag", "integer_flag", "method", "missing", "extra"):
            with self.subTest(mutation=mutation):
                result = deposit_result(digest, response)
                args = dict(expected)
                call = {"method_name": "storage_deposit", "gas": 30000000000000,
                        "deposit": state["storage_deposit"]}
                if mutation == "budget":
                    call["deposit"] = "1250000000000000000001"
                elif mutation == "gas":
                    call["gas"] += 1
                elif mutation == "receiver":
                    args["account_id"] = "other.near"
                elif mutation == "flag":
                    args["registration_only"] = False
                elif mutation == "integer_flag":
                    args["registration_only"] = 1
                elif mutation == "method":
                    call["method_name"] = "near_deposit"
                call["args"] = base64.b64encode(json.dumps(args).encode()).decode()
                if mutation != "missing":
                    result["transaction"]["actions"].insert(0, {"FunctionCall": call})
                if mutation == "extra":
                    result["transaction"]["actions"].append({"Transfer": {"deposit": "1"}})
                if mutation is None:
                    oneclick._deposit_result(state, result)
                else:
                    with self.assertRaises(ValueError):
                        oneclick._deposit_result(state, result)

    def test_final_failure_requires_original_receipt_rollback(self):
        response = quote()
        digest = base58.encode(bytes(range(32)))
        receipt_id = base58.encode(bytes(reversed(range(32))))
        failure = {"Failure": {"ActionError": {"index": 0, "kind": {
            "FunctionCallError": {"ExecutionError": "synthetic failed receipt"}}}}}
        for mutation in (None, "conversion_failure", "child", "missing", "executor", "conversion", "duplicate", "unfinalized"):
            with self.subTest(mutation=mutation):
                state = {"deposit_hash": digest, "sender": SENDER, "amount": response["quote"]["amountIn"],
                         "quote_response": response}
                result = deposit_result(digest, response)
                result["status"] = copy.deepcopy(failure)
                result["transaction_outcome"] = {"id": digest, "outcome": {
                    "executor_id": SENDER, "status": {"SuccessReceiptId": receipt_id}}}
                result["receipts_outcome"] = [{"id": receipt_id, "outcome": {
                    "executor_id": "wrap.near", "status": copy.deepcopy(failure)}}]
                if mutation == "conversion_failure":
                    result["transaction_outcome"]["outcome"]["status"] = copy.deepcopy(failure)
                    result["receipts_outcome"] = []
                elif mutation == "child":
                    result["receipts_outcome"][0]["outcome"]["status"] = {"SuccessReceiptId": digest}
                    result["receipts_outcome"].append({"id": digest, "outcome": {
                        "executor_id": "other.near", "status": copy.deepcopy(failure)}})
                elif mutation == "missing":
                    result["receipts_outcome"] = []
                elif mutation == "executor":
                    result["receipts_outcome"][0]["outcome"]["executor_id"] = "other.near"
                elif mutation == "conversion":
                    result["transaction_outcome"]["id"] = receipt_id
                elif mutation == "duplicate":
                    result["receipts_outcome"] *= 2
                elif mutation == "unfinalized":
                    result["final_execution_status"] = "EXECUTED"
                if mutation in (None, "conversion_failure"):
                    self.assertFalse(oneclick._deposit_result(state, result))
                    self.assertEqual(state["phase"], "deposit_failed")
                else:
                    with self.assertRaises(ValueError):
                        oneclick._deposit_result(state, result)
                    self.assertNotEqual(state.get("phase"), "deposit_failed")


class ExpiryTests(Case):
    def setUp(self):
        super().setUp()
        self.reference = {"block_hash": base58.encode(bytes(range(32))), "block_height": 200000000,
                          "public_key": base58.encode_key(bytes(range(32))), "nonce": 8}
        self.state = {"sender": SENDER, "deposit_hash": base58.encode(bytes([1] * 32)),
                      "deposit_reference": self.reference, "amount": quote()["quote"]["amountIn"],
                      "quote_response": quote(), "verdict": "UNKNOWN", "phase": "broadcast",
                      "version": 1, "min_amount_out": FLOOR, "judgement": judgement(quote()),
                      "destination": quote()["quoteRequest"]["destinationAsset"],
                      "recipient": quote()["quoteRequest"]["recipient"]}
        self.head = {"hash": base58.encode(bytes(reversed(range(32)))), "height": 200086401, "timestamp": 1}
        self.base = {"hash": self.reference["block_hash"], "height": 200000000, "timestamp": 1}
        self.config = {"chain_id": "mainnet", "transaction_validity_period": 86400}
        self.key = {"nonce": 7, "permission": "FullAccess", "block_hash": self.head["hash"],
                    "block_height": self.head["height"]}
        self.latest = {"data": [{"block_height": "200086410", "block_hash": self.head["hash"]}]}
        self.reply = {"jsonrpc": "2.0", "id": "gate", "error": {
            "name": "HANDLER_ERROR", "cause": {"info": {"requested_transaction_hash": self.state["deposit_hash"]},
                                              "name": "UNKNOWN_TRANSACTION"},
            "code": -32000, "message": "Server error", "data": "Transaction does not exist"}}
        self.client = rpc.Client(rpc.MAINNET_RPC_URL, "off")
        def exchange(url, method, params, body):
            self.assertEqual(body, {"jsonrpc": "2.0", "id": "gate", "method": method, "params": params})
            if method == "tx":
                self.assertEqual(params["tx_hash"], self.state["deposit_hash"])
                self.assertEqual(params["sender_account_id"], SENDER)
                self.assertIn(params["wait_until"], ("NONE", "FINAL"))
                return copy.deepcopy(self.reply)
            if method == "block":
                result = {"header": self.head if params == {"finality": "final"} else self.base}
            elif method == "EXPERIMENTAL_protocol_config":
                self.assertEqual(params, {"block_id": self.head["hash"]})
                result = self.config
            else:
                self.assertEqual((method, params), ("query", {"request_type": "view_access_key",
                    "account_id": SENDER, "public_key": self.reference["public_key"], "block_id": self.head["hash"]}))
                result = self.key
            return {"jsonrpc": "2.0", "id": "gate", "result": copy.deepcopy(result)}
        patcher = mock.patch.object(self.client, "_exchange", side_effect=exchange)
        self.exchange = patcher.start()
        self.addCleanup(patcher.stop)
        archival = self.archival_client()
        patcher = mock.patch("gate.oneclick.rpc.Client", return_value=archival)
        patcher.start()
        self.addCleanup(patcher.stop)

    def archival_client(self):
        client = mock.Mock(url=rpc.ARCHIVAL_RPC_URL)
        client.call.side_effect = lambda method, params: {"header": copy.deepcopy(
            self.head if params == {"block_id": self.head["hash"]} else self.base)}
        client._exchange.return_value = copy.deepcopy(self.reply)
        return client

    def test_real_client_unknown_error_expires_with_protocol_and_unused_nonce_proof(self):
        self.assertFalse(oneclick._broadcast_result(self.state, self.client))
        self.assertEqual((self.state["phase"], self.state["verdict"]), ("deposit_expired", "ASK"))
        proof = self.state["deposit_expiry"]
        self.assertEqual(proof["unknown_transaction"], self.reply)
        self.assertEqual(proof["access_key"], self.key)
        self.assertEqual([call.args[1] for call in self.exchange.call_args_list],
                         ["tx", "block", "EXPERIMENTAL_protocol_config", "query"])

    def test_unknown_error_must_match_without_a_mixed_result(self):
        original = copy.deepcopy(self.reply)
        for mutation in ("id", "jsonrpc", "hash", "result", "data", "code", "extra"):
            with self.subTest(mutation=mutation):
                self.reply = copy.deepcopy(original)
                if mutation == "id":
                    self.reply["id"] = "other"
                elif mutation == "jsonrpc":
                    self.reply["jsonrpc"] = "1.0"
                elif mutation == "hash":
                    self.reply["error"]["cause"]["info"]["requested_transaction_hash"] = self.reference["block_hash"]
                elif mutation == "result":
                    self.reply["result"] = {}
                elif mutation == "data":
                    self.reply["error"]["data"] = {"result": "mixed"}
                elif mutation == "code":
                    self.reply["error"]["code"] = -32000.0
                else:
                    self.reply["error"]["unexpected"] = "value"
                with self.assertRaises(ValueError):
                    oneclick._broadcast_result(self.state, self.client)
                self.assertEqual(self.state["phase"], "broadcast")

    def test_invalid_or_changed_access_key_cannot_prove_non_inclusion(self):
        original = copy.deepcopy(self.key)
        for field, value in (("nonce", 6), ("nonce", True), ("nonce", 2**64),
                             ("permission", {"GasKeyFullAccess": {"balance": "1", "num_nonces": 1}}),
                             ("block_hash", self.reference["block_hash"]), ("block_height", self.head["height"] - 1)):
            with self.subTest(field=field, value=value):
                self.key = dict(original, **{field: value})
                with self.assertRaises(ValueError):
                    oneclick._broadcast_result(self.state, self.client)
                self.assertNotIn("deposit_expiry", self.state)

    def test_consumed_nonce_needs_expiry_and_an_archival_unknown_hash(self):
        for nonce in (8, 9):
            with self.subTest(nonce=nonce):
                self.key["nonce"] = nonce
                archival = self.archival_client()
                independent = mock.Mock(url=rpc.NEARBLOCKS_URL)
                independent.get.side_effect = [copy.deepcopy(self.latest), {"data": None}]
                def factory(url, *args, **kwargs):
                    return independent if url == rpc.NEARBLOCKS_URL else archival
                state = dict(self.state)
                with mock.patch("gate.oneclick.rpc.Client", side_effect=factory) as client:
                    self.assertFalse(oneclick._broadcast_result(state, self.client))
                self.assertEqual(client.call_args_list, [
                    mock.call(rpc.ARCHIVAL_RPC_URL, "off", fixtures_dir=self.client.fixtures_dir),
                    mock.call(rpc.NEARBLOCKS_URL, "off", kind="http", fixtures_dir=self.client.fixtures_dir)])
                self.assertEqual(archival.call.call_args_list, [
                    mock.call("block", {"block_id": self.reference["block_hash"]}),
                    mock.call("block", {"block_id": self.reference["block_height"]}),
                    mock.call("block", {"block_id": self.head["hash"]})])
                self.assertEqual(archival._exchange.call_args.args[2], {
                    "tx_hash": state["deposit_hash"], "sender_account_id": SENDER, "wait_until": "NONE"})
                self.assertEqual((state["phase"], state["verdict"]), ("deposit_expired", "ASK"))
                self.assertIn("nonce is consumed", state["reason"])
                self.assertEqual(state["deposit_expiry"]["archival_lookup"]["response"], self.reply)
        self.head["height"] -= 1
        with mock.patch("gate.oneclick.rpc.Client") as client:
            with self.assertRaisesRegex(ValueError, "validity window"):
                oneclick._broadcast_result(self.state, self.client)
        client.assert_not_called()

    def test_consumed_nonce_refuses_missing_or_conflicting_archival_proof(self):
        self.key["nonce"] = 8
        for problem in ("timeout", "malformed", "different_hash", "different_block", "incomplete_final"):
            with self.subTest(problem=problem):
                archival = self.archival_client()
                if problem == "timeout":
                    archival._exchange.side_effect = TimeoutError("synthetic archival timeout")
                elif problem == "malformed":
                    archival._exchange.return_value = {"jsonrpc": "2.0", "id": "gate", "result": None}
                elif problem == "different_hash":
                    archival._exchange.return_value["error"]["cause"]["info"]["requested_transaction_hash"] = self.head["hash"]
                elif problem == "different_block":
                    call = archival.call.side_effect
                    def changed(method, params):
                        return {"header": copy.deepcopy(self.base)} if params == {"block_id": self.head["hash"]} else call(method, params)
                    archival.call.side_effect = changed
                else:
                    archival._exchange.return_value = {"jsonrpc": "2.0", "id": "gate", "result": {
                        "final_execution_status": "INCLUDED_FINAL"}}
                    archival.call.side_effect = [{"header": copy.deepcopy(self.base)}, {"header": copy.deepcopy(self.base)},
                        {"header": copy.deepcopy(self.head)}, {
                        "final_execution_status": "INCLUDED_FINAL"}]
                state = dict(self.state)
                with mock.patch("gate.oneclick.rpc.Client", return_value=archival):
                    with self.assertRaises((ValueError, TimeoutError)):
                        oneclick._broadcast_result(state, self.client)
                self.assertNotIn("deposit_expiry", state)

    def test_consumed_nonce_found_executed_in_archive_never_expires(self):
        self.key["nonce"] = 8
        archival = mock.Mock(url=rpc.ARCHIVAL_RPC_URL)
        result = deposit_result(self.state["deposit_hash"], self.state["quote_response"])
        archival.call.side_effect = [{"header": copy.deepcopy(self.base)}, {"header": copy.deepcopy(self.base)},
                                    {"header": copy.deepcopy(self.head)}, result]
        archival._exchange.return_value = {"jsonrpc": "2.0", "id": "gate", "result": {
            "final_execution_status": "INCLUDED_FINAL"}}
        with mock.patch("gate.oneclick.rpc.Client", return_value=archival):
            self.assertTrue(oneclick._broadcast_result(self.state, self.client))
        self.assertEqual(archival.call.call_args, mock.call("tx", {
            "tx_hash": self.state["deposit_hash"], "sender_account_id": SENDER, "wait_until": "FINAL"}))
        self.assertNotIn("deposit_expiry", self.state)

    def test_reference_blocks_are_read_from_archive_after_nonarchival_garbage_collection(self):
        exchange = self.exchange.side_effect
        def pruned(url, method, params, body):
            if method == "block" and "block_id" in params:
                raise rpc.RpcError({"code": -32000, "message": "UNKNOWN_BLOCK: pruned reference"})
            return exchange(url, method, params, body)
        self.exchange.side_effect = pruned
        archival = mock.Mock(url=rpc.ARCHIVAL_RPC_URL)
        archival.call.return_value = {"header": copy.deepcopy(self.base)}
        with mock.patch("gate.oneclick.rpc.Client", return_value=archival):
            self.assertFalse(oneclick._broadcast_result(self.state, self.client))
        self.assertEqual(archival.call.call_args_list, [
            mock.call("block", {"block_id": self.reference["block_hash"]}),
            mock.call("block", {"block_id": self.reference["block_height"]})])
        self.assertEqual(self.state["deposit_expiry"]["reference_rpc"], rpc.ARCHIVAL_RPC_URL)

    def test_consumed_nonce_requires_independent_absence_and_refuses_disagreement_or_unavailability(self):
        self.key["nonce"] = 8
        for problem in (None, "found", "different_hash", "malformed", "unavailable"):
            with self.subTest(problem=problem):
                archival = mock.Mock(url=rpc.ARCHIVAL_RPC_URL)
                archival.call.side_effect = lambda method, params: {"header": copy.deepcopy(
                    self.head if params == {"block_id": self.head["hash"]} else self.base)}
                archival._exchange.return_value = copy.deepcopy(self.reply)
                independent = mock.Mock(url=rpc.NEARBLOCKS_URL)
                found = {"data": None}
                if problem in ("found", "different_hash"):
                    found = {"data": {"transaction_hash": (
                        self.state["deposit_hash"] if problem == "found" else self.head["hash"])}}
                elif problem == "malformed":
                    found = {}
                independent.get.side_effect = (TimeoutError("synthetic independent timeout") if problem == "unavailable"
                                               else [copy.deepcopy(self.latest), found])
                def client(url, *args, **kwargs):
                    return independent if url == rpc.NEARBLOCKS_URL else archival
                state = dict(self.state)
                with mock.patch("gate.oneclick.rpc.Client", side_effect=client):
                    if problem is None:
                        self.assertFalse(oneclick._broadcast_result(state, self.client))
                        self.assertEqual(state["deposit_expiry"]["independent_lookup"]["response"], {"data": None})
                        self.assertEqual(state["phase"], "deposit_expired")
                    else:
                        reason = {"found": "NearBlocks.*found.*original deposit",
                                  "different_hash": "NearBlocks.*unrelated transaction",
                                  "malformed": "NearBlocks.*malformed",
                                  "unavailable": "NearBlocks.*synthetic independent timeout"}[problem]
                        with self.assertRaisesRegex(ValueError, reason):
                            oneclick._broadcast_result(state, self.client)
                        self.assertNotIn("deposit_expiry", state)
                calls = [mock.call("/v3/blocks/latest", params={"limit": 1})]
                if problem != "unavailable":
                    calls.append(mock.call(f"/v3/txns/{state['deposit_hash']}"))
                self.assertEqual(independent.get.call_args_list, calls)

    def test_consumed_nonce_requires_indexing_past_validity_plus_ten_blocks(self):
        self.key["nonce"] = 8
        required = self.reference["block_height"] + self.config["transaction_validity_period"] + 10
        for height in (required - 11, required - 1, required, required + 1):
            with self.subTest(height=height):
                archival = self.archival_client()
                independent = mock.Mock(url=rpc.NEARBLOCKS_URL)
                latest = {"data": [{"block_height": str(height), "block_hash": self.head["hash"]}]}
                independent.get.side_effect = [latest, {"data": None}]
                def client(url, *args, **kwargs):
                    return independent if url == rpc.NEARBLOCKS_URL else archival
                state = copy.deepcopy(self.state)
                with mock.patch("gate.oneclick.rpc.Client", side_effect=client):
                    if height < required:
                        with self.assertRaisesRegex(ValueError, "NearBlocks.*indexed height"):
                            oneclick._broadcast_result(state, self.client)
                        self.assertNotIn("deposit_expiry", state)
                        independent.get.assert_called_once_with("/v3/blocks/latest", params={"limit": 1})
                    else:
                        self.assertFalse(oneclick._broadcast_result(state, self.client))
                        proof = state["deposit_expiry"]["independent_indexed_block"]
                        self.assertEqual(proof, {"url": rpc.NEARBLOCKS_URL + "/v3/blocks/latest",
                            "params": {"limit": 1}, "response": latest, "required_height": required})
                        self.assertEqual(independent.get.call_args_list, [
                            mock.call("/v3/blocks/latest", params={"limit": 1}),
                            mock.call(f"/v3/txns/{state['deposit_hash']}")])

    def test_malformed_deposit_hash_refuses_before_any_provider_read(self):
        for value in (None, True, 1, "", "not-a-transaction", "0" * 44, "1" * 31, "1" * 33, "z" * 44, "1" * 100):
            with self.subTest(value=value), mock.patch("gate.oneclick.rpc.Client") as network:
                state = dict(self.state, deposit_hash=value)
                with self.assertRaises(ValueError):
                    oneclick._broadcast_result(state, self.client)
                self.exchange.assert_not_called()
                network.assert_not_called()
                self.assertNotIn("deposit_expiry", state)

    def reserve_report(self, state, *, signing_attempted=1):
        report = self.home / "report.json"
        report.write_text(json.dumps(state), encoding="utf-8")
        with oneclick._run_registry(SENDER) as registry:
            registry.execute("INSERT INTO open_runs (report, signing_attempted) VALUES (?, ?)",
                             (oneclick._report_key(report), signing_attempted))
            registry.execute("INSERT INTO open_runs (report) VALUES (?)", (oneclick._report_key(self.home / "other.json"),))
        return report

    def test_release_unsigned_row_requires_durable_no_signing_and_clears_only_it(self):
        state = {"version": 1, "sender": SENDER, "phase": "ready", "deposit_hash": None}
        report = self.reserve_report(state, signing_attempted=0)
        with mock.patch("gate.oneclick.rpc.Client") as network, mock.patch("gate.oneclick.signer.NaClSigner") as signing:
            result = oneclick.release(SENDER, report)
        self.assertIs(result["registry_released"], True)
        self.assertEqual(result["verdict"], "ASK")
        network.assert_not_called()
        signing.assert_not_called()
        with oneclick._run_registry(SENDER) as registry:
            self.assertEqual(registry.execute("SELECT report FROM open_runs").fetchall(), [(oneclick._report_key(self.home / "other.json"),)])

    def test_release_ambiguous_unsigned_and_signed_markers_refuses(self):
        for marker, field in ((None, None), (1, None), (0, "deposit_reference"), (0, "signed_tx_base64"),
                              (0, "signed_transaction"), (0, "signed_tx")):
            with self.subTest(marker=marker, field=field):
                state = {"version": 1, "sender": SENDER, "phase": "ready", "deposit_hash": None}
                if field:
                    state[field] = "synthetic signed marker"
                report = self.home / "report.json"
                report.write_text(json.dumps(state), encoding="utf-8")
                with oneclick._run_registry(SENDER) as registry:
                    registry.execute("INSERT OR REPLACE INTO open_runs (report, signing_attempted) VALUES (?, ?)",
                                     (oneclick._report_key(report), marker))
                with mock.patch("gate.oneclick.rpc.Client") as network:
                    with self.assertRaisesRegex(ValueError, "cannot prove.*before signing"):
                        oneclick.release(SENDER, report)
                network.assert_not_called()
                with oneclick._run_registry(SENDER) as registry:
                    self.assertEqual(registry.execute("SELECT report FROM open_runs").fetchall(), [(oneclick._report_key(report),)])

    def test_release_expired_row_rechecks_the_chain_and_retains_proof(self):
        report = self.reserve_report(self.state)
        with mock.patch("gate.oneclick.rpc.Client", return_value=self.client), mock.patch("gate.oneclick.signer.NaClSigner") as signing:
            result = oneclick.release(SENDER, report)
        self.assertEqual((result["phase"], result["verdict"]), ("deposit_expired", "ASK"))
        self.assertIs(result["registry_released"], True)
        self.assertEqual(json.loads(report.read_text())["deposit_expiry"]["unknown_transaction"], self.reply)
        signing.assert_not_called()
        with oneclick._run_registry(SENDER) as registry:
            self.assertNotIn((oneclick._report_key(report),), registry.execute("SELECT report FROM open_runs").fetchall())

    def test_release_final_failure_requires_the_original_receipt(self):
        report = self.reserve_report(self.state)
        result = deposit_result(self.state["deposit_hash"], self.state["quote_response"])
        failure = {"Failure": {"ActionError": {"index": 0, "kind": {
            "FunctionCallError": {"ExecutionError": "synthetic failed deposit"}}}}}
        receipt = base58.encode(bytes([3] * 32))
        result.update(status=failure, transaction_outcome={"id": self.state["deposit_hash"], "outcome": {
            "executor_id": SENDER, "status": {"SuccessReceiptId": receipt}}},
            receipts_outcome=[{"id": receipt, "outcome": {"executor_id": "wrap.near", "status": failure}}])
        self.reply = {"jsonrpc": "2.0", "id": "gate", "result": result}
        with mock.patch("gate.oneclick.rpc.Client", return_value=self.client):
            released = oneclick.release(SENDER, report)
        self.assertEqual(released["phase"], "deposit_failed")
        self.assertIs(released["registry_released"], True)
        self.assertEqual(released["deposit_result"], result)

    def test_release_success_pending_unavailable_and_loaded_terminal_rows_refuse(self):
        report = self.reserve_report(self.state)
        original = copy.deepcopy(self.reply)
        for problem in ("success", "pending", "unavailable", "loaded_terminal"):
            with self.subTest(problem=problem):
                state = dict(self.state, phase="deposit_expired" if problem == "loaded_terminal" else "broadcast")
                report.write_text(json.dumps(state), encoding="utf-8")
                self.reply = copy.deepcopy(original)
                self.head["height"] = self.reference["block_height"] + 86400
                if problem == "success":
                    self.reply = {"jsonrpc": "2.0", "id": "gate", "result": deposit_result(
                        state["deposit_hash"], state["quote_response"])}
                client = mock.Mock() if problem == "unavailable" else self.client
                if problem == "unavailable":
                    client._exchange.side_effect = TimeoutError("synthetic proof unavailable")
                with mock.patch("gate.oneclick.rpc.Client", return_value=client):
                    with self.assertRaisesRegex((ValueError, TimeoutError),
                                                "finalized successfully|validity window|proof unavailable"):
                        oneclick.release(SENDER, report)
                with oneclick._run_registry(SENDER) as registry:
                    self.assertIn((oneclick._report_key(report),), registry.execute("SELECT report FROM open_runs").fetchall())
                self.assertEqual(json.loads(report.read_text()), state)

    def test_release_missing_malformed_wrong_sender_and_unregistered_reports_refuse(self):
        report = self.reserve_report(self.state)
        for problem in ("missing", "malformed", "wrong_sender", "unregistered"):
            with self.subTest(problem=problem):
                state = dict(self.state)
                if problem == "missing":
                    report.unlink()
                elif problem == "malformed":
                    report.write_text("{", encoding="utf-8")
                elif problem == "wrong_sender":
                    report.write_text(json.dumps(dict(state, sender="other.near")), encoding="utf-8")
                else:
                    report = self.home / "unregistered.json"
                    report.write_text(json.dumps(state), encoding="utf-8")
                with mock.patch("gate.oneclick.rpc.Client") as network:
                    with self.assertRaises((OSError, ValueError)):
                        oneclick.release(SENDER, report)
                network.assert_not_called()

    def test_release_failed_report_save_keeps_signed_reservation(self):
        report = self.reserve_report(self.state)
        with mock.patch("gate.oneclick.rpc.Client", return_value=self.client):
            with mock.patch("gate.oneclick._save", side_effect=OSError("synthetic release save failure")):
                with self.assertRaisesRegex(OSError, "release save failure"):
                    oneclick.release(SENDER, report)
        with oneclick._run_registry(SENDER) as registry:
            self.assertIn((oneclick._report_key(report),), registry.execute("SELECT report FROM open_runs").fetchall())

    def test_release_consumed_nonce_requires_archival_nonexecution_proof(self):
        self.key["nonce"] = 9
        report = self.reserve_report(self.state)
        archival = self.archival_client()
        independent = mock.Mock(url=rpc.NEARBLOCKS_URL)
        independent.get.side_effect = [copy.deepcopy(self.latest), {"data": None}]
        def client(url, *args, **kwargs):
            return (archival if url == rpc.ARCHIVAL_RPC_URL else independent if url == rpc.NEARBLOCKS_URL
                    else self.client)
        with mock.patch("gate.oneclick.rpc.Client", side_effect=client):
            result = oneclick.release(SENDER, report)
        self.assertEqual(result["phase"], "deposit_expired")
        self.assertIs(result["registry_released"], True)
        self.assertEqual(result["deposit_expiry"]["archival_lookup"]["rpc"], rpc.ARCHIVAL_RPC_URL)
        with oneclick._run_registry(SENDER) as registry:
            self.assertNotIn((oneclick._report_key(report),), registry.execute("SELECT report FROM open_runs").fetchall())

    def test_release_consumed_nonce_keeps_the_report_and_reservation_when_either_source_fails(self):
        self.key["nonce"] = 9
        report = self.reserve_report(self.state)
        original = report.read_bytes()
        for problem in ("archival_unavailable", "independent_unavailable", "independent_found", "independent_malformed"):
            with self.subTest(problem=problem):
                archival = self.archival_client()
                independent = mock.Mock(url=rpc.NEARBLOCKS_URL)
                found = {"data": None}
                if problem == "archival_unavailable":
                    archival._exchange.side_effect = TimeoutError("synthetic archival unavailable")
                elif problem == "independent_found":
                    found = {"data": {"transaction_hash": self.state["deposit_hash"]}}
                elif problem == "independent_malformed":
                    found = {"data": None, "error": "unavailable"}
                independent.get.side_effect = (rpc.HttpError(429, rpc.NEARBLOCKS_URL) if problem == "independent_unavailable"
                                               else [copy.deepcopy(self.latest), found])
                def client(url, *args, **kwargs):
                    return (archival if url == rpc.ARCHIVAL_RPC_URL else independent
                            if url == rpc.NEARBLOCKS_URL else self.client)
                with mock.patch("gate.oneclick.rpc.Client", side_effect=client):
                    with self.assertRaisesRegex(ValueError, "archival unavailable|429|found the original deposit|malformed"):
                        oneclick.release(SENDER, report)
                self.assertEqual(report.read_bytes(), original)
                with oneclick._run_registry(SENDER) as registry:
                    self.assertEqual(len(registry.execute("SELECT report FROM open_runs").fetchall()), 2)

    def test_release_consumed_nonce_keeps_report_and_row_when_indexing_is_lagging_malformed_or_unavailable(self):
        self.key["nonce"] = 9
        report = self.reserve_report(self.state)
        original = report.read_bytes()
        latest = {"data": [{"block_height": "200086410", "block_hash": self.head["hash"]}]}
        problems = [None, {}, {"data": None}, {"data": []}, {"data": [None]}, {"data": [1]},
                    {"data": latest["data"] * 2}, dict(latest, errors=[{"message": "synthetic partial index"}]),
                    TimeoutError("synthetic latest timeout"), rpc.HttpError(429, rpc.NEARBLOCKS_URL)]
        for height in (None, True, 200086410, "", "0200086410", "-1", "1.0", str(2**64), "200086409"):
            problems.append({"data": [dict(latest["data"][0], block_height=height)]})
        for block_hash in (None, True, "bad", "0" * 44):
            problems.append({"data": [dict(latest["data"][0], block_hash=block_hash)]})
        for answer in problems:
            with self.subTest(answer=answer):
                archival = self.archival_client()
                independent = mock.Mock(url=rpc.NEARBLOCKS_URL)
                independent.get.side_effect = [answer, {"data": None}]
                def client(url, *args, **kwargs):
                    return (archival if url == rpc.ARCHIVAL_RPC_URL else independent
                            if url == rpc.NEARBLOCKS_URL else self.client)
                with mock.patch("gate.oneclick.rpc.Client", side_effect=client):
                    with self.assertRaises(ValueError):
                        oneclick.release(SENDER, report)
                self.assertEqual(report.read_bytes(), original)
                independent.get.assert_called_once_with("/v3/blocks/latest", params={"limit": 1})
                with oneclick._run_registry(SENDER) as registry:
                    self.assertEqual(len(registry.execute("SELECT report FROM open_runs").fetchall()), 2)

    def test_legacy_registry_migration_preserves_an_ambiguous_unsigned_row(self):
        directory = self.home / ".siyab-oneclick"
        directory.mkdir()
        report = self.home / "report.json"
        report.write_text(json.dumps({"version": 1, "sender": SENDER, "phase": "ready", "deposit_hash": None}),
                          encoding="utf-8")
        with contextlib.closing(oneclick.sqlite3.connect(directory / (SENDER + ".sqlite3"))) as registry:
            registry.execute("CREATE TABLE open_runs (report TEXT PRIMARY KEY)")
            registry.execute("INSERT INTO open_runs (report) VALUES (?)", (str(report),))
            registry.commit()
        with oneclick._run_registry(SENDER) as registry:
            self.assertEqual(registry.execute("SELECT report, signing_attempted, reserved_at_ms FROM open_runs").fetchall(),
                             [(oneclick._report_key(report), None, None)])
        with self.assertRaisesRegex(ValueError, "cannot prove.*before signing"):
            oneclick.release(SENDER, report)

    def test_missing_legacy_reference_and_unreadable_protocol_never_expire(self):
        del self.state["deposit_reference"]
        with self.assertRaisesRegex(ValueError, "no signed reference"):
            oneclick._broadcast_result(self.state, self.client)
        self.state["deposit_reference"] = self.reference
        for period in (None, True, 0, -1, "86400", 2**64):
            with self.subTest(period=period):
                self.config["transaction_validity_period"] = period
                with self.assertRaisesRegex(ValueError, "validity period"):
                    oneclick._broadcast_result(self.state, self.client)
                self.assertEqual(self.state["phase"], "broadcast")

    def test_uncanonical_reference_cannot_be_called_expired(self):
        self.base["hash"] = self.head["hash"]
        with self.assertRaises(ValueError):
            oneclick._broadcast_result(self.state, self.client)
        self.assertNotIn("deposit_expiry", self.state)

    def test_known_and_unfinalized_deposits_use_the_existing_result_checks(self):
        del self.reply["error"]
        self.reply["result"] = deposit_result(self.state["deposit_hash"], self.state["quote_response"])
        self.assertTrue(oneclick._broadcast_result(self.state, self.client))
        self.assertEqual(self.exchange.call_count, 2)
        self.assertEqual([call.args[2]["wait_until"] for call in self.exchange.call_args_list], ["NONE", "FINAL"])
        self.reply["result"]["final_execution_status"] = "EXECUTED_OPTIMISTIC"
        with self.assertRaisesRegex(ValueError, "finalized"):
            oneclick._broadcast_result(self.state, self.client)
        self.assertNotIn("deposit_expiry", self.state)


class RecordedEvidenceTests(Case):
    def test_nearblocks_recordings_pin_hash_absence_presence_and_pair_lookup(self):
        digests = {
            "84c642679bda366b1068d3001e5ee316ff32cab65219e8f612192e5d76fa2362.json":
                "82655e836e06bfe755fe1e6e3d9baf10b0b351a59f2c098a886943b68bf4d77f",
            "06919c0a0a5a6e9315e96902b0ff524267a4e343659de2dc218ffc54a7b58fbf.json":
                "d901689bf78853e3c92d22f5241c6bf945128aa99b0ad4a7a431ef5c0a5574c0",
            "c00bd6821cc93e29dcfe528eb5651eff0c486cc07a79ecebd90cbf23f995342c.json":
                "a521259024080375fd8cb568586c3fdb85858461d19cee11b378c08fc1fa537a",
            "9869e9959778db276c2e3322d1697dd9122e40750d0db5916b2ff7c7f90d8013.json":
                "a14dc8258bbaca88d722cd6d092b8c8952180920227f8785fe48748011d72dbf",
        }
        for filename, digest in digests.items():
            with self.subTest(filename=filename):
                self.assertEqual(hashlib.sha256((ROOT / filename).read_bytes()).hexdigest(), digest)
        client = rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http", fixtures_dir=ROOT)
        self.assertEqual(client.get("/v3/txns/11111111111111111111111111111111"), {"data": None})
        known = "DUzJv1DVj3tyEgewoKK8ihHaq72SrJ38xMYeMeGoyFNh"
        present = client.get(f"/v3/txns/{known}")
        self.assertEqual(present["data"]["transaction_hash"], known)
        with contextlib.redirect_stdout(io.StringIO()):
            recent = oneclick._recent_transactions("wrap.near", mode="replay", fixtures_root=ROOT.parent)
        self.assertEqual(len(recent["response"]["data"]), 4)
        self.assertEqual(recent["params"], {"signer": "wrap.near", "receiver": "wrap.near", "limit": 10})
        self.assertEqual(recent["response"]["data"][0]["block_timestamp"], "1624542750855738077")
        latest = client.get("/v3/blocks/latest", params={"limit": 1})
        self.assertEqual(len(latest["data"]), 1)
        self.assertEqual(latest["data"][0]["block_height"], "225608394")
        self.assertEqual(len(base58.decode(latest["data"][0]["block_hash"])), 32)

    def test_all_nearblocks_paths_send_the_repository_user_agent(self):
        unknown = json.loads((ROOT / "84c642679bda366b1068d3001e5ee316ff32cab65219e8f612192e5d76fa2362.json").read_bytes())
        recent = json.loads((ROOT / "c00bd6821cc93e29dcfe528eb5651eff0c486cc07a79ecebd90cbf23f995342c.json").read_bytes())
        latest = json.loads((ROOT / "9869e9959778db276c2e3322d1697dd9122e40750d0db5916b2ff7c7f90d8013.json").read_bytes())
        self.urlopen.side_effect = [io.BytesIO(json.dumps(record["response"]).encode()) for record in (unknown, recent, latest)]
        with mock.patch("gate.rpc.Client._respect_nearblocks"), contextlib.redirect_stdout(io.StringIO()):
            client = rpc.Client(rpc.NEARBLOCKS_URL, "off", kind="http")
            client.get("/v3/txns/11111111111111111111111111111111")
            oneclick._recent_transactions("wrap.near", mode="off", fixtures_root=ROOT.parent)
            client.get("/v3/blocks/latest", params={"limit": 1})
        self.assertEqual(self.urlopen.call_count, 3)
        for call in self.urlopen.call_args_list:
            self.assertEqual(call.args[0].get_header("User-agent"), "siyab")

    def test_known_discovery_fetches_a_faithfully_built_matching_landed_deposit(self):
        params = {"tx_hash": "DzQkkA8RPM6crTH4iyhSYViwj71Ca7x3dXC52SMnwKYg",
                  "sender_account_id": "31ac7a2705a0686ff427b1a52d3ffd1fcfaa4b1f3cb3e83a0f767494e724a540",
                  "wait_until": "FINAL"}
        client = rpc.Client(rpc.ARCHIVAL_RPC_URL, "replay", fixtures_dir=ROOT)
        result = client.call("tx", params)
        response = quote()
        digest, signed, _, reference = oneclick._signed_deposit(SENDER, response,
            client=SigningTests.funding_client(self), tx_signer=transaction_signer())
        # Synthetic supported deposit, using the recorded FINAL envelope and outcome structure.
        result["transaction"].update(hash=digest, signer_id=SENDER, nonce=reference["nonce"],
                                      public_key=reference["public_key"], receiver_id="wrap.near",
                                      signature_bytes=list(base64.b64decode(signed)[-64:]))
        args = json.dumps({"receiver_id": response["quote"]["depositAddress"], "amount": response["quote"]["amountIn"]},
                          separators=(",", ":")).encode()
        result["transaction"]["actions"][0]["FunctionCall"].update(
            gas=oneclick.GAS, args=base64.b64encode(args).decode())
        self.assertIn(len(args).to_bytes(4, "little") + args, base64.b64decode(signed)[:-65])
        result["transaction_outcome"]["id"] = digest
        result["transaction_outcome"]["outcome"]["executor_id"] = SENDER
        state = {"deposit_hash": digest, "sender": SENDER, "amount": response["quote"]["amountIn"],
                 "quote_response": response, "deposit_reference": reference}
        def read(url, method, request, body):
            self.assertEqual((method, request["tx_hash"], request["sender_account_id"]), ("tx", digest, SENDER))
            if request["wait_until"] == "NONE":
                return {"jsonrpc": "2.0", "id": "gate", "result": {"final_execution_status": "INCLUDED_FINAL"}}
            return {"jsonrpc": "2.0", "id": "gate", "result": copy.deepcopy(result)}
        with mock.patch.object(client, "_exchange", side_effect=read) as reads:
            self.assertTrue(oneclick._broadcast_result(state, client))
        self.assertEqual([call.args[2]["wait_until"] for call in reads.call_args_list], ["NONE", "FINAL"])
        self.assertEqual(result["status"], {"SuccessValue": ""})
        self.assertEqual(result["final_execution_status"], "FINAL")

    def test_catalog_replays_and_matches_the_extracted_api_contract(self):
        client = rpc.Client(intents.ONECLICK_URL, "replay", kind="http", fixtures_dir=ROOT)
        self.assertEqual(oneclick._catalog(client, quote()["quoteRequest"]["destinationAsset"]), "base")
        operation = CONTRACT["paths"]["/v0/tokens"]["get"]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(schema["items"]["$ref"], "#/components/schemas/TokenResponse")
        fields = CONTRACT["schemas"]["TokenResponse"]["properties"]
        self.assertTrue({"assetId", "blockchain", "contractAddress", "decimals"} <= fields.keys())

    def test_fresh_address_registration_and_bounds_replay_actual_rpc(self):
        client = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=ROOT)
        record = json.loads((ROOT / "allocated-quote-20261121.json").read_text(encoding="utf-8"))
        address = record["response"]["quote"]["depositAddress"]
        self.assertIsNone(client.call_function("wrap.near", "storage_balance_of", {"account_id": address}))
        bounds = client.call_function("wrap.near", "storage_balance_bounds", {})
        self.assertEqual(bounds, {"min": "1250000000000000000000", "max": "1250000000000000000000"})
        historical = json.loads((ROOT / "status-near-success-20261121.json").read_text(encoding="utf-8"))
        account = historical["request"]["params"]["depositAddress"]
        self.assertEqual(client.call_function("wrap.near", "storage_balance_of", {"account_id": account}),
                         {"total": bounds["min"], "available": "0"})

    def test_historical_near_origin_hash_is_distinct_from_internal_near_transactions(self):
        record = json.loads((ROOT / "status-near-success-20261121.json").read_text(encoding="utf-8"))
        status = record["response"]
        self.assertEqual(status["status"], "SUCCESS")
        self.assertEqual(status["quoteResponse"]["quoteRequest"]["originAsset"], oneclick.ORIGIN)
        details = status["swapDetails"]
        self.assertEqual(details["originChainTxHashes"][0]["hash"], "9CLCjpPN2tY6UE7HNd5e5eABLedzVrvXTvLUGJDwXgEq")
        self.assertNotIn(details["originChainTxHashes"][0]["hash"], details["nearTxHashes"])

    def test_historical_transfer_replays_through_client_and_refuses_its_uncapped_gas(self):
        params = {"tx_hash": "DzQkkA8RPM6crTH4iyhSYViwj71Ca7x3dXC52SMnwKYg",
                  "sender_account_id": "31ac7a2705a0686ff427b1a52d3ffd1fcfaa4b1f3cb3e83a0f767494e724a540",
                  "wait_until": "FINAL"}
        client = rpc.Client(rpc.ARCHIVAL_RPC_URL, "replay", fixtures_dir=ROOT)
        result = client.call("tx", params)
        tx = result["transaction"]
        call = tx["actions"][0]["FunctionCall"]
        self.assertEqual((result["final_execution_status"], result["status"]), ("FINAL", {"SuccessValue": ""}))
        self.assertEqual((tx["receiver_id"], call["method_name"], call["deposit"]), ("wrap.near", "ft_transfer", "1"))
        self.assertIs(type(call["gas"]), int)
        self.assertEqual(call["gas"], 300000000000000)
        args = json.loads(base64.b64decode(call["args"], validate=True))
        state = {"deposit_hash": tx["hash"], "sender": tx["signer_id"], "amount": args["amount"],
                 "quote_response": {"quote": {"depositAddress": args["receiver_id"]}}}
        with self.assertRaisesRegex(ValueError, "capped token transfer"):
            oneclick._deposit_result(state, result)


class FlowTests(Case):
    funding_client = SigningTests.funding_client

    def run_flow(self, *, live=False, sequence=None, malformed=None, send_error=False,
                 pre_status="PENDING_DEPOSIT", submit_status="KNOWN_DEPOSIT_TX", judged_verdict="ALLOW", fail_stage=None,
                 register=False, key_account=True, deposit_failure=False, register_on_sign=False, report_name="report.json"):
        response = quote()
        judged = judgement(response)
        judged["verdict"] = judged["document"]["verdict"] = judged_verdict
        judged["evidence_hash"] = evidence.evidence_hash(judged["document"])
        http = mock.Mock()
        near = self.funding_client()
        near.url = rpc.MAINNET_RPC_URL
        if register:
            receiver_reads = 0
            def funding(token, method, args):
                nonlocal receiver_reads
                if method == "storage_balance_of":
                    if args["account_id"] != SENDER:
                        receiver_reads += 1
                        if not register_on_sign or receiver_reads == 2:
                            return None
                    return {"total": "1250000000000000000000"}
                if method == "storage_balance_bounds":
                    return {"min": "1250000000000000000000", "max": "1250000000000000000000"}
                return str(oneclick.MAX_AMOUNT)
            near.call_function.side_effect = funding
        evm, evm_replies = evm_client(response)
        if fail_stage == "destination_chain":
            evm_replies["eth_chainId"] = "0x1"
        elif fail_stage == "finalized_block":
            evm_replies["eth_getBlockByNumber"] = None
        deposit_hash, _, registration, _ = oneclick._signed_deposit(SENDER, response, client=near, tx_signer=transaction_signer())
        final = success(response, deposit_hash)
        pending = status(response, pre_status)
        catalog = [{"assetId": oneclick.ORIGIN, "blockchain": "near", "contractAddress": "wrap.near", "decimals": 24},
                   {"assetId": response["quoteRequest"]["destinationAsset"], "blockchain": "base",
                    "contractAddress": intents.parse_destination(response["quoteRequest"]["destinationAsset"])[1], "decimals": 6}]
        queue = [pending, pending] if live else [pending]
        queue += [status(response, value) if value != "SUCCESS" else final for value in (sequence or ["SUCCESS"])]
        status_reads = 0
        def get(path, **kwargs):
            nonlocal status_reads
            if path == "/v0/tokens":
                if fail_stage == "catalog_http":
                    raise rpc.HttpError(503, intents.ONECLICK_URL)
                if fail_stage == "catalog_malformed":
                    return {"tokens": []}
            else:
                status_reads += 1
                if (fail_stage in ("prestatus_http", "prestatus_404") and status_reads == 1
                        or fail_stage == "prebroadcast_http" and status_reads == 2
                        or fail_stage == "poll_http" and status_reads == 3):
                    raise rpc.HttpError(404 if fail_stage == "prestatus_404" else 503, intents.ONECLICK_URL)
            return copy.deepcopy(catalog if path == "/v0/tokens" else queue.pop(0))
        http.get.side_effect = get
        def post(path, body):
            if (fail_stage == "quote_http" and path == "/v0/quote"
                    or fail_stage == "submit_http" and path == "/v0/deposit/submit"):
                raise rpc.HttpError(503, intents.ONECLICK_URL)
            return copy.deepcopy(malformed if path == "/v0/quote" and malformed is not None
                                 else response if path == "/v0/quote" else status(response, submit_status))
        http.post.side_effect = post
        def near_call(method, params):
            if method == "status":
                if fail_stage == "origin_status":
                    raise TimeoutError("synthetic origin status timeout")
                return {"chain_id": "mainnet"}
            if method == "query":
                with oneclick._run_registry(SENDER) as registry:
                    self.assertEqual(registry.execute("SELECT signing_attempted FROM open_runs WHERE report = ?",
                                                      (oneclick._report_key(report),)).fetchone(), (1,))
                return {"permission": "FullAccess", "nonce": 7, "block_hash": base58.encode(bytes(range(32))),
                        "block_height": 200000000}
            if method == "send_tx":
                persisted = json.loads(report.read_text(encoding="utf-8"))
                self.assertEqual(persisted["deposit_hash"], deposit_hash)
                self.assertEqual(persisted["phase"], "broadcast")
                self.assertIn("never redeposit", persisted["reason"])
                self.assertNotIn("fresh run", persisted["reason"])
            if method == "send_tx" and send_error:
                raise TimeoutError("synthetic uncertain broadcast")
            result = deposit_result(deposit_hash, response, registration)
            if deposit_failure:
                result["status"] = {"Failure": {"ActionError": {"index": 0, "kind": {
                    "FunctionCallError": {"ExecutionError": "synthetic batch failure"}}}}}
                receipt_id = base58.encode(bytes(reversed(range(32))))
                result["transaction_outcome"] = {"id": deposit_hash, "outcome": {
                    "executor_id": SENDER, "status": {"SuccessReceiptId": receipt_id}}}
                result["receipts_outcome"] = [{"id": receipt_id, "outcome": {
                    "executor_id": "wrap.near", "status": copy.deepcopy(result["status"])}}]
            return result
        near.call.side_effect = near_call
        def factory(url, *args, **kwargs):
            return (http if url == intents.ONECLICK_URL else near
                    if url in (rpc.MAINNET_RPC_URL, rpc.ARCHIVAL_RPC_URL) else evm)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        home_patcher = mock.patch("gate.oneclick.Path.home", return_value=self.home)
        home_patcher.start()
        self.addCleanup(home_patcher.stop)
        report = Path(tmp.name) / report_name
        key = Path(tmp.name) / "key.json"
        key.write_text(json.dumps({"account_id": SENDER} if key_account else {"implicit_account_id": "0" * 64}), encoding="utf-8")
        signer_options = ({"side_effect": oneclick.signer.SignerError("synthetic signer refusal")}
                          if fail_stage == "signer" else {"return_value": transaction_signer()})
        if fail_stage == "signed_output":
            signer_options["return_value"].sign.return_value = bytes(1)
        patches = (mock.patch("gate.oneclick.rpc.Client", side_effect=factory),
                   mock.patch("gate.intents.judge_quote", return_value=judged),
                   mock.patch("gate.oneclick.signer.NaClSigner", **signer_options))
        with patches[0], patches[1], patches[2] as signing, contextlib.redirect_stdout(io.StringIO()) as printed:
            result = oneclick.settle(SENDER, response["quoteRequest"]["destinationAsset"],
                                     response["quoteRequest"]["amount"], response["quoteRequest"]["recipient"],
                                     min_amount_out=FLOOR, clock=lambda: CLOCK, live=live,
                                     key_file=key if live else None, out=report, sleep=lambda _: None, polls=3)
        return result, http, near, signing, printed.getvalue(), report, factory

    def test_external_boundary_failures_stop_once_and_preserve_deposit(self):
        stages = {"origin_status": (0, 0, 0), "catalog_http": (0, 0, 0),
                  "catalog_malformed": (0, 0, 0), "destination_chain": (0, 0, 0),
                  "finalized_block": (0, 0, 0), "quote_http": (0, 0, 1),
                  "prestatus_http": (0, 1, 1), "prestatus_404": (0, 1, 1),
                 "signer": (0, 0, 0), "prebroadcast_http": (0, 2, 1),
                  "submit_http": (1, 2, 2), "poll_http": (1, 3, 2)}
        for stage, (sends, reads, posts) in stages.items():
            with self.subTest(stage=stage):
                result, http, near, _, _, report, _ = self.run_flow(live=True, fail_stage=stage)
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(len([call for call in near.call.call_args_list if call.args[0] == "send_tx"]), sends)
                self.assertEqual(len([call for call in near.call.call_args_list if call.args[0] == "status"]),
                                 0 if stage == "signer" else 1)
                self.assertEqual(len([call for call in http.get.call_args_list if call.args[0] == "/v0/status"]), reads)
                self.assertEqual(http.post.call_count, posts)
                if sends or stage == "prebroadcast_http":
                    self.assertTrue(result["deposit_hash"])
                    self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["deposit_hash"], result["deposit_hash"])

    def test_preview_stops_before_signing_and_broadcast(self):
        result, http, near, signing, printed, *_ = self.run_flow()
        self.assertEqual((result["verdict"], result["phase"]), ("ASK", "ready"))
        self.assertIsNone(result["deposit_hash"])
        signing.assert_not_called()
        self.assertNotIn("send_tx", [call.args[0] for call in near.call.call_args_list])
        self.assertEqual([call.args[0] for call in http.post.call_args_list], ["/v0/quote"])
        self.assertIn("cap: 0.25 wNEAR", printed)

    def test_nonallow_recipient_or_preexisting_swap_never_deposits(self):
        for verdict in ("DENY", "UNKNOWN"):
            with self.subTest(verdict=verdict):
                result, http, near, signing, *_ = self.run_flow(live=True, judged_verdict=verdict)
                self.assertEqual(result["verdict"], verdict)
                http.post.assert_not_called()
                signing.return_value.sign.assert_not_called()
                self.assertNotIn("send_tx", [call.args[0] for call in near.call.call_args_list])
        for lifecycle in ("PROCESSING", "SUCCESS", "REFUNDED", "FAILED", "INCOMPLETE_DEPOSIT", "KNOWN_DEPOSIT_TX"):
            with self.subTest(status=lifecycle):
                result, _, near, signing, *_ = self.run_flow(live=True, pre_status=lifecycle)
                self.assertEqual(result["verdict"], "UNKNOWN")
                signing.return_value.sign.assert_not_called()
                self.assertNotIn("send_tx", [call.args[0] for call in near.call.call_args_list])

    def test_terminal_submit_response_stops_without_poll(self):
        for lifecycle in ("REFUNDED", "FAILED", "INCOMPLETE_DEPOSIT"):
            with self.subTest(status=lifecycle):
                result, http, _, *_ = self.run_flow(live=True, submit_status=lifecycle)
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(len([call for call in http.get.call_args_list if call.args[0] == "/v0/status"]), 2)

    def test_live_settlement_once_and_resume_without_redeposit(self):
        result, http, near, _, _, report, factory = self.run_flow(live=True)
        self.assertEqual((result["verdict"], result["phase"]), ("ALLOW", "settled"))
        sends = [call for call in near.call.call_args_list if call.args[0] == "send_tx"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0].args[1]["wait_until"], "FINAL")
        self.assertEqual(http.post.call_args_list[-1].args[1]["txHash"], result["deposit_hash"])
        self.assertEqual(json.loads(report.read_text())["deposit_hash"], result["deposit_hash"])
        http.reset_mock()
        near.reset_mock()
        http.get.side_effect = lambda *args, **kwargs: success(result["quote_response"], result["deposit_hash"])
        with mock.patch("gate.oneclick.rpc.Client", side_effect=factory), mock.patch("gate.oneclick.signer.NaClSigner") as signing:
            resumed = oneclick.resume(report, clock=lambda: CLOCK + 86400, sleep=lambda _: None, polls=1)
        self.assertEqual(resumed["verdict"], "ALLOW")
        signing.assert_not_called()
        http.post.assert_not_called()
        self.assertEqual([call.args[0] for call in near.call.call_args_list], ["tx"])

    def test_new_receiver_registration_settles_once_and_resume_never_registers_again(self):
        result, http, near, _, printed, report, factory = self.run_flow(live=True, register=True)
        self.assertEqual(result["verdict"], "ALLOW")
        self.assertEqual(result["storage_deposit"], "1250000000000000000000")
        self.assertIn("registration: 1250000000000000000000 yoctoNEAR", printed)
        self.assertEqual(len([call for call in near.call.call_args_list if call.args[0] == "send_tx"]), 1)
        near.reset_mock()
        http.reset_mock()
        http.get.side_effect = lambda *args, **kwargs: success(result["quote_response"], result["deposit_hash"])
        with mock.patch("gate.oneclick.rpc.Client", side_effect=factory):
            resumed = oneclick.resume(report, clock=lambda: CLOCK + 86400, sleep=lambda _: None, polls=1)
        self.assertEqual(resumed["verdict"], "ALLOW")
        self.assertEqual([call.args[0] for call in near.call.call_args_list], ["tx"])
        near.call_function.assert_not_called()
        http.post.assert_not_called()

    def test_cli_rs_key_metadata_without_account_id_uses_explicit_sender_and_access_key(self):
        result, _, near, signing, *_ = self.run_flow(live=True, key_account=False)
        self.assertEqual(result["verdict"], "ALLOW")
        signing.assert_called_once()
        query = next(call.args[1] for call in near.call.call_args_list if call.args[0] == "query")
        self.assertEqual(query["account_id"], SENDER)
        self.assertEqual(query["public_key"], base58.encode_key(bytes(range(32))))

    def test_signed_storage_deposit_replaces_the_preview_amount_in_the_report(self):
        result, _, near, _, printed, report, _ = self.run_flow(live=True, register=True, register_on_sign=True)
        self.assertEqual((result["verdict"], result["storage_deposit"]), ("ALLOW", "0"))
        self.assertIn("registration: 1250000000000000000000 yoctoNEAR", printed)
        self.assertEqual(json.loads(report.read_text())["storage_deposit"], "0")
        self.assertEqual(len([call for call in near.call.call_args_list if call.args[0] == "send_tx"]), 1)

    def test_existing_report_cannot_be_reused_for_new_deposit(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True)
        original = report.read_bytes()
        with self.assertRaises(FileExistsError):
            oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                            min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=report)
        self.assertEqual(report.read_bytes(), original)

    def test_atomic_report_failure_preserves_original_hash_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            original = {"deposit_hash": base58.encode(bytes(range(32))), "phase": "broadcast"}
            for operation in ("replace", "fsync"):
                with self.subTest(operation=operation):
                    report.write_text(json.dumps(original), encoding="utf-8")
                    with mock.patch("gate.oneclick.os." + operation, side_effect=OSError("synthetic disk failure")):
                        with self.assertRaises(OSError):
                            oneclick._save(dict(original, phase="deposited"), report)
                    self.assertEqual(json.loads(report.read_text(encoding="utf-8")), original)
                    self.assertEqual(list(report.parent.glob(".oneclick-*.tmp")), [])

    def test_report_replace_syncs_the_parent_directory_on_posix(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            with mock.patch("gate.oneclick.os", wraps=os) as system:
                system.name = "posix"
                system.O_RDONLY = os.O_RDONLY
                system.open.return_value = 12345
                system.fsync.return_value = None
                system.close.return_value = None
                oneclick._save({"phase": "broadcast"}, report)
            system.open.assert_called_once_with(report.parent, os.O_RDONLY)
            self.assertEqual(system.fsync.call_count, 2)
            system.fsync.assert_called_with(12345)
            system.close.assert_called_once_with(12345)
            calls = system.mock_calls
            self.assertLess(next(i for i, call in enumerate(calls) if call[0] == "replace"),
                            next(i for i, call in enumerate(calls) if call == mock.call.fsync(12345)))
            self.assertEqual(json.loads(report.read_text()), {"phase": "broadcast"})

    def test_windows_report_sync_does_not_attempt_to_open_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            with mock.patch("gate.oneclick.os", wraps=os) as system:
                system.name = "nt"
                oneclick._save({"phase": "prepared"}, report)
            system.open.assert_not_called()
            system.fsync.assert_called_once()

    def test_directory_sync_failure_is_reported_and_closes_its_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            with mock.patch("gate.oneclick.os", wraps=os) as system:
                system.name = "posix"
                system.O_RDONLY = os.O_RDONLY
                system.open.return_value = 12345
                system.fsync.side_effect = [None, OSError("synthetic directory sync failure")]
                system.close.return_value = None
                with self.assertRaisesRegex(OSError, "directory sync failure"):
                    oneclick._save({"phase": "broadcast"}, report)
            system.close.assert_called_once_with(12345)
            self.assertEqual(json.loads(report.read_text()), {"phase": "broadcast"})

    def test_resume_nested_null_status_stops_unknown(self):
        result, http, _, _, _, report, factory = self.run_flow(live=True)
        malformed = status(result["quote_response"])
        malformed["quoteResponse"] = None
        http.get.side_effect = None
        http.get.return_value = malformed
        with mock.patch("gate.oneclick.rpc.Client", side_effect=factory):
            resumed = oneclick.resume(report, clock=lambda: CLOCK, sleep=lambda _: None, polls=1)
        self.assertEqual(resumed["verdict"], "UNKNOWN")
        self.assertEqual(resumed["deposit_hash"], result["deposit_hash"])

    def test_refund_failed_incomplete_and_timeout_never_allow(self):
        for lifecycle in ("REFUNDED", "FAILED", "INCOMPLETE_DEPOSIT", "PROCESSING", "KNOWN_DEPOSIT_TX", "PENDING_DEPOSIT"):
            with self.subTest(status=lifecycle):
                result, _, near, *_ = self.run_flow(live=True, sequence=[lifecycle] * 3)
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertEqual(len([call for call in near.call.call_args_list if call.args[0] == "send_tx"]), 1)

    def test_uncertain_broadcast_persists_hash_and_never_submits(self):
        result, http, near, _, _, report, _ = self.run_flow(live=True, send_error=True)
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertTrue(result["deposit_hash"])
        self.assertEqual(json.loads(report.read_text())["deposit_hash"], result["deposit_hash"])
        self.assertIn("never redeposit", result["reason"])
        self.assertEqual([call.args[0] for call in http.post.call_args_list], ["/v0/quote"])

    def test_final_deposit_failure_is_cited_and_permits_a_fresh_run(self):
        result, http, near, _, _, report, factory = self.run_flow(live=True, register=True, deposit_failure=True)
        self.assertEqual((result["verdict"], result["phase"]), ("ASK", "deposit_failed"))
        self.assertIn("synthetic batch failure", result["reason"])
        self.assertIn("fresh run", result["reason"])
        self.assertNotIn("never redeposit", result["reason"])
        self.assertEqual(result["deposit_result"]["final_execution_status"], "FINAL")
        self.assertIn("Failure", result["deposit_result"]["status"])
        self.assertEqual(json.loads(report.read_text())["deposit_result"], result["deposit_result"])
        self.assertEqual([call.args[0] for call in http.post.call_args_list], ["/v0/quote"])
        near.reset_mock()
        with mock.patch("gate.oneclick.rpc.Client", side_effect=factory):
            resumed = oneclick.resume(report, clock=lambda: CLOCK, polls=1)
        self.assertEqual((resumed["verdict"], resumed["phase"]), ("ASK", "deposit_failed"))
        self.assertEqual([call.args[0] for call in near.call.call_args_list], ["tx"])

    def test_unknown_deposit_expires_only_after_its_signed_block_validity_window(self):
        result, http, near, _, _, report, factory = self.run_flow(live=True, send_error=True)
        reference = result["deposit_reference"]
        head_hash = base58.encode(bytes(reversed(range(32))))
        period = 86400
        for prior, delta, phase in (("broadcast", period, "broadcast"), ("broadcast", period + 1, "deposit_expired"),
                                    ("prepared", period, "prepared"), ("prepared", period + 1, "deposit_expired")):
            with self.subTest(prior=prior, delta=delta):
                report.write_text(json.dumps(dict(result, phase=prior)), encoding="utf-8")
                near.reset_mock()
                http.reset_mock()
                head = {"hash": head_hash, "height": reference["block_height"] + delta}
                def call(method, params):
                    if method == "EXPERIMENTAL_protocol_config":
                        self.assertEqual(params, {"block_id": head_hash})
                        return {"chain_id": "mainnet", "transaction_validity_period": period}
                    if method == "block":
                        return {"header": head if params == {"finality": "final"} else {
                            "hash": reference["block_hash"], "height": reference["block_height"]}}
                    self.assertEqual((method, params), ("query", {"request_type": "view_access_key",
                        "account_id": SENDER, "public_key": reference["public_key"], "block_id": head_hash}))
                    return {"permission": "FullAccess", "nonce": reference["nonce"] - 1,
                            "block_hash": head_hash, "block_height": head["height"]}
                near._exchange.return_value = {"jsonrpc": "2.0", "id": "gate", "error": {
                    "name": "HANDLER_ERROR", "code": -32000, "message": "Server error", "cause": {
                        "name": "UNKNOWN_TRANSACTION", "info": {"requested_transaction_hash": result["deposit_hash"]}}}}
                near.call.side_effect = call
                with mock.patch("gate.oneclick.rpc.Client", side_effect=factory):
                    resumed = oneclick.resume(report, clock=lambda: CLOCK + 86400, polls=1)
                self.assertEqual(resumed["phase"], phase)
                self.assertEqual(resumed["verdict"], "ASK" if phase == "deposit_expired" else "UNKNOWN")
                self.assertEqual(near._exchange.call_args.args[2]["wait_until"], "NONE")
                if phase == "deposit_expired":
                    self.assertIn("fresh run", resumed["reason"])
                    self.assertNotIn("never redeposit", resumed["reason"])
                    self.assertEqual(resumed["deposit_expiry"]["transaction_validity_period"], period)
                else:
                    self.assertIn("never redeposit", resumed["reason"])
                http.get.assert_not_called()
                http.post.assert_not_called()
                self.assertNotIn("send_tx", [call.args[0] for call in near.call.call_args_list])

    def test_unresolved_sibling_report_blocks_before_signer_or_network_unless_named(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True)
        for phase in ("prepared", "broadcast", "deposited", "submitted", "polling", "unrecognized_later_phase"):
            with self.subTest(phase=phase):
                report.write_text(json.dumps(dict(result, phase=phase, verdict="UNKNOWN")), encoding="utf-8")
                fresh = report.parent / "fresh.json"
                with mock.patch("gate.oneclick.signer.NaClSigner") as signing, mock.patch("gate.oneclick.rpc.Client") as network:
                    with self.assertRaisesRegex(ValueError, "override-report"):
                        oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                        min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=fresh)
                signing.assert_not_called()
                network.assert_not_called()
                self.assertFalse(fresh.exists())
        previous = report.read_bytes()
        with mock.patch("gate.oneclick._recent_transactions", return_value={"response": {"data": []}}), \
                mock.patch("gate.oneclick.sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="OVERRIDE " + SENDER), \
                mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("override accepted")):
            allowed = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                     min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=fresh,
                                     override_reports=[report])
        self.assertIn("override accepted", allowed["reason"])
        self.assertEqual(allowed["override_reports"], [str(report)])
        self.assertEqual(report.read_bytes(), previous)

    def test_sibling_reports_are_found_without_a_json_suffix_and_each_needs_an_override(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True)
        siblings = [report.parent / "older.report", report.parent / "older", report.parent / ".oneclick-live.tmp"]
        for path in siblings:
            path.write_text(json.dumps(dict(result, phase="broadcast", verdict="UNKNOWN")), encoding="utf-8")
        with mock.patch("gate.oneclick.signer.NaClSigner") as signing, mock.patch("gate.oneclick.rpc.Client") as network:
            for overrides in (None, [siblings[0]], [report], [report.parent / "missing.json"]):
                with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, "override-report"):
                    oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                    min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json",
                                    out=report.parent / "new.json", override_reports=overrides)
        signing.assert_not_called()
        network.assert_not_called()
        with mock.patch("gate.oneclick._recent_transactions", return_value={"response": {"data": []}}), \
                mock.patch("gate.oneclick.sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="OVERRIDE " + SENDER), \
                mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("both named")):
            allowed = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                     min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json",
                                     out=report.parent / "new.json", override_reports=siblings)
        self.assertIn("both named", allowed["reason"])

    def test_final_and_prebroadcast_reports_do_not_block_a_new_run(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True)
        for phase in ("started", "ready", "settled", "deposit_failed", "deposit_expired"):
            with self.subTest(phase=phase):
                previous = dict(result, phase=phase)
                if phase in ("started", "ready"):
                    previous["deposit_hash"] = None
                    previous.pop("deposit_reference", None)
                report.write_text(json.dumps(previous), encoding="utf-8")
                with mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("no blocker")):
                    allowed = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                             min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json",
                                             out=report.parent / (phase + ".json"))
                self.assertIn("no blocker", allowed["reason"])

    def test_malformed_sibling_report_stops_before_reserving_or_signing(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True)
        report.write_text('{"version": 1, "phase": "broadcast",', encoding="utf-8")
        with mock.patch("gate.oneclick.signer.NaClSigner") as signing, mock.patch("gate.oneclick.rpc.Client") as network:
            with self.assertRaises(ValueError):
                oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=report.parent / "new.json")
        signing.assert_not_called()
        network.assert_not_called()
        self.assertFalse((report.parent / "new.json").exists())

    def test_prepared_failure_needs_chain_proof_before_a_fresh_run(self):
        result, http, near, _, _, report, factory = self.run_flow(live=True, fail_stage="prebroadcast_http")
        self.assertEqual(result["phase"], "prepared")
        self.assertIn("do not redeposit yet", result["reason"])
        self.assertNotIn("fresh run", result["reason"])
        self.assertNotIn("send_tx", [call.args[0] for call in near.call.call_args_list])
        near._exchange.side_effect = TimeoutError("synthetic unavailable deposit proof")
        with mock.patch("gate.oneclick.rpc.Client", side_effect=factory):
            resumed = oneclick.resume(report, clock=lambda: CLOCK, polls=1)
        self.assertEqual(resumed["verdict"], "UNKNOWN")
        self.assertIn("do not redeposit yet", resumed["reason"])
        self.assertNotIn("fresh run", resumed["reason"])
        near._exchange.assert_called_once()

    def test_rolled_back_prepared_report_accepts_only_a_proven_final_failure(self):
        result, _, near, _, _, report, factory = self.run_flow(live=True, deposit_failure=True)
        report.write_text(json.dumps(dict(result, phase="prepared", verdict="UNKNOWN")), encoding="utf-8")
        near._exchange.return_value = {"jsonrpc": "2.0", "id": "gate", "result": {
            "final_execution_status": "INCLUDED_FINAL"}}
        near.reset_mock()
        with mock.patch("gate.oneclick.rpc.Client", side_effect=factory):
            resumed = oneclick.resume(report, clock=lambda: CLOCK, polls=1)
        self.assertEqual((resumed["verdict"], resumed["phase"]), ("ASK", "deposit_failed"))
        self.assertEqual([call.args[1]["wait_until"] for call in near.call.call_args_list], ["FINAL"])

    def test_known_broadcast_without_an_outcome_resumes_to_a_proven_settlement(self):
        result, http, near, _, _, report, factory = self.run_flow(live=True, send_error=True)
        near._exchange.return_value = {"jsonrpc": "2.0", "id": "gate", "result": {
            "final_execution_status": "INCLUDED_FINAL"}}
        near.call.side_effect = None
        near.call.return_value = deposit_result(result["deposit_hash"], result["quote_response"])
        near.reset_mock()
        http.get.side_effect = lambda *args, **kwargs: success(result["quote_response"], result["deposit_hash"])
        with mock.patch("gate.oneclick.rpc.Client", side_effect=factory):
            resumed = oneclick.resume(report, clock=lambda: CLOCK, polls=1)
        self.assertEqual((resumed["verdict"], resumed["phase"]), ("ALLOW", "settled"))
        self.assertEqual([call.args[0] for call in near.call.call_args_list], ["tx"])
        self.assertEqual(near.call.call_args.args[1]["wait_until"], "FINAL")

    def test_account_registry_blocks_other_directories_and_missing_or_rolled_back_reports(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True, send_error=True)
        other = report.parent / "different"
        other.mkdir()
        for state in (None, {"version": 1, "phase": "ready", "deposit_hash": None}):
            with self.subTest(state=state):
                if state is None:
                    report.unlink()
                else:
                    report.write_text(json.dumps(state), encoding="utf-8")
                with mock.patch("gate.oneclick.signer.NaClSigner") as signing, mock.patch("gate.oneclick.rpc.Client") as network:
                    with self.assertRaisesRegex(ValueError, "override-report"):
                        oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                        min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=other / "new.json")
                signing.assert_not_called()
                network.assert_not_called()
                self.assertFalse((other / "new.json").exists())
        with mock.patch("gate.oneclick._recent_transactions", return_value={"response": {"data": []}}), \
                mock.patch("gate.oneclick.sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="OVERRIDE " + SENDER), \
                mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("named across directories")):
            allowed = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                     min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json",
                                     out=other / "new.json", override_reports=[report])
        self.assertIn("named across directories", allowed["reason"])
        with mock.patch("gate.oneclick.signer.NaClSigner") as signing:
            with self.assertRaisesRegex(ValueError, "override-report"):
                oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=other / "again.json")
        signing.assert_not_called()

    def test_registry_reservations_are_released_after_unsigned_refusal_or_proven_completion(self):
        for options in ({"fail_stage": "signer"}, {}, {"deposit_failure": True}):
            with self.subTest(options=options):
                result, _, _, _, _, report, _ = self.run_flow(live=True, **options)
                other = report.parent / "different"
                other.mkdir()
                with mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("reservation released")):
                    allowed = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                             min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=other / "new.json")
                self.assertIn("reservation released", allowed["reason"])

    def test_registry_is_account_scoped(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True, send_error=True)
        other = report.parent / "different"
        other.mkdir()
        with mock.patch("gate.oneclick.rpc.Client") as network:
            allowed = oneclick.settle("different.near", result["destination"], result["amount"], result["recipient"],
                                     min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=other / "new.json")
        self.assertIn("key file does not name the sender", allowed["reason"])
        network.assert_not_called()

    def test_failed_resume_does_not_release_a_loaded_terminal_phase(self):
        result, _, near, _, _, report, factory = self.run_flow(live=True, send_error=True)
        other = report.parent / "different"
        other.mkdir()
        near.call.side_effect = TimeoutError("synthetic missing final proof")
        near._exchange.side_effect = TimeoutError("synthetic missing final proof")
        for phase in ("settled", "deposit_failed", "deposit_expired"):
            with self.subTest(phase=phase):
                report.write_text(json.dumps(dict(result, phase=phase, verdict="ASK", registry_released=True)), encoding="utf-8")
                with mock.patch("gate.oneclick.rpc.Client", side_effect=factory):
                    resumed = oneclick.resume(report, clock=lambda: CLOCK, polls=1)
                self.assertEqual(resumed["verdict"], "UNKNOWN")
                self.assertNotIn("registry_released", resumed)
                with mock.patch("gate.oneclick.signer.NaClSigner") as signing:
                    with self.assertRaisesRegex(ValueError, "override-report"):
                        oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                        min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=other / "new.json")
                signing.assert_not_called()

    def test_unsaved_terminal_result_does_not_release_the_reservation(self):
        save = oneclick._save
        def write(state, handle):
            if state["phase"] == "settled":
                raise OSError("synthetic final report write failure")
            return save(state, handle)
        with mock.patch("gate.oneclick._save", side_effect=write):
            with self.assertRaisesRegex(OSError, "final report write failure"):
                self.run_flow(live=True)
        report = self.home.parent / "report.json"
        result = json.loads(report.read_text())
        other = report.parent / "different"
        other.mkdir()
        with mock.patch("gate.oneclick.signer.NaClSigner") as signing:
            with self.assertRaisesRegex(ValueError, "override-report"):
                oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=other / "new.json")
        signing.assert_not_called()

    def test_report_save_failure_before_signing_releases_only_its_reservation(self):
        save = oneclick._save
        for phase in ("started", "ready"):
            with self.subTest(phase=phase):
                def write(state, handle):
                    if state["phase"] == phase:
                        with oneclick._run_registry(SENDER) as registry:
                            registry.execute("INSERT OR IGNORE INTO open_runs (report) VALUES (?)",
                                             (oneclick._report_key(handle.parent / "other.json"),))
                        raise OSError("synthetic unsigned save failure")
                    return save(state, handle)
                with mock.patch("gate.oneclick._save", side_effect=write):
                    with self.assertRaisesRegex(OSError, "unsigned save failure"):
                        self.run_flow(live=True)
                with oneclick._run_registry(SENDER) as registry:
                    self.assertEqual(registry.execute("SELECT report FROM open_runs").fetchall(),
                                     [(oneclick._report_key(self.home.parent / "other.json"),)])

    def test_release_of_an_active_unsigned_run_prevents_it_from_signing(self):
        save = oneclick._save
        released = False
        def write(state, handle):
            nonlocal released
            save(state, handle)
            if state["phase"] == "ready" and not released:
                released = True
                oneclick.release(SENDER, handle)
        with mock.patch("gate.oneclick._save", side_effect=write):
            result, _, near, signing, *_ = self.run_flow(live=True)
        self.assertIn("released before signing", result["reason"])
        signing.return_value.sign.assert_not_called()
        self.assertNotIn("send_tx", [call.args[0] for call in near.call.call_args_list])

    def test_override_of_an_active_run_prevents_a_second_signature(self):
        response = quote()
        request = response["quoteRequest"]
        catalog = rpc.Client(intents.ONECLICK_URL, "replay", kind="http", fixtures_dir=ROOT).get("/v0/tokens")
        save = oneclick._save
        cases = [(marker, False) for marker in (0, 1, None, -1, "unknown")]
        if os.name == "nt":
            cases.append((0, True))
        for marker, missing_mixed_case in cases:
            with self.subTest(marker=marker, missing_mixed_case=missing_mixed_case):
                paused, release = threading.Event(), threading.Event()
                reports = []
                def write(state, handle):
                    save(state, handle)
                    if state["phase"] == "ready" and not paused.is_set():
                        reports.append(handle)
                        paused.set()
                        if not release.wait(10):
                            raise AssertionError("override did not finish before the older run resumed")
                with mock.patch("gate.oneclick._save", side_effect=write), ThreadPoolExecutor(max_workers=1) as pool:
                    older = pool.submit(self.run_flow, live=True,
                                        report_name="Run.json" if missing_mixed_case else "report.json")
                    try:
                        self.assertTrue(paused.wait(10))
                        report = reports[0]
                        with oneclick._run_registry(SENDER) as registry:
                            self.assertEqual(registry.execute("SELECT signing_attempted FROM open_runs WHERE report = ?",
                                                              (oneclick._report_key(report),)).fetchone(), (0,))
                            registry.execute("UPDATE open_runs SET signing_attempted = ? WHERE report = ?",
                                             (marker, oneclick._report_key(report)))
                        override = report
                        if missing_mixed_case:
                            report.unlink()
                            override = report.with_name("run.json")
                            self.assertEqual(report, override)
                            self.assertNotEqual(str(report), str(override))
                            self.assertFalse(override.exists())
                        out = report.parent / "override" / "run.json"
                        out.parent.mkdir()
                        http = mock.Mock()
                        http.get.side_effect = [catalog, status(response), status(response)]
                        http.post.return_value = response
                        near = self.funding_client()
                        key = near.call.return_value
                        def call(method, params):
                            if method == "status":
                                return {"chain_id": "mainnet"}
                            if method == "query":
                                return key
                            self.assertEqual(method, "send_tx")
                            raise TimeoutError("synthetic overriding broadcast")
                        near.call.side_effect = call
                        evm, _ = evm_client(response)
                        tx_signer = transaction_signer()
                        def factory(url, *args, **kwargs):
                            return http if url == intents.ONECLICK_URL else near if url == rpc.MAINNET_RPC_URL else evm
                        with mock.patch("gate.oneclick.rpc.Client", side_effect=factory), \
                                mock.patch("gate.intents.judge_quote", return_value=judgement(response)), \
                                mock.patch("gate.oneclick.signer.NaClSigner", return_value=tx_signer), \
                                mock.patch("gate.oneclick._recent_transactions", return_value={"response": {"data": []}}), \
                                mock.patch("gate.oneclick.sys.stdin.isatty", return_value=True), \
                                mock.patch("builtins.input", return_value="OVERRIDE " + SENDER) as prompt:
                            replacement = oneclick.settle(SENDER, request["destinationAsset"], request["amount"],
                                request["recipient"], min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json",
                                out=out, override_reports=[override], clock=lambda: CLOCK, polls=1)
                        self.assertEqual(prompt.call_count, int(marker != 0))
                        self.assertEqual((replacement["verdict"], replacement["phase"]), ("UNKNOWN", "broadcast"))
                        self.assertIn("synthetic overriding broadcast", replacement["reason"])
                        tx_signer.sign.assert_called_once()
                        with oneclick._run_registry(SENDER) as registry:
                            previous_row = registry.execute("SELECT signing_attempted FROM open_runs WHERE report = ?",
                                                            (oneclick._report_key(report),)).fetchone()
                            self.assertEqual(registry.execute("SELECT signing_attempted FROM open_runs WHERE report = ?",
                                                              (oneclick._report_key(out),)).fetchone(), (1,))
                    finally:
                        release.set()
                    result, _, near, signing, *_ = older.result(timeout=10)
                signing.return_value.sign.assert_not_called()
                self.assertNotIn("send_tx", [call.args[0] for call in near.call.call_args_list])
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertIsNone(result["deposit_hash"])
                self.assertIn("nothing was signed", result["reason"])
                self.assertEqual(previous_row, None if marker == 0 else (marker,))
                with oneclick._run_registry(SENDER) as registry:
                    expected = [(oneclick._report_key(out), 1)] + ([] if marker == 0 else [(oneclick._report_key(report), marker)])
                    self.assertEqual(sorted(registry.execute("SELECT report, signing_attempted FROM open_runs").fetchall()),
                                     sorted(expected))

    def test_override_failed_unsigned_delete_rolls_back_before_key_read(self):
        request = quote()["quoteRequest"]
        reports = [self.home / "first.json", self.home / "second.json"]
        with oneclick._run_registry(SENDER) as registry:
            for index, report in enumerate(reports, 1):
                registry.execute("INSERT INTO open_runs (report, signing_attempted, reserved_at_ms) VALUES (?, 0, ?)",
                                 (oneclick._report_key(report), index))
            registry.execute("CREATE TRIGGER keep_unsigned BEFORE DELETE ON open_runs "
                             "WHEN OLD.reserved_at_ms = 2 BEGIN SELECT RAISE(IGNORE); END")
        output = self.home / "reports" / "new.json"
        output.parent.mkdir()
        with mock.patch("gate.oneclick.Path.read_text", side_effect=AssertionError("key read before revocation")), \
                mock.patch("gate.oneclick._recent_transactions", return_value={"response": {"data": []}}) as lookup, \
                mock.patch("gate.oneclick.signer.NaClSigner") as signing, \
                mock.patch("gate.oneclick.rpc.Client") as network:
            with self.assertRaisesRegex(ValueError, "could not revoke.*unsigned"):
                oneclick.settle(SENDER, request["destinationAsset"], request["amount"], request["recipient"],
                                min_amount_out=FLOOR, live=True, key_file=self.home / "never-read.json", out=output,
                                override_reports=reports, clock=lambda: CLOCK)
        lookup.assert_not_called()
        signing.assert_not_called()
        network.assert_not_called()
        self.assertFalse(output.exists())
        with oneclick._run_registry(SENDER) as registry:
            self.assertEqual(sorted(registry.execute("SELECT report, signing_attempted FROM open_runs").fetchall()),
                             sorted((oneclick._report_key(report), 0) for report in reports))

    def test_registry_stores_canonical_paths_and_resumes_with_the_same_key(self):
        result, http, near, _, _, report, factory = self.run_flow(live=True, send_error=True, report_name="Run.json")
        expected = str(report.resolve())
        if os.name == "nt":
            expected = expected.casefold()
        database = self.home / ".siyab-oneclick" / (SENDER + ".sqlite3")
        with contextlib.closing(oneclick.sqlite3.connect(database)) as registry:
            self.assertEqual(registry.execute("SELECT report, signing_attempted FROM open_runs").fetchall(), [(expected, 1)])
        alias = report.with_name("run.json") if os.name == "nt" else report.parent / "unused" / ".." / report.name
        near._exchange.return_value = {"jsonrpc": "2.0", "id": "gate", "result": {
            "final_execution_status": "INCLUDED_FINAL"}}
        near.call.side_effect = None
        near.call.return_value = deposit_result(result["deposit_hash"], result["quote_response"])
        http.get.side_effect = lambda *args, **kwargs: success(result["quote_response"], result["deposit_hash"])
        with mock.patch("gate.oneclick.rpc.Client", side_effect=factory), mock.patch("gate.oneclick.signer.NaClSigner") as signing:
            resumed = oneclick.resume(alias, clock=lambda: CLOCK, polls=1)
        self.assertEqual(resumed["verdict"], "ALLOW")
        signing.assert_not_called()
        with oneclick._run_registry(SENDER) as registry:
            self.assertEqual(registry.execute("SELECT report FROM open_runs").fetchall(), [])

    def test_registry_normalizes_legacy_paths_without_changing_markers(self):
        report = self.home / "Run.json"
        report.write_text(json.dumps({"version": 1, "sender": SENDER, "phase": "ready", "deposit_hash": None}),
                          encoding="utf-8")
        stored = str(report.parent / "unused" / ".." / report.name)
        with oneclick._run_registry(SENDER) as registry:
            registry.execute("INSERT INTO open_runs (report, signing_attempted, reserved_at_ms) VALUES (?, 0, 123)", (stored,))
        expected = str(report.resolve())
        if os.name == "nt":
            expected = expected.casefold()
        with oneclick._run_registry(SENDER) as registry:
            self.assertEqual(registry.execute("SELECT report, signing_attempted, reserved_at_ms FROM open_runs").fetchall(),
                             [(expected, 0, 123)])
        alias = report.with_name("run.json") if os.name == "nt" else report
        self.assertIs(oneclick.release(SENDER, alias)["registry_released"], True)
        with oneclick._run_registry(SENDER) as registry:
            self.assertEqual(registry.execute("SELECT report FROM open_runs").fetchall(), [])

    def test_registry_canonical_collision_refuses_and_preserves_both_legacy_rows(self):
        request = quote()["quoteRequest"]
        report = self.home / "Run.json"
        alias = report.with_name("run.json") if os.name == "nt" else report.parent / "unused" / ".." / report.name
        rows = [(str(report), 0, 123), (str(alias), 1, 456)]
        with oneclick._run_registry(SENDER) as registry:
            registry.executemany("INSERT INTO open_runs (report, signing_attempted, reserved_at_ms) VALUES (?, ?, ?)", rows)
        output = self.home / "reports" / "new.json"
        output.parent.mkdir()
        with mock.patch("gate.oneclick.Path.read_text", side_effect=AssertionError("key read before collision refusal")), \
                mock.patch("gate.oneclick.signer.NaClSigner") as signing, \
                mock.patch("gate.oneclick.rpc.Client") as network:
            with self.assertRaisesRegex(ValueError, "registry is unavailable.*UNIQUE"):
                oneclick.settle(SENDER, request["destinationAsset"], request["amount"], request["recipient"],
                                min_amount_out=FLOOR, live=True, key_file=self.home / "never-read.json", out=output,
                                override_reports=[report], clock=lambda: CLOCK)
        signing.assert_not_called()
        network.assert_not_called()
        self.assertFalse(output.exists())
        database = self.home / ".siyab-oneclick" / (SENDER + ".sqlite3")
        with contextlib.closing(oneclick.sqlite3.connect(database)) as registry:
            self.assertEqual(sorted(registry.execute("SELECT report, signing_attempted, reserved_at_ms FROM open_runs").fetchall()),
                             sorted(rows))

    def test_failed_prepared_report_save_keeps_the_signing_marker(self):
        save = oneclick._save
        def write(state, handle):
            if state["phase"] == "prepared":
                raise OSError("synthetic signed save failure")
            return save(state, handle)
        with mock.patch("gate.oneclick._save", side_effect=write):
            with self.assertRaisesRegex(OSError, "signed save failure"):
                self.run_flow(live=True)
        report = self.home.parent / "report.json"
        self.assertEqual(json.loads(report.read_text())["phase"], "ready")
        with oneclick._run_registry(SENDER) as registry:
            self.assertEqual(registry.execute("SELECT signing_attempted FROM open_runs WHERE report = ?",
                                              (oneclick._report_key(report),)).fetchone(), (1,))
        with self.assertRaisesRegex(ValueError, "cannot prove.*before signing"):
            oneclick.release(SENDER, report)

    def test_signing_marker_precedes_sign_and_survives_ready_report_rollback(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True, send_error=True)
        with oneclick._run_registry(SENDER) as registry:
            self.assertEqual(registry.execute("SELECT signing_attempted FROM open_runs WHERE report = ?",
                                              (oneclick._report_key(report),)).fetchone(), (1,))
        rolled_back = dict(result, phase="ready", deposit_hash=None)
        rolled_back.pop("deposit_reference")
        report.write_text(json.dumps(rolled_back), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cannot prove.*before signing"):
            oneclick.release(SENDER, report)

    def test_override_prints_rollback_warning_and_records_the_sender_lookup(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True, send_error=True)
        recent = rpc.Client(rpc.NEARBLOCKS_URL, "off", kind="http")
        response = {"data": [{"signer_account_id": SENDER, "transaction_hash": result["deposit_hash"],
                              "receiver_account_id": "wrap.near", "block_timestamp": str(int(CLOCK * 10**9))}],
                    "meta": {"next_page": "more"}}
        with mock.patch("gate.oneclick.rpc.Client", return_value=recent):
            with mock.patch.object(recent, "_exchange", return_value=response) as lookup:
                with mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("stop before signing")):
                    with contextlib.redirect_stdout(io.StringIO()) as printed:
                        refused = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                            min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json",
                            out=report.parent / "new.json", override_reports=[report])
        self.assertIn("Windows power cut", printed.getvalue())
        self.assertIn("back to ready", printed.getvalue())
        self.assertIn("Check the chain before any override", printed.getvalue())
        self.assertIn(f"/v3/accounts/{SENDER}/txns?signer={SENDER}&receiver=wrap.near&limit=10", printed.getvalue())
        self.assertIn(result["deposit_hash"], printed.getvalue())
        self.assertEqual(lookup.call_args.args[:3], (rpc.NEARBLOCKS_URL + f"/v3/accounts/{SENDER}/txns",
                                                    "GET", {"signer": SENDER, "receiver": "wrap.near", "limit": 10}))
        self.assertEqual(refused["override_lookup"]["response"], response)
        with oneclick._run_registry(SENDER) as registry:
            self.assertEqual(registry.execute("SELECT report FROM open_runs").fetchall(), [(oneclick._report_key(report),)])

    def test_override_lookup_error_stops_before_key_loading_and_preserves_old_row(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True, send_error=True)
        for response in (None, {}, {"data": []}, {"data": [], "errors": [{"message": "synthetic partial lookup"}]},
                         {"data": [{}]}, {"data": [1] * 11},
                         {"data": [{"signer_account_id": "other.near", "transaction_hash": result["deposit_hash"]}]}):
            with self.subTest(response=response):
                recent = rpc.Client(rpc.NEARBLOCKS_URL, "off", kind="http")
                error = rpc.HttpError(403, rpc.NEARBLOCKS_URL)
                with mock.patch("gate.oneclick.rpc.Client", return_value=recent), \
                        mock.patch("gate.oneclick.sys.stdin.isatty", return_value=False):
                    with mock.patch.object(recent, "_exchange", return_value=response,
                                           side_effect=error if response is None else None):
                        with mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("lookup passed")) as signing:
                            with contextlib.redirect_stdout(io.StringIO()):
                                refused = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                    min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json",
                                    out=report.parent / "new.json", override_reports=[report])
                signing.assert_not_called()
                self.assertEqual(refused["verdict"], "UNKNOWN")
                self.assertIn("non-interactive", refused["reason"])
                (report.parent / "new.json").unlink()
                with oneclick._run_registry(SENDER) as registry:
                    self.assertEqual(registry.execute("SELECT report FROM open_runs").fetchall(), [(oneclick._report_key(report),)])

    def test_override_recent_wrap_or_failed_lookup_requires_typed_interactive_confirmation_before_key_read(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True, send_error=True)
        read = Path.read_text
        for problem in ("recent_wrap", "lookup_failed", "legacy_reservation"):
            for tty, answer in ((False, "OVERRIDE " + SENDER), (True, "yes"), (True, "OVERRIDE " + SENDER),
                                (True, EOFError("synthetic closed input"))):
                with self.subTest(problem=problem, tty=tty, answer=answer):
                    with oneclick._run_registry(SENDER) as registry:
                        registry.execute("UPDATE open_runs SET reserved_at_ms = ? WHERE report = ?",
                                         (None if problem == "legacy_reservation" else int(CLOCK * 1000), oneclick._report_key(report)))
                    response = {"data": [] if problem != "recent_wrap" else [{
                        "signer_account_id": SENDER, "receiver_account_id": "wrap.near",
                        "transaction_hash": result["deposit_hash"], "block_timestamp": str(int(CLOCK * 10**9) + 1)}]}
                    options = ({"side_effect": rpc.HttpError(503, rpc.NEARBLOCKS_URL)} if problem == "lookup_failed"
                               else {"return_value": {"response": response}})
                    loaded = []
                    def load(path, *args, **kwargs):
                        if path == report.parent / "key.json":
                            loaded.append(path)
                        return read(path, *args, **kwargs)
                    output = report.parent / "new.json"
                    with mock.patch("gate.oneclick._recent_transactions", **options), \
                            mock.patch("gate.oneclick.sys.stdin.isatty", return_value=tty), \
                            mock.patch("builtins.input", **({"side_effect": answer} if isinstance(answer, EOFError)
                                                           else {"return_value": answer})) as prompt, \
                            mock.patch("gate.oneclick.Path.read_text", new=load), \
                            mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("after confirmation")) as signing, \
                            contextlib.redirect_stdout(io.StringIO()):
                        state = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                            min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=output,
                            override_reports=[report], clock=lambda: CLOCK)
                    if tty and answer == "OVERRIDE " + SENDER:
                        self.assertEqual(len(loaded), 1)
                        signing.assert_called_once()
                        self.assertIs(state["override_confirmed"], True)
                        self.assertIn("after confirmation", state["reason"])
                    else:
                        self.assertEqual(loaded, [])
                        signing.assert_not_called()
                        self.assertIn("confirmation", state["reason"])
                        self.assertIn("non-interactive" if not tty else "nothing was signed", state["reason"])
                    self.assertEqual(prompt.call_count, int(tty))
                    self.assertIn("override_confirmation_required", state)
                    with oneclick._run_registry(SENDER) as registry:
                        self.assertEqual(registry.execute("SELECT report FROM open_runs").fetchall(), [(oneclick._report_key(report),)])
                    output.unlink()

    def test_override_compares_skew_adjusted_nanoseconds_with_every_unsigned_reservation(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True, send_error=True)
        other = report.parent / "other.json"
        cutoff = int(CLOCK * 1000) - 5000
        for offset in (-1, 0, 1, 1000000000):
            with self.subTest(offset=offset):
                with oneclick._run_registry(SENDER) as registry:
                    registry.execute("INSERT OR REPLACE INTO open_runs (report, signing_attempted, reserved_at_ms) VALUES (?, 0, ?)",
                                     (oneclick._report_key(report), int(CLOCK * 1000)))
                    registry.execute("INSERT OR REPLACE INTO open_runs (report, signing_attempted, reserved_at_ms) VALUES (?, 0, ?)",
                                     (oneclick._report_key(other), int(CLOCK * 1000) + 1000))
                recent = {"response": {"data": [{"signer_account_id": SENDER, "receiver_account_id": "wrap.near",
                    "transaction_hash": result["deposit_hash"], "block_timestamp": str(cutoff * 1000000 + offset)}]}}
                out = report.parent / "new.json"
                with mock.patch("gate.oneclick._recent_transactions", return_value=recent), \
                        mock.patch("gate.oneclick.sys.stdin.isatty", return_value=False), \
                        mock.patch("builtins.input", side_effect=AssertionError("non-interactive prompt")), \
                        mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("older lookup passed")) as signing, \
                        contextlib.redirect_stdout(io.StringIO()):
                    state = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                        min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=out,
                        override_reports=[report, other], clock=lambda: CLOCK)
                if offset < 0:
                    signing.assert_called_once()
                    self.assertNotIn("override_confirmation_required", state)
                    self.assertIn("older lookup passed", state["reason"])
                else:
                    signing.assert_not_called()
                    self.assertIn("non-interactive", state["reason"])
                    self.assertEqual(len(state["override_confirmation_required"]), 2 if offset == 1000000000 else 1)
                    self.assertIn(str(report), " ".join(state["override_confirmation_required"]))
                out.unlink()

    def test_override_signed_or_unknown_row_prompts_even_without_indexed_activity(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True, send_error=True)
        read = Path.read_text
        for marker in (1, None, -1, "unknown"):
            with self.subTest(marker=marker):
                with oneclick._run_registry(SENDER) as registry:
                    registry.execute("UPDATE open_runs SET signing_attempted = ?, reserved_at_ms = ?",
                                     (marker, int(CLOCK * 1000)))
                loaded = []
                def load(path, *args, **kwargs):
                    if path == report.parent / "key.json":
                        loaded.append(path)
                    return read(path, *args, **kwargs)
                out = report.parent / "new.json"
                with mock.patch("gate.oneclick._recent_transactions", return_value={"response": {"data": []}}), \
                        mock.patch("gate.oneclick.sys.stdin.isatty", return_value=False), \
                        mock.patch("builtins.input", side_effect=AssertionError("non-interactive prompt")), \
                        mock.patch("gate.oneclick.Path.read_text", new=load), \
                        mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("signing marker ignored")) as signing, \
                        contextlib.redirect_stdout(io.StringIO()):
                    state = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                        min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=out,
                        override_reports=[report], clock=lambda: CLOCK)
                self.assertEqual(loaded, [])
                signing.assert_not_called()
                self.assertEqual(state["verdict"], "UNKNOWN")
                self.assertIn("non-interactive", state["reason"])
                self.assertIn("signing", state["override_confirmation_required"][0])
                self.assertEqual(json.loads(out.read_text())["override_confirmation_required"], state["override_confirmation_required"])
                with oneclick._run_registry(SENDER) as registry:
                    self.assertEqual(registry.execute("SELECT report FROM open_runs").fetchall(), [(oneclick._report_key(report),)])
                out.unlink()

    def test_override_unsigned_row_with_old_activity_needs_no_prompt(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True, send_error=True)
        with oneclick._run_registry(SENDER) as registry:
            registry.execute("UPDATE open_runs SET signing_attempted = 0, reserved_at_ms = ?", (int(CLOCK * 1000),))
        recent = {"response": {"data": [{"signer_account_id": SENDER, "receiver_account_id": "wrap.near",
            "transaction_hash": result["deposit_hash"], "block_timestamp": str(int(CLOCK - 10) * 10**9)}]}}
        with mock.patch("gate.oneclick._recent_transactions", return_value=recent), \
                mock.patch("gate.oneclick.sys.stdin.isatty", return_value=False), \
                mock.patch("builtins.input", side_effect=AssertionError("old activity prompted")), \
                mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("unsigned old activity passed")) as signing, \
                contextlib.redirect_stdout(io.StringIO()):
            state = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=report.parent / "new.json",
                override_reports=[report], clock=lambda: CLOCK)
        signing.assert_called_once()
        self.assertNotIn("override_confirmation_required", state)
        self.assertIn("unsigned old activity passed", state["reason"])

    def test_override_rejects_millisecond_timestamps_before_key_loading(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True, send_error=True)
        recent = rpc.Client(rpc.NEARBLOCKS_URL, "off", kind="http")
        response = {"data": [{"signer_account_id": SENDER, "receiver_account_id": "wrap.near",
            "transaction_hash": result["deposit_hash"], "block_timestamp": str(int(CLOCK * 1000))}]}
        with mock.patch("gate.oneclick.rpc.Client", return_value=recent), \
                mock.patch.object(recent, "_exchange", return_value=response), \
                mock.patch("gate.oneclick.sys.stdin.isatty", return_value=False), \
                mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("wrong units passed")) as signing, \
                contextlib.redirect_stdout(io.StringIO()):
            state = oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=report.parent / "new.json",
                override_reports=[report], clock=lambda: CLOCK)
        signing.assert_not_called()
        self.assertIn("non-interactive", state["reason"])
        self.assertIn("timestamp", state["override_lookup"]["error"])

    def test_signing_exception_keeps_the_reservation_even_without_a_saved_hash(self):
        result, _, near, _, _, report, _ = self.run_flow(live=True, fail_stage="signed_output")
        self.assertIsNone(result["deposit_hash"])
        self.assertEqual(result["verdict"], "UNKNOWN")
        other = report.parent / "different"
        other.mkdir()
        with mock.patch("gate.oneclick.signer.NaClSigner") as signing:
            with self.assertRaisesRegex(ValueError, "override-report"):
                oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=other / "new.json")
        signing.assert_not_called()
        self.assertNotIn("send_tx", [call.args[0] for call in near.call.call_args_list])

    def test_two_concurrent_runs_reserve_only_one_before_key_loading(self):
        request = quote()["quoteRequest"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key.json"
            key.write_text(json.dumps({"account_id": SENDER}), encoding="utf-8")
            outputs = [root / "first" / "run.json", root / "second" / "run.json"]
            for path in outputs:
                path.parent.mkdir()
            start = threading.Barrier(2)
            entered, release = threading.Event(), threading.Event()
            def load_key(path):
                entered.set()
                if not release.wait(10):
                    raise AssertionError("concurrent run did not finish")
                raise oneclick.signer.SignerError("synthetic signer refusal")
            def run(path):
                start.wait(10)
                try:
                    return oneclick.settle(SENDER, request["destinationAsset"], request["amount"], request["recipient"],
                                           min_amount_out=FLOOR, live=True, key_file=key, out=path)
                except ValueError as error:
                    return str(error)
            with mock.patch("gate.oneclick.signer.NaClSigner", side_effect=load_key) as signing:
                with mock.patch("gate.oneclick.rpc.Client") as network, ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(run, path) for path in outputs]
                    try:
                        self.assertTrue(entered.wait(10))
                        done, pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
                        self.assertEqual((len(done), len(pending)), (1, 1))
                        self.assertIn("override-report", next(iter(done)).result())
                    finally:
                        release.set()
                    answers = [future.result(timeout=10) for future in futures]
            signing.assert_called_once()
            network.assert_not_called()
            self.assertEqual(sum(path.exists() for path in outputs), 1)
            self.assertEqual(sum(isinstance(answer, dict) for answer in answers), 1)

    def test_prepared_reference_or_signed_payload_blocks_even_without_a_hash(self):
        result, _, _, _, _, report, _ = self.run_flow(live=True)
        for field in ("deposit_reference", "signed_tx_base64", "signed_transaction", "signed_tx"):
            with self.subTest(field=field):
                previous = {"version": 1, "phase": "prepared", "deposit_hash": None, field: "synthetic marker"}
                report.write_text(json.dumps(previous), encoding="utf-8")
                with mock.patch("gate.oneclick.signer.NaClSigner") as signing, mock.patch("gate.oneclick.rpc.Client") as network:
                    with self.assertRaisesRegex(ValueError, "override-report"):
                        oneclick.settle(SENDER, result["destination"], result["amount"], result["recipient"],
                                        min_amount_out=FLOOR, live=True, key_file=report.parent / "key.json", out=report.parent / "new.json")
                signing.assert_not_called()
                network.assert_not_called()

    def test_home_registry_uses_private_modes_and_durable_exclusive_transactions(self):
        report = self.home / "Run.json"
        with mock.patch("gate.oneclick.os", wraps=os) as system, mock.patch("gate.oneclick.Path.chmod") as chmod:
            system.name = "posix"
            with mock.patch("gate.oneclick._sync_directory") as sync:
                with oneclick._run_registry(SENDER) as registry:
                    self.assertEqual(registry.execute("PRAGMA journal_mode").fetchone(), ("delete",))
                    self.assertEqual(registry.execute("PRAGMA synchronous").fetchone(), (3,))
                    registry.execute("INSERT INTO open_runs (report) VALUES (?)", (oneclick._report_key(report),))
        directory = self.home / ".siyab-oneclick"
        self.assertTrue((directory / (SENDER + ".sqlite3")).is_file())
        self.assertEqual(chmod.call_args_list, [mock.call(0o700), mock.call(0o600)])
        self.assertEqual(sync.call_args_list, [mock.call(self.home), mock.call(directory)])
        with oneclick._run_registry(SENDER) as registry:
            self.assertEqual(registry.execute("SELECT report FROM open_runs").fetchall(), [(oneclick._report_key(report),)])

    def test_other_process_is_refused_while_the_account_registry_is_locked(self):
        request = quote()["quoteRequest"]
        out = self.home / "reports"
        out.mkdir()
        program = """
import json
import sqlite3
import sys
from pathlib import Path
from unittest import mock
from gate import oneclick
home, output, encoded = sys.argv[1:]
request = json.loads(encoded)
connect = sqlite3.connect
def short_lock(path, **kwargs):
    return connect(path, **dict(kwargs, timeout=0.05))
with mock.patch('gate.oneclick.Path.home', return_value=Path(home)):
    with mock.patch('gate.oneclick.sqlite3.connect', side_effect=short_lock):
        with mock.patch('gate.oneclick.Path.read_text', side_effect=AssertionError('key read before lock')):
            with mock.patch('gate.oneclick.rpc.Client', side_effect=AssertionError('network before lock')):
                try:
                    oneclick.settle('payer.near', request['destinationAsset'], request['amount'], request['recipient'],
                                    min_amount_out='280000', live=True, key_file=Path(home) / 'never-read.json', out=output)
                except ValueError as error:
                    print(error)
                else:
                    raise AssertionError('locked account was accepted')
"""
        with oneclick._run_registry(SENDER):
            completed = subprocess.run([sys.executable, "-c", program, str(self.home), str(out / "new.json"),
                                        json.dumps(request)], capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("registry is unavailable", completed.stdout)
        self.assertIn("locked", completed.stdout)
        self.assertFalse((out / "new.json").exists())

    def test_pending_receipt_and_finality_poll_the_same_swap_at_thirty_seconds(self):
        response = quote()
        state = {"quote_response": response, "deposit_hash": "original", "phase": "deposited"}
        http = mock.Mock()
        http.get.return_value = success(response, "original")
        evm, replies = evm_client(response)
        attempts = 0
        def call(method, params):
            nonlocal attempts
            if method == "eth_getTransactionReceipt":
                attempts += 1
                if attempts == 1:
                    raise rpc.MalformedResponse("the JSON-RPC body has no result")
                if attempts == 2:
                    return dict(replies[method], blockNumber="0x65")
            return copy.deepcopy(replies[method])
        evm.call.side_effect = call
        sleep = mock.Mock()
        result = oneclick._poll(state, http, evm, clock=lambda: CLOCK, sleep=sleep, polls=3, handle=None)
        self.assertEqual(result["verdict"], "ALLOW")
        self.assertEqual(sleep.call_args_list, [mock.call(30), mock.call(30)])
        self.assertEqual(http.get.call_count, 3)
        http.post.assert_not_called()

    def test_key_identity_and_signer_failures_happen_before_network(self):
        response = quote()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key.json"
            for case in ("missing", "wrong_account", "missing_pynacl", "invalid_key"):
                with self.subTest(case=case):
                    if case != "missing":
                        key.write_text(json.dumps({"account_id": "other.near" if case == "wrong_account" else SENDER}), encoding="utf-8")
                    refusal = (mock.patch("gate.oneclick.signer.NaClSigner", side_effect=oneclick.signer.SignerError("invalid key"))
                               if case == "invalid_key" else contextlib.nullcontext())
                    with mock.patch("gate.oneclick.rpc.Client", side_effect=AssertionError("network before signer")) as client:
                        with mock.patch.dict("sys.modules", {"nacl": None}), refusal:
                            result = oneclick.settle(SENDER, response["quoteRequest"]["destinationAsset"],
                                                     response["quoteRequest"]["amount"], response["quoteRequest"]["recipient"],
                                                     min_amount_out=FLOOR, live=True, key_file=key,
                                                     out=root / (case + ".json"), clock=lambda: CLOCK)
                    self.assertEqual(result["verdict"], "UNKNOWN")
                    self.assertIsNone(result["deposit_hash"])
                    if case == "invalid_key":
                        self.assertIn("invalid key", result["reason"])
                    client.assert_not_called()

    def test_malformed_nested_quote_returns_unknown(self):
        for field in ("quote", "quoteRequest"):
            with self.subTest(field=field):
                malformed = quote()
                malformed[field] = None
                result, _, near, *_ = self.run_flow(malformed=malformed)
                self.assertEqual(result["verdict"], "UNKNOWN")
                self.assertNotIn("send_tx", [call.args[0] for call in near.call.call_args_list])


class InputTests(Case):
    def test_sender_recipient_and_destination_stop_before_network(self):
        request = quote()["quoteRequest"]
        args = [SENDER, request["destinationAsset"], request["amount"], request["recipient"]]
        for index, value in ((0, "bad sender"), (0, ""), (1, "nep141:wrap.near"),
                             (1, "nep141:base-unlisted.omft.near"), (3, "alice.near"), (3, "0x12")):
            with self.subTest(index=index, value=value):
                changed = list(args)
                changed[index] = value
                with self.assertRaises(ValueError):
                    oneclick.settle(*changed, min_amount_out=FLOOR)
        self.urlopen.assert_not_called()


class CliAndInputTests(Case):
    def test_release_routes_to_nonspending_path_and_reports_refusal(self):
        answer = {"verdict": "ASK", "phase": "deposit_expired", "reason": "proved expiry", "deposit_hash": "hash",
                  "registry_released": True}
        args = ["intents-release", "--sender", SENDER, "--report", "old.json"]
        with mock.patch("gate.oneclick.release", return_value=answer) as release:
            with mock.patch("gate.oneclick.settle") as settle, mock.patch("gate.oneclick.resume") as resume:
                with contextlib.redirect_stdout(io.StringIO()) as printed:
                    self.assertEqual(gate_main.main(args), 0)
        release.assert_called_once_with(SENDER, "old.json")
        settle.assert_not_called()
        resume.assert_not_called()
        self.assertIn("proved expiry", printed.getvalue())
        for failure in (ValueError("still within its transaction validity window"),
                        rpc.HttpError(403, rpc.MAINNET_RPC_URL), rpc.RpcError({"code": -32000, "message": "proof unavailable"})):
            with self.subTest(failure=failure):
                with mock.patch("gate.oneclick.release", side_effect=failure):
                    with contextlib.redirect_stderr(io.StringIO()) as error:
                        self.assertEqual(gate_main.main(args), 2)
                self.assertIn(str(failure), error.getvalue())

    def test_deposit_flag_cannot_be_abbreviated(self):
        request = quote()["quoteRequest"]
        args = ["intents-settle", "--sender", SENDER, "--destination", request["destinationAsset"],
                "--amount", request["amount"], "--recipient", request["recipient"], "--min-amount-out", FLOOR, "--dep"]
        with mock.patch("gate.oneclick.settle") as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                gate_main.main(args)
        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_cli_prints_the_final_recorded_storage_deposit_and_passes_named_override(self):
        request = quote()["quoteRequest"]
        args = ["intents-settle", "--sender", SENDER, "--destination", request["destinationAsset"],
                "--amount", request["amount"], "--recipient", request["recipient"], "--min-amount-out", FLOOR,
                "--deposit", "--key-file", "external.json", "--out", "new.json", "--override-report", "old.json"]
        answer = {"verdict": "UNKNOWN", "phase": "broadcast", "reason": "uncertain", "deposit_hash": "hash",
                  "storage_deposit": "0"}
        with mock.patch("gate.oneclick.settle", return_value=answer) as run, contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(gate_main.main(args), 1)
        self.assertEqual(run.call_args.kwargs["override_reports"], ["old.json"])
        self.assertIn("storage_deposit: 0 yoctoNEAR", printed.getvalue())

    def test_default_preview_and_explicit_deposit(self):
        request = quote()["quoteRequest"]
        args = ["intents-settle", "--sender", SENDER, "--destination", request["destinationAsset"],
                "--amount", request["amount"], "--recipient", request["recipient"], "--min-amount-out", FLOOR]
        answer = {"verdict": "ASK", "phase": "ready", "reason": "Ready", "deposit_hash": None}
        for flags, live in (([], False), (["--deposit", "--key-file", "external.json", "--out", "report.json"], True)):
            with self.subTest(live=live), mock.patch("gate.oneclick.settle", return_value=answer) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(gate_main.main(args + flags), 1 if live else 0)
                self.assertIs(run.call_args.kwargs["live"], live)
                self.assertEqual(run.call_args.kwargs["min_amount_out"], FLOOR)

    def test_live_unknown_at_ready_phase_exits_nonzero(self):
        request = quote()["quoteRequest"]
        args = ["intents-settle", "--sender", SENDER, "--destination", request["destinationAsset"],
                "--amount", request["amount"], "--recipient", request["recipient"], "--min-amount-out", FLOOR,
                "--deposit", "--key-file", "external.json", "--out", "report.json"]
        answer = {"verdict": "UNKNOWN", "phase": "ready", "reason": "live signer refused", "deposit_hash": None}
        with mock.patch("gate.oneclick.settle", return_value=answer), contextlib.redirect_stdout(io.StringIO()):
            self.assertNotEqual(gate_main.main(args), 0)

    def test_read_only_live_flag_cannot_enable_settlement(self):
        request = quote()["quoteRequest"]
        args = ["intents-settle", "--sender", SENDER, "--destination", request["destinationAsset"],
                "--amount", request["amount"], "--recipient", request["recipient"], "--min-amount-out", FLOOR, "--live"]
        with mock.patch("gate.oneclick.settle") as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                gate_main.main(args)
        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_resume_routes_to_read_only_path_and_errors_refuse(self):
        answer = {"verdict": "UNKNOWN", "phase": "polling", "reason": "uncertain", "deposit_hash": "hash",
                  "registry_released": True}
        with mock.patch("gate.oneclick.resume", return_value=answer) as resume, mock.patch("gate.oneclick.settle") as settle:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gate_main.main(["intents-resume", "report.json", "--polls", "1"]), 1)
            resume.assert_called_once_with("report.json", polls=1)
            settle.assert_not_called()
        with mock.patch("gate.oneclick.resume", side_effect=ValueError("bad report")):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(gate_main.main(["intents-resume", "report.json"]), 2)
            self.assertIn("bad report", error.getvalue())

    def test_amount_floor_and_poll_bounds_stop_before_network(self):
        request = quote()["quoteRequest"]
        base = (SENDER, request["destinationAsset"], request["amount"], request["recipient"])
        for amount in ("0", "01", "-1", 1, "250000000000000000000001"):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    oneclick.settle(base[0], base[1], amount, base[3], min_amount_out=FLOOR)
        for floor in ("0", "01", "-1", None):
            with self.subTest(floor=floor):
                with self.assertRaises(ValueError):
                    oneclick.settle(*base, min_amount_out=floor)
        for polls in (0, 121, True):
            with self.subTest(polls=polls):
                with self.assertRaises(ValueError):
                    oneclick.settle(*base, min_amount_out=FLOOR, polls=polls)
        self.urlopen.assert_not_called()

    def test_resume_malformed_report_never_opens_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            for value in (None, {}, {"phase": "ready"}, {"deposit_hash": "not-a-transaction"}):
                with self.subTest(report=value):
                    path.write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        oneclick.resume(path, clock=lambda: CLOCK, sleep=lambda _: None)
        self.urlopen.assert_not_called()

    def test_live_requires_external_key_new_report_and_off_mode(self):
        request = quote()["quoteRequest"]
        args = (SENDER, request["destinationAsset"], request["amount"], request["recipient"])
        for kwargs in ({}, {"mode": "replay"}, {"key_file": ROOT / "missing.json"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    oneclick.settle(*args, min_amount_out=FLOOR, live=True, **kwargs)
        self.urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
