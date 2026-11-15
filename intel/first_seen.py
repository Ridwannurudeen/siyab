"""Receipt-backed creation evidence and inclusive block coverage windows.

Lookups require explicit inclusive from_height and to_height bounds.
Implicit transfers do not establish creation, so implicit misses remain unknown.
"""

import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

DATABASE_NAME = "first_seen.sqlite3"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
    account_id TEXT PRIMARY KEY, height INTEGER NOT NULL, timestamp_ns TEXT NOT NULL,
    creator TEXT NOT NULL, deposit_yocto TEXT NOT NULL, receipt_id TEXT NOT NULL,
    block_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage(start INTEGER PRIMARY KEY, end INTEGER NOT NULL);
"""


def _path(state_dir):
    directory = state_dir if state_dir is not None else os.environ.get(
        "FEED_STATE_DIR", Path(__file__).resolve().parent / "feed")
    return Path(directory) / DATABASE_NAME


def advance(state_dir, height, block):
    """Commit creations and coverage for one processed height in one transaction.

    None marks an explicit null slot, which executes no receipts and is covered.
    Failed or unavailable blocks never reach this function. Replays are idempotent.
    """
    path = _path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(_SCHEMA)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if block is not None:
                for receipt in block["receipts"]:
                    if not receipt["ok"] or not any(action["type"] == "CreateAccount" for action in receipt["actions"]):
                        continue
                    deposit = sum(int(action["deposit"]) for action in receipt["actions"]
                                  if action["type"] == "Transfer")
                    connection.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,?,?,?,?,?)",
                                       (receipt["receiver_id"], block["height"], block["timestamp_ns"],
                                        receipt["predecessor_id"], str(deposit), receipt["receipt_id"], block["hash"]))
            ranges = connection.execute("SELECT start, end FROM coverage WHERE start<=? AND end>=? ORDER BY start",
                                        (height + 1, height - 1)).fetchall()
            if not ranges:
                connection.execute("INSERT INTO coverage VALUES(?,?)", (height, height))
            elif not (ranges[0][0] <= height <= ranges[0][1]):
                connection.execute("UPDATE coverage SET start=?, end=? WHERE start=?",
                                   (min(height, ranges[0][0]), max(height, ranges[-1][1]), ranges[0][0]))
                for start, _ in ranges[1:]:
                    connection.execute("DELETE FROM coverage WHERE start=?", (start,))


def covered_ranges(*, state_dir=None):
    """Return inclusive (start, end) ranges; no readable index means no coverage."""
    path = _path(state_dir)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            return connection.execute("SELECT start, end FROM coverage ORDER BY start").fetchall()
    except (OSError, sqlite3.Error):
        return []


def lookup(account_id, from_height, to_height, *, state_dir=None):
    """Return created evidence, not_created_in_covered, or unknown.

    A recorded creation at or before the window end remains evidence even with
    earlier gaps. A covered miss says nothing about existence before the window.
    """
    if (type(from_height) is not int or from_height < 0 or
            type(to_height) is not int or to_height < from_height):
        raise ValueError("invalid height window")
    path = _path(state_dir)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.execute("BEGIN")
            row = connection.execute("SELECT * FROM accounts WHERE account_id=? AND height<=?",
                                     (account_id, to_height)).fetchone()
            if row:
                account, height, stamp, creator, deposit, receipt_id, block_hash = row
                return {"status": "created", "account_id": account, "height": height,
                        "timestamp_ns": stamp, "creator": creator, "deposit_yocto": deposit,
                        "receipt_id": receipt_id, "block": {"height": height, "hash": block_hash,
                        "url": f"https://mainnet.neardata.xyz/v0/block/{height}"}}
            if re.fullmatch(r"(?:[0-9a-f]{64}|0x[0-9a-f]{40}|0s[0-9a-f]+)", account_id):
                return {"status": "unknown"}
            covered = connection.execute("SELECT 1 FROM coverage WHERE start<=? AND end>=?",
                                         (from_height, to_height)).fetchone()
            return {"status": "not_created_in_covered" if covered else "unknown"}
    except (OSError, sqlite3.Error):
        return {"status": "unknown"}
