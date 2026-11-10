"""Index retained feed evidence and deliver new matching events to durable team inboxes."""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path


MAX_EVENT_BYTES = 128 * 1024
LEASE_MS = 30000
STALE_AFTER_MS = 30000
_EVENT_NAME = re.compile(r"([a-f0-9]{64})\.json")


def _reject_constant(value):
    raise ValueError("event contains a nonstandard JSON constant")


def _empty_status():
    return {"initialized": False, "running": False, "mode": None, "source_available": False,
            "coverage_from_ms": None, "last_scan_ms": None, "last_success_ms": None,
            "heartbeat_ms": None, "pending": 0, "backfill_pending": 0, "retry_pending": 0,
            "indexed": 0, "ignored_files": 0, "last_error": None, "event_errors": [],
            "delivery_mode": "team_inbox", "external_delivery_configured": False,
            "coverage": "Retained feed evidence only; an empty inbox does not establish no account activity."}


def read_status(state_path, *, clock=time.time):
    """Read monitor health without creating files or starting an observer."""
    result = _empty_status()
    path = Path(state_path)
    if not path.is_file():
        return result
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            meta = connection.execute("SELECT * FROM monitor_state WHERE singleton=1").fetchone()
            if meta is None:
                return result
            counts = connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(done),0) AS indexed, "
                "COALESCE(SUM(CASE WHEN done=0 AND backfill=1 THEN 1 ELSE 0 END),0) AS backfill, "
                "COALESCE(SUM(CASE WHEN done=0 AND attempts>0 THEN 1 ELSE 0 END),0) AS retry "
                "FROM monitor_files").fetchone()
            errors = connection.execute(
                "SELECT DISTINCT last_error FROM monitor_files WHERE done=0 AND last_error IS NOT NULL "
                "ORDER BY last_error LIMIT 10").fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        result["last_error"] = "state_unreadable"
        return result
    heartbeat = meta["heartbeat_ms"]
    result.update({"initialized": meta["initialized_ms"] is not None, "mode": meta["mode"],
                   "source_available": bool(meta["source_available"]),
                   "coverage_from_ms": meta["initialized_ms"], "last_scan_ms": meta["last_scan_ms"],
                   "last_success_ms": meta["last_success_ms"], "heartbeat_ms": heartbeat,
                   "pending": counts["total"] - counts["indexed"], "indexed": counts["indexed"],
                   "backfill_pending": counts["backfill"], "retry_pending": counts["retry"],
                   "ignored_files": meta["ignored_files"], "last_error": meta["last_error"],
                   "event_errors": [row[0] for row in errors]})
    result["running"] = (meta["mode"] == "service" and meta["stopped_ms"] is None
                         and heartbeat is not None and 0 <= int(clock() * 1000) - heartbeat <= STALE_AFTER_MS
                         and result["source_available"])
    return result


