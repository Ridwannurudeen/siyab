"""Authenticated team workspaces and the durable feed journal, stored outside releases."""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from gate import canon
from intel import feed


SESSION_MS = 7 * 86400 * 1000
INVITE_MS = 86400 * 1000
MAX_TEAMS = 3
MAX_USER_WATCHES = 5
_WATCH_ALERTED = False
_WATCH_ALERT_LOCK = threading.Lock()
_HASH_SLOTS = threading.BoundedSemaphore(2)
_USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,38}[a-z0-9]")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_HEX = re.compile(r"[a-f0-9]{64}")
_DUMMY_SALT = b"siyab-login-dummy-salt"


class WorkspaceError(ValueError):
    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retry_after = retry_after


def _utf8(value):
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise WorkspaceError(400, "invalid_unicode", "Text must contain valid Unicode characters.") from error


def _password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128:
        raise WorkspaceError(400, "invalid_password", "Use a password of 12 to 128 characters.")
    encoded = _utf8(value)
    if len(encoded) > 512:
        raise WorkspaceError(400, "invalid_password", "Use a password of 12 to 128 characters.")
    return encoded


def _username(value):
    if not isinstance(value, str) or _USERNAME.fullmatch(value) is None:
        raise WorkspaceError(400, "invalid_username", "Use 3 to 40 lowercase letters, digits, dots, underscores or hyphens.")
    return value


def _derive(password, salt):
    if not _HASH_SLOTS.acquire(blocking=False):
        raise WorkspaceError(503, "auth_busy", "Authentication is busy. Try again shortly.")
    try:
        return hashlib.scrypt(password, salt=salt, n=32768, r=8, p=1, maxmem=67108864, dklen=32)
    finally:
        _HASH_SLOTS.release()


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _random_token():
    return base64.b64encode(secrets.token_bytes(32), altchars=b"-_").rstrip(b"=").decode("ascii")


