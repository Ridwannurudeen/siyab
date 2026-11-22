"""Single use NEP-413 sign in with a current mainnet FullAccess key."""

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from urllib.parse import urlsplit

from gate import base58, canon, ed25519, rpc
from intel.workspace import WorkspaceError, _random_token


CHALLENGE_MS = 5 * 60 * 1000
MAX_CHALLENGES = 4096
MAX_CHALLENGES_PER_BUCKET = 8
MAX_VERIFY_RPC_PER_BUCKET_HOUR = 8
MAX_VERIFY_RPC_PER_BUCKET_DAY = 20
MAX_VERIFY_RPC_PER_BUCKET_MINUTE = 3
MAX_VERIFY_BUCKETS = 4096
NEGATIVE_KEY_MS = 60 * 1000
MAX_NEGATIVE_KEYS = 4096
NEP413_TAG = 2**31 + 413
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def _borsh_string(value):
    if not isinstance(value, str):
        raise ValueError("a NEP-413 string must be text")
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(4, "little") + encoded


def serialize_payload(message, nonce, recipient, callback_url=None):
    """The prefixed Borsh bytes from NEP-413, before SHA-256."""
    if not isinstance(nonce, bytes) or len(nonce) != 32:
        raise ValueError("a NEP-413 nonce must be 32 bytes")
    callback = b"\0" if callback_url is None else b"\1" + _borsh_string(callback_url)
    return (NEP413_TAG.to_bytes(4, "little") + _borsh_string(message) + nonce
            + _borsh_string(recipient) + callback)


def _origin(value):
    if not isinstance(value, str) or not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("the NEAR sign in origin must be an absolute origin")
    try:
        parsed = urlsplit(value)
        valid = (parsed.hostname is not None and parsed.username is None and parsed.password is None
                 and parsed.port != 0 and not parsed.path and not parsed.query and not parsed.fragment
                 and value == parsed.scheme + "://" + parsed.netloc and "\\" not in value and "%" not in value
                 and (parsed.scheme == "https" or (parsed.scheme == "http"
                      and parsed.hostname in ("localhost", "127.0.0.1", "::1"))))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("the NEAR sign in origin must use HTTPS or loopback HTTP without a path")
    return value


def _account(value):
    try:
        account = canon.account_id(value, "account")
    except canon.CanonError:
        raise WorkspaceError(400, "invalid_near_account", "Enter a valid NEAR mainnet account.") from None
    if account == "testnet" or account.endswith(".testnet"):
        raise WorkspaceError(400, "invalid_near_account", "NEAR sign in accepts mainnet accounts only.")
    if re.fullmatch(r"0x[0-9a-f]{40}", account):
        raise WorkspaceError(400, "invalid_near_account", "ETH-implicit (0x) accounts cannot sign in with NEP-413.")
    return account


def _binding(value):
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise WorkspaceError(401, "near_challenge_invalid", "Start a new NEAR sign in from this browser.")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _client_bucket(value):
    try:
        if not isinstance(value, str) or "%" in value:
            raise ValueError("invalid client address")
        address = ipaddress.ip_address(value)
    except ValueError:
        raise WorkspaceError(400, "invalid_near_client", "NEAR sign in requires a valid client address.") from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return str(ipaddress.ip_network((address, 32 if address.version == 4 else 48), strict=False))


def _prime_order(encoded):
    try:
        point = ed25519.decodepoint(encoded)
    except ValueError:
        return False
    return point != [0, 1] and ed25519.scalarmult(point, ed25519.l) == [0, 1]


