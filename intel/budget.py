"""Durable provider budgets shared by every Siyab process.

The API and the history and investigation workers are separate processes, so a provider count kept in memory
would be neither shared nor kept across a restart. gate.rpc.Client._exchange calls reserve before every real
fetch in mode off, or record with caller class recording; replay and other record callers never call it.
reserve counts the call in one SQLite BEGIN IMMEDIATE
transaction, which every process takes in turn, after checking every window the call falls in, so concurrent
processes cannot overshoot a limit; when one is spent it raises Refused and counts nothing. A reserved call stays
counted whether or not the provider answers.

The database is the file SIYAB_PROVIDER_BUDGET_DB names, else the one an entry point names with use_database
(DATABASE_NAME in its --state-dir). With neither, nothing is counted and reserve never lets the FastNEAR key
out, so the key is spent only where it is counted. A database that cannot be opened or read refuses the call.

A keyed call is one that carries the FastNEAR key to a FastNEAR host (FASTNEAR_CLASSES). Counters, each per UTC
day, hour and minute: every call by mode (keyed or keyless), caller class and provider class; the keyed limits
of KEYED_LIMITS; the keyless ceilings of KEYLESS_LIMITS; the pooled count of every keyed call in a UTC day,
refused at POOLED_DAILY for every caller class; and the cumulative count of keyed calls since the database
began. Reaching ALERT_PERCENTS of POOLED_DAILY, or CUMULATIVE_ALERT, records a row in alerts and writes one
stderr line. A 403 or 429 answering a keyed call (key_failure) starts KEY_COOLDOWN_S, shared by every process,
during which reserve sends no key.
"""

import math
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

