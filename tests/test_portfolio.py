import copy
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal, localcontext
from pathlib import Path
from unittest import mock

from gate import rpc
from intel import portfolio


NOW = 1794321142


class Reader:
    def __init__(self):
        self.native = {
            "account_id": "alice.near",
            "network": "mainnet",
            "exists": True,
            "native": {
                "amount_yocto": "2000000000000000000000001",
                "locked_yocto": "1000000000000000000000000",
                "block_height": "100",
                "storage_usage": 100,
            },
            "source": {"provider": "rpc"},
        }
        self.tokens = [
            {
                "contract_id": "wrap.near",
                "balance": "1000000000000000000000000",
                "balance_status": "indexed",
                "metadata": {"name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24},
                "metadata_source": {"provider": "rpc"},
                "last_update_block_height": "99",
            }
        ]
        self.calls = []

    def profile(self, account):
        return copy.deepcopy(self.native)

    def holdings(self, account, offset=0, limit=20, *, _inventory=None):
        self.calls.append((offset, limit))
        return {
            "account_id": account,
            "network": "mainnet",
            "items": copy.deepcopy(self.tokens[offset : offset + limit]),
            "pagination": {
                "total": len(self.tokens),
                "next_offset": offset + limit
                if offset + limit < len(self.tokens)
                else None,
            },
            "source": {"provider": "inventory"},
        }


class Providers:
    def __init__(self):
        self.calls = []
        self.prices = {
            "coingecko:near": {"price": "2", "timestamp": NOW, "confidence": 0.99},
            "near:wrap.near": {
                "price": "2.5",
                "timestamp": NOW,
                "confidence": 0.99,
                "decimals": 24,
            },
        }
        self.header = {
            "height": 50,
            "timestamp": (NOW - 86400) * 1000000000,
            "hash": "blockhash",
        }
        self.balance = {
            "amount": "1000000000000000000000000",
            "locked": "0",
            "storage_usage": 10,
            "block_height": 50,
            "block_hash": "blockhash",
            "code_hash": "codehash",
        }
        self.views = []
        self.final_height = 100
        self.final_error = None
        self.failed_method = None
        self.metadata = {"name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24}

    def factory(self, url, mode, **options):
        parent = self

        class Client:
            last_source = {
                "provider": "test-provider",
                "request_sha256": "a" * 64,
                "fetched_at": "2026-11-10T00:00:00Z",
            }
            last_block_height = parent.header["height"]

            def get(self, path, **kwargs):
                parent.calls.append((url, path, kwargs))
                return {"coins": copy.deepcopy(parent.prices)}

            def block_at(self, height):
                parent.views.append(("block", height))
                return {"header": copy.deepcopy(parent.header)}

            def final_block(self):
                parent.views.append(("final",))
                if parent.final_error is not None:
                    raise parent.final_error
                return {"header": {**parent.header, "height": parent.final_height}}

            def view_account_at(self, account, height):
                parent.views.append(("native", account, height))
                return copy.deepcopy(parent.balance)

            def call_function(self, contract, method, args, *, block_id=None):
                parent.views.append((method, contract, args, block_id))
                if method == parent.failed_method:
                    raise TimeoutError("deadline")
                if method == "ft_metadata":
                    return copy.deepcopy(parent.metadata)
                return "5000000000000000000000000"

        return Client()


class PortfolioTest(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict("os.environ", {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.reader, self.provider = Reader(), Providers()
        self.data = portfolio.PortfolioData(
            self.reader, client_factory=self.provider.factory, clock=lambda: NOW
        )

    def test_exact_native_locked_and_wrapped_values_are_counted_once(self):
        result = self.data.inspect("alice.near")
        self.assertEqual(
            result["assets"][0]["balance_base_units"], "3000000000000000000000001"
        )
        self.assertEqual(result["assets"][0]["value_usd"], "6.000000000000000000000002")
        self.assertEqual(result["assets"][1]["value_usd"], "2.5")
        self.assertEqual(
            result["summary"]["total_value_usd"], "8.500000000000000000000002"
        )
        self.assertEqual(result["summary"]["priced_assets"], 2)
        self.assertFalse(result["coverage"]["history_complete"])
        self.assertIn("staking", result["coverage"]["excluded"])
        self.assertEqual(len(self.provider.calls), 1)

    def test_unknown_price_is_not_zero_and_subtotal_is_not_total(self):
        self.provider.prices.pop("near:wrap.near")
        result = self.data.inspect("alice.near")
        self.assertIsNone(result["assets"][1]["value_usd"])
        self.assertEqual(result["assets"][1]["price"]["status"], "missing")
        self.assertIsNone(result["summary"]["total_value_usd"])
        self.assertEqual(
            result["summary"]["known_value_usd"], "6.000000000000000000000002"
        )

    def test_unusable_price_fields_and_decimals_are_explicit(self):
        for changes, status in (
            ({"timestamp": NOW - 3601}, "stale"),
            ({"timestamp": NOW + 61}, "future"),
            ({"confidence": 0.1}, "low_confidence"),
            ({"price": "NaN"}, "invalid"),
            ({"decimals": 6}, "decimals_mismatch"),
        ):
            with self.subTest(status=status):
                self.provider.prices["near:wrap.near"] = {
                    "price": "2",
                    "timestamp": NOW,
                    "confidence": 0.99,
                    "decimals": 24,
                    **changes,
                }
                result = self.data.inspect("alice.near")
                self.assertEqual(result["assets"][1]["price"]["status"], status)
                self.assertIsNone(result["assets"][1]["value_usd"])

    def test_bounded_inventory_preserves_partial_coverage(self):
        self.reader.tokens = [
            dict(self.reader.tokens[0], contract_id=f"token{i}.near") for i in range(22)
        ]
        result = self.data.inspect("alice.near", max_tokens=20)
        self.assertEqual(len(result["assets"]), 21)
        self.assertFalse(result["coverage"]["inventory_complete"])
        self.assertEqual(result["coverage"]["tokens_total"], 22)
        self.assertIsNone(result["summary"]["total_value_usd"])

    def test_missing_metadata_does_not_infer_decimals_from_symbol(self):
        self.reader.tokens[0]["metadata"] = None
        result = self.data.inspect("alice.near")
        self.assertIsNone(result["assets"][1]["balance"])
        self.assertIsNone(result["assets"][1]["value_usd"])
        self.assertEqual(result["assets"][1]["status"], "decimals_unavailable")

    def test_historical_balances_and_metadata_are_pinned_to_real_block(self):
        for price in self.provider.prices.values():
            price["timestamp"] = NOW - 86400
        result = self.data.historical("alice.near", 50, ["wrap.near"])
        self.assertEqual(result["kind"], "historical")
        self.assertEqual(result["assets"][1]["balance"], "5")
        self.assertEqual(result["summary"]["known_value_usd"], "14.5")
        self.assertTrue(
            all(
                row[-1] == 50 for row in self.provider.views if row[0].startswith("ft_")
            )
        )
        self.assertEqual(self.reader.calls, [])
        self.assertIn("/prices/historical/", self.provider.calls[0][1])
        self.assertFalse(result["coverage"]["inventory_complete"])

    def test_wrong_historical_block_and_future_timestamp_are_refused(self):
        self.provider.header["height"] = 51
        with self.assertRaises(portfolio.PortfolioReadError):
            self.data.historical("alice.near", 50)
        self.provider.header.update(height=50, timestamp=(NOW + 3600) * 1000000000)
        with self.assertRaises(portfolio.PortfolioReadError):
            self.data.historical("alice.near", 50)

    def test_archival_account_hash_must_match_the_block_header(self):
        self.provider.balance["block_hash"] = "different-block"
        with self.assertRaises(portfolio.PortfolioReadError):
            self.data.historical("alice.near", 50)

    def test_historical_price_cannot_come_after_the_block(self):
        target = NOW - 86400
        for delta, expected in (
            (103, "future"),
            (1, "future"),
            (0, "known"),
            (-3600, "known"),
            (-3601, "stale"),
        ):
            with self.subTest(delta=delta):
                for price in self.provider.prices.values():
                    price["timestamp"] = target + delta
                data = portfolio.PortfolioData(
                    self.reader, client_factory=self.provider.factory, clock=lambda: NOW
                )
                result = data.historical("alice.near", 50)
                self.assertEqual(result["assets"][0]["price"]["status"], expected)
                if expected != "known":
                    self.assertIsNone(result["summary"]["known_value_usd"])

    def test_partial_provider_failure_preserves_unknowns_and_never_fakes_total(self):
        with mock.patch.object(
            self.reader, "holdings", side_effect=TimeoutError("deadline")
        ):
            result = self.data.inspect("alice.near")
        self.assertFalse(result["coverage"]["inventory_complete"])
        self.assertIsNone(result["summary"]["total_value_usd"])

    def test_price_provider_failure_keeps_all_values_unknown(self):
        with mock.patch.object(self.data, "client_factory") as factory:
            factory.return_value.get.side_effect = rpc.HttpError(
                503, "https://coins.llama.fi"
            )
            result = self.data.inspect("alice.near")
        self.assertIsNone(result["summary"]["known_value_usd"])
        self.assertIsNone(result["summary"]["total_value_usd"])
        self.assertTrue(
            all(
                asset["price"]["status"] == "provider_unavailable"
                for asset in result["assets"]
            )
        )

    def test_balance_reads_reserve_time_for_the_price_request(self):
        deadlines = {}
        original_holdings = self.reader.holdings
        original_factory = self.provider.factory

        def holdings(*args, **kwargs):
            deadlines["balances"] = rpc._REQUEST_DEADLINE.get()
            return original_holdings(*args, **kwargs)

        def factory(*args, **kwargs):
            client = original_factory(*args, **kwargs)
            original_get = client.get

            def get(*arguments, **options):
                deadlines["prices"] = rpc._REQUEST_DEADLINE.get()
                return original_get(*arguments, **options)

            client.get = get
            return client

        self.reader.holdings = holdings
        self.data.client_factory = factory
        self.data.inspect("alice.near")
        self.assertGreaterEqual(deadlines["prices"] - deadlines["balances"], 5)

    def test_valuation_preserves_all_validated_decimal_digits(self):
        self.provider.prices["coingecko:near"]["price"] = "1." + "1234567890" * 10
        result = self.data.inspect("alice.near")
        with localcontext() as context:
            context.prec = 1024
            expected = Decimal(result["assets"][0]["balance"]) * Decimal(
                self.provider.prices["coingecko:near"]["price"]
            )
        self.assertEqual(Decimal(result["assets"][0]["value_usd"]), expected)

    def test_historical_sources_cover_block_time_balance_and_metadata(self):
        for price in self.provider.prices.values():
            price["timestamp"] = NOW - 86400
        result = self.data.historical("alice.near", 50, ["wrap.near"])
        self.assertEqual(result["sources"][0]["read_kind"], "archival_block")
        self.assertEqual(
            result["assets"][1]["details"]["metadata_source"]["read_kind"],
            "archival_metadata",
        )

    def test_a_ticker_name_never_maps_another_contract_to_a_known_price(self):
        self.reader.tokens[0]["contract_id"] = "unrelated.near"
        result = self.data.inspect("alice.near")
        self.assertIsNone(result["assets"][1]["value_usd"])
        self.assertEqual(result["assets"][1]["price"]["status"], "missing")

    def test_current_token_maximum_is_40(self):
        self.assertEqual(portfolio.MAX_TOKENS, 40)
        with self.assertRaisesRegex(portfolio.PortfolioInputError, "1 to 40"):
            self.data.inspect("alice.near", max_tokens=41)
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.provider.calls, [])

    def test_token_limit_refusal_uses_the_current_bound(self):
        with mock.patch.object(portfolio, "MAX_TOKENS", 2):
            with self.assertRaisesRegex(portfolio.PortfolioInputError, "1 to 2"):
                self.data.inspect("alice.near", max_tokens=3)
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(self.provider.calls, [])

    def test_price_cache_hit_preserves_the_recently_used_coin(self):
        token = copy.deepcopy(self.reader.tokens[0])
        self.provider.prices["near:other.near"] = dict(
            self.provider.prices["near:wrap.near"]
        )
        with mock.patch.object(portfolio, "MAX_PRICE_CACHE_ENTRIES", 2):
            self.data.inspect("alice.near")
            self.reader.tokens = []
            self.data.inspect("alice.near")
            self.reader.tokens = [dict(token, contract_id="other.near")]
            self.data.inspect("alice.near")
            self.reader.tokens = []
            self.data.inspect("alice.near")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.provider.calls[-1][1], "/prices/current/near:other.near")

    def test_price_cache_reuses_coins_until_60_seconds(self):
        clock = [NOW]
        self.data.clock = lambda: clock[0]
        first = self.data.inspect("alice.near")
        first["assets"][0]["price"]["usd"] = "999"
        clock[0] += 59
        repeat = self.data.inspect("alice.near")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(repeat["assets"][0]["price"]["usd"], "2")
        clock[0] += 1
        self.data.inspect("alice.near")
        self.assertEqual(len(self.provider.calls), 2)

    def test_price_cache_fetches_only_missing_coins_and_does_not_cache_failure(self):
        self.provider.prices.pop("near:wrap.near")
        first = self.data.inspect("alice.near")
        self.assertIsNone(first["assets"][1]["value_usd"])
        self.provider.prices["near:wrap.near"] = {
            "price": "3", "timestamp": NOW, "confidence": 0.99, "decimals": 24
        }
        self.provider.prices["coingecko:near"]["price"] = "99"
        repeat = self.data.inspect("alice.near")
        self.assertEqual(repeat["assets"][0]["price"]["usd"], "2")
        self.assertEqual(self.provider.calls[-1][1], "/prices/current/near:wrap.near")
        self.assertEqual(repeat["assets"][1]["price"]["usd"], "3")
        self.data.inspect("alice.near")
        self.assertEqual(len(self.provider.calls), 2)

    def test_failed_price_request_is_not_cached(self):
        with mock.patch.object(self.data, "client_factory") as factory:
            factory.return_value.get.side_effect = TimeoutError("deadline")
            first = self.data.inspect("alice.near")
        self.assertIsNone(first["summary"]["known_value_usd"])
        self.assertIsNotNone(
            self.data.inspect("alice.near")["summary"]["total_value_usd"]
        )
        self.assertEqual(len(self.provider.calls), 1)

    def test_historical_cache_uses_account_block_and_requested_contract_order(self):
        for price in self.provider.prices.values():
            price["timestamp"] = NOW - 86400
        self.provider.prices["near:other.near"] = dict(
            self.provider.prices["near:wrap.near"]
        )
        first = self.data.historical("alice.near", 50, ["wrap.near", "other.near"])
        calls, views = len(self.provider.calls), len(self.provider.views)
        first["assets"][0]["balance"] = "999"
        self.data.clock = lambda: NOW + 86400
        repeat = self.data.historical("alice.near", 50, ["wrap.near", "other.near"])
        self.assertEqual(repeat["assets"][0]["balance"], "1")
        self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(len(self.provider.views), views)
        reordered = self.data.historical("alice.near", 50, ["other.near", "wrap.near"])
        self.assertEqual(
            reordered["coverage"]["selected_contracts"], ["other.near", "wrap.near"]
        )
        self.assertGreater(len(self.provider.calls), calls)
        self.assertGreater(len(self.provider.views), views)
        views = len(self.provider.views)
        self.data.historical("alice.near", 50, ["other.near", "wrap.near"])
        self.assertEqual(len(self.provider.views), views)
        self.data.historical("bob.near", 50, ["wrap.near", "other.near"])
        self.assertGreater(len(self.provider.views), views)
        views = len(self.provider.views)
        self.data.historical("alice.near", 50, ["wrap.near"])
        self.assertGreater(len(self.provider.views), views)
        views = len(self.provider.views)
        self.provider.header["height"] = self.provider.balance["block_height"] = 51
        self.data.historical("alice.near", 51, ["wrap.near"])
        self.assertGreater(len(self.provider.views), views)

    def test_historical_failed_and_unknown_reads_are_not_cached(self):
        for failure in ("balance", "metadata", "price", "block"):
            with self.subTest(failure=failure):
                provider = Providers()
                for price in provider.prices.values():
                    price["timestamp"] = NOW - 86400
                data = portfolio.PortfolioData(
                    self.reader, client_factory=provider.factory, clock=lambda: NOW
                )
                if failure == "balance":
                    provider.failed_method = "ft_balance_of"
                elif failure == "metadata":
                    provider.metadata = None
                elif failure == "price":
                    provider.prices.pop("near:wrap.near")
                else:
                    provider.header["height"] = 51
                if failure == "block":
                    with self.assertRaises(portfolio.PortfolioReadError):
                        data.historical("alice.near", 50, ["wrap.near"])
                    provider.header["height"] = 50
                else:
                    first = data.historical("alice.near", 50, ["wrap.near"])
                    self.assertIsNone(first["assets"][1]["value_usd"])
                provider.failed_method = None
                provider.metadata = {
                    "name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24
                }
                provider.prices["near:wrap.near"] = dict(
                    provider.prices["coingecko:near"]
                )
                views = len(provider.views)
                repeat = data.historical("alice.near", 50, ["wrap.near"])
                self.assertGreater(len(provider.views), views)
                self.assertEqual(repeat["assets"][1]["status"], "priced")

    def test_historical_contract_order_and_snapshot_hash_follow_request_order(self):
        for price in self.provider.prices.values():
            price["timestamp"] = NOW - 86400
        self.provider.prices["near:other.near"] = dict(
            self.provider.prices["near:wrap.near"]
        )
        result = self.data.historical("alice.near", 50, ["wrap.near", "other.near"])
        self.assertEqual(
            [asset["asset_id"] for asset in result["assets"]],
            ["native:near", "nep141:wrap.near", "nep141:other.near"],
        )
        self.assertEqual(
            result["coverage"]["selected_contracts"], ["wrap.near", "other.near"]
        )
        fresh = portfolio.PortfolioData(
            self.reader, client_factory=self.provider.factory, clock=lambda: NOW
        ).historical("alice.near", 50, ["wrap.near", "other.near"])
        self.assertEqual(
            hashlib.sha256(portfolio._body(result)).digest(),
            hashlib.sha256(portfolio._body(fresh)).digest(),
        )
        reordered = portfolio.PortfolioData(
            self.reader, client_factory=self.provider.factory, clock=lambda: NOW
        ).historical("alice.near", 50, ["other.near", "wrap.near"])
        self.assertNotEqual(
            hashlib.sha256(portfolio._body(result)).digest(),
            hashlib.sha256(portfolio._body(reordered)).digest(),
        )

    def test_historical_cache_hit_preserves_the_recently_used_account(self):
        for price in self.provider.prices.values():
            price["timestamp"] = NOW - 86400
        with mock.patch.object(portfolio, "MAX_HISTORICAL_CACHE_ENTRIES", 2):
            self.data.historical("alice.near", 50)
            self.data.historical("bob.near", 50)
            self.data.historical("alice.near", 50)
            self.data.historical("carol.near", 50)
            views = len(self.provider.views)
            self.data.historical("alice.near", 50)
            self.assertEqual(len(self.provider.views), views)
            self.data.historical("bob.near", 50)
            self.assertGreater(len(self.provider.views), views)

    def test_historical_cache_waits_for_the_final_head_to_reach_the_block(self):
        for price in self.provider.prices.values():
            price["timestamp"] = NOW - 86400
        self.provider.final_height = 49
        self.data.historical("alice.near", 50)
        self.assertEqual(self.provider.views[:2], [("final",), ("block", 50)])
        views = len(self.provider.views)
        self.provider.final_height = 50
        self.data.historical("alice.near", 50)
        self.assertGreater(len(self.provider.views), views)
        views = len(self.provider.views)
        self.data.historical("alice.near", 50)
        self.assertEqual(len(self.provider.views), views)

    def test_historical_unknown_final_head_is_not_cached(self):
        for final_height, error in (
            (None, None),
            (True, None),
            (100, rpc.MalformedResponse("missing final head")),
            (100, TimeoutError("deadline")),
        ):
            with self.subTest(final_height=final_height, error=error):
                provider = Providers()
                provider.final_height, provider.final_error = final_height, error
                for price in provider.prices.values():
                    price["timestamp"] = NOW - 86400
                data = portfolio.PortfolioData(
                    self.reader, client_factory=provider.factory, clock=lambda: NOW
                )
                data.historical("alice.near", 50)
                views = len(provider.views)
                data.historical("alice.near", 50)
                self.assertGreater(len(provider.views), views)

    def test_historical_unknown_block_hash_is_not_cached(self):
        for block_hash in (None, ""):
            with self.subTest(block_hash=block_hash):
                provider = Providers()
                provider.header["hash"] = provider.balance["block_hash"] = block_hash
                for price in provider.prices.values():
                    price["timestamp"] = NOW - 86400
                data = portfolio.PortfolioData(
                    self.reader, client_factory=provider.factory, clock=lambda: NOW
                )
                data.historical("alice.near", 50)
                views = len(provider.views)
                provider.header["hash"] = provider.balance["block_hash"] = "blockhash"
                data.historical("alice.near", 50)
                self.assertGreater(len(provider.views), views)

    def test_caches_evict_oldest_entries_at_their_bounds(self):
        with mock.patch.object(portfolio, "MAX_PRICE_CACHE_ENTRIES", 1):
            self.data.inspect("alice.near")
            self.data.inspect("alice.near")
        self.assertEqual(len(self.provider.calls), 2)
        self.provider.calls.clear()
        for price in self.provider.prices.values():
            price["timestamp"] = NOW - 86400
        with mock.patch.object(portfolio, "MAX_HISTORICAL_CACHE_ENTRIES", 1):
            self.data.historical("alice.near", 50)
            self.data.historical("bob.near", 50)
            views = len(self.provider.views)
            self.data.historical("alice.near", 50)
        self.assertGreater(len(self.provider.views), views)

    def test_invalid_accounts_and_bounds_are_rejected_before_read(self):
        for account in ("foo.testnet", "testnet", "bad/name"):
            with self.assertRaises(portfolio.PortfolioInputError):
                self.data.inspect(account)
        for maximum in (0, 41, True):
            with self.assertRaises(portfolio.PortfolioInputError):
                self.data.inspect("alice.near", max_tokens=maximum)


class PortfolioHistoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "portfolio.sqlite3"
        self.clock = [NOW]
        self.store = portfolio.PortfolioHistory(self.path, clock=lambda: self.clock[0])
        self.payload = portfolio.PortfolioData(
            Reader(), client_factory=Providers().factory, clock=lambda: NOW
        ).inspect("alice.near")

    def current(self, amount):
        reader = Reader()
        reader.native["native"]["amount_yocto"] = amount
        return portfolio.PortfolioData(
            reader, client_factory=Providers().factory, clock=lambda: NOW
        ).inspect("alice.near")

    def test_retained_61_asset_observation_lists_after_token_cap_drops_to_40(self):
        reader = Reader()
        reader.tokens = [
            dict(reader.tokens[0], contract_id=f"token{i:02}.near") for i in range(60)
        ]
        with mock.patch.object(portfolio, "MAX_TOKENS", 100):
            payload = portfolio.PortfolioData(
                reader, client_factory=Providers().factory, clock=lambda: NOW
            ).inspect("alice.near", max_tokens=100)
            saved = self.store.save(payload)
        self.assertEqual(portfolio.MAX_TOKENS, 40)
        restarted = portfolio.PortfolioHistory(self.path)
        records = restarted.list_records("alice.near")
        self.assertEqual(records["items"][0]["id"], saved["id"])
        self.assertEqual(records["items"][0]["assets_count"], 61)
        self.assertEqual(json.loads(restarted.get_bytes(saved["id"])), payload)

    def test_current_observation_cannot_be_relabeled_as_archival_history(self):
        forged = copy.deepcopy(self.payload)
        forged.update(kind="historical", effective_at="2026-11-09T00:00:00Z")
        with self.assertRaises(portfolio.PortfolioInputError):
            self.store.save(forged)

    def test_retained_summary_must_equal_asset_values_and_coverage(self):
        for update in (
            {"known_value_usd": "999"},
            {"total_value_usd": "999"},
            {"priced_assets": 1},
            {"unpriced_assets": 1},
        ):
            with self.subTest(update=update):
                forged = copy.deepcopy(self.payload)
                forged["summary"].update(update)
                with self.assertRaises(portfolio.PortfolioInputError):
                    self.store.save(forged)
        forged = copy.deepcopy(self.payload)
        forged["coverage"]["inventory_complete"] = False
        with self.assertRaises(portfolio.PortfolioInputError):
            self.store.save(forged)

    def test_indexed_columns_cannot_disagree_with_hashed_body(self):
        saved = self.store.save(self.payload)
        for column, value in (
            ("account", "bob.near"),
            ("kind", "historical"),
            ("effective_ms", 1),
            ("observed_ms", 1),
        ):
            with self.subTest(column=column):
                with self.store._connection(write=True) as connection:
                    original = connection.execute(
                        "SELECT " + column + " FROM observations WHERE id=?",
                        (saved["id"],),
                    ).fetchone()[0]
                    connection.execute(
                        "UPDATE observations SET " + column + "=? WHERE id=?",
                        (value, saved["id"]),
                    )
                with self.assertRaises(portfolio.PortfolioReadError):
                    self.store.list_records(
                        "bob.near" if column == "account" else "alice.near"
                    )
                with self.assertRaises(portfolio.PortfolioReadError):
                    self.store.get_bytes(saved["id"])
                with self.assertRaises(portfolio.PortfolioReadError):
                    self.store.save(self.payload)
                with self.store._connection(write=True) as connection:
                    connection.execute(
                        "UPDATE observations SET " + column + "=? WHERE id=?",
                        (original, saved["id"]),
                    )

    def test_duplicate_save_rejects_corrupt_retained_bytes(self):
        saved = self.store.save(self.payload)
        with self.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE observations SET body=? WHERE id=?", (b"{}\n", saved["id"])
            )
        with self.assertRaises(portfolio.PortfolioReadError):
            self.store.save(self.payload)

    def test_snapshot_bytes_hash_and_actual_observation_chart_survive_restart(self):
        saved = self.store.save(self.payload)
        raw = self.store.get_bytes(saved["id"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), saved["id"])
        self.assertEqual(json.loads(raw), self.payload)
        self.assertEqual(self.store.save(self.payload), saved)
        restarted = portfolio.PortfolioHistory(self.path)
        chart = restarted.list_records("alice.near")
        self.assertEqual(
            chart["items"][0]["total_value_usd"],
            self.payload["summary"]["total_value_usd"],
        )
        self.assertFalse(chart["coverage"]["history_complete"])
        self.assertEqual(restarted.list_records("bob.near")["items"], [])

    def test_late_historical_point_has_correct_time_keyset_and_selected_balance(self):
        self.store.save(self.payload)
        provider = Providers()
        provider.header["timestamp"] = 1794182400 * 1000000000
        for price in provider.prices.values():
            price["timestamp"] = 1794182400
        earlier = portfolio.PortfolioData(
            Reader(), client_factory=provider.factory, clock=lambda: NOW
        ).historical("alice.near", 50)
        self.store.save(earlier)
        newest = self.store.list_records("alice.near", limit=1)
        older = self.store.list_records(
            "alice.near",
            before=newest["pagination"]["next_before"],
            limit=1,
            asset_id="native:near",
        )
        self.assertEqual(older["items"][0]["effective_at"], "2026-11-09T00:00:00Z")
        self.assertEqual(
            older["items"][0]["balance_base_units"],
            earlier["assets"][0]["balance_base_units"],
        )

    def test_archival_retention_binds_time_scope_and_balance_sources(self):
        provider = Providers()
        for price in provider.prices.values():
            price["timestamp"] = NOW - 86400
        genuine = portfolio.PortfolioData(
            Reader(), client_factory=provider.factory, clock=lambda: NOW
        ).historical("alice.near", 50, ["wrap.near"])
        self.store.save(genuine)
        for change in ("time", "height", "complete", "source", "price", "value"):
            with self.subTest(change=change):
                self.clock[0] += 61
                forged = copy.deepcopy(genuine)
                if change == "time":
                    forged["effective_at"] = "2026-11-09T00:00:00Z"
                elif change == "height":
                    forged["sources"][0]["block_height"] = "51"
                elif change == "complete":
                    forged["coverage"]["inventory_complete"] = True
                    forged["summary"]["total_value_usd"] = forged["summary"][
                        "known_value_usd"
                    ]
                elif change == "source":
                    forged["assets"][1]["balance_source"]["block_height"] = "51"
                elif change == "price":
                    forged["assets"][0]["price"]["timestamp"] += 103
                else:
                    forged["assets"][0]["value_usd"] = "999"
                    forged["summary"]["known_value_usd"] = "1011.5"
                with self.assertRaises(portfolio.PortfolioInputError):
                    self.store.save(forged)

    def test_private_data_and_capacity_refusal_preserve_retained_evidence(self):
        with self.assertRaises(portfolio.PortfolioInputError):
            self.store.save({**self.payload, "notes": "private"})
        saved = self.store.save(self.payload)
        self.clock[0] += 61
        with (
            mock.patch.object(portfolio, "MAX_SNAPSHOTS", 1),
            self.assertRaises(portfolio.PortfolioCapacityError),
        ):
            self.store.save(self.current("2000000000000000000000002"))
        self.assertIsNotNone(self.store.get_bytes(saved["id"]))

    def test_duplicate_and_invalid_cursor_or_hash_do_not_cross_accounts(self):
        self.store.save(self.payload)
        with self.assertRaises(portfolio.PortfolioInputError):
            self.store.list_records("bob.near", before=1)
        with self.assertRaises(portfolio.PortfolioInputError):
            self.store.get_bytes("../secret")

    def test_retained_capture_budget_is_durable_and_old_observations_remain(self):
        first = self.store.save(self.payload)
        second = self.current("2000000000000000000000002")
        self.store.save(second)
        restarted = portfolio.PortfolioHistory(self.path, clock=lambda: self.clock[0])
        third = self.current("2000000000000000000000003")
        with self.assertRaises(portfolio.PortfolioCapacityError):
            restarted.save(third)
        self.assertIsNotNone(restarted.get_bytes(first["id"]))
        self.clock[0] += 61
        self.assertIsNotNone(restarted.save(third))

    def test_tampered_snapshot_bytes_are_not_served(self):
        first = self.store.save(self.payload)
        with self.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE observations SET body=? WHERE id=?", (b"{}\n", first["id"])
            )
        with self.assertRaises(portfolio.PortfolioReadError):
            self.store.get_bytes(first["id"])
        with self.assertRaises(portfolio.PortfolioReadError):
            self.store.list_records("alice.near")

    def test_repeated_identical_reads_reuse_one_retained_snapshot(self):
        provider = Providers()
        for price in provider.prices.values():
            price["timestamp"] = NOW - 86400
        genuine = portfolio.PortfolioData(
            Reader(), client_factory=provider.factory, clock=lambda: NOW
        ).historical("alice.near", 50, ["wrap.near"])
        first = self.store.save(genuine)
        repeat = copy.deepcopy(genuine)
        repeat["observed_at"] = "2026-11-10T14:40:00Z"
        for source in repeat["sources"]:
            source["fetched_at"] = "2026-11-10T14:40:00Z"
        for asset in repeat["assets"]:
            asset["price"]["source"]["fetched_at"] = "2026-11-10T14:40:00Z"
        for _ in range(3):
            self.assertEqual(self.store.save(repeat), first)
        self.assertEqual(json.loads(self.store.get_bytes(first["id"])), genuine)
        self.assertEqual(len(self.store.list_records("alice.near")["items"]), 1)
        self.assertNotEqual(self.store.save(self.payload), first)
        self.assertEqual(len(self.store.list_records("alice.near")["items"]), 2)

    def test_current_reads_a_minute_apart_reuse_the_snapshot_until_a_balance_changes(self):
        reader, clock = Reader(), [NOW]
        reader.tokens[0]["metadata_source"]["block_height"] = "99"
        data = portfolio.PortfolioData(
            reader, client_factory=Providers().factory, clock=lambda: clock[0]
        )
        genuine = data.inspect("alice.near")
        first = self.store.save(genuine)
        for step in range(1, 4):
            clock[0] = self.clock[0] = NOW + 61 * step
            reader.native["native"]["block_height"] = str(100 + 60 * step)
            reader.tokens[0]["metadata_source"]["block_height"] = str(99 + 60 * step)
            self.assertEqual(self.store.save(data.inspect("alice.near")), first)
        self.assertEqual(json.loads(self.store.get_bytes(first["id"])), genuine)
        self.assertEqual(len(self.store.list_records("alice.near")["items"]), 1)
        reader.native["native"]["amount_yocto"] = "2000000000000000000000002"
        changed = self.store.save(data.inspect("alice.near"))
        self.assertNotEqual(changed, first)
        reader.native["native"]["amount_yocto"] = "2000000000000000000000001"
        clock[0] = self.clock[0] = clock[0] + 61
        restored = self.store.save(data.inspect("alice.near"))
        self.assertNotIn(restored, (first, changed))
        self.assertEqual(len(self.store.list_records("alice.near")["items"]), 3)

    def test_a_corrupt_latest_current_observation_is_not_reused_or_cited(self):
        clock = [NOW]
        data = portfolio.PortfolioData(
            Reader(), client_factory=Providers().factory, clock=lambda: clock[0]
        )
        corrupt = self.store.save(data.inspect("alice.near"))
        with self.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE observations SET body=? WHERE id=?", (b"{}\n", corrupt["id"])
            )
        clock[0] = NOW + 61
        fresh = self.store.save(data.inspect("alice.near"))
        self.assertNotEqual(fresh, corrupt)
        self.assertIsNotNone(self.store.get_bytes(fresh["id"]))
        clock[0] = NOW + 122
        self.assertEqual(self.store.save(data.inspect("alice.near")), fresh)
        with self.store._connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2
            )
        nested = b'{"a":' * 3000 + b"1" + b"}" * 3000
        identifier = hashlib.sha256(nested).hexdigest()
        with self.store._connection(write=True) as connection:
            connection.execute(
                "UPDATE observations SET body=?,id=? WHERE id=?",
                (nested, identifier, fresh["id"]),
            )
        with self.assertRaises(portfolio.PortfolioReadError):
            self.store.get_bytes(identifier)
        clock[0] = self.clock[0] = NOW + 183
        deeper = self.store.save(data.inspect("alice.near"))
        self.assertNotIn(deeper, (corrupt, fresh))
        self.assertIsNotNone(self.store.get_bytes(deeper["id"]))
        clock[0] = self.clock[0] = NOW + 244
        self.assertEqual(self.store.save(data.inspect("alice.near")), deeper)
        with self.store._connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 3
            )

    def test_a_latest_observation_that_cannot_be_compared_or_serialized_is_not_reused(self):
        nested = 1
        for _ in range(28):
            nested = {"level": nested}
        for name, value in (("nested", nested), ("surrogate", "\ud800")):
            with self.subTest(name=name):
                clock = [NOW]
                self.clock[0] = NOW
                store = portfolio.PortfolioHistory(
                    Path(self.directory.name) / (name + ".sqlite3"),
                    clock=lambda: self.clock[0],
                )
                data = portfolio.PortfolioData(
                    Reader(), client_factory=Providers().factory, clock=lambda: clock[0]
                )
                saved = store.save(data.inspect("alice.near"))
                document = json.loads(store.get_bytes(saved["id"]))
                document["assets"][0]["details"]["extra"] = value
                body = (
                    json.dumps(document, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
                identifier = hashlib.sha256(body).hexdigest()
                with store._connection(write=True) as connection:
                    connection.execute(
                        "UPDATE observations SET body=?,id=? WHERE id=?",
                        (body, identifier, saved["id"]),
                    )
                with self.assertRaises(portfolio.PortfolioReadError):
                    store.get_bytes(identifier)
                with self.assertRaises(portfolio.PortfolioReadError):
                    store.list_records("alice.near")
                clock[0] = self.clock[0] = NOW + 61
                fresh = store.save(data.inspect("alice.near"))
                self.assertNotIn(fresh["id"], (saved["id"], identifier))
                self.assertIsNotNone(store.get_bytes(fresh["id"]))
                clock[0] = self.clock[0] = NOW + 122
                self.assertEqual(store.save(data.inspect("alice.near")), fresh)
                with store._connection() as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM observations"
                        ).fetchone()[0],
                        2,
                    )


if __name__ == "__main__":
    unittest.main()
