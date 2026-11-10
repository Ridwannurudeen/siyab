"""Private workspace commands, separate from public intelligence caches."""

import hashlib
import hmac
import ipaddress
import json
import re
import sqlite3
import threading
import time
import urllib.parse
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import format_datetime
from http.cookies import CookieError, SimpleCookie

from intel import investigation_jobs
from intel.workspace import WorkspaceError


PREFIX = "/workspace-api"
MAX_BODY_BYTES = 32768
TOKEN = re.compile(r"[A-Za-z0-9_-]{43,128}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")


class WorkspaceAPI:
    def __init__(self, store, *, origin="https://siyab.xyz", secure_cookie=True,
                 clock=time.monotonic, monitor_status=None, investigation_jobs=None, delivery=None):
        parsed = urllib.parse.urlsplit(origin)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path
                or parsed.query or parsed.fragment or parsed.username is not None
                or parsed.password is not None):
            raise ValueError("workspace origin must be one HTTP origin")
        if parsed.scheme != "https" and (secure_cookie or parsed.hostname not in ("127.0.0.1", "localhost", "::1")):
            raise ValueError("HTTP workspace origins are restricted to explicit local development")
        self.store = store
        self.origin = origin
        self.secure_cookie = secure_cookie
        self.cookie_name = "__Host-siyab-session" if secure_cookie else "siyab-local-session"
        self.clock = clock
        self.monitor_status = monitor_status
        self.investigation_jobs = investigation_jobs
        self.delivery = delivery
        self._rates = OrderedDict()
        self._lock = threading.Lock()

    def handle(self, method, target, peer, headers, body=b""):
        try:
            parsed = urllib.parse.urlsplit(target)
            if parsed.path != PREFIX and not parsed.path.startswith(PREFIX + "/"):
                return None
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise WorkspaceError(400, "invalid_request", "Use a local workspace request path.")
            if method not in ("GET", "HEAD", "POST"):
                return 405, {"error": "method_not_allowed", "message": "Use GET or POST."}, {"Allow": "GET, HEAD, POST"}
            parts = parsed.path[len(PREFIX):].strip("/").split("/")
            if any(not IDENTIFIER.fullmatch(part) for part in parts):
                raise WorkspaceError(404, "not_found", "No workspace route matches this path.")
            client = peer
            if peer in ("127.0.0.1", "::1") and headers.get("X-Forwarded-For"):
                try:
                    client = str(ipaddress.ip_address(headers.get("X-Forwarded-For")))
                except ValueError:
                    raise WorkspaceError(400, "invalid_request", "Client address is malformed.") from None
            if not self._allowed(("requests", client), 120, 60):
                return self._limited(60)
            if method == "POST":
                origins = headers.get_all("Origin", [])
                if origins != [self.origin]:
                    raise WorkspaceError(403, "origin_denied", "The request must originate from this Siyab site.")
                if parsed.query:
                    raise WorkspaceError(400, "invalid_request", "Workspace commands do not take query fields.")
                content_types = headers.get_all("Content-Type", [])
                if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
                    raise WorkspaceError(415, "json_required", "Send an application/json request.")
                if len(body) > MAX_BODY_BYTES:
                    raise WorkspaceError(413, "body_too_large", "Workspace request exceeds 32 KiB.")
                try:
                    values = json.loads(body.decode("utf-8"), object_pairs_hook=self._unique_object)
                except (UnicodeError, ValueError, RecursionError):
                    raise WorkspaceError(400, "invalid_json", "Send one valid JSON object with unique fields.") from None
                if not isinstance(values, dict):
                    raise WorkspaceError(400, "invalid_json", "Send a JSON object.")
            else:
                values = None
            if parts[:1] == ["auth"] and method == "POST":
                return self._auth(parts, values, client, headers)
            token = self._token(headers)
            if parts == ["session"] and method in ("GET", "HEAD"):
                self._query(parsed.query, set())
                if token is None:
                    return 200, {"authenticated": False, "user": None, "teams": []}, {}
                try:
                    session = self.store.authenticate(token)
                except WorkspaceError as error:
                    if error.status != 401:
                        raise
                    return 200, {"authenticated": False, "user": None, "teams": []}, {"Set-Cookie": self._clear_cookie()}
                return 200, {**session, "authenticated": True, "teams": self.store.list_teams(session["user"]["id"])}, {}
            if token is None:
                raise WorkspaceError(401, "authentication_required", "Sign in to use a shared workspace.")
            session = self.store.authenticate(token)
            if method == "POST":
                supplied = headers.get_all("X-CSRF-Token", [])
                if (len(supplied) != 1 or not TOKEN.fullmatch(supplied[0])
                        or not hmac.compare_digest(supplied[0], session["csrf_token"])):
                    raise WorkspaceError(403, "csrf_denied", "Refresh the session and try this command again.")
            actor = session["user"]["id"]
            if parts == ["teams"]:
                if method in ("GET", "HEAD"):
                    self._query(parsed.query, set())
                    return 200, {"teams": self.store.list_teams(actor)}, {}
                self._fields(values, {"name"})
                return 201, self.store.create_team(actor, values["name"]), {}
            if parts == ["teams", "join"] and method == "POST":
                self._fields(values, {"invite_token"})
                return 200, self.store.join_team(actor, values["invite_token"]), {}
            if parts == ["monitor"] and method in ("GET", "HEAD"):
                self._query(parsed.query, set())
                return 200, (self.monitor_status() if self.monitor_status else {"configured": False}), {}
            if len(parts) >= 3 and parts[0] == "teams":
                return self._team(actor, parts[1], parts[2:], method, parsed.query, values)
            raise WorkspaceError(404, "not_found", "No workspace route matches this path.")
        except WorkspaceError as error:
            return error.status, {"error": error.code, "message": error.message}, {}
        except investigation_jobs.JobNotFound as error:
            return 404, {"error": "not_found", "message": str(error)}, {}
        except investigation_jobs.JobInputError as error:
            return 400, {"error": "invalid_job", "message": str(error)}, {}
        except investigation_jobs.JobCapacityError as error:
            return 503, {"error": "job_capacity", "message": str(error)}, {"Retry-After": "60"}
        except (sqlite3.Error, OSError):
            return 503, {"error": "storage_unavailable", "message": "Workspace storage is temporarily unavailable. Try again."}, {}

    def _auth(self, parts, values, client, headers):
        if len(parts) != 2 or parts[1] not in ("register", "login", "recover"):
            if parts in (["auth", "logout"], ["auth", "password"]):
                return self._protected_auth(parts, values, headers)
            raise WorkspaceError(404, "not_found", "No authentication route matches this path.")
        command = parts[1]
        if not self._allowed(("auth", client), 10, 60):
            return self._limited(60)
        username = values.get("username")
        if not isinstance(username, str) or not username.isascii() or not 3 <= len(username) <= 40:
            raise WorkspaceError(400, "invalid_username", "Use a valid Siyab username of 3 to 40 ASCII characters.")
        identity = hashlib.sha256(username.casefold().encode("ascii")).hexdigest()
        if not self._allowed(("identity", command, client, identity), 10, 60):
            return self._limited(60)
        if command == "register":
            if not self._allowed(("register", client), 5, 3600):
                return self._limited(3600)
            self._fields(values, {"username", "password"})
            result = self.store.register(values["username"], values["password"])
        elif command == "login":
            self._fields(values, {"username", "password"})
            result = self.store.login(values["username"], values["password"])
        else:
            if not self._allowed(("recover", client), 5, 3600):
                return self._limited(3600)
            self._fields(values, {"username", "recovery_code", "new_password"})
            result = self.store.recover(values["username"], values["recovery_code"], values["new_password"])
        previous = self._token(headers)
        if previous is not None:
            self.store.logout(previous)
        return self._auth_reply(result, 201 if command == "register" else 200)

    def _protected_auth(self, parts, values, headers):
        token = self._token(headers)
        if token is None:
            raise WorkspaceError(401, "authentication_required", "Sign in before changing your session.")
        session = self.store.authenticate(token)
        csrf = headers.get_all("X-CSRF-Token", [])
        if len(csrf) != 1 or not TOKEN.fullmatch(csrf[0]) or not hmac.compare_digest(csrf[0], session["csrf_token"]):
            raise WorkspaceError(403, "csrf_denied", "Refresh the session and try this command again.")
        if parts[1] == "logout":
            self._fields(values, set())
            self.store.logout(token)
            return 200, {"signed_out": True}, {"Set-Cookie": self._clear_cookie()}
        self._fields(values, {"current_password", "new_password"})
        if not self._allowed(("password", session["user"]["id"]), 5, 60):
            return self._limited(60)
        return self._auth_reply(self.store.change_password(token, values["current_password"], values["new_password"]))

    def _team(self, actor, team, parts, method, query, values):
        if parts[0] == "delivery":
            with self.store._connection() as connection:
                self.store._member(connection, actor, team)
            if self.delivery is None:
                raise WorkspaceError(503, "delivery_unavailable", "Telegram delivery is unavailable.")
            if method in ("GET", "HEAD"):
                if parts == ["delivery"]:
                    self._query(query, set())
                    return 200, self.delivery.status(actor, team), {}
                if parts == ["delivery", "log"]:
                    params = self._numbers(self._query(query, {"before", "limit"}), {"before": (1, 2**63 - 1), "limit": (1, 100)})
                    return 200, self.delivery.list_deliveries(actor, team, **params), {}
            elif parts == ["delivery", "link"]:
                self._fields(values, set())
                return 201, self.delivery.begin_link(actor, team), {}
            elif parts == ["delivery", "confirm"]:
                self._fields(values, {"generation"})
                return 200, self.delivery.confirm_link(actor, team, values["generation"]), {}
            elif parts == ["delivery", "revoke"]:
                self._fields(values, set())
                return 200, self.delivery.revoke(actor, team), {}
            elif len(parts) == 3 and parts[2] == "retry":
                self._fields(values, set())
                identifier = self._numbers({"id": parts[1]}, {"id": (1, 2**63 - 1)})["id"]
                return 200, self.delivery.retry_dead(actor, team, identifier), {}
            raise WorkspaceError(404, "not_found", "No delivery route matches this path.")
        if parts[0] == "investigation-jobs":
            with self.store._connection(write=method == "POST") as connection:
                self.store._member(connection, actor, team, write=method == "POST")
                if self.investigation_jobs is None:
                    raise WorkspaceError(503, "jobs_unavailable", "Queued investigations are unavailable.")
                if method in ("GET", "HEAD"):
                    if len(parts) == 1:
                        params = self._numbers(self._query(query, {"limit"}), {"limit": (1, 100)})
                        return 200, self.investigation_jobs.list(team, **params), {}
                    if len(parts) == 2:
                        self._query(query, set())
                        return 200, self.investigation_jobs.get(team, parts[1]), {}
                    if len(parts) == 3 and parts[2] == "result":
                        params = self._numbers(self._query(query, {"before", "limit"}), {"before": (0, 2**63 - 1), "limit": (1, 100)})
                        return 200, self.investigation_jobs.result(team, parts[1], **params), {}
                elif len(parts) == 1:
                    allowed = {"account", "kind", "from_timestamp_ms", "to_timestamp_ms", "max_pages", "max_rows", "depth", "max_accounts", "pages_per_account"}
                    if "account" not in values or set(values) - allowed:
                        raise WorkspaceError(400, "invalid_fields", "Send an account and supported investigation bounds only.")
                    params = {key: value for key, value in values.items() if key != "account"}
                    job = self.investigation_jobs.create(team, actor, values["account"], **params)
                    self.store._audit(connection, team, actor, "investigation.created", job["id"], {"kind": job["kind"], "account": job["account_id"]})
                    return 201, job, {}
                elif len(parts) == 3 and parts[2] in ("cancel", "retry"):
                    self._fields(values, set())
                    job = (self.investigation_jobs.cancel(team, parts[1]) if parts[2] == "cancel" else self.investigation_jobs.retry(team, parts[1]))
                    self.store._audit(connection, team, actor, "investigation." + parts[2], job["id"])
                    return 200, job, {}
            raise WorkspaceError(404, "not_found", "No queued investigation route matches this path.")
        if method in ("GET", "HEAD"):
            if parts == ["cases"]:
                params = self._query(query, {"offset", "limit"})
                return 200, self.store.list_cases(actor, team, **self._numbers(params, {"offset": (0, 10000), "limit": (1, 50)})), {}
            if parts == ["members"]:
                self._query(query, set())
                return 200, {"members": self.store.list_members(actor, team)}, {}
            if parts == ["watches"]:
                self._query(query, set())
                return 200, {"watches": self.store.list_watchlist(actor, team)}, {}
            if parts in (["inbox"], ["audit"]):
                allowed = {"after", "before", "limit"} if parts == ["inbox"] else {"after", "limit"}
                params = self._numbers(self._query(query, allowed), {"after": (0, 2**63 - 1), "before": (1, 2**63 - 1), "limit": (1, 50)})
                return 200, (self.store.list_inbox(actor, team, **params) if parts == ["inbox"] else self.store.list_audit(actor, team, **params)), {}
        elif parts == ["import"]:
            self._fields(values, {"cases", "watchlist"})
            return 200, self.store.import_workspace(actor, team, values["cases"], values["watchlist"]), {}
        elif parts == ["cases"]:
            self._fields(values, {"title", "notes", "url"})
            return 201, self.store.create_case(actor, team, values["title"], values["notes"], values["url"]), {}
        elif len(parts) == 2 and parts[0] == "cases":
            self._fields(values, {"title", "notes", "url", "version"})
            return 200, self.store.update_case(actor, team, parts[1], values["title"], values["notes"], values["url"], values["version"]), {}
        elif len(parts) == 3 and parts[0] == "cases" and parts[2] == "delete":
            self._fields(values, {"version"})
            return 200, self.store.delete_case(actor, team, parts[1], values["version"]), {}
        elif parts == ["watches"]:
            self._fields(values, {"account"})
            return 201, self.store.add_watch(actor, team, values["account"]), {}
        elif parts == ["watches", "remove"]:
            self._fields(values, {"account"})
            return 200, self.store.remove_watch(actor, team, values["account"]), {}
        elif parts == ["invites"]:
            self._fields(values, {"username", "role"})
            return 201, self.store.invite_member(actor, team, values["username"], values["role"]), {}
        elif len(parts) == 3 and parts[0] == "members" and parts[2] == "role":
            self._fields(values, {"role"})
            return 200, self.store.set_role(actor, team, parts[1], values["role"]), {}
        elif len(parts) == 3 and parts[0] == "members" and parts[2] == "remove":
            self._fields(values, set())
            return 200, self.store.remove_member(actor, team, parts[1]), {}
        elif len(parts) == 3 and parts[0] == "inbox" and parts[2] == "ack":
            self._fields(values, set())
            alert_id = self._numbers({"id": parts[1]}, {"id": (1, 2**63 - 1)})["id"]
            return 200, self.store.ack_inbox(actor, team, alert_id), {}
        raise WorkspaceError(404, "not_found", "No team workspace route matches this path.")

    def _token(self, headers):
        cookies = headers.get_all("Cookie", [])
        if not cookies:
            return None
        if len(cookies) != 1:
            raise WorkspaceError(400, "invalid_cookie", "The workspace session cookie is ambiguous.")
        count = sum(part.strip().startswith(self.cookie_name + "=") for part in cookies[0].split(";"))
        if not count:
            return None
        if count != 1:
            raise WorkspaceError(400, "invalid_cookie", "The workspace session cookie is ambiguous.")
        parsed = SimpleCookie()
        try:
            parsed.load(cookies[0])
        except CookieError:
            raise WorkspaceError(400, "invalid_cookie", "The workspace session cookie is malformed.") from None
        item = parsed.get(self.cookie_name)
        if item is None or not TOKEN.fullmatch(item.value):
            return None
        return item.value

    def _auth_reply(self, result, status=200):
        cookie = f'{self.cookie_name}={result["session_token"]}; Path=/; HttpOnly; SameSite=Strict'
        if self.secure_cookie:
            cookie += "; Secure"
        cookie += "; Expires=" + format_datetime(datetime.fromtimestamp(result["expires_at"] / 1000, UTC), usegmt=True)
        body = {key: value for key, value in result.items() if key != "session_token"}
        return status, body, {"Set-Cookie": cookie}

    def _clear_cookie(self):
        return f'{self.cookie_name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0' + ("; Secure" if self.secure_cookie else "")

    def _allowed(self, key, limit, seconds):
        now = self.clock()
        with self._lock:
            start, count = self._rates.get(key, (now, 0))
            if now - start >= seconds:
                start, count = now, 0
            if len(self._rates) >= 4096 and key not in self._rates:
                self._rates.popitem(last=False)
            self._rates[key] = (start, count + 1)
            self._rates.move_to_end(key)
            return count < limit

    @staticmethod
    def _limited(seconds):
        return 429, {"error": "rate_limited", "message": "Too many workspace requests. Try again later."}, {"Retry-After": str(seconds)}

    @staticmethod
    def _fields(values, expected):
        if set(values) != expected:
            raise WorkspaceError(400, "invalid_fields", "Request fields must be: " + (", ".join(sorted(expected)) or "none") + ".")

    @staticmethod
    def _query(query, allowed):
        try:
            pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        except ValueError:
            raise WorkspaceError(400, "invalid_query", "Query fields are malformed.") from None
        if len({name for name, _ in pairs}) != len(pairs) or any(name not in allowed for name, _ in pairs):
            raise WorkspaceError(400, "invalid_query", "Query fields are unsupported or repeated.")
        return dict(pairs)

    @staticmethod
    def _numbers(values, bounds):
        result = {}
        for key, value in values.items():
            low, high = bounds[key]
            if not re.fullmatch(r"0|[1-9][0-9]{0,18}", value) or not low <= int(value) <= high:
                raise WorkspaceError(400, "invalid_query", "Pagination is outside the supported range.")
            result[key] = int(value)
        return result

    @staticmethod
    def _unique_object(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError("repeated JSON field")
            values[key] = value
        return values
