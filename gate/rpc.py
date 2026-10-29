"""NEAR JSON-RPC and HTTP JSON client with record and replay fixtures.

The mode comes from GATE_FIXTURES_MODE:

    replay  (default) answer from fixtures only; a request with no fixture raises FixtureMiss,
            which the rules report as unknown, never as a pass
    record  answer from a fixture younger than 24 hours without a request; otherwise fetch,
            write the fixture and answer
    off     fetch every time and write nothing

A fixture is fixtures/rpc/<key>.json holding {"request", "response", "url", "fetched_at"}, where
the key is the sha256 hex of json.dumps({"url", "method", "params"}, sort_keys=True,
separators=(",", ":")). A POST body is never written: its params are {"body_sha256": ...}, the
sha256 of the body as sorted compact JSON. No header is written, and a URL with user info or a
query string is refused. Every request has a 30 s timeout. HTTP errors (a 429 included) raise
HttpError and are never written; a truncated or broken reply raises TransportError. A body
without the fields the caller reads raises MalformedResponse. NearBlocks fetches (its free tier
allows 6 calls a minute and 333 a day) are spaced at least 10 s apart, and past 300 in one UTC
day per process they raise RateLimited without a request.
"""

import base64
import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from gate import base58

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "rpc"
MODES = ("replay", "record", "off")
KINDS = ("rpc", "http")
TIMEOUT_S = 30
MAX_AGE_S = 24 * 60 * 60
MAINNET_RPC_URL = "https://free.rpc.fastnear.com"
TESTNET_RPC_URL = "https://test.rpc.fastnear.com"
NEARBLOCKS_URL = "https://api.nearblocks.io"
NEARBLOCKS_TESTNET_URL = "https://api-testnet.nearblocks.io"
NEARBLOCKS_HOSTS = ("api.nearblocks.io", "api-testnet.nearblocks.io")
NEARBLOCKS_MIN_INTERVAL_S = 10
NEARBLOCKS_DAILY_BUDGET = 300
# The code_hash view_account reports for an account with no contract: base58 of 32 zero bytes.
EMPTY_CODE_HASH = base58.encode(bytes(32))
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Per process NearBlocks use: host -> {"last": seconds of the last fetch, "day": UTC date, "count"}.
_NEARBLOCKS_USE = {}


class FixtureMiss(Exception):
    """Replay mode found no fixture for the request."""

    def __init__(self, key, url, method, params):
        super().__init__(f"no fixture {key}.json for {method} {url}")
        self.key = key


class HttpError(Exception):
    """The server answered with a status outside 2xx."""

    def __init__(self, status, url):
        super().__init__(f"HTTP {status} from {url}")
        self.status = status


class TransportError(Exception):
    """The reply was cut off or was not valid HTTP."""


class RateLimited(Exception):
    """The NearBlocks daily budget is used up; no request was sent."""


class MalformedResponse(ValueError):
    """A body that lacks the fields the caller reads."""


class RpcError(Exception):
    """The JSON-RPC response, or a call_function result, carried an error."""

    def __init__(self, error):
        super().__init__(json.dumps(error, sort_keys=True))
        self.error = error

    @property
    def cause(self):
        """The nearcore cause name, such as UNKNOWN_ACCOUNT, when the error has one."""
        if isinstance(self.error, dict) and isinstance(self.error.get("cause"), dict):
            return self.error["cause"].get("name")
        return None


def fixture_key(url, method, params):
    text = json.dumps({"url": url, "method": method, "params": params}, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def body_params(body):
    """The params a POST body is recorded and keyed as: only its sha256."""
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ValueError("a URL with user info would be written into fixtures; it is refused")
    if parts.query:
        raise ValueError("a URL with a query string would be written into fixtures; it is refused")
    return url


def _timestamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).strftime(_TIME_FORMAT)


def _seconds(timestamp):
    return datetime.strptime(timestamp, _TIME_FORMAT).replace(tzinfo=UTC).timestamp()


