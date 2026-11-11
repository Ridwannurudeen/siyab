"""Confirmed Telegram destinations and durable, bounded alert delivery."""

import hashlib
import http.client
import json
import os
import re
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from gate import rpc
from intel.workspace import WorkspaceError


MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_SECONDS = 15
LINK_MS = 15 * 60 * 1000
LEASE_MS = 45000
MAX_ATTEMPTS = 5
MAX_QUEUE_ROWS = 10000
MAX_ATTEMPT_ROWS = 50000
RETENTION_MS = 30 * 86400 * 1000
_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")
_BOT_TOKEN = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,128}")
_START = re.compile(r"/start(?:@[A-Za-z0-9_]{5,32})? ([a-f0-9]{64})")
_METHODS = frozenset({"getMe", "getWebhookInfo", "getUpdates", "sendMessage"})


class TelegramError(Exception):
    def __init__(self, code, *, retryable=False, retry_after=None, uncertain=False):
        super().__init__(code)
        self.code, self.retryable, self.uncertain = code, retryable, uncertain
        self.retry_after = min(3600, max(1, retry_after)) if type(retry_after) is int else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TelegramError("redirect_refused")


def _reject_constant(value):
    raise ValueError("nonstandard JSON constant")


class TelegramClient:
    def __init__(self, token_loader=None, *, opener=None):
        self.token_loader = token_loader
        self.opener = opener if opener is not None else build_opener(
            ProxyHandler({}), _NoRedirect(), rpc._DeadlineHTTPSHandler())
        self._token_value = None

    def _token(self):
        if self._token_value is not None:
            return self._token_value
        if self.token_loader is None:
            raise TelegramError("credentials_missing")
        try:
            value = self.token_loader()
        except (OSError, UnicodeError):
            raise TelegramError("credentials_unavailable") from None
        if not isinstance(value, str) or len(value) > 512 or _BOT_TOKEN.fullmatch(value.strip()) is None:
            raise TelegramError("credentials_invalid")
        self._token_value = value.strip()
        return self._token_value

    def configuration(self):
        try:
            self._token()
        except TelegramError as error:
            return {"configured": False, "reason": error.code}
        return {"configured": True, "reason": None}

    @staticmethod
    def _read(reply):
        chunks, size = [], 0
        while True:
            chunk = reply.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise TelegramError("response_too_large", retryable=True, uncertain=True)
        return json.loads(b"".join(chunks), parse_constant=_reject_constant)

    def call(self, method, payload):
        if method not in _METHODS:
            raise ValueError("Telegram method is outside the delivery contract")
        token = self._token()
        request = Request("https://api.telegram.org/bot" + token + "/" + method,
                          data=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8"),
                          method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
        status = None
        try:
            with rpc.request_deadline(REQUEST_SECONDS):
                try:
                    with self.opener.open(request, timeout=REQUEST_SECONDS) as reply:
                        body = self._read(reply)
                except HTTPError as error:
                    status = error.code
                    if 300 <= status < 400:
                        error.close()
                        raise TelegramError("redirect_refused") from None
                    try:
                        body = self._read(error)
                    except (ValueError, UnicodeError, AttributeError):
                        body = {}
                    finally:
                        error.close()
        except TelegramError:
            raise
        except (TimeoutError, URLError, OSError, http.client.HTTPException):
            raise TelegramError("transport_unavailable", retryable=True, uncertain=method == "sendMessage") from None
        except (ValueError, UnicodeError):
            raise TelegramError("response_unreadable", retryable=True, uncertain=method == "sendMessage") from None
        if not isinstance(body, dict) or type(body.get("ok")) is not bool:
            raise TelegramError("response_unreadable", retryable=True, uncertain=method == "sendMessage")
        if status is not None or not body["ok"]:
            code = body.get("error_code", status)
            if type(code) is not int or not 400 <= code <= 599:
                code = status if type(status) is int and 400 <= status <= 599 else 503
            parameters = body.get("parameters")
            retry = parameters.get("retry_after") if isinstance(parameters, dict) else None
            raise TelegramError("provider_" + str(code), retryable=code == 429 or code >= 500,
                                retry_after=retry, uncertain=method == "sendMessage" and code >= 500)
        if "result" not in body:
            raise TelegramError("response_unreadable", retryable=True, uncertain=method == "sendMessage")
        return body["result"]


class TelegramDelivery:
    def __init__(self, store, client=None, *, bot_username="siyabonnearbot", clock=time.time):
        if not isinstance(bot_username, str) or _USERNAME.fullmatch(bot_username) is None:
            raise ValueError("Telegram bot username is invalid")
        self.store, self.client, self.bot_username, self.clock = store, client or TelegramClient(), bot_username, clock
        with store._connection(write=True) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS delivery_state ("
                               "singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL,"
                               "bot_id INTEGER,update_offset INTEGER NOT NULL DEFAULT 0,last_update_ms INTEGER,"
                               "last_poll_ms INTEGER,last_send_ms INTEGER,last_error TEXT,"
                               "next_poll_ms INTEGER NOT NULL DEFAULT 0,next_send_ms INTEGER NOT NULL DEFAULT 0,"
                               "lease_token TEXT,lease_until_ms INTEGER NOT NULL DEFAULT 0,last_binding INTEGER NOT NULL DEFAULT 0)")
            connection.execute("INSERT OR IGNORE INTO delivery_state(singleton,version) VALUES(1,1)")
            if connection.execute("SELECT version FROM delivery_state WHERE singleton=1").fetchone()[0] != 1:
                raise ValueError("Telegram delivery database version is unsupported")
            connection.execute("CREATE TABLE IF NOT EXISTS delivery_bindings ("
                               "id INTEGER PRIMARY KEY AUTOINCREMENT,team_id TEXT NOT NULL UNIQUE REFERENCES teams(id) ON DELETE CASCADE,"
                               "generation INTEGER NOT NULL,nonce_hash TEXT UNIQUE,nonce_expires_ms INTEGER,"
                               "observed_chat_id INTEGER,observed_label TEXT,observed_ms INTEGER,"
                               "active INTEGER NOT NULL DEFAULT 0,active_ms INTEGER,baseline_id INTEGER NOT NULL DEFAULT 0,"
                               "cursor_id INTEGER NOT NULL DEFAULT 0,send_after_ms INTEGER NOT NULL DEFAULT 0,"
                               "created_ms INTEGER NOT NULL,updated_ms INTEGER NOT NULL,user_id TEXT REFERENCES users(id))")
            if "user_id" not in {row["name"] for row in connection.execute("PRAGMA table_info(delivery_bindings)")}:
                connection.execute("ALTER TABLE delivery_bindings ADD COLUMN user_id TEXT REFERENCES users(id)")
            connection.execute("CREATE TABLE IF NOT EXISTS delivery_queue ("
                               "id INTEGER PRIMARY KEY AUTOINCREMENT,binding_id INTEGER NOT NULL REFERENCES delivery_bindings(id) ON DELETE CASCADE,"
                               "team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,generation INTEGER NOT NULL,"
                               "inbox_id INTEGER NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,event_id TEXT NOT NULL,message TEXT NOT NULL,"
                               "state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','dead','cancelled')),"
                               "attempts INTEGER NOT NULL DEFAULT 0,next_attempt_ms INTEGER NOT NULL,lease_token TEXT,"
                               "lease_until_ms INTEGER NOT NULL DEFAULT 0,last_error TEXT,message_id INTEGER,"
                               "uncertain_ack INTEGER NOT NULL DEFAULT 0,created_ms INTEGER NOT NULL,updated_ms INTEGER NOT NULL,"
                               "UNIQUE(binding_id,generation,inbox_id))")
            connection.execute("CREATE INDEX IF NOT EXISTS delivery_pending ON delivery_queue(state,next_attempt_ms,lease_until_ms)")
            connection.execute("CREATE TABLE IF NOT EXISTS delivery_attempts ("
                               "id INTEGER PRIMARY KEY AUTOINCREMENT,queue_id INTEGER NOT NULL REFERENCES delivery_queue(id) ON DELETE CASCADE,"
                               "attempt INTEGER NOT NULL,started_ms INTEGER NOT NULL,finished_ms INTEGER,error TEXT)")
            connection.execute("CREATE INDEX IF NOT EXISTS delivery_team ON delivery_queue(team_id,state)")
            connection.execute("CREATE INDEX IF NOT EXISTS delivery_attempt_queue ON delivery_attempts(queue_id)")

    def _now(self):
        return int(self.clock() * 1000)

    @staticmethod
    def _room(connection, team_id, attempts, *, prune, need=1, keep=None):
        limit = (MAX_ATTEMPT_ROWS if attempts else MAX_QUEUE_ROWS) - need + 1
        count = ("SELECT COUNT(*) FROM delivery_attempts a JOIN delivery_queue q ON q.id=a.queue_id WHERE q.team_id=?" if attempts
                 else "SELECT COUNT(*) FROM delivery_queue WHERE team_id=?")
        while connection.execute(count, (team_id,)).fetchone()[0] >= limit:
            oldest = connection.execute("SELECT id FROM delivery_queue q WHERE team_id=? AND id IS NOT ? AND state IN ('sent','cancelled','dead')"
                                        + (" AND EXISTS(SELECT 1 FROM delivery_attempts a WHERE a.queue_id=q.id)" if attempts else "")
                                        + " ORDER BY state='dead',id LIMIT 1", (team_id, keep)).fetchone()
            if oldest is None or not prune:
                return oldest is not None
            connection.execute("DELETE FROM delivery_queue WHERE id=?", (oldest["id"],))
        return True

    @classmethod
    def _capacity(cls, connection, team_id=None):
        teams = [team_id] if team_id is not None else [row["team_id"] for row in connection.execute("SELECT team_id FROM delivery_bindings")]
        queue = any(not cls._room(connection, team, False, prune=False) for team in teams)
        attempts = any(not cls._room(connection, team, True, prune=False) for team in teams)
        return {"paused": queue or attempts, "queue_paused": queue, "attempts_paused": attempts,
                "queue_rows_limit": MAX_QUEUE_ROWS, "attempt_rows_limit": MAX_ATTEMPT_ROWS}

    def status(self, user_id, team_id):
        configuration = self.client.configuration()
        with self.store._connection() as connection:
            role = self.store._member(connection, user_id, team_id)
            binding = connection.execute("SELECT * FROM delivery_bindings WHERE team_id=?", (team_id,)).fetchone()
            meta = connection.execute("SELECT * FROM delivery_state WHERE singleton=1").fetchone()
            capacity = self._capacity(connection, team_id)
            counts = {row["state"]: row["n"] for row in connection.execute(
                "SELECT state,COUNT(*) AS n FROM delivery_queue WHERE team_id=? GROUP BY state", (team_id,))}
            failure = connection.execute("SELECT last_error FROM delivery_queue WHERE team_id=? AND state IN ('pending','dead') "
                                         "AND last_error IS NOT NULL ORDER BY updated_ms DESC,id DESC LIMIT 1", (team_id,)).fetchone()
        state = "disconnected"
        if binding:
            if binding["active"]:
                state = "active"
            elif binding["nonce_expires_ms"] is not None and binding["nonce_expires_ms"] > self._now():
                state = "observed" if binding["observed_chat_id"] is not None else "pending"
            elif binding["nonce_expires_ms"] is not None:
                state = "expired"
        result = {**configuration, "bot_username": self.bot_username, "state": state,
                  "counts": counts, "capacity": capacity, "last_poll_ms": meta["last_poll_ms"], "last_send_ms": meta["last_send_ms"],
                  "last_error": meta["last_error"], "delivery_semantics": "at_least_once",
                  "last_delivery_error": failure["last_error"] if failure else None,
                  "scope": "New eligible team alerts after confirmation. In-flight sends may complete after disconnect; uncertain acknowledgements can cause duplicates."}
        if role == "owner" and binding:
            result.update({"generation": binding["generation"], "expires_at": binding["nonce_expires_ms"],
                           "observed_chat_label": binding["observed_label"], "active_ms": binding["active_ms"]})
        return result

    def begin_link(self, user_id, team_id):
        nonce, now = secrets.token_hex(32), self._now()
        with self.store._connection(write=True) as connection:
            self.store._member(connection, user_id, team_id, owner=True)
            if not self.client.configuration()["configured"]:
                raise WorkspaceError(503, "delivery_unconfigured", "Telegram delivery is not configured by the operator.")
            previous = connection.execute("SELECT generation FROM delivery_bindings WHERE team_id=?", (team_id,)).fetchone()
            generation = previous["generation"] + 1 if previous else 1
            connection.execute("UPDATE delivery_queue SET state='cancelled',lease_token=NULL,lease_until_ms=0,updated_ms=? "
                               "WHERE team_id=? AND state IN ('pending','sending')", (now, team_id))
            connection.execute("INSERT INTO delivery_bindings(team_id,generation,nonce_hash,nonce_expires_ms,created_ms,updated_ms,user_id) "
                               "VALUES(?,?,?,?,?,?,?) ON CONFLICT(team_id) DO UPDATE SET generation=excluded.generation,"
                               "nonce_hash=excluded.nonce_hash,nonce_expires_ms=excluded.nonce_expires_ms,observed_chat_id=NULL,"
                               "observed_label=NULL,observed_ms=NULL,active=0,active_ms=NULL,updated_ms=excluded.updated_ms,user_id=excluded.user_id",
                               (team_id, generation, hashlib.sha256(nonce.encode("ascii")).hexdigest(), now + LINK_MS, now, now, user_id))
            self.store._audit(connection, team_id, user_id, "telegram.link_started", str(generation))
        return {"link": "https://t.me/" + self.bot_username + "?start=" + nonce,
                "expires_at": now + LINK_MS, "generation": generation}

    def confirm_link(self, user_id, team_id, generation):
        if type(generation) is not int or generation < 1:
            raise WorkspaceError(400, "invalid_confirmation", "Confirm the currently observed Telegram account.")
        now = self._now()
        with self.store._connection(write=True) as connection:
            self.store._member(connection, user_id, team_id, owner=True)
            binding = connection.execute("SELECT * FROM delivery_bindings WHERE team_id=?", (team_id,)).fetchone()
            meta = connection.execute("SELECT bot_id FROM delivery_state WHERE singleton=1").fetchone()
            if (binding is None or binding["generation"] != generation or binding["observed_chat_id"] is None
                    or binding["nonce_expires_ms"] is None or binding["nonce_expires_ms"] <= now or meta["bot_id"] is None):
                raise WorkspaceError(409, "confirmation_unavailable", "Observe the current link in Telegram before confirming it.")
            if binding["active"]:
                return self.status(user_id, team_id)
            baseline = connection.execute("SELECT COALESCE(MAX(id),0) FROM inbox WHERE team_id=?", (team_id,)).fetchone()[0]
            connection.execute("UPDATE delivery_bindings SET active=1,active_ms=?,baseline_id=?,cursor_id=?,nonce_hash=NULL,updated_ms=? WHERE team_id=?",
                               (now, baseline, baseline, now, team_id))
            self.store._audit(connection, team_id, user_id, "telegram.connected", str(generation))
        return self.status(user_id, team_id)

    def revoke(self, user_id, team_id):
        now = self._now()
        with self.store._connection(write=True) as connection:
            self.store._member(connection, user_id, team_id, owner=True)
            connection.execute("UPDATE delivery_bindings SET active=0,nonce_hash=NULL,nonce_expires_ms=NULL,"
                               "observed_chat_id=NULL,observed_label=NULL,observed_ms=NULL,generation=generation+1,updated_ms=? WHERE team_id=?", (now, team_id))
            connection.execute("UPDATE delivery_queue SET state='cancelled',lease_token=NULL,lease_until_ms=0,updated_ms=? "
                               "WHERE team_id=? AND state IN ('pending','sending')", (now, team_id))
            self.store._audit(connection, team_id, user_id, "telegram.disconnected", team_id)
        return self.status(user_id, team_id)

    def list_deliveries(self, user_id, team_id, limit=50, before=None):
        if type(limit) is not int or not 1 <= limit <= 100 or before is not None and (type(before) is not int or before < 1):
            raise WorkspaceError(400, "invalid_page", "Use a valid delivery cursor and a limit from 1 to 100.")
        with self.store._connection() as connection:
            self.store._member(connection, user_id, team_id)
            rows = [dict(row) for row in connection.execute(
                "SELECT id,event_id,state,attempts,next_attempt_ms,last_error,message_id,uncertain_ack,created_ms,updated_ms "
                "FROM delivery_queue WHERE team_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                (team_id, before, before, limit))]
        return {"items": rows, "next_before": rows[-1]["id"] if rows else before}

    def retry_dead(self, user_id, team_id, delivery_id):
        if type(delivery_id) is not int or delivery_id < 1:
            raise WorkspaceError(400, "invalid_delivery", "Choose a valid dead-letter delivery.")
        with self.store._connection(write=True) as connection:
            self.store._member(connection, user_id, team_id, owner=True)
            row = connection.execute("SELECT q.id FROM delivery_queue q JOIN delivery_bindings b ON b.id=q.binding_id "
                                     "WHERE q.id=? AND q.team_id=? AND q.state='dead' AND b.active=1 AND b.generation=q.generation",
                                     (delivery_id, team_id)).fetchone()
            if row is None:
                raise WorkspaceError(409, "delivery_not_retryable", "Only a current connected destination's dead letters can be retried.")
            dropped = connection.execute("DELETE FROM delivery_attempts WHERE queue_id=?", (delivery_id,)).rowcount
            retained = connection.execute("SELECT COUNT(*) FROM delivery_queue WHERE team_id=?", (team_id,)).fetchone()[0]
            if not self._room(connection, team_id, True, prune=True, need=MAX_ATTEMPTS, keep=delivery_id):
                raise WorkspaceError(503, "delivery_capacity", "This team's undelivered alerts hold its delivery attempt records, so this retry has no room until they finish.")
            pruned = retained - connection.execute("SELECT COUNT(*) FROM delivery_queue WHERE team_id=?", (team_id,)).fetchone()[0]
            connection.execute("UPDATE delivery_queue SET state='pending',attempts=0,next_attempt_ms=?,last_error=NULL,updated_ms=? WHERE id=?",
                               (self._now(), self._now(), delivery_id))
            self.store._audit(connection, team_id, user_id, "telegram.delivery_retried", str(delivery_id), {"pruned": pruned, "dropped_attempts": dropped})
        return {"retried": True}

    def _lease(self):
        token, now = secrets.token_hex(16), self._now()
        with self.store._connection(write=True) as connection:
            updated = connection.execute("UPDATE delivery_state SET lease_token=?,lease_until_ms=? WHERE singleton=1 AND lease_until_ms<=?",
                                         (token, now + LEASE_MS, now)).rowcount
        return token if updated else None

    def _renew(self, token):
        with self.store._connection(write=True) as connection:
            changed = connection.execute("UPDATE delivery_state SET lease_until_ms=? WHERE singleton=1 AND lease_token=?",
                                         (self._now() + LEASE_MS, token)).rowcount
        if not changed:
            raise TelegramError("worker_lease_lost", retryable=True)

    def _identity(self):
        identity = self.client.call("getMe", {})
        if (not isinstance(identity, dict) or identity.get("is_bot") is not True
                or type(identity.get("id")) is not int or not 1 <= identity["id"] < 2 ** 52
                or not isinstance(identity.get("username"), str)
                or identity["username"].lower() != self.bot_username.lower()):
            raise TelegramError("bot_identity_mismatch")
        hook = self.client.call("getWebhookInfo", {})
        if not isinstance(hook, dict) or not isinstance(hook.get("url"), str):
            raise TelegramError("response_unreadable", retryable=True)
        if hook["url"]:
            raise TelegramError("bot_webhook_configured")
        with self.store._connection(write=True) as connection:
            previous = connection.execute("SELECT bot_id FROM delivery_state WHERE singleton=1").fetchone()[0]
            if previous is not None and previous != identity["id"]:
                raise TelegramError("bot_identity_mismatch")
            connection.execute("UPDATE delivery_state SET bot_id=? WHERE singleton=1", (identity["id"],))

    def _poll(self):
        now = self._now()
        with self.store._connection() as connection:
            meta = connection.execute("SELECT update_offset,last_update_ms FROM delivery_state WHERE singleton=1").fetchone()
        payload = {"offset": meta["update_offset"], "limit": 100, "timeout": 10, "allowed_updates": ["message"]}
        if meta["last_update_ms"] is not None and now - meta["last_update_ms"] > 7 * 86400 * 1000:
            payload.pop("offset")
        updates = self.client.call("getUpdates", payload)
        if (not isinstance(updates, list) or len(updates) > 100
                or not all(isinstance(item, dict) and type(item.get("update_id")) is int and 0 <= item["update_id"] < 2 ** 63 for item in updates)):
            raise TelegramError("response_unreadable", retryable=True)
        with self.store._connection(write=True) as connection:
            current = connection.execute("SELECT update_offset,last_update_ms FROM delivery_state WHERE singleton=1").fetchone()
            reset = "offset" not in payload and current["last_update_ms"] == meta["last_update_ms"]
            updates = [item for item in updates if reset or item["update_id"] >= current["update_offset"]]
            for update in sorted(updates, key=lambda item: item["update_id"]):
                message = update.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("text"), str):
                    continue
                match = _START.fullmatch(message["text"])
                chat, sender = message.get("chat"), message.get("from")
                if (match is None or not isinstance(chat, dict) or chat.get("type") != "private"
                        or type(chat.get("id")) is not int or not 1 <= chat["id"] < 2 ** 52
                        or not isinstance(sender, dict) or sender.get("is_bot") is not False
                        or type(sender.get("id")) is not int or sender["id"] != chat["id"]):
                    continue
                command = message["text"].split(" ", 1)[0]
                if "@" in command and command.partition("@")[2].lower() != self.bot_username.lower():
                    continue
                username = sender.get("username")
                name = chat.get("first_name")
                label = "@" + username if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,32}", username) else (
                    name[:64] if isinstance(name, str) else "Private Telegram account")
                label += " (ID ending " + str(chat["id"])[-4:] + ")"
                label = label.encode("utf-8", errors="replace").decode("utf-8")
                digest = hashlib.sha256(match[1].encode("ascii")).hexdigest()
                connection.execute("UPDATE delivery_bindings SET nonce_hash=NULL,observed_chat_id=?,observed_label=?,observed_ms=?,updated_ms=? "
                                   "WHERE nonce_hash=? AND nonce_expires_ms>? AND active=0 AND observed_chat_id IS NULL",
                                   (chat["id"], label, now, now, digest, now))
            if updates:
                connection.execute("UPDATE delivery_state SET update_offset=?,last_update_ms=? WHERE singleton=1",
                                   (max(item["update_id"] for item in updates) + 1, now))
            connection.execute("UPDATE delivery_state SET last_poll_ms=?,last_error=NULL WHERE singleton=1", (now,))

    @staticmethod
    def _message(row):
        event = json.loads(row["body_json"])
        signal = " ".join(str(event.get("signal", "Observed activity")).split())[:200]
        observed = row["observed_ms"]
        when = (datetime.fromtimestamp(observed / 1000, UTC).isoformat()
                if type(observed) is int and 0 <= observed <= 253402300799999 else "Unavailable")
        return ("Siyab alert\nAccount: " + row["account"] + "\nSignal: " + signal + "\nObserved UTC: " + when
                + "\nEvidence: " + row["event_id"] + "\nView: https://siyab.xyz/feed.html")

    def _enqueue(self):
        count, now = 0, self._now()
        with self.store._connection(write=True) as connection:
            connection.execute("DELETE FROM delivery_queue WHERE state IN ('sent','cancelled','dead') AND updated_ms<=?", (now - RETENTION_MS,))
            last = connection.execute("SELECT last_binding FROM delivery_state WHERE singleton=1").fetchone()[0]
            bindings = connection.execute("SELECT * FROM delivery_bindings WHERE active=1 AND id>? ORDER BY id LIMIT 20", (last,)).fetchall()
            if not bindings:
                bindings = connection.execute("SELECT * FROM delivery_bindings WHERE active=1 ORDER BY id LIMIT 20").fetchall()
            for binding in bindings:
                cursor = binding["cursor_id"]
                retained = connection.execute("SELECT COUNT(*) FROM delivery_queue WHERE team_id=?", (binding["team_id"],)).fetchone()[0]
                rows = connection.execute("SELECT inbox.id,inbox.account,inbox.candidate,events.event_id,events.body_json,events.observed_ms "
                                          "FROM inbox JOIN events ON events.seq=inbox.event_seq WHERE inbox.team_id=? AND inbox.id>? ORDER BY inbox.id LIMIT 200",
                                          (binding["team_id"], binding["cursor_id"])).fetchall()
                for row in rows:
                    if not row["candidate"] and row["observed_ms"] is not None and row["observed_ms"] >= binding["active_ms"]:
                        exists = connection.execute("SELECT 1 FROM delivery_queue WHERE binding_id=? AND generation=? AND inbox_id=?",
                                                    (binding["id"], binding["generation"], row["id"])).fetchone()
                        if exists is None:
                            if retained >= MAX_QUEUE_ROWS:
                                if not self._room(connection, binding["team_id"], False, prune=True):
                                    break
                                retained = MAX_QUEUE_ROWS - 1
                            connection.execute("INSERT INTO delivery_queue(binding_id,team_id,generation,inbox_id,event_id,message,state,next_attempt_ms,created_ms,updated_ms) "
                                               "VALUES(?,?,?,?,?,?,'pending',?,?,?)",
                                               (binding["id"], binding["team_id"], binding["generation"], row["id"], row["event_id"], self._message(row), now, now, now))
                            count += 1
                            retained += 1
                    cursor = row["id"]
                if cursor != binding["cursor_id"]:
                    connection.execute("UPDATE delivery_bindings SET cursor_id=? WHERE id=?", (cursor, binding["id"]))
            if bindings:
                connection.execute("UPDATE delivery_state SET last_binding=? WHERE singleton=1", (bindings[-1]["id"],))
        return count

    def _claim(self):
        now, token = self._now(), secrets.token_hex(16)
        with self.store._connection(write=True) as connection:
            connection.execute("UPDATE delivery_queue SET state=CASE WHEN attempts>=? THEN 'dead' ELSE 'pending' END,"
                               "lease_token=NULL,last_error='delivery_ack_unknown',uncertain_ack=1,updated_ms=? "
                               "WHERE state='sending' AND lease_until_ms<=?", (MAX_ATTEMPTS, now, now))
            if connection.execute("SELECT next_send_ms FROM delivery_state WHERE singleton=1").fetchone()[0] > now:
                return None
            full = []
            while True:
                row = connection.execute("SELECT q.*,b.observed_chat_id FROM delivery_queue q JOIN delivery_bindings b ON b.id=q.binding_id "
                                         "WHERE q.state='pending' AND q.next_attempt_ms<=? AND b.active=1 AND b.generation=q.generation "
                                         "AND b.send_after_ms<=? AND q.team_id NOT IN (" + ",".join("?" * len(full)) + ") ORDER BY q.id LIMIT 1",
                                         (now, now, *full)).fetchone()
                if row is None:
                    return None
                if self._room(connection, row["team_id"], True, prune=True):
                    break
                full.append(row["team_id"])
            connection.execute("UPDATE delivery_queue SET state='sending',attempts=attempts+1,lease_token=?,lease_until_ms=?,updated_ms=? WHERE id=?",
                               (token, now + LEASE_MS, now, row["id"]))
            connection.execute("UPDATE delivery_bindings SET send_after_ms=? WHERE id=?", (now + 1000, row["binding_id"]))
            attempt = connection.execute("INSERT INTO delivery_attempts(queue_id,attempt,started_ms) VALUES(?,?,?)",
                                         (row["id"], row["attempts"] + 1, now)).lastrowid
            return {**dict(row), "attempts": row["attempts"] + 1, "lease_token": token, "attempt_id": attempt}

    def _send(self, row):
        with self.store._connection() as connection:
            valid = connection.execute("SELECT 1 FROM delivery_queue q JOIN delivery_bindings b ON b.id=q.binding_id "
                                       "WHERE q.id=? AND q.state='sending' AND q.lease_token=? AND b.active=1 AND b.generation=q.generation",
                                       (row["id"], row["lease_token"])).fetchone()
        if valid is None:
            return False
        failure, message_id = None, None
        try:
            result = self.client.call("sendMessage", {"chat_id": row["observed_chat_id"], "text": row["message"]})
            if (not isinstance(result, dict) or type(result.get("message_id")) is not int or result["message_id"] < 1
                    or not isinstance(result.get("chat"), dict) or result["chat"].get("id") != row["observed_chat_id"]):
                raise TelegramError("response_unreadable", retryable=True, uncertain=True)
            message_id = result["message_id"]
        except TelegramError as error:
            failure = error
        now = self._now()
        with self.store._connection(write=True) as connection:
            connection.execute("UPDATE delivery_attempts SET finished_ms=?,error=? WHERE id=?", (now, failure.code if failure else None, row["attempt_id"]))
            if failure is None:
                connection.execute("UPDATE delivery_queue SET state='sent',message_id=?,last_error=NULL,lease_token=NULL,lease_until_ms=0,updated_ms=? "
                                   "WHERE id=? AND state='sending' AND lease_token=?", (message_id, now, row["id"], row["lease_token"]))
                connection.execute("UPDATE delivery_state SET last_send_ms=? WHERE singleton=1", (now,))
            else:
                retry = failure.retryable and row["attempts"] < MAX_ATTEMPTS
                delay = failure.retry_after or min(300, 2 ** row["attempts"])
                connection.execute("UPDATE delivery_queue SET state=?,next_attempt_ms=?,last_error=?,lease_token=NULL,lease_until_ms=0,"
                                   "uncertain_ack=MAX(uncertain_ack,?),updated_ms=? WHERE id=? AND state='sending' AND lease_token=?",
                                   ("pending" if retry else "dead", now + delay * 1000, failure.code, int(failure.uncertain), now, row["id"], row["lease_token"]))
                if failure.code == "provider_429":
                    connection.execute("UPDATE delivery_state SET next_send_ms=MAX(next_send_ms,?),next_poll_ms=MAX(next_poll_ms,?) WHERE singleton=1",
                                       (now + delay * 1000, now + delay * 1000))
        return failure is None

    def run_once(self, batch_size=10):
        if type(batch_size) is not int or not 1 <= batch_size <= 20:
            raise ValueError("delivery batch size must be between 1 and 20")
        configuration = self.client.configuration()
        with self.store._connection() as connection:
            capacity = self._capacity(connection)
        result = {**configuration, "capacity": capacity, "queued": 0, "sent": 0, "failed": 0, "last_error": configuration["reason"]}
        if not configuration["configured"]:
            return result
        token = self._lease()
        if token is None:
            return {**result, "last_error": "worker_busy"}
        try:
            with self.store._connection() as connection:
                meta = connection.execute("SELECT next_poll_ms FROM delivery_state WHERE singleton=1").fetchone()
            if meta["next_poll_ms"] > self._now():
                return {**result, "last_error": "provider_cooldown"}
            self._renew(token)
            self._identity()
            self._renew(token)
            self._poll()
            result["queued"] = self._enqueue()
            for _ in range(batch_size):
                self._renew(token)
                row = self._claim()
                if row is None:
                    break
                sent = self._send(row)
                result["sent"] += sent
                result["failed"] += not sent
            result["last_error"] = "delivery_failed" if result["failed"] else None
        except TelegramError as error:
            result["last_error"] = error.code
            with self.store._connection(write=True) as connection:
                delay = error.retry_after or 5
                connection.execute("UPDATE delivery_state SET last_error=?,next_poll_ms=? WHERE singleton=1",
                                   (error.code, self._now() + delay * 1000))
        finally:
            with self.store._connection(write=True) as connection:
                connection.execute("UPDATE delivery_state SET lease_token=NULL,lease_until_ms=0 WHERE singleton=1 AND lease_token=?", (token,))
                result["capacity"] = self._capacity(connection)
        return result


def configured_client():
    filename = os.environ.get("SIYAB_TELEGRAM_TOKEN_FILE")
    if not filename:
        return TelegramClient()
    path = Path(filename).expanduser()
    def load():
        with path.open("r", encoding="utf-8") as source:
            return source.read(513)
    return TelegramClient(load)


def run(store, client=None, *, once=False, interval=5, bot_username="siyabonnearbot", stop=None):
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= interval <= 60:
        raise ValueError("delivery interval must be between 1 and 60 seconds")
    worker = TelegramDelivery(store, client if client is not None else configured_client(), bot_username=bot_username)
    while True:
        result = worker.run_once()
        if once:
            return result
        if stop is not None:
            if stop.wait(interval):
                return result
        else:
            time.sleep(interval)