class Monitor:
    def __init__(self, store, events_dir, state_path=None, *, clock=time.time, batch_size=200, mode="once"):
        if type(batch_size) is not int or not 1 <= batch_size <= 200:
            raise ValueError("monitor batch size must be between 1 and 200")
        if mode not in ("once", "service"):
            raise ValueError("monitor mode must be once or service")
        self.store = store
        self.events_dir = Path(events_dir).resolve()
        self.state_path = Path(state_path) if state_path is not None else Path(store.path).parent / "monitor.sqlite3"
        if self.state_path.resolve() == Path(store.path).resolve():
            raise ValueError("monitor state must be separate from the workspace database")
        self.clock, self.batch_size, self.mode = clock, batch_size, mode
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("monitor database version is unsupported")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS monitor_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), source_dir TEXT NOT NULL,
                    initialized_ms INTEGER, last_scan_ms INTEGER, last_success_ms INTEGER,
                    heartbeat_ms INTEGER, stopped_ms INTEGER, mode TEXT NOT NULL,
                    source_available INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                    ignored_files INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS monitor_files (
                    event_id TEXT PRIMARY KEY, backfill INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_ms INTEGER NOT NULL DEFAULT 0,
                    lease_until_ms INTEGER NOT NULL DEFAULT 0, lease_token TEXT,
                    last_error TEXT, event_seq INTEGER
                );
                CREATE INDEX IF NOT EXISTS monitor_pending ON monitor_files(done,next_attempt_ms,lease_until_ms);
                PRAGMA user_version=1;
            """)
            connection.execute("INSERT OR IGNORE INTO monitor_state(singleton,source_dir,mode) VALUES(1,?,?)",
                               (str(self.events_dir), mode))
            source = connection.execute("SELECT source_dir FROM monitor_state WHERE singleton=1").fetchone()[0]
            if Path(source) != self.events_dir:
                raise ValueError("monitor state belongs to a different evidence directory")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.state_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _discover(self, now):
        try:
            if not self.events_dir.is_dir():
                raise FileNotFoundError("feed evidence directory is unavailable")
            names, ignored = [], 0
            for path in self.events_dir.iterdir():
                match = _EVENT_NAME.fullmatch(path.name)
                if match:
                    names.append(match[1])
                elif path.suffix == ".json":
                    ignored += 1
        except OSError as error:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE monitor_state SET heartbeat_ms=?,source_available=0,last_error=?,mode=?,stopped_ms=NULL "
                    "WHERE singleton=1", (now, type(error).__name__, self.mode))
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            initialized = connection.execute("SELECT initialized_ms FROM monitor_state WHERE singleton=1").fetchone()[0]
            connection.executemany("INSERT OR IGNORE INTO monitor_files(event_id,backfill) VALUES(?,?)",
                                   ((name, initialized is None) for name in names))
            connection.execute(
                "UPDATE monitor_state SET initialized_ms=COALESCE(initialized_ms,?),last_scan_ms=?,heartbeat_ms=?,"
                "source_available=1,last_error=NULL,ignored_files=?,mode=?,stopped_ms=NULL WHERE singleton=1",
                (now, now, now, ignored, self.mode))
        return True

    def _claim(self, now):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT event_id,backfill FROM monitor_files WHERE done=0 AND next_attempt_ms<=? "
                "AND lease_until_ms<=? ORDER BY attempts,event_id LIMIT ?", (now, now, self.batch_size)).fetchall()
            claims = []
            for row in rows:
                token = secrets.token_hex(16)
                connection.execute("UPDATE monitor_files SET lease_token=?,lease_until_ms=? WHERE event_id=?",
                                   (token, now + LEASE_MS, row["event_id"]))
                claims.append((row["event_id"], bool(row["backfill"]), token))
            return claims

    def _read_event(self, event_id):
        path = self.events_dir / (event_id + ".json")
        resolved = path.resolve()
        if resolved.parent != self.events_dir or not resolved.is_file():
            raise ValueError("event path is outside the evidence directory or unavailable")
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("event is not a regular file")
            body = handle.read(MAX_EVENT_BYTES + 1)
        if len(body) > MAX_EVENT_BYTES:
            raise ValueError("event exceeds the size limit")
        if hashlib.sha256(body).hexdigest() != event_id:
            raise ValueError("event bytes do not match their evidence identifier")
        document = json.loads(body, parse_constant=_reject_constant)
        if not isinstance(document, dict) or document.get("kind") != "feed_event":
            raise ValueError("event document is not a feed event")
        observed = document.get("observed")
        if (not isinstance(observed, dict) or type(observed.get("block_height")) is not int
                or observed["block_height"] < 0 or type(observed.get("timestamp_ms")) is not int
                or not 0 <= observed["timestamp_ms"] <= 8640000000000000
                or not all(isinstance(document.get(name), str) and document[name].strip()
                           for name in ("signal", "subject", "detail"))
                or not isinstance(document.get("source"), dict)):
            raise ValueError("event fields are unreadable")
        return document

    def _finish(self, event_id, token, seq, now):
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE monitor_files SET done=1,event_seq=?,lease_token=NULL,lease_until_ms=0,last_error=NULL "
                "WHERE event_id=? AND lease_token=?", (seq, event_id, token)).rowcount
            if changed:
                connection.execute("UPDATE monitor_state SET last_success_ms=? WHERE singleton=1", (now,))
            return bool(changed)

    def _retry(self, event_id, token, error, now):
        with self._connect() as connection:
            row = connection.execute("SELECT attempts FROM monitor_files WHERE event_id=? AND lease_token=?",
                                     (event_id, token)).fetchone()
            if row is None:
                return
            attempts = row[0] + 1
            connection.execute(
                "UPDATE monitor_files SET attempts=?,next_attempt_ms=?,lease_token=NULL,lease_until_ms=0,last_error=? "
                "WHERE event_id=? AND lease_token=?",
                (attempts, now + min(300, 2 ** min(attempts, 9)) * 1000, type(error).__name__, event_id, token))

    def run_once(self):
        now = int(self.clock() * 1000)
        processed = 0
        if self._discover(now):
            for event_id, backfill, token in self._claim(now):
                try:
                    document = self._read_event(event_id)
                    seq = self.store.ingest_event(event_id, document, notify=not backfill)
                except (OSError, ValueError, sqlite3.Error) as error:
                    self._retry(event_id, token, error, int(self.clock() * 1000))
                else:
                    processed += self._finish(event_id, token, seq, int(self.clock() * 1000))
        with self._connect() as connection:
            connection.execute("UPDATE monitor_state SET heartbeat_ms=? WHERE singleton=1", (int(self.clock() * 1000),))
        return {**self.status(), "processed_this_run": processed}

    def status(self):
        return read_status(self.state_path, clock=self.clock)

    def stop(self):
        if self.mode == "service":
            with self._connect() as connection:
                connection.execute("UPDATE monitor_state SET stopped_ms=? WHERE singleton=1", (int(self.clock() * 1000),))


def run(store, events_dir, state_path=None, *, once=False, interval=5, stop_event=None):
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 1 <= interval <= 10:
        raise ValueError("monitor interval must be between 1 and 10 seconds")
    observer = Monitor(store, events_dir, state_path, mode="once" if once else "service")
    processed = 0
    try:
        while True:
            try:
                result = observer.run_once()
                processed = result["processed_this_run"]
                if once or stop_event is not None and stop_event.wait(interval):
                    break
                if stop_event is None:
                    time.sleep(interval)
            except KeyboardInterrupt:
                break
    finally:
        observer.stop()
    return {**observer.status(), "processed_this_run": processed}
