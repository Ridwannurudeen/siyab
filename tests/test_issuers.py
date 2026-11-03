import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from intel import issuers, labels


class IssuerRowsTest(unittest.TestCase):
    def test_transaction_recording_retries_and_reduces_signatures_at_write_time(self):
        response = {"transactions": [{"transaction": {"signature": "outer", "hash": "tx"},
                                     "receipts": [{"signature": "receipt", "receipt": {
                                         "receipt": {"Action": {"actions": [{"Delegate": {
                                             "signature": "delegate", "actions": []}}]}}}}]}]}
        expected = {"transactions": [{"transaction": {"hash": "tx"},
                                      "receipts": [{"receipt": {
                                          "receipt": {"Action": {"actions": [{"Delegate": {
                                              "actions": []}}]}}}}]}]}
        client = Mock()
        client.post.side_effect = [issuers.rpc.HttpError(429, "url"), response]
        client.last_source = {"fetched_at": "2026-11-03T00:00:00Z"}
        sleeps = []
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(issuers.rpc, "Client", return_value=client):
            recorded = issuers.record_transaction_request("/v0/transactions", {"tx_hashes": ["tx"]},
                                                           fixtures_dir=tmp, sleep=sleeps.append,
                                                           progress=lambda _: None)
            [fixture_path] = list(Path(tmp).glob("*.json"))
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        self.assertEqual(recorded, expected)
        self.assertEqual(fixture["response"], expected)
        self.assertEqual(fixture["reduced"],
                         "every signature field dropped at record time; nothing else changed")
        self.assertEqual(sleeps, [10])
        self.assertEqual(client.post.call_count, 2)

    def test_history_rows_name_the_transaction_the_block_and_the_method(self):
        rows = issuers.history_rows()
        self.assertGreater(len(rows), 0)
        self.assertEqual(issuers.HISTORY_PATH.read_text(encoding="utf-8"), issuers.history_rows_jsonl())
        phrases = {
            "add_to_blacklist": "added to the issuer blacklist of",
            "remove_from_blacklist": "removed from the issuer blacklist of",
            "destroy_black_funds": "black funds destroyed by the issuer of",
        }
        for row in rows:
            self.assertEqual(row["category"], "watch")
            self.assertEqual(row["provenance"], "onchain")
            self.assertEqual(row["source"]["method"], "POST /v0/transactions")
            self.assertIn(f"at block {row['source']['block']}", row["label"])
            [method] = [method for method in phrases if method in row["note"]]
            self.assertIn(phrases[method], row["label"])
            [tx_hash] = row["source"]["args"]["tx_hashes"]
            self.assertIn(f"transaction {tx_hash}", row["label"])

    def test_every_multisig_transaction_on_the_page_has_recorded_details(self):
        page_path = next(issuers.FIXTURE_DIR.glob("bf0b*.json"))
        page = json.loads(page_path.read_text(encoding="utf-8"))["response"]
        page_hashes = {row["transaction_hash"] for row in page["account_txs"]}
        detail_hashes = set()
        for path in issuers.FIXTURE_DIR.glob("*.json"):
            fixture = json.loads(path.read_text(encoding="utf-8"))
            if (fixture.get("reduced") == issuers.TRANSACTION_REDUCED
                    and "transactions" in fixture["response"]):
                detail_hashes.update(txn["transaction"]["hash"]
                                     for txn in fixture["response"]["transactions"])

        self.assertEqual(page["txs_count"], len(page["account_txs"]))
        self.assertNotIn("resume_token", page)
        self.assertEqual(page_hashes, detail_hashes)
        self.assertEqual(issuers.history_coverage(), {
            "transactions": 83, "txs_count": 83, "tx_block_min": 73185391,
            "tx_block_max": 202996702,
            "actions": {"add_to_blacklist": 11, "destroy_black_funds": 9,
                        "remove_from_blacklist": 0},
        })

    def test_rows_command_rewrites_both_files(self):
        status_rows = [issuers.seed.make_row(
            entity="issuer", subject="status.test", chain="near", label="status row",
            category="watch", provenance="onchain",
            source={"rpc": issuers.RPC_URL, "method": "status", "args": {}, "block": 1},
            observed_at="2026-11-03T00:00:00Z", note="status row")]
        history_rows = [issuers.seed.make_row(
            entity="issuer", subject="history.test", chain="near", label="history row",
            category="watch", provenance="onchain",
            source={"rpc": issuers.RPC_URL, "method": "history", "args": {}, "block": 2},
            observed_at="2026-11-03T00:00:00Z", note="history row")]
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.jsonl"
            history_path = Path(tmp) / "history.jsonl"
            with patch.object(issuers, "STATUS_PATH", status_path), \
                    patch.object(issuers, "HISTORY_PATH", history_path), \
                    patch.object(issuers, "rows", return_value=status_rows), \
                    patch.object(issuers, "history_rows", return_value=history_rows), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(issuers.main(["rows"]), 0)

            self.assertEqual(status_path.read_text(encoding="utf-8"), labels.row_line(status_rows[0]) + "\n")
            self.assertEqual(history_path.read_text(encoding="utf-8"), labels.row_line(history_rows[0]) + "\n")
        self.assertIn("1 issuer status rows, 1 issuer history rows", output.getvalue())

    def test_a_non_existent_account_id_is_recorded_as_such_with_no_inference(self):
        subject = "missing.test"
        block = 500
        args = json.dumps({"account_id": subject}, separators=(",", ":")).encode("utf-8")
        action_args = base64.b64encode(args).decode("ascii")
        body = {"tx_hashes": ["tx-missing"]}
        tx_url = issuers.FASTNEAR_TX_URL + "/v0/transactions"
        tx_response = {"transactions": [{"transaction": {"hash": "tx-missing"}, "receipts": [{
            "receipt": {"block_height": block, "predecessor_id": "tether.multisafe.near",
                        "receiver_id": "usdt.tether-token.near", "receipt_id": "receipt-missing",
                        "receipt": {"Action": {"actions": [{"FunctionCall": {
                            "method_name": "add_to_blacklist", "args": action_args}}]}}},
            "execution_outcome": {"outcome": {"status": {"SuccessValue": ""}}}}]}]}
        params = issuers.rpc.body_params(body)
        tx_key = issuers.rpc.fixture_key(tx_url, "POST", params)
        account_params = {"request_type": "view_account", "block_id": block, "account_id": subject}
        account_key = issuers.rpc.fixture_key(issuers.RPC_URL, "query", account_params)
        account_fixture = {"request": {"method": "query", "params": account_params},
                           "response": {"jsonrpc": "2.0", "id": "gate",
                                        "error": {"cause": {"name": "UNKNOWN_ACCOUNT"}}},
                           "url": issuers.RPC_URL, "fetched_at": "2026-11-03T00:00:00Z"}
        owner_params = issuers._query_params("usdt.tether-token.near", "owner", {})
        owner_key = issuers.rpc.fixture_key(issuers.RPC_URL, "query", owner_params)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / f"{tx_key}.json").write_text(json.dumps({
                "request": {"method": "POST", "params": params}, "response": tx_response,
                "url": tx_url, "fetched_at": "2026-11-03T00:00:00Z",
                "reduced": issuers.TRANSACTION_REDUCED,
            }), encoding="utf-8")
            (directory / f"{account_key}.json").write_text(json.dumps(account_fixture), encoding="utf-8")
            owner_path = issuers.FIXTURE_DIR / f"{owner_key}.json"
            (directory / f"{owner_key}.json").write_bytes(owner_path.read_bytes())
            rows = issuers.history_rows(directory)

        [row] = rows
        self.assertIn("UNKNOWN_ACCOUNT", row["note"])
        self.assertIn(f"at block {block}", row["note"])
        self.assertIn("nothing further is inferred", row["note"])
        self.assertEqual(row["category"], "watch")
        self.assertEqual(row["provenance"], "onchain")

    def test_status_rows_cover_only_the_named_sets_and_carry_blocks(self):
        store_subjects = {a.subject for a in labels.load().assertions
                          if a.chain == "near" and a.superseded_by is None
                          and a.category in issuers.STATUS_CATEGORIES}
        receiver_subjects = {item["subject"] for item in json.loads(
            issuers.RECEIVERS_PATH.read_text(encoding="utf-8"))["subjects"]}
        reads = issuers.reads()
        self.assertEqual({read["subject"] for read in reads}, store_subjects | receiver_subjects)
        self.assertEqual(len(reads), len(store_subjects | receiver_subjects) * len(issuers.issuer_entries()))
        self.assertTrue(all(isinstance(read["block"], int) and read["block"] > 0 for read in reads))
        self.assertTrue(all("answer" in read for read in reads))

    def test_a_banned_status_makes_a_watch_row_never_a_denylist_row(self):
        rows = issuers.rows()
        entries = {entry["token"]: entry for entry in issuers.issuer_entries()}
        reads = {(read["subject"], read["issuer"]): read for read in issuers.reads()}
        banned_count = sum(read["answer"] == entries[read["issuer"]]["banned"]
                           for read in issuers.reads())
        self.assertEqual(len(rows), banned_count)
        for row in rows:
            self.assertEqual(row["category"], "watch")
            self.assertEqual(row["provenance"], "onchain")
            token = row["entity"].removesuffix(" issuer action")
            read = reads[(row["subject"], token)]
            self.assertIn(json.dumps(read["answer"], ensure_ascii=False), row["label"])
            self.assertIn(str(read["block"]), row["label"])
        self.assertFalse(any(row["category"] == "denylist" for row in rows))

    def test_build_rows_makes_a_row_only_for_a_banned_answer(self):
        tether, usdc = issuers.issuer_entries()
        usdc = {**usdc, "method": tether["method"]}
        reads = [
            {"issuer": tether["token"], "subject": "banned.near", "method": tether["method"],
             "args": {"account_id": "banned.near"}, "answer": "Banned", "block": 10,
             "observed_at": "2026-11-03T00:00:00Z"},
            {"issuer": tether["token"], "subject": "allowed.near", "method": tether["method"],
             "args": {"account_id": "allowed.near"}, "answer": "Allowable", "block": 11,
             "observed_at": "2026-11-03T00:00:00Z"},
            {"issuer": usdc["token"], "subject": "blocked.near", "method": usdc["method"],
             "args": {"account_id": "blocked.near"}, "answer": True, "block": 12,
             "observed_at": "2026-11-03T00:00:00Z"},
        ]

        with patch.object(issuers, "issuer_entries", return_value=[tether, usdc]):
            rows = issuers.build_rows(reads)

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["category"] == "watch" for row in rows))
        for row, answer, block in zip(rows, ('"Banned"', "true"), (10, 12)):
            self.assertIn(answer, row["label"])
            self.assertIn(str(block), row["label"])

    def test_rows_rebuild_from_the_fixtures(self):
        self.assertEqual(issuers.STATUS_PATH.read_text(encoding="utf-8"), issuers.rows_jsonl())

    def test_append_command_delegates_to_seed_append(self):
        status_rows = [{"id": "status row"}]
        history_rows = [{"id": "history row"}]
        output = io.StringIO()
        with patch.object(issuers, "rows", return_value=status_rows), \
                patch.object(issuers, "history_rows", return_value=history_rows), \
                patch.object(issuers.seed, "append", return_value=1) as append, \
                contextlib.redirect_stdout(output):
            self.assertEqual(issuers.main(["append"]), 0)
        append.assert_called_once_with(status_rows + history_rows)
        self.assertIn("1 issuer status rows appended", output.getvalue())


if __name__ == "__main__":
    unittest.main()
