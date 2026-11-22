"""The measured, live-only verdict service. No endpoint accepts signing key material."""

import copy
import dataclasses
import io
import json
import re
import secrets
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from enclave import dstack, keys, measure
from enclave import labels as enclave_labels
from gate import agent, base58, canon, rpc, rules, verdict
from intel import labels

CONFIG_PATH = Path(__file__).with_name("build.json")
MAX_BODY = 65536
REQUEST_TIMEOUT_S = 10
REQUEST_DEADLINE_S = 45
ATTESTATION_REFRESH_S = 3600
ATTESTATION_RETRY_S = 60
ATTESTATION_LIFETIME_S = 86400
ROUTES = {"/health": "GET", "/attestation": "GET", "/verdict": "POST"}


def _network_account(value, network):
    canon.account_id(value, "account")
    testnet = value == "testnet" or value.endswith(".testnet")
    if testnet != (network == "testnet"):
        raise canon.CanonError("account differs from the measured network")


@dataclasses.dataclass(frozen=True)
class BuildConfig:
    network: str
    registry: str
    label_delta_url: str | None = None

    def __post_init__(self):
        if self.network not in ("mainnet", "testnet"):
            raise ValueError("build network must be mainnet or testnet")
        _network_account(self.registry, self.network)
        if self.label_delta_url is not None:
            parts = urlsplit(self.label_delta_url)
            if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                    or parts.password is not None or parts.query or parts.fragment):
                raise ValueError("label delta needs a measured HTTPS URL without credentials, query or fragment")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _json(data):
    return json.loads(data, object_pairs_hook=_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid JSON number")))


def _policy(policy):
    if not isinstance(policy, dict):
        raise ValueError("the Gate agent policy could not be checked")
    for field in ("enabled", "gate_paused"):
        if type(policy.get(field)) is not bool:
            raise ValueError("the Gate policy has an invalid flag")
    for field in ("allowlist", "gate_denylist"):
        values = policy.get(field)
        if not isinstance(values, list):
            raise ValueError("the Gate policy has an invalid account list")
        for value in values:
            canon.account_id(value, field)
    if "unknown_caps" not in policy:
        raise ValueError("the Gate policy omits unknown caps")
    for field in ("caps", "unknown_caps"):
        caps = policy.get(field)
        if field == "unknown_caps" and caps is None:
            continue
        if not isinstance(caps, list):
            raise ValueError("the Gate policy has invalid caps")
        for cap in caps:
            if not isinstance(cap, dict):
                raise ValueError("a cap must be an object")
            asset = cap.get("asset")
            if asset != "Near":
                if not isinstance(asset, dict) or set(asset) != {"Ft"}:
                    raise ValueError("a cap has an invalid asset")
                canon.account_id(asset["Ft"], "cap asset")
            for key in ("per_call", "window_cap", "spent"):
                value = cap.get(key)
                if (not isinstance(value, str) or len(value) > 39
                        or not re.fullmatch(r"0|[1-9][0-9]*", value) or int(value) >= 2**128):
                    raise ValueError("a cap must contain u128 decimal amounts")
            for key in ("window_ms", "window_start_ms"):
                if type(cap.get(key)) is not int or not 0 <= cap[key] < 2**64:
                    raise ValueError("a cap must contain u64 window times")
    return policy


class _VerdictSigner:
    key_id = keys.EnclaveSigner.key_id

    def __init__(self, enclave, store):
        self.enclave = enclave
        self.store = store

    def sign(self, digest):
        with self.enclave._lock:
            self.enclave._require_ready()
            if self.enclave._store is not self.store:
                raise ValueError("labels changed during judgement; retry the request")
            return self.enclave._signer.sign(digest)


class _RequestReader(io.RawIOBase):
    def __init__(self, connection):
        super().__init__()
        self.connection = connection
        self.deadline = time.monotonic() + REQUEST_TIMEOUT_S

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request read deadline exceeded")
        self.connection.settimeout(remaining)
        return self.connection.recv_into(buffer)


class _NearblocksClient(rpc.Client):
    _reservation_lock = threading.Lock()

    def _respect_nearblocks(self, host):
        with self._reservation_lock:
            return super()._respect_nearblocks(host)


class EnclaveService:
    def __init__(self, config, client, nearblocks, archival, guest, *, floor=None):
        self.config = config
        self.client = client
        self.clients = (client, nearblocks, archival)
        self.guest = guest
        self._signer = keys.EnclaveSigner()
        self._store = labels.load() if floor is None else floor
        self._floor_sha256 = self._store.sha256
        self._measurement_id = None
        self._identity_fault = False
        self._attestation = None
        self._attestation_deadline = 0
        self._ready = False
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def _chain_time(self, client):
        block = client.final_block()
        value = block["header"]["timestamp_nanosec"]
        if (not isinstance(value, str) or len(value) > 20 or not re.fullmatch(r"0|[1-9][0-9]*", value)
                or int(value) >= 2**64):
            raise ValueError("the final block timestamp is invalid")
        return int(value) // 1000000

    def _clients_off(self):
        if any(client.mode != "off" for client in self.clients):
            raise ValueError("enclave reads require mode off")

    def refresh_attestation(self):
        with self._refresh_lock:
            started = time.monotonic()
            try:
                with rpc.request_deadline(REQUEST_DEADLINE_S):
                    with self._lock:
                        if self._identity_fault:
                            raise ValueError("measurement changed; restart with a new enclave key")
                        previous = self._attestation
                    self._clients_off()
                    client = copy.copy(self.client)
                    if isinstance(client, rpc.Client):
                        client._block_hashes = {}
                    issued = self._chain_time(client)
                    if previous is not None and issued < previous["issued_at_ms"]:
                        raise ValueError("the final block timestamp moved backwards")
                    public = self._signer.public_key_hex
                    aux = bytes(32).hex()
                    binding = measure.report_data(self.config.registry, public, issued, aux)
                    quoted = self.guest.get_quote(binding)
                    quote = bytes.fromhex(quoted["quote"])
                    if quoted["report_data"] != binding.hex() or measure.parse_quote(quote)["report_data"] != binding.hex():
                        raise ValueError("quote report data differs from enclave binding")
                    info = _json(self.guest.info()["tcb_info"])
                    measurement = measure.measurement_from_tcb(quote, info)
                    events = _json(quoted["event_log"])
                    if (not isinstance(events, list) or any(not isinstance(event, dict)
                            or type(event.get("imr")) is not int for event in events)):
                        raise ValueError("invalid GetQuote event log")
                    actual = [event for event in events if event["imr"] == 3]
                    expected = [event for event in info["event_log"] if event["imr"] == 3]
                    if len(actual) != len(expected) or any(
                            type(event.get("event_type")) is not int
                            or (event != raw and event != {**raw, "digest": ""})
                            for event, raw in zip(actual, expected)):
                        raise ValueError("Info and GetQuote RTMR3 events differ")
                    identity = measure.measurement_id(measurement)
                    with self._lock:
                        if self._measurement_id is not None and self._measurement_id != identity:
                            self._identity_fault = True
                            self._ready = False
                            raise ValueError("measurement changed; restart with a new enclave key")
                        deadline = started + ATTESTATION_LIFETIME_S
                        if previous is not None and issued == previous["issued_at_ms"]:
                            deadline = min(deadline, self._attestation_deadline)
                        if time.monotonic() >= deadline:
                            raise ValueError("the quote has no new chain time before cache expiry")
                        self._measurement_id = identity
                        self._attestation = {"registry": self.config.registry, "public_key_hex": public,
                                             "issued_at_ms": issued, "aux": aux, "quote": quoted["quote"],
                                             "tcb_info": info}
                        self._attestation_deadline = deadline
                        self._ready = True
                        return copy.deepcopy(self._attestation)
            except (OSError, ValueError, KeyError, TypeError, rpc.RpcError, rpc.TransportError,
                    rpc.HttpError, rpc.RateLimited) as error:
                raise ValueError("attestation refresh unavailable") from error

    def _require_ready(self):
        if not self._ready or self._identity_fault or time.monotonic() >= self._attestation_deadline:
            raise ValueError("attestation unavailable; signing is disabled")

    def attestation(self):
        with self._lock:
            self._require_ready()
            return copy.deepcopy(self._attestation)

    def follow_attestations(self, stop):
        delay = ATTESTATION_REFRESH_S if self.health()["ok"] else ATTESTATION_RETRY_S
        while not stop.wait(delay):
            try:
                self.refresh_attestation()
            except ValueError:
                delay = ATTESTATION_RETRY_S
            else:
                delay = ATTESTATION_REFRESH_S

    def health(self):
        with self._lock:
            return {"ok": self._ready and not self._identity_fault
                    and time.monotonic() < self._attestation_deadline, "network": self.config.network,
                    "label_floor_sha256": self._floor_sha256, "labels_sha256": self._store.sha256}

    def judge(self, body):
        if not isinstance(body, dict):
            raise canon.CanonError("the request must be an object")
        allowed = {"kind", "token", "receiver", "amount", "memo", "gate", "agent_pk_hex", "policy"}
        if set(body) - allowed:
            raise canon.CanonError("unknown verdict request field")
        gate, public = body.get("gate"), body.get("agent_pk_hex")
        _network_account(gate, self.config.network)
        canon.validate_transfer(gate=gate, agent_pk_hex=public, kind=body.get("kind"), token_id=body.get("token"),
                                receiver_id=body.get("receiver"), amount=body.get("amount"), memo=body.get("memo"))
        with rpc.request_deadline(REQUEST_DEADLINE_S):
            self._clients_off()
            with self._lock:
                self._require_ready()
                attestation = self._attestation
            clients = tuple(copy.copy(client) for client in self.clients)
            for client in clients:
                if isinstance(client, rpc.Client):
                    client._block_hashes = {}
            now_ms = self._chain_time(clients[0])
            policy = _policy(agent.read_policy_views(clients[0], gate, base58.encode_key(bytes.fromhex(public))))
            delta = (_json(enclave_labels.fetch_delta(self.config.label_delta_url))
                     if self.config.label_delta_url is not None else None)
            with self._lock:
                self._require_ready()
                if (now_ms < attestation["issued_at_ms"]
                        or now_ms - attestation["issued_at_ms"] >= ATTESTATION_LIFETIME_S * 1000):
                    if self._attestation is attestation:
                        self._ready = False
                    raise ValueError("refresh the enclave attestation before signing")
                if delta is not None:
                    self._store = enclave_labels.merge(self._store, delta)
                store = self._store
                identity = self._measurement_id
            with tempfile.TemporaryDirectory(prefix="siyab-evidence-") as directory:
                result = verdict.request(
                    body.get("kind"), body.get("token"), body.get("receiver"), body.get("amount"), body.get("memo"),
                    gate=gate, agent_pk_hex=public, policy=policy, store=store, rpc=clients[0],
                    nearblocks=clients[1], archival=clients[2], signer=_VerdictSigner(self, store),
                    sign_actions=("sign",), injection_scanner=None, evidence_dir=directory,
                    clock=lambda: now_ms / 1000,
                    issuers=rules.issuers_for(self.config.network))
                evidence_bytes = Path(result.pop("evidence_path")).read_bytes()
            return {**result, "evidence": evidence_bytes.decode("utf-8"),
                    "signer": {"public_key_hex": self._signer.public_key_hex, "measurement_id": identity,
                               "registry": self.config.registry},
                    "label_floor_sha256": self._floor_sha256, "labels_sha256": store.sha256}


def make_service(config):
    mainnet = config.network == "mainnet"
    return EnclaveService(config, rpc.Client(rpc.MAINNET_RPC_URL if mainnet else rpc.TESTNET_RPC_URL, "off"),
                          _NearblocksClient(rpc.NEARBLOCKS_URL if mainnet else rpc.NEARBLOCKS_TESTNET_URL,
                                            "off", kind="http"),
                          rpc.Client(rpc.ARCHIVAL_RPC_URL if mainnet else rpc.ARCHIVAL_TESTNET_RPC_URL, "off"),
                          dstack.DstackClient())


def make_server(host, port, enclave):
    slots = threading.BoundedSemaphore(8)

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, address, handler):
            self.relay_token = secrets.token_hex(32)
            super().__init__(address, handler)

        def process_request(self, request, address):
            if not slots.acquire(blocking=False):
                request.close()
                return
            try:
                super().process_request(request, address)
            except Exception:
                slots.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                slots.release()

        def handle_error(self, request, address):
            print("enclave request failed", file=sys.stderr)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(REQUEST_TIMEOUT_S)
            super().setup()
            self.rfile.close()
            self.rfile = io.BufferedReader(_RequestReader(self.request))

        def log_message(self, format, *args):
            return

        def _send(self, status, body):
            data = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(data)

        def _dispatch(self):
            route = ROUTES.get(self.path)
            if route is None:
                self._send(404, {"error": "not found"})
                return
            if self.command != route:
                self._send(405, {"error": "method not allowed"})
                return
            if self.path != "/health":
                authorization = self.headers.get_all("Authorization", [])
                expected = ("Bearer " + self.server.relay_token).encode("ascii")
                if len(authorization) != 1 or not secrets.compare_digest(authorization[0].encode("utf-8"), expected):
                    self._send(401, {"error": "relay authentication required"})
                    return
            try:
                if self.path == "/health":
                    health = enclave.health()
                    self._send(200 if health["ok"] else 503, health)
                elif self.path == "/attestation":
                    self._send(200, enclave.attestation())
                else:
                    lengths = self.headers.get_all("Content-Length", [])
                    if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0])
                            or self.headers.get("Transfer-Encoding") is not None):
                        self._send(400, {"error": "one Content-Length is required"})
                        return
                    if int(lengths[0]) > MAX_BODY:
                        self._send(413, {"error": "request too large"})
                        return
                    if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                        self._send(415, {"error": "application/json is required"})
                        return
                    raw = self.rfile.read(int(lengths[0]))
                    if len(raw) != int(lengths[0]):
                        raise ValueError("truncated request")
                    self.request.settimeout(REQUEST_TIMEOUT_S)
                    try:
                        body = _json(raw)
                        if not isinstance(body, dict):
                            raise ValueError("request must be an object")
                    except (ValueError, UnicodeError, RecursionError):
                        self._send(400, {"error": "invalid verdict request"})
                        return
                    self._send(200, enclave.judge(body))
            except (canon.CanonError, json.JSONDecodeError, UnicodeError, RecursionError):
                self._send(400, {"error": "invalid verdict request"})
            except (ValueError, OSError, KeyError, TypeError, rpc.RpcError, rpc.TransportError,
                    rpc.HttpError, rpc.RateLimited):
                self._send(503, {"outcome": "UNKNOWN", "action": "ask", "verdict": None,
                                 "error": "the request could not be checked"})

        def __getattr__(self, name):
            if name.startswith("do_"):
                return self._dispatch
            raise AttributeError(name)

    return Server((host, port), Handler)


def main():
    try:
        document = _json(CONFIG_PATH.read_bytes())
        if not isinstance(document, dict) or set(document) != {"network", "registry", "label_delta_url"}:
            raise ValueError("expected measured build settings")
        config = BuildConfig(**document)
        enclave = make_service(config)
        try:
            enclave.refresh_attestation()
        except ValueError:
            print("attestation unavailable; signing is disabled", file=sys.stderr)
        with make_server("0.0.0.0", 8080, enclave) as server:
            print(json.dumps({"relay_bearer_token": server.relay_token}), flush=True)
            stop = threading.Event()
            worker = threading.Thread(target=enclave.follow_attestations, args=(stop,), daemon=True)
            worker.start()
            try:
                server.serve_forever()
            finally:
                stop.set()
                worker.join(timeout=1)
    except (ValueError, OSError, TypeError, ImportError):
        print("the measured enclave service could not start", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
