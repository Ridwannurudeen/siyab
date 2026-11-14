import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gate import base58, canon, evidence, rpc, rules
from gate.token_check import check_token
from intel import feed, labels

ROOT = Path(__file__).resolve().parent.parent
RECEIVER_RPC = ROOT / "fixtures" / "receivers" / "rpc"
NOW_MS = 1794528000000
TOKEN = "token.synthetic.near"
REFERENCE = "catalog.synthetic.near"
ISSUER = "issuer.synthetic.near"
COPY = "copy.synthetic.near"
LEGACY = "legacy.synthetic.near"
FT_METADATA = {"method_name": "ft_metadata", "args_base64": "e30="}
PUBLIC_KEY = "ed25519:" + base58.encode(bytes(range(32)))
# The synthetic block 100 every fake read answers at; the strict schema needs a base58 hash of 32 bytes.
BLOCK_HASH = base58.encode(bytes([100]) * 32)
BLOCK = {"block_height": 100, "block_hash": BLOCK_HASH}
# The view_account fields the strict schema requires besides code_hash and the block context.
ACCOUNT = {"amount": "0", "locked": "0", "storage_usage": 0, "block_hash": BLOCK_HASH}


def native_usdc():
    return next(entry["token"] for entry in rules.issuers_for("mainnet") if entry["method"] == "is_blocklisted")