def _text(value, name, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise WorkspaceError(400, "invalid_input", f"{name} must be {'at most' if empty else '1 to'} {maximum} characters.")
    _utf8(value)
    return value if empty else value.strip()


def _page(offset, limit, maximum=100):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
        raise WorkspaceError(400, "invalid_page", f"Use a nonnegative cursor and a limit between 1 and {maximum}.")


def _case_values(title, notes, url):
    title, notes = _text(title, "Title", 120), _text(notes, "Notes", 4000, empty=True)
    if (not isinstance(url, str) or len(url) > 2048 or not url.startswith("/") or url.startswith("//")
            or "\\" in url or any(ord(character) < 32 for character in url)):
        raise WorkspaceError(400, "invalid_url", "Use a local investigation page address.")
    _utf8(url)
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise WorkspaceError(400, "invalid_url", "Use a local investigation page address.") from error
    if (parsed.scheme or parsed.netloc or re.fullmatch(
            r"/|/(?:index|workspace|investigate|feed|verify|issuers|top-receivers)\.html|"
            r"/(?:accounts|entities|flows|clusters|incidents|records)/[A-Za-z0-9._-]+\.html", parsed.path) is None):
        raise WorkspaceError(400, "invalid_url", "Use a local investigation page address.")
    return title, notes, url


def _watch_account(value):
    try:
        account = canon.account_id(value, "account")
    except canon.CanonError as error:
        raise WorkspaceError(400, "invalid_account", "Enter a valid NEAR mainnet account.") from error
    if account == "testnet" or account.endswith(".testnet"):
        raise WorkspaceError(400, "invalid_account", "Watchlists monitor NEAR mainnet accounts.")
    return account


def _transfer_identity(event):
    if event.get("signal") != "account_transfer_observed":
        return None
    stable = {key: value for key, value in event.items() if key not in ("tx_hash", "block_timestamp", "observed")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SQLiteWorkspaceStore:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        for name, attribute, default, minimum, maximum in (
                ("SIYAB_REGISTRATIONS_PER_HOUR", "max_hourly_registrations", 20, 1, 200),
                ("SIYAB_REGISTRATIONS_PER_DAY", "max_daily_registrations", 100, 1, 2000),
                ("SIYAB_WATCH_PUBLIC_ACCOUNTS", "watch_public_accounts", 90, 0, 500),
                ("SIYAB_WATCH_RESERVED_ACCOUNTS", "watch_reserved_accounts", 30, 0, 100)):
            value = os.environ.get(name) or str(default)
            if re.fullmatch(r"[0-9]+", value) is None or not minimum <= int(value) <= maximum:
                raise ValueError(f"{name} must be an integer from {minimum} to {maximum}.")
            setattr(self, attribute, int(value))
        if self.max_daily_registrations < self.max_hourly_registrations:
            raise ValueError("SIYAB_REGISTRATIONS_PER_DAY must not be below SIYAB_REGISTRATIONS_PER_HOUR.")
        self.watch_alert_accounts = self.watch_public_accounts * 4 // 5
        if self.path.is_symlink():
            raise WorkspaceError(503, "invalid_store", "The workspace database cannot be a symbolic link.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for attempt in range(4):
            try:
                self._initialize()
                break
            except WorkspaceError as error:
                if error.code != "workspace_busy":
                    raise
                if attempt == 3:
                    raise WorkspaceError(503, "workspace_busy", "Workspace storage is busy while upgrading stored events") from error
                time.sleep(0.05)
        os.chmod(self.path, 0o600)

    def _initialize(self):
        with self._connection(transaction=False) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise WorkspaceError(503, "unsupported_store", "The workspace database schema is unsupported.")
            if version == 0:
                connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    password_salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                    password_version INTEGER NOT NULL DEFAULT 1, recovery_hash TEXT NOT NULL,
                    created_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL, created_ms INTEGER NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS teams (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS memberships (
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),
                    PRIMARY KEY(team_id,user_id)
                );
                CREATE INDEX IF NOT EXISTS memberships_user ON memberships(user_id);
                CREATE TABLE IF NOT EXISTS invites (
                    token_hash TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    username TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('editor','viewer')),
                    expires_at INTEGER NOT NULL, used_ms INTEGER
                );
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    title TEXT NOT NULL, notes TEXT NOT NULL, url TEXT NOT NULL, version INTEGER NOT NULL,
                    created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), updated_by TEXT NOT NULL REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS cases_team ON cases(team_id,created_ms,id);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    body_json TEXT NOT NULL, observed_ms INTEGER, ingested_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist (
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    account TEXT NOT NULL, created_ms INTEGER NOT NULL, created_seq INTEGER NOT NULL,
                    PRIMARY KEY(team_id,account)
                );
                CREATE INDEX IF NOT EXISTS watch_account ON watchlist(account);
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    event_seq INTEGER NOT NULL REFERENCES events(seq), account TEXT NOT NULL,
                    candidate INTEGER NOT NULL, acknowledged_ms INTEGER, UNIQUE(team_id,event_seq)
                );
                CREATE INDEX IF NOT EXISTS inbox_team ON inbox(team_id,id);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    resource_id TEXT NOT NULL, detail_json TEXT NOT NULL, created_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_team ON audit(team_id,id);
                PRAGMA user_version=1;
                COMMIT;
                """)
        with self._connection(write=True) as connection:
            if "transfer_id" not in {row["name"] for row in connection.execute("PRAGMA table_info(events)")}:
                connection.execute("ALTER TABLE events ADD COLUMN transfer_id TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS events_transfer ON events(transfer_id)")
            connection.execute("CREATE TABLE IF NOT EXISTS watch_creators ("
                               "team_id TEXT NOT NULL, account TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id), "
                               "PRIMARY KEY(team_id,account), FOREIGN KEY(team_id,account) "
                               "REFERENCES watchlist(team_id,account) ON DELETE CASCADE)")
            connection.execute("CREATE INDEX IF NOT EXISTS watch_creators_user ON watch_creators(user_id)")
            connection.execute("CREATE TABLE IF NOT EXISTS access_codes ("
                               "code_hash TEXT PRIMARY KEY, uses_left INTEGER NOT NULL, "
                               "created_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS reserved_users ("
                               "user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, created_ms INTEGER NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO watch_creators SELECT watchlist.team_id,watchlist.account,audit.actor_id "
                               "FROM watchlist JOIN audit ON audit.id=(SELECT MAX(id) FROM audit WHERE "
                               "team_id=watchlist.team_id AND resource_id=watchlist.account AND action='watch.added')")
        while True:
            with self._connection() as connection:
                rows = connection.execute("SELECT seq,body_json FROM events WHERE transfer_id IS NULL ORDER BY seq LIMIT 2000").fetchall()
            if not rows:
                return
            updates = []
            for row in rows:
                try:
                    event = json.loads(row["body_json"])
                except ValueError:
                    event = None
                if not isinstance(event, dict):
                    identity = f"invalid-json:{row['seq']}"
                else:
                    identity = _transfer_identity(event) or f"non-transfer:{row['seq']}"
                updates.append((identity, row["seq"]))
            with self._connection(write=True) as connection:
                connection.executemany("UPDATE events SET transfer_id=? WHERE seq=? AND transfer_id IS NULL", updates)

    @contextmanager
    def _connection(self, *, write=False, transaction=True):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            if transaction:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            if transaction:
                connection.commit()
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                raise WorkspaceError(503, "workspace_busy", "Workspace storage is busy. Try again shortly.") from error
            raise
        finally:
            connection.close()

    def _now(self):
        return int(self.clock() * 1000)

    @staticmethod
    def _user(row):
        return {"id": row["id"], "username": row["username"]}

    def _session(self, connection, token):
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            raise WorkspaceError(401, "unauthenticated", "Sign in to continue.")
        row = connection.execute("SELECT users.id,users.username,sessions.csrf_token,sessions.expires_at "
                                 "FROM sessions JOIN users ON users.id=sessions.user_id "
                                 "WHERE token_hash=? AND expires_at>?", (_digest(token), self._now())).fetchone()
        if row is None:
            raise WorkspaceError(401, "unauthenticated", "Sign in to continue.")
        return {"user": self._user(row), "csrf_token": row["csrf_token"], "expires_at": row["expires_at"]}

    def _new_session(self, connection, user):
        token, csrf = _random_token(), _random_token()
        now = self._now()
        connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        connection.execute("DELETE FROM sessions WHERE user_id=? AND token_hash NOT IN "
                           "(SELECT token_hash FROM sessions WHERE user_id=? ORDER BY created_ms DESC LIMIT 9)",
                           (user["id"], user["id"]))
        connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (_digest(token), user["id"], csrf, now, now + SESSION_MS))
        return {"user": self._user(user), "session_token": token, "csrf_token": csrf, "expires_at": now + SESSION_MS}

    def create_access_code(self, uses, days):
        if type(uses) is not int or not 1 <= uses <= 50 or type(days) is not int or not 1 <= days <= 60:
            raise WorkspaceError(400, "invalid_access_code", "Use 1 to 50 uses and 1 to 60 days.")
        code, now = _random_token(), self._now()
        expires = now + days * 86400000
        with self._connection(write=True) as connection:
            remaining = connection.execute("SELECT COALESCE(SUM(uses_left),0) FROM access_codes WHERE expires_ms>?", (now,)).fetchone()[0]
            if remaining + uses > 100:
                raise WorkspaceError(409, "access_code_limit", "Unexpired access codes can have at most 100 remaining uses.")
            connection.execute("INSERT INTO access_codes VALUES (?,?,?,?)", (_digest(code), uses, now, expires))
        return {"code": code, "uses_left": uses, "created_ms": now, "expires_ms": expires}

    def list_access_codes(self):
        with self._connection() as connection:
            return [dict(row) for row in connection.execute("SELECT created_ms,expires_ms,uses_left FROM access_codes ORDER BY created_ms,code_hash")]

    def revoke_access_codes(self):
        with self._connection(write=True) as connection:
            connection.execute("UPDATE access_codes SET uses_left=0")
        return {"revoked": True}

    def grant_reserved(self, username):
        username = _username(username)
        with self._connection(write=True) as connection:
            user = connection.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if user is None:
                raise WorkspaceError(404, "user_missing", "The user was not found.")
            connection.execute("INSERT OR IGNORE INTO reserved_users VALUES (?,?)", (user["id"], self._now()))
        return {"reserved": True}

    def is_reserved(self, user_id):
        with self._connection() as connection:
            return connection.execute("SELECT 1 FROM reserved_users WHERE user_id=?", (user_id,)).fetchone() is not None

    def register(self, username, password, access_code=None):
        username, password = _username(username), _password(password)
        if access_code is not None and (not isinstance(access_code, str) or _TOKEN.fullmatch(access_code) is None):
            raise WorkspaceError(403, "invalid_access_code", "The access code is not valid or has no uses left.")
        salt, recovery, user_id = secrets.token_bytes(16), _random_token(), secrets.token_hex(16)
        derived = _derive(password, salt)
        with self._connection(write=True) as connection:
            now = self._now()
            if access_code is not None:
                code = connection.execute("SELECT code_hash FROM access_codes WHERE code_hash=? AND uses_left>0 AND expires_ms>?",
                                          (_digest(access_code), now)).fetchone()
                if code is None:
                    raise WorkspaceError(403, "invalid_access_code", "The access code is not valid or has no uses left.")
            if connection.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise WorkspaceError(409, "username_taken", "That username is already registered.")
            if access_code is not None:
                connection.execute("UPDATE access_codes SET uses_left=uses_left-1 WHERE code_hash=?", (code["code_hash"],))
            else:
                waits = []
                for window, limit in ((86400000, self.max_daily_registrations), (3600000, self.max_hourly_registrations)):
                    start = now // window * window
                    count = connection.execute("SELECT COUNT(*) FROM users WHERE created_ms>=? AND created_ms<? "
                                               "AND NOT EXISTS (SELECT 1 FROM reserved_users WHERE user_id=users.id)",
                                               (start, start + window)).fetchone()[0]
                    if count >= limit:
                        waits.append((start + window - now + 999) // 1000)
                if waits:
                    raise WorkspaceError(429, "registration_limit", "Registration capacity is full. Try again after the window turns. "
                                         "If you were given an access code, enter it in the Access code field.", retry_after=max(waits))
            connection.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?)",
                               (user_id, username, salt, derived, 1, _digest(recovery), now))
            if access_code is not None:
                connection.execute("INSERT INTO reserved_users VALUES (?,?)", (user_id, now))
            user = {"id": user_id, "username": username}
            team_id = secrets.token_hex(16)
            connection.execute("INSERT INTO teams VALUES (?,?,?)", (team_id, username + "'s workspace", self._now()))
            connection.execute("INSERT INTO memberships VALUES (?,?,?)", (team_id, user_id, "owner"))
            self._audit(connection, team_id, user_id, "team.created", team_id)
            result = self._new_session(connection, user)
            return {**result, "recovery_code": recovery}

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str):
            raise WorkspaceError(401, "invalid_credentials", "Username or password is incorrect.")
        try:
            encoded = _utf8(password)
        except WorkspaceError as error:
            raise WorkspaceError(401, "invalid_credentials", "Username or password is incorrect.") from error
        if len(encoded) > 512:
            raise WorkspaceError(401, "invalid_credentials", "Username or password is incorrect.")
        with self._connection() as connection:
            user = connection.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        actual = _derive(encoded, user["password_salt"] if user else _DUMMY_SALT)
        if user is None or not hmac.compare_digest(actual, user["password_hash"]):
            raise WorkspaceError(401, "invalid_credentials", "Username or password is incorrect.")
        with self._connection(write=True) as connection:
            current = connection.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if not hmac.compare_digest(current["password_hash"], user["password_hash"]):
                raise WorkspaceError(401, "invalid_credentials", "Username or password is incorrect.")
            return self._new_session(connection, current)

    def authenticate(self, session_token):
        with self._connection() as connection:
            return self._session(connection, session_token)

    def logout(self, session_token):
        if isinstance(session_token, str) and _TOKEN.fullmatch(session_token):
            with self._connection(write=True) as connection:
                connection.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(session_token),))
        return {"signed_out": True}

    def change_password(self, session_token, current_password, new_password):
        authenticated = self.authenticate(session_token)
        with self._connection() as connection:
            user = connection.execute("SELECT * FROM users WHERE id=?", (authenticated["user"]["id"],)).fetchone()
        if not hmac.compare_digest(_derive(_password(current_password), user["password_salt"]), user["password_hash"]):
            raise WorkspaceError(401, "invalid_credentials", "Current password is incorrect.")
        return self._replace_password(user, new_password, session_token=session_token)

    def recover(self, username, recovery_code, new_password):
        if not isinstance(username, str) or not isinstance(recovery_code, str) or _TOKEN.fullmatch(recovery_code) is None:
            raise WorkspaceError(401, "invalid_recovery", "Username or recovery code is incorrect.")
        with self._connection() as connection:
            user = connection.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user is None or not hmac.compare_digest(_digest(recovery_code), user["recovery_hash"]):
            raise WorkspaceError(401, "invalid_recovery", "Username or recovery code is incorrect.")
        return self._replace_password(user, new_password, recovery_hash=user["recovery_hash"])

    def _replace_password(self, user, password, *, session_token=None, recovery_hash=None):
        salt, recovery = secrets.token_bytes(16), _random_token()
        derived = _derive(_password(password), salt)
        with self._connection(write=True) as connection:
            current = connection.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if session_token is not None:
                self._session(connection, session_token)
            if (not hmac.compare_digest(current["password_hash"], user["password_hash"])
                    or recovery_hash is not None and not hmac.compare_digest(current["recovery_hash"], recovery_hash)):
                raise WorkspaceError(401, "invalid_credentials", "Credentials changed. Sign in again.")
            connection.execute("UPDATE users SET password_salt=?,password_hash=?,recovery_hash=? WHERE id=?",
                               (salt, derived, _digest(recovery), user["id"]))
            connection.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            for team in connection.execute("SELECT team_id FROM memberships WHERE user_id=?", (user["id"],)).fetchall():
                self._audit(connection, team["team_id"], user["id"], "account.credentials_changed", user["id"])
            return {**self._new_session(connection, current), "recovery_code": recovery}

    @staticmethod
    def _member(connection, user_id, team_id, *, write=False, owner=False):
        row = connection.execute("SELECT role FROM memberships WHERE team_id=? AND user_id=?", (team_id, user_id)).fetchone()
        if row is None or owner and row["role"] != "owner" or write and row["role"] == "viewer":
            raise WorkspaceError(403, "forbidden", "Your team role does not permit this action.")
        return row["role"]

    def _audit(self, connection, team_id, actor_id, action, resource_id, detail=None):
        connection.execute("INSERT INTO audit(team_id,actor_id,action,resource_id,detail_json,created_ms) VALUES (?,?,?,?,?,?)",
                           (team_id, actor_id, action, resource_id, json.dumps(detail or {}, separators=(",", ":")), self._now()))

    def list_teams(self, user_id):
        with self._connection() as connection:
            return [dict(row) for row in connection.execute("SELECT teams.*,memberships.role FROM teams JOIN memberships "
                    "ON memberships.team_id=teams.id WHERE memberships.user_id=? ORDER BY teams.created_ms,teams.id", (user_id,))]

    def create_team(self, user_id, name):
        name = _text(name, "Team name", 120)
        with self._connection(write=True) as connection:
            if not connection.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise WorkspaceError(401, "unauthenticated", "Sign in to continue.")
            if connection.execute("SELECT COUNT(*) FROM memberships WHERE user_id=?", (user_id,)).fetchone()[0] >= MAX_TEAMS:
                raise WorkspaceError(409, "team_limit", f"An account can belong to at most {MAX_TEAMS} teams.")
            team_id, now = secrets.token_hex(16), self._now()
            connection.execute("INSERT INTO teams VALUES (?,?,?)", (team_id, name, now))
            connection.execute("INSERT INTO memberships VALUES (?,?,?)", (team_id, user_id, "owner"))
            self._audit(connection, team_id, user_id, "team.created", team_id)
            return {"id": team_id, "name": name, "created_ms": now, "role": "owner"}

    def list_members(self, user_id, team_id):
        with self._connection() as connection:
            self._member(connection, user_id, team_id)
            return [dict(row) for row in connection.execute("SELECT users.id,users.username,memberships.role FROM memberships "
                    "JOIN users ON users.id=memberships.user_id WHERE team_id=? ORDER BY users.username", (team_id,))]

    def invite_member(self, user_id, team_id, username, role):
        username = _username(username)
        if role not in ("editor", "viewer"):
            raise WorkspaceError(400, "invalid_role", "Invitations must grant editor or viewer access.")
        token, expires = _random_token(), self._now() + INVITE_MS
        with self._connection(write=True) as connection:
            self._member(connection, user_id, team_id, owner=True)
            connection.execute("DELETE FROM invites WHERE expires_at<=? OR (team_id=? AND username=?)", (self._now(), team_id, username))
            if connection.execute("SELECT COUNT(*) FROM invites WHERE team_id=?", (team_id,)).fetchone()[0] >= 100:
                raise WorkspaceError(409, "invite_limit", "This team has 100 active invitations.")
            connection.execute("INSERT INTO invites VALUES (?,?,?,?,?,NULL)", (_digest(token), team_id, username, role, expires))
            self._audit(connection, team_id, user_id, "member.invited", username, {"role": role})
        return {"token": token, "username": username, "role": role, "expires_at": expires}

    def join_team(self, user_id, invite_token):
        if not isinstance(invite_token, str) or _TOKEN.fullmatch(invite_token) is None:
            raise WorkspaceError(400, "invalid_invite", "The invitation is invalid.")
        with self._connection(write=True) as connection:
            user = connection.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
            invite = connection.execute("SELECT * FROM invites WHERE token_hash=?", (_digest(invite_token),)).fetchone()
            if invite is None:
                raise WorkspaceError(404, "invalid_invite", "The invitation was not found.")
            if user is None or user["username"] != invite["username"]:
                raise WorkspaceError(403, "invite_recipient", "Sign in as the invitation's intended recipient.")
            if invite["used_ms"] is not None:
                raise WorkspaceError(409, "invite_used", "The invitation has already been used.")
            if invite["expires_at"] <= self._now():
                raise WorkspaceError(410, "invite_expired", "The invitation has expired.")
            if connection.execute("SELECT COUNT(*) FROM memberships WHERE user_id=?", (user_id,)).fetchone()[0] >= MAX_TEAMS:
                raise WorkspaceError(409, "team_limit", f"An account can belong to at most {MAX_TEAMS} teams.")
            existing = connection.execute("SELECT role FROM memberships WHERE team_id=? AND user_id=?", (invite["team_id"], user_id)).fetchone()
            if existing is not None:
                raise WorkspaceError(409, "already_member", "This account already belongs to the team.")
            connection.execute("INSERT INTO memberships VALUES (?,?,?)", (invite["team_id"], user_id, invite["role"]))
            connection.execute("UPDATE invites SET used_ms=? WHERE token_hash=?", (self._now(), _digest(invite_token)))
            self._audit(connection, invite["team_id"], user_id, "member.joined", user_id, {"role": invite["role"]})
            team = dict(connection.execute("SELECT * FROM teams WHERE id=?", (invite["team_id"],)).fetchone())
            return {**team, "role": invite["role"]}

    def set_role(self, user_id, team_id, target_user_id, role):
        if role not in ("owner", "editor", "viewer"):
            raise WorkspaceError(400, "invalid_role", "Choose owner, editor or viewer.")
        with self._connection(write=True) as connection:
            self._member(connection, user_id, team_id, owner=True)
            previous = self._member(connection, target_user_id, team_id)
            if previous == "owner" and role != "owner":
                self._other_owner(connection, team_id, target_user_id)
                self._disconnect_delivery(connection, team_id, target_user_id, user_id)
            connection.execute("UPDATE memberships SET role=? WHERE team_id=? AND user_id=?", (role, team_id, target_user_id))
            self._audit(connection, team_id, user_id, "member.role_changed", target_user_id, {"role": role})
            return {"user_id": target_user_id, "role": role}

    @staticmethod
    def _other_owner(connection, team_id, target):
        if connection.execute("SELECT COUNT(*) FROM memberships WHERE team_id=? AND role='owner' AND user_id!=?", (team_id, target)).fetchone()[0] == 0:
            raise WorkspaceError(409, "last_owner", "The team must retain an owner.")

    def _disconnect_delivery(self, connection, team_id, member_id, actor_id):
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(delivery_bindings)")}
        if not columns:
            return
        binding = connection.execute("SELECT * FROM delivery_bindings WHERE team_id=?", (team_id,)).fetchone()
        if (binding is None or "user_id" in columns and binding["user_id"] not in (None, member_id)
                or not binding["active"] and binding["nonce_hash"] is None and binding["observed_chat_id"] is None):
            return
        now = self._now()
        connection.execute("UPDATE delivery_bindings SET active=0,nonce_hash=NULL,nonce_expires_ms=NULL,observed_chat_id=NULL,"
                           "observed_label=NULL,observed_ms=NULL,generation=generation+1,updated_ms=? WHERE id=?", (now, binding["id"]))
        connection.execute("UPDATE delivery_queue SET state='cancelled',lease_token=NULL,lease_until_ms=0,updated_ms=? "
                           "WHERE team_id=? AND state IN ('pending','sending')", (now, team_id))
        self._audit(connection, team_id, actor_id, "telegram.disconnected", team_id, {"member": member_id})

    def remove_member(self, user_id, team_id, target_user_id):
        with self._connection(write=True) as connection:
            self._member(connection, user_id, team_id, owner=True)
            previous = self._member(connection, target_user_id, team_id)
            if previous == "owner":
                self._other_owner(connection, team_id, target_user_id)
            self._disconnect_delivery(connection, team_id, target_user_id, user_id)
            connection.execute("DELETE FROM memberships WHERE team_id=? AND user_id=?", (team_id, target_user_id))
            self._audit(connection, team_id, user_id, "member.removed", target_user_id)
            return {"removed": True}

    def list_cases(self, user_id, team_id, offset=0, limit=50):
        _page(offset, limit)
        with self._connection() as connection:
            self._member(connection, user_id, team_id)
            total = connection.execute("SELECT COUNT(*) FROM cases WHERE team_id=?", (team_id,)).fetchone()[0]
            rows = [dict(row) for row in connection.execute("SELECT * FROM cases WHERE team_id=? ORDER BY created_ms DESC,id LIMIT ? OFFSET ?", (team_id, limit, offset))]
            return {"cases": rows, "total": total, "next_offset": offset + len(rows) if offset + len(rows) < total else None}

    def create_case(self, user_id, team_id, title, notes, url):
        title, notes, url = _case_values(title, notes, url)
        with self._connection(write=True) as connection:
            self._member(connection, user_id, team_id, write=True)
            if connection.execute("SELECT COUNT(*) FROM cases WHERE team_id=?", (team_id,)).fetchone()[0] >= 500:
                raise WorkspaceError(409, "case_limit", "This team has 500 investigations. Export or remove one before adding another.")
            case_id, now = secrets.token_hex(16), self._now()
            connection.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?)", (case_id, team_id, title, notes, url, 1, now, now, user_id, user_id))
            self._audit(connection, team_id, user_id, "case.created", case_id)
            return dict(connection.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())

    def update_case(self, user_id, team_id, case_id, title, notes, url, version):
        title, notes, url = _case_values(title, notes, url)
        if type(version) is not int or version < 1:
            raise WorkspaceError(400, "invalid_version", "Supply the investigation's current version.")
        with self._connection(write=True) as connection:
            self._member(connection, user_id, team_id, write=True)
            self._case_version(connection, team_id, case_id, version)
            connection.execute("UPDATE cases SET title=?,notes=?,url=?,version=version+1,updated_ms=?,updated_by=? WHERE id=? AND team_id=?",
                               (title, notes, url, self._now(), user_id, case_id, team_id))
            self._audit(connection, team_id, user_id, "case.updated", case_id, {"version": version + 1})
            return dict(connection.execute("SELECT * FROM cases WHERE id=? AND team_id=?", (case_id, team_id)).fetchone())

    @staticmethod
    def _case_version(connection, team_id, case_id, version):
        row = connection.execute("SELECT version FROM cases WHERE id=? AND team_id=?", (case_id, team_id)).fetchone()
        if row is None:
            raise WorkspaceError(404, "case_missing", "The investigation was not found.")
        if row["version"] != version:
            raise WorkspaceError(409, "version_conflict", "This investigation changed. Reload before saving your edits.")

    def delete_case(self, user_id, team_id, case_id, version):
        if type(version) is not int or version < 1:
            raise WorkspaceError(400, "invalid_version", "Supply the investigation's current version.")
        with self._connection(write=True) as connection:
            self._member(connection, user_id, team_id, write=True)
            self._case_version(connection, team_id, case_id, version)
            connection.execute("DELETE FROM cases WHERE id=? AND team_id=?", (case_id, team_id))
            self._audit(connection, team_id, user_id, "case.deleted", case_id)
            return {"deleted": True}

    def import_workspace(self, user_id, team_id, cases, watchlist):
        if not isinstance(cases, list) or len(cases) > 50 or not isinstance(watchlist, list) or len(watchlist) > 100:
            raise WorkspaceError(400, "invalid_import", "Import at most 50 investigations and 100 watched accounts per batch.")
        validated_cases = []
        for case in cases:
            if not isinstance(case, dict) or set(case) != {"title", "notes", "url"}:
                raise WorkspaceError(400, "invalid_import", "Each investigation must contain only title, notes and url.")
            validated_cases.append(_case_values(case["title"], case["notes"], case["url"]))
        validated_accounts = list(dict.fromkeys(_watch_account(account) for account in watchlist))
        with self._connection(write=True) as connection:
            self._member(connection, user_id, team_id, write=True)
            current_cases = connection.execute("SELECT title,notes,url FROM cases WHERE team_id=?", (team_id,)).fetchall()
            seen = {tuple(case) for case in current_cases}
            new_cases = []
            for case in validated_cases:
                if case not in seen:
                    seen.add(case)
                    new_cases.append(case)
            existing_accounts = {row["account"] for row in connection.execute("SELECT account FROM watchlist WHERE team_id=?", (team_id,))}
            new_accounts = [account for account in validated_accounts if account not in existing_accounts]
            if len(current_cases) + len(new_cases) > 500:
                raise WorkspaceError(409, "case_limit", "This import exceeds the team's 500-investigation limit.")
            if len(existing_accounts) + len(new_accounts) > 100:
                raise WorkspaceError(409, "watch_limit", "This import exceeds the team's 100-account watchlist limit.")
            watched_count = self._watch_capacity(connection, user_id, new_accounts)
            now = self._now()
            for title, notes, url in new_cases:
                case_id = secrets.token_hex(16)
                connection.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (case_id, team_id, title, notes, url, 1, now, now, user_id, user_id))
                self._audit(connection, team_id, user_id, "case.created", case_id, {"imported": True})
            seq = connection.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            for account in new_accounts:
                connection.execute("INSERT INTO watchlist VALUES (?,?,?,?)", (team_id, account, now, seq))
                connection.execute("INSERT INTO watch_creators VALUES (?,?,?)", (team_id, account, user_id))
                self._audit(connection, team_id, user_id, "watch.added", account, {"imported": True})
            result = {"created_cases": len(new_cases), "added_watches": len(new_accounts),
                      "skipped_cases": len(validated_cases) - len(new_cases)}
            if new_cases or new_accounts:
                self._audit(connection, team_id, user_id, "workspace.imported", team_id, result)
        if new_accounts:
            self._watch_alert(watched_count)
        return result

    def list_watchlist(self, user_id, team_id):
        with self._connection() as connection:
            self._member(connection, user_id, team_id)
            return [dict(row) for row in connection.execute("SELECT * FROM watchlist WHERE team_id=? ORDER BY created_ms,account", (team_id,))]

    def watched_accounts(self):
        with self._connection() as connection:
            return [row["account"] for row in connection.execute("SELECT DISTINCT account FROM watchlist ORDER BY account")]

    def watch_schedule(self):
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT watchlist.account,MIN(watchlist.created_ms) AS created_ms,"
                "MAX(CASE WHEN reserved_users.user_id IS NULL THEN 0 ELSE 1 END) AS reserved "
                "FROM watchlist LEFT JOIN watch_creators ON watch_creators.team_id=watchlist.team_id "
                "AND watch_creators.account=watchlist.account "
                "LEFT JOIN reserved_users ON reserved_users.user_id=watch_creators.user_id "
                "GROUP BY watchlist.account ORDER BY watchlist.account")]

    def _watch_capacity(self, connection, user_id, accounts):
        added = set(accounts)
        owned = {row["account"] for row in connection.execute("SELECT DISTINCT account FROM watch_creators WHERE user_id=?", (user_id,))}
        if added - owned and len(owned | added) > MAX_USER_WATCHES:
            raise WorkspaceError(409, "watch_limit", f"An account can add at most {MAX_USER_WATCHES} distinct watched accounts across teams.")
        watched = {row["account"] for row in connection.execute("SELECT DISTINCT account FROM watchlist")}
        reserved = {row["account"] for row in connection.execute("SELECT DISTINCT account FROM watch_creators JOIN reserved_users ON reserved_users.user_id=watch_creators.user_id")}
        public = watched - reserved
        if connection.execute("SELECT 1 FROM reserved_users WHERE user_id=?", (user_id,)).fetchone():
            if added and len(reserved | added) > self.watch_reserved_accounts:
                raise WorkspaceError(409, "watch_limit", "The reserved watch pool is full.")
            return len(public - added)
        new = added - watched
        if new and len(public | new) > self.watch_public_accounts:
            raise WorkspaceError(409, "watch_limit", "The public watch pool is full. Accounts created with an access code use a reserved pool.")
        return len(public | new)

    def _watch_alert(self, count):
        global _WATCH_ALERTED
        with _WATCH_ALERT_LOCK:
            if self.watch_public_accounts and count >= self.watch_alert_accounts and not _WATCH_ALERTED:
                _WATCH_ALERTED = True
                print(f"workspace public watch capacity: {count}/{self.watch_public_accounts} distinct accounts (alert at {self.watch_alert_accounts}).", file=sys.stderr)

    def add_watch(self, user_id, team_id, account):
        account = _watch_account(account)
        with self._connection(write=True) as connection:
            self._member(connection, user_id, team_id, write=True)
            existing = connection.execute("SELECT * FROM watchlist WHERE team_id=? AND account=?", (team_id, account)).fetchone()
            if existing:
                return dict(existing)
            if connection.execute("SELECT COUNT(*) FROM watchlist WHERE team_id=?", (team_id,)).fetchone()[0] >= 100:
                raise WorkspaceError(409, "watch_limit", "This team has 100 watched accounts.")
            watched_count = self._watch_capacity(connection, user_id, [account])
            seq = connection.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            connection.execute("INSERT INTO watchlist VALUES (?,?,?,?)", (team_id, account, self._now(), seq))
            connection.execute("INSERT INTO watch_creators VALUES (?,?,?)", (team_id, account, user_id))
            self._audit(connection, team_id, user_id, "watch.added", account)
            result = dict(connection.execute("SELECT * FROM watchlist WHERE team_id=? AND account=?", (team_id, account)).fetchone())
        self._watch_alert(watched_count)
        return result

    def remove_watch(self, user_id, team_id, account):
        account = _watch_account(account)
        with self._connection(write=True) as connection:
            self._member(connection, user_id, team_id, write=True)
            connection.execute("DELETE FROM watchlist WHERE team_id=? AND account=?", (team_id, account))
            self._audit(connection, team_id, user_id, "watch.removed", account)
            return {"removed": True}

    def ingest_event(self, event_id, event, notify=False):
        if (not isinstance(event_id, str) or _HEX.fullmatch(event_id) is None or not isinstance(event, dict)
                or not isinstance(event.get("signal"), str) or not event["signal"].strip()
                or not isinstance(event.get("detail"), str) or not isinstance(event.get("observed"), dict)):
            raise WorkspaceError(400, "invalid_event", "The event identifier or document is invalid.")
        try:
            encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise WorkspaceError(400, "invalid_event", "The event must contain valid JSON values.") from error
        if len(_utf8(encoded)) > 131072:
            raise WorkspaceError(400, "invalid_event", "The event exceeds the journal size limit.")
        observed = event["observed"].get("timestamp_ms")
        if type(observed) is not int or not 0 <= observed <= 8640000000000000:
            observed = None
        accounts = {value for value in (event.get("subject"), event.get("account_id"), feed.event_counterparty(event)) if isinstance(value, str)}
        if isinstance(event.get("accounts"), list):
            accounts.update(value for value in event["accounts"] if isinstance(value, str))
        transfer = _transfer_identity(event)
        with self._connection(write=True) as connection:
            previous = connection.execute("SELECT seq FROM events WHERE event_id=?", (event_id,)).fetchone()
            if previous is None and transfer is not None:
                previous = connection.execute("SELECT seq FROM events WHERE transfer_id=? ORDER BY seq LIMIT 1", (transfer,)).fetchone()
            if previous:
                return previous["seq"]
            cursor = connection.execute("INSERT INTO events(event_id,body_json,observed_ms,ingested_ms,transfer_id) VALUES (?,?,?,?,?)", (event_id, encoded, observed, self._now(), transfer))
            seq = cursor.lastrowid
            if transfer is None:
                connection.execute("UPDATE events SET transfer_id=? WHERE seq=?", (f"non-transfer:{seq}", seq))
            if notify and observed is not None:
                for account in sorted(accounts):
                    for watch in connection.execute("SELECT * FROM watchlist WHERE account=? AND created_ms<=? AND created_seq<?", (account, observed, seq)).fetchall():
                        connection.execute("INSERT OR IGNORE INTO inbox(team_id,event_seq,account,candidate) VALUES (?,?,?,?)",
                                           (watch["team_id"], seq, account, int(event["signal"] in feed.UNMEASURED_SIGNALS)))
            return seq

    @staticmethod
    def _event_row(row):
        result = dict(row)
        result["event"] = json.loads(result.pop("body_json"))
        return result

    def events_after(self, after=0, limit=100):
        _page(after, limit)
        with self._connection() as connection:
            rows = [self._event_row(row) for row in connection.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))]
            return {"items": rows, "next_after": rows[-1]["seq"] if rows else after}

    def list_inbox(self, user_id, team_id, after=0, limit=50, before=None):
        _page(after, limit)
        if before is not None and (type(before) is not int or before < 1 or after):
            raise WorkspaceError(400, "invalid_page", "Use either a newer or an older inbox cursor.")
        with self._connection() as connection:
            self._member(connection, user_id, team_id)
            if before is not None:
                query = "SELECT * FROM (SELECT inbox.*,events.event_id,events.body_json,events.observed_ms,events.ingested_ms FROM inbox JOIN events ON events.seq=inbox.event_seq WHERE inbox.team_id=? AND inbox.id<? ORDER BY inbox.id DESC LIMIT ?) ORDER BY id"
                result = connection.execute(query, (team_id, before, limit))
            elif after == 0:
                query = "SELECT * FROM (SELECT inbox.*,events.event_id,events.body_json,events.observed_ms,events.ingested_ms FROM inbox JOIN events ON events.seq=inbox.event_seq WHERE inbox.team_id=? ORDER BY inbox.id DESC LIMIT ?) ORDER BY id"
                result = connection.execute(query, (team_id, limit))
            else:
                result = connection.execute("SELECT inbox.*,events.event_id,events.body_json,events.observed_ms,events.ingested_ms FROM inbox JOIN events ON events.seq=inbox.event_seq WHERE inbox.team_id=? AND inbox.id>? ORDER BY inbox.id LIMIT ?", (team_id, after, limit))
            rows = []
            for row in result:
                item = self._event_row(row)
                item["candidate"] = bool(item["candidate"])
                item["external_delivery_eligible"] = not item["candidate"]
                rows.append(item)
            first = rows[0]["id"] if rows else before
            older = first is not None and connection.execute("SELECT 1 FROM inbox WHERE team_id=? AND id<? LIMIT 1", (team_id, first)).fetchone() is not None
            return {"items": rows, "next_after": rows[-1]["id"] if rows else after,
                    "next_before": first, "has_older": bool(older)}

    def team_event(self, user_id, team_id, event_id):
        with self._connection() as connection:
            self._member(connection, user_id, team_id)
            row = connection.execute("SELECT events.body_json FROM inbox JOIN events ON events.seq=inbox.event_seq WHERE inbox.team_id=? AND events.event_id=?",
                                     (team_id, event_id)).fetchone()
            return None if row is None else row["body_json"]

    def ack_inbox(self, user_id, team_id, inbox_id):
        if type(inbox_id) is not int or inbox_id < 1:
            raise WorkspaceError(400, "invalid_alert", "Use a valid alert identifier.")
        with self._connection(write=True) as connection:
            self._member(connection, user_id, team_id, write=True)
            row = connection.execute("SELECT acknowledged_ms FROM inbox WHERE id=? AND team_id=?", (inbox_id, team_id)).fetchone()
            if row is None:
                raise WorkspaceError(404, "alert_missing", "The alert was not found.")
            if row["acknowledged_ms"] is None:
                connection.execute("UPDATE inbox SET acknowledged_ms=? WHERE id=? AND team_id=?", (self._now(), inbox_id, team_id))
                self._audit(connection, team_id, user_id, "alert.acknowledged", str(inbox_id))
            return {"acknowledged": True}

    def list_audit(self, user_id, team_id, after=0, limit=50):
        _page(after, limit)
        with self._connection() as connection:
            self._member(connection, user_id, team_id)
            rows = []
            for row in connection.execute("SELECT audit.*,users.username AS actor FROM audit JOIN users ON users.id=audit.actor_id WHERE team_id=? AND audit.id>? ORDER BY audit.id LIMIT ?", (team_id, after, limit)):
                item = dict(row)
                item["detail"] = json.loads(item.pop("detail_json"))
                rows.append(item)
            return {"items": rows, "next_after": rows[-1]["id"] if rows else after}

    def backup(self, destination):
        destination = Path(destination)
        if destination.resolve() == self.path.resolve() or destination.is_symlink():
            raise WorkspaceError(400, "invalid_backup", "Choose a separate regular database backup path.")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        os.chmod(destination, 0o600)
        return {"backed_up": True}
