"""Offline creation evidence and bounded coverage lookups."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from intel import feed, first_seen
from tests.test_feed import BLOCKS, raw_block, raw_receipt


class FirstSeenTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def advance(self, height, receipts=(), missing=False):
        block = None if missing else feed.reduce_block(raw_block(height, list(receipts)))
        first_seen.advance(self.root, height, block)

    def test_creation_captures_receipt_deposits_and_block_evidence(self):
        receipt = raw_receipt("child.near", ["CreateAccount", {"Transfer": {"deposit": "5"}},
                                              {"Transfer": {"deposit": "7"}}], "maker.near")
        self.advance(10, [receipt])
        result = first_seen.lookup("child.near", 10, 10, state_dir=self.root)
        self.assertEqual(result, {"status": "created", "account_id": "child.near", "height": 10,
                                 "timestamp_ns": "1793284740730787531", "creator": "maker.near",
                                 "deposit_yocto": "12", "receipt_id": "r-child.near",
                                 "block": {"height": 10, "hash": "hash10",
                                           "url": feed.NEARDATA_URL + "/v0/block/10"}})

    def test_recorded_named_creation_with_zero_deposit(self):
        block = json.loads((BLOCKS / "222139030.json").read_text(encoding="utf-8"))
        first_seen.advance(self.root, block["height"], block)
        result = first_seen.lookup("bestrat2585.near", block["height"], block["height"], state_dir=self.root)
        self.assertEqual((result["status"], result["creator"], result["deposit_yocto"]),
                         ("created", "near", "0"))

    def test_transfer_only_implicit_account_is_never_claimed_created_or_old(self):
        for height, account in enumerate(("ab" * 32, "0x" + "ab" * 20), start=10):
            self.advance(height, [raw_receipt(account, [{"Transfer": {"deposit": "5"}}], "maker.near")])
            self.assertEqual(first_seen.lookup(account, height, height, state_dir=self.root)["status"], "unknown")

    def test_state_init_deterministic_accounts_remain_unknown_in_covered_heights(self):
        for height, account in enumerate(("0s" + "ab" * 20, "0s" + "ab" * 31), start=10):
            with self.subTest(account=account):
                self.advance(height, [raw_receipt(account, [{"Transfer": {"deposit": "5"}}])])
                self.assertEqual(first_seen.lookup(account, height, height, state_dir=self.root),
                                 {"status": "unknown"})
        self.assertEqual(first_seen.lookup("0sample.near", 10, 11, state_dir=self.root)["status"],
                         "not_created_in_covered")

    def test_reader_transaction_does_not_block_advance(self):
        self.advance(10)
        with closing(sqlite3.connect(self.root / "first_seen.sqlite3")) as reader:
            reader.execute("BEGIN")
            self.assertEqual(reader.execute("SELECT start, end FROM coverage").fetchall(), [(10, 10)])
            self.advance(11, [raw_receipt("child.near", ["CreateAccount"])])
            self.assertEqual(reader.execute("SELECT start, end FROM coverage").fetchall(), [(10, 10)])
            reader.rollback()
            self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(reader.execute("SELECT start, end FROM coverage").fetchall(), [(10, 11)])
        self.assertEqual(first_seen.lookup("child.near", 10, 11, state_dir=self.root)["status"], "created")

    def test_failed_creation_does_not_create_a_row(self):
        receipt = raw_receipt("child.near", ["CreateAccount"], status={"Failure": {}})
        self.advance(10, [receipt])
        self.assertEqual(first_seen.lookup("child.near", 10, 10, state_dir=self.root)["status"],
                         "not_created_in_covered")

    def test_inclusive_window_requires_every_height(self):
        self.advance(10)
        self.advance(11)
        self.advance(13)
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(10, 11), (13, 13)])
        for window in ((9, 11), (10, 12), (12, 12), (11, 13), (13, 14)):
            with self.subTest(window=window):
                self.assertEqual(first_seen.lookup("absent.near", *window, state_dir=self.root)["status"], "unknown")
        for window in ((10, 11), (11, 11), (13, 13)):
            self.assertEqual(first_seen.lookup("absent.near", *window, state_dir=self.root)["status"],
                             "not_created_in_covered")

    def test_known_creation_is_evidence_despite_earlier_gaps_but_not_before_its_height(self):
        self.advance(10, [raw_receipt("child.near", ["CreateAccount"])])
        self.assertEqual(first_seen.lookup("child.near", 9, 9, state_dir=self.root)["status"], "unknown")
        self.assertEqual(first_seen.lookup("child.near", 9, 10, state_dir=self.root)["status"], "created")
        self.assertEqual(first_seen.lookup("child.near", 11, 12, state_dir=self.root)["status"], "created")

    def test_missing_or_damaged_index_is_unknown_and_not_created_on_read(self):
        self.assertEqual(first_seen.lookup("absent.near", 10, 10, state_dir=self.root), {"status": "unknown"})
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [])
        self.assertEqual(list(self.root.iterdir()), [])
        (self.root / "first_seen.sqlite3").write_bytes(b"broken database")
        self.assertEqual(first_seen.lookup("absent.near", 10, 10, state_dir=self.root), {"status": "unknown"})
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [])

    def test_environment_selects_the_followers_state_directory(self):
        self.advance(10)
        with mock.patch.dict(os.environ, {"FEED_STATE_DIR": str(self.root)}):
            self.assertEqual(first_seen.covered_ranges(), [(10, 10)])

    def test_replay_does_not_duplicate_or_replace_the_first_creation(self):
        self.advance(10, [raw_receipt("child.near", ["CreateAccount", {"Transfer": {"deposit": "5"}}])])
        self.advance(10, [raw_receipt("child.near", ["CreateAccount", {"Transfer": {"deposit": "8"}}])])
        self.advance(11, [raw_receipt("child.near", ["CreateAccount", {"Transfer": {"deposit": "9"}}])])
        self.assertEqual(first_seen.lookup("child.near", 10, 11, state_dir=self.root)["deposit_yocto"], "5")
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(10, 11)])
        with closing(sqlite3.connect(self.root / "first_seen.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM accounts").fetchone()[0], 1)

    def test_invalid_windows_are_rejected(self):
        self.advance(10)
        for window in ((-1, 10), (True, 10), (11, 10), (1, "now"), (1, None), (1, False), (1, 1.5)):
            with self.subTest(window=window), self.assertRaises(ValueError):
                first_seen.lookup("child.near", *window, state_dir=self.root)

    def test_legacy_lookup_forms_are_rejected(self):
        for value in (10, "now", (10, 11)):
            with self.subTest(value=value), self.assertRaises(TypeError):
                first_seen.lookup("child.near", value, state_dir=self.root)

    def test_null_slot_extends_coverage_and_replay_can_join_ranges(self):
        self.advance(10)
        self.advance(12)
        self.advance(13)
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(10, 10), (12, 13)])
        self.advance(11, missing=True)
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(10, 13)])
        self.advance(11, missing=True)
        self.advance(10)
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(10, 13)])
        self.assertEqual(first_seen.lookup("absent.near", 10, 13, state_dir=self.root)["status"],
                         "not_created_in_covered")

    def test_process_death_before_coverage_write_rolls_back_the_whole_height(self):
        self.advance(10)
        program = textwrap.dedent('''
            import os
            import sys
            from intel import first_seen
            from tests.test_feed import raw_block, raw_receipt
            from intel import feed

            connect = first_seen.sqlite3.connect
            def interrupted(*args, **kwargs):
                connection = connect(*args, **kwargs)
                connection.set_trace_callback(lambda statement: os._exit(23)
                    if statement.startswith("UPDATE coverage") else None)
                return connection
            first_seen.sqlite3.connect = interrupted
            block = feed.reduce_block(raw_block(11, [raw_receipt("child.near", ["CreateAccount"])]))
            first_seen.advance(sys.argv[1], 11, block)
        ''')
        result = subprocess.run([sys.executable, "-c", program, str(self.root)],
                                cwd=Path(__file__).resolve().parent.parent, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 23, result.stderr.decode("utf-8"))
        self.assertEqual(first_seen.covered_ranges(state_dir=self.root), [(10, 10)])
        self.assertEqual(first_seen.lookup("child.near", 11, 11, state_dir=self.root)["status"], "unknown")
        self.advance(11, [raw_receipt("child.near", ["CreateAccount"])])
        self.assertEqual(first_seen.lookup("child.near", 11, 11, state_dir=self.root)["status"], "created")


if __name__ == "__main__":
    unittest.main()
