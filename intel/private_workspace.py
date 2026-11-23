"""Wallet sessions and opaque personal workspace snapshots in the external workspace database."""

import base64
import binascii
import hashlib
import hmac
import json
import re
from http.cookies import CookieError, SimpleCookie

from intel.near_auth import CHALLENGE_MS, NearAuth
from intel.workspace import WorkspaceError, _random_token


VAULT_BODY_BYTES = 1048576
SESSION_MS = 3600000
RESET_SESSION_MS = 300000
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
ENVELOPE_FIELDS = {"version", "algorithm", "derivation", "origin", "account_id", "public_key", "vault_id", "iv", "ciphertext"}


class PrivateWorkspace:
    def __init__(self, store, origin, *, secure_cookie=True, auth=None):
        self.store, self.origin, self.secure_cookie = store, origin, secure_cookie
        self.auth = auth if auth is not None else NearAuth(store, origin)
        self.cookie_name = "__Host-siyab-near-session" if secure_cookie else "siyab-local-near-session"
        self.challenge_cookie_name = "__Host-siyab-near-challenge" if secure_cookie else "siyab-local-near-challenge"
        with store._connection(write=True) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS near_sessions ("
                               "token_hash TEXT PRIMARY KEY, origin TEXT NOT NULL, account_id TEXT NOT NULL, public_key TEXT NOT NULL, "
                               "csrf_token TEXT NOT NULL, created_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS near_sessions_account ON near_sessions(account_id)")
            connection.execute("CREATE TABLE IF NOT EXISTS near_vaults ("
                               "origin TEXT NOT NULL, account_id TEXT NOT NULL, public_key TEXT NOT NULL, "
                               "revision INTEGER NOT NULL, envelope_json TEXT NOT NULL, updated_ms INTEGER NOT NULL, "
                               "PRIMARY KEY(origin,account_id))")

    def _token(self, headers, cookie_name=None):
        cookie_name = self.cookie_name if cookie_name is None else cookie_name
        cookies = headers.get_all("Cookie", [])
        if not cookies:
            return None
        if len(cookies) != 1:
            raise WorkspaceError(400, "invalid_cookie", "The wallet session cookie is ambiguous.")
        count = sum(part.split("=", 1)[0].strip() == cookie_name for part in cookies[0].split(";"))
        if not count:
            return None
        if count != 1:
            raise WorkspaceError(400, "invalid_cookie", "The wallet session cookie is ambiguous.")
        parsed = SimpleCookie()
        try:
            parsed.load(cookies[0])
        except CookieError:
            raise WorkspaceError(400, "invalid_cookie", "The wallet session cookie is malformed.") from None
        item = parsed.get(cookie_name)
        return item.value if item is not None and TOKEN.fullmatch(item.value) else None

    def _cookie(self, token, seconds, *, challenge=False):
        cookie_name = self.challenge_cookie_name if challenge else self.cookie_name
        return (f"{cookie_name}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={seconds}"
                + ("; Secure" if self.secure_cookie else ""))

    def _session(self, token):
        if token is not None:
            with self.store._connection() as connection:
                row = connection.execute("SELECT account_id,public_key,csrf_token,created_ms,expires_ms FROM near_sessions "
                                         "WHERE token_hash=? AND origin=? AND expires_ms>?",
                                         (hashlib.sha256(token.encode()).hexdigest(), self.origin, self.store._now())).fetchone()
            if row is not None:
                return {**dict(row), "authenticated": True}
        raise WorkspaceError(401, "authentication_required", "Sign in with your NEAR wallet to open the encrypted workspace.")

    def handle(self, parts, method, values, headers, *, address=None):
        token = self._token(headers)
        if parts == ["challenge"] and method == "POST":
            self._fields(values, {"account_id"})
            binding = self._token(headers, self.challenge_cookie_name) or _random_token()
            challenge = self.auth.issue(values["account_id"], binding, client_address=str(address))
            return 201, challenge, {"Set-Cookie": self._cookie(binding, CHALLENGE_MS // 1000, challenge=True)}
        if parts == ["verify"] and method == "POST":
            binding = self._token(headers, self.challenge_cookie_name)
            if binding is None:
                raise WorkspaceError(401, "authentication_required", "Start a new wallet sign-in from this browser.")
            identity = self.auth.verify(values, binding)
            next_token, csrf, now = _random_token(), _random_token(), self.store._now()
            with self.store._connection(write=True) as connection:
                connection.execute("DELETE FROM near_sessions WHERE expires_ms<=? OR token_hash=?",
                                   (now, hashlib.sha256((token or "").encode()).hexdigest()))
                connection.execute("DELETE FROM near_sessions WHERE origin=? AND account_id=? AND token_hash NOT IN "
                                   "(SELECT token_hash FROM near_sessions WHERE origin=? AND account_id=? ORDER BY created_ms DESC LIMIT 9)",
                                   (self.origin, identity["account_id"], self.origin, identity["account_id"]))
                connection.execute("INSERT INTO near_sessions VALUES (?,?,?,?,?,?,?)",
                                   (hashlib.sha256(next_token.encode()).hexdigest(), self.origin, identity["account_id"],
                                    identity["public_key"], csrf, now, now + SESSION_MS))
            return 200, {**identity, "authenticated": True, "csrf_token": csrf, "expires_ms": now + SESSION_MS}, {
                "Set-Cookie": self._cookie(next_token, SESSION_MS // 1000)}
        if parts == ["session"] and method in ("GET", "HEAD"):
            try:
                return 200, self._session(token), {}
            except WorkspaceError as error:
                if error.status != 401:
                    raise
                return 200, {"authenticated": False}, ({"Set-Cookie": self._cookie("", 0)} if token is not None else {})
        session = self._session(token)
        if method == "POST":
            csrf = headers.get_all("X-CSRF-Token", [])
            if len(csrf) != 1 or not TOKEN.fullmatch(csrf[0]) or not hmac.compare_digest(csrf[0], session["csrf_token"]):
                raise WorkspaceError(403, "csrf_denied", "Refresh the wallet session before saving.")
        if parts == ["logout"] and method == "POST":
            self._fields(values, set())
            with self.store._connection(write=True) as connection:
                connection.execute("DELETE FROM near_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
            return 200, {"signed_out": True}, {"Set-Cookie": self._cookie("", 0)}
        if parts == ["vault", "reset"] and method == "POST":
            self._fields(values, {"confirmation"})
            if values["confirmation"] != "DELETE " + session["account_id"]:
                raise WorkspaceError(400, "confirmation_required", "Type DELETE followed by your NEAR account to permanently delete the encrypted copy.")
            with self.store._connection(write=True) as connection:
                if not 0 <= self.store._now() - session["created_ms"] < RESET_SESSION_MS:
                    raise WorkspaceError(403, "near_reauthentication_required", "Sign out and sign in with NEAR again before resetting. The sign-in must be less than five minutes old.")
                previous = connection.execute("SELECT revision FROM near_vaults WHERE origin=? AND account_id=?",
                                              (self.origin, session["account_id"])).fetchone()
                revision = (previous["revision"] if previous else 0) + 1
                if revision > 2**53 - 1:
                    raise WorkspaceError(409, "invalid_revision", "The encrypted workspace revision limit was reached. No saved work was changed.")
                connection.execute("INSERT INTO near_vaults VALUES (?,?,?,?,?,?) ON CONFLICT(origin,account_id) "
                                   "DO UPDATE SET revision=excluded.revision,envelope_json=excluded.envelope_json,updated_ms=excluded.updated_ms",
                                   (self.origin, session["account_id"], session["public_key"], revision, "null", self.store._now()))
            return 200, {"reset": True, "revision": revision}, {}
        if parts == ["vault"]:
            if method in ("GET", "HEAD"):
                with self.store._connection() as connection:
                    row = connection.execute("SELECT revision,envelope_json,updated_ms FROM near_vaults "
                                             "WHERE origin=? AND account_id=?", (self.origin, session["account_id"])).fetchone()
                return 200, ({"vault": json.loads(row["envelope_json"]), "revision": row["revision"], "updated_ms": row["updated_ms"]}
                             if row else {"vault": None, "revision": 0, "updated_ms": None}), {}
            self._fields(values, {"revision", "vault"})
            revision = values["revision"]
            if type(revision) is not int or not 0 <= revision < 2**53 - 1:
                raise WorkspaceError(400, "invalid_revision", "Send the current encrypted workspace revision.")
            vault = self._envelope(values["vault"], session, revision + 1)
            with self.store._connection(write=True) as connection:
                previous = connection.execute("SELECT revision,public_key,envelope_json FROM near_vaults WHERE origin=? AND account_id=?",
                                              (self.origin, session["account_id"])).fetchone()
                if (previous["revision"] if previous else 0) != revision:
                    raise WorkspaceError(409, "version_conflict", "Another device saved this workspace. Load it before saving again.")
                if previous is not None and previous["envelope_json"] != "null" and previous["public_key"] != session["public_key"]:
                    raise WorkspaceError(409, "vault_key_changed", "This workspace needs its original wallet signing key. Reset encrypted copy can delete it permanently after a fresh sign-in.")
                now = self.store._now()
                connection.execute("INSERT INTO near_vaults VALUES (?,?,?,?,?,?) ON CONFLICT(origin,account_id) "
                                   "DO UPDATE SET public_key=excluded.public_key,revision=excluded.revision,envelope_json=excluded.envelope_json,updated_ms=excluded.updated_ms",
                                   (self.origin, session["account_id"], session["public_key"], revision + 1,
                                    json.dumps(vault, sort_keys=True, separators=(",", ":")), now))
            return 200, {"revision": revision + 1, "updated_ms": now}, {}
        raise WorkspaceError(404, "not_found", "No encrypted workspace route matches this path.")

    def _envelope(self, value, session, revision):
        if (not isinstance(value, dict) or set(value) != ENVELOPE_FIELDS or type(value.get("version")) is not int
                or value["version"] != 1 or value["algorithm"] != "AES-256-GCM"
                or value["derivation"] != "NEP413-HKDF-SHA256-v1" or value["origin"] != self.origin
                or value["account_id"] != session["account_id"] or value["public_key"] != session["public_key"]
                or value["vault_id"] != "workspace:" + str(revision)):
            raise WorkspaceError(400, "invalid_ciphertext", "Send a version 1 encrypted workspace bound to this wallet, origin and revision.")
        for field, minimum, maximum in (("iv", 12, 12), ("ciphertext", 16, 524304)):
            encoded = value[field]
            if not isinstance(encoded, str) or len(encoded) > (maximum + 2) // 3 * 4:
                raise WorkspaceError(400, "invalid_ciphertext", "The encrypted workspace has an invalid size or encoding.")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise WorkspaceError(400, "invalid_ciphertext", "The encrypted workspace has an invalid size or encoding.") from None
            if not minimum <= len(decoded) <= maximum or base64.b64encode(decoded).decode() != encoded:
                raise WorkspaceError(400, "invalid_ciphertext", "The encrypted workspace has an invalid size or encoding.")
        return value

    @staticmethod
    def _fields(values, expected):
        if set(values) != expected:
            raise WorkspaceError(400, "invalid_fields", "Send only the fields required by this encrypted workspace command.")
