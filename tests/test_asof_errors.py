import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import incident_replay, rpc, rules
from gate.asof import AsOf
from intel import labels
from tests.test_incident_replay import Chain, HEIGHT, START_NS


# Every committed RPC response validates. The reduced demo and scan recordings
# (gate/demo.py:4) are listed with their sha256 in fixtures/reduced-recordings.json;
# replay alone accepts them without block_hash, amount, locked, storage_usage or logs.
_MALFORMED_RPC_FIXTURES = set()
# Pinned at block 222848858 with no block response recorded beside them, so the
# pinned block hash cannot be checked in replay and the read stays a FixtureMiss.
_PINNED_BLOCK_UNRECORDED = {
    ("issuers", "150e0b3a"), ("issuers", "1b95a48b"), ("issuers", "4852890d"), ("issuers", "5bda2aa4"),
    ("issuers", "66b0f57d"), ("issuers", "752e5a35"), ("issuers", "7b22ba97"), ("issuers", "7bc83cf6"),
    ("issuers", "9d4dd153"), ("issuers", "ba74bd58"), ("issuers", "e8ecde1b"),
}


def _reduced_fixtures():
    root = rpc.FIXTURES_DIR.parent
    for name in sorted(rpc.REDUCED_RECORDINGS):
        path = root / name
        yield path, json.loads(path.read_text(encoding="utf-8"))


def _query_error(name, info):
    if isinstance(info, dict):
        info = dict(info)
        if name == "NO_CONTRACT_CODE":
            info["contract_account_id"] = "old.near"
        if name == "CONTRACT_EXECUTION_ERROR":
            info.setdefault("vm_error", "execution failed")
    return {"name": "HANDLER_ERROR", "code": -32000, "message": "Server error",
            "cause": {"name": name, **({"info": info} if info is not None else {})}}


