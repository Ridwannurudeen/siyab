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

    def holdings(self, account, offset=0, limit=20):
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

    def factory(self, url, mode, **options):
        parent = self

        class Client:
            last_source = {
                "provider": "test-provider",
                "request_sha256": "a" * 64,
                "fetched_at": "2026-11-10T00:00:00Z",
            }
            last_block_height = 50

            def get(self, path, **kwargs):
                parent.calls.append((url, path, kwargs))
                return {"coins": copy.deepcopy(parent.prices)}

            def block_at(self, height):
                parent.views.append(("block", height))
                return {"header": copy.deepcopy(parent.header)}

            def view_account_at(self, account, height):
                parent.views.append(("native", account, height))
                return copy.deepcopy(parent.balance)

            def call_function(self, contract, method, args, *, block_id=None):
                parent.views.append((method, contract, args, block_id))
                if method == "ft_metadata":
                    return {"name": "Wrapped NEAR", "symbol": "wNEAR", "decimals": 24}
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
                result = self.data.historical("alice.near", 50)
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

    def test_invalid_accounts_and_bounds_are_rejected_before_read(self):
        for account in ("foo.testnet", "testnet", "bad/name"):
            with self.assertRaises(portfolio.PortfolioInputError):
                self.data.inspect(account)
        for maximum in (0, 101, True):
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
            self.store.save({**self.payload, "observed_at": "2026-11-10T15:00:00Z"})
        self.assertIsNotNone(self.store.get_bytes(saved["id"]))

    def test_duplicate_and_invalid_cursor_or_hash_do_not_cross_accounts(self):
        self.store.save(self.payload)
        with self.assertRaises(portfolio.PortfolioInputError):
            self.store.list_records("bob.near", before=1)
        with self.assertRaises(portfolio.PortfolioInputError):
            self.store.get_bytes("../secret")

    def test_retained_capture_budget_is_durable_and_old_observations_remain(self):
        first = self.store.save(self.payload)
        second = {**self.payload, "observed_at": "2026-11-10T14:00:00Z"}
        self.store.save(second)
        restarted = portfolio.PortfolioHistory(self.path, clock=lambda: self.clock[0])
        with self.assertRaises(portfolio.PortfolioCapacityError):
            restarted.save({**self.payload, "observed_at": "2026-11-10T14:00:01Z"})
        self.assertIsNotNone(restarted.get_bytes(first["id"]))
        self.clock[0] += 61
        self.assertIsNotNone(
            restarted.save({**self.payload, "observed_at": "2026-11-10T14:00:01Z"})
        )

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


if __name__ == "__main__":
    unittest.main()
