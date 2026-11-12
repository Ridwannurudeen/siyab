"""Private, bounded investigation jobs advanced one provider page per worker tick."""

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from gate import canon, rpc
from intel import accounts, history

MAX_TEAM_JOBS = 100
MAX_ACTIVE_JOBS = 10
MAX_JOB_BYTES = 64 * 1024 * 1024
MAX_TEAM_JOB_BYTES = 16 * 1024 * 1024
MAX_WATCH_YIELDS = 3
MAX_CAPACITY_WAITS = 3
MIN_JOB_ROOM = 2048
CAPACITY_STOP_BYTES = len('"job_capacity"') - len("null")
RETENTION_MS = 30 * 86400 * 1000
MAX_EXPIRED_PER_WRITE = 1000


class JobInputError(ValueError):
    """A queued investigation exceeds its supported bounds."""


class JobNotFound(ValueError):
    """The requested job does not exist within the supplied team."""


class JobCapacityError(ValueError):
    """Private job retention capacity has been reached."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise JobInputError("A trusted workspace identity is required.")
    return value


def _remove(connection, row):
    items = connection.execute("SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))+256),0) FROM job_items WHERE job=?", (row["id"],)).fetchone()[0]
    reads = connection.execute("SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0) FROM job_reads WHERE job=?", (row["id"],)).fetchone()[0]
    connection.execute("DELETE FROM job_items WHERE job=?", (row["id"],))
    connection.execute("DELETE FROM job_reads WHERE job=?", (row["id"],))
    connection.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
    size = len((row["request"] + row["progress"]).encode("utf-8")) + 512 + items + reads
    connection.execute("UPDATE job_usage SET bytes=MAX(0,bytes-?) WHERE singleton=1", (size,))
    connection.execute("UPDATE job_team_usage SET bytes=MAX(0,bytes-?) WHERE team=?", (size, row["team"]))
    connection.execute("DELETE FROM job_team_usage WHERE team=? AND NOT EXISTS(SELECT 1 FROM jobs WHERE team=?)", (row["team"], row["team"]))


def _remove_oldest(connection, team):
    row = connection.execute("SELECT id,team,request,progress FROM jobs WHERE team=? AND state IN ('completed','cancelled','failed') ORDER BY created_ms,id LIMIT 1", (team,)).fetchone()
    if row is None:
        return False
    _remove(connection, row)
    return True


def _remove_finished(connection, finished_by_ms):
    row = connection.execute("SELECT id,team,request,progress FROM jobs INDEXED BY finished_jobs WHERE state IN ('completed','cancelled','failed') AND updated_ms<=? ORDER BY updated_ms,id LIMIT 1", (finished_by_ms,)).fetchone()
    if row is None:
        return False
    _remove(connection, row)
    return True


def _expire(connection, now):
    for _ in range(MAX_EXPIRED_PER_WRITE):
        if not _remove_finished(connection, now - RETENTION_MS):
            break


class InvestigationJobs:
    def __init__(self, path, history_store, *, clock=time.time):
        self.path, self.history, self.clock = Path(path), history_store, clock
        if self.path.resolve() == history_store.path.resolve() or self.path.is_symlink():
            raise JobInputError("Private jobs require their own external database.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection(transaction=False) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            if connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1, 2):
                raise JobInputError("The investigation job schema is unsupported.")
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,team TEXT NOT NULL,actor TEXT NOT NULL,request TEXT NOT NULL,progress TEXT NOT NULL,state TEXT NOT NULL,created_ms INTEGER NOT NULL,updated_ms INTEGER NOT NULL,not_before_ms INTEGER NOT NULL,lease_token TEXT,lease_until_ms INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT);
                CREATE INDEX IF NOT EXISTS team_jobs ON jobs(team,created_ms DESC,id);
                CREATE INDEX IF NOT EXISTS due_jobs ON jobs(state,not_before_ms,updated_ms);
                CREATE TABLE IF NOT EXISTS job_items(seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT NOT NULL REFERENCES jobs(id),row_id TEXT NOT NULL,body TEXT NOT NULL,page_id TEXT NOT NULL,UNIQUE(job,row_id));
                CREATE TABLE IF NOT EXISTS job_reads(seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT NOT NULL REFERENCES jobs(id),body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS job_usage(singleton INTEGER PRIMARY KEY CHECK(singleton=1),bytes INTEGER NOT NULL);
                INSERT OR IGNORE INTO job_usage VALUES(1,0);
                CREATE TABLE IF NOT EXISTS job_worker(singleton INTEGER PRIMARY KEY CHECK(singleton=1),mode TEXT,heartbeat_ms INTEGER,stopped_ms INTEGER);
                INSERT OR IGNORE INTO job_worker(singleton) VALUES(1);
                COMMIT;
            """)
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] in (0, 1):
                connection.execute("ALTER TABLE jobs ADD COLUMN provider_retry_ms INTEGER NOT NULL DEFAULT 0")
            columns = {column["name"] for column in connection.execute("PRAGMA table_info(jobs)")}
            for column in ("watch_yields", "capacity_waits", "capacity_need"):
                if column not in columns:
                    connection.execute("ALTER TABLE jobs ADD COLUMN " + column + " INTEGER NOT NULL DEFAULT 0")
            connection.execute("CREATE INDEX IF NOT EXISTS job_reads_job ON job_reads(job)")
            connection.execute("CREATE INDEX IF NOT EXISTS finished_jobs ON jobs(updated_ms,id) WHERE state IN ('completed','cancelled','failed')")
            connection.execute("CREATE TABLE IF NOT EXISTS job_team_usage(team TEXT PRIMARY KEY,bytes INTEGER NOT NULL)")
            connection.execute("DELETE FROM job_team_usage")
            connection.execute("INSERT INTO job_team_usage SELECT team,SUM(LENGTH(CAST(request AS BLOB))+LENGTH(CAST(progress AS BLOB))+512"
                               "+(SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))+256),0) FROM job_items WHERE job=jobs.id)"
                               "+(SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0) FROM job_reads WHERE job=jobs.id)) FROM jobs GROUP BY team")
            connection.execute("UPDATE job_usage SET bytes=(SELECT COALESCE(SUM(bytes),0) FROM job_team_usage) WHERE singleton=1")
            connection.execute("PRAGMA user_version=2")
            connection.commit()
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self, *, transaction=True, write=False):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA max_page_count=32768")
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
    def _reserve(connection, team, size):
        spare = CAPACITY_STOP_BYTES * connection.execute("SELECT COUNT(*) FROM jobs WHERE team=? AND state IN ('queued','running','retry_wait')", (team,)).fetchone()[0]
        while connection.execute("SELECT COALESCE(MAX(bytes),0) FROM job_team_usage WHERE team=?", (team,)).fetchone()[0] + size + spare > MAX_TEAM_JOB_BYTES:
            if not _remove_oldest(connection, team):
                raise JobCapacityError("Private job retention capacity reached; existing results remain available.")
        spare = CAPACITY_STOP_BYTES * connection.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','retry_wait')").fetchone()[0]
        while connection.execute("SELECT bytes FROM job_usage WHERE singleton=1").fetchone()[0] + size + spare > MAX_JOB_BYTES:
            if not _remove_finished(connection, 2 ** 63 - 1):
                raise JobCapacityError("Private job retention capacity reached; existing results remain available.")
        connection.execute("UPDATE job_usage SET bytes=bytes+? WHERE singleton=1", (size,))
        connection.execute("INSERT INTO job_team_usage VALUES(?,?) ON CONFLICT(team) DO UPDATE SET bytes=bytes+excluded.bytes", (team, size))

    def create(self, team_id, actor_id, account, *, kind="transfers", from_timestamp_ms=None,
               to_timestamp_ms=None, max_pages=20, max_rows=None, depth=3, max_accounts=20, pages_per_account=2):
        team_id, actor_id = _identifier(team_id), _identifier(actor_id)
        try:
            account = accounts._account(account)
        except accounts.AccountInputError as error:
            raise JobInputError(str(error)) from None
        if kind not in ("transfers", "transactions", "graph"):
            raise JobInputError("Choose a transaction, transfer or graph investigation.")
        max_rows = (200 if kind == "graph" else 1000) if max_rows is None else max_rows
        for value, maximum in ((max_pages, 100), (max_rows, 200 if kind == "graph" else 5000),
                               (depth, 5), (max_accounts, 50), (pages_per_account, 5)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise JobInputError("The investigation page, row, depth or account bound is unsupported.")
        until = self._now() if to_timestamp_ms is None else to_timestamp_ms
        for value in (from_timestamp_ms, until):
            if value is not None and (type(value) is not int or not 0 <= value <= min(self._now(), canon.U64_MAX // 1000000)):
                raise JobInputError("Date bounds must be integer milliseconds up to the request time.")
        if from_timestamp_ms is not None and from_timestamp_ms >= until:
            raise JobInputError("The requested date window must have increasing bounds.")
        request = {"account_id": account, "kind": kind, "from_timestamp_ms": from_timestamp_ms,
                   "to_timestamp_ms": until, "max_pages": max_pages, "max_rows": max_rows,
                   "depth": depth, "max_accounts": max_accounts, "pages_per_account": pages_per_account}
        progress = {"pages": 0, "scanned_rows": 0, "matched_rows": 0, "more_provider_pages": False,
                    "unknown_counterparties": 0, "account_limit_rows": 0, "stop_reason": None, "snapshot": None, "graph": None,
                    "frontier": {account: {"depth": 0, "cursor": None, "pages": 0, "done": False}}}
        encoded, state = _json(request), _json(progress)
        identifier, now = secrets.token_hex(16), self._now()
        with self._connection(write=True) as connection:
            _expire(connection, now)
            if connection.execute("SELECT COUNT(*) FROM jobs WHERE team=? AND state NOT IN ('completed','cancelled','failed')", (team_id,)).fetchone()[0] >= MAX_ACTIVE_JOBS:
                raise JobCapacityError("The active investigation job limit has been reached.")
            while connection.execute("SELECT COUNT(*) FROM jobs WHERE team=?", (team_id,)).fetchone()[0] >= MAX_TEAM_JOBS:
                if not _remove_oldest(connection, team_id):
                    raise JobCapacityError("The retained or active investigation job limit has been reached.")
            connection.execute("INSERT INTO jobs(id,team,actor,request,progress,state,created_ms,updated_ms,not_before_ms) VALUES(?,?,?,?,?,'queued',?,?,?)", (identifier, team_id, actor_id, encoded, state, now, now, now))
            self._reserve(connection, team_id, len((encoded + state).encode("utf-8")) + 512)
        return self.get(team_id, identifier)

    @staticmethod
    def _row(connection, team_id, identifier):
        row = connection.execute("SELECT * FROM jobs WHERE team=? AND id=?", (_identifier(team_id), _identifier(identifier))).fetchone()
        if row is None:
            raise JobNotFound("No investigation job exists in this workspace.")
        return row

    @staticmethod
    def _shape(row):
        request, progress = json.loads(row["request"]), json.loads(row["progress"])
        return {"id": row["id"], "account_id": request["account_id"], "kind": request["kind"], "state": row["state"],
                "created_ms": row["created_ms"], "updated_ms": row["updated_ms"], "retry_at_ms": row["not_before_ms"],
                "attempts": row["attempts"], "last_error": row["last_error"], "bounds": request,
                "counts": {key: progress[key] for key in ("pages", "scanned_rows", "matched_rows")},
                "snapshot": None, "coverage": {"history_complete": False, "stop_reason": progress["stop_reason"],
                    "more_provider_pages": progress["more_provider_pages"], "account_limit_rows": progress["account_limit_rows"],
                    "unknown_counterparty_rows": progress["unknown_counterparties"],
                    "accounts_with_unread_pages": sum(value["cursor"] is not None for value in progress["frontier"].values()),
                    "scope": "Bounded indexed provider observations; provider exhaustion does not establish complete chain history."}}

    def get(self, team_id, identifier):
        with self._connection() as connection:
            return self._shape(self._row(connection, team_id, identifier))

    def list(self, team_id, limit=25):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise JobInputError("List between 1 and 100 retained jobs.")
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM jobs WHERE team=? ORDER BY created_ms DESC,id LIMIT ?", (_identifier(team_id), limit)).fetchall()
        return {"items": [self._shape(row) for row in rows]}

    def cancel(self, team_id, identifier):
        with self._connection(write=True) as connection:
            row = self._row(connection, team_id, identifier)
            if row["state"] not in ("completed", "cancelled"):
                connection.execute("UPDATE jobs SET state='cancelled',lease_token=NULL,lease_until_ms=0,updated_ms=? WHERE id=?", (self._now(), identifier))
        return self.get(team_id, identifier)

    def retry(self, team_id, identifier):
        with self._connection(write=True) as connection:
            row = self._row(connection, team_id, identifier)
            if row["state"] not in ("failed", "cancelled", "retry_wait"):
                raise JobInputError("Only a paused, failed or cancelled investigation can be retried.")
            if row["state"] != "retry_wait" and connection.execute("SELECT COUNT(*) FROM jobs WHERE team=? AND state NOT IN ('completed','cancelled','failed')", (row["team"],)).fetchone()[0] >= MAX_ACTIVE_JOBS:
                raise JobCapacityError("The active investigation job limit has been reached.")
            connection.execute("UPDATE jobs SET state='queued',attempts=0,capacity_waits=0,capacity_need=0,last_error=NULL,not_before_ms=MAX(?,provider_retry_ms),updated_ms=?,lease_token=NULL,lease_until_ms=0 WHERE id=?", (self._now(), self._now(), identifier))
            if row["state"] != "retry_wait":
                self._reserve(connection, row["team"], 0)
        return self.get(team_id, identifier)

    def result(self, team_id, identifier, *, before=0, limit=100):
        if type(before) is not int or before < 0 or before > 2 ** 63 - 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise JobInputError("Use a nonnegative result cursor and a page size from 1 to 100.")
        with self._connection() as connection:
            stored = self._row(connection, team_id, identifier)
            job = self._shape(stored)
            graph = json.loads(stored["progress"]).get("graph")
            if before and connection.execute("SELECT 1 FROM job_items WHERE seq=? AND job=?", (before, identifier)).fetchone() is None:
                raise JobInputError("The result cursor does not belong to this investigation.")
            rows = connection.execute("SELECT * FROM job_items WHERE job=? AND seq>? ORDER BY seq LIMIT ?", (identifier, before, limit + 1)).fetchall()
            observations = [json.loads(row[0]) for row in connection.execute("SELECT body FROM job_reads WHERE job=? ORDER BY seq", (identifier,))]
        return {"items": [json.loads(row["body"]) for row in rows[:limit]], "observations": observations,
                "pagination": {"limit": limit, "next_before": rows[limit - 1]["seq"] if len(rows) > limit else None},
                "snapshot": None, "graph": graph, "coverage": job["coverage"]}

    def tick(self, reader):
        now, token = self._now(), secrets.token_hex(16)
        with self._connection(write=True) as connection:
            _expire(connection, now)
            row = connection.execute("SELECT * FROM jobs WHERE ((state IN ('queued','retry_wait') AND not_before_ms<=?) OR (state='running' AND lease_until_ms<=?)) ORDER BY updated_ms,id LIMIT 1", (now, now)).fetchone()
            if row is None:
                return {"processed": 0}
            connection.execute("UPDATE jobs SET state='running',lease_token=?,lease_until_ms=?,updated_ms=? WHERE id=?", (token, now + history.LEASE_MS, now, row["id"]))
        request, progress = json.loads(row["request"]), json.loads(row["progress"])
        account_token, subject, need = None, None, row["capacity_need"]
        try:
            if progress["matched_rows"] >= request["max_rows"] or progress["pages"] >= request["max_pages"]:
                self._finish(row, token, request, progress, "row_limit" if progress["matched_rows"] >= request["max_rows"] else "page_limit")
                return {"processed": 1}
            candidates = [(state["depth"], account) for account, state in progress["frontier"].items()
                          if not state["done"] and (request["kind"] != "graph" or state["depth"] < request["depth"])]
            if not candidates:
                self._finish(row, token, request, progress, "depth_window_exhausted" if request["kind"] == "graph" else "provider_pages_exhausted")
                return {"processed": 1}
            _, subject = min(candidates)
            state = progress["frontier"][subject]
            room = max(need, MIN_JOB_ROOM)
            with self._connection(write=True) as connection:
                if connection.execute("SELECT 1 FROM jobs WHERE id=? AND state='running' AND lease_token=?", (row["id"], token)).fetchone() is None:
                    return {"processed": 1}
                self._reserve(connection, row["team"], room)
                connection.execute("UPDATE job_usage SET bytes=bytes-? WHERE singleton=1", (room,))
                connection.execute("UPDATE job_team_usage SET bytes=bytes-? WHERE team=?", (room, row["team"]))
                connection.execute("UPDATE jobs SET capacity_waits=0 WHERE id=?", (row["id"],))
            account_token = self.history.claim_job_read(subject, "transfers" if request["kind"] == "graph" else request["kind"],
                                                        yield_to_watches=row["watch_yields"] < MAX_WATCH_YIELDS)
            with rpc.request_deadline(20):
                payload = reader.history_page(subject, "transfers" if request["kind"] == "graph" else request["kind"],
                    cursor=state["cursor"], limit=min(100, request["max_rows"] - progress["matched_rows"]),
                    from_timestamp_ms=request["from_timestamp_ms"], to_timestamp_ms=request["to_timestamp_ms"])
            next_cursor = history._cursor(payload["pagination"]["next_cursor"])
            if next_cursor is not None and next_cursor == state["cursor"]:
                raise history.HistoryReadError("The provider repeated a job continuation.")
            with self._connection() as connection:
                active = connection.execute("SELECT state,lease_token,lease_until_ms FROM jobs WHERE id=?", (row["id"],)).fetchone()
            if active is None or active["state"] != "running" or active["lease_token"] != token or active["lease_until_ms"] <= self._now():
                return {"processed": 1}
            saved = self.history.record_page(subject, "transfers" if request["kind"] == "graph" else request["kind"], payload, reserved=True)
            progress["pages"] += 1
            progress["scanned_rows"] += len(payload["items"])
            state.update(cursor=next_cursor, pages=state["pages"] + 1)
            state["done"] = next_cursor is None or (request["kind"] == "graph" and state["pages"] >= request["pages_per_account"])
            selected = []
            for retained in saved["rows"]:
                item = retained["item"]
                stamp = int(item["block_timestamp"] if request["kind"] != "transactions" else item["tx_block_timestamp"])
                if stamp >= request["to_timestamp_ms"] * 1000000:
                    continue
                if request["from_timestamp_ms"] is not None and stamp < request["from_timestamp_ms"] * 1000000:
                    state["done"] = True
                    state["date_window_reached"] = True
                    continue
                if request["kind"] == "graph":
                    if item["from"] is None or item["to"] is None:
                        progress["unknown_counterparties"] += 1
                        continue
                    endpoints = [endpoint for endpoint in (item["from"], item["to"]) if endpoint not in progress["frontier"]]
                    if len(progress["frontier"]) + len(set(endpoints)) > request["max_accounts"]:
                        progress["account_limit_rows"] += 1
                        continue
                    for endpoint in endpoints:
                        progress["frontier"][endpoint] = {"depth": state["depth"] + 1, "cursor": None, "pages": 0, "done": False}
                    item = dict(item, depth=state["depth"] + 1, block=int(item["block_height"]), provenance="onchain", confidence=None)
                selected.append((retained["page_id"], item))
            progress["more_provider_pages"] = any(value["cursor"] is not None for value in progress["frontier"].values())
            observations = [{"page_id": read["page_id"], "account_id": subject, "source": read["source"], "observed_at": read["observed_at"]} for read in saved["observations"]]
            with self._connection(write=True) as connection:
                current = connection.execute("SELECT state,lease_token,lease_until_ms,progress FROM jobs WHERE id=?", (row["id"],)).fetchone()
                if current is None or current["state"] != "running" or current["lease_token"] != token or current["lease_until_ms"] <= self._now():
                    return {"processed": 1}
                extra_bytes = sum(len(_json(observation).encode("utf-8")) for observation in observations)
                for page_id, item in selected:
                    row_id = item["transaction_hash"] if request["kind"] == "transactions" else item["id"]
                    if progress["matched_rows"] >= request["max_rows"]:
                        break
                    encoded = _json(item)
                    inserted = connection.execute("INSERT OR IGNORE INTO job_items(job,row_id,body,page_id) VALUES(?,?,?,?)", (row["id"], row_id, encoded, page_id)).rowcount
                    if inserted:
                        progress["matched_rows"] += 1
                        extra_bytes += len(encoded.encode("utf-8")) + 256
                need = extra_bytes + len(_json(progress).encode("utf-8")) - len(current["progress"].encode("utf-8"))
                self._reserve(connection, row["team"], need)
                for observation in observations:
                    connection.execute("INSERT INTO job_reads(job,body) VALUES(?,?)", (row["id"], _json(observation)))
                following = [(value["depth"], account) for account, value in progress["frontier"].items() if not value["done"] and (request["kind"] != "graph" or value["depth"] < request["depth"])]
                next_due = self._now() + 60000 if following and min(following)[1] == subject else self._now()
                connection.execute("UPDATE jobs SET progress=?,state='queued',attempts=0,last_error=NULL,not_before_ms=?,updated_ms=?,lease_token=NULL,lease_until_ms=0,provider_retry_ms=0,watch_yields=0,capacity_waits=0,capacity_need=0 WHERE id=?", (_json(progress), next_due, self._now(), row["id"]))
            reason = "row_limit" if progress["matched_rows"] >= request["max_rows"] else "page_limit" if progress["pages"] >= request["max_pages"] else None
            pending = any(not value["done"] and (request["kind"] != "graph" or value["depth"] < request["depth"]) for value in progress["frontier"].values())
            if reason or not pending:
                self._finish(row, None, request, progress, reason or ("depth_window_exhausted" if request["kind"] == "graph" else "date_window_reached" if state.get("date_window_reached") else "provider_pages_exhausted"))
        except history.WatchPriorityError:
            with self._connection(write=True) as connection:
                connection.execute("UPDATE jobs SET state='queued',watch_yields=watch_yields+1,last_error='watch_priority',not_before_ms=MAX(?,provider_retry_ms),updated_ms=?,lease_token=NULL,lease_until_ms=0 WHERE id=? AND state NOT IN ('cancelled','completed') AND (lease_token=? OR lease_token IS NULL)", (self._now() + 10000, self._now(), row["id"], token))
        except history.HistoryCapacityError as error:
            cooldown = error.retry_at_ms or 0
            with self._connection(write=True) as connection:
                connection.execute("UPDATE jobs SET state='queued',last_error='history_capacity',not_before_ms=MAX(?,provider_retry_ms),provider_retry_ms=MAX(provider_retry_ms,?),updated_ms=?,lease_token=NULL,lease_until_ms=0 WHERE id=? AND state NOT IN ('cancelled','completed') AND (lease_token=? OR lease_token IS NULL)", (max(self._now() + 60000, cooldown), cooldown, self._now(), row["id"], token))
        except JobCapacityError:
            with self._connection(write=True) as connection:
                current = connection.execute("SELECT progress,capacity_waits FROM jobs WHERE id=? AND state IN ('queued','running','retry_wait') AND (lease_token=? OR lease_token IS NULL)", (row["id"], token)).fetchone()
                if current is not None and current["capacity_waits"] + 1 < MAX_CAPACITY_WAITS:
                    connection.execute("UPDATE jobs SET state='queued',capacity_waits=capacity_waits+1,capacity_need=?,last_error='history_capacity',not_before_ms=MAX(?,provider_retry_ms),updated_ms=?,lease_token=NULL,lease_until_ms=0 WHERE id=?", (need, self._now() + 60000, self._now(), row["id"]))
                elif current is not None:
                    ended = _json(dict(json.loads(current["progress"]), stop_reason="job_capacity"))
                    grown = len(ended.encode("utf-8")) - len(current["progress"].encode("utf-8"))
                    connection.execute("SAVEPOINT ending")
                    try:
                        self._reserve(connection, row["team"], 0)
                    except JobCapacityError:
                        connection.execute("ROLLBACK TO ending")
                        connection.execute("UPDATE jobs SET state='failed',capacity_waits=0,capacity_need=0,last_error='job_capacity',updated_ms=?,lease_token=NULL,lease_until_ms=0 WHERE id=?", (self._now(), row["id"]))
                    else:
                        connection.execute("UPDATE job_usage SET bytes=bytes+? WHERE singleton=1", (grown,))
                        connection.execute("UPDATE job_team_usage SET bytes=bytes+? WHERE team=?", (grown, row["team"]))
                        connection.execute("UPDATE jobs SET state='completed',progress=?,capacity_waits=0,capacity_need=0,last_error=NULL,updated_ms=?,lease_token=NULL,lease_until_ms=0 WHERE id=?", (ended, self._now(), row["id"]))
                    connection.execute("RELEASE ending")
        except (accounts.AccountReadError, accounts.AccountInputError, history.HistoryReadError, history.HistoryInputError, OSError, sqlite3.Error) as error:
            code = "provider_timeout" if isinstance(error, TimeoutError) else type(error).__name__
            attempts = row["attempts"] + 1
            cooldown = error.retry_at_ms if isinstance(error, accounts.AccountReadError) else None
            if isinstance(error, accounts.AccountReadError) and error.status is not None:
                code = "provider_http_" + str(error.status)
            if cooldown is not None and subject is not None:
                self.history.defer_account(subject, "transfers" if request["kind"] == "graph" else request["kind"], cooldown)
            next_due = max(self._now() + min(300000, 60000 * 2 ** min(attempts - 1, 2)), cooldown or 0)
            with self._connection(write=True) as connection:
                connection.execute("UPDATE jobs SET state=?,attempts=?,last_error=?,not_before_ms=MAX(?,provider_retry_ms),provider_retry_ms=MAX(provider_retry_ms,?),updated_ms=?,lease_token=NULL,lease_until_ms=0 WHERE id=? AND state NOT IN ('cancelled','completed') AND (lease_token=? OR lease_token IS NULL)",
                    ("failed" if attempts >= 3 else "retry_wait", attempts, code, next_due, cooldown or 0, self._now(), row["id"], token))
        finally:
            if account_token is not None:
                self.history.release_job_read(subject, account_token)
            with self._connection(write=True) as connection:
                connection.execute("UPDATE job_worker SET heartbeat_ms=? WHERE singleton=1", (self._now(),))
        return {"processed": 1}

    def _finish(self, row, token, request, progress, reason):
        if request["kind"] == "graph":
            with self._connection() as connection:
                records = [json.loads(item[0]) for item in connection.execute("SELECT body FROM job_items WHERE job=? ORDER BY seq", (row["id"],))]
                reads = [json.loads(item[0]) for item in connection.execute("SELECT body FROM job_reads WHERE job=? ORDER BY seq", (row["id"],))]
            grouped = {}
            for item in records:
                key = (item["from"], item["to"], item["asset_id"])
                if key not in grouped:
                    grouped[key] = {"id": "edge-" + hashlib.sha256(_json(key).encode()).hexdigest(), "from": key[0], "to": key[1], "asset_id": key[2], "record_ids": [], "provenance": "onchain", "confidence": None}
                grouped[key]["record_ids"].append(item["id"])
            graph = {"kind": "flow", "root": request["account_id"],
                "nodes": [{"id": account, "label": account, "url": "/investigate.html?account=" + account, "depth": value["depth"]} for account, value in progress["frontier"].items()],
                "edges": list(grouped.values()), "records": records, "total_nodes": len(progress["frontier"]), "total_edges": len(grouped), "total_records": len(records),
                "bounds": {"requested_depth": request["depth"], "reached_depth": max(value["depth"] for value in progress["frontier"].values()), "window": {"from_timestamp_ms": request["from_timestamp_ms"], "to_timestamp_ms": request["to_timestamp_ms"]}},
                "coverage": {"truncated": True, "history_complete": False, "stop_reason": reason,
                    "filters": {"max_pages": request["max_pages"], "max_rows": request["max_rows"], "max_accounts": request["max_accounts"], "pages_per_account": request["pages_per_account"]},
                    "completeness": {"reason": "bounded requested indexed neighborhood; not complete account history"},
                    "unknown_counterparty_rows": progress["unknown_counterparties"], "requests": progress["pages"],
                    "account_limit_rows": progress["account_limit_rows"],
                    "accounts_with_unread_pages": [account for account, value in progress["frontier"].items() if value["cursor"] is not None],
                    "unexpanded_accounts": [account for account, value in progress["frontier"].items() if value["pages"] == 0]},
                "source": {"provider": "FastNEAR Transfers API", "network": "mainnet", "recorded": False, "reads": reads},
                "assertion_ids": [], "shares": {}, "state_at_block": []}
            progress["graph"] = graph
            progress["snapshot"] = None
        progress["stop_reason"] = reason
        with self._connection(write=True) as connection:
            current = connection.execute("SELECT state,lease_token,progress FROM jobs WHERE id=?", (row["id"],)).fetchone()
            if current is None or current["state"] == "cancelled" or current["lease_token"] != token:
                return
            self._reserve(connection, row["team"], len(_json(progress).encode("utf-8")) - len(current["progress"].encode("utf-8")))
            connection.execute("UPDATE jobs SET state='completed',progress=?,updated_ms=?,lease_token=NULL,lease_until_ms=0 WHERE id=?", (_json(progress), self._now(), row["id"]))

    def status(self):
        with self._connection() as connection:
            worker = connection.execute("SELECT * FROM job_worker WHERE singleton=1").fetchone()
            counts = {row["state"]: row["count"] for row in connection.execute("SELECT state,COUNT(*) AS count FROM jobs GROUP BY state")}
            due = connection.execute("SELECT MIN(not_before_ms) FROM jobs WHERE state IN ('queued','retry_wait')").fetchone()[0]
        heartbeat = worker["heartbeat_ms"]
        return {"running": worker["mode"] == "service" and worker["stopped_ms"] is None and heartbeat is not None and 0 <= self._now() - heartbeat <= 30000,
                "heartbeat_ms": heartbeat, "counts": counts, "next_due_ms": due,
                "coverage": "One provider page per tick; requested limits, account budgets and watch priority may delay progress."}


def run(path, history_store, reader, *, once=False, interval=5, stop_event=None):
    if type(interval) not in (int, float) or not 5 <= interval <= 60:
        raise JobInputError("The job polling interval must be between 5 and 60 seconds.")
    jobs = InvestigationJobs(path, history_store)
    if not once:
        with jobs._connection(write=True) as connection:
            connection.execute("UPDATE job_worker SET mode='service',stopped_ms=NULL,heartbeat_ms=? WHERE singleton=1", (jobs._now(),))
    try:
        while True:
            try:
                try:
                    jobs.tick(reader)
                    with jobs._connection(write=True) as connection:
                        connection.execute("UPDATE job_worker SET heartbeat_ms=? WHERE singleton=1", (jobs._now(),))
                except (OSError, sqlite3.Error) as error:
                    if once:
                        raise
                    print("investigation job tick failed (" + type(error).__name__ + "); retrying after the polling interval", file=sys.stderr)
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
            with jobs._connection(write=True) as connection:
                connection.execute("UPDATE job_worker SET stopped_ms=? WHERE singleton=1", (jobs._now(),))
    return jobs.status()