class Client:
    """One endpoint: kind "rpc" for NEAR JSON-RPC at url, kind "http" for JSON over HTTP.

    After each request, last_source is {provider, request_sha256, fetched_at} for the evidence
    document, and last_block_height is the block a JSON-RPC result was read at (or None).
    """

    def __init__(self, url, mode=None, *, kind="rpc", fixtures_dir=FIXTURES_DIR, clock=time.time,
                 sleep=time.sleep):
        mode = mode or os.environ.get("GATE_FIXTURES_MODE") or "replay"
        if mode not in MODES:
            raise ValueError(f"GATE_FIXTURES_MODE must be one of {MODES}, not {mode!r}")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
        self.url = _check_url(url).rstrip("/")
        self.mode = mode
        self.kind = kind
        self.fixtures_dir = Path(fixtures_dir)
        self.clock = clock
        self.sleep = sleep
        self.last_source = None
        self.last_block_height = None

    def call(self, method, params):
        """The result of a JSON-RPC call; raises RpcError when the response carries an error and
        MalformedResponse when it carries neither an error nor a result."""
        if self.kind != "rpc":
            raise ValueError("call needs a client of kind rpc")
        self.last_block_height = None
        body = {"jsonrpc": "2.0", "id": "gate", "method": method, "params": params}
        response = self._exchange(self.url, method, params, body)
        if not isinstance(response, dict):
            raise MalformedResponse("the JSON-RPC body is not an object")
        if "error" in response:
            raise RpcError(response["error"])
        result = response.get("result")
        if result is None:
            raise MalformedResponse("the JSON-RPC body has no result")
        self.last_block_height = result.get("block_height") if isinstance(result, dict) else None
        return result

    def get(self, url):
        """The JSON body of a GET; url is absolute or a path under the client's url."""
        return self._exchange(self._absolute(url), "GET", None, None)

    def post(self, url, body):
        """The JSON body of a POST of body; the fixture keeps only the body's sha256."""
        return self._exchange(self._absolute(url), "POST", body_params(body), body)

    def view_account(self, account_id):
        result = self.call("query", {"request_type": "view_account", "finality": "final",
                                     "account_id": account_id})
        if not isinstance(result, dict) or not isinstance(result.get("code_hash"), str):
            raise MalformedResponse("the view_account result has no code_hash")
        return result

    def code_hash(self, account_id):
        """The account's code hash in base58; EMPTY_CODE_HASH when it has no contract."""
        return self.view_account(account_id)["code_hash"]

    def call_function(self, contract, method, args):
        """The parsed JSON a view method returns."""
        encoded = json.dumps(args, sort_keys=True, separators=(",", ":")).encode("utf-8")
        result = self.call("query", {
            "request_type": "call_function",
            "finality": "final",
            "account_id": contract,
            "method_name": method,
            "args_base64": base64.b64encode(encoded).decode("ascii"),
        })
        if not isinstance(result, dict):
            raise MalformedResponse("the call_function result is not an object")
        if "error" in result:
            raise RpcError(result["error"])
        value = result.get("result")
        if not isinstance(value, list) or not all(isinstance(b, int) and 0 <= b <= 255 for b in value):
            raise MalformedResponse("the call_function result holds no byte list")
        try:
            return json.loads(bytes(value))
        except ValueError as error:
            raise MalformedResponse(f"the view method did not return JSON: {error}") from error

    def view_access_key_list(self, account_id):
        result = self.call("query", {"request_type": "view_access_key_list", "finality": "final",
                                     "account_id": account_id})
        if not isinstance(result, dict) or not isinstance(result.get("keys"), list):
            raise MalformedResponse("the view_access_key_list result has no keys list")
        return result

    def final_block(self):
        result = self.call("block", {"finality": "final"})
        if not isinstance(result, dict) or not isinstance(result.get("header"), dict):
            raise MalformedResponse("the block result has no header")
        return result

    def _absolute(self, url):
        if self.kind != "http":
            raise ValueError("get and post need a client of kind http")
        return _check_url(url if url.startswith(("https://", "http://")) else self.url + url)

    def _respect_nearblocks(self, host):
        """Space NearBlocks fetches 10 s apart and refuse past the daily budget."""
        now = self.clock()
        use = _NEARBLOCKS_USE.setdefault(host, {"last": None, "day": None, "count": 0})
        day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
        if use["day"] != day:
            use.update(day=day, count=0)
        if use["count"] >= NEARBLOCKS_DAILY_BUDGET:
            raise RateLimited(f"{use['count']} NearBlocks requests from this process today; the budget "
                              f"is {NEARBLOCKS_DAILY_BUDGET}")
        wait = 0 if use["last"] is None else use["last"] + NEARBLOCKS_MIN_INTERVAL_S - now
        if wait > 0:
            self.sleep(wait)
        use["last"] = now + max(wait, 0)
        use["count"] += 1

    def _exchange(self, url, method, params, body):
        key = fixture_key(url, method, params)
        path = self.fixtures_dir / f"{key}.json"
        host = urllib.parse.urlsplit(url).netloc
        self.last_source = {"provider": host, "request_sha256": key, "fetched_at": None}
        if self.mode != "off" and path.exists():
            fixture = json.loads(path.read_text(encoding="utf-8"))
            if self.mode == "replay" or self.clock() - _seconds(fixture["fetched_at"]) < MAX_AGE_S:
                self.last_source["fetched_at"] = fixture["fetched_at"]
                return fixture["response"]
        if self.mode == "replay":
            raise FixtureMiss(key, url, method, params)
        if host in NEARBLOCKS_HOSTS:
            self._respect_nearblocks(host)
        response = _fetch(url, body)
        fetched_at = _timestamp(self.clock())
        self.last_source["fetched_at"] = fetched_at
        if self.mode == "record":
            fixture = {"request": {"method": method, "params": params}, "response": response,
                       "url": url, "fetched_at": fetched_at}
            self.fixtures_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes((json.dumps(fixture, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        return response


def _fetch(url, body):
    headers = {"Accept": "application/json", "User-Agent": "shieldbot-near"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:
            return json.loads(reply.read())
    except urllib.error.HTTPError as error:
        raise HttpError(error.code, url) from error
    except http.client.HTTPException as error:
        raise TransportError(f"{type(error).__name__} from {url}") from error
