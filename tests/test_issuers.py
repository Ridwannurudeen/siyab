import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

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

    def test_build_rows_requires_the_exact_banned_type(self):
        entry = issuers.issuer_entries()[1]
        for answer in (1, "true", 0, None, []):
            with self.subTest(answer=answer):
                read = {"issuer": entry["token"], "subject": "unexpected.near", "method": entry["method"],
                        "args": {entry["args_key"]: "unexpected.near"}, "answer": answer, "block": 10,
                        "observed_at": "2026-11-03T00:00:00Z"}
                self.assertEqual(issuers.build_rows([read]), [])

    def test_rows_rebuild_from_the_fixtures(self):
        self.assertEqual(issuers.STATUS_PATH.read_text(encoding="utf-8"), issuers.rows_jsonl())

    def test_append_command_delegates_to_seed_append(self):
        status_rows = [{"id": "status row"}]
        history_rows = [{"id": "history row"}]
        events = []

        def status():
            events.append("status rows built")
            return status_rows

        def append_rows(rows):
            events.append(("appended", rows))
            return 1

        output = io.StringIO()
        with patch.object(issuers, "rows", side_effect=status), \
                patch.object(issuers, "history_rows", return_value=history_rows), \
                patch.object(issuers.seed, "append", side_effect=append_rows) as append, \
                contextlib.redirect_stdout(output):
            self.assertEqual(issuers.main(["append"]), 0)
        self.assertEqual(append.call_args_list, [call(history_rows), call(status_rows)])
        self.assertEqual(events, [("appended", history_rows), "status rows built", ("appended", status_rows)])
        self.assertIn("2 issuer rows appended", output.getvalue())