class TokenCheckTest(unittest.TestCase):
    def setUp(self):
        for target in ("urllib.request.urlopen", "gate.rpc._open_with_deadline", "gate.rpc._fetch"):
            patcher = mock.patch(target, side_effect=AssertionError("network is blocked"))
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("NEAR_MAINNET_RPC_URL", None)
        self.data = json.loads((ROOT / "fixtures" / "token-check" / "synthetic.json").read_text(encoding="utf-8"))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.rpc = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=self.directory)
        self.write({"block_id": 100}, {"header": {"height": 100, "timestamp": NOW_MS * 1000000, "hash": BLOCK_HASH}},
                   method="block")
        self.issuer_patch = mock.patch("gate.rules.issuers_for", return_value=self.data["issuers"])
        self.issuer_patch.start()
        self.addCleanup(self.issuer_patch.stop)
        self.store = labels.Store([labels.validate(row) for row in self.data["labels"]], "5" * 64)
        for token, values in self.data["tokens"].items():
            self.function(token, values["metadata"])
            self.account(token, values["code_hash"])
            self.keys(token, values["keys"])

    def write(self, params, result, method="query"):
        key = rpc.fixture_key(rpc.MAINNET_RPC_URL, method, params)
        value = {"synthetic": True, "url": rpc.MAINNET_RPC_URL,
                 "request": {"method": method, "params": params},
                 "response": {"jsonrpc": "2.0", "id": "gate", "result": result},
                 "fetched_at": "2026-11-13T00:00:00Z"}
        (self.directory / f"{key}.json").write_text(json.dumps(value), encoding="utf-8", newline="\n")

    def function(self, token, value):
        params = {"request_type": "call_function", "finality": "final", "account_id": token, **FT_METADATA}
        self.write(params, {"result": list(json.dumps(value).encode("utf-8")), "logs": [], **BLOCK})

    def account(self, token, code_hash, **extra):
        self.account_result(token, {"code_hash": code_hash, "block_height": 100, **extra})

    def account_result(self, token, result):
        """view_account answering result over the other fields the strict schema requires."""
        self.write({"request_type": "view_account", "finality": "final", "account_id": token},
                   {**ACCOUNT, **result})

    def keys(self, token, rows, height=100):
        """The key list at the final block and at block 100, where the account read is; the second
        answers at height, which is absent when height is None."""
        self.write({"request_type": "view_access_key_list", "finality": "final", "account_id": token},
                   {"keys": rows, **BLOCK})
        result = ({"keys": rows, "block_hash": BLOCK_HASH} if height is None
                  else {"keys": rows, "block_height": height, "block_hash": BLOCK_HASH})
        self.write({"request_type": "view_access_key_list", "block_id": 100, "account_id": token}, result)

    def remove_keys(self, token):
        self.remove(token, "view_access_key_list")
        params = {"request_type": "view_access_key_list", "block_id": 100, "account_id": token}
        (self.directory / f"{rpc.fixture_key(rpc.MAINNET_RPC_URL, 'query', params)}.json").unlink()

    def remove(self, token, request_type, **extra):
        params = {"request_type": request_type, "finality": "final", "account_id": token, **extra}
        (self.directory / f"{rpc.fixture_key(rpc.MAINNET_RPC_URL, 'query', params)}.json").unlink()

    def row(self, subject, label):
        """A synthetic catalog_token row like the fixture's first, with its own id."""
        data = dict(self.data["labels"][0], subject=subject, label=label)
        data["id"] = labels.assertion_id(data)
        return labels.validate(data)

    def add(self, *rows):
        self.store = labels.Store(list(self.store.assertions) + list(rows), self.store.sha256)

    def unlisted(self):
        self.store = labels.Store(self.store.assertions[1:], self.store.sha256)

    def run_check(self, token=TOKEN):
        return check_token(token, store=self.store, rpc=self.rpc, now_ms=NOW_MS)

    def checks(self, answer):
        return {check["id"]: check for check in json.loads(answer["evidence"])["checks"]}

    def recorded(self):
        self.issuer_patch.stop()
        self.store = labels.load()
        self.rpc = rpc.Client(rpc.MAINNET_RPC_URL, "replay", fixtures_dir=RECEIVER_RPC)

    def test_recorded_usdt_catalog_and_issuer(self):
        self.recorded()
        checks = self.checks(self.run_check("usdt.tether-token.near"))
        self.assertEqual(checks["token_in_catalog"]["status"], "pass")
        self.assertEqual(checks["issuer_known"]["status"], "pass")
        self.assertIn("get_blacklist_status", checks["issuer_known"]["detail"])

    def test_recorded_native_usdc_catalog_and_issuer(self):
        self.recorded()
        checks = self.checks(self.run_check(native_usdc()))
        self.assertEqual(checks["token_in_catalog"]["status"], "pass")
        self.assertEqual(checks["issuer_known"]["status"], "pass")
        self.assertEqual(checks["ft_metadata_sane"]["status"], "pass")
        self.assertIn("is_blocklisted", checks["issuer_known"]["detail"])

    def test_recorded_native_usdc_shared_catalog_symbol_not_signal(self):
        self.recorded()
        token = native_usdc()
        with mock.patch.object(self.rpc, "call_function", wraps=self.rpc.call_function) as reads:
            answer = self.run_check(token)
        references = [c.args[0] for c in reads.call_args_list if c.args[1] == "ft_metadata" and c.args[0] != token]
        self.assertEqual(references, ["usdt.tether-token.near"])
        self.assertNotEqual(self.checks(answer)["symbol_impersonation"]["status"], "signal")
        self.assertNotEqual(answer["outcome"], "DENY")

    def test_recorded_usdt_full_reads_allow(self):
        self.recorded()
        answer = self.run_check("usdt.tether-token.near")
        checks = self.checks(answer)
        self.assertEqual({name: check["status"] for name, check in checks.items()},
                         dict.fromkeys(("token_in_catalog", "ft_metadata_sane", "symbol_impersonation", "issuer_known",
                                        "contract_control", "incident_code"), "pass"))
        self.assertEqual((answer["outcome"], answer["action"]), ("ALLOW", "sign"))
        self.assertEqual(answer["freshness"]["block_height"], 224460142)
        self.assertEqual(checks["ft_metadata_sane"]["detail"], 'symbol "USDt", 6 decimals')
        self.assertIn("none of 0 access keys", checks["contract_control"]["detail"])
        self.assertFalse(any("no recorded response" in check["detail"] for check in checks.values()))

    def test_recorded_native_usdc_full_reads_ask_for_held_name_and_full_access_key(self):
        """Native USDC reads ASK, not ALLOW: its name "USDC" equals its own catalog symbol, which bridged
        USDC copies also hold, and the label store records no names to tell a copy from the same asset; and
        one full access key at the recorded block can replace the contract code."""
        self.recorded()
        answer = self.run_check(native_usdc())
        checks = self.checks(answer)
        self.assertEqual({name: check["status"] for name, check in checks.items()},
                         {"token_in_catalog": "pass", "ft_metadata_sane": "pass", "symbol_impersonation": "unknown",
                          "issuer_known": "pass", "contract_control": "unknown", "incident_code": "pass"})
        self.assertEqual((answer["outcome"], answer["action"]), ("ASK", "ask"))
        self.assertEqual(answer["freshness"]["block_height"], 224460146)
        self.assertIn('the name "USDC" equals its own catalog symbol', checks["symbol_impersonation"]["detail"])
        self.assertEqual(checks["contract_control"]["detail"], "1 full access key(s) can replace the contract code")
        self.assertEqual(answer["reasons"], [checks["symbol_impersonation"]["detail"],
                                             checks["contract_control"]["detail"]])
        self.assertFalse(any("no recorded response" in check["detail"] for check in checks.values()))

    def test_synthetic_catalog_token_reads_at_most_two_references(self):
        second = "second.synthetic.near"
        self.function(second, {"name": "Second Dollar", "symbol": "SUSD", "decimals": 6})
        self.add(*[self.row(f"c{index}.synthetic.near", f'symbol "S{index}"') for index in range(20)],
                 self.row(LEGACY, "legacy token entry"))
        issuers = self.data["issuers"] + [dict(self.data["issuers"][0], token=second)]
        with mock.patch("gate.rules.issuers_for", return_value=issuers), \
                mock.patch.object(self.rpc, "call_function", wraps=self.rpc.call_function) as reads:
            answer = self.run_check()
        references = [c.args[0] for c in reads.call_args_list if c.args[0] != TOKEN]
        self.assertEqual(references, [ISSUER, second])
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "pass")
        self.assertIn("none of 21 recorded catalog symbols", check["detail"])
        self.assertIn("2 issuer tokens", check["detail"])
        self.assertEqual(answer["outcome"], "ALLOW")

    def test_synthetic_listed_token_changed_symbol_usdt_signal(self):
        self.function(TOKEN, {"name": "Synthetic Token", "symbol": "USDT", "decimals": 6})
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "signal")
        self.assertIn(ISSUER, check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_listed_token_name_copies_catalog_symbol_signal(self):
        self.function(TOKEN, {"name": " cat ", "symbol": "TOK", "decimals": 6})
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "signal")
        self.assertIn(REFERENCE, check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_genuine_catalog_copy_shared_symbol_pass(self):
        self.add(self.row(COPY, 'symbol "TOK"'))
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "pass")
        self.assertIn("its symbol is not compared with 1 catalog rows", check["detail"])
        self.assertEqual(answer["outcome"], "ALLOW")

    def test_synthetic_genuine_copy_exemption_uses_normalised_symbol(self):
        self.add(self.row(COPY, 'symbol "TOK"'))
        self.function(TOKEN, {"name": "Synthetic Token", "symbol": " tok ", "decimals": 6})
        answer = self.run_check()
        self.assertEqual(self.checks(answer)["symbol_impersonation"]["status"], "pass")
        self.assertEqual(answer["outcome"], "ALLOW")

    def test_synthetic_issuer_token_outside_catalog_unknown(self):
        answer = self.run_check(ISSUER)
        checks = self.checks(answer)
        self.assertEqual(checks["token_in_catalog"]["status"], "unknown")
        self.assertEqual(checks["issuer_known"]["status"], "pass")
        self.assertEqual(checks["symbol_impersonation"]["status"], "unknown")
        self.assertIn("has no catalog row", checks["symbol_impersonation"]["detail"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_usdt_symbol_impersonation(self):
        self.unlisted()
        self.function(TOKEN, {"name": "Different Dollar", "symbol": "USDT", "decimals": 6})
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "signal")
        self.assertIn(ISSUER, check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_symbol_variants_signal(self):
        self.unlisted()
        for symbol in ("usdt", " USDT ", "\uff35\uff33\uff24\uff34", "USDT\t"):
            with self.subTest(symbol=symbol):
                self.function(TOKEN, {"name": "Different Dollar", "symbol": symbol, "decimals": 6})
                answer = self.run_check()
                check = self.checks(answer)["symbol_impersonation"]
                self.assertEqual(check["status"], "signal")
                self.assertIn(ISSUER, check["detail"])
                self.assertIn(json.dumps(symbol, ensure_ascii=False), check["detail"])
                self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_name_variant_signal(self):
        self.unlisted()
        self.function(TOKEN, {"name": "  synthetic\u3000DOLLAR ", "symbol": "OTHER", "decimals": 6})
        check = self.checks(self.run_check())["symbol_impersonation"]
        self.assertEqual(check["status"], "signal")
        self.assertIn(ISSUER, check["detail"])

    def test_synthetic_confusable_and_format_characters_unknown(self):
        for symbol in ("U\u0405DT", "US\u200bDT", "US\u00adDT", "\u200b",
                       "USDT\x00", "US\x7fDT"):
            with self.subTest(symbol=symbol):
                self.function(TOKEN, {"name": "Different Dollar", "symbol": symbol, "decimals": 6})
                answer = self.run_check()
                check = self.checks(answer)["symbol_impersonation"]
                self.assertEqual(check["status"], "unknown")
                self.assertIn(json.dumps(symbol, ensure_ascii=False), check["detail"])
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_format_character_in_name_unknown(self):
        self.function(TOKEN, {"name": "Other\u2060Token", "symbol": "OTHER", "decimals": 6})
        self.assertEqual(self.checks(self.run_check())["symbol_impersonation"]["status"], "unknown")

    def test_synthetic_unlisted_token_name_copies_catalog_symbol_signal(self):
        self.unlisted()
        self.function(TOKEN, {"name": "Cat", "symbol": "OTHER", "decimals": 6})
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "signal")
        self.assertIn(REFERENCE, check["detail"])
        self.assertIn("name", check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_issuer_reference_fields_not_normalisable_unknown(self):
        for metadata in ({"name": "Synthetic Dollar", "symbol": "U\u0405DT"},
                         {"name": "\u200b", "symbol": "USDT"},
                         {"name": "   ", "symbol": "USDT"}):
            with self.subTest(metadata=metadata):
                self.function(ISSUER, dict(metadata, decimals=6))
                answer = self.run_check()
                check = self.checks(answer)["symbol_impersonation"]
                self.assertEqual(check["status"], "unknown")
                self.assertIn(ISSUER, check["detail"])
                self.assertIn("cannot be normalised", check["detail"])
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_genuine_copy_name_equal_to_own_symbol_unknown(self):
        self.add(self.row(COPY, 'symbol "TOK"'))
        self.function(TOKEN, {"name": "TOK", "symbol": "TOK", "decimals": 6})
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "unknown")
        self.assertIn(COPY, check["detail"])
        self.assertIn("records no names", check["detail"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_name_matching_issuer_name_field_signal(self):
        self.function(TOKEN, {"name": "TOK", "symbol": "TOK", "decimals": 6})
        self.function(ISSUER, {"name": "TOK", "symbol": "USDT", "decimals": 6})
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "signal")
        self.assertIn(ISSUER, check["detail"])
        self.assertIn("name", check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_name_matching_another_own_recorded_symbol_signal(self):
        self.add(self.row(TOKEN, 'symbol "CAT"'))
        self.function(TOKEN, {"name": "CAT", "symbol": "TOK", "decimals": 6})
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "signal")
        self.assertIn(REFERENCE, check["detail"])
        self.assertIn("name", check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_held_name_gap_does_not_hide_another_signal(self):
        self.add(self.row(COPY, 'symbol "TOK"'))
        self.function(TOKEN, {"name": "TOK", "symbol": "TOK", "decimals": 6})
        code = next(iter(feed.Context(self.store).code_hashes))
        self.account(TOKEN, code)
        answer = self.run_check()
        checks = self.checks(answer)
        self.assertEqual(checks["symbol_impersonation"]["status"], "unknown")
        self.assertEqual(checks["incident_code"]["status"], "signal")
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_metadata_read_once_for_both_checks(self):
        responses = iter([{"name": "Synthetic Token", "symbol": "TOK", "decimals": 6},
                          {"name": "Different Dollar", "symbol": "USDT", "decimals": 6}])
        original = self.rpc.call_function

        def read(contract, method, args, **kwargs):
            if (contract, method) == (TOKEN, "ft_metadata"):
                self.rpc.last_source = {"provider": "synthetic", "request_sha256": None, "fetched_at": None}
                return next(responses)
            return original(contract, method, args, **kwargs)

        with mock.patch.object(self.rpc, "call_function", side_effect=read) as reads:
            answer = self.run_check()
        own = [c for c in reads.call_args_list if c.args[:2] == (TOKEN, "ft_metadata")]
        self.assertEqual(len(own), 1)
        checks = self.checks(answer)
        self.assertEqual(checks["ft_metadata_sane"]["status"], "pass")
        self.assertEqual(checks["symbol_impersonation"]["status"], "pass")
        self.assertEqual(answer["outcome"], "ALLOW")

    def test_synthetic_issuer_name_impersonation(self):
        self.unlisted()
        self.function(TOKEN, {"name": "Synthetic Dollar", "symbol": "OTHER", "decimals": 6})
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "signal")
        self.assertIn(ISSUER, check["detail"])
        self.assertIn("name", check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_catalog_row_without_symbol_not_read(self):
        self.add(self.row(LEGACY, "legacy token entry"))
        answer = self.run_check()
        self.assertEqual(self.checks(answer)["symbol_impersonation"]["status"], "pass")
        self.assertEqual(answer["outcome"], "ALLOW")
        self.unlisted()
        check = self.checks(self.run_check())["symbol_impersonation"]
        self.assertEqual(check["status"], "unknown")
        self.assertIn("1 catalog tokens record no symbol", check["detail"])

    def test_synthetic_catalog_symbol_without_reference_metadata(self):
        self.unlisted()
        self.function(TOKEN, {"name": "Other", "symbol": "CAT", "decimals": 6})
        self.remove(REFERENCE, "call_function", **FT_METADATA)
        self.remove(ISSUER, "call_function", **FT_METADATA)
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "signal")
        self.assertIn(REFERENCE, check["detail"])
        self.assertEqual(check["source"]["request_sha256"], self.store.sha256)
        self.assertIn(self.store.assertions[0].id, json.loads(answer["evidence"])["labels"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_signal_wins_over_unreadable_reference(self):
        self.unlisted()
        self.remove(ISSUER, "call_function", **FT_METADATA)
        self.function(TOKEN, {"name": "Different", "symbol": "CAT", "decimals": 6})
        self.assertEqual(self.checks(self.run_check())["symbol_impersonation"]["status"], "signal")

    def test_synthetic_unreadable_metadata_unknown(self):
        self.unlisted()
        self.remove(TOKEN, "call_function", **FT_METADATA)
        answer = self.run_check()
        checks = self.checks(answer)
        self.assertEqual(checks["ft_metadata_sane"]["status"], "unknown")
        self.assertEqual(checks["symbol_impersonation"]["status"], "unknown")
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_absent_catalog_unknown(self):
        self.unlisted()
        answer = self.run_check()
        checks = self.checks(answer)
        self.assertEqual(checks["token_in_catalog"]["status"], "unknown")
        self.assertEqual(checks["symbol_impersonation"]["status"], "unknown")
        self.assertIn("none of 1 recorded catalog symbols", checks["symbol_impersonation"]["detail"])
        self.assertIn("1 issuer tokens", checks["symbol_impersonation"]["detail"])
        self.assertIn("0 catalog tokens record no symbol", checks["symbol_impersonation"]["detail"])
        self.assertEqual(answer["outcome"], "ASK")
        self.assertNotIn("issuer_known", checks)

    def test_synthetic_superseded_catalog_unknown(self):
        rows = list(self.store.assertions)
        rows[0] = dataclasses.replace(rows[0], superseded_by=rows[1].id)
        self.store = labels.Store(rows, self.store.sha256)
        self.assertEqual(self.checks(self.run_check())["token_in_catalog"]["status"], "unknown")

    def test_synthetic_clean_checks_allow_and_answer_shape(self):
        answer = self.run_check()
        document = json.loads(answer["evidence"])
        self.assertEqual(set(answer), {"evidence", "evidence_hash", "checks", "reasons", "outcome", "action",
                                       "verdict", "signing", "freshness", "latency_ms"})
        self.assertEqual(document["subject"], {"network": "mainnet", "kind": "token", "token": TOKEN})
        self.assertEqual(answer["evidence"].encode("utf-8"), evidence.canonical_bytes(document))
        self.assertEqual(answer["evidence_hash"], evidence.evidence_hash(document))
        self.assertEqual(answer["outcome"], "ALLOW")
        self.assertEqual(document["verdict"], "ALLOW")
        self.assertEqual(answer["action"], "sign")
        self.assertEqual(answer["signing"], "never")
        self.assertIsNone(answer["verdict"])
        for field in ("issued_ms", "expires_ms", "verdict_id"):
            self.assertIsNone(document[field])
        self.assertEqual(answer["freshness"], {"block_height": 100, "timestamp_ms": NOW_MS})
        self.assertEqual(answer["latency_ms"], document["latency_ms"])
        self.assertIsInstance(answer["latency_ms"], int)
        self.assertEqual(answer["checks"], [{"name": c["id"], "status": c["status"], "detail": c["detail"]}
                                            for c in document["checks"]])
        for check in document["checks"]:
            self.assertEqual(set(check), {"id", "status", "detail", "source"})
            self.assertEqual(set(check["source"]), {"provider", "request_sha256", "fetched_at"})
        self.assertTrue(all(c["status"] == "pass" for c in document["checks"]))

    def test_synthetic_full_access_unknown_with_count(self):
        row = {"public_key": PUBLIC_KEY, "access_key": {"permission": "FullAccess", "nonce": 0}}
        other = dict(row, public_key="ed25519:" + base58.encode(bytes(range(1, 33))))
        self.keys(TOKEN, [row, other])
        answer = self.run_check()
        check = self.checks(answer)["contract_control"]
        self.assertEqual(check["status"], "unknown")
        self.assertIn("2", check["detail"])
        self.assertIn("replace", check["detail"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_function_call_key_control_pass(self):
        for allowance in ("1", None):
            with self.subTest(allowance=allowance):
                self.keys(TOKEN, [{"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {
                    "FunctionCall": {"receiver_id": TOKEN, "method_names": ["transfer"], "allowance": allowance}}}}])
                self.assertEqual(self.checks(self.run_check())["contract_control"]["status"], "pass")

    def test_synthetic_key_list_read_at_the_account_block(self):
        self.remove(TOKEN, "view_access_key_list")
        answer = self.run_check()
        self.assertEqual(self.checks(answer)["contract_control"]["status"], "pass")
        self.assertEqual(answer["freshness"]["block_height"], 100)
        self.assertEqual(answer["outcome"], "ALLOW")

    def test_synthetic_key_list_height_missing_wrong_or_stale_unknown(self):
        for height in (None, "100", True, 99, 101):
            with self.subTest(height=height):
                self.keys(TOKEN, [], height=height)
                answer = self.run_check()
                self.assertEqual(self.checks(answer)["contract_control"]["status"], "unknown")
                self.assertEqual(answer["freshness"]["block_height"], 100)
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_key_bounds(self):
        call = {"receiver_id": TOKEN, "method_names": [], "allowance": "1"}
        for nonce, allowance, status in ((canon.U64_MAX, "1", "pass"), (canon.U64_MAX + 1, "1", "unknown"),
                                         (0, str(canon.U128_MAX), "pass"), (0, str(canon.U128_MAX + 1), "unknown")):
            with self.subTest(nonce=nonce, allowance=allowance):
                self.keys(TOKEN, [{"public_key": PUBLIC_KEY, "access_key": {"nonce": nonce, "permission": {
                    "FunctionCall": dict(call, allowance=allowance)}}}])
                self.assertEqual(self.checks(self.run_check())["contract_control"]["status"], status)

    def test_synthetic_unreadable_keys_unknown(self):
        self.remove_keys(TOKEN)
        answer = self.run_check()
        self.assertEqual(self.checks(answer)["contract_control"]["status"], "unknown")
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_partial_key_records_unknown(self):
        call = {"receiver_id": TOKEN, "method_names": [], "allowance": "1"}
        good = {"nonce": 0, "permission": {"FunctionCall": call}}
        rows = (
            None, {}, {"access_key": {}},
            {"access_key": {"permission": {"FunctionCall": {}}}},
            {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {"FunctionCall": {}}}},
            {"public_key": PUBLIC_KEY, "access_key": {"permission": "FullAccess"}},
            {"public_key": PUBLIC_KEY, "access_key": {"nonce": True, "permission": "FullAccess"}},
            {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": "Unexpected"}},
            {"public_key": "ed25519:Synthetic", "access_key": good},
            {"public_key": "rsa:" + PUBLIC_KEY[len("ed25519:"):], "access_key": good},
            {"access_key": good},
            {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {
                "FunctionCall": dict(call, allowance=5)}}},
            {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {
                "FunctionCall": dict(call, receiver_id="Bad/Receiver")}}},
            {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {
                "FunctionCall": dict(call, method_names="transfer")}}},
            {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {
                "FunctionCall": dict(call, receiver_id=None)}}},
            {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {
                "FunctionCall": dict(call, receiver_id=5)}}},
            {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {
                "FunctionCall": {"receiver_id": TOKEN, "method_names": []}}}},
            {"public_key": PUBLIC_KEY, "access_key": {"nonce": 0, "permission": {
                "FunctionCall": call, "FullAccess": None}}},
        )
        for row in rows:
            with self.subTest(row=row):
                self.keys(TOKEN, [row])
                answer = self.run_check()
                self.assertEqual(self.checks(answer)["contract_control"]["status"], "unknown")
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_malformed_account_unknown(self):
        short = base58.encode(bytes([7]) * 31)
        for result in ({"code_hash": "!", "block_height": 100}, {"code_hash": "", "block_height": 100},
                       {"code_hash": short, "block_height": 100},
                       {"code_hash": self.data["tokens"][TOKEN]["code_hash"]},
                       {"code_hash": self.data["tokens"][TOKEN]["code_hash"], "block_height": "old"},
                       {"code_hash": self.data["tokens"][TOKEN]["code_hash"], "block_height": True},
                       {"code_hash": self.data["tokens"][TOKEN]["code_hash"], "block_height": -1}):
            with self.subTest(result=result):
                self.account_result(TOKEN, result)
                answer = self.run_check()
                checks = self.checks(answer)
                self.assertEqual(checks["contract_control"]["status"], "unknown")
                self.assertEqual(checks["incident_code"]["status"], "unknown")
                self.assertIsNone(answer["freshness"]["block_height"])
                self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_unreadable_account_unknown(self):
        self.remove(TOKEN, "view_account")
        answer = self.run_check()
        checks = self.checks(answer)
        self.assertEqual(checks["contract_control"]["status"], "unknown")
        self.assertEqual(checks["incident_code"]["status"], "unknown")
        self.assertIsNone(answer["freshness"]["block_height"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_incident_code_signal_wins_unknown(self):
        code, incident = next(iter(feed.Context(self.store).code_hashes.items()))
        self.account(TOKEN, code)
        self.remove_keys(TOKEN)
        answer = self.run_check()
        check = self.checks(answer)["incident_code"]
        self.assertEqual(check["status"], "signal")
        self.assertIn(code, check["detail"])
        self.assertIn(incident, check["detail"])
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_empty_or_unreadable_incident_corpus_unknown(self):
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as broken:
            (Path(broken) / "broken.json").write_text("{", encoding="utf-8")
            for directory in (Path(empty), Path(broken), Path(empty) / "absent"):
                with self.subTest(directory=directory), mock.patch("intel.feed.INCIDENTS_DIR", directory):
                    answer = self.run_check()
                    self.assertEqual(self.checks(answer)["incident_code"]["status"], "unknown")
                    self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_missing_issuer_metadata_unknown(self):
        self.remove(ISSUER, "call_function", **FT_METADATA)
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "unknown")
        self.assertIn(ISSUER, check["detail"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_malformed_issuer_metadata_unknown(self):
        self.function(ISSUER, {"symbol": "USDT"})
        answer = self.run_check()
        check = self.checks(answer)["symbol_impersonation"]
        self.assertEqual(check["status"], "unknown")
        self.assertIn(ISSUER, check["detail"])
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_no_code_omits_contract_control(self):
        self.account(TOKEN, rpc.EMPTY_CODE_HASH)
        checks = self.checks(self.run_check())
        self.assertNotIn("contract_control", checks)
        self.assertEqual(checks["incident_code"]["status"], "pass")

    def test_synthetic_invalid_metadata_uses_existing_helper(self):
        self.function(TOKEN, {"name": "Token", "symbol": "TOK", "decimals": 25})
        with mock.patch("gate.rules._ft_metadata_check", wraps=rules._ft_metadata_check) as helper:
            answer = self.run_check()
        helper.assert_called_once()
        self.assertEqual(helper.call_args.args[1], TOKEN)
        self.assertEqual(self.checks(answer)["ft_metadata_sane"]["status"], "signal")
        self.assertEqual(answer["outcome"], "DENY")

    def test_synthetic_boolean_decimals_rejected_by_client_unknown(self):
        self.function(TOKEN, {"name": "Token", "symbol": "TOK", "decimals": True})
        answer = self.run_check()
        checks = self.checks(answer)
        self.assertEqual(checks["ft_metadata_sane"]["status"], "unknown")
        self.assertEqual(checks["symbol_impersonation"]["status"], "unknown")
        others = {key: check["status"] for key, check in checks.items()
                  if key not in ("ft_metadata_sane", "symbol_impersonation")}
        self.assertEqual(set(others), {"token_in_catalog", "contract_control", "incident_code"})
        self.assertTrue(all(status == "pass" for status in others.values()), others)
        self.assertEqual(answer["outcome"], "ASK")

    def test_synthetic_testnet_and_invalid_ids_refused_before_reads(self):
        with mock.patch.object(self.rpc, "call", side_effect=AssertionError("unexpected read")):
            for token in ("token.testnet", "bad/token", ""):
                with self.subTest(token=token), self.assertRaises(ValueError):
                    self.run_check(token)

    def test_synthetic_non_mainnet_endpoints_refused_before_reads(self):
        endpoints = [rpc.Client(url, "replay", fixtures_dir=self.directory) for url in (
            rpc.TESTNET_RPC_URL, rpc.TESTNET_RPC_URL + "/proxy", rpc.MAINNET_RPC_URL + "/proxy",
            rpc.ARCHIVAL_RPC_URL, rpc.ARCHIVAL_TESTNET_RPC_URL, "https://mainnet.example")]
        endpoints.append(rpc.Client(rpc.MAINNET_RPC_URL, "replay", kind="http", fixtures_dir=self.directory))
        for client in endpoints:
            with self.subTest(url=client.url, kind=client.kind), \
                    mock.patch.object(client, "call", side_effect=AssertionError("unexpected read")), \
                    self.assertRaises(ValueError):
                check_token(TOKEN, store=self.store, rpc=client, now_ms=NOW_MS)

    def test_synthetic_configured_endpoint_not_trusted(self):
        for url in ("https://mainnet.example", rpc.TESTNET_RPC_URL + "/proxy", rpc.TESTNET_RPC_URL):
            with self.subTest(url=url):
                os.environ["NEAR_MAINNET_RPC_URL"] = url
                client = rpc.Client(url, "replay", fixtures_dir=self.directory)
                with mock.patch.object(client, "call", side_effect=AssertionError("unexpected read")), \
                        self.assertRaises(ValueError):
                    check_token(TOKEN, store=self.store, rpc=client, now_ms=NOW_MS)


if __name__ == "__main__":
    unittest.main()