class NearAuth:
    def __init__(self, store, origin, *, client=None, clock=time.time):
        self.store, self.origin, self.clock = store, _origin(origin), clock
        self.callback_url = self.origin + "/workspace.html"
        self.client = client if client is not None else rpc.Client(rpc.MAINNET_RPC_URL, mode="off")
        if self.client.mode != "off" or self.client.kind != "rpc":
            raise ValueError("NEAR sign in requires live RPC with fixture mode off")
        with self.store._connection(write=True) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS near_auth_challenges ("
                               "id_hash TEXT PRIMARY KEY, binding_hash TEXT NOT NULL, account_id TEXT NOT NULL, "
                               "nonce BLOB NOT NULL, message TEXT NOT NULL, recipient TEXT NOT NULL, "
                               "callback_url TEXT NOT NULL, created_ms INTEGER NOT NULL, expires_at INTEGER NOT NULL)")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(near_auth_challenges)")}
            if "client_bucket" not in columns:
                connection.execute("ALTER TABLE near_auth_challenges ADD COLUMN client_bucket TEXT NOT NULL DEFAULT ''")
                connection.execute("DELETE FROM near_auth_challenges")
            connection.execute("CREATE INDEX IF NOT EXISTS near_auth_expiry ON near_auth_challenges(expires_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS near_auth_bucket ON near_auth_challenges(client_bucket)")
            connection.execute("CREATE TABLE IF NOT EXISTS near_auth_rpc_buckets ("
                               "client_bucket TEXT NOT NULL, hour INTEGER NOT NULL, count INTEGER NOT NULL, "
                               "PRIMARY KEY(client_bucket,hour))")
            connection.execute("CREATE INDEX IF NOT EXISTS near_auth_rpc_hour ON near_auth_rpc_buckets(hour)")
            connection.execute("CREATE TABLE IF NOT EXISTS near_auth_rpc_days ("
                               "client_bucket TEXT NOT NULL, day INTEGER NOT NULL, count INTEGER NOT NULL, "
                               "minute INTEGER NOT NULL, minute_count INTEGER NOT NULL, PRIMARY KEY(client_bucket,day))")
            connection.execute("CREATE INDEX IF NOT EXISTS near_auth_rpc_day ON near_auth_rpc_days(day)")
            now = int(self.clock())
            connection.execute("INSERT OR IGNORE INTO near_auth_rpc_days "
                               "SELECT client_bucket,hour/24,SUM(count),?,SUM(CASE WHEN hour=? THEN count ELSE 0 END) "
                               "FROM near_auth_rpc_buckets WHERE hour>=? GROUP BY client_bucket,hour/24",
                               (now // 60, now // 3600, now // 86400 * 24))
            connection.execute("CREATE TABLE IF NOT EXISTS near_auth_negative_keys ("
                               "account_id TEXT NOT NULL, public_key TEXT NOT NULL, checked_ms INTEGER NOT NULL, "
                               "expires_at INTEGER NOT NULL, PRIMARY KEY(account_id,public_key))")
            connection.execute("CREATE INDEX IF NOT EXISTS near_auth_negative_expiry ON near_auth_negative_keys(expires_at)")

    def _reserve_lookup(self, row, public_key):
        now = int(self.clock() * 1000)
        hour = now // 3600000
        day, minute = now // 86400000, now // 60000
        with self.store._connection(write=True) as connection:
            connection.execute("DELETE FROM near_auth_negative_keys WHERE expires_at<=? OR checked_ms>?", (now, now))
            cached = connection.execute("SELECT 1 FROM near_auth_negative_keys WHERE account_id=? AND public_key=?",
                                        (row["account_id"], public_key)).fetchone()
            if cached is not None:
                raise WorkspaceError(401, "near_key_invalid", "This signing key is not a current FullAccess key on the claimed NEAR account.")
            connection.execute("DELETE FROM near_auth_rpc_buckets WHERE hour<?", (hour,))
            connection.execute("DELETE FROM near_auth_rpc_days WHERE day<?", (day,))
            counter = connection.execute("SELECT count FROM near_auth_rpc_buckets WHERE client_bucket=? AND hour=?",
                                         (row["client_bucket"], hour)).fetchone()
            daily = connection.execute("SELECT count,minute,minute_count FROM near_auth_rpc_days WHERE client_bucket=? AND day=?",
                                       (row["client_bucket"], day)).fetchone()
            retry_after = 0
            if counter is not None and counter["count"] >= MAX_VERIFY_RPC_PER_BUCKET_HOUR:
                retry_after = (hour + 1) * 3600 - now // 1000
            if daily is not None and daily["count"] >= MAX_VERIFY_RPC_PER_BUCKET_DAY:
                retry_after = max(retry_after, (day + 1) * 86400 - now // 1000)
            if daily is not None and daily["minute"] == minute and daily["minute_count"] >= MAX_VERIFY_RPC_PER_BUCKET_MINUTE:
                retry_after = max(retry_after, (minute + 1) * 60 - now // 1000)
            if retry_after:
                raise WorkspaceError(429, "near_verify_limit", "Too many NEAR key checks from this network. Try again later.",
                                     retry_after)
            if daily is None:
                count = connection.execute("SELECT COUNT(*) FROM near_auth_rpc_days").fetchone()[0]
                if count >= MAX_VERIFY_BUCKETS:
                    raise WorkspaceError(503, "near_verify_capacity", "NEAR sign in is busy. Try again shortly.", 60)
            if counter is None:
                count = connection.execute("SELECT COUNT(*) FROM near_auth_rpc_buckets").fetchone()[0]
                if count >= MAX_VERIFY_BUCKETS:
                    raise WorkspaceError(503, "near_verify_capacity", "NEAR sign in is busy. Try again shortly.", 60)
                connection.execute("INSERT INTO near_auth_rpc_buckets VALUES (?,?,1)", (row["client_bucket"], hour))
            else:
                connection.execute("UPDATE near_auth_rpc_buckets SET count=count+1 WHERE client_bucket=? AND hour=?",
                                   (row["client_bucket"], hour))
            if daily is None:
                connection.execute("INSERT INTO near_auth_rpc_days VALUES (?,?,1,?,1)", (row["client_bucket"], day, minute))
            else:
                minute_count = daily["minute_count"] + 1 if daily["minute"] == minute else 1
                connection.execute("UPDATE near_auth_rpc_days SET count=count+1,minute=?,minute_count=? WHERE client_bucket=? AND day=?",
                                   (minute, minute_count, row["client_bucket"], day))

    def _cache_invalid_key(self, account, public_key):
        now = int(self.clock() * 1000)
        with self.store._connection(write=True) as connection:
            connection.execute("DELETE FROM near_auth_negative_keys WHERE expires_at<=? OR checked_ms>?", (now, now))
            connection.execute("INSERT OR REPLACE INTO near_auth_negative_keys VALUES (?,?,?,?)",
                               (account, public_key, now, now + NEGATIVE_KEY_MS))
            connection.execute("DELETE FROM near_auth_negative_keys WHERE rowid IN ("
                               "SELECT rowid FROM near_auth_negative_keys ORDER BY expires_at DESC,rowid DESC LIMIT -1 OFFSET ?)",
                               (MAX_NEGATIVE_KEYS,))

    def issue(self, account, binding, *, client_address):
        account, binding_hash = _account(account), _binding(binding)
        client_bucket = _client_bucket(client_address)
        now = int(self.clock() * 1000)
        expires_at = now + CHALLENGE_MS
        identifier = _random_token()
        nonce = secrets.token_bytes(32)
        message = ("Sign in to Siyab. This request does not authorize a transaction or payment.\n"
                   f"Account: {account}\nOrigin: {self.origin}\nChallenge: {identifier}\n"
                   f"Issued at: {now}\nExpires at: {expires_at}")
        with self.store._connection(write=True) as connection:
            connection.execute("DELETE FROM near_auth_challenges WHERE expires_at<=? OR created_ms>?", (now, now))
            bucket_count = connection.execute("SELECT COUNT(*) FROM near_auth_challenges WHERE client_bucket=?",
                                              (client_bucket,)).fetchone()[0]
            if bucket_count >= MAX_CHALLENGES_PER_BUCKET:
                raise WorkspaceError(429, "near_challenge_limit", "Too many open NEAR sign in requests. Try again shortly.", 60)
            count = connection.execute("SELECT COUNT(*) FROM near_auth_challenges").fetchone()[0]
            if count >= MAX_CHALLENGES:
                raise WorkspaceError(503, "near_challenge_capacity", "NEAR sign in is busy. Try again shortly.", 60)
            connection.execute("INSERT INTO near_auth_challenges VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (hashlib.sha256(identifier.encode("ascii")).hexdigest(), binding_hash, account,
                                nonce, message, self.origin, self.callback_url, now, expires_at, client_bucket))
        return {"challenge_id": identifier, "account_id": account, "message": message, "recipient": self.origin,
                "nonce": base64.b64encode(nonce).decode("ascii"), "callbackUrl": self.callback_url,
                "state": identifier, "expires_at": expires_at}

    def verify(self, response, binding):
        if (not isinstance(response, dict) or not isinstance(response.get("challenge_id"), str)
                or _TOKEN.fullmatch(response["challenge_id"]) is None):
            raise WorkspaceError(400, "invalid_near_response", "Send the NEAR signed message and its challenge.")
        identifier = response["challenge_id"]
        identity = hashlib.sha256(identifier.encode("ascii")).hexdigest()
        with self.store._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM near_auth_challenges WHERE id_hash=?", (identity,)).fetchone()
            connection.execute("DELETE FROM near_auth_challenges WHERE id_hash=?", (identity,))
        if row is None:
            raise WorkspaceError(401, "near_challenge_invalid", "This NEAR sign in challenge is missing or already used.")
        now = int(self.clock() * 1000)
        if (not hmac.compare_digest(row["binding_hash"], _binding(binding))
                or not row["created_ms"] <= now < row["expires_at"]
                or row["expires_at"] - row["created_ms"] != CHALLENGE_MS
                or row["recipient"] != self.origin or row["callback_url"] != self.callback_url):
            raise WorkspaceError(401, "near_challenge_invalid", "This NEAR sign in challenge is expired or belongs to another browser.")
        required = {"challenge_id", "accountId", "publicKey", "signature"}
        if set(response) not in (required, required | {"state"}):
            raise WorkspaceError(400, "invalid_near_response", "Send only the NEAR signed message and its challenge.")
        if "state" in response and (not isinstance(response["state"], str)
                                    or _TOKEN.fullmatch(response["state"]) is None
                                    or not hmac.compare_digest(response["state"], identifier)):
            raise WorkspaceError(401, "near_challenge_invalid", "The wallet returned a different sign in state.")
        if _account(response["accountId"]) != row["account_id"]:
            raise WorkspaceError(401, "near_account_mismatch", "The wallet signed for a different NEAR account.")
        public_key, signature = response["publicKey"], response["signature"]
        try:
            if not isinstance(public_key, str) or not 40 <= len(public_key) <= 52:
                raise ValueError("invalid public key")
            public = base58.decode_key(public_key)
            if len(public) != 32 or base58.encode_key(public) != public_key:
                raise ValueError("invalid public key")
            if not isinstance(signature, str) or len(signature) != 88:
                raise ValueError("invalid signature")
            signed = base64.b64decode(signature, validate=True)
            if len(signed) != 64 or base64.b64encode(signed).decode("ascii") != signature:
                raise ValueError("invalid signature")
        except ValueError:
            raise WorkspaceError(400, "invalid_near_signature", "Use an Ed25519 public key and a base64 NEP-413 signature.") from None
        digest = hashlib.sha256(serialize_payload(row["message"], row["nonce"], row["recipient"], row["callback_url"])).digest()
        if (not _prime_order(public) or not _prime_order(signed[:32])
                or not ed25519.verify(public, digest, signed)):
            raise WorkspaceError(401, "near_signature_invalid", "The NEAR signature does not match this sign in challenge.")
        if self.client.mode != "off" or self.client.kind != "rpc":
            raise WorkspaceError(503, "near_key_unavailable", "A current NEAR access key could not be checked.")
        params = {"request_type": "view_access_key", "finality": "final",
                  "account_id": row["account_id"], "public_key": public_key}
        self._reserve_lookup(row, public_key)
        try:
            with rpc.request_deadline(10), rpc.caller_class("near_auth"):
                key = self.client.call("query", params)
                rpc.response_gate({"result": key}, "query", params)
        except rpc.RpcError as error:
            missing_key = (error.cause == "UNKNOWN_ACCESS_KEY"
                           or error.error == f"access key {public_key} does not exist while viewing")
            if missing_key:
                self._cache_invalid_key(row["account_id"], public_key)
            if missing_key or error.cause == "UNKNOWN_ACCOUNT":
                raise WorkspaceError(401, "near_key_invalid", "This signing key is no longer on the claimed NEAR account.") from None
            raise WorkspaceError(503, "near_key_unavailable", "A current NEAR access key could not be checked.") from None
        except (rpc.HttpError, rpc.TransportError, rpc.RateLimited, rpc.MalformedResponse, OSError, ValueError):
            raise WorkspaceError(503, "near_key_unavailable", "A current NEAR access key could not be checked.") from None
        if not isinstance(key, dict) or key.get("permission") != "FullAccess":
            self._cache_invalid_key(row["account_id"], public_key)
            raise WorkspaceError(401, "near_key_invalid", "NEAR sign in requires a current FullAccess key.")
        if not row["created_ms"] <= int(self.clock() * 1000) < row["expires_at"]:
            raise WorkspaceError(401, "near_challenge_invalid", "This NEAR sign in challenge expired while the key was checked.")
        return {"account_id": row["account_id"], "public_key": public_key}
