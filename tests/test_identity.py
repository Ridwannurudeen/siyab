import copy
import dataclasses
import unittest
from unittest import mock

from gate import base58, rpc
from intel import accounts, identity, labels


class Provider:
    def __init__(self):
        self.requests = []
        self.answers = {
            "view_account": {"code_hash": base58.encode(bytes([7]) * 32), "block_height": 123},
            "ft_metadata": {"spec": "ft-1.0.0", "name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24},
            "contract_source_metadata": {"version": "v1", "link": "https://github.com/near/core-contracts/tree/v1",
                                         "standards": [{"standard": "nep141", "version": "1.0.0"}]},
            "validators": {"epoch_start_height": 120, "epoch_height": 8,
                           "current_validators": [{"account_id": "pool.near", "stake": "12345678901234567890123456789"}]},
        }

    def client(self, url, mode):
        provider = self

        class Client:
            last_source = None
            last_block_height = None

            def read(self, method, args):
                provider.requests.append((method, args, rpc._REQUEST_DEADLINE.get(), url, mode))
                self.last_source = {"provider": "free.rpc.fastnear.com", "request_sha256": str(len(provider.requests)) * 64,
                                    "fetched_at": "2026-11-10T00:00:00Z"}
                self.last_block_height = 123 if method != "validators" else None
                answer = provider.answers[method]
                if isinstance(answer, Exception):
                    raise answer
                return copy.deepcopy(answer)

            def view_account(self, account):
                return self.read("view_account", account)

            def call_function(self, account, method, args, *, block_id=None):
                return self.read(method, {"account_id": account, "args": args, "block_id": block_id})

            def call(self, method, params):
                return self.read(method, params)

        return Client()


class IdentityTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict("os.environ", {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.provider = Provider()
        self.data = identity.IdentityData(labels.Store([], "b" * 64), client_factory=self.provider.client,
                                          clock=lambda: 1794268800)

    def test_source_backed_metadata_is_pinned_and_never_verified_ownership(self):
        value = self.data.inspect("wrap.near")
        self.assertEqual(value["observed_at"], "2026-11-10T00:00:00Z")
        self.assertEqual(value["contract"]["status"], "deployed")
        self.assertEqual(value["contract"]["block_height"], "123")
        self.assertEqual(value["token_metadata"]["provenance"], "contract_reported")
        self.assertEqual(value["contract_source"]["provenance"], "contract_reported")
        self.assertEqual(value["validator"]["status"], "not_current")
        self.assertEqual(len(value["sources"]), 4)
        self.assertNotEqual(value["sources"][0]["request_sha256"], value["sources"][1]["request_sha256"])
        self.assertEqual(self.provider.requests[1][1]["block_id"], 123)
        self.assertEqual(self.provider.requests[-1][1], [None])
        self.assertEqual(len({row[2] for row in self.provider.requests}), 1)
        self.assertTrue(all(row[-1] == "off" for row in self.provider.requests))
        self.assertNotIn("owner", value)

    def test_current_validator_stake_and_epoch_are_exact(self):
        value = self.data.inspect("pool.near")["validator"]
        self.assertEqual(value["status"], "current")
        self.assertEqual(value["stake_yocto"], "12345678901234567890123456789")
        self.assertEqual(value["epoch_start_height"], "120")

    def test_failed_or_malformed_reads_are_unknown_never_absence(self):
        for failure in (rpc.MalformedResponse("bad"), rpc.HttpError(429, rpc.MAINNET_RPC_URL),
                        {"current_validators": []},
                        {"epoch_start_height": 120, "current_validators": [{"account_id": "bad!", "stake": "1"}]}):
            self.provider.answers["validators"] = failure
            self.assertEqual(self.data.inspect("pool.near")["validator"]["status"], "unknown")
        self.provider.answers["view_account"] = rpc.TransportError("unavailable")
        value = self.data.inspect("pool.near")
        self.assertEqual(value["contract"]["status"], "unknown")
        self.assertIsNone(value["token_metadata"])

    def test_noncontract_skips_optional_methods(self):
        self.provider.answers["view_account"]["code_hash"] = rpc.EMPTY_CODE_HASH
        value = self.data.inspect("alice.near")
        self.assertEqual(value["contract"]["status"], "none")
        self.assertEqual([row[0] for row in self.provider.requests], ["view_account", "validators"])

    def test_strict_metadata_types_lengths_and_https_source_links(self):
        for metadata in ({"spec": "ft-1.0.0", "name": "x", "symbol": "X", "decimals": True},
                         {"spec": "ft-1.0.0", "name": "x" * 257, "symbol": "X", "decimals": 24},
                         {"name": "x", "symbol": "X", "decimals": 24}):
            self.provider.answers["ft_metadata"] = metadata
            self.assertIsNone(self.data.inspect("wrap.near")["token_metadata"])
        for link in ("javascript:alert(1)", "http://example.com", "https://user:pass@example.com", "https://example.com/\n"):
            self.provider.answers["contract_source_metadata"]["link"] = link
            self.assertIsNone(self.data.inspect("wrap.near")["contract_source"])

    def test_untrusted_text_is_data_and_icons_are_never_emitted(self):
        self.provider.answers["ft_metadata"].update(name="<script>alert(1)</script>", icon="data:image/svg+xml,evil")
        value = self.data.inspect("wrap.near")["token_metadata"]
        self.assertEqual(value["name"], "<script>alert(1)</script>")
        self.assertNotIn("icon", value)

    def test_optional_method_errors_and_timeout_behavior(self):
        self.provider.answers["ft_metadata"] = rpc.RpcError({"cause": {"name": "CONTRACT_EXECUTION_ERROR"}})
        self.assertIsNone(self.data.inspect("alice.near")["token_metadata"])
        self.provider.answers["ft_metadata"] = TimeoutError("deadline")
        with self.assertRaises(TimeoutError):
            self.data.inspect("alice.near")

    def test_mainnet_only_and_no_suffix_inference(self):
        for account in ("alice.testnet", "testnet", "bad!", "UPPER.near"):
            with self.assertRaises(accounts.AccountInputError):
                self.data.inspect(account)
        value = self.data.inspect("fake.binance.near")
        self.assertEqual(value["registry_entities"], [])
        self.assertEqual(value["assertions"], [])

    def test_registry_and_assertions_keep_original_dates_sources(self):
        row = labels.Assertion("a" * 64, "Registry", "alice.near", "near", "Recorded association", "organisation",
                               "official_source", {"url": "https://example.com", "content_sha256": "c" * 64,
                                                   "fetched_at": "2025-01-01T00:00:00Z"},
                               "2025-01-01T00:00:00Z", 1.0, None, None, "Historical registry record")
        self.data.store = labels.Store([row, dataclasses.replace(row, id="d" * 64, superseded_by=row.id)], "b" * 64)
        entity = {"id": "registry-alice", "name": "Registry", "kind": "dao", "members": ["alice.near"],
                  "source": row.source, "observed_at": row.observed_at}
        with mock.patch.object(identity, "_registry_entities", return_value=([entity], False)):
            value = self.data.inspect("alice.near")
        self.assertEqual(value["registry_entities"], [entity])
        self.assertEqual(len(value["assertions"]), 1)
        self.assertEqual(value["assertions"][0]["observed_at"], "2025-01-01T00:00:00Z")
        self.assertEqual(value["assertions"][0]["source"], row.source)

    def test_oversized_validator_list_is_unknown(self):
        self.provider.answers["validators"]["current_validators"] *= 1001
        self.assertEqual(self.data.inspect("pool.near")["validator"]["status"], "unknown")

    def test_metadata_read_cannot_claim_a_different_pinned_block(self):
        original = self.provider.client

        def factory(url, mode):
            client = original(url, mode)
            call_function = client.call_function

            def call(*args, **kwargs):
                value = call_function(*args, **kwargs)
                client.last_block_height = 124
                return value

            client.call_function = call
            return client

        self.data.client_factory = factory
        value = self.data.inspect("wrap.near")
        self.assertIsNone(value["token_metadata"])
        self.assertIsNone(value["contract_source"])

    def test_failure_before_response_does_not_reuse_previous_read_source(self):
        original = self.provider.client

        def factory(url, mode):
            client = original(url, mode)

            def fail(*args):
                raise rpc.TransportError("no response")

            client.call = fail
            return client

        self.data.client_factory = factory
        value = self.data.inspect("wrap.near")
        self.assertIsNone(value["validator"]["source"])


if __name__ == "__main__":
    unittest.main()
