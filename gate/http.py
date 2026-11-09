"""An unsigned local verdict endpoint on the standard library http.server: python -m gate http.

Routes:

    POST /verdict   one transfer as a JSON object of kind, token, receiver, amount and memo, the fields
                    check_near_payment takes; answers the gate.verdict.request result with verdict and
                    message null and the field signing, because this endpoint never signs
    GET  /health    {"ok": true, "mode": "replay" or "live"}, without a key

POST /verdict needs Authorization: Bearer <key> with a key from the file SHIELDBOT_API_KEYS_FILE names (one
per line; blank lines and lines starting with # are skipped), compared as fixed-length digests with
hmac.compare_digest against every key. Without that file, or with no key in it, the server does not start.
Each key gets SHIELDBOT_API_RATE requests (default 60) per fixed minute window, counted in this process; past
that the answer is 429 with Retry-After. Connections have a 10 second socket timeout, and at most eight
requests may be in flight. Other answers: 400 for an invalid body or Content-Length, 401 without a known key,
404 for any other path, 405 with Allow for any other method, 411 without a Content-Length, 413 when the
Content-Length is over MAX_BODY (the body is not read), 503 when all request slots are occupied (the body is
not read), and 500 with no traceback. Every answer is JSON and carries Cache-Control: no-store.

The transfer is judged as check_near_payment judges it without live signing: bound to GATE_ACCOUNT_ID
(default gate.near) and the TEST agent key, against GATE_POLICY (default the MCP server's DEFAULT_POLICY),
over recorded fixtures (mode replay). Live reads (mode live) need GATE_FIXTURES_MODE=off and a .testnet
GATE_ACCOUNT_ID; any other GATE_FIXTURES_MODE, or off with any other Gate, refuses to start. No signing key
is read and no verdict signer is built: sign_actions is empty, so even an ALLOW whose action is sign comes
back unsigned. The server binds 127.0.0.1 by default and speaks plain HTTP; TLS is the job of the reverse
proxy in front of it. Evidence documents go to a temporary directory, removed when the server stops unless
GATE_KEEP_EVIDENCE=1.
"""

import hashlib
import hmac
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gate import canon, ed25519, rules, verdict
from gate.__main__ import _clients
from intel import labels
from mcp.server import DEFAULT_GATE, DEFAULT_POLICY, KEEP_EVIDENCE

KEYS_FILE = "SHIELDBOT_API_KEYS_FILE"
RATE = "SHIELDBOT_API_RATE"
DEFAULT_RATE = 60
WINDOW_S = 60
MAX_BODY = 65536
REQUEST_TIMEOUT_S = 10
MAX_IN_FLIGHT = 8
FIELDS = ("kind", "token", "receiver", "amount", "memo")
ROUTES = {"/verdict": "POST", "/health": "GET"}
JSON_TYPE = "application/json; charset=utf-8"
SIGNING = ("this endpoint does not sign; a signed verdict needs the verifier key, which is never on this "
           "server")


class ConfigError(Exception):
    """A configuration the server refuses to start with."""


