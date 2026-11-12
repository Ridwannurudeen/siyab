"""Durable public account observations, bounded backfills and ordinary-transfer monitoring."""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from gate import canon, rpc
from intel.accounts import AccountInputError, AccountReadError

KINDS = ("profile", "holdings", "transactions", "transfers")
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
EVENT_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_PAGES = 10000
MAX_ARCHIVE_ROWS = 100000
MAX_ARCHIVE_GRAPHS = 2000
MAX_ARCHIVE_EVENTS = 100000
MAX_WATCH_SEEN = 200000
MAX_ACCOUNT_WATCH_SEEN = 1000
PUBLIC_ARCHIVE_PERCENT = 75
PUBLIC_BACKFILL_PAGES = 4
JOB_BACKFILL_PAGES = 8
JOB_PAGE_BYTES = 2 * MAX_DOCUMENT_BYTES
GLOBAL_INTERVAL_MS = 30000
ACCOUNT_INTERVAL_MS = 60000
LEASE_MS = 45000
FRESH_MS = 900000
_HEX = re.compile(r"[a-f0-9]{64}")
_NS = re.compile(r"[0-9]{1,20}")
_PRIVATE_FIELDS = {"password", "password_hash", "password_salt", "authorization", "cookie", "session_token",
                   "csrf_token", "recovery_code", "team_id", "user_id", "watchlist", "notes"}


class HistoryInputError(ValueError):
    """An archive operation is outside the supported public account-data scope."""


class HistoryConflictError(HistoryInputError):
    """An archived transfer or transaction identifier came back with a different timestamp."""


class HistoryReadError(ValueError):
    """A durable observation or archive state cannot be read consistently."""


class HistoryCapacityError(HistoryReadError):
    """An archive retention or upstream-read budget has been reached."""

    def __init__(self, message, *, retry_at_ms=None):
        super().__init__(message)
        self.retry_at_ms = retry_at_ms


class WatchPriorityError(HistoryCapacityError):
    """A due watched account takes the provider before a background investigation."""


def _account(value):
    try:
        account = canon.account_id(value, "account")
    except canon.CanonError:
        raise HistoryInputError("Use a valid mainnet account identifier.") from None
    if account == "testnet" or account.endswith(".testnet"):
        raise HistoryInputError("The account archive reads mainnet only.")
    return account


def _kind(value):
    if value not in KINDS:
        raise HistoryInputError("The account observation kind is unsupported.")
    return value


