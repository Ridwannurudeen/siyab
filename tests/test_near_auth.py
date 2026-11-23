import base64
import concurrent.futures
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gate import base58, ed25519, rpc
from intel import budget, near_auth
from intel.workspace import SQLiteWorkspaceStore, WorkspaceError


class AccessKeyRPC:
    mode = "off"
    kind = "rpc"

    def __init__(self):
        self.calls = []
        self.result = {"nonce": 1, "permission": "FullAccess", "block_height": 100,
                       "block_hash": base58.encode(bytes(range(32)))}
        self.error = None
        self.on_call = None

    def call(self, method, params):
        self.calls.append((method, params))
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        return self.result


class NEP413VectorTest(unittest.TestCase):
    def test_okx_signature_verifies_only_with_the_exact_callback(self):
        path = Path(__file__).resolve().parents[1] / "fixtures" / "near-auth" / "okx-callback-20261122.json"
        capture = json.loads(path.read_text(encoding="utf-8"))
        challenge, response = capture["challenge"], capture["verify"]
        nonce = base64.b64decode(challenge["nonce"], validate=True)
        public = base58.decode_key(response["publicKey"])
        signature = base64.b64decode(response["signature"], validate=True)
        callback = challenge["recipient"] + "/workspace.html"
        self.assertEqual(challenge["callbackUrl"], callback)
        payload = near_auth.serialize_payload(challenge["message"], nonce, challenge["recipient"], callback)
        self.assertTrue(ed25519.verify(public, hashlib.sha256(payload).digest(), signature))
        for changed in (None, challenge["recipient"] + "/index.html", callback + "/"):
            with self.subTest(callback=changed):
                payload = near_auth.serialize_payload(challenge["message"], nonce, challenge["recipient"], changed)
                self.assertFalse(ed25519.verify(public, hashlib.sha256(payload).digest(), signature))

    def test_spec_example_exact_borsh_with_callback(self):
        expected = (b"\x9d\x01\x00\x80\x02\x00\x00\x00hi" + bytes(range(32))
                    + b"\x09\x00\x00\x00myapp.com\x01\x12\x00\x00\x00myapp.com/callback")
        self.assertEqual(near_auth.serialize_payload("hi", bytes(range(32)), "myapp.com", "myapp.com/callback"), expected)

    def test_official_near_api_js_payload_hash(self):
        # near/near-api-js test/unit/signers/key_pair_signer.test.ts, NEP-413 payload hash test.
        nonce = base64.b64decode("KNV0cOpvJ50D5vfF9pqWom8wo2sliQ4W+Wa7uZ3Uk6Y=")
        digest = hashlib.sha256(near_auth.serialize_payload("Hello NEAR!", nonce, "round-toad.testnet")).digest()
        self.assertEqual(digest, bytes([1, 152, 236, 223, 103, 218, 230, 0, 34, 54, 210, 18, 244, 68, 108, 252,
                                       140, 166, 102, 57, 242, 4, 202, 234, 205, 94, 246, 245, 198, 141, 23, 250]))

    def test_optional_callback_presence_is_serialized(self):
        absent = near_auth.serialize_payload("hi", bytes(32), "myapp.com")
        empty = near_auth.serialize_payload("hi", bytes(32), "myapp.com", "")
        self.assertEqual(absent[-1:], b"\0")
        self.assertEqual(empty, absent[:-1] + b"\1\0\0\0\0")

    def test_utf8_lengths_count_bytes(self):
        payload = near_auth.serialize_payload("é", bytes(32), "myapp.com")
        self.assertEqual(payload[4:10], b"\2\0\0\0\xc3\xa9")

    def test_nonce_is_fixed_32_bytes(self):
        for nonce in (bytes(31), bytes(33), "0" * 32, [0] * 32, None):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                near_auth.serialize_payload("hi", nonce, "myapp.com")


class NearAuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.public = base58.encode_key(ed25519.public_from_seed(ed25519.TEST_AGENT_SEED))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1700000000.0
        self.store = SQLiteWorkspaceStore(Path(self.directory.name) / "workspace.sqlite3", clock=lambda: self.now)
        self.client = AccessKeyRPC()
        self.auth = near_auth.NearAuth(self.store, "https://siyab.xyz", client=self.client, clock=lambda: self.now)
        self.binding = "b" * 43

    def issue(self):
        return self.auth.issue("alice.near", self.binding, client_address="192.0.2.1")

    def test_verification_uses_a_separate_rpc_caller_class(self):
        seen = []
        self.client.on_call = lambda: seen.append(rpc._CALLER_CLASS.get())
        self.auth.verify(self.response(self.issue()), self.binding)
        self.assertEqual(seen, ["near_auth"])

    def test_twenty_five_rotating_key_networks_cannot_deny_a_new_client_for_the_day(self):
        response = self.response(self.issue())
        valid = self.client.result
        self.auth.client = rpc.Client(rpc.MAINNET_RPC_URL, mode="off", clock=lambda: self.now)

        def fetch(url, body, headers, record_http_errors=()):
            key = body["params"]["public_key"]
            result = valid if key == self.public else {
                "block_height": valid["block_height"], "block_hash": valid["block_hash"],
                "error": f"access key {key} does not exist while viewing", "logs": []}
            return {"jsonrpc": "2.0", "id": body["id"], "result": result}

        with patch.dict("os.environ", {budget.DATABASE_ENV: str(Path(self.directory.name) / "budget.sqlite3")}), \
                patch.object(rpc, "_fetch", side_effect=fetch) as exchange, \
                patch.object(near_auth, "_prime_order", return_value=True), patch.object(ed25519, "verify", return_value=True):
            for version in (4, 6):
                self.now = start = (int(self.now // 86400) + 1) * 86400
                before = exchange.call_count
                for attempt in range(20):
                    self.now = start + attempt * 1200
                    for network in range(25):
                        address = f"192.0.2.{network + 1}" if version == 4 else f"2001:db8:{network + 1:x}:{attempt:x}::1"
                        challenge = self.auth.issue("alice.near", self.binding, client_address=address)
                        key = base58.encode_key(hashlib.sha256(f"{version}:{attempt}:{network}".encode()).digest())
                        candidate = {**response, "challenge_id": challenge["challenge_id"], "state": challenge["state"], "publicKey": key}
                        self.refused(candidate, 401, "near_key_invalid")
                self.assertEqual(exchange.call_count - before, 500)
                challenge = self.auth.issue("alice.near", self.binding, client_address="198.51.100.1")
                self.assertEqual(self.auth.verify({**response, "challenge_id": challenge["challenge_id"],
                                                  "state": challenge["state"]}, self.binding)["account_id"], "alice.near")
                self.assertEqual(exchange.call_count - before, 501)

    def test_twenty_five_networks_cannot_consume_the_newcomer_minute_reserve(self):
        for version in (4, 6):
            self.now = (int(self.now // 86400) + 1) * 86400
            for attempt in range(3):
                for network in range(25):
                    address = f"192.0.2.{network + 1}" if version == 4 else f"2001:db8:{network + 1:x}:{attempt:x}::1"
                    row = {"account_id": "alice.near", "client_bucket": near_auth._client_bucket(address)}
                    if attempt == 0 or attempt == 1 or network < 5:
                        self.auth._reserve_lookup(row, self.public)
                    else:
                        with self.assertRaises(WorkspaceError) as caught:
                            self.auth._reserve_lookup(row, self.public)
                        self.assertEqual(caught.exception.code, "near_verify_capacity")
            self.auth = near_auth.NearAuth(SQLiteWorkspaceStore(self.store.path), "https://siyab.xyz",
                                          client=self.client, clock=lambda: self.now)
            self.auth._reserve_lookup({"account_id": "alice.near", "client_bucket": "198.51.100.1/32"}, self.public)
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT SUM(count) FROM near_auth_rpc_days").fetchone()[0], 56)
            self.now += 60
            address = "192.0.2.1" if version == 4 else "2001:db8:1:ffff::1"
            self.auth._reserve_lookup({"account_id": "alice.near", "client_bucket": near_auth._client_bucket(address)}, self.public)

    def test_newcomer_hour_and_day_reserves_survive_restart_and_reopen_exactly(self):
        for span, maximum in ((budget.DAY_S, 6144), (budget.HOUR_S, 768)):
            self.now = (int(self.now // 86400) + 1) * 86400 + 7200
            day, hour, minute = int(self.now // 86400), int(self.now // 3600), int(self.now // 60)
            rows = [(f"198.18.{index // 256}.{index % 256}/32", day, 1, minute - 1, 1) for index in range(maximum - 1)]
            with self.store._connection(write=True) as connection:
                connection.execute("DELETE FROM near_auth_rpc_days")
                connection.execute("DELETE FROM near_auth_rpc_buckets")
                connection.executemany("INSERT INTO near_auth_rpc_days VALUES (?,?,?,?,?)", rows)
                if span == budget.HOUR_S:
                    connection.executemany("INSERT INTO near_auth_rpc_buckets VALUES (?,?,1)", [(row[0], hour) for row in rows])
            self.auth = near_auth.NearAuth(SQLiteWorkspaceStore(self.store.path), "https://siyab.xyz",
                                          client=self.client, clock=lambda: self.now)
            self.auth._reserve_lookup({"account_id": "alice.near", "client_bucket": "198.51.100.1/32"}, self.public)
            self.now = (int(self.now // span) + 1) * span - .001
            row = {"account_id": "alice.near", "client_bucket": "198.51.100.2/32"}
            with self.assertRaises(WorkspaceError) as caught:
                self.auth._reserve_lookup(row, self.public)
            self.assertEqual((caught.exception.code, caught.exception.retry_after), ("near_verify_capacity", 1))
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT SUM(count) FROM near_auth_rpc_days").fetchone()[0], maximum)
            self.now += .001
            self.auth._reserve_lookup(row, self.public)

    def test_newcomer_reserve_checks_all_windows_before_counting_and_reopens_exactly(self):
        self.now = self.now // 60 * 60
        for network in range(90):
            self.auth._reserve_lookup({"account_id": "alice.near", "client_bucket": f"192.0.2.{network + 1}/32"}, self.public)
        self.now += 59.999
        row = {"account_id": "alice.near", "client_bucket": "198.51.100.1/32"}
        with self.assertRaises(WorkspaceError) as caught:
            self.auth._reserve_lookup(row, self.public)
        self.assertEqual((caught.exception.code, caught.exception.retry_after), ("near_verify_capacity", 1))
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT SUM(count) FROM near_auth_rpc_days").fetchone()[0], 90)
            self.assertIsNone(connection.execute("SELECT 1 FROM near_auth_rpc_days WHERE client_bucket=?", (row["client_bucket"],)).fetchone())
        self.now += .001
        self.auth._reserve_lookup(row, self.public)

    def test_concurrent_workers_cannot_exceed_the_newcomer_reserve(self):
        for network in range(89):
            self.auth._reserve_lookup({"account_id": "alice.near", "client_bucket": f"192.0.2.{network + 1}/32"}, self.public)
        second = near_auth.NearAuth(SQLiteWorkspaceStore(self.store.path), "https://siyab.xyz",
                                   client=self.client, clock=lambda: self.now)
        barrier = threading.Barrier(2)

        def reserve(candidate):
            auth, address = candidate
            barrier.wait(timeout=5)
            try:
                auth._reserve_lookup({"account_id": "alice.near", "client_bucket": address}, self.public)
                return "reserved"
            except WorkspaceError as error:
                return error.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, ((self.auth, "198.51.100.1/32"), (second, "198.51.100.2/32"))))
        self.assertEqual(sorted(results), ["near_verify_capacity", "reserved"])
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT SUM(count) FROM near_auth_rpc_days").fetchone()[0], 90)

    def test_three_ipv4_and_four_ipv6_buckets_cannot_spend_the_global_daily_rpc_cap(self):
        start = self.now // 86400 * 86400
        response = self.response(self.issue())
        with patch.object(near_auth, "_prime_order", return_value=True), patch.object(ed25519, "verify", return_value=True):
            for addresses in (("192.0.2.1", "192.0.2.2", "192.0.2.3"),
                              ("2001:db8:1::1", "2001:db8:2::1", "2001:db8:3::1", "2001:db8:4::1")):
                self.now = start
                before = len(self.client.calls)
                for attempt in range(21):
                    self.now = start + attempt * 1200
                    for address in addresses:
                        address = address.replace("::1", f":{attempt:x}::1") if ":" in address else address
                        challenge = self.auth.issue("alice.near", self.binding, client_address=address)
                        candidate = {**response, "challenge_id": challenge["challenge_id"], "state": challenge["state"]}
                        if attempt < 20:
                            self.auth.verify(candidate, self.binding)
                        else:
                            self.refused(candidate, 429, "near_verify_limit")
                self.assertEqual(len(self.client.calls) - before, len(addresses) * 20)
                self.assertLess(len(self.client.calls) - before, 500)
        self.assertEqual((500 + near_auth.MAX_VERIFY_RPC_PER_BUCKET_DAY - 1)
                         // near_auth.MAX_VERIFY_RPC_PER_BUCKET_DAY, 25)

    def test_three_ipv4_and_four_ipv6_buckets_cannot_spend_the_global_minute_rpc_cap(self):
        start = self.now // 60 * 60 + 59.999
        response = self.response(self.issue())
        with patch.object(near_auth, "_prime_order", return_value=True), patch.object(ed25519, "verify", return_value=True):
            for addresses in (("192.0.2.1", "192.0.2.2", "192.0.2.3"),
                              ("2001:db8:1::1", "2001:db8:2::1", "2001:db8:3::1", "2001:db8:4::1")):
                before = len(self.client.calls)
                for self.now in (start, start + .001):
                    for attempt in range(4):
                        for address in addresses:
                            address = address.replace("::1", f":{attempt:x}::1") if ":" in address else address
                            challenge = self.auth.issue("alice.near", self.binding, client_address=address)
                            candidate = {**response, "challenge_id": challenge["challenge_id"], "state": challenge["state"]}
                            if attempt < 3:
                                self.auth.verify(candidate, self.binding)
                            else:
                                self.refused(candidate, 429, "near_verify_limit")
                    self.assertEqual(len(self.client.calls) - before, len(addresses) * 3 * (1 + (self.now > start)))
                self.assertLess(len(self.client.calls) - before, 30)

    def test_daily_limit_survives_restart_and_rolls_at_exact_utc_day(self):
        self.now = self.now // 86400 * 86400
        row = {"account_id": "alice.near", "client_bucket": "192.0.2.1/32"}
        for attempt in range(20):
            self.now += 1200
            self.auth._reserve_lookup(row, self.public)
        self.auth = near_auth.NearAuth(SQLiteWorkspaceStore(self.store.path), "https://siyab.xyz",
                                       client=self.client, clock=lambda: self.now)
        self.now = self.now // 86400 * 86400 + 86399.999
        with self.assertRaises(WorkspaceError) as caught:
            self.auth._reserve_lookup(row, self.public)
        self.assertEqual((caught.exception.code, caught.exception.retry_after), ("near_verify_limit", 1))
        self.now += .001
        self.auth._reserve_lookup(row, self.public)
        with self.store._connection() as connection:
            rows = connection.execute("SELECT count,minute_count FROM near_auth_rpc_days").fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1, 1)])

    def test_minute_limit_shares_accounts_bindings_and_workers_without_partial_reservations(self):
        response = self.response(self.issue())
        with patch.object(near_auth, "_prime_order", return_value=True), patch.object(ed25519, "verify", return_value=True):
            for attempt in range(4):
                self.auth = near_auth.NearAuth(SQLiteWorkspaceStore(self.store.path), "https://siyab.xyz",
                                               client=self.client, clock=lambda: self.now)
                binding = chr(ord("c") + attempt) * 43
                account = f"alice{attempt}.near"
                challenge = self.auth.issue(account, binding, client_address="::ffff:192.0.2.1")
                candidate = {**response, "accountId": account, "challenge_id": challenge["challenge_id"], "state": challenge["state"]}
                if attempt < 3:
                    self.auth.verify(candidate, binding)
                else:
                    self.refused(candidate, 429, "near_verify_limit", binding=binding)
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT count FROM near_auth_rpc_buckets").fetchone()[0], 3)
            self.assertEqual(tuple(connection.execute("SELECT count,minute_count FROM near_auth_rpc_days").fetchone()), (3, 3))
        self.assertEqual(len(self.client.calls), 3)

    def test_verify_limit_survives_restart_and_shares_ipv6_48(self):
        with patch.object(near_auth, "MAX_VERIFY_RPC_PER_BUCKET_HOUR", 1, create=True):
            challenge = self.auth.issue("alice.near", self.binding, client_address="2001:db8:1234::1")
            self.auth.verify(self.response(challenge), self.binding)
            self.auth = near_auth.NearAuth(self.store, "https://siyab.xyz", client=self.client, clock=lambda: self.now)
            challenge = self.auth.issue("alice.near", self.binding, client_address="2001:db8:1234:ffff::2")
            self.refused(self.response(challenge), 429, "near_verify_limit")
        self.assertEqual(len(self.client.calls), 1)

    def test_upgrade_preserves_existing_hour_counts_in_new_windows(self):
        hour = int(self.now // 3600)
        with self.store._connection(write=True) as connection:
            connection.execute("DROP TABLE near_auth_rpc_days")
            connection.execute("INSERT INTO near_auth_rpc_buckets VALUES (?,?,?)", ("192.0.2.1/32", hour, 3))
        self.auth = near_auth.NearAuth(self.store, "https://siyab.xyz", client=self.client, clock=lambda: self.now)
        self.refused(self.response(self.issue()), 429, "near_verify_limit")
        self.now = (int(self.now // 60) + 1) * 60
        self.auth.verify(self.response(self.issue()), self.binding)
        self.auth = near_auth.NearAuth(self.store, "https://siyab.xyz", client=self.client, clock=lambda: self.now)
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT count FROM near_auth_rpc_buckets").fetchone()[0], 4)
            self.assertEqual(tuple(connection.execute("SELECT count,minute_count FROM near_auth_rpc_days").fetchone()), (4, 1))

    def test_daily_bucket_capacity_preserves_live_counters_across_hour_rollover(self):
        row = {"account_id": "alice.near", "client_bucket": "192.0.2.1/32"}
        with patch.object(near_auth, "MAX_VERIFY_BUCKETS", 1):
            self.auth._reserve_lookup(row, self.public)
            self.now = (int(self.now // 3600) + 1) * 3600
            with self.assertRaises(WorkspaceError) as caught:
                self.auth._reserve_lookup({**row, "client_bucket": "192.0.2.2/32"}, self.public)
            self.assertEqual(caught.exception.code, "near_verify_capacity")
            with self.store._connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM near_auth_rpc_days").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count FROM near_auth_rpc_days").fetchone()[0], 1)
            self.auth._reserve_lookup(row, self.public)
            self.now = (int(self.now // 86400) + 1) * 86400
            self.auth._reserve_lookup({**row, "client_bucket": "192.0.2.2/32"}, self.public)
        with self.store._connection() as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT client_bucket,count FROM near_auth_rpc_days")],
                             [("192.0.2.2/32", 1)])

    def test_concurrent_workers_cannot_exceed_minute_or_daily_limit(self):
        row = {"account_id": "alice.near", "client_bucket": "192.0.2.1/32"}
        second = near_auth.NearAuth(SQLiteWorkspaceStore(self.store.path), "https://siyab.xyz",
                                   client=self.client, clock=lambda: self.now)
        for window in ("minute", "day"):
            self.now = (int(self.now // 86400) + 1) * 86400
            for attempt in range(19 if window == "day" else 2):
                if window == "day":
                    self.now += 1200
                self.auth._reserve_lookup(row, self.public)
            barrier = threading.Barrier(2)

            def reserve(auth):
                barrier.wait(timeout=5)
                try:
                    auth._reserve_lookup(row, self.public)
                    return "reserved"
                except WorkspaceError as error:
                    return error.code

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(reserve, (self.auth, second)))
            self.assertEqual(sorted(results), ["near_verify_limit", "reserved"])
            with self.store._connection() as connection:
                daily = connection.execute("SELECT count,minute_count FROM near_auth_rpc_days").fetchone()
                hourly = connection.execute("SELECT count FROM near_auth_rpc_buckets").fetchone()[0]
            self.assertEqual(tuple(daily), (20, 2) if window == "day" else (3, 3))
            self.assertEqual(hourly, 3)

    def test_rpc_failure_spends_minute_daily_and_hour_reservation(self):
        self.client.error = rpc.TransportError("unavailable")
        for _ in range(3):
            self.refused(self.response(self.issue()), 503, "near_key_unavailable")
        self.refused(self.response(self.issue()), 429, "near_verify_limit")
        with self.store._connection() as connection:
            self.assertEqual(tuple(connection.execute("SELECT count,minute_count FROM near_auth_rpc_days").fetchone()), (3, 3))
            self.assertEqual(connection.execute("SELECT count FROM near_auth_rpc_buckets").fetchone()[0], 3)
        self.assertEqual(len(self.client.calls), 3)

    def test_confirmed_missing_key_is_cached_across_restart_until_exact_expiry(self):
        self.client.error = rpc.RpcError({"cause": {"name": "UNKNOWN_ACCESS_KEY"}})
        self.refused(self.response(self.issue()), 401, "near_key_invalid")
        self.auth = near_auth.NearAuth(self.store, "https://siyab.xyz", client=self.client, clock=lambda: self.now)
        self.now += 59.999
        self.refused(self.response(self.issue()), 401, "near_key_invalid")
        self.assertEqual(len(self.client.calls), 1)
        self.now += .001
        self.client.error = None
        self.auth.verify(self.response(self.issue()), self.binding)
        self.assertEqual(len(self.client.calls), 2)

    def test_legacy_missing_key_result_is_invalid_and_cached_until_exact_expiry(self):
        valid = self.client.result
        result = {"block_hash": valid["block_hash"], "block_height": valid["block_height"],
                  "error": f"access key {self.public} does not exist while viewing", "logs": []}
        client = rpc.Client(rpc.MAINNET_RPC_URL, mode="off")
        self.auth = near_auth.NearAuth(self.store, "https://siyab.xyz", client=client, clock=lambda: self.now)
        with patch.object(client, "_exchange", return_value={"jsonrpc": "2.0", "id": "gate", "result": result}) as exchange:
            self.refused(self.response(self.issue()), 401, "near_key_invalid")
            self.auth = near_auth.NearAuth(self.store, "https://siyab.xyz", client=client, clock=lambda: self.now)
            self.now += 59.999
            self.refused(self.response(self.issue()), 401, "near_key_invalid")
            self.assertEqual(exchange.call_count, 1)
            self.now += .001
            exchange.return_value = {"jsonrpc": "2.0", "id": "gate", "result": valid}
            self.auth.verify(self.response(self.issue()), self.binding)
            self.assertEqual(exchange.call_count, 2)

    def test_other_legacy_key_errors_are_unavailable_and_not_cached(self):
        valid = self.client.result
        for error in (f"access key {self.public} does not exist while viewing extra",
                      "access key ed25519:other does not exist while viewing", "unavailable", None):
            self.now += 60
            self.client.result = {"block_hash": valid["block_hash"], "block_height": valid["block_height"],
                                  "error": error, "logs": []}
            with self.subTest(error=error):
                self.refused(self.response(self.issue()), 503, "near_key_unavailable")
                self.client.result = valid
                self.auth.verify(self.response(self.issue()), self.binding)
        self.assertEqual(len(self.client.calls), 8)

    def test_confirmed_negative_cache_binds_account_and_public_key(self):
        self.client.error = rpc.RpcError({"cause": {"name": "UNKNOWN_ACCESS_KEY"}})
        self.refused(self.response(self.issue()), 401, "near_key_invalid")
        self.client.error = None
        challenge = self.auth.issue("bobby.near", self.binding, client_address="192.0.2.1")
        self.auth.verify(self.response(challenge), self.binding)
        seed = bytes(reversed(ed25519.TEST_AGENT_SEED))
        response = self.response(self.issue())
        with self.store._connection() as connection:
            row = connection.execute("SELECT * FROM near_auth_challenges").fetchone()
        digest = hashlib.sha256(near_auth.serialize_payload(row["message"], row["nonce"], row["recipient"], row["callback_url"])).digest()
        response.update(publicKey=base58.encode_key(ed25519.public_from_seed(seed)),
                        signature=base64.b64encode(ed25519.sign(seed, digest)).decode("ascii"))
        self.auth.verify(response, self.binding)
        self.assertEqual(len(self.client.calls), 3)

    def test_confirmed_function_call_key_is_cached_without_rpc(self):
        self.client.result["permission"] = {"FunctionCall": {"allowance": None, "receiver_id": "alice.near", "method_names": []}}
        self.refused(self.response(self.issue()), 401, "near_key_invalid")
        self.client.result["permission"] = "FullAccess"
        self.refused(self.response(self.issue()), 401, "near_key_invalid")
        self.assertEqual(len(self.client.calls), 1)

    def test_transient_malformed_and_unknown_account_are_not_negative_cached(self):
        for error in (rpc.TransportError("unavailable"), rpc.MalformedResponse("bad"),
                      rpc.RpcError({"cause": {"name": "UNKNOWN_ACCOUNT"}})):
            self.now += 60
            self.client.error = error
            self.refused(self.response(self.issue()), 401 if isinstance(error, rpc.RpcError) else 503,
                         "near_key_invalid" if isinstance(error, rpc.RpcError) else "near_key_unavailable")
            self.client.error = None
            self.auth.verify(self.response(self.issue()), self.binding)
        self.assertEqual(len(self.client.calls), 6)

    def test_malformed_non_full_access_result_is_not_cached(self):
        valid = self.client.result
        self.client.result = {"permission": {"FunctionCall": {"allowance": None, "receiver_id": "alice.near", "method_names": []}}}
        self.refused(self.response(self.issue()), 503, "near_key_unavailable")
        self.client.result = valid
        self.auth.verify(self.response(self.issue()), self.binding)
        self.assertEqual(len(self.client.calls), 2)

    def test_default_open_challenge_cap_allows_eight_per_bucket(self):
        for _ in range(8):
            self.issue()
        with self.assertRaises(WorkspaceError) as caught:
            self.issue()
        self.assertEqual(caught.exception.code, "near_challenge_limit")

    def test_distinct_concurrent_verifies_cannot_exceed_rpc_bucket_limit(self):
        second = near_auth.NearAuth(self.store, "https://siyab.xyz", client=self.client, clock=lambda: self.now)
        candidates = [(auth, self.response(self.issue())) for auth in (self.auth, second)]
        barrier = threading.Barrier(2)

        def verify(candidate):
            auth, response = candidate
            barrier.wait(timeout=5)
            try:
                return auth.verify(response, self.binding)
            except WorkspaceError as error:
                return error.code

        with patch.object(near_auth, "MAX_VERIFY_RPC_PER_BUCKET_HOUR", 1):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(verify, candidates))
        self.assertEqual(results.count("near_verify_limit"), 1)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(len(self.client.calls), 1)

    def test_rpc_bucket_capacity_never_evicts_live_counters_and_rolls_at_exact_hour(self):
        self.now = self.now // 3600 * 3600
        with patch.object(near_auth, "MAX_VERIFY_RPC_PER_BUCKET_HOUR", 1), patch.object(near_auth, "MAX_VERIFY_BUCKETS", 1):
            self.auth.verify(self.response(self.issue()), self.binding)
            challenge = self.auth.issue("alice.near", self.binding, client_address="192.0.2.2")
            self.refused(self.response(challenge), 503, "near_verify_capacity")
            self.now += 3599.999
            self.refused(self.response(self.issue()), 429, "near_verify_limit")
            self.now += .001
            self.auth.verify(self.response(self.issue()), self.binding)
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM near_auth_rpc_buckets").fetchone()[0], 1)
        self.assertEqual(len(self.client.calls), 2)

    def test_negative_cache_is_bounded_and_cache_hit_spends_no_lookup(self):
        self.client.error = rpc.RpcError({"cause": {"name": "UNKNOWN_ACCESS_KEY"}})
        with patch.object(near_auth, "MAX_NEGATIVE_KEYS", 1):
            self.refused(self.response(self.issue()), 401, "near_key_invalid")
            with patch.object(near_auth, "MAX_VERIFY_RPC_PER_BUCKET_HOUR", 0):
                self.refused(self.response(self.issue()), 401, "near_key_invalid")
            self.now += 1
            challenge = self.auth.issue("bobby.near", self.binding, client_address="192.0.2.1")
            self.refused(self.response(challenge), 401, "near_key_invalid")
        with self.store._connection() as connection:
            rows = connection.execute("SELECT account_id FROM near_auth_negative_keys").fetchall()
            self.assertEqual(tuple(connection.execute("SELECT count,minute_count FROM near_auth_rpc_days").fetchone()), (2, 2))
        self.assertEqual([row[0] for row in rows], ["bobby.near"])
        self.assertEqual(len(self.client.calls), 2)

    def test_open_challenges_share_an_ipv6_48_bucket_across_service_restarts(self):
        with patch.object(near_auth, "MAX_CHALLENGES_PER_BUCKET", 2, create=True):
            for address in ("2001:db8:1234::1", "2001:db8:1234:ffff::2"):
                self.auth.issue("alice.near", self.binding, client_address=address)
            restarted = near_auth.NearAuth(self.store, "https://siyab.xyz", client=self.client, clock=lambda: self.now)
            with self.assertRaises(WorkspaceError) as caught:
                restarted.issue("alice.near", "c" * 43, client_address="2001:db8:1234:abcd::3")
            self.assertEqual((caught.exception.status, caught.exception.code), (429, "near_challenge_limit"))
            restarted.issue("alice.near", self.binding, client_address="2001:db8:1235::1")
            self.now += 300
            restarted.issue("alice.near", self.binding, client_address="2001:db8:1234::1")

    def test_ipv4_bucket_is_one_address_and_mapped_ipv6_cannot_bypass_it(self):
        with patch.object(near_auth, "MAX_CHALLENGES_PER_BUCKET", 1, create=True):
            self.issue()
            for address in ("192.0.2.1", "::ffff:192.0.2.1"):
                with self.subTest(address=address), self.assertRaises(WorkspaceError) as caught:
                    self.auth.issue("alice.near", self.binding, client_address=address)
                self.assertEqual(caught.exception.code, "near_challenge_limit")
            self.auth.issue("alice.near", self.binding, client_address="192.0.2.2")

    def test_client_address_must_be_a_transport_ip_address(self):
        for address in (None, "", "unknown", "192.0.2.1, 192.0.2.2", "2001:db8::1%zone", 123):
            with self.subTest(address=address), self.assertRaises(WorkspaceError) as caught:
                self.auth.issue("alice.near", self.binding, client_address=address)
            self.assertEqual((caught.exception.status, caught.exception.code), (400, "invalid_near_client"))

    def test_parallel_issue_cannot_exceed_a_bucket_cap(self):
        second = near_auth.NearAuth(self.store, "https://siyab.xyz", client=self.client, clock=lambda: self.now)
        barrier = threading.Barrier(2)

        def issue(auth):
            barrier.wait(timeout=5)
            try:
                return auth.issue("alice.near", self.binding, client_address="192.0.2.1")
            except WorkspaceError as error:
                return error.code

        with patch.object(near_auth, "MAX_CHALLENGES_PER_BUCKET", 1, create=True):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(issue, (self.auth, second)))
        self.assertEqual(results.count("near_challenge_limit"), 1)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)

    def test_old_challenge_schema_is_migrated_by_expiring_unbucketed_challenges(self):
        with self.store._connection(write=True) as connection:
            connection.execute("DROP TABLE near_auth_challenges")
            connection.execute("CREATE TABLE near_auth_challenges ("
                               "id_hash TEXT PRIMARY KEY, binding_hash TEXT NOT NULL, account_id TEXT NOT NULL, "
                               "nonce BLOB NOT NULL, message TEXT NOT NULL, recipient TEXT NOT NULL, "
                               "callback_url TEXT NOT NULL, created_ms INTEGER NOT NULL, expires_at INTEGER NOT NULL)")
            connection.execute("INSERT INTO near_auth_challenges VALUES (?,?,?,?,?,?,?,?,?)",
                               ("old", "binding", "alice.near", bytes(32), "old", self.auth.origin,
                                self.auth.callback_url, int(self.now * 1000), int(self.now * 1000) + 300000))
        self.auth = near_auth.NearAuth(self.store, "https://siyab.xyz", client=self.client, clock=lambda: self.now)
        self.issue()
        with self.store._connection() as connection:
            rows = connection.execute("SELECT client_bucket FROM near_auth_challenges").fetchall()
        self.assertEqual([row[0] for row in rows], ["192.0.2.1/32"])

    def test_eth_implicit_accounts_are_explicitly_rejected(self):
        with self.assertRaises(WorkspaceError) as caught:
            self.auth.issue("0x" + "a" * 40, self.binding, client_address="192.0.2.1")
        self.assertEqual(caught.exception.code, "invalid_near_account")
        self.assertIn("ETH-implicit", str(caught.exception))

    def response(self, challenge, **changes):
        payload = {"message": challenge["message"], "nonce": base64.b64decode(challenge["nonce"]),
                   "recipient": challenge["recipient"], "callback_url": challenge["callbackUrl"]}
        payload.update(changes)
        digest = hashlib.sha256(near_auth.serialize_payload(**payload)).digest()
        signature = ed25519.sign(ed25519.TEST_AGENT_SEED, digest)
        return {"challenge_id": challenge["challenge_id"], "accountId": challenge["account_id"],
                "publicKey": self.public, "signature": base64.b64encode(signature).decode("ascii"),
                "state": challenge["state"]}

    def refused(self, response, status, code, *, binding=None):
        with self.assertRaises(WorkspaceError) as caught:
            self.auth.verify(response, self.binding if binding is None else binding)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))
        with self.assertRaises(WorkspaceError) as replay:
            self.auth.verify(response, self.binding)
        self.assertEqual((replay.exception.status, replay.exception.code), (401, "near_challenge_invalid"))

    def test_issue_has_random_nonce_origin_account_and_five_minute_expiry(self):
        first, second = self.issue(), self.issue()
        self.assertEqual(first["recipient"], "https://siyab.xyz")
        self.assertEqual(first["callbackUrl"], "https://siyab.xyz/workspace.html")
        self.assertEqual(first["account_id"], "alice.near")
        self.assertEqual(first["state"], first["challenge_id"])
        self.assertEqual(len(base64.b64decode(first["nonce"], validate=True)), 32)
        self.assertEqual(first["expires_at"] - int(self.now * 1000), 300000)
        self.assertIn(first["challenge_id"], first["message"])
        self.assertIn("alice.near", first["message"])
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertNotEqual(first["challenge_id"], second["challenge_id"])
        with self.store._connection() as connection:
            stored = "\n".join(connection.iterdump())
        self.assertNotIn(self.binding, stored)

    def test_current_full_access_key_authenticates_and_replay_fails(self):
        response = self.response(self.issue())
        self.assertEqual(self.auth.verify(response, self.binding), {"account_id": "alice.near", "public_key": self.public})
        self.assertEqual(self.client.calls, [("query", {"request_type": "view_access_key", "finality": "final",
                                                        "account_id": "alice.near", "public_key": self.public})])
        self.refused(response, 401, "near_challenge_invalid")
        self.assertEqual(len(self.client.calls), 1)

    def test_non_redirect_wallet_may_omit_state(self):
        response = self.response(self.issue())
        del response["state"]
        self.assertEqual(self.auth.verify(response, self.binding)["account_id"], "alice.near")

    def test_challenge_survives_restart_then_consumption_does_too(self):
        response = self.response(self.issue())
        self.auth = near_auth.NearAuth(SQLiteWorkspaceStore(self.store.path), "https://siyab.xyz",
                                       client=self.client, clock=lambda: self.now)
        self.auth.verify(response, self.binding)
        self.auth = near_auth.NearAuth(SQLiteWorkspaceStore(self.store.path), "https://siyab.xyz",
                                       client=self.client, clock=lambda: self.now)
        self.refused(response, 401, "near_challenge_invalid")

    def test_wrong_nonce_fails_before_rpc_and_is_consumed(self):
        self.refused(self.response(self.issue(), nonce=bytes(32)), 401, "near_signature_invalid")
        self.assertEqual(self.client.calls, [])

    def test_wrong_recipient_fails_before_rpc_and_is_consumed(self):
        self.refused(self.response(self.issue(), recipient="https://other.example"), 401, "near_signature_invalid")
        self.assertEqual(self.client.calls, [])

    def test_wrong_message_fails_before_rpc_and_is_consumed(self):
        self.refused(self.response(self.issue(), message="Sign in elsewhere"), 401, "near_signature_invalid")
        self.assertEqual(self.client.calls, [])

    def test_wrong_callback_fails_before_rpc_and_is_consumed(self):
        for callback in (None, "https://other.example/workspace.html"):
            with self.subTest(callback=callback):
                self.refused(self.response(self.issue(), callback_url=callback), 401, "near_signature_invalid")
        self.assertEqual(self.client.calls, [])

    def test_expired_nonce_fails_including_exact_expiry(self):
        for age in (300, 301):
            response = self.response(self.issue())
            self.now += age
            with self.subTest(age=age):
                self.refused(response, 401, "near_challenge_invalid")
        self.assertEqual(self.client.calls, [])

    def test_clock_rollback_fails(self):
        response = self.response(self.issue())
        self.now -= 1
        self.refused(response, 401, "near_challenge_invalid")
        self.assertEqual(self.client.calls, [])

    def test_expiry_during_rpc_fails(self):
        response = self.response(self.issue())
        self.client.on_call = lambda: setattr(self, "now", self.now + 300)
        self.refused(response, 401, "near_challenge_invalid")
        self.assertEqual(len(self.client.calls), 1)

    def test_wrong_browser_binding_fails_and_is_consumed(self):
        self.refused(self.response(self.issue()), 401, "near_challenge_invalid", binding="c" * 43)
        self.assertEqual(self.client.calls, [])

    def test_malformed_binding_fails_and_is_consumed(self):
        self.refused(self.response(self.issue()), 401, "near_challenge_invalid", binding="wrong")
        self.assertEqual(self.client.calls, [])

    def test_changed_service_origin_rejects_old_challenge(self):
        response = self.response(self.issue())
        self.auth = near_auth.NearAuth(self.store, "https://other.example", client=self.client, clock=lambda: self.now)
        self.refused(response, 401, "near_challenge_invalid")

    def test_wrong_account_fails_and_is_consumed(self):
        response = self.response(self.issue())
        response["accountId"] = "bobby.near"
        self.refused(response, 401, "near_account_mismatch")
        self.assertEqual(self.client.calls, [])

    def test_bad_state_fails_and_is_consumed(self):
        for state in ("c" * 43, None, "é" * 43, 3):
            response = self.response(self.issue())
            response["state"] = state
            with self.subTest(state=state):
                self.refused(response, 401, "near_challenge_invalid")
        self.assertEqual(self.client.calls, [])

    def test_deleted_key_fails_and_is_consumed(self):
        self.client.error = rpc.RpcError({"cause": {"name": "UNKNOWN_ACCESS_KEY"}})
        self.refused(self.response(self.issue()), 401, "near_key_invalid")

    def test_deleted_account_fails_and_is_consumed(self):
        self.client.error = rpc.RpcError({"cause": {"name": "UNKNOWN_ACCOUNT"}})
        self.refused(self.response(self.issue()), 401, "near_key_invalid")

    def test_function_call_and_gas_keys_are_refused(self):
        for permission in ({"FunctionCall": {"allowance": None, "receiver_id": "alice.near", "method_names": []}},
                           {"GasKeyFullAccess": {"balance": "1", "num_nonces": 1}}):
            self.client.result["permission"] = permission
            with self.subTest(permission=permission):
                self.refused(self.response(self.issue()), 401, "near_key_invalid")

    def test_key_is_read_afresh_for_each_login(self):
        self.auth.verify(self.response(self.issue()), self.binding)
        self.client.error = rpc.RpcError({"cause": {"name": "UNKNOWN_ACCESS_KEY"}})
        self.refused(self.response(self.issue()), 401, "near_key_invalid")
        self.assertEqual(len(self.client.calls), 2)

    def test_rpc_errors_fail_closed_and_consume_challenge(self):
        errors = (rpc.RpcError({"cause": {"name": "NO_SYNCED_BLOCKS"}}), rpc.HttpError(429, "https://rpc.example"),
                  rpc.TransportError("truncated"), rpc.RateLimited("budget"), rpc.MalformedResponse("malformed"),
                  TimeoutError("deadline"), OSError("unreachable"), ValueError("invalid JSON"))
        for error in errors:
            self.now += 60
            self.client.error = error
            with self.subTest(error=type(error).__name__):
                self.refused(self.response(self.issue()), 503, "near_key_unavailable")

    def test_malformed_rpc_results_fail_closed(self):
        for result in ({"permission": "FullAccess"}, {"permission": "FullAccess", "nonce": True,
                                                    "block_height": 100, "block_hash": "not-a-hash"},
                       {**self.client.result, "permission": "full_access"},
                       {**self.client.result, "permission": None}, None):
            self.now += 60
            self.client.result = result
            with self.subTest(result=result):
                self.refused(self.response(self.issue()), 503, "near_key_unavailable")

    def test_malformed_public_keys_fail_before_rpc(self):
        for value in (None, 3, self.public.replace("ed25519:", "secp256k1:"), "ed25519:" + "1" * 31,
                      "ed25519:" + "0" * 32, self.public + "1", "ed25519:" + "1" * 1000):
            response = self.response(self.issue())
            response["publicKey"] = value
            with self.subTest(public=value):
                self.refused(response, 400, "invalid_near_signature")
        self.assertEqual(self.client.calls, [])

    def test_malformed_signatures_fail_before_rpc(self):
        for value in (None, 3, "x" * 88, "A" * 86 + "=!", base64.b64encode(bytes(63)).decode(),
                      base64.b64encode(bytes(65)).decode()):
            response = self.response(self.issue())
            response["signature"] = value
            with self.subTest(signature=value):
                self.refused(response, 400, "invalid_near_signature")
        self.assertEqual(self.client.calls, [])

    def test_malleable_signature_scalar_is_refused(self):
        response = self.response(self.issue())
        signed = base64.b64decode(response["signature"])
        scalar = int.from_bytes(signed[32:], "little") + ed25519.l
        response["signature"] = base64.b64encode(signed[:32] + scalar.to_bytes(32, "little")).decode()
        self.refused(response, 401, "near_signature_invalid")
        self.assertEqual(self.client.calls, [])

    def test_identity_and_small_order_public_keys_are_refused(self):
        encodings = ((1).to_bytes(32, "little"), bytes(32), (ed25519.q - 1).to_bytes(32, "little"))
        for encoded in encodings:
            response = self.response(self.issue())
            response["publicKey"] = base58.encode_key(encoded)
            response["signature"] = base64.b64encode((1).to_bytes(32, "little") + bytes(32)).decode()
            with self.subTest(public=encoded.hex()):
                self.refused(response, 401, "near_signature_invalid")
        self.assertEqual(self.client.calls, [])

    def test_torsion_component_on_otherwise_valid_public_key_is_refused(self):
        point = ed25519.decodepoint(base58.decode_key(self.public))
        mixed = ed25519.encodepoint(ed25519.edwards(point, [0, ed25519.q - 1]))
        response = self.response(self.issue())
        response["publicKey"] = base58.encode_key(mixed)
        self.refused(response, 401, "near_signature_invalid")
        self.assertEqual(self.client.calls, [])

    def test_small_order_or_noncanonical_signature_r_is_refused(self):
        for encoded in (bytes(32), (1).to_bytes(32, "little"), ed25519.q.to_bytes(32, "little")):
            response = self.response(self.issue())
            signature = base64.b64decode(response["signature"])
            response["signature"] = base64.b64encode(encoded + signature[32:]).decode()
            with self.subTest(r=encoded.hex()):
                self.refused(response, 401, "near_signature_invalid")
        self.assertEqual(self.client.calls, [])

    def test_malformed_or_unknown_challenge_fails(self):
        for response in (None, [], {}, {"challenge_id": None}, {"challenge_id": "wrong"}):
            with self.subTest(response=response), self.assertRaises(WorkspaceError) as caught:
                self.auth.verify(response, self.binding)
            self.assertEqual((caught.exception.status, caught.exception.code), (400, "invalid_near_response"))
        self.refused({"challenge_id": "a" * 43}, 401, "near_challenge_invalid")

    def test_extra_and_missing_response_fields_consume_challenge(self):
        for field in ("accountId", "publicKey", "signature", "extra"):
            response = self.response(self.issue())
            if field == "extra":
                response[field] = "unexpected"
            else:
                del response[field]
            with self.subTest(field=field):
                self.refused(response, 400, "invalid_near_response")
        self.assertEqual(self.client.calls, [])

    def test_invalid_and_testnet_accounts_are_rejected(self):
        for account in (None, 2, "Alice.near", "a", "a" * 65, "alice..near", "alice.testnet", "testnet", "é.near"):
            with self.subTest(account=account), self.assertRaises(WorkspaceError) as caught:
                self.auth.issue(account, self.binding, client_address="192.0.2.1")
            self.assertEqual(caught.exception.code, "invalid_near_account")
        self.assertEqual(self.auth.issue("a" * 64, self.binding, client_address="192.0.2.1")["account_id"], "a" * 64)

    def test_strict_origin_and_live_rpc_configuration(self):
        invalid = ("http://siyab.xyz", "https://siyab.xyz/", "https://user@siyab.xyz", "https://siyab.xyz?q=1",
                   "https://siyab.xyz#x", "https://siyab.xyz:bad", "https://siyab.xyz:0", "https://siyab.xyz\x7f", None)
        for origin in invalid:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                near_auth.NearAuth(self.store, origin, client=self.client)
        for origin in ("https://siyab.xyz", "http://127.0.0.1:8792", "http://localhost:8792", "http://[::1]:8792"):
            with self.subTest(origin=origin):
                self.assertEqual(near_auth.NearAuth(self.store, origin, client=self.client).origin, origin)
        for mode in ("record", "replay"):
            self.client.mode = mode
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                near_auth.NearAuth(self.store, "https://siyab.xyz", client=self.client)

    def test_fixture_mode_environment_does_not_enable_replay(self):
        with patch.dict("os.environ", {"GATE_FIXTURES_MODE": "replay"}):
            auth = near_auth.NearAuth(self.store, "https://siyab.xyz")
        self.assertEqual(auth.client.mode, "off")
        self.assertEqual(auth.client.kind, "rpc")
        self.assertEqual(auth.client.url, rpc.MAINNET_RPC_URL)

    def test_client_changed_to_replay_fails_before_rpc(self):
        response = self.response(self.issue())
        self.client.mode = "replay"
        self.refused(response, 503, "near_key_unavailable")
        self.assertEqual(self.client.calls, [])

    def test_parallel_verification_only_one_succeeds_across_services(self):
        response = self.response(self.issue())
        second = near_auth.NearAuth(SQLiteWorkspaceStore(self.store.path), "https://siyab.xyz",
                                    client=self.client, clock=lambda: self.now)
        barrier = threading.Barrier(2)

        def verify(auth):
            barrier.wait(timeout=5)
            try:
                return auth.verify(response, self.binding)
            except WorkspaceError as error:
                return error.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(verify, (self.auth, second)))
        self.assertEqual(results.count("near_challenge_invalid"), 1)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(len(self.client.calls), 1)

    def test_expired_challenges_are_pruned_and_capacity_is_bounded(self):
        self.issue()
        with patch.object(near_auth, "MAX_CHALLENGES", 1):
            with self.assertRaises(WorkspaceError) as caught:
                self.issue()
            self.assertEqual((caught.exception.status, caught.exception.code), (503, "near_challenge_capacity"))
            self.now += 300
            self.issue()
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM near_auth_challenges").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