def read_keys(path):
    """The API keys in the file: one per line, blank lines and # comment lines skipped."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read the API keys file {path}: {error}") from error
    keys = tuple(line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))
    if not keys:
        raise ConfigError(f"the API keys file {path} holds no key")
    return keys


def settings(environ=os.environ):
    """The keys, rate, mode, Gate and policy the server runs with; raises ConfigError for any it refuses."""
    path = environ.get(KEYS_FILE)
    if not path:
        raise ConfigError(f"set {KEYS_FILE} to a file of API keys; the endpoint has no open mode")
    keys = read_keys(path)
    try:
        rate = int(environ.get(RATE) or DEFAULT_RATE)
    except ValueError as error:
        raise ConfigError(f"{RATE} must be a whole number of requests per minute") from error
    if rate < 1:
        raise ConfigError(f"{RATE} must be at least 1, not {rate}")
    fixtures_mode = environ.get("GATE_FIXTURES_MODE") or "replay"
    gate = environ.get("GATE_ACCOUNT_ID") or DEFAULT_GATE
    if fixtures_mode not in ("replay", "off"):
        raise ConfigError(f"GATE_FIXTURES_MODE must be replay or off for this endpoint, not {fixtures_mode}")
    if fixtures_mode == "off" and not gate.endswith(".testnet"):
        raise ConfigError(f"live reads are for a .testnet Gate only, not {gate}")
    try:
        policy = json.loads(environ.get("GATE_POLICY") or DEFAULT_POLICY)
    except ValueError as error:
        raise ConfigError(f"GATE_POLICY is not JSON: {error}") from error
    if not isinstance(policy, dict):
        raise ConfigError("GATE_POLICY must be a JSON object in the get_agent shape")
    reports_account = environ.get("REPORTS_ACCOUNT_ID")
    return {"keys": keys, "rate": rate, "mode": "replay" if fixtures_mode == "replay" else "live", "gate": gate,
            "policy": policy, "reports_account": reports_account}


class RateLimit:
    """At most rate requests per key in each fixed WINDOW_S window of clock()."""

    def __init__(self, rate, clock):
        self.rate, self.clock = rate, clock
        self.counts = {}
        self.lock = threading.Lock()

    def retry_after(self, key):
        """None when the key may make this request, which is then counted; else the seconds to wait."""
        with self.lock:
            now = self.clock()
            window = math.floor(now / WINDOW_S)
            start, count = self.counts.get(key, (window, 0))
            if window > start:
                count = 0
                start = window
            if count >= self.rate:
                return max(1, math.ceil((start + 1) * WINDOW_S - now))
            self.counts[key] = (start, count + 1)
            return None


def _key(header, keys):
    """The key a Bearer header names, compared against every key, or None."""
    if not header or not header.startswith("Bearer "):
        return None
    presented = hashlib.sha256(header[len("Bearer "):].encode("utf-8")).digest()
    found = None
    for key in keys:
        if hmac.compare_digest(presented, hashlib.sha256(key.encode("utf-8")).digest()):
            found = key
    return found


def make_server(host, port, config, evidence_dir, store=None, clock=time.monotonic, request_timeout=REQUEST_TIMEOUT_S,
                max_in_flight=MAX_IN_FLIGHT):
    """A ThreadingHTTPServer for the endpoint; port 0 picks a free port. clock drives the rate windows."""
    store = store or labels.load()
    gate, policy = config["gate"], config["policy"]
    reports_account = config["reports_account"]
    agent_pk_hex = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex()
    client, nearblocks, scanner, archival = _clients(gate)
    issuers = None if gate.endswith(".testnet") else rules.issuers_for("mainnet")
    limit = RateLimit(config["rate"], clock)
    judging = threading.Lock()
    requests = threading.BoundedSemaphore(max_in_flight)

    def judge(body):
        with judging:
            result = verdict.request(body.get("kind"), body.get("token"), body.get("receiver"), body.get("amount"),
                                     body.get("memo"), gate=gate, agent_pk_hex=agent_pk_hex, policy=policy,
                                     store=store, rpc=client, nearblocks=nearblocks, signer=None,
                                     sign_actions=(), injection_scanner=scanner, archival=archival,
                                     evidence_dir=evidence_dir, issuers=issuers,
                                     reports_account=reports_account)
        result.pop("evidence_path")
        return {**result, "verdict": None, "message": None, "signing": SIGNING}

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self):
            if self.command not in ("GET", "POST"):
                self.close_connection = True
                self._send(405, {"error": "method not allowed"}, {"Allow": "GET, POST"})
                return
            if not requests.acquire(blocking=False):
                self.close_connection = True
                self._send(503, {"error": "server busy"})
                return
            try:
                path = self.path.split("?", 1)[0]
                if path not in ROUTES:
                    self._send(404, {"error": "not found"})
                elif self.command != ROUTES[path]:
                    self._send(405, {"error": f"{path} takes {ROUTES[path]} only"}, {"Allow": ROUTES[path]})
                elif path == "/health":
                    self._send(200, {"ok": True, "mode": config["mode"]})
                else:
                    self._verdict()
            finally:
                requests.release()

        def __getattr__(self, name):
            if name.startswith("do_"):
                return self._dispatch
            raise AttributeError(name)

        def _verdict(self):
            key = _key(self.headers.get("Authorization"), config["keys"])
            if key is None:
                self._send(401, {"error": "a known API key is required: Authorization: Bearer <key>"},
                           {"WWW-Authenticate": "Bearer"})
                return
            wait = limit.retry_after(key)
            if wait is not None:
                self._send(429, {"error": f"over {config['rate']} requests a minute for this key"},
                           {"Retry-After": str(wait)})
                return
            length = self.headers.get("Content-Length")
            if length is None:
                self._send(411, {"error": "a Content-Length is required"})
                return
            if not (length.isascii() and length.isdigit()):
                self._send(400, {"error": "Content-Length is not a byte count"})
                return
            if len(length) > 10:
                self._send(400, {"error": "Content-Length must not exceed 10 digits"})
                return
            try:
                length = int(length)
            except ValueError:
                self._send(400, {"error": "Content-Length is not a byte count"})
                return
            if length > MAX_BODY:
                self._send(413, {"error": f"the body is over {MAX_BODY} bytes"})
                return
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError as error:
                self._send(400, {"error": f"the body is not JSON: {error}"})
                return
            if not isinstance(body, dict):
                self._send(400, {"error": "the body must be a JSON object"})
                return
            unknown = sorted(set(body) - set(FIELDS))
            if unknown:
                self._send(400, {"error": f"unknown fields: {', '.join(unknown)}"})
                return
            try:
                result = judge(body)
            except canon.CanonError as error:
                self._send(400, {"error": f"invalid request: {error}"})
                return
            except Exception as error:  # noqa: BLE001 (a failed verdict is answered 500, never a traceback)
                print(f"verdict failed: {type(error).__name__}: {error}", file=sys.stderr)
                self._send(500, {"error": "internal error"})
                return
            self._send(200, result)

        def _send(self, status, body, headers=None):
            data = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", JSON_TYPE)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

    class Server(ThreadingHTTPServer):
        def get_request(self):
            request, client_address = super().get_request()
            request.settimeout(request_timeout)
            return request, client_address

    return Server((host, port), Handler)


def serve(host, port):
    try:
        config = settings()
    except ConfigError as error:
        print(f"not started: {error}", file=sys.stderr)
        return 2
    evidence_dir = tempfile.mkdtemp(prefix="siyab-http-")
    server = make_server(host, port, config, evidence_dir)
    print(f"serving the unsigned verdict endpoint ({config['mode']}) on "
          f"http://{server.server_address[0]}:{server.server_address[1]}; evidence documents go to {evidence_dir}, "
          f"removed at exit unless {KEEP_EVIDENCE}=1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
        if os.environ.get(KEEP_EVIDENCE) != "1" or not os.listdir(evidence_dir):
            shutil.rmtree(evidence_dir)
    return 0