def _cursor(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 4096 or any(not 33 <= ord(char) <= 126 for char in value):
        raise HistoryInputError("The provider cursor is invalid.")
    return value


def _public(value, depth=0):
    if depth > 24:
        raise HistoryInputError("The observation exceeds the nesting limit.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _PRIVATE_FIELDS:
                raise HistoryInputError("Private workspace fields cannot enter the public account archive.")
            _public(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _public(item, depth + 1)


def _canonical(value):
    _public(value)
    try:
        body = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise HistoryInputError("The observation must contain valid UTF-8 JSON values.") from None
    if len(body) > MAX_DOCUMENT_BYTES:
        raise HistoryInputError("The observation exceeds the archive document limit.")
    return body


def _settled(value):
    if isinstance(value, dict):
        return {key: _settled(item) for key, item in value.items() if key not in ("observed_at", "fetched_at", "request_sha256")}
    if isinstance(value, list):
        return [_settled(item) for item in value]
    return value


def _comparable(value, kind):
    value = _settled(value)
    position = value.get("native") if kind == "profile" else value.get("metadata_source") if kind == "holdings" else None
    if isinstance(position, dict):
        position.pop("block_height", None)
        position.pop("block_hash", None)
    if value.get("items") == [] and isinstance(value.get("pagination"), dict):
        value["pagination"].pop("limit", None)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _same_content(retained, current, kind):
    return _comparable(retained, kind) == _comparable(current, kind)


def _retained(body):
    try:
        value = json.loads(body)
        canonical = _canonical(value)
    except (ValueError, TypeError, RecursionError):
        raise HistoryReadError("A retained archive observation is unreadable.") from None
    if not isinstance(value, dict) or canonical != body:
        raise HistoryReadError("A retained archive observation is not a canonical JSON object.")
    return value


def _retained_page(row, kind, account):
    if row is None or not isinstance(row["body"], bytes) or hashlib.sha256(row["body"]).hexdigest() != row["id"]:
        raise HistoryReadError("A retained archive page is missing or does not match its identifier.")
    document = _retained(row["body"])
    items = [document] if kind == "profile" else document.get("items")
    if document.get("account_id") != account or document.get("network") != "mainnet" or not isinstance(document.get("source"), dict) or not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise HistoryReadError("A retained archive page is malformed.")
    try:
        return document, {_identity(item, kind, account): item for item in items}
    except HistoryInputError:
        raise HistoryReadError("A retained archive page has an invalid row identifier.") from None


def _retained_row(record, kind, account):
    item = _retained(record["body"])
    try:
        sound = _identity(item, kind, account) == record["row_id"] and (kind not in ("transactions", "transfers") or _timestamp(item, kind, 0) == record["stamp"])
    except HistoryInputError:
        sound = False
    if not sound:
        raise HistoryReadError("A retained archive row does not match its identifier or timestamp.")
    return item


def _retained_records(connection, records, kind, account):
    items, documents = [], {}
    for record in records:
        item = _retained_row(record, kind, account)
        if record["page_id"] not in documents:
            documents[record["page_id"]] = _retained_page(connection.execute("SELECT id,body FROM pages WHERE id=?", (record["page_id"],)).fetchone(), kind, account)
        held = documents[record["page_id"]][1].get(record["row_id"])
        if held is None or _canonical(held) != record["body"]:
            raise HistoryReadError("A retained archive row cites a page that does not hold it.")
        items.append(item)
    return items, documents


def _window(document):
    pagination = document.get("pagination")
    return (pagination.get("offset"), pagination.get("limit")) if isinstance(pagination, dict) else None


def _observation(page_id, document):
    return {"page_id": page_id, "source": document["source"], "observed_at": document.get("observed_at")}


def _timestamp(item, kind, now):
    value = item.get("block_timestamp") if kind == "transfers" else item.get("tx_block_timestamp") if kind == "transactions" else str(now * 1000000)
    if not isinstance(value, str) or _NS.fullmatch(value) is None or int(value) > canon.U64_MAX:
        raise HistoryInputError("The observation has an invalid nanosecond timestamp.")
    return value.zfill(20)


def _identity(item, kind, account):
    value = item.get("id") if kind == "transfers" else item.get("transaction_hash") if kind == "transactions" else item.get("contract_id") if kind == "holdings" else account
    if not isinstance(value, str) or not value or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise HistoryInputError("The observation has an invalid stable identifier.")
    return value


def _empty_status():
    return {"running": False, "mode": None, "heartbeat_ms": None, "last_success_ms": None,
            "monitored_accounts": 0, "initialized_accounts": 0, "retry_pending": 0, "coverage_gap_accounts": 0,
            "backfill_pending": 0, "pending_events": 0, "archived_rows": 0, "last_error": None,
            "lag_blocks": None, "oldest_due_ms": None,
            "coverage": "Bounded account polling and retained provider pages. Gaps and older unindexed history may remain.",
            "delivery_mode": "team_inbox", "external_delivery_configured": False}


def read_status(path, *, clock=time.time):
    result = _empty_status()
    database = Path(path)
    if not database.is_file():
        return result
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            state = connection.execute("SELECT * FROM history_state WHERE singleton=1").fetchone()
            counts = connection.execute("SELECT COUNT(*) AS monitored,COALESCE(SUM(initialized),0) AS initialized,"
                                        "COALESCE(SUM(attempts>0),0) AS retries,COALESCE(SUM(coverage_gap),0) AS gaps,"
                                        "MIN(next_due_ms) AS due FROM watch_poll").fetchone()
            pending = connection.execute("SELECT COUNT(*) FROM event_outbox WHERE delivered_seq IS NULL").fetchone()[0]
            records = connection.execute("SELECT COUNT(*) FROM history_rows").fetchone()[0]
            backfill = connection.execute("SELECT COUNT(*) FROM backfills WHERE exhausted=0").fetchone()[0]
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        result["last_error"] = "state_unreadable"
        return result
    if state is None:
        result["last_error"] = "state_unreadable"
        return result
    result.update(mode=state["mode"], heartbeat_ms=state["heartbeat_ms"], last_success_ms=state["last_success_ms"],
                  last_error=state["last_error"], monitored_accounts=counts["monitored"], initialized_accounts=counts["initialized"],
                  retry_pending=counts["retries"], coverage_gap_accounts=counts["gaps"], oldest_due_ms=counts["due"],
                  pending_events=pending, archived_rows=records, backfill_pending=backfill)
    heartbeat = state["heartbeat_ms"]
    result["running"] = (state["mode"] == "service" and state["stopped_ms"] is None and heartbeat is not None
                         and 0 <= int(clock() * 1000) - heartbeat <= 30000)
    return result


class AccountHistory:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        if self.path.is_symlink():
            raise HistoryInputError("The history database cannot be a symbolic link.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection(transaction=False) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise HistoryReadError("The history database schema is unsupported.")
            if version == 0:
                connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY,account TEXT NOT NULL,kind TEXT NOT NULL,body BLOB NOT NULL,created_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS history_rows (seq INTEGER PRIMARY KEY AUTOINCREMENT,account TEXT NOT NULL,kind TEXT NOT NULL,row_id TEXT NOT NULL,stamp TEXT NOT NULL,body BLOB NOT NULL,page_id TEXT NOT NULL REFERENCES pages(id),UNIQUE(account,kind,row_id));
                CREATE INDEX IF NOT EXISTS rows_account ON history_rows(account,kind,stamp DESC,seq DESC);
                CREATE TABLE IF NOT EXISTS graphs (id TEXT PRIMARY KEY,body BLOB NOT NULL,created_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS backfills (account TEXT NOT NULL,kind TEXT NOT NULL,cursor TEXT,exhausted INTEGER NOT NULL,updated_ms INTEGER NOT NULL,PRIMARY KEY(account,kind));
                CREATE TABLE IF NOT EXISTS watch_poll (account TEXT PRIMARY KEY,initialized INTEGER NOT NULL DEFAULT 0,next_due_ms INTEGER NOT NULL DEFAULT 0,last_success_ms INTEGER,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,coverage_gap INTEGER NOT NULL DEFAULT 0,lease_token TEXT,lease_until_ms INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS watch_seen (account TEXT NOT NULL,row_id TEXT NOT NULL,PRIMARY KEY(account,row_id));
                CREATE TABLE IF NOT EXISTS event_outbox (id TEXT PRIMARY KEY,body BLOB NOT NULL,notify INTEGER NOT NULL,created_ms INTEGER NOT NULL,delivered_seq INTEGER);
                CREATE TABLE IF NOT EXISTS history_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1),mode TEXT,heartbeat_ms INTEGER,last_success_ms INTEGER,last_tick_ms INTEGER,stopped_ms INTEGER,last_error TEXT);
                INSERT OR IGNORE INTO history_state(singleton) VALUES (1);
                PRAGMA user_version=1;
                COMMIT;
                """)
            if version in (0, 1):
                connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS backfill_control(account TEXT NOT NULL,kind TEXT NOT NULL,last_start_ms INTEGER NOT NULL,lease_token TEXT,lease_until_ms INTEGER NOT NULL,PRIMARY KEY(account,kind));
                CREATE INDEX IF NOT EXISTS backfill_account_budget ON backfill_control(account,last_start_ms);
                CREATE TABLE IF NOT EXISTS backfill_budget(singleton INTEGER PRIMARY KEY CHECK(singleton=1),window INTEGER NOT NULL,pages INTEGER NOT NULL);
                INSERT OR IGNORE INTO backfill_budget VALUES(1,0,0);
                CREATE TABLE IF NOT EXISTS archive_usage(singleton INTEGER PRIMARY KEY CHECK(singleton=1),pages INTEGER NOT NULL,records INTEGER NOT NULL,graphs INTEGER NOT NULL,events INTEGER NOT NULL,seen INTEGER NOT NULL,bytes INTEGER NOT NULL);
                INSERT OR IGNORE INTO archive_usage SELECT 1,
                  (SELECT COUNT(*) FROM pages),(SELECT COUNT(*) FROM history_rows),(SELECT COUNT(*) FROM graphs),
                  (SELECT COUNT(*) FROM event_outbox),(SELECT COUNT(*) FROM watch_seen),
                  (SELECT COALESCE(SUM(LENGTH(body)),0) FROM pages)+(SELECT COALESCE(SUM(LENGTH(body)),0) FROM history_rows)+
                  (SELECT COALESCE(SUM(LENGTH(body)),0) FROM graphs)+(SELECT COALESCE(SUM(LENGTH(body)),0) FROM event_outbox);
                PRAGMA user_version=2;
                COMMIT;
                """)
            connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS provider_cooldowns(account TEXT NOT NULL,kind TEXT NOT NULL,not_before_ms INTEGER NOT NULL,PRIMARY KEY(account,kind));
            CREATE INDEX IF NOT EXISTS pages_account ON pages(account,kind,created_ms);
            CREATE INDEX IF NOT EXISTS outbox_delivered ON event_outbox(created_ms,id) WHERE delivered_seq IS NOT NULL;
            CREATE TABLE IF NOT EXISTS job_read_budget(singleton INTEGER PRIMARY KEY CHECK(singleton=1),window INTEGER NOT NULL,pages INTEGER NOT NULL);
            INSERT OR IGNORE INTO job_read_budget VALUES(1,0,0);
            COMMIT;
            """)
            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(watch_poll)")}
            for column in ("reserved", "added_ms"):
                if column not in columns:
                    connection.execute("ALTER TABLE watch_poll ADD COLUMN " + column + " INTEGER NOT NULL DEFAULT 0")
            if "tick_count" not in {row["name"] for row in connection.execute("PRAGMA table_info(history_state)")}:
                connection.execute("ALTER TABLE history_state ADD COLUMN tick_count INTEGER NOT NULL DEFAULT 0")
            connection.commit()
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self, *, write=False, transaction=True):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA max_page_count=131072")
        connection.execute("PRAGMA journal_size_limit=16777216")
        connection.execute("PRAGMA wal_autocheckpoint=256")
        try:
            if transaction:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            if transaction:
                connection.commit()
        finally:
            connection.close()

    def _now(self):
        return int(self.clock() * 1000)

    @staticmethod
    def _reserve(connection, *, pages=0, records=0, graphs=0, events=0, seen=0, size=0, reserved=False, held=None):
        usage = connection.execute("SELECT * FROM archive_usage WHERE singleton=1").fetchone()
        changes = {"pages": (pages, MAX_ARCHIVE_PAGES), "records": (records, MAX_ARCHIVE_ROWS),
                   "graphs": (graphs, MAX_ARCHIVE_GRAPHS), "events": (events, MAX_ARCHIVE_EVENTS),
                   "seen": (seen, MAX_WATCH_SEEN), "bytes": (size, MAX_ARCHIVE_BYTES)}
        if not reserved:
            changes = {field: (delta, maximum * PUBLIC_ARCHIVE_PERCENT // 100) for field, (delta, maximum) in changes.items()}
        if events <= 0:
            changes["bytes"] = (size, min(changes["bytes"][1], MAX_ARCHIVE_BYTES - EVENT_ARCHIVE_BYTES))
        elif held + size <= EVENT_ARCHIVE_BYTES:
            del changes["bytes"]
        if any(delta > 0 and usage[field] + delta > maximum for field, (delta, maximum) in changes.items()):
            raise HistoryCapacityError("Archive retention capacity reached; retained evidence remains available and new observations are paused.")
        connection.execute("UPDATE archive_usage SET pages=pages+?,records=records+?,graphs=graphs+?,events=events+?,seen=seen+?,bytes=bytes+? WHERE singleton=1",
                           (pages, records, graphs, events, seen, size))

    def record_page(self, account, kind, payload, *, reserved=False):
        account, kind = _account(account), _kind(kind)
        if not isinstance(payload, dict) or payload.get("account_id") != account or payload.get("network") != "mainnet" or not isinstance(payload.get("source"), dict):
            raise HistoryInputError("The provider page must name its mainnet account and source.")
        items = [payload] if kind == "profile" else payload.get("items")
        if not isinstance(items, list) or len(items) > 100 or not all(isinstance(item, dict) for item in items):
            raise HistoryInputError("The provider page exceeds its row limit or is malformed.")
        body, now = _canonical(payload), self._now()
        page_id = hashlib.sha256(body).hexdigest()
        rows = [(_identity(item, kind, account), _timestamp(item, kind, now), _canonical(item)) for item in items]
        if len({(row_id, stamp) for row_id, stamp, _ in rows}) != len({row_id for row_id, _, _ in rows}):
            raise HistoryInputError("A transfer or transaction identifier changed its timestamp.")
        inserted = 0
        with self._connection(write=True) as connection:
            try:
                retained = self._retained_rows(connection, account, kind, body, rows)
            except HistoryReadError:
                retained = None
            if retained is not None:
                total = connection.execute("SELECT COUNT(*) FROM history_rows WHERE account=? AND kind=?", (account, kind)).fetchone()[0]
                return {**retained, "inserted": 0, "total_rows": total}
            stored = connection.execute("SELECT body FROM pages WHERE id=?", (page_id,)).fetchone()
            if stored is None:
                self._reserve(connection, pages=1, size=len(body), reserved=reserved)
            elif stored["body"] != body:
                raise HistoryReadError("The retained archive page bytes disagree with the identical observation.")
            connection.execute("INSERT OR IGNORE INTO pages VALUES (?,?,?,?,?)", (page_id, account, kind, body, now))
            for row_id, stamp, encoded in rows:
                previous = connection.execute("SELECT row_id,stamp,body,page_id,LENGTH(body) AS size FROM history_rows WHERE account=? AND kind=? AND row_id=?", (account, kind, row_id)).fetchone()
                if previous:
                    if kind in ("transactions", "transfers") and previous["stamp"] != stamp:
                        try:
                            _retained_records(connection, [previous], kind, account)
                            changed = True
                        except HistoryReadError:
                            changed = False
                        if changed:
                            raise HistoryConflictError("A transfer or transaction identifier changed its timestamp.")
                    self._reserve(connection, size=len(encoded) - previous["size"], reserved=reserved)
                    connection.execute("UPDATE history_rows SET stamp=?,body=?,page_id=? WHERE account=? AND kind=? AND row_id=?", (stamp, encoded, page_id, account, kind, row_id))
                else:
                    self._reserve(connection, records=1, size=len(encoded), reserved=reserved)
                    connection.execute("INSERT INTO history_rows(account,kind,row_id,stamp,body,page_id) VALUES (?,?,?,?,?,?)", (account, kind, row_id, stamp, encoded, page_id))
                    inserted += 1
            total = connection.execute("SELECT COUNT(*) FROM history_rows WHERE account=? AND kind=?", (account, kind)).fetchone()[0]
        return {"page_id": page_id, "inserted": inserted, "total_rows": total,
                "observations": [_observation(page_id, payload)], "rows": [{"page_id": page_id, "item": item} for item in items]}

    @staticmethod
    def _retained_rows(connection, account, kind, body, rows):
        current = json.loads(body)
        records = []
        for row_id, _, _ in rows:
            previous = connection.execute("SELECT row_id,stamp,body,page_id FROM history_rows WHERE account=? AND kind=? AND row_id=?", (account, kind, row_id)).fetchone()
            if previous is None:
                return None
            records.append(previous)
        items, documents = _retained_records(connection, records, kind, account)
        if not all(_same_content(item, json.loads(encoded), kind) for item, (_, _, encoded) in zip(items, rows)):
            return None
        pointers = [{"page_id": record["page_id"], "item": item} for record, item in zip(records, items)]
        if kind == "holdings":
            for stored in connection.execute("SELECT id,body FROM pages WHERE account=? AND kind=? ORDER BY created_ms DESC,rowid DESC", (account, kind)):
                document, held = _retained_page(stored, kind, account)
                if _window(document) == _window(current):
                    break
            else:
                return None
            same = (held.keys() == {row_id for row_id, _, _ in rows} and document.get("pagination") == current.get("pagination")
                    and all(_same_content(held[row_id], json.loads(encoded), kind) for row_id, _, encoded in rows))
            if not same or not rows and not _same_content(document, current, kind):
                return None
            return {"page_id": stored["id"], "observations": [_observation(stored["id"], document)],
                    "rows": [{"page_id": stored["id"], "item": held[row_id]} for row_id, _, _ in rows]}
        if not rows:
            latest = connection.execute("SELECT id,body FROM pages WHERE account=? AND kind=? ORDER BY created_ms DESC,rowid DESC LIMIT 1", (account, kind)).fetchone()
            if latest is None:
                return None
            document, _ = _retained_page(latest, kind, account)
            if not _same_content(document, current, kind):
                return None
            return {"page_id": latest["id"], "observations": [_observation(latest["id"], document)], "rows": []}
        for page_id, (document, held) in documents.items():
            if all(row_id in held and _same_content(held[row_id], json.loads(encoded), kind) for row_id, _, encoded in rows):
                return {"page_id": page_id, "observations": [_observation(page_id, document)],
                        "rows": [{"page_id": page_id, "item": held[row_id]} for row_id, _, _ in rows]}
        return {"page_id": None, "observations": [_observation(page_id, document) for page_id, (document, _) in documents.items()], "rows": pointers}

    def list_records(self, account, kind, before=0, limit=25):
        account, kind = _account(account), _kind(kind)
        if type(before) is not int or not 0 <= before <= 2 ** 63 - 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise HistoryInputError("Use a nonnegative archive cursor and page size from 1 to 100.")
        with self._connection() as connection:
            parameters, clause = [account, kind], ""
            if before:
                anchor = connection.execute("SELECT row_id,stamp,body,page_id FROM history_rows WHERE seq=? AND account=? AND kind=?", (before, account, kind)).fetchone()
                if anchor is None:
                    raise HistoryInputError("The archive cursor does not belong to this account and kind.")
                _retained_records(connection, [anchor], kind, account)
                clause = " AND (stamp<? OR (stamp=? AND seq<?))"
                parameters += [anchor["stamp"], anchor["stamp"], before]
            rows = connection.execute("SELECT seq,row_id,stamp,body,page_id FROM history_rows WHERE account=? AND kind=?" + clause + " ORDER BY stamp DESC,seq DESC LIMIT ?", [*parameters, limit + 1]).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM history_rows WHERE account=? AND kind=?", (account, kind)).fetchone()[0]
            page = rows[:limit]
            items, documents = _retained_records(connection, page, kind, account)
        return {"account_id": account, "network": "mainnet", "kind": kind,
                "items": items, "observations": [_observation(page_id, document) for page_id, (document, _) in documents.items()],
                "pagination": {"limit": limit, "next_before": page[-1]["seq"] if len(rows) > limit else None},
                "coverage": {"stored_rows": total, "history_complete": False,
                             "scope": "Retained provider observations only; archive pagination ending does not establish complete chain history."}}

    def save_graph(self, payload):
        allowed = {"kind", "root", "nodes", "edges", "records", "total_nodes", "total_edges", "total_records",
                   "bounds", "coverage", "source", "assertion_ids", "shares", "state_at_block"}
        if not isinstance(payload, dict) or set(payload) - allowed or payload.get("kind") != "flow" or not isinstance(payload.get("source"), dict) or payload["source"].get("network") != "mainnet":
            raise HistoryInputError("Only public mainnet graph observations can be archived.")
        _account(payload.get("root"))
        for name, maximum in (("nodes", 200), ("edges", 200), ("records", 200)):
            values = payload.get(name)
            if not isinstance(values, list) or len(values) > maximum or not all(isinstance(item, dict) for item in values):
                raise HistoryInputError("The graph exceeds its observation limit or is malformed.")
        body = _canonical(payload)
        identifier = hashlib.sha256(body).hexdigest()
        with self._connection(write=True) as connection:
            if connection.execute("SELECT 1 FROM graphs WHERE id=?", (identifier,)).fetchone() is None:
                self._reserve(connection, graphs=1, size=len(body))
            connection.execute("INSERT OR IGNORE INTO graphs VALUES (?,?,?)", (identifier, body, self._now()))
        return {"id": identifier, "url": "/live/account-data/" + identifier + ".json"}

    def get_graph_bytes(self, identifier):
        if not isinstance(identifier, str) or _HEX.fullmatch(identifier) is None:
            raise HistoryInputError("A graph observation ID is a lowercase SHA-256 identifier.")
        with self._connection() as connection:
            row = connection.execute("SELECT body FROM graphs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return None
        body = row["body"]
        if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != identifier:
            raise HistoryReadError("The stored graph bytes do not match their identifier.")
        _retained(body)
        return body

    def get_graph(self, identifier):
        body = self.get_graph_bytes(identifier)
        return json.loads(body) if body is not None else None

    def defer_account(self, account, kind, retry_at_ms):
        account = _account(account)
        if kind not in ("transactions", "transfers") or type(retry_at_ms) is not int or not 0 <= retry_at_ms <= 2 ** 63 - 1:
            raise HistoryInputError("A provider cooldown needs a supported history kind and timestamp.")
        with self._connection(write=True) as connection:
            connection.execute("INSERT INTO provider_cooldowns VALUES(?,?,?) ON CONFLICT(account,kind) DO UPDATE SET not_before_ms=MAX(not_before_ms,excluded.not_before_ms)", (account, kind, retry_at_ms))

    def claim_job_read(self, account, kind="transfers", *, yield_to_watches=True):
        account, now, token = _account(account), self._now(), secrets.token_hex(16)
        if kind not in ("transactions", "transfers"):
            raise HistoryInputError("Choose a supported history kind for the bounded read.")
        with self._connection(write=True) as connection:
            cooldown = connection.execute("SELECT not_before_ms FROM provider_cooldowns WHERE account=? AND kind=?", (account, kind)).fetchone()
            if cooldown and cooldown["not_before_ms"] > now:
                raise HistoryCapacityError("The account's history source requested a cooldown; retained observations remain available.", retry_at_ms=cooldown["not_before_ms"])
            usage = connection.execute("SELECT pages,records,bytes FROM archive_usage WHERE singleton=1").fetchone()
            if usage["pages"] + 1 > MAX_ARCHIVE_PAGES or usage["records"] + 100 > MAX_ARCHIVE_ROWS or usage["bytes"] + JOB_PAGE_BYTES > MAX_ARCHIVE_BYTES - EVENT_ARCHIVE_BYTES:
                raise HistoryCapacityError("Archive retention capacity reached; retained evidence remains available and new observations are paused.")
            state = connection.execute("SELECT heartbeat_ms,last_tick_ms FROM history_state WHERE singleton=1").fetchone()
            heartbeat = state["heartbeat_ms"]
            if (yield_to_watches and heartbeat is not None and 0 <= now - heartbeat <= 30000
                    and (state["last_tick_ms"] is None or now - state["last_tick_ms"] >= GLOBAL_INTERVAL_MS)
                    and connection.execute("SELECT 1 FROM watch_poll WHERE next_due_ms<=? AND lease_until_ms<=? LIMIT 1", (now, now)).fetchone() is not None):
                raise WatchPriorityError("A watched account is due; background investigations yield to the watch monitor.")
            active = connection.execute("SELECT MAX(last_start_ms) AS last_start,MAX(lease_until_ms) AS lease_until FROM backfill_control WHERE account=?", (account,)).fetchone()
            if active["lease_until"] is not None and active["lease_until"] > now:
                raise HistoryCapacityError("This account already has an active bounded read.")
            if active["last_start"] is not None and now - active["last_start"] < ACCOUNT_INTERVAL_MS:
                raise HistoryCapacityError("Account history budget reached; retry after one minute.")
            budget = connection.execute("SELECT window,pages FROM job_read_budget WHERE singleton=1").fetchone()
            window = now // 60000
            reserved = budget["pages"] if budget["window"] == window else 0
            if reserved >= JOB_BACKFILL_PAGES:
                raise HistoryCapacityError("Global history budget reached; retry after one minute.")
            if connection.execute("SELECT 1 FROM backfill_control WHERE account=? AND kind='job'", (account,)).fetchone() is None and connection.execute("SELECT COUNT(*) FROM backfill_control").fetchone()[0] >= 20000:
                connection.execute("DELETE FROM backfill_control WHERE lease_until_ms<=? AND last_start_ms<=?", (now, now - ACCOUNT_INTERVAL_MS))
                if connection.execute("SELECT COUNT(*) FROM backfill_control").fetchone()[0] >= 20000:
                    raise HistoryCapacityError("History read checkpoint capacity reached.")
            connection.execute("UPDATE job_read_budget SET window=?,pages=? WHERE singleton=1", (window, reserved + 1))
            connection.execute("INSERT INTO backfill_control VALUES (?,'job',?,?,?) ON CONFLICT(account,kind) DO UPDATE SET last_start_ms=excluded.last_start_ms,lease_token=excluded.lease_token,lease_until_ms=excluded.lease_until_ms", (account, now, token, now + LEASE_MS))
        return token

    def release_job_read(self, account, token):
        with self._connection(write=True) as connection:
            connection.execute("UPDATE backfill_control SET lease_token=NULL,lease_until_ms=0 WHERE account=? AND kind='job' AND lease_token=?", (account, token))

    def backfill_account(self, reader, account, kind, cursor=None, max_pages=2):
        account, kind, cursor = _account(account), _kind(kind), _cursor(cursor)
        if kind not in ("transactions", "transfers") or type(max_pages) is not int or not 1 <= max_pages <= 2:
            raise HistoryInputError("Backfills read one or two transaction or transfer pages.")
        now, token = self._now(), secrets.token_hex(16)
        with self._connection(write=True) as connection:
            state = connection.execute("SELECT cursor,exhausted FROM backfills WHERE account=? AND kind=?", (account, kind)).fetchone()
            expected = state["cursor"] if state else None
            if cursor is not None and (state is None or state["exhausted"] or cursor != expected):
                raise HistoryInputError("The backfill cursor must match the archive's committed continuation.")
            if state and state["exhausted"]:
                return {"pages": 0, "inserted": 0, "next_cursor": None,
                        "coverage": {"provider_pages_exhausted": True, "history_complete": False}}
            cooldown = connection.execute("SELECT not_before_ms FROM provider_cooldowns WHERE account=? AND kind=?", (account, kind)).fetchone()
            if cooldown and cooldown["not_before_ms"] > now:
                raise HistoryCapacityError("The account's history source requested a cooldown; retained observations remain available.", retry_at_ms=cooldown["not_before_ms"])
            cursor = expected
            active = connection.execute("SELECT MAX(last_start_ms) AS last_start,MAX(lease_until_ms) AS lease_until FROM backfill_control WHERE account=?", (account,)).fetchone()
            if active["lease_until"] is not None and active["lease_until"] > now:
                raise HistoryCapacityError("This account already has an active backfill; retry after the bounded read finishes.")
            if active["last_start"] is not None and now - active["last_start"] < ACCOUNT_INTERVAL_MS:
                raise HistoryCapacityError("Account backfill budget reached; retry after one minute. Retained history remains available.")
            budget = connection.execute("SELECT window,pages FROM backfill_budget WHERE singleton=1").fetchone()
            window = now // 60000
            reserved = budget["pages"] if budget["window"] == window else 0
            if reserved + max_pages > PUBLIC_BACKFILL_PAGES:
                raise HistoryCapacityError("The public backfill share of the global budget is used up; retry after one minute. Retained history remains available.")
            if state is None:
                if connection.execute("SELECT COUNT(*) FROM backfills").fetchone()[0] >= 10000:
                    idle = connection.execute("SELECT account,kind FROM backfills AS b WHERE NOT EXISTS (SELECT 1 FROM backfill_control AS c WHERE c.account=b.account AND c.kind=b.kind AND (c.lease_until_ms>? OR c.last_start_ms>?)) ORDER BY exhausted DESC,updated_ms,account,kind LIMIT 1", (now, now - ACCOUNT_INTERVAL_MS)).fetchone()
                    if idle is None:
                        raise HistoryCapacityError("Archive backfill checkpoint capacity reached; existing evidence remains available.")
                    connection.execute("DELETE FROM backfills WHERE account=? AND kind=?", (idle["account"], idle["kind"]))
                    connection.execute("DELETE FROM backfill_control WHERE account=? AND kind=?", (idle["account"], idle["kind"]))
                connection.execute("INSERT INTO backfills VALUES (?,?,NULL,0,?)", (account, kind, now))
            connection.execute("UPDATE backfill_budget SET window=?,pages=? WHERE singleton=1", (window, reserved + max_pages))
            connection.execute("INSERT INTO backfill_control VALUES (?,?,?,?,?) ON CONFLICT(account,kind) DO UPDATE SET last_start_ms=excluded.last_start_ms,lease_token=excluded.lease_token,lease_until_ms=excluded.lease_until_ms", (account, kind, now, token, now + LEASE_MS))
        pages, inserted = 0, 0
        try:
            with rpc.request_deadline(20):
                for _ in range(max_pages):
                    payload = getattr(reader, kind)(account, cursor=cursor, limit=100)
                    next_cursor = _cursor(payload.get("pagination", {}).get("next_cursor"))
                    if cursor is not None and next_cursor == cursor:
                        raise HistoryReadError("The provider repeated the backfill cursor.")
                    with self._connection() as connection:
                        lease = connection.execute("SELECT lease_token,lease_until_ms FROM backfill_control WHERE account=? AND kind=?", (account, kind)).fetchone()
                    if lease is None or lease["lease_token"] != token or lease["lease_until_ms"] <= self._now():
                        raise HistoryReadError("The backfill lease expired before its continuation could be committed.")
                    saved = self.record_page(account, kind, payload)
                    pages += 1
                    inserted += saved["inserted"]
                    with self._connection(write=True) as connection:
                        committed = connection.execute("UPDATE backfills SET cursor=?,exhausted=?,updated_ms=? WHERE account=? AND kind=? AND EXISTS (SELECT 1 FROM backfill_control WHERE account=? AND kind=? AND lease_token=? AND lease_until_ms>?)", (next_cursor, int(next_cursor is None), self._now(), account, kind, account, kind, token, self._now())).rowcount
                        if not committed:
                            raise HistoryReadError("The backfill lease expired before its continuation could be committed.")
                    cursor = next_cursor
                    if cursor is None:
                        break
        except AccountReadError as error:
            if error.retry_at_ms is not None:
                self.defer_account(account, kind, error.retry_at_ms)
            raise
        finally:
            with self._connection(write=True) as connection:
                connection.execute("UPDATE backfill_control SET lease_token=NULL,lease_until_ms=0 WHERE account=? AND kind=? AND lease_token=?", (account, kind, token))
        return {"pages": pages, "inserted": inserted, "next_cursor": cursor,
                "coverage": {"provider_pages_exhausted": cursor is None, "history_complete": False}}

    @staticmethod
    def _event(item):
        sender, receiver = item.get("from"), item.get("to")
        endpoints = sorted({value for value in (sender, receiver) if isinstance(value, str)})
        if not endpoints:
            raise HistoryInputError("A transfer event needs a known account endpoint.")
        document = {"kind": "feed_event", "version": "1", "signal": "account_transfer_observed",
                    "subject": sender or receiver, "other_account_id": receiver if sender else None,
                    "accounts": endpoints, "asset_id": item["asset_id"], "amount": item["amount"],
                    "block_timestamp": item["block_timestamp"], "receipt_id": item["receipt_id"],
                    "transfer_index": item["transfer_index"], "tx_hash": item.get("transaction_hash"),
                    "detail": "Indexed transfer of " + item["amount"] + " base units of " + item["asset_id"] +
                              " from " + (sender or "an unknown account") + " to " + (receiver or "an unknown account") + ".",
                    "observed": {"block_height": int(item["block_height"]), "timestamp_ms": int(item["block_timestamp"]) // 1000000},
                    "source": {"provider": "transfers.main.fastnear.com", "receipt_id": item["receipt_id"],
                               "transfer_index": item["transfer_index"], "network": "mainnet"}}
        if item["asset_id"] == "native:near":
            document["amount_yocto"] = item["amount"]
        body = _canonical(document)
        return hashlib.sha256(body).hexdigest(), body

    def _flush_events(self, store):
        with self._connection() as connection:
            rows = connection.execute("SELECT id,body,notify FROM event_outbox WHERE delivered_seq IS NULL ORDER BY created_ms,id LIMIT 200").fetchall()
        for row in rows:
            try:
                sequence = store.ingest_event(row["id"], json.loads(row["body"]), notify=bool(row["notify"]))
            except (OSError, ValueError, sqlite3.Error) as error:
                with self._connection(write=True) as connection:
                    connection.execute("UPDATE history_state SET last_error=? WHERE singleton=1", (type(error).__name__,))
                break
            with self._connection(write=True) as connection:
                connection.execute("UPDATE event_outbox SET delivered_seq=? WHERE id=?", (sequence, row["id"]))

    def get_event_bytes(self, identifier):
        if not isinstance(identifier, str) or _HEX.fullmatch(identifier) is None:
            raise HistoryInputError("An event observation ID is a lowercase SHA-256 identifier.")
        with self._connection() as connection:
            row = connection.execute("SELECT body FROM event_outbox WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return None
        if not isinstance(row["body"], bytes) or hashlib.sha256(row["body"]).hexdigest() != identifier:
            raise HistoryReadError("The stored event bytes do not match their identifier.")
        _retained(row["body"])
        return row["body"]

    def poll_watches(self, workspace_store, reader, limit=1):
        if type(limit) is not int or limit != 1:
            raise HistoryInputError("Each watch tick reads at most one account.")
        now = self._now()
        schedule = workspace_store.watch_schedule()
        watched = [_account(row["account"]) for row in schedule]
        claim = None
        try:
            with self._connection(write=True) as connection:
                known = {row[0] for row in connection.execute("SELECT account FROM watch_poll")} | {row[0] for row in connection.execute("SELECT DISTINCT account FROM watch_seen")}
                stale = [(account,) for account in known - set(watched)]
                deleted = connection.executemany("DELETE FROM watch_seen WHERE account=?", stale).rowcount
                connection.executemany("DELETE FROM watch_poll WHERE account=?", stale)
                self._reserve(connection, seen=-deleted)
                for row, account in zip(schedule, watched):
                    connection.execute("INSERT OR IGNORE INTO watch_poll(account,next_due_ms,added_ms,reserved) VALUES (?,?,?,?)", (account, now, row["created_ms"], row["reserved"]))
                    connection.execute("UPDATE watch_poll SET reserved=?,next_due_ms=MAX(next_due_ms,COALESCE((SELECT not_before_ms FROM provider_cooldowns WHERE account=? AND kind='transfers'),0)) WHERE account=?", (row["reserved"], account, account))
                state = connection.execute("SELECT last_tick_ms,tick_count FROM history_state WHERE singleton=1").fetchone()
                if state["last_tick_ms"] is None or now - state["last_tick_ms"] >= GLOBAL_INTERVAL_MS:
                    lanes = (("reserved", "fresh", "rest"), ("fresh", "reserved", "rest"),
                             ("reserved", "fresh", "rest"), ("rest", "reserved", "fresh"))
                    due = None
                    for lane in lanes[state["tick_count"] % 4]:
                        if lane == "reserved":
                            due = connection.execute("SELECT * FROM watch_poll WHERE next_due_ms<=? AND lease_until_ms<=? AND reserved=1 ORDER BY initialized,(added_ms>?) DESC,next_due_ms,account LIMIT 1", (now, now, now - FRESH_MS)).fetchone()
                        elif lane == "fresh":
                            due = connection.execute("SELECT * FROM watch_poll WHERE next_due_ms<=? AND lease_until_ms<=? AND reserved=0 AND added_ms>? ORDER BY initialized,next_due_ms,account LIMIT 1", (now, now, now - FRESH_MS)).fetchone()
                        else:
                            due = connection.execute("SELECT * FROM watch_poll WHERE next_due_ms<=? AND lease_until_ms<=? AND reserved=0 AND added_ms<=? ORDER BY next_due_ms,account LIMIT 1", (now, now, now - FRESH_MS)).fetchone()
                        if due:
                            break
                    if due:
                        token = secrets.token_hex(16)
                        connection.execute("UPDATE watch_poll SET lease_token=?,lease_until_ms=? WHERE account=?", (token, now + LEASE_MS, due["account"]))
                        connection.execute("UPDATE history_state SET last_tick_ms=?,tick_count=tick_count+1 WHERE singleton=1", (now,))
                        claim = dict(due), token
        except sqlite3.Error as error:
            claim = None
            try:
                with self._connection(write=True) as connection:
                    connection.execute("UPDATE history_state SET last_error=? WHERE singleton=1", (type(error).__name__,))
            except sqlite3.Error as failure:
                print("history watch tick failed (" + type(error).__name__ + ") and could not record it (" + type(failure).__name__ + ")", file=sys.stderr)
        self._flush_events(workspace_store)
        if claim:
            state, token = claim
            account = state["account"]
            try:
                found_anchor, pending, more, skipped = False, [], False, False
                with rpc.request_deadline(20):
                    payload = reader.transfers(account, cursor=None, limit=100)
                    try:
                        self.record_page(account, "transfers", payload, reserved=True)
                    except (HistoryCapacityError, HistoryConflictError):
                        skipped = True
                    items = payload["items"]
                    with self._connection() as connection:
                        known = {row[0] for row in connection.execute("SELECT row_id FROM watch_seen WHERE account=?", (account,))}
                    for item in items:
                        found_anchor = found_anchor or item["id"] in known
                        pending.append(item)
                    more = _cursor(payload.get("pagination", {}).get("next_cursor")) is not None
                with self._connection(write=True) as connection:
                    active = connection.execute("SELECT lease_token FROM watch_poll WHERE account=?", (account,)).fetchone()
                    if active is not None and active["lease_token"] == token:
                        dropped = connection.execute("DELETE FROM watch_seen WHERE rowid IN (SELECT rowid FROM watch_seen WHERE account=? ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (account, MAX_ACCOUNT_WATCH_SEEN)).rowcount
                        self._reserve(connection, seen=-dropped)
                        held = None
                        for item in pending:
                            inserted = connection.execute("INSERT OR IGNORE INTO watch_seen VALUES (?,?)", (account, item["id"])).rowcount
                            if inserted:
                                dropped = connection.execute("DELETE FROM watch_seen WHERE rowid IN (SELECT rowid FROM watch_seen WHERE account=? ORDER BY rowid DESC LIMIT -1 OFFSET ?)", (account, MAX_ACCOUNT_WATCH_SEEN)).rowcount
                                excess = connection.execute("SELECT seen FROM archive_usage WHERE singleton=1").fetchone()[0] + 1 - dropped - MAX_WATCH_SEEN
                                if excess > 0:
                                    dropped += connection.execute("DELETE FROM watch_seen WHERE rowid IN (SELECT rowid FROM watch_seen ORDER BY rowid LIMIT ?)", (excess,)).rowcount
                                self._reserve(connection, seen=1 - dropped, reserved=True)
                            if inserted and (state["initialized"] or (state["added_ms"] and int(_timestamp(item, "transfers", now)) >= state["added_ms"] * 1000000)):
                                event_id, body = self._event(item)
                                if connection.execute("SELECT 1 FROM event_outbox WHERE id=?", (event_id,)).fetchone() is None:
                                    if held is None:
                                        held = connection.execute("SELECT COALESCE(SUM(LENGTH(body)),0) FROM event_outbox").fetchone()[0]
                                    usage = connection.execute("SELECT events,bytes FROM archive_usage WHERE singleton=1").fetchone()
                                    events = usage["events"] + 1 - MAX_ARCHIVE_EVENTS
                                    size = min(usage["bytes"] + len(body) - MAX_ARCHIVE_BYTES, held + len(body) - EVENT_ARCHIVE_BYTES)
                                    old = []
                                    if events > 0 or size > 0:
                                        for row in connection.execute("SELECT id,LENGTH(body) AS size FROM event_outbox WHERE delivered_seq IS NOT NULL ORDER BY created_ms,id"):
                                            if events <= 0 and size <= 0:
                                                break
                                            old.append(row)
                                            events, size = events - 1, size - row["size"]
                                    if events <= 0 and size <= 0:
                                        freed = sum(row["size"] for row in old)
                                        connection.executemany("DELETE FROM event_outbox WHERE id=?", [(row["id"],) for row in old])
                                        self._reserve(connection, events=-len(old), size=-freed)
                                        held -= freed
                                    self._reserve(connection, events=1, size=len(body), reserved=True, held=held)
                                    held += len(body)
                                connection.execute("INSERT OR IGNORE INTO event_outbox VALUES (?,?,?,?,NULL)", (event_id, body, 1, now))
                        gap = int(skipped or bool(state["initialized"]) and more and not found_anchor)
                        connection.execute("UPDATE watch_poll SET initialized=1,next_due_ms=?,last_success_ms=?,attempts=0,last_error=NULL,coverage_gap=MAX(coverage_gap,?),lease_token=NULL,lease_until_ms=0 WHERE account=? AND lease_token=?", (self._now() + ACCOUNT_INTERVAL_MS, self._now(), gap, account, token))
                        connection.execute("UPDATE history_state SET last_success_ms=?,last_error=NULL WHERE singleton=1", (self._now(),))
            except (AccountReadError, AccountInputError, HistoryInputError, HistoryReadError, OSError, sqlite3.Error) as error:
                code = "provider_timeout" if isinstance(error, TimeoutError) else type(error).__name__
                if isinstance(error, AccountReadError) and error.status is not None:
                    code = "provider_http_" + str(error.status)
                attempts = state["attempts"] + 1
                delay = min(300000, ACCOUNT_INTERVAL_MS * 2 ** min(attempts, 3))
                provider_retry = error.retry_at_ms if isinstance(error, AccountReadError) else None
                if provider_retry is not None:
                    self.defer_account(account, "transfers", provider_retry)
                with self._connection(write=True) as connection:
                    connection.execute("UPDATE watch_poll SET attempts=?,last_error=?,next_due_ms=?,lease_token=NULL,lease_until_ms=0 WHERE account=? AND lease_token=?", (attempts, code, max(self._now() + delay, provider_retry or 0), account, token))
                    connection.execute("UPDATE history_state SET last_error=? WHERE singleton=1", (code,))
            self._flush_events(workspace_store)
        with self._connection(write=True) as connection:
            connection.execute("UPDATE history_state SET heartbeat_ms=? WHERE singleton=1", (self._now(),))
        return self.status()

    def status(self):
        return read_status(self.path, clock=self.clock)


def run(workspace_store, reader, path, *, once=False, interval=5, stop_event=None):
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 5 <= interval <= 60:
        raise HistoryInputError("History polling interval must be between 5 and 60 seconds.")
    archive = AccountHistory(path)
    if not once:
        with archive._connection(write=True) as connection:
            connection.execute("UPDATE history_state SET mode='service',stopped_ms=NULL WHERE singleton=1")
    try:
        while True:
            try:
                try:
                    archive.poll_watches(workspace_store, reader)
                except (OSError, sqlite3.Error) as error:
                    if once:
                        raise
                    print("history watch tick failed (" + type(error).__name__ + "); retrying after the polling interval", file=sys.stderr)
                if once:
                    break
                if stop_event is not None:
                    if stop_event.wait(interval):
                        break
                else:
                    time.sleep(interval)
            except KeyboardInterrupt:
                break
    finally:
        if not once:
            with archive._connection(write=True) as connection:
                connection.execute("UPDATE history_state SET stopped_ms=? WHERE singleton=1", (archive._now(),))
    return archive.status()