class ErrorContextTest(unittest.TestCase):
    def setUp(self):
        self.chain = Chain()
        self.client = AsOf(self.chain, HEIGHT)
        self.original = self.chain.call
        self.info = {"block_height": HEIGHT, "requested_account_id": "old.near", "block_hash": rpc.base58.encode(bytes([1]) * 32)}
        for target in ("urllib.request.urlopen", "gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            patcher = mock.patch(target, side_effect=AssertionError("network"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def judge(self):
        return rules.judge(
            {"kind": "near", "token_id": None, "receiver_id": "old.near", "amount": "1", "memo": None},
            {"allowlist": [], "caps": []}, labels.Store([], ""), self.client, incident_replay._NoNearBlocks(),
            now_ms=(START_NS + HEIGHT * 10**9) // 10**6, archival=self.client, injection_scanner=None)

    def response(self, error, embedded=False, context=None, block_error=None):
        def call(method, params):
            if method == "block" and block_error is not None:
                raise rpc.RpcError(block_error)
            if params.get("request_type") == "call_function":
                if embedded:
                    return {"error": error, **({"block_height": params["block_id"]}
                                               if context is None else context)}
                raise rpc.RpcError(error)
            result = self.original(method, params)
            if params.get("request_type") == "view_account":
                result["code_hash"] = rpc.base58.encode(bytes([3]) * 32)
            return result
        return mock.patch.object(self.chain, "call", side_effect=call)

    def test_missing_code_and_method_context_must_match_before_judging(self):
        for cause, marker in (("UNKNOWN_ACCOUNT", None), ("NO_CONTRACT_CODE", None),
                              ("CONTRACT_EXECUTION_ERROR", "MethodNotFound"),
                              ("CONTRACT_EXECUTION_ERROR", "CodeDoesNotExist")):
            for info in ({**self.info, "block_height": HEIGHT + 1},
                         {**self.info, "requested_account_id": "other.near"},
                         {**self.info, "block_hash": rpc.base58.encode(bytes([2]) * 32)},
                         {"requested_account_id": "old.near"}, {"block_height": HEIGHT}, {}, None, []):
                if isinstance(info, dict) and marker:
                    info = {**info, "vm_error": marker}
                error = _query_error(cause, info)
                for embedded in (False, True):
                    with self.subTest(cause=cause, marker=marker, info=info, embedded=embedded):
                        with self.response(error, embedded):
                            with self.assertRaises(rpc.MalformedResponse):
                                self.client.call_function("old.near", "contract_source_metadata", {})
                            judgement = self.judge()
                        code = next(c for c in judgement.checks if c["id"] == "code_without_metadata")
                        self.assertEqual(code["status"], "unknown")
                        self.assertEqual(judgement.verdict, "UNKNOWN")

    def test_matching_missing_code_and_method_context_preserves_current_judgement(self):
        for cause, marker in (("UNKNOWN_ACCOUNT", None), ("NO_CONTRACT_CODE", None),
                              ("CONTRACT_EXECUTION_ERROR", "MethodNotFound"),
                              ("CONTRACT_EXECUTION_ERROR", "CodeDoesNotExist")):
            for include_hash in (False, True):
                info = dict(self.info)
                if not include_hash:
                    del info["block_hash"]
                if marker:
                    info["vm_error"] = marker
                error = _query_error(cause, info)
                for embedded in (False, True):
                    with self.subTest(cause=cause, marker=marker, include_hash=include_hash, embedded=embedded):
                        with self.response(error, embedded):
                            with self.assertRaises(rpc.RpcError if include_hash else rpc.MalformedResponse):
                                self.client.call_function("old.near", "contract_source_metadata", {})
                            judgement = self.judge()
                        self.assertEqual(judgement.verdict, "ALLOW" if include_hash else "UNKNOWN")

    def test_embedded_error_outer_context_must_also_match_before_judging(self):
        error = _query_error("NO_CONTRACT_CODE", self.info)
        for context in ({"block_height": HEIGHT + 1}, {"block_height": True}, {},
                        {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([2]) * 32)},
                        {"block_height": HEIGHT, "block_hash": None}):
            with self.subTest(context=context), self.response(error, True, context):
                judgement = self.judge()
                self.assertEqual(judgement.verdict, "UNKNOWN")
                code = next(c for c in judgement.checks if c["id"] == "code_without_metadata")
                self.assertEqual(code["status"], "unknown")
                with self.assertRaises(rpc.MalformedResponse):
                    self.client.call_function("old.near", "contract_source_metadata", {})
        with self.response(error, True, {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32)}):
            self.assertEqual(self.judge().verdict, "ALLOW")

    def test_failed_block_hash_lookup_cannot_be_interpreted_as_missing_code(self):
        for failure in (_query_error("NO_CONTRACT_CODE", None),
                        _query_error("UNKNOWN_ACCOUNT", None), "MethodNotFound", "CodeDoesNotExist"):
            for embedded, outer_hash in ((False, False), (True, False), (True, True)):
                info = dict(self.info)
                context = {"block_height": HEIGHT}
                if outer_hash:
                    del info["block_hash"]
                    context["block_hash"] = rpc.base58.encode(bytes([1]) * 32)
                error = _query_error("NO_CONTRACT_CODE", info)
                with self.subTest(failure=failure, embedded=embedded, outer_hash=outer_hash):
                    with self.response(error, embedded, context, block_error=failure):
                        judgement = self.judge()
                        self.assertEqual(judgement.verdict, "UNKNOWN")
                        code = next(c for c in judgement.checks if c["id"] == "code_without_metadata")
                        self.assertEqual(code["status"], "unknown")
                        with self.assertRaises(rpc.MalformedResponse):
                            self.client.call_function("old.near", "contract_source_metadata", {})

    def test_legacy_missing_method_errors_without_context_read_unknown(self):
        for marker in ("MethodNotFound", "CodeDoesNotExist"):
            for embedded in (False, True):
                with self.subTest(marker=marker, embedded=embedded), self.response(marker, embedded):
                    with self.assertRaises(rpc.MalformedResponse):
                        self.client.call_function("old.near", "contract_source_metadata", {})
                    self.assertEqual(self.judge().verdict, "UNKNOWN")

    def test_query_result_with_embedded_error_reads_unknown(self):
        for request_type, read, check_id in (
                ("view_account", self.client.view_account, "receiver_exists"),
                ("view_access_key_list", self.client.view_access_key_list, "account_age"),
                ("call_function", lambda account: self.client.call_function(
                    account, "contract_source_metadata", {}), "code_without_metadata")):
            for info in (self.info, {**self.info, "block_height": HEIGHT + 1},
                         {**self.info, "requested_account_id": "other.near"}):
                error = _query_error("UNKNOWN_ACCOUNT", info)
                def call(method, params):
                    result = self.original(method, params)
                    if params.get("request_type") == "view_account":
                        result["code_hash"] = rpc.base58.encode(bytes([3]) * 32)
                    if params.get("request_type") == request_type:
                        result["error"] = error
                    return result
                with self.subTest(request_type=request_type, info=info):
                    with mock.patch.object(self.chain, "call", side_effect=call):
                        with self.assertRaises(rpc.MalformedResponse):
                            read("old.near")
                        judgement = self.judge()
                    self.assertEqual(judgement.verdict, "UNKNOWN")
                    check = next(c for c in judgement.checks if c["id"] == check_id)
                    self.assertEqual(check["status"], "unknown")

    def test_every_query_embedded_error_requires_matching_context(self):
        for request_type, read, check_id in (
                ("view_account", self.client.view_account, "receiver_exists"),
                ("view_access_key_list", self.client.view_access_key_list, "account_age"),
                ("call_function", lambda account: self.client.call_function(
                    account, "contract_source_metadata", {}), "code_without_metadata")):
            contexts = [(self.info, {"block_height": HEIGHT}),
                        (self.info, {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32)})]
            contexts += [(info, {"block_height": HEIGHT}) for info in (
                {**self.info, "block_height": HEIGHT + 1},
                {**self.info, "block_height": True},
                {**self.info, "requested_account_id": "other.near"},
                {**self.info, "block_hash": rpc.base58.encode(bytes([2]) * 32)}, {}, None)]
            contexts += [(self.info, context) for context in (
                {"block_height": HEIGHT + 1}, {"block_height": True}, {},
                {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([2]) * 32)})]
            for index, (info, context) in enumerate(contexts):
                error = _query_error("UNKNOWN_ACCOUNT", info)
                def call(method, params):
                    result = self.original(method, params)
                    if params.get("request_type") == request_type:
                        return {"error": error, **context}
                    if params.get("request_type") == "view_account":
                        result["code_hash"] = rpc.base58.encode(bytes([3]) * 32)
                    return result
                with self.subTest(request_type=request_type, info=info, context=context):
                    with mock.patch.object(self.chain, "call", side_effect=call):
                        expected = rpc.RpcError if index < 2 else rpc.MalformedResponse
                        with self.assertRaises(expected):
                            read("old.near")
                        judgement = self.judge()
                    check = next(c for c in judgement.checks if c["id"] == check_id)
                    status = ("signal" if request_type == "view_account" else
                              "pass" if request_type == "call_function" else "unknown")
                    self.assertEqual(check["status"], status if index < 2 else "unknown")
                    verdict = ("DENY" if request_type == "view_account" else
                               "ALLOW" if request_type == "call_function" else "UNKNOWN")
                    self.assertEqual(judgement.verdict, verdict if index < 2 else "UNKNOWN")

    def test_query_envelope_with_result_and_error_reads_unknown(self):
        chain = rpc.Client("https://archival.example", "replay")
        client = AsOf(chain, HEIGHT)
        for request_type, read, check_id in (
                ("view_account", client.view_account, "receiver_exists"),
                ("view_access_key_list", client.view_access_key_list, "account_age"),
                ("call_function", lambda account: client.call_function(
                    account, "contract_source_metadata", {}), "code_without_metadata")):
            for include_result in (False, True):
                error = _query_error("UNKNOWN_ACCOUNT", self.info)
                def exchange(url, method, params, body):
                    result = self.original(method, params)
                    chain.last_source = dict(self.chain.last_source)
                    if params.get("request_type") == "view_account":
                        result["code_hash"] = rpc.base58.encode(bytes([3]) * 32)
                    if params.get("request_type") == request_type:
                        return {"jsonrpc": "2.0", "id": "gate", "error": error, **({"result": result} if include_result else {})}
                    return {"jsonrpc": "2.0", "id": "gate", "result": result}
                with self.subTest(request_type=request_type, include_result=include_result):
                    with mock.patch.object(chain, "_exchange", side_effect=exchange), \
                            mock.patch.object(self, "client", client):
                        expected = rpc.MalformedResponse if include_result else rpc.RpcError
                        with self.assertRaises(expected):
                            read("old.near")
                        judgement = self.judge()
                    check = next(c for c in judgement.checks if c["id"] == check_id)
                    status = ("signal" if request_type == "view_account" else
                              "pass" if request_type == "call_function" else "unknown")
                    self.assertEqual(check["status"], "unknown" if include_result else status)
                    verdict = ("DENY" if request_type == "view_account" else
                               "ALLOW" if request_type == "call_function" else "UNKNOWN")
                    self.assertEqual(judgement.verdict, "UNKNOWN" if include_result else verdict)

    def test_account_and_access_key_absence_require_context(self):
        for request_type, read in (("view_account", self.client.view_account),
                                   ("view_access_key_list", self.client.view_access_key_list)):
            for info in (self.info, {**self.info, "block_height": HEIGHT + 1},
                         {**self.info, "requested_account_id": "other.near"},
                         {**self.info, "block_hash": rpc.base58.encode(bytes([2]) * 32)}, {}):
                error = rpc.RpcError(_query_error("UNKNOWN_ACCOUNT", info))
                def call(method, params):
                    result = self.original(method, params)
                    if params.get("request_type") == request_type:
                        raise error
                    return result
                with self.subTest(request_type=request_type, info=info):
                    with mock.patch.object(self.chain, "call", side_effect=call):
                        expected = rpc.RpcError if info == self.info else rpc.MalformedResponse
                        with self.assertRaises(expected):
                            read("old.near")
                        judgement = self.judge()
                    check_id = "receiver_exists" if request_type == "view_account" else "account_age"
                    check = next(c for c in judgement.checks if c["id"] == check_id)
                    self.assertEqual(check["status"], "signal" if info == self.info and
                                     request_type == "view_account" else "unknown")


    def test_block_header_with_nested_error_never_passes_judge_or_resolution(self):
        error = _query_error("UNKNOWN_ACCOUNT", {
            **self.info, "block_height": HEIGHT + 1, "requested_account_id": "other.near"})
        for nested in (False, True):
            def call(method, params):
                result = self.original(method, params)
                if params.get("request_type") == "view_access_key_list":
                    result["keys"][0]["access_key"]["nonce"] = rules.NONCE_PER_HEIGHT
                if method == "block":
                    result["header" if nested else "error"] = (
                        {**result["header"], "error": error} if nested else error)
                return result
            with self.subTest(nested=nested), mock.patch.object(self.chain, "call", side_effect=call):
                with self.assertRaises(rpc.MalformedResponse):
                    self.client.final_block()
                judgement = self.judge()
                self.assertEqual(judgement.verdict, "UNKNOWN")
                self.assertEqual(next(c for c in judgement.checks if c["id"] == "account_age")["status"],
                                 "unknown")
            for fail_head in (False, True):
                client = rpc.Client("https://archival.example", "replay")
                def exchange(url, method, params, body):
                    result = (call(method, params) if fail_head or "block_id" in params
                              else self.original(method, params))
                    return {"jsonrpc": "2.0", "id": "gate", "result": result}
                with self.subTest(fail_head=fail_head), mock.patch.object(client, "_exchange", side_effect=exchange):
                    with self.assertRaises(rpc.MalformedResponse):
                        incident_replay._resolve(client, START_NS + HEIGHT * 10**9)

    def test_matching_successful_block_and_decoded_metadata_controls(self):
        self.assertEqual(self.client.final_block()["header"]["height"], HEIGHT)
        self.assertEqual(self.client.call_function("old.near", "contract_source_metadata", {}),
                         {"version": "1", "link": None, "standards": []})
        self.assertEqual(self.judge().verdict, "ALLOW")
        client = rpc.Client("https://archival.example", "replay")
        def exchange(url, method, params, body):
            return {"jsonrpc": "2.0", "id": "gate", "result": self.original(method, params)}
        with mock.patch.object(client, "_exchange", side_effect=exchange):
            self.assertEqual(incident_replay._resolve(client, START_NS + HEIGHT * 10**9)["height"], HEIGHT)

    def test_decoded_error_envelopes_require_matching_context_before_judging(self):
        for method_name in ("contract_source_metadata", "another_view"):
            for info in (self.info, {**self.info, "block_height": HEIGHT + 1},
                         {**self.info, "requested_account_id": "other.near"},
                         {**self.info, "block_hash": rpc.base58.encode(bytes([2]) * 32)}):
                for extra in ({}, {"version": "1"}):
                    error = _query_error("NO_CONTRACT_CODE", info)
                    def call(method, params):
                        result = self.original(method, params)
                        if params.get("request_type") == "view_account":
                            result["code_hash"] = rpc.base58.encode(bytes([3]) * 32)
                        if params.get("request_type") == "call_function":
                            result["result"] = list(json.dumps({"error": error, **extra}).encode("utf-8"))
                        return result
                    with self.subTest(method=method_name, info=info, extra=extra):
                        with mock.patch.object(self.chain, "call", side_effect=call):
                            expected = rpc.RpcError if info == self.info and not extra else rpc.MalformedResponse
                            with self.assertRaises(expected):
                                self.client.call_function("old.near", method_name, {})
                            judgement = self.judge()
                        check = next(c for c in judgement.checks if c["id"] == "code_without_metadata")
                        self.assertEqual(check["status"], "pass" if expected is rpc.RpcError else "unknown")
                        self.assertEqual(judgement.verdict, "ALLOW" if expected is rpc.RpcError else "UNKNOWN")

    def test_nested_block_error_is_not_lost_when_recording_trims_header(self):
        response = {"jsonrpc": "2.0", "id": "gate", "result": {
            "header": {"height": HEIGHT, "timestamp": START_NS, "hash": rpc.base58.encode(bytes([1]) * 32)},
            "error": _query_error("UNKNOWN_ACCOUNT", self.info)}}
        with tempfile.TemporaryDirectory() as directory:
            client = rpc.Client("https://archival.example", "record", fixtures_dir=directory)
            with mock.patch.object(rpc, "_fetch", return_value=response):
                with self.assertRaises(rpc.MalformedResponse):
                    client.block_at(HEIGHT)
            client.mode = "replay"
            with self.assertRaises(rpc.MalformedResponse):
                client.block_at(HEIGHT)

    def test_other_rpc_methods_cannot_return_data_with_nested_error(self):
        client = rpc.Client("https://archival.example", "replay")
        with mock.patch.object(client, "_exchange", return_value={"jsonrpc": "2.0", "id": "gate", "result": {
                "status": {"error": "unavailable"}, "height": HEIGHT}}):
            with self.assertRaises(rpc.MalformedResponse):
                client.call("status", [])

    def test_decoded_error_bytes_are_gated_by_the_rpc_client(self):
        client = rpc.Client("https://archival.example", "replay")
        for info in (self.info, {**self.info, "block_height": HEIGHT + 1}):
            error = _query_error("NO_CONTRACT_CODE", info)
            def exchange(url, method, params, body):
                if method == "block":
                    return {"jsonrpc": "2.0", "id": "gate", "result": self.original(method, params)}
                return {"jsonrpc": "2.0", "id": "gate", "result": {"result": list(json.dumps({"error": error}).encode("utf-8")),
                                   "block_height": HEIGHT}}
            with self.subTest(info=info), mock.patch.object(client, "_exchange", side_effect=exchange):
                with self.assertRaises(rpc.RpcError if info == self.info else rpc.MalformedResponse):
                    client.call_function("old.near", "contract_source_metadata", {}, block_id=HEIGHT)

    def test_errors_in_envelope_context_fields_cannot_hide_beside_results(self):
        client = rpc.Client("https://archival.example", "replay")
        for field in ("id", "jsonrpc", "block_height", "block_hash", "requested_account_id"):
            for result in ({}, {"header": {"height": HEIGHT, "timestamp": START_NS, "hash": rpc.base58.encode(bytes([1]) * 32)}}):
                response = {"jsonrpc": "2.0", "id": "gate", "result": result, field: {"error": "unavailable"}}
                with self.subTest(field=field, result=result):
                    with mock.patch.object(client, "_exchange", return_value=response):
                        with self.assertRaises(rpc.MalformedResponse):
                            client.call("block", {"block_id": HEIGHT})

    def test_unpinned_pure_error_without_account_context_remains_unavailable(self):
        client = rpc.Client("https://archival.example", "replay")
        error = {"name": "INTERNAL_ERROR", "code": -32000, "message": "Server error",
                 "cause": {"name": "INTERNAL_ERROR", "info": {"error_message": "unavailable"}}}
        with mock.patch.object(client, "_exchange", return_value={"jsonrpc": "2.0", "id": "gate", "error": error}):
            with self.assertRaises(rpc.RpcError):
                client.view_account("old.near")

    def test_error_hash_lookup_must_return_the_requested_block_height(self):
        client = rpc.Client("https://archival.example", "replay")
        error = _query_error("NO_CONTRACT_CODE", self.info)
        def exchange(url, method, params, body):
            if method == "block":
                return {"jsonrpc": "2.0", "id": "gate", "result": {"header": {"height": HEIGHT + 1, "timestamp": START_NS, "hash": rpc.base58.encode(bytes([1]) * 32)}}}
            return {"jsonrpc": "2.0", "id": "gate", "error": error}
        with mock.patch.object(client, "_exchange", side_effect=exchange):
            with self.assertRaises(rpc.MalformedResponse):
                client.call_function("old.near", "contract_source_metadata", {}, block_id=HEIGHT)

    def test_decoded_error_requires_matching_outer_block_context(self):
        error = _query_error("NO_CONTRACT_CODE", self.info)
        for context in ({}, {"block_height": HEIGHT + 1}, {"block_height": True},
                        {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([2]) * 32)}):
            def call(method, params):
                result = self.original(method, params)
                if params.get("request_type") == "view_account":
                    result["code_hash"] = rpc.base58.encode(bytes([3]) * 32)
                if params.get("request_type") == "call_function":
                    return {"result": list(json.dumps({"error": error}).encode("utf-8")), **context}
                return result
            with self.subTest(context=context), mock.patch.object(self.chain, "call", side_effect=call):
                with self.assertRaises(rpc.MalformedResponse):
                    self.client.call_function("old.near", "contract_source_metadata", {})
                judgement = self.judge()
                self.assertEqual(judgement.verdict, "UNKNOWN")
                self.assertEqual(next(c for c in judgement.checks if c["id"] == "code_without_metadata")["status"],
                                 "unknown")

    def test_pure_error_retains_outer_block_height_after_validation(self):
        client = rpc.Client("https://archival.example", "replay")
        response = {"jsonrpc": "2.0", "id": "gate", "result": {"error": "contract execution failed", "block_height": HEIGHT}}
        with mock.patch.object(client, "_exchange", return_value=response):
            with self.assertRaises(rpc.RpcError):
                client.call_function("old.near", "ft_metadata", {})
        self.assertEqual(client.last_block_height, HEIGHT)

    def test_pinned_block_error_context_is_checked_before_interpretation(self):
        client = rpc.Client("https://archival.example", "replay")
        for info in ({"block_height": HEIGHT}, {"block_height": HEIGHT + 1},
                     {"block_height": True}, {}):
            error = _query_error("UNKNOWN_BLOCK", info)
            with self.subTest(info=info), mock.patch.object(client, "_exchange", return_value={"jsonrpc": "2.0", "id": "gate", "error": error}):
                expected = rpc.RpcError if info in ({"block_height": HEIGHT}, {}) else rpc.MalformedResponse
                with self.assertRaises(expected):
                    client.block_at(HEIGHT)

    def test_boolean_error_bytes_never_pass_the_gate_or_consumer(self):
        error = _query_error("NO_CONTRACT_CODE", self.info)
        value = [byte if byte else False for byte in json.dumps({"error": error}).encode("utf-16le")]
        client = rpc.Client("https://archival.example", "replay")
        for invalid in (value, [True], [-1], [256], [1.0], ["1"], None):
            def call(method, params):
                result = self.original(method, params)
                if params.get("request_type") == "view_account":
                    result["code_hash"] = rpc.base58.encode(bytes([3]) * 32)
                if params.get("request_type") == "call_function":
                    result["result"] = invalid
                return result
            with self.subTest(value=invalid), mock.patch.object(self.chain, "call", side_effect=call):
                with self.assertRaises(rpc.MalformedResponse):
                    self.client.call_function("old.near", "contract_source_metadata", {})
                self.assertEqual(self.judge().verdict, "UNKNOWN")
                response = {"jsonrpc": "2.0", "id": "gate", "result": {"result": invalid, "block_height": HEIGHT}}
                with mock.patch.object(client, "_exchange", return_value=response):
                    with self.assertRaises(rpc.MalformedResponse):
                        client.call_function("old.near", "contract_source_metadata", {}, block_id=HEIGHT)
                with mock.patch.object(client, "call", return_value=response["result"]):
                    with self.assertRaises(rpc.MalformedResponse):
                        client.call_function("old.near", "contract_source_metadata", {})

    def test_successful_query_context_must_match_the_pinned_header(self):
        for request_type, read, check_id in (
                ("view_account", self.client.view_account, "receiver_exists"),
                ("view_access_key_list", self.client.view_access_key_list, "account_age"),
                ("call_function", lambda account: self.client.call_function(
                    account, "contract_source_metadata", {}), "code_without_metadata")):
            for context in ({"block_hash": rpc.base58.encode(bytes([2]) * 32)}, {"block_hash": None},
                            {"block_hash": rpc.base58.encode(bytes([1]) * 32)}, {"block_height": HEIGHT + 1},
                            {"block_height": True}, {"block_height": HEIGHT}):
                for nested in (False, True):
                    def call(method, params):
                        result = self.original(method, params)
                        if params.get("request_type") == "view_account":
                            result["code_hash"] = rpc.base58.encode(bytes([3]) * 32)
                        if params.get("request_type") == request_type:
                            result.update({"data": context} if nested else context)
                        return result
                    matching = context in ({"block_hash": rpc.base58.encode(bytes([1]) * 32)}, {"block_height": HEIGHT})
                    with self.subTest(kind=request_type, context=context, nested=nested):
                        with mock.patch.object(self.chain, "call", side_effect=call):
                            if matching:
                                read("old.near")
                            else:
                                with self.assertRaises(rpc.MalformedResponse):
                                    read("old.near")
                            judgement = self.judge()
                        self.assertEqual(judgement.verdict, "ALLOW" if matching else "UNKNOWN")
                        self.assertEqual(next(c for c in judgement.checks if c["id"] == check_id)["status"],
                                         "pass" if matching else "unknown")

    def test_pure_error_context_fields_are_checked_at_every_depth(self):
        chain = rpc.Client("https://archival.example", "replay")
        client = AsOf(chain, HEIGHT)
        for field, bad, good in (("requested_account_id", "other.near", "old.near"),
                                  ("account_id", "other.near", "old.near"),
                                  ("block_height", HEIGHT + 1, HEIGHT),
                                  ("block_hash", rpc.base58.encode(bytes([2]) * 32), rpc.base58.encode(bytes([1]) * 32))):
            for location in ("outer", "error", "data"):
                for value in (bad, good):
                    error = _query_error("NO_CONTRACT_CODE", dict(self.info))
                    response = {"jsonrpc": "2.0", "id": "gate", "error": error}
                    if location == "outer":
                        response[field] = value
                    elif location == "error":
                        error[field] = value
                    else:
                        error["data"] = {"nested": [{field: value}]}
                    def exchange(url, method, params, body):
                        result = self.original(method, params)
                        chain.last_source = dict(self.chain.last_source)
                        if params.get("request_type") == "call_function":
                            return response
                        if params.get("request_type") == "view_account":
                            result["code_hash"] = rpc.base58.encode(bytes([3]) * 32)
                        return {"jsonrpc": "2.0", "id": "gate", "result": result}
                    with self.subTest(field=field, location=location, value=value):
                        with mock.patch.object(chain, "_exchange", side_effect=exchange), \
                                mock.patch.object(self, "client", client):
                            with self.assertRaises(rpc.MalformedResponse if value == bad else rpc.RpcError):
                                client.call_function("old.near", "contract_source_metadata", {})
                            self.assertEqual(self.judge().verdict, "UNKNOWN" if value == bad else "ALLOW")

    def test_nested_error_payloads_never_become_missing_code(self):
        for location in ("error", "data", "cause"):
            for payload in ({"error": "unavailable", "result": {"version": "1"}},
                            {"error": "unavailable"}, {"result": {"version": "1"}}):
                error = _query_error("NO_CONTRACT_CODE", dict(self.info))
                if location == "error":
                    error.update(payload)
                elif location == "cause":
                    error["cause"].update(payload)
                else:
                    error["data"] = payload
                for embedded in (False, True):
                    with self.subTest(location=location, payload=payload, embedded=embedded):
                        with self.response(error, embedded):
                            with self.assertRaises(rpc.MalformedResponse):
                                self.client.call_function("old.near", "contract_source_metadata", {})
                            judgement = self.judge()
                        self.assertEqual(judgement.verdict, "UNKNOWN")
                        self.assertEqual(next(c for c in judgement.checks
                                              if c["id"] == "code_without_metadata")["status"], "unknown")
        with self.response(_query_error("NO_CONTRACT_CODE", dict(self.info))):
            self.assertEqual(self.judge().verdict, "ALLOW")

    def test_malformed_account_and_key_payloads_read_unknown(self):
        for kind, payload, check_id in (
                ("view_account", {"amount": {}}, "receiver_exists"),
                ("view_access_key_list", {"permission": {"FunctionCall": None}}, "account_age"),
                ("view_access_key_list", {}, "account_age")):
            def call(method, params):
                result = self.original(method, params)
                if params.get("request_type") == kind:
                    if kind == "view_account":
                        result.update(payload)
                    else:
                        access = result["keys"][0]["access_key"]
                        access.pop("permission")
                        access.update(payload)
                return result
            with self.subTest(kind=kind, payload=payload), mock.patch.object(self.chain, "call", side_effect=call):
                with self.assertRaises(rpc.MalformedResponse):
                    getattr(self.client, kind)("old.near")
                judgement = self.judge()
                self.assertEqual(judgement.verdict, "UNKNOWN")
                self.assertEqual(next(c for c in judgement.checks if c["id"] == check_id)["status"], "unknown")

    def test_account_envelope_must_match_request_before_judging(self):
        chain = rpc.Client("https://archival.example", "replay")
        client = AsOf(chain, HEIGHT)
        for envelope in ({"id": "different-request"}, {"id": {}}, {"jsonrpc": []},
                         {"id": None}, {"id": True}, {"id": 1}, {"jsonrpc": "1.0"}):
            def exchange(url, method, params, body):
                response = {"jsonrpc": body["jsonrpc"], "id": body["id"],
                            "result": self.original(method, params)}
                chain.last_source = dict(self.chain.last_source)
                if params.get("request_type") == "view_account":
                    response.update(envelope)
                return response
            with self.subTest(envelope=envelope):
                with mock.patch.object(chain, "_exchange", side_effect=exchange), \
                        mock.patch.object(self, "client", client):
                    judgement = self.judge()
                    self.assertEqual(judgement.verdict, "UNKNOWN")
                    self.assertEqual(next(c for c in judgement.checks
                                          if c["id"] == "receiver_exists")["status"], "unknown")
                    with self.assertRaises(rpc.MalformedResponse):
                        client.view_account("old.near")

    def test_matching_account_envelope_preserves_allow(self):
        chain = rpc.Client("https://archival.example", "replay")
        def exchange(url, method, params, body):
            chain.last_source = {"provider": "archival.example", "request_sha256": method}
            return {"jsonrpc": body["jsonrpc"], "id": body["id"],
                    "result": self.original(method, params)}
        with mock.patch.object(chain, "_exchange", side_effect=exchange), \
                mock.patch.object(self, "client", AsOf(chain, HEIGHT)):
            judgement = self.judge()
        self.assertEqual(judgement.verdict, "ALLOW")
        self.assertEqual(next(c for c in judgement.checks
                              if c["id"] == "receiver_exists")["status"], "pass")

    def test_hash_validation_keeps_the_query_evidence_source(self):
        chain = rpc.Client("https://archival.example", "replay")
        for client in (chain, AsOf(chain, HEIGHT)):
            for kind in ("view_account", "view_access_key_list", "call_function"):
                params = {"request_type": kind, "account_id": "old.near", "block_id": HEIGHT}
                def exchange(url, method, params, body):
                    result = self.original(method, params)
                    chain.last_source = {"provider": "archival.example", "request_sha256": method}
                    if method == "query":
                        result["block_hash"] = rpc.base58.encode(bytes([1]) * 32)
                    return {"jsonrpc": "2.0", "id": "gate", "result": result}
                with self.subTest(client=type(client).__name__, kind=kind):
                    with mock.patch.object(chain, "_exchange", side_effect=exchange):
                        client.call("query", params)
                    self.assertEqual(client.last_source["request_sha256"], "query")
                    self.assertEqual(client.last_block_height, HEIGHT)

    def test_successful_query_hash_lookup_rejects_a_nested_block_error(self):
        for kind in ("view_account", "view_access_key_list", "call_function"):
            def call(method, params):
                result = self.original(method, params)
                if method == "block":
                    result["header"]["error"] = "unavailable"
                else:
                    result["block_hash"] = rpc.base58.encode(bytes([1]) * 32)
                return result
            with self.subTest(kind=kind), mock.patch.object(self.chain, "call", side_effect=call):
                with self.assertRaises(rpc.MalformedResponse):
                    self.client.call("query", {"request_type": kind, "account_id": "old.near",
                                               "block_id": HEIGHT})

    def assert_schema_unknown(self, method, params, response, check_id):
        chain = rpc.Client("https://archival.example", "replay")
        pinned = AsOf(chain, HEIGHT)
        def exchange(url, called_method, called_params, body):
            chain.last_source = {"provider": "archival.example", "request_sha256": called_method}
            matches = called_method == method
            if method == "query":
                matches = matches and called_params.get("request_type") == params["request_type"]
                if params["request_type"] == "call_function":
                    matches = matches and called_params.get("method_name") == params["method_name"]
            if matches:
                return response
            result = self.original(called_method, called_params)
            if called_params.get("request_type") == "view_account":
                result["code_hash"] = rpc.base58.encode(bytes([3]) * 32)
            if called_params.get("request_type") == "view_access_key_list":
                result["keys"][0]["access_key"]["nonce"] = rules.NONCE_PER_HEIGHT
            if called_params.get("method_name") == "ft_metadata":
                result = {"block_height": called_params["block_id"], "block_hash": rpc.base58.encode(bytes([1]) * 32), "logs": [],
                          "result": list(json.dumps({"name": "Token", "symbol": "T", "decimals": 6}).encode())}
            if called_params.get("method_name") == "storage_balance_of":
                result = {"block_height": called_params["block_id"], "block_hash": rpc.base58.encode(bytes([1]) * 32), "logs": [],
                          "result": list(json.dumps({"total": "1", "available": "0"}).encode())}
            return {"jsonrpc": "2.0", "id": "gate", "result": result}
        with mock.patch.object(chain, "_exchange", side_effect=exchange):
            with self.assertRaises(rpc.MalformedResponse):
                chain.call(method, params)
            if params.get("request_type") not in ("view_access_key", "view_code"):
                with self.assertRaises(rpc.MalformedResponse):
                    pinned.call(method, params)
                token = params.get("method_name") in ("ft_metadata", "storage_balance_of")
                judgement = rules.judge(
                    {"kind": "ft" if token else "near", "token_id": "token.near" if token else None,
                     "receiver_id": "old.near", "amount": "1", "memo": None},
                    {"allowlist": [], "caps": [{"asset": {"Ft": "token.near"}}] if token else []},
                    labels.Store([], ""), pinned, incident_replay._NoNearBlocks(),
                    now_ms=(START_NS + HEIGHT * 10**9) // 10**6, archival=pinned, injection_scanner=None,
                    reports_account="reports.near" if params.get("method_name") == "subject_summary" else None)
                self.assertEqual(judgement.verdict, "UNKNOWN")
                self.assertEqual(next(c for c in judgement.checks if c["id"] == check_id)["status"], "unknown")

    def test_reported_error_and_timestamp_payloads_read_unknown(self):
        params = {"request_type": "call_function", "method_name": "contract_source_metadata",
                  "account_id": "old.near", "block_id": HEIGHT}
        for fields in ({"code": [], "message": {}}, {"code": True, "message": 7}):
            error = {**_query_error("NO_CONTRACT_CODE", dict(self.info)), **fields}
            with self.subTest(fields=fields):
                self.assert_schema_unknown("query", params, {"jsonrpc": "2.0", "id": "gate", "error": error},
                                           "code_without_metadata")
        for timestamp in ("1776297600000000000", [], True):
            response = {"jsonrpc": "2.0", "id": "gate", "result": {"header": {
                "height": 2, "hash": rpc.base58.encode(bytes([1]) * 32), "timestamp": START_NS + 2 * 10**9,
                "timestamp_nanosec": timestamp}}}
            with self.subTest(timestamp=timestamp):
                self.assert_schema_unknown("block", {"block_id": 2}, response, "account_age")

    def test_schema_fields_reject_wrong_types_and_missing_required_fields(self):
        context = {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32)}
        function = {"allowance": "1", "receiver_id": "old.near", "method_names": ["pay"]}
        access = {"nonce": rules.NONCE_PER_HEIGHT, "permission": {"FunctionCall": function}}
        source = {"version": "1", "link": "source", "standards": [{"standard": "nep330", "version": "1"}]}
        error = {**self.info, "account_id": "old.near", "name": "HANDLER_ERROR", "code": -32000,
                 "message": "Server error", "data": "details", "cause": {"name": "CONTRACT_EXECUTION_ERROR",
                 "info": {**self.info, "account_id": "old.near", "vm_error": "MethodNotFound"}}}
        rows = [
            ("block", None, {"header": {"height": 2, "timestamp": START_NS + 2 * 10**9,
                                      "timestamp_nanosec": str(START_NS + 2 * 10**9), "hash": rpc.base58.encode(bytes([1]) * 32)}},
             ("header", "header.height", "header.timestamp", "header.hash"), ("header.timestamp_nanosec",), "account_age"),
            ("view_account", None, {**context, "amount": "1", "locked": "0", "storage_usage": 1,
                                   "storage_paid_at": 0, "code_hash": rpc.base58.encode(bytes([3]) * 32)},
             ("block_height", "block_hash", "amount", "locked", "storage_usage", "code_hash"),
             ("storage_paid_at",), "receiver_exists"),
            ("view_access_key_list", None, {**context, "keys": [{"public_key": "ed25519:" + "1" * 32, "access_key": access}]},
             ("block_height", "block_hash", "keys", "keys.0.public_key", "keys.0.access_key",
              "keys.0.access_key.nonce", "keys.0.access_key.permission",
              "keys.0.access_key.permission.FunctionCall", "keys.0.access_key.permission.FunctionCall.allowance",
              "keys.0.access_key.permission.FunctionCall.receiver_id", "keys.0.access_key.permission.FunctionCall.method_names"),
             ("keys.0.access_key.permission.FunctionCall.method_names.0",), "account_age"),
            ("view_access_key", None, {**context, **access},
             ("block_height", "block_hash", "nonce", "permission"), (), None),
            ("view_code", None, {**context, "hash": rpc.base58.encode(hashlib.sha256(bytes(1)).digest()), "code_base64": "AA=="},
             ("block_height", "block_hash", "hash", "code_base64"), (), None),
            ("call_function", None, {**context, "logs": ["log"], "result": list(json.dumps(source).encode())},
             ("block_height", "block_hash", "logs", "result"), ("logs.0", "result.0"), "code_without_metadata"),
            ("call_function", "contract_source_metadata", source,
             ("version", "link", "standards", "standards.0.standard", "standards.0.version"), (), "code_without_metadata"),
            ("call_function", "ft_metadata", {"name": "Token", "symbol": "T", "decimals": 6,
                                              "spec": "ft-1.0.0", "icon": "icon", "reference": "ref", "reference_hash": "A" * 43 + "="},
             ("name", "symbol", "decimals"), ("spec", "icon", "reference", "reference_hash"), "ft_metadata_sane"),
            ("call_function", "storage_balance_of", {"total": "1", "available": "0"},
             ("total", "available"), (), "storage_registered"),
            ("call_function", "subject_summary", {"accepted": 0, "pending": 0, "contested": 0,
                                                  "latest_accepted_id": None, "latest_deny_id": None, "deny": False},
             ("accepted", "pending", "contested", "latest_accepted_id", "latest_deny_id", "deny"), (), "community_report"),
            ("error", None, error, ("name", "code", "message", "cause", "cause.name", "cause.info",
                                     "cause.info.block_height", "cause.info.block_hash",
                                     "cause.info.vm_error"),
             ("data", "block_height", "block_hash", "requested_account_id", "account_id",
              "cause.info.account_id", "cause.info.requested_account_id"), "code_without_metadata"),
        ]
        for kind, decoded, value, required, optional, check_id in rows:
            fields = [(field, missing) for field in required for missing in (False, True)]
            fields += [(field, False) for field in optional]
            for field, missing in fields:
                changed = json.loads(json.dumps(value))
                target = changed
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[int(part)] if isinstance(target, list) else target[part]
                key = int(parts[-1]) if isinstance(target, list) else parts[-1]
                if missing:
                    del target[key]
                else:
                    target[key] = True if type(target[key]) is int else 7
                method = "block" if kind == "block" else "query"
                params = {"block_id": 2} if method == "block" else {
                    "block_id": HEIGHT, "account_id": "old.near", "request_type": kind if kind != "error" else "call_function"}
                if params.get("request_type") == "call_function":
                    params["method_name"] = decoded or "contract_source_metadata"
                if decoded:
                    changed = {**context, "logs": [], "result": list(json.dumps(changed).encode())}
                response = {"jsonrpc": "2.0", "id": "gate", "error" if kind == "error" else "result": changed}
                with self.subTest(kind=kind, decoded=decoded, field=field, missing=missing):
                    self.assert_schema_unknown(method, params, response, check_id)
        for field in ("jsonrpc", "id", "result"):
            for missing in (False, True):
                response = {"jsonrpc": "2.0", "id": "gate", "result": self.original("query", {
                    "request_type": "view_account", "account_id": "old.near", "block_id": HEIGHT})}
                if missing:
                    del response[field]
                else:
                    response[field] = True
                with self.subTest(envelope=field, missing=missing):
                    self.assert_schema_unknown("query", {"request_type": "view_account", "account_id": "old.near",
                                                         "block_id": HEIGHT}, response, "receiver_exists")

    def assert_method_constraint(self, method, value, check_id):
        params = {"request_type": "call_function", "method_name": method,
                  "account_id": "old.near", "block_id": HEIGHT}
        response = {"jsonrpc": "2.0", "id": "gate", "result": {
            "block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32), "logs": [],
            "result": list(json.dumps(value).encode())}}
        self.assert_schema_unknown("query", params, response, check_id)

    def test_nested_error_diagnostic_fields_require_declared_types(self):
        params = {"request_type": "call_function", "method_name": "contract_source_metadata",
                  "account_id": "old.near", "block_id": HEIGHT}
        for field, value in (("vm_error", ["MethodNotFound"]), ("message", {"MethodNotFound": True}),
                             ("name", ["MethodNotFound"]), ("code", True), ("limit", "1"),
                             ("cause", []), ("cause", {"name": []})):
            for nested in (False, True):
                data = {field: value}
                error = {**_query_error("CONTRACT_EXECUTION_ERROR", dict(self.info)),
                         "data": {"nested": [data]} if nested else data}
                with self.subTest(field=field, value=value, nested=nested):
                    self.assert_schema_unknown("query", params,
                                               {"jsonrpc": "2.0", "id": "gate", "error": error},
                                               "code_without_metadata")

    def test_missing_method_markers_only_come_from_diagnostic_strings(self):
        for data in ({"MethodNotFound": False}, {"unread": "MethodNotFound"},
                     {"nested": ["CodeDoesNotExist"]}, {"name": "MethodNotFound"}):
            error = {**_query_error("CONTRACT_EXECUTION_ERROR", dict(self.info)), "data": data}
            with self.subTest(data=data), self.response(error):
                self.assertEqual(self.judge().verdict, "UNKNOWN")
        for data in ("MethodNotFound", {"vm_error": "MethodNotFound"}):
            error = {**_query_error("CONTRACT_EXECUTION_ERROR", dict(self.info)), "data": data}
            with self.subTest(data=data), self.response(error):
                self.assertEqual(self.judge().verdict, "ALLOW")

    def test_storage_available_cannot_exceed_total(self):
        for total, available in (("0", "1"), ("1", "2"), ("9", "10")):
            with self.subTest(total=total, available=available):
                self.assert_method_constraint("storage_balance_of", {"total": total, "available": available},
                                              "storage_registered")

    def test_summary_singleton_deny_references_must_match(self):
        for accepted_id, deny_id in ((1, 2), (2, 1), (0, 1)):
            with self.subTest(accepted_id=accepted_id, deny_id=deny_id):
                self.assert_method_constraint("subject_summary", {
                    "accepted": 1, "pending": 0, "contested": 0, "latest_accepted_id": accepted_id,
                    "latest_deny_id": deny_id, "deny": True}, "community_report")

    def test_summary_contested_is_a_subset_of_accepted(self):
        for accepted, contested in ((0, 1), (1, 2), (2, 3)):
            with self.subTest(accepted=accepted, contested=contested):
                self.assert_method_constraint("subject_summary", {
                    "accepted": accepted, "pending": 0, "contested": contested,
                    "latest_accepted_id": 0 if accepted else None,
                    "latest_deny_id": None, "deny": False}, "community_report")

    def test_summary_accepted_count_and_reference_are_paired(self):
        for accepted, reference in ((0, 0), (1, None), (2, None)):
            with self.subTest(accepted=accepted, reference=reference):
                self.assert_method_constraint("subject_summary", {
                    "accepted": accepted, "pending": 0, "contested": 0,
                    "latest_accepted_id": reference, "latest_deny_id": None, "deny": False}, "community_report")

    def test_summary_deny_and_reference_are_paired(self):
        for deny, reference in ((False, 0), (True, None)):
            with self.subTest(deny=deny, reference=reference):
                self.assert_method_constraint("subject_summary", {
                    "accepted": 1, "pending": 0, "contested": 0, "latest_accepted_id": 0,
                    "latest_deny_id": reference, "deny": deny}, "community_report")

    def test_summary_deny_requires_accepted_reports(self):
        for reference in (0, 1, 2):
            with self.subTest(reference=reference):
                self.assert_method_constraint("subject_summary", {
                    "accepted": 0, "pending": 0, "contested": 0, "latest_accepted_id": None,
                    "latest_deny_id": reference, "deny": True}, "community_report")

    def test_summary_ids_must_have_been_allocatable(self):
        for field in ("latest_accepted_id", "latest_deny_id"):
            for reference in (2**64 - 1, 2**64):
                value = {"accepted": 2, "pending": 0, "contested": 0,
                         "latest_accepted_id": 0, "latest_deny_id": 0, "deny": True, field: reference}
                with self.subTest(field=field, reference=reference):
                    self.assert_method_constraint("subject_summary", value, "community_report")

    def test_summary_disjoint_counts_fit_allocated_report_space(self):
        for accepted, pending in ((2**64 - 1, 1), (1, 2**64 - 1), (2**63, 2**63)):
            with self.subTest(accepted=accepted, pending=pending):
                self.assert_method_constraint("subject_summary", {
                    "accepted": accepted, "pending": pending, "contested": 0, "latest_accepted_id": 0,
                    "latest_deny_id": None, "deny": False}, "community_report")

    def test_token_reference_and_hash_are_nullable_together(self):
        for fields in ({"reference": "ref"}, {"reference_hash": "aGFzaA=="},
                       {"reference": None, "reference_hash": "aGFzaA=="},
                       {"reference": "ref", "reference_hash": None}):
            with self.subTest(fields=fields):
                self.assert_method_constraint("ft_metadata", {"name": "Token", "symbol": "T", "decimals": 6,
                                                             **fields}, "ft_metadata_sane")

    def test_code_hash_matches_decoded_code(self):
        params = {"request_type": "view_code", "account_id": "old.near", "block_id": HEIGHT}
        for code, digest in (("AA==", "Code"), ("AQ==", rpc.EMPTY_CODE_HASH), ("", rpc.EMPTY_CODE_HASH)):
            with self.subTest(code=code, digest=digest):
                self.assert_schema_unknown("query", params, {"jsonrpc": "2.0", "id": "gate", "result": {
                    "block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32), "code_base64": code, "hash": digest}}, None)

    def test_access_key_list_has_unique_public_keys(self):
        params = {"request_type": "view_access_key_list", "account_id": "old.near", "block_id": HEIGHT}
        for nonce in (1, 2):
            keys = [{"public_key": "ed25519:" + "1" * 32, "access_key": {"nonce": n, "permission": "FullAccess"}}
                    for n in (1, nonce)]
            with self.subTest(nonce=nonce):
                self.assert_schema_unknown("query", params, {"jsonrpc": "2.0", "id": "gate", "result": {
                    "block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32), "keys": keys}}, "account_age")

    def test_balance_fields_fit_unsigned_amount_range(self):
        for amount in (str(2**128), "9" * 5000):
            with self.subTest(amount_length=len(amount)):
                self.assert_method_constraint("storage_balance_of", {"total": amount, "available": "0"},
                                              "storage_registered")

    def test_error_limits_fit_unsigned_height_range(self):
        params = {"request_type": "call_function", "method_name": "contract_source_metadata",
                  "account_id": "old.near", "block_id": HEIGHT}
        for limit in (2**64, 2**64 + 1):
            error = _query_error("TOO_MANY_ACCESS_KEYS", {**self.info, "limit": limit})
            with self.subTest(limit=limit):
                self.assert_schema_unknown("query", params, {"jsonrpc": "2.0", "id": "gate", "error": error},
                                           "code_without_metadata")

    def test_token_reference_hash_requires_canonical_base64(self):
        for digest in ("!", "A", "AA=A", "A" * 43, "A" * 44 + "="):
            with self.subTest(digest=digest):
                self.assert_method_constraint("ft_metadata", {"name": "Token", "symbol": "T", "decimals": 6,
                    "reference": "ref", "reference_hash": digest}, "ft_metadata_sane")

    def test_unpinned_contexts_must_agree_with_each_other(self):
        chain = rpc.Client("https://archival.example", "replay")
        for field, value in (("block_height", HEIGHT + 1), ("block_hash", rpc.base58.encode(bytes([2]) * 32)),
                             ("requested_account_id", "other.near"), ("account_id", "other.near")):
            for error_response in (False, True):
                response = ({"error": {**_query_error("NO_CONTRACT_CODE", dict(self.info)),
                                       "data": {"nested": [{field: value}]}}} if error_response else
                            {"result": {**self.original("query", {"request_type": "view_account", "block_id": HEIGHT,
                                                                 "account_id": "old.near"}),
                                        "account_id": "old.near", "data": {field: value}}})
                with self.subTest(field=field, error_response=error_response):
                    with mock.patch.object(chain, "_exchange", return_value={"jsonrpc": "2.0", "id": "gate", **response}):
                        with self.assertRaises(rpc.MalformedResponse):
                            chain.view_account("old.near")

    def test_block_header_binds_to_numeric_and_hash_requests(self):
        chain = rpc.Client("https://archival.example", "replay")
        response = {"jsonrpc": "2.0", "id": "gate", "result": {"header": {
            "height": 2, "timestamp": START_NS, "hash": rpc.base58.encode(bytes([1]) * 32)}}}
        for block_id in (3, rpc.base58.encode(bytes([2]) * 32)):
            with self.subTest(block_id=block_id), mock.patch.object(chain, "_exchange", return_value=response):
                with self.assertRaises(rpc.MalformedResponse):
                    chain.call("block", {"block_id": block_id})

    def test_cross_field_controls_preserve_valid_payloads(self):
        chain = rpc.Client("https://archival.example", "replay")
        summary = {"accepted": 2, "pending": 0, "contested": 1,
                   "latest_accepted_id": 0, "latest_deny_id": 4, "deny": True}
        values = [("subject_summary", summary), ("subject_summary", {**summary, "latest_accepted_id": 4,
                   "latest_deny_id": 0}), ("subject_summary", {**summary, "accepted": 1,
                   "latest_accepted_id": 0, "latest_deny_id": 0}),
                  ("storage_balance_of", {"total": "0" * 5000 + "1", "available": "1"}),
                  ("storage_balance_of", {"total": str(2**128 - 1), "available": "0"}),
                  ("ft_metadata", {"name": "Token", "symbol": "T", "decimals": 6,
                   "reference": "ref", "reference_hash": "A" * 43 + "="})]
        for method, value in values:
            response = {"jsonrpc": "2.0", "id": "gate", "result": {"block_height": HEIGHT,
                "block_hash": rpc.base58.encode(bytes([1]) * 32), "logs": [], "result": list(json.dumps(value).encode())}}
            with self.subTest(method=method, value=value), mock.patch.object(chain, "_exchange", return_value=response):
                self.assertEqual(chain.call_function("old.near", method, {}), value)

    def test_matching_block_contexts_agree_with_header(self):
        chain = rpc.Client("https://archival.example", "replay")
        for fields in ({"block_height": 2}, {"block_hash": rpc.base58.encode(bytes([1]) * 32)},
                       {"block_height": 2, "block_hash": rpc.base58.encode(bytes([1]) * 32)}):
            response = {"jsonrpc": "2.0", "id": "gate", "result": {"header": {
                "height": 2, "timestamp": START_NS, "hash": rpc.base58.encode(bytes([1]) * 32)}, **fields}}
            with self.subTest(fields=fields), mock.patch.object(chain, "_exchange", return_value=response):
                self.assertEqual(chain.block_at(2), response["result"])

    def test_schema_numeric_ranges_reject_out_of_range_fields(self):
        context = {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32)}
        access = {"nonce": 1, "permission": "FullAccess"}
        rows = [
            ("block", {"header": {"height": 2, "timestamp": START_NS, "hash": rpc.base58.encode(bytes([1]) * 32)}},
             (("header.height", 2**64), ("header.timestamp", 2**64)), "account_age"),
            ("view_account", {**context, "amount": "1", "locked": "0", "storage_usage": 1, "storage_paid_at": 0,
                              "code_hash": rpc.base58.encode(bytes([3]) * 32)},
             (("amount", str(2**128)), ("locked", str(2**128)), ("storage_usage", 2**64),
              ("storage_paid_at", 2**64), ("block_height", 2**64)), "receiver_exists"),
            ("view_access_key", {**context, **access}, (("nonce", 2**64),), None),
            ("view_access_key_list", {**context, "keys": [{"public_key": "ed25519:" + "1" * 32, "access_key": access}]},
             (("keys.0.access_key.nonce", 2**64),), "account_age"),
            ("view_access_key", {**context, "nonce": 1, "permission": {"FunctionCall": {
                "allowance": "0", "receiver_id": "old.near", "method_names": []}}},
             (("permission.FunctionCall.allowance", str(2**128)),), None),
        ]
        for kind, value, fields, check_id in rows:
            for field, invalid in fields:
                changed = json.loads(json.dumps(value))
                target = changed
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[int(part)] if isinstance(target, list) else target[part]
                target[parts[-1]] = invalid
                method = "block" if kind == "block" else "query"
                params = {"block_id": 2} if method == "block" else {
                    "request_type": kind, "account_id": "old.near", "block_id": HEIGHT}
                with self.subTest(kind=kind, field=field):
                    self.assert_schema_unknown(method, params, {"jsonrpc": "2.0", "id": "gate", "result": changed},
                                               check_id)

    def test_independent_fields_and_matching_code_digest_remain_valid(self):
        chain = rpc.Client("https://archival.example", "replay")
        context = {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32)}
        for amount, locked in (("0", "1"), ("1", "0"), (str(2**128 - 1), "0")):
            value = {**context, "amount": amount, "locked": locked, "storage_usage": 1,
                     "code_hash": rpc.EMPTY_CODE_HASH}
            with self.subTest(amount=amount, locked=locked), mock.patch.object(chain, "_exchange", return_value={
                    "jsonrpc": "2.0", "id": "gate", "result": value}):
                self.assertEqual(chain.view_account("old.near"), value)
        for code in (b"", bytes(1), b"code"):
            value = {**context, "code_base64": rpc.base64.b64encode(code).decode(),
                     "hash": rpc.base58.encode(hashlib.sha256(code).digest())}
            with self.subTest(code=code), mock.patch.object(chain, "_exchange", return_value={
                    "jsonrpc": "2.0", "id": "gate", "result": value}):
                self.assertEqual(chain.call("query", {"request_type": "view_code", "account_id": "old.near",
                                                     "finality": "final"}), value)

    def test_existing_rpc_fixtures_validate_unchanged(self):
        root = rpc.FIXTURES_DIR.parent
        malformed, unrecorded = set(), set()
        for path in sorted(root.rglob("*.json")):
            fixture = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(fixture, dict) or not isinstance(fixture.get("request"), dict):
                continue
            request = fixture["request"]
            if request.get("method") in (None, "GET", "POST"):
                continue
            identifier = (path.parent.relative_to(root).as_posix(), path.stem[:8])
            client = rpc.Client(fixture["url"], "replay", fixtures_dir=path.parent)
            with self.subTest(fixture=path.relative_to(root).as_posix()):
                try:
                    client.call(request["method"], request["params"])
                except rpc.RpcError as error:
                    self.assertIsNotNone(error.error)
                except rpc.MalformedResponse:
                    malformed.add(identifier)
                except rpc.FixtureMiss as error:
                    self.assertNotEqual(error.key, path.stem)
                    unrecorded.add(identifier)
        self.assertEqual(malformed, _MALFORMED_RPC_FIXTURES)
        self.assertEqual(unrecorded, _PINNED_BLOCK_UNRECORDED)

    def test_pinned_block_hash_is_read_once_per_client(self):
        chain = rpc.Client("https://archival.example", "replay")
        params = {"request_type": "view_account", "account_id": "old.near", "block_id": HEIGHT}
        calls = []
        def exchange(url, method, called, body):
            calls.append(method)
            return {"jsonrpc": "2.0", "id": "gate", "result": self.original(method, called)}
        with mock.patch.object(chain, "_exchange", side_effect=exchange):
            for _ in range(3):
                chain.call("query", params)
        self.assertEqual(calls, ["query", "block", "query", "query"])

    def test_reduced_recordings_are_listed_with_their_bytes_and_validate_in_replay(self):
        root = rpc.FIXTURES_DIR.parent
        self.assertEqual(len(rpc.REDUCED_RECORDINGS), 11)
        for path, fixture in _reduced_fixtures():
            name = path.relative_to(root).as_posix()
            with self.subTest(fixture=name):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), rpc.REDUCED_RECORDINGS[name])
                client = rpc.Client(fixture["url"], "replay", fixtures_dir=path.parent)
                request = fixture["request"]
                self.assertEqual(client.call(request["method"], request["params"]), fixture["response"]["result"])

    def test_a_changed_byte_in_a_reduced_recording_reads_malformed(self):
        for path, fixture in _reduced_fixtures():
            data = path.read_bytes()
            changed = data[:-1] + b" "
            self.assertEqual(json.loads(changed), json.loads(data))
            client = rpc.Client(fixture["url"], "replay", fixtures_dir=path.parent)
            request = fixture["request"]
            with self.subTest(fixture=path.name), mock.patch.object(client, "_fixture_bytes", return_value=changed):
                with self.assertRaisesRegex(rpc.MalformedResponse, "lacks"):
                    client.call(request["method"], request["params"])

    def test_a_reduced_body_fetched_in_mode_off_reads_malformed(self):
        for path, fixture in _reduced_fixtures():
            client = rpc.Client(fixture["url"], "off", fixtures_dir=path.parent)
            request = fixture["request"]
            with self.subTest(fixture=path.name),                     mock.patch.object(rpc, "_fetch", return_value=fixture["response"]),                     mock.patch.object(client, "_metered", return_value=({}, False)):
                with self.assertRaisesRegex(rpc.MalformedResponse, "lacks"):
                    client.call(request["method"], request["params"])

    def test_a_reduced_recording_cannot_answer_a_pinned_request(self):
        for path, fixture in _reduced_fixtures():
            client = rpc.Client(fixture["url"], "replay", fixtures_dir=path.parent)
            params = {**fixture["request"]["params"], "block_id": fixture["response"]["result"]["block_height"]}
            params.pop("finality")
            with self.subTest(fixture=path.name), mock.patch.object(rpc, "fixture_key", return_value=path.stem):
                with self.assertRaisesRegex(rpc.MalformedResponse, "cannot answer a pinned request"):
                    client.call(fixture["request"]["method"], params)
                self.assertTrue(client.last_reduced)

    def test_a_reduced_recording_under_an_unlisted_name_reads_malformed(self):
        for path, fixture in _reduced_fixtures():
            with self.subTest(fixture=path.name), tempfile.TemporaryDirectory(dir=path.parent) as directory:
                (Path(directory) / path.name).write_bytes(path.read_bytes())
                client = rpc.Client(fixture["url"], "replay", fixtures_dir=directory)
                request = fixture["request"]
                with self.assertRaisesRegex(rpc.MalformedResponse, "lacks"):
                    client.call(request["method"], request["params"])

    def test_matching_timestamp_alias_and_unread_extra_fields_validate(self):
        chain = rpc.Client("https://archival.example", "replay")
        response = {"jsonrpc": "2.0", "id": "gate", "result": {"header": {
            "height": 2, "hash": rpc.base58.encode(bytes([1]) * 32), "timestamp": START_NS + 2 * 10**9,
            "timestamp_nanosec": str(START_NS + 2 * 10**9), "unused": []}, "chunks": []}}
        with mock.patch.object(chain, "_exchange", return_value=response):
            self.assertEqual(chain.block_at(2), response["result"])
            self.assertEqual(AsOf(chain, HEIGHT).block_at(2), response["result"])

    def test_recording_keeps_invalid_timestamp_alias_unchanged(self):
        for alias in ("1776297600000000000", [], True):
            response = {"jsonrpc": "2.0", "id": "gate", "result": {"header": {
                "height": 2, "hash": rpc.base58.encode(bytes([1]) * 32), "timestamp": START_NS + 2 * 10**9, "timestamp_nanosec": alias}}}
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as directory:
                client = rpc.Client("https://archival.example", "record", fixtures_dir=directory)
                with mock.patch.object(rpc, "_fetch", return_value=response):
                    with self.assertRaises(rpc.MalformedResponse):
                        client.block_at(2)
                [path] = client.fixtures_dir.glob("*.json")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["response"], response)
                client.mode = "replay"
                with self.assertRaises(rpc.MalformedResponse):
                    client.block_at(2)


    def test_closed_error_pairs_and_required_fields_read_unknown(self):
        params = {"request_type": "view_account", "account_id": "old.near", "block_id": HEIGHT}
        valid = {"name": "HANDLER_ERROR", "code": -32000, "message": "Server error",
                 "cause": {"name": "UNKNOWN_ACCOUNT", "info": dict(self.info)}}
        invalid = [{**valid, "name": "REQUEST_VALIDATION_ERROR"},
                   {**valid, "name": "INTERNAL_ERROR"},
                   {**valid, "name": "OTHER_ERROR"},
                   _query_error("OTHER_CAUSE", dict(self.info))]
        invalid += [{key: value for key, value in valid.items() if key != missing}
                    for missing in ("name", "code", "message", "cause")]
        invalid += [{**valid, "code": code} for code in (2**100, 2**63, -2**63 - 1)]
        for missing in ("requested_account_id", "block_height", "block_hash"):
            invalid.append(_query_error("UNKNOWN_ACCOUNT", {
                key: value for key, value in self.info.items() if key != missing}))
        for index, error in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_schema_unknown("query", params,
                    {"jsonrpc": "2.0", "id": "gate", "error": error}, "receiver_exists")

    def test_invalid_string_encodings_read_unknown(self):
        rows = [("view_account", "code_hash", "receiver_exists"),
                ("view_account", "block_hash", "receiver_exists"),
                ("view_access_key_list", "public_key", "account_age"),
                ("block", "hash", "account_age")]
        for kind, field, check_id in rows:
            params = ({"block_id": 2} if kind == "block" else
                      {"request_type": kind, "account_id": "old.near", "block_id": HEIGHT})
            method = "block" if kind == "block" else "query"
            result = self.original(method, params)
            if kind == "block":
                result["header"][field] = "!"
            elif field == "public_key":
                result["keys"][0][field] = "!"
            else:
                result[field] = "!"
            with self.subTest(kind=kind, field=field):
                self.assert_schema_unknown(method, params,
                    {"jsonrpc": "2.0", "id": "gate", "result": result}, check_id)


    def test_error_pair_table_requires_each_variant_field(self):
        block = {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32)}
        account = {**block, "requested_account_id": "old.near"}
        rows = [
            ("REQUEST_VALIDATION_ERROR", "METHOD_NOT_FOUND", {"method_name": "query"}),
            ("REQUEST_VALIDATION_ERROR", "PARSE_ERROR", {"error_message": "invalid JSON"}),
            ("INTERNAL_ERROR", "INTERNAL_ERROR", {"error_message": "unavailable"}),
            ("HANDLER_ERROR", "NO_SYNCED_BLOCKS", {}),
            ("HANDLER_ERROR", "NOT_SYNCED_YET", {}),
            ("HANDLER_ERROR", "UNAVAILABLE_SHARD", {"requested_shard_id": 1}),
            ("HANDLER_ERROR", "GARBAGE_COLLECTED_BLOCK", block),
            ("HANDLER_ERROR", "UNKNOWN_BLOCK", {}),
            ("HANDLER_ERROR", "INVALID_ACCOUNT", account),
            ("HANDLER_ERROR", "UNKNOWN_ACCOUNT", account),
            ("HANDLER_ERROR", "NO_CONTRACT_CODE", {**block, "contract_account_id": "old.near"}),
            ("HANDLER_ERROR", "TOO_LARGE_CONTRACT_STATE", {**block, "contract_account_id": "old.near"}),
            ("HANDLER_ERROR", "UNKNOWN_ACCESS_KEY", {**block, "public_key": "ed25519:" + "1" * 32}),
            ("HANDLER_ERROR", "UNKNOWN_GAS_KEY", {**block, "public_key": "ed25519:" + "1" * 32}),
            ("HANDLER_ERROR", "CONTRACT_EXECUTION_ERROR", {**block, "vm_error": "execution failed"}),
            ("HANDLER_ERROR", "TOO_MANY_ACCESS_KEYS", {**account, "limit": 100}),
        ]
        fixtures = [json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(rpc.FIXTURES_DIR.glob("*.json"))]
        for cause in ("UNKNOWN_ACCOUNT", "TOO_MANY_ACCESS_KEYS"):
            control = next(item["response"]["error"] for item in fixtures
                           if isinstance(item["response"].get("error"), dict)
                           and item["response"]["error"]["cause"]["name"] == cause)
            rpc._validate_schema(control, rpc._ERROR_SCHEMA)
        for name, cause, info in rows:
            value = {"name": name, "code": {"METHOD_NOT_FOUND": -32601, "PARSE_ERROR": -32700}.get(cause, -32000), "message": "Server error",
                     "cause": {"name": cause, **({"info": info} if info else {})}}
            with self.subTest(name=name, cause=cause):
                rpc._validate_schema(value, rpc._ERROR_SCHEMA)
            for code in (-32000, -32601, -32700, -32603):
                if code != value["code"]:
                    with self.subTest(name=name, cause=cause, code=code):
                        with self.assertRaises(rpc.MalformedResponse):
                            rpc._validate_schema({**value, "code": code}, rpc._ERROR_SCHEMA)
            for missing in info:
                broken = {**value, "cause": {"name": cause, "info": {
                    key: child for key, child in info.items() if key != missing}}}
                with self.subTest(name=name, cause=cause, missing=missing):
                    with self.assertRaises(rpc.MalformedResponse):
                        rpc._validate_schema(broken, rpc._ERROR_SCHEMA)
            for other in ("REQUEST_VALIDATION_ERROR", "HANDLER_ERROR", "INTERNAL_ERROR", "OTHER"):
                if other == name:
                    continue
                with self.subTest(name=other, cause=cause):
                    with self.assertRaises(rpc.MalformedResponse):
                        rpc._validate_schema({**value, "name": other}, rpc._ERROR_SCHEMA)
        for code in (-2**63, 2**63 - 1):
            with self.assertRaises(rpc.MalformedResponse):
                rpc._validate_schema({"name": "HANDLER_ERROR", "code": code, "message": "Server error",
                                      "cause": {"name": "UNKNOWN_ACCOUNT", "info": account}}, rpc._ERROR_SCHEMA)

    def test_matching_invalid_block_hashes_still_read_unknown(self):
        chain = rpc.Client("https://archival.example", "replay")
        pinned = AsOf(chain, HEIGHT)
        def exchange(url, method, params, body):
            result = self.original(method, params)
            chain.last_source = dict(self.chain.last_source)
            if method == "block":
                result["header"]["hash"] = "!"
            else:
                result["block_hash"] = "!"
            return {"jsonrpc": "2.0", "id": "gate", "result": result}
        with mock.patch.object(chain, "_exchange", side_effect=exchange):
            for client in (chain, pinned):
                with self.subTest(client=type(client).__name__):
                    with self.assertRaises(rpc.MalformedResponse):
                        client.view_account_at("old.near", HEIGHT)
            with mock.patch.object(self, "client", pinned):
                judgement = self.judge()
        self.assertEqual(judgement.verdict, "UNKNOWN")
        self.assertEqual(next(c for c in judgement.checks if c["id"] == "receiver_exists")["status"], "unknown")


    def test_nested_error_pairs_and_block_references_are_closed(self):
        params = {"request_type": "view_account", "account_id": "old.near", "block_id": HEIGHT}
        for missing in (None, "code", "message"):
            nested = {"name": "REQUEST_VALIDATION_ERROR", "code": -32000, "message": "Server error",
                      "cause": {"name": "UNKNOWN_ACCOUNT", "info": dict(self.info)}}
            if missing:
                del nested[missing]
            error = {**_query_error("UNKNOWN_ACCOUNT", self.info), "data": {"nested": [nested]}}
            with self.subTest(missing=missing):
                self.assert_schema_unknown("query", params,
                    {"jsonrpc": "2.0", "id": "gate", "error": error}, "receiver_exists")
        for reference in ({"block_id": HEIGHT, "finality": "invalid"},
                          {"block_id": HEIGHT, "unexpected": "!"},
                          {"finality": "final", "sync_checkpoint": "genesis"}):
            error = _query_error("UNKNOWN_BLOCK", {**self.info, "block_reference": reference})
            with self.subTest(reference=reference):
                self.assert_schema_unknown("query", params,
                    {"jsonrpc": "2.0", "id": "gate", "error": error}, "receiver_exists")

    def test_global_contract_identifier_encodings(self):
        block = {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32)}
        for identifier in ({"hash": rpc.EMPTY_CODE_HASH}, {"account_id": "old.near"}):
            error = _query_error("NO_GLOBAL_CONTRACT_CODE", {**block, "identifier": identifier})
            with self.subTest(identifier=identifier):
                rpc._validate_schema(error, rpc._ERROR_SCHEMA)
                for missing in ("identifier", "block_height", "block_hash"):
                    broken = json.loads(json.dumps(error))
                    del broken["cause"]["info"][missing]
                    with self.assertRaises(rpc.MalformedResponse):
                        rpc._validate_schema(broken, rpc._ERROR_SCHEMA)
        for identifier in ({}, {"hash": "!"}, {"account_id": "!"},
                           {"hash": rpc.EMPTY_CODE_HASH, "account_id": "old.near"}):
            with self.subTest(identifier=identifier):
                with self.assertRaises(rpc.MalformedResponse):
                    rpc._validate_schema(_query_error("NO_GLOBAL_CONTRACT_CODE", {
                        **block, "identifier": identifier}), rpc._ERROR_SCHEMA)


    def test_query_unknown_block_requires_its_reference(self):
        params = {"request_type": "view_account", "account_id": "old.near", "block_id": HEIGHT}
        error = _query_error("UNKNOWN_BLOCK", self.info)
        self.assert_schema_unknown("query", params,
            {"jsonrpc": "2.0", "id": "gate", "error": error}, "receiver_exists")

    def test_generic_result_strings_require_utf8(self):
        chain = rpc.Client("https://archival.example", "replay")
        response = {"jsonrpc": "2.0", "id": "gate", "result": chr(0xd800)}
        with mock.patch.object(chain, "_exchange", return_value=response):
            with self.assertRaises(rpc.MalformedResponse):
                chain.call("status", [])


    def test_contract_error_account_context_cannot_conflict(self):
        params = {"request_type": "call_function", "account_id": "old.near", "block_id": HEIGHT,
                  "method_name": "contract_source_metadata"}
        error = _query_error("NO_CONTRACT_CODE", self.info)
        error["cause"]["info"]["contract_account_id"] = "other.near"
        self.assert_schema_unknown("query", params,
            {"jsonrpc": "2.0", "id": "gate", "error": error}, "code_without_metadata")


    def test_account_global_contract_fields_read_unknown_when_malformed(self):
        params = {"request_type": "view_account", "account_id": "old.near", "block_id": HEIGHT}
        invalid = [{field: value} for field in ("global_contract_hash", "global_contract_account_id")
                   for value in ("!", 123)]
        invalid += [{"global_contract_hash": rpc.EMPTY_CODE_HASH, "global_contract_account_id": "global.near"},
                    {"global_contract_hash": rpc.EMPTY_CODE_HASH,
                     "code_hash": rpc.base58.encode(bytes([3]) * 32)},
                    {"global_contract_account_id": "global.near",
                     "code_hash": rpc.base58.encode(bytes([3]) * 32)}]
        for fields in invalid:
            result = self.original("query", params)
            result.update(fields)
            with self.subTest(fields=fields):
                self.assert_schema_unknown("query", params,
                    {"jsonrpc": "2.0", "id": "gate", "result": result}, "receiver_exists")

    def test_error_codes_are_bound_to_their_variants_before_judging(self):
        params = {"request_type": "view_account", "account_id": "old.near", "block_id": HEIGHT}
        for code in (-32601, -32700, -32603, -2**63, 2**63 - 1, 0):
            error = {**_query_error("UNKNOWN_ACCOUNT", self.info), "code": code}
            with self.subTest(code=code):
                self.assert_schema_unknown("query", params,
                    {"jsonrpc": "2.0", "id": "gate", "error": error}, "receiver_exists")


    def test_global_contract_account_views_accept_protocol_variants(self):
        params = {"request_type": "view_account", "account_id": "old.near", "block_id": HEIGHT}
        chain = rpc.Client("https://archival.example", "replay")
        for fields in ({}, {"global_contract_hash": None, "global_contract_account_id": None},
                       {"global_contract_hash": rpc.EMPTY_CODE_HASH},
                       {"global_contract_account_id": "global.near"}):
            result = {**self.original("query", params), **fields}
            def exchange(url, method, params, body):
                return {"jsonrpc": "2.0", "id": "gate", "result":
                        self.original(method, params) if method == "block" else result}
            with self.subTest(fields=fields), mock.patch.object(chain, "_exchange", side_effect=exchange):
                for client in (chain, AsOf(chain, HEIGHT)):
                    self.assertEqual(client.call("query", params), result)

    def test_access_key_handles_permissions_and_historical_receiver_text(self):
        params = {"request_type": "view_access_key_list", "account_id": "old.near", "block_id": HEIGHT}
        chain = rpc.Client("https://archival.example", "replay")
        function = {"allowance": None, "receiver_id": "historical genesis text!", "method_names": ["pay"]}
        gas = {"balance": "1", "num_nonces": 65535}
        for public_key in ("ed25519:" + "1" * 32, "secp256k1:" + "1" * 64, "ml-dsa-65-hash:" + "1" * 32):
            for permission in ("FullAccess", {"FunctionCall": function}, {"GasKeyFullAccess": gas},
                               {"GasKeyFunctionCall": {**gas, **function}}):
                result = {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32),
                          "keys": [{"public_key": public_key, "access_key": {"nonce": 1, "permission": permission}}]}
                def exchange(url, method, params, body):
                    return {"jsonrpc": "2.0", "id": "gate", "result":
                            self.original(method, params) if method == "block" else result}
                with self.subTest(public_key=public_key, permission=permission):
                    with mock.patch.object(chain, "_exchange", side_effect=exchange):
                        for client in (chain, AsOf(chain, HEIGHT)):
                            self.assertEqual(client.call("query", params), result)
        with mock.patch.object(chain, "_exchange", side_effect=exchange):
            with self.assertRaises(rpc.MalformedResponse):
                chain.call("query", {**params, "account_id": "historical genesis text!"})

    def test_malformed_key_handles_and_gas_permissions_read_unknown(self):
        params = {"request_type": "view_access_key_list", "account_id": "old.near", "block_id": HEIGHT}
        gas = {"balance": "1", "num_nonces": 1}
        function = {"allowance": None, "receiver_id": "old.near", "method_names": []}
        invalid = [(key, "FullAccess") for key in ("ml-dsa-65-hash:!", "ml-dsa-65-hash:" + "1" * 31,
                                                   "ml-dsa-65:" + "1" * 1952)]
        for variant, fields in (("GasKeyFullAccess", gas), ("GasKeyFunctionCall", {**gas, **function})):
            invalid += [("ed25519:" + "1" * 32, {variant: {key: value for key, value in fields.items() if key != missing}})
                        for missing in fields]
            invalid += [("ed25519:" + "1" * 32, {variant: {**fields, field: value}})
                        for field, value in (("balance", 1), ("balance", str(2**128)),
                                             ("num_nonces", True), ("num_nonces", -1), ("num_nonces", 65536))]
        for public_key, permission in invalid:
            result = {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32),
                      "keys": [{"public_key": public_key, "access_key": {"nonce": 1, "permission": permission}}]}
            with self.subTest(public_key=public_key[:30], permission=permission):
                self.assert_schema_unknown("query", params,
                    {"jsonrpc": "2.0", "id": "gate", "result": result}, "account_age")

    def test_query_errors_use_full_public_keys_instead_of_key_handles(self):
        chain = rpc.Client("https://archival.example", "replay")
        block = {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32)}
        for cause in ("UNKNOWN_ACCESS_KEY", "UNKNOWN_GAS_KEY"):
            for key in ("ed25519:" + "1" * 32, "secp256k1:" + "1" * 64, "ml-dsa-65:" + "1" * 1952):
                error = _query_error(cause, {**block, "public_key": key})
                with self.subTest(cause=cause, key=key[:20]), mock.patch.object(chain, "_exchange", return_value={
                        "jsonrpc": "2.0", "id": "gate", "error": error}):
                    with self.assertRaises(rpc.RpcError):
                        chain.view_access_key_list("old.near")
            error = _query_error(cause, {**block, "public_key": "ml-dsa-65-hash:" + "1" * 32})
            with self.assertRaises(rpc.MalformedResponse):
                rpc._validate_schema(error, rpc._ERROR_SCHEMA)

    def test_structured_contract_execution_errors_accept_protocol_variants(self):
        chain = rpc.Client("https://archival.example", "replay")
        variants = [{"MethodResolveError": "MethodNotFound"}, {"LinkError": {"msg": "link"}},
                    {"CompilationError": {"CodeDoesNotExist": {"account_id": "global.near"}}},
                    {"CompilationError": {"PrepareError": "TooManyFunctions"}},
                    {"CompilationError": {"WasmerCompileError": {"msg": "compile"}}},
                    {"WasmTrap": "Unreachable"}, "WasmUnknownError", "_EVMError",
                    {"HostError": "BadUTF8"}, {"HostError": {"GuestPanic": {"panic_msg": "panic"}}},
                    {"HostError": {"KeyLengthExceeded": {"length": 2, "limit": 1}}},
                    {"ExecutionError": "failed"}]
        for structured in variants:
            error = _query_error("CONTRACT_EXECUTION_ERROR", {**self.info, "vm_error": "execution failed", "error": structured})
            for location in ("outer", "result", "decoded"):
                context = {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32)}
                payload = {"error": error}
                response = {"jsonrpc": "2.0", "id": "gate", **payload}
                if location == "result":
                    response = {"jsonrpc": "2.0", "id": "gate", "result": {**context, **payload}}
                elif location == "decoded":
                    response = {"jsonrpc": "2.0", "id": "gate", "result": {**context, "logs": [],
                                "result": list(json.dumps(payload).encode())}}
                def exchange(url, method, params, body):
                    return ({"jsonrpc": "2.0", "id": "gate", "result": self.original(method, params)}
                            if method == "block" else response)
                with self.subTest(structured=structured, location=location):
                    with mock.patch.object(chain, "_exchange", side_effect=exchange):
                        for client in (chain, AsOf(chain, HEIGHT)):
                            with self.assertRaises(rpc.RpcError):
                                client.call_function("old.near", "contract_source_metadata", {}, block_id=HEIGHT)

    def test_malformed_structured_contract_errors_read_unknown(self):
        params = {"request_type": "call_function", "account_id": "old.near", "block_id": HEIGHT,
                  "method_name": "contract_source_metadata"}
        for structured in (None, 123, {}, {"MethodResolveError": "Unknown"},
                           {"MethodResolveError": "MethodNotFound", "ExecutionError": "failed"},
                           {"LinkError": {}}, {"HostError": {"GuestPanic": {"panic_msg": 1}}},
                           {"HostError": {"KeyLengthExceeded": {"length": True, "limit": 1}}},
                           {"CompilationError": {"CodeDoesNotExist": {"account_id": "!"}}}):
            error = _query_error("CONTRACT_EXECUTION_ERROR", {**self.info, "vm_error": "MethodNotFound", "error": structured})
            with self.subTest(structured=structured):
                self.assert_schema_unknown("query", params,
                    {"jsonrpc": "2.0", "id": "gate", "error": error}, "code_without_metadata")


    def test_metadata_reference_hash_uses_base64_vector_wire_type(self):
        chain = rpc.Client("https://archival.example", "replay")
        for digest in ("", "aGFzaA==", "A" * 43 + "="):
            value = {"name": "Token", "symbol": "T", "decimals": 6, "reference": "ref", "reference_hash": digest}
            response = {"jsonrpc": "2.0", "id": "gate", "result": {"block_height": HEIGHT,
                        "block_hash": rpc.base58.encode(bytes([1]) * 32), "logs": [],
                        "result": list(json.dumps(value).encode())}}
            with self.subTest(digest=digest), mock.patch.object(chain, "_exchange", return_value=response):
                self.assertEqual(chain.call_function("old.near", "ft_metadata", {}), value)


    def test_query_error_context_only_requires_protocol_account_fields(self):
        chain = rpc.Client("https://archival.example", "replay")
        block = {"block_height": HEIGHT, "block_hash": rpc.base58.encode(bytes([1]) * 32)}
        rows = [("CONTRACT_EXECUTION_ERROR", {**block, "vm_error": "MethodNotFound",
                 "error": {"MethodResolveError": "MethodNotFound"}}),
                ("UNKNOWN_ACCESS_KEY", {**block, "public_key": "ed25519:" + "1" * 32}),
                ("UNKNOWN_GAS_KEY", {**block, "public_key": "ed25519:" + "1" * 32}),
                ("NO_GLOBAL_CONTRACT_CODE", {**block, "identifier": {"account_id": "global.near"}})]
        for cause, info in rows:
            error = _query_error(cause, info)
            def exchange(url, method, params, body):
                return {"jsonrpc": "2.0", "id": "gate", **(
                    {"result": self.original(method, params)} if method == "block" else {"error": error})}
            with self.subTest(cause=cause), mock.patch.object(chain, "_exchange", side_effect=exchange):
                for client in (chain, AsOf(chain, HEIGHT)):
                    with self.assertRaises(rpc.RpcError):
                        client.call("query", {"request_type": "call_function", "account_id": "old.near",
                                              "method_name": "contract_source_metadata", "block_id": HEIGHT})


    def test_contextless_protocol_errors_and_unknown_block_references(self):
        chain = rpc.Client("https://archival.example", "replay")
        params = {"request_type": "view_account", "account_id": "old.near", "block_id": HEIGHT}
        rows = [("query", params, _query_error("NO_SYNCED_BLOCKS", None)),
                ("query", params, _query_error("UNAVAILABLE_SHARD", {"requested_shard_id": 0})),
                ("query", params, _query_error("UNKNOWN_BLOCK", {"block_reference": {"block_id": HEIGHT}})),
                ("block", {"block_id": HEIGHT}, _query_error("UNKNOWN_BLOCK", {})),
                ("block", {"block_id": HEIGHT}, _query_error("NOT_SYNCED_YET", None))]
        for method, request, error in rows:
            response = {"jsonrpc": "2.0", "id": "gate", "error": error}
            with self.subTest(method=method, error=error), mock.patch.object(chain, "_exchange", return_value=response):
                with self.assertRaises(rpc.RpcError):
                    chain.call(method, request)
                with self.assertRaises(rpc.MalformedResponse if method == "block" else rpc.RpcError):
                    AsOf(chain, HEIGHT).call(method, request)
        for reference in ({"block_id": HEIGHT + 1}, {"finality": "final"}, {"sync_checkpoint": "genesis"}):
            error = _query_error("UNKNOWN_BLOCK", {"block_reference": reference})
            with self.subTest(reference=reference):
                self.assert_schema_unknown("query", params,
                    {"jsonrpc": "2.0", "id": "gate", "error": error}, "receiver_exists")


    def test_structured_error_exemption_only_applies_to_a_typed_cause(self):
        params = {"request_type": "call_function", "account_id": "old.near", "block_id": HEIGHT,
                  "method_name": "contract_source_metadata"}
        error = _query_error("NO_CONTRACT_CODE", self.info)
        error["data"] = {"name": "CONTRACT_EXECUTION_ERROR", "info": {
            "error": {"MethodResolveError": "MethodNotFound"}}}
        self.assert_schema_unknown("query", params,
            {"jsonrpc": "2.0", "id": "gate", "error": error}, "code_without_metadata")


if __name__ == "__main__":
    unittest.main()
