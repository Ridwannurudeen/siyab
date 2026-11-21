"""Parity against public responses kept outside the replay fixture directories."""

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gate import rpc, rules

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data" / "nearblocks_parity"


def _event(value):
    if value is None or not value:
        return value
    timestamp, transaction_hash = value.get("block_timestamp"), value.get("transaction_hash")
    return {"block_timestamp": str(timestamp) if timestamp is not None else None,
            "transaction_hash": transaction_hash}


def _keys_read(body):
    keys, creations = {}, []
    for row in body["keys"]:
        created = _event(row["created"])
        if created is not None and created.get("block_timestamp") is not None:
            creations.append(created)
        keys.setdefault(row["public_key"], []).append({"permission_kind": row["permission_kind"],
                                                     "created": created,
                                                     "deleted": _event(row["deleted"])})
    return {"key_count": len(body["keys"]),
            "oldest_creation": min(creations, key=lambda row: int(row["block_timestamp"])) if creations else None,
            "keys": {key: sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
                     for key, rows in keys.items()}}


def _txn_read(body):
    transactions = []
    for row in body["txns"]:
        actions = []
        for action in row["actions"]:
            args = action["args"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = None
            if not isinstance(args, dict):
                args = None
            actions.append({"action": action["action"], "method": action["method"], "args": args})
        with localcontext() as context:
            context.prec = 80
            deposit = format(Decimal(str(row["actions_agg"]["deposit"])).normalize(), "f")
        transactions.append({"transaction_hash": row["transaction_hash"],
                             "signer_account_id": row["signer_account_id"],
                             "receiver_account_id": row["receiver_account_id"],
                             "block_height": row["block"]["block_height"],
                             "deposit": deposit, "status": row["outcomes"]["status"],
                             "actions": actions})
    return {"txns": transactions}


def _differences(v1, v3, path=""):
    differences = []
    if isinstance(v1, dict) and isinstance(v3, dict):
        for name in sorted(v1.keys() | v3.keys()):
            child_path = f"{path}.{name}" if path else name
            if name not in v1 or name not in v3:
                differences.append({"path": child_path, "v1": v1.get(name), "v3": v3.get(name)})
            else:
                differences.extend(_differences(v1[name], v3[name], child_path))
    elif isinstance(v1, list) and isinstance(v3, list) and len(v1) == len(v3):
        for index, (old, new) in enumerate(zip(v1, v3)):
            differences.extend(_differences(old, new, f"{path}[{index}]"))
    elif type(v1) is not type(v3) or v1 != v3:
        differences.append({"path": path, "v1": v1, "v3": v3})
    return differences


class KeysProjectionTest(unittest.TestCase):
    def test_reordering_tied_oldest_keys_changes_the_consumed_creation(self):
        timestamp = 1_000_000_000_000_000_000
        first = {"public_key": "first", "permission_kind": "FULL_ACCESS",
                 "created": {"block": {"block_timestamp": str(timestamp)}, "transaction_hash": "1" * 32},
                 "deleted": None}
        second = {**first, "public_key": "second",
                  "created": {"block": {"block_timestamp": str(timestamp)}, "transaction_hash": "2" * 32}}
        for page in ([first, second], [second, first]):
            with self.subTest(first=page[0]["public_key"]):
                client = SimpleNamespace(get=mock.Mock(return_value={"data": page}))
                body, _, _ = rpc.nearblocks_keys(client, "keys.near")
                creations = rules._key_creations(body, now_ms=timestamp // 1_000_000)
                expected = [(timestamp, key["created"]["transaction_hash"]) for key in page]
                self.assertEqual(creations, expected)
                self.assertEqual(min(creations, key=lambda row: row[0]), expected[0])
                client.get.assert_called_once_with("/v3/accounts/keys.near/keys")

    def test_no_creation_event_keeps_the_selected_creation_unresolved(self):
        for created in (None, {}):
            with self.subTest(created=created):
                raw = {"data": [{"public_key": "first", "permission_kind": "FULL_ACCESS",
                                 "created": created, "deleted": None}]}
                body, _, _ = rpc.nearblocks_keys(SimpleNamespace(get=lambda _path: raw), "keys.near")
                self.assertEqual(rules._key_creations(body, now_ms=1_000_000_000_000), [(None, None)])
        for data in (None, []):
            with self.subTest(data=data):
                body, _, _ = rpc.nearblocks_keys(SimpleNamespace(get=lambda _path: {"data": data}), "keys.near")
                self.assertEqual(rules._key_creations(body, now_ms=1_000_000_000_000), [])

    def test_parity_distinguishes_null_empty_and_all_none_events(self):
        events = (None, {}, {"block_timestamp": None, "transaction_hash": None})
        for event in events:
            with self.subTest(event=event):
                self.assertEqual(_event(event), event)
        for index, event in enumerate(events):
            for other in events[index + 1:]:
                with self.subTest(event=event, other=other):
                    self.assertNotEqual(_event(event), _event(other))


class CollectorResumeTest(unittest.TestCase):
    def test_a_changed_recording_refuses_resume_without_replacing_the_baseline(self):
        from tests.data.nearblocks_parity import collect

        recordings = json.loads((DATA / "manifest.json").read_bytes())["recordings"]
        changed = copy.deepcopy(recordings)
        changed[0]["fixture_sha256"] = "changed"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            original = json.dumps({"recordings": recordings, "captures": []}).encode("utf-8")
            path.write_bytes(original)
            with mock.patch.object(collect, "inventory", return_value=changed), \
                    mock.patch.object(collect, "MANIFEST", path), \
                    mock.patch.object(collect.rpc, "Client", side_effect=AssertionError("network opened")) as client:
                with self.assertRaisesRegex(SystemExit, "inventory changed"):
                    collect.main()
                client.assert_not_called()
            self.assertEqual(path.read_bytes(), original)


class NearBlocksParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((DATA / "manifest.json").read_bytes())
        cls.captures = {row["id"]: row for row in cls.manifest["captures"]}

    def test_all_committed_nearblocks_recordings_keep_their_exact_bytes(self):
        # 173 v1 recordings plus 2 /v3/accounts/.../txns recordings; test_rpc covers the v1 subset.
        self.assertEqual(len(self.manifest["recordings"]), 175)
        for row in self.manifest["recordings"]:
            with self.subTest(fixture=row["fixture"]):
                self.assertEqual(hashlib.sha256((ROOT / row["fixture"]).read_bytes()).hexdigest(),
                                 row["fixture_sha256"])

    def test_every_transaction_recording_has_a_successful_v3_capture(self):
        recordings = {row["fixture"] for row in self.manifest["recordings"] if row["kind"] == "txn"}
        captures = [row for row in self.captures.values() if row["kind"] == "txn"]
        self.assertEqual(len(recordings), 69)
        self.assertEqual(len(captures), 69)
        self.assertEqual({row["fixture"] for row in captures}, recordings)
        for row in captures:
            with self.subTest(capture=row["id"]):
                self.assertEqual(row["status"], 200)
                self.assertEqual(row["version"], "v3")

    def test_at_least_ten_keys_recordings_have_both_live_versions(self):
        keys = [row for row in self.captures.values() if row["kind"] == "keys"]
        fixtures = {row["fixture"] for row in keys}
        self.assertGreaterEqual(len(fixtures), 10)
        for fixture in fixtures:
            rows = [row for row in keys if row["fixture"] == fixture]
            with self.subTest(fixture=fixture):
                self.assertEqual({row["version"] for row in rows}, {"v1", "v3"})
                self.assertTrue(all(row["status"] == 200 for row in rows))

    def test_capture_digests_bind_exact_server_bytes(self):
        inventory = {row["fixture"]: row for row in self.manifest["recordings"]}
        archive_path = DATA / self.manifest["initial_capture_archive"]
        archive = json.loads(archive_path.read_bytes())
        self.assertEqual(archive["recordings"], self.manifest["recordings"])
        self.assertEqual(len(archive["captures"]), 16)
        captures = ([(DATA, row) for row in self.captures.values()]
                    + [(archive_path.parent, row) for row in archive["captures"]])
        for directory, row in captures:
            with self.subTest(capture=row["id"]):
                raw = (directory / row["response_file"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), row["response_sha256"])
                self.assertIsInstance(json.loads(raw), dict)
                self.assertEqual(row["source"]["request_sha256"],
                                 rpc.fixture_key(row["request_url"], "GET", None))
                self.assertEqual(row["fixture_sha256"], inventory[row["fixture"]]["fixture_sha256"])

    def test_primary_nearblocks_calls_are_at_least_eleven_seconds_apart(self):
        self.assertEqual(self.manifest["interval_seconds"], 11)
        captures = self.manifest["captures"]
        for old, new in zip(captures, captures[1:]):
            with self.subTest(capture=new["id"]):
                if old["capture_run"] == new["capture_run"]:
                    self.assertGreaterEqual(new["requested_monotonic"] - old["requested_monotonic"], 11)
                else:
                    elapsed = (datetime.fromisoformat(new["requested_at"])
                               - datetime.fromisoformat(old["requested_at"])).total_seconds()
                    self.assertGreaterEqual(elapsed, 11)

    def test_helpers_keep_raw_answers_and_the_endpoint_method(self):
        for row in self.captures.values():
            if row["version"] != "v3":
                continue
            with self.subTest(capture=row["id"]):
                raw = json.loads((DATA / row["response_file"]).read_bytes())
                before = copy.deepcopy(raw)
                client = SimpleNamespace(get=lambda _path: raw, last_source=row["source"])
                if row["kind"] == "txn":
                    subject = row["request_url"].rsplit("/", 1)[1]
                    _, returned_raw, method = rpc.nearblocks_txn(client, subject)
                    self.assertEqual(method, "GET /v3/txns/{transaction_hash}")
                else:
                    subject = row["request_url"].split("/accounts/", 1)[1].split("/", 1)[0]
                    _, returned_raw, method = rpc.nearblocks_keys(client, subject)
                    self.assertEqual(method, "GET /v3/accounts/{account_id}/keys")
                self.assertIs(returned_raw, raw)
                self.assertEqual(raw, before)

    def test_transaction_deposit_differences_are_v1_float_rounding(self):
        """Immutable v1 numeric deposits lost digits that the v3 answers retain."""
        count, precision_losses = 0, 0
        for row in self.captures.values():
            if row["kind"] != "txn":
                continue
            with self.subTest(capture=row["id"], fixture=row["fixture"]):
                raw = json.loads((DATA / row["response_file"]).read_bytes())
                client = SimpleNamespace(get=lambda _path: raw, last_source=row["source"])
                historical = json.loads((ROOT / row["fixture"]).read_bytes())["response"]
                subject = row["request_url"].rsplit("/", 1)[1]
                body, _, _ = rpc.nearblocks_txn(client, subject)
                v1 = historical["txns"][0]["actions_agg"]["deposit"]
                v3 = body["txns"][0]["actions_agg"]["deposit"]
                if Decimal(str(v1)) != Decimal(v3):
                    self.assertEqual(float(int(v3)), v1)
                    precision_losses += 1
                count += 1
        self.assertEqual(count, 69)
        self.assertEqual(precision_losses, 2)

    def test_transaction_differences_are_exactly_the_reviewed_precision_losses(self):
        expected = json.loads((DATA / "differences.json").read_bytes())
        for capture, differences in expected["transaction_differences"].items():
            for difference in differences:
                with self.subTest(capture=capture):
                    self.assertEqual(difference["path"], "txns[0].deposit")
        observed = {}
        count = 0
        for row in self.captures.values():
            if row["kind"] != "txn":
                continue
            raw = json.loads((DATA / row["response_file"]).read_bytes())
            client = SimpleNamespace(get=lambda _path: raw, last_source=row["source"])
            historical = json.loads((ROOT / row["fixture"]).read_bytes())["response"]
            subject = row["request_url"].rsplit("/", 1)[1]
            body, _, _ = rpc.nearblocks_txn(client, subject)
            differences = _differences(_txn_read(historical), _txn_read(body))
            if differences:
                observed[row["id"]] = differences
            count += 1
        self.assertEqual(count, 69)
        self.assertEqual(observed, expected["transaction_differences"])

    def test_keys_differences_are_exactly_the_reviewed_live_differences(self):
        expected = json.loads((DATA / "differences.json").read_bytes())
        observed = {}
        historical_keys_count, current_keys_count = 0, 0
        for row in self.captures.values():
            if row["version"] != "v3" or row["kind"] != "keys":
                continue
            raw = json.loads((DATA / row["response_file"]).read_bytes())
            client = SimpleNamespace(get=lambda _path: raw, last_source=row["source"])
            historical = json.loads((ROOT / row["fixture"]).read_bytes())["response"]
            subject = row["request_url"].split("/accounts/", 1)[1].split("/", 1)[0]
            body, _, _ = rpc.nearblocks_keys(client, subject)
            differences = _differences(_keys_read(historical), _keys_read(body))
            historical_keys_count += 1
            v1 = self.captures[row["id"].replace("-v3", "-v1")]
            current = json.loads((DATA / v1["response_file"]).read_bytes())
            current_differences = _differences(_keys_read(current), _keys_read(body))
            if current_differences:
                observed[row["id"] + "-current-v1"] = current_differences
            current_keys_count += 1
            if differences:
                observed[row["id"] + "-recorded-v1"] = differences
        self.assertEqual((historical_keys_count, current_keys_count), (10, 10))
        self.assertEqual(observed, expected["differences"])


if __name__ == "__main__":
    unittest.main()
