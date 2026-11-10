import unittest
from unittest import mock

from intel import accounts, live


class LiveAccountsTest(unittest.TestCase):
    def setUp(self):
        self.service = live.LiveAPI()

    def test_account_routes_dispatch_exact_queries_without_signing(self):
        for kind, query, method, kwargs in (
            ("profile", "", "profile", {}),
            ("holdings", "offset=20&limit=10", "holdings", {"offset": 20, "limit": 10}),
            ("transactions", "cursor=opaque%2Ftoken&limit=25", "transactions", {"cursor": "opaque/token", "limit": 25}),
            ("transfers", "limit=50", "transfers", {"limit": 50}),
            ("graph", "depth=2", "neighborhood", {"depth": 2}),
        ):
            with self.subTest(kind=kind), mock.patch.object(accounts.AccountData, method, return_value={"kind": kind}) as read:
                status, body, _ = self.service.handle(f"/live/accounts/wrap.near/{kind}" + ("?" + query if query else ""), "203.0.113.1", {})
                self.assertEqual((status, body), (200, {"kind": kind}))
                read.assert_called_once_with("wrap.near", **kwargs)

    def test_unknown_fields_repeated_fields_and_oversized_limits_are_refused(self):
        for path in (
            "/live/accounts/wrap.near/profile?limit=2",
            "/live/accounts/wrap.near/holdings?offset=-1",
            "/live/accounts/wrap.near/holdings?limit=21",
            "/live/accounts/wrap.near/transactions?limit=101",
            "/live/accounts/wrap.near/transfers?limit=2&limit=3",
            "/live/accounts/wrap.near/graph?depth=3",
            "/live/accounts/vendor.testnet/profile",
            "/live/accounts/bad%2Fname/profile",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.service.handle(path, "203.0.113.1", {})[0], 400)

    def test_provider_errors_and_deadlines_are_not_favorable_results(self):
        for error, expected in ((accounts.AccountReadError("missing amount"), 503),
                                (accounts.AccountInputError("bad account"), 400),
                                (TimeoutError("deadline"), 504)):
            with self.subTest(error=type(error).__name__), mock.patch.object(accounts.AccountData, "profile", side_effect=error):
                status, body, _ = self.service.handle("/live/accounts/wrap.near/profile", "203.0.113.1", {})
                self.assertEqual(status, expected)
                self.assertNotIn("ALLOW", str(body))