DATABASE_ENV = "SIYAB_PROVIDER_BUDGET_DB"
DATABASE_NAME = "provider-budget.sqlite3"
DAY_S = 24 * 60 * 60
HOUR_S = 60 * 60
MINUTE_S = 60
SPANS = (DAY_S, HOUR_S, MINUTE_S)
# How long a counter row is kept after its window ends; the cumulative counter (span 0) is never removed.
RETENTION_S = {DAY_S: 90 * DAY_S, HOUR_S: 2 * DAY_S, MINUTE_S: HOUR_S}
POOLED_DAILY = 13000
CUMULATIVE_ALERT = 300000
ALERT_PERCENTS = (50, 80, 100)
KEY_COOLDOWN_S = 15 * 60
KEY_FAILURE_STATUSES = (403, 429)
# Retry-After for a call refused because the database could not be read.
REFUSAL_RETRY_S = 60
# backfill is the public backfill route: its calls count toward the anonymous budget as well as its own.
CALLER_CLASSES = ("anonymous", "backfill", "near_auth", "team", "watch", "investigation", "feed", "operator", "recording")
PARENT_CLASSES = {"backfill": "anonymous"}
# Keyed calls a caller class may make: provider class, or "*" for every provider class together, to
# (per UTC day, per hour, per minute), None leaving that window open. A provider class a table leaves out gets
# no keyed call. The operator shares a daily cap across provider classes.
KEYED_LIMITS = {
    "anonymous": {"fastnear_rpc": (2500, 420, 120), "fastnear_index": (2200, 370, 90),
                  "fastnear_archival": (800, 130, 45)},
    "backfill": {"fastnear_index": (500, None, None)},
    "near_auth": {"fastnear_rpc": (500, 100, 30)},
    "team": {"*": (1500, 250, None)},
    "watch": {"*": (3000, 125, None)},
    "investigation": {"*": (1500, 250, None)},
    "feed": {"*": (0, 0, None)},
    "operator": {"*": (1500, None, None)},
    "recording": {"*": (3000, None, None)},
}
# Keyless FastNEAR calls spend no credit; these politeness ceilings are shared by every caller class.
KEYLESS_LIMITS = {"fastnear_rpc": (20000, 2000, None), "fastnear_archival": (3000, 300, None)}
KEYLESS_CALLER_LIMITS = {"near_auth": {"fastnear_rpc": (500, 100, 30)}}
PROVIDER_HOSTS = {
    "free.rpc.fastnear.com": "fastnear_rpc",
    "rpc.mainnet.fastnear.com": "fastnear_rpc",
    "archival-rpc.mainnet.fastnear.com": "fastnear_archival",
    "api.fastnear.com": "fastnear_index",
    "tx.main.fastnear.com": "fastnear_index",
    "transfers.main.fastnear.com": "fastnear_index",
    "mainnet.neardata.xyz": "neardata",
    "test.rpc.fastnear.com": "fastnear_testnet",
    "archival-rpc.testnet.fastnear.com": "fastnear_testnet",
    "api.nearblocks.io": "nearblocks",
    "api-testnet.nearblocks.io": "nearblocks",
    "1click.chaindefuser.com": "oneclick",
    "api.gopluslabs.io": "goplus",
    "coins.llama.fi": "defillama",
}
FASTNEAR_CLASSES = ("fastnear_rpc", "fastnear_archival", "fastnear_index", "neardata")
_SPAN_NAMES = {DAY_S: "day", HOUR_S: "hour", MINUTE_S: "minute"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters(counter TEXT NOT NULL,window INTEGER NOT NULL,count INTEGER NOT NULL,expires_s INTEGER,PRIMARY KEY(counter,window));
CREATE INDEX IF NOT EXISTS counters_expiry ON counters(expires_s);
CREATE TABLE IF NOT EXISTS alerts(kind TEXT NOT NULL,window INTEGER NOT NULL,count INTEGER NOT NULL,created_ms INTEGER NOT NULL,message TEXT NOT NULL,PRIMARY KEY(kind,window));
CREATE TABLE IF NOT EXISTS key_cooldown(singleton INTEGER PRIMARY KEY CHECK(singleton=1),until_ms INTEGER NOT NULL,status INTEGER NOT NULL);
"""
_database = None


class Refused(Exception):
    """A budget is spent, or cannot be read, so the call was not counted; retry_after is whole seconds until
    every refusing window has turned."""

    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


@contextmanager
def use_database(path):
    """Count in the database at path for the duration of the block, unless SIYAB_PROVIDER_BUDGET_DB names one."""
    global _database
    previous, _database = _database, Path(path)
    try:
        yield
    finally:
        _database = previous


def database():
    configured = os.environ.get(DATABASE_ENV)
    return Path(configured) if configured else _database


def provider_class(host):
    return PROVIDER_HOSTS.get(host.lower(), "other")


@contextmanager
def _transaction(path):
    if path.is_symlink():
        raise sqlite3.OperationalError("the provider budget database cannot be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(_SCHEMA)
        os.chmod(path, 0o600)
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    finally:
        connection.close()


def _counters(provider, caller, keyed):
    """{(counter, span): limit or None} for every counter one call adds to; span 0 is the cumulative counter."""
    mode = "keyed" if keyed else "keyless"
    counters = {(f"{mode}:{caller}:{provider}", span): None for span in SPANS}
    if keyed:
        tables = [(f"keyed:{owner}", KEYED_LIMITS[owner]) for owner in (caller, PARENT_CLASSES.get(caller))
                  if owner in KEYED_LIMITS]
    else:
        tables = [("keyless:*", KEYLESS_LIMITS)] if provider in KEYLESS_LIMITS else []
        if caller in KEYLESS_CALLER_LIMITS:
            tables.append((f"keyless:{caller}", KEYLESS_CALLER_LIMITS[caller]))
    for prefix, table in tables:
        share = "*" if "*" in table else provider
        for span, limit in zip(SPANS, table.get(share, (0, 0, 0))):
            if limit is not None:
                counters[(f"{prefix}:{share}", span)] = limit
    if keyed:
        counters[("pooled", DAY_S)] = POOLED_DAILY
        counters[("cumulative", 0)] = None
    return counters


def _alert(connection, kind, window, count, now, message):
    inserted = connection.execute("INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?)",
                                  (kind, window, count, int(now * 1000), message)).rowcount
    return ["provider budget alert: " + message] if inserted else []


def reserve(provider, caller, *, keyed, now):
    """Count one call to provider class provider by caller class caller before it is sent, at wall time now.

    Returns True when the call may carry the FastNEAR key: keyed is true, a database is configured and no key
    cooldown runs. Raises Refused, counting nothing, when a window the call falls in is spent or the database
    cannot be opened or read. A keyed recording call also refuses during cooldown instead of going keyless."""
    path = database()
    if path is None:
        return False
    lines = []
    try:
        with _transaction(path) as connection:
            if keyed:
                cooldown = connection.execute("SELECT until_ms FROM key_cooldown WHERE singleton=1").fetchone()
                keyed = cooldown is None or cooldown[0] <= int(now * 1000)
                if not keyed and caller == "recording":
                    raise Refused("The provider budget refused the recording key during cooldown; retry later.",
                                  math.ceil(cooldown[0] / 1000 - now))
            counters = _counters(provider, caller, keyed)
            spent, retry_after = [], 1
            for (counter, span), limit in counters.items():
                if limit is None:
                    continue
                window = int(now // span)
                row = connection.execute("SELECT count FROM counters WHERE counter=? AND window=?",
                                         (counter, window)).fetchone()
                if (row[0] if row else 0) >= limit:
                    spent.append(f"{counter} allows {limit} a {_SPAN_NAMES[span]}")
                    retry_after = max(retry_after, math.ceil((window + 1) * span - now))
            if spent:
                lines.append("provider budget spent: " + "; ".join(spent) + ".")
                raise Refused("The provider budget for this kind of read is spent; retry later.", retry_after)
            connection.execute("DELETE FROM counters WHERE expires_s<?", (int(now),))
            for counter, span in counters:
                window = int(now // span) if span else 0
                expires = (window + 1) * span + RETENTION_S[span] if span else None
                connection.execute("INSERT INTO counters VALUES(?,?,1,?) ON CONFLICT(counter,window) DO UPDATE "
                                   "SET count=count+1", (counter, window, expires))
            if keyed:
                day = int(now // DAY_S)
                pooled = connection.execute("SELECT count FROM counters WHERE counter='pooled' AND window=?",
                                            (day,)).fetchone()[0]
                total = connection.execute("SELECT count FROM counters WHERE counter='cumulative' AND window=0"
                                           ).fetchone()[0]
                date = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
                for percent in ALERT_PERCENTS:
                    if pooled == POOLED_DAILY * percent // 100:
                        lines += _alert(connection, f"pooled_{percent}", day, pooled, now,
                                        f"{pooled} keyed FastNEAR calls on UTC day {date}, {percent} percent of "
                                        f"the pooled cap of {POOLED_DAILY}.")
                if total == CUMULATIVE_ALERT:
                    lines += _alert(connection, f"cumulative_{CUMULATIVE_ALERT}", 0, total, now,
                                    f"{total} keyed FastNEAR calls since the counter started.")
    except Refused:
        for line in lines:
            print(line, file=sys.stderr)
        raise
    except (sqlite3.Error, OSError) as error:
        print(f"provider budget database could not be read: {error}", file=sys.stderr)
        raise Refused("The provider budget database could not be read.", REFUSAL_RETRY_S) from error
    for line in lines:
        print(line, file=sys.stderr)
    return keyed


def key_failure(status, now):
    """After HTTP status answered a keyed FastNEAR call at wall time now: a 403 or 429 starts the shared key
    cooldown and records an alert."""
    path = database()
    if status not in KEY_FAILURE_STATUSES or path is None:
        return
    until = datetime.fromtimestamp(now + KEY_COOLDOWN_S, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _transaction(path) as connection:
            connection.execute("INSERT INTO key_cooldown VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                               "until_ms=MAX(until_ms,excluded.until_ms),status=excluded.status",
                               (int((now + KEY_COOLDOWN_S) * 1000), status))
            lines = _alert(connection, "key_cooldown", int(now), status, now,
                           f"HTTP {status} from a keyed FastNEAR call; FastNEAR requests go out without the key "
                           f"until {until}.")
    except (sqlite3.Error, OSError) as error:
        lines = [f"provider budget: the key cooldown could not be recorded: {error}"]
    for line in lines:
        print(line, file=sys.stderr)