class IssuerBlacklistSeedTest(unittest.TestCase):
    def write_status_fixture(self, directory, read):
        params = issuers._query_params(read["issuer"], read["method"], read["args"])
        key = issuers.rpc.fixture_key(issuers.RPC_URL, "query", params)
        fixture = {
            "request": {"method": "query", "params": params},
            "response": {"jsonrpc": "2.0", "id": "gate", "result": {
                "block_height": read["block"], "result": list(json.dumps(read["answer"]).encode("utf-8"))}},
            "url": issuers.RPC_URL,
            "fetched_at": read["observed_at"],
        }
        (Path(directory) / f"{key}.json").write_text(json.dumps(fixture), encoding="utf-8")

    def test_current_bans_become_fixture_backed_denylist_assertions(self):
        rows = issuers.blacklist_rows()
        entries = {entry["token"]: entry for entry in issuers.issuer_entries()}
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({row["subject"] for row in rows}), 10)
        self.assertEqual(len({(row["entity"], row["subject"]) for row in rows}), len(rows))
        for row in rows:
            assertion = labels.validate(row)
            entry = entries[row["entity"].removeprefix("issuer of ")]
            self.assertEqual((assertion.category, assertion.provenance), ("denylist", "onchain"))
            self.assertEqual(assertion.source["rpc"], issuers.RPC_URL)
            self.assertEqual(assertion.source["method"], entry["method"])
            self.assertEqual(assertion.source["args"], {entry["args_key"]: row["subject"]})
            self.assertIn(f"as of block {assertion.source['block']}", assertion.label)

    def test_a_catalog_token_blacklist_read_makes_no_assertion(self):
        entry = next(entry for entry in issuers.issuer_entries() if entry["token"] == "usdt.tether-token.near")
        subject = "wrap.near"
        read = {"issuer": entry["token"], "subject": subject, "method": entry["method"],
                "args": {entry["args_key"]: subject}, "answer": entry["banned"], "block": 10,
                "observed_at": "2026-11-03T00:00:00Z"}
        with tempfile.TemporaryDirectory() as tmp:
            self.write_status_fixture(tmp, read)
            self.assertEqual(issuers.blacklist_rows([read], tmp), [])

    def test_an_issuer_blacklist_read_of_its_own_contract_makes_no_assertion(self):
        entry = next(entry for entry in issuers.issuer_entries() if entry["token"] == "usdt.tether-token.near")
        subject = entry["token"]
        read = {"issuer": entry["token"], "subject": subject, "method": entry["method"],
                "args": {entry["args_key"]: subject}, "answer": entry["banned"], "block": 10,
                "observed_at": "2026-11-03T00:00:00Z"}
        with tempfile.TemporaryDirectory() as tmp:
            self.write_status_fixture(tmp, read)
            self.assertEqual(issuers.blacklist_rows([read], tmp), [])

    def test_a_status_without_a_recorded_view_fixture_makes_no_assertion(self):
        entry = issuers.issuer_entries()[0]
        read = {"issuer": entry["token"], "subject": "saved.near", "method": entry["method"],
                "args": {entry["args_key"]: "saved.near"}, "answer": entry["banned"], "block": 10,
                "observed_at": "2026-11-03T00:00:00Z"}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(issuers.blacklist_rows([read], tmp), [])

    def test_rerunning_the_seed_with_the_same_fixtures_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.jsonl"
            self.assertEqual(issuers.seed_blacklists(path), 10)
            first = path.read_bytes()
            self.assertEqual(issuers.seed_blacklists(path), 0)
            self.assertEqual(path.read_bytes(), first)
            self.assertEqual(len(labels.load(path).assertions), 10)

    def test_a_recorded_removal_supersedes_the_current_blacklist_assertion(self):
        entry = issuers.issuer_entries()[0]
        subject = "removed.near"
        read = {"issuer": entry["token"], "subject": subject, "method": entry["method"],
                "args": {entry["args_key"]: subject}, "answer": entry["banned"], "block": 10,
                "observed_at": "2026-11-03T00:00:00Z"}
        removal = issuers.seed.make_row(
            entity=f"{entry['token']} issuer action", subject=subject, chain="near",
            label=f"removed from the issuer blacklist of {entry['token']} at block 12",
            category="watch", provenance="onchain",
            source={"rpc": issuers.FASTNEAR_TX_URL, "method": "POST /v0/transactions",
                    "args": {"tx_hashes": ["history-tx"]}, "block": 12},
            observed_at="2026-11-03T00:00:00Z",
            note="The issuer action method remove_from_blacklist names this account id.")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            reads_path = directory / "reads.json"
            store_path = directory / "labels.jsonl"
            self.write_status_fixture(directory, read)
            old_row = issuers.blacklist_rows([read], directory)[0]
            store_path.write_text(labels.row_line(old_row) + "\n", encoding="utf-8")
            read.update(answer="Allowable", block=11, observed_at="2026-11-03T00:01:00Z")
            self.write_status_fixture(directory, read)
            reads_path.write_text(json.dumps([read]), encoding="utf-8")
            with patch.object(issuers, "history_rows", return_value=[removal]):
                self.assertEqual(issuers.seed_blacklists(store_path, directory, reads_path), 1)
                before = store_path.read_bytes()
                self.assertEqual(issuers.seed_blacklists(store_path, directory, reads_path), 0)
                self.assertEqual(store_path.read_bytes(), before)
            store = labels.load(store_path)

        [saved_old] = [row for row in store.by_subject(subject, "near") if row.category == "denylist"]
        self.assertEqual(saved_old.superseded_by, removal["id"])
        self.assertFalse(any(row.category == "denylist" and row.superseded_by is None
                             for row in store.by_subject(subject, "near")))

    def test_blacklist_rows_requires_the_exact_banned_type(self):
        entry = issuers.issuer_entries()[1]
        read = {"issuer": entry["token"], "subject": "unexpected.near", "method": entry["method"],
                "args": {entry["args_key"]: "unexpected.near"}, "answer": 1, "block": 10,
                "observed_at": "2026-11-03T00:00:00Z"}
        with tempfile.TemporaryDirectory() as tmp:
            self.write_status_fixture(tmp, read)
            self.assertEqual(issuers.blacklist_rows([read], tmp), [])

    def test_a_cached_status_must_match_the_recorded_answer_type(self):
        entry = issuers.issuer_entries()[1]
        read = {"issuer": entry["token"], "subject": "unexpected.near", "method": entry["method"],
                "args": {entry["args_key"]: "unexpected.near"}, "answer": False, "block": 10,
                "observed_at": "2026-11-03T00:00:00Z"}
        with tempfile.TemporaryDirectory() as tmp:
            self.write_status_fixture(tmp, read)
            read["answer"] = 0
            with self.assertRaises(issuers.seed.SeedError):
                issuers._verified_status_read(read, tmp)

    def test_an_unexpected_status_never_retires_a_blacklist_assertion(self):
        entry = issuers.issuer_entries()[1]
        subject = "still-listed.near"
        for answer, has_clear in (("false", True), (0, True), (None, True), ([], True), (False, False)):
            with self.subTest(answer=answer, has_clear=has_clear), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                reads_path = directory / "reads.json"
                store_path = directory / "labels.jsonl"
                read = {"issuer": entry["token"], "subject": subject, "method": entry["method"],
                        "args": {entry["args_key"]: subject}, "answer": True, "block": 10,
                        "observed_at": "2026-11-03T00:00:00Z"}
                self.write_status_fixture(directory, read)
                old_row = issuers.blacklist_rows([read], directory)[0]
                store_path.write_text(labels.row_line(old_row) + "\n", encoding="utf-8")
                read.update(answer=answer, block=11, observed_at="2026-11-03T00:01:00Z")
                self.write_status_fixture(directory, read)
                reads_path.write_text(json.dumps([read]), encoding="utf-8")
                selected = dict(entry)
                if not has_clear:
                    selected.pop("clear", None)
                with patch.object(issuers, "issuer_entries", return_value=[selected]), \
                        patch.object(issuers, "history_rows", return_value=[]):
                    self.assertEqual(issuers.seed_blacklists(store_path, directory, reads_path), 0)
                [saved] = labels.load(store_path).assertions
                self.assertIsNone(saved.superseded_by)

    def test_seed_command_appends_fixture_backed_issuer_assertions(self):
        output = io.StringIO()
        with patch.object(issuers, "seed_blacklists", return_value=11), \
                contextlib.redirect_stdout(output):
            self.assertEqual(issuers.main(["seed"]), 0)
        self.assertIn("11 issuer assertions appended", output.getvalue())


if __name__ == "__main__":
    unittest.main()
