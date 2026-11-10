import unittest
from unittest import mock

from intel import accounts, labels
from tests.test_accounts import Provider, transfer


class AccountHistoryPagesTest(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.provider = Provider()
        self.reader = accounts.AccountData(labels.Store([], "b" * 64), client_factory=self.provider.client)

    def test_transfer_time_bounds_and_opaque_cursor_are_sent_unchanged(self):
        self.provider.answers[accounts.TRANSFERS_URL, "/v0/transfers"] = {"transfers": [transfer(1)], "resume_token": "next+/="}
        result = self.reader.history_page("alice.near", "transfers", cursor="previous+/=", from_timestamp_ms=1735689600000, to_timestamp_ms=1738368000000)
        self.assertEqual(self.provider.requests[-1][3], {"account_id": "alice.near", "desc": True, "limit": 100,
            "resume_token": "previous+/=", "from_timestamp_ms": 1735689600000, "to_timestamp_ms": 1738368000000})
        self.assertEqual(result["pagination"]["next_cursor"], "next+/=")
        self.assertEqual(result["coverage"]["date_filter"], "provider_time_bounds")

    def test_transaction_dates_are_explicit_worker_filters_not_unsupported_provider_fields(self):
        self.provider.answers[accounts.TX_URL, "/v0/account"] = {"account_txs": [], "resume_token": "next+/="}
        result = self.reader.history_page("alice.near", "transactions", from_timestamp_ms=10, to_timestamp_ms=20)
        self.assertNotIn("from_timestamp_ms", self.provider.requests[-1][3])
        self.assertNotIn("to_timestamp_ms", self.provider.requests[-1][3])
        self.assertEqual(result["coverage"]["date_filter"], "worker_filters_bounded_newest_first_scan")

    def test_bad_date_bounds_are_rejected_before_network(self):
        for bounds in ({"from_timestamp_ms": -1}, {"from_timestamp_ms": True}, {"from_timestamp_ms": 20, "to_timestamp_ms": 10}):
            with self.subTest(bounds=bounds), self.assertRaises(accounts.AccountInputError):
                self.reader.history_page("alice.near", "transfers", **bounds)
        self.assertEqual(self.provider.requests, [])
