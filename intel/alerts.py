"""Local alert values and standard library delivery sinks."""

from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_PAYLOAD_BYTES = 16 * 1024
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_S = 30
MIN_SECRET_BYTES = 32
TIMEOUT_S = 5
SIGNATURE_WINDOW_S = 300
# A timeout, a rate limit or a server fault is worth another go; every other 4xx is the receiver saying no.
RETRYABLE_STATUS = frozenset({408, 425, 429})
_SIGNATURE = re.compile(r"t=([0-9]+),v1=([0-9a-f]{64})")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_STDOUT_LOCK = threading.Lock()


def _body(alert):
    return json.dumps(asdict(alert), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Alert:
    source: str
    severity: str
    subject: str
    ref: str
    title: str
    detail: str
    evidence_sha256: str
    created_ms: int

    def __post_init__(self):
        if self.severity not in {"info", "warn", "critical"}:
            raise ValueError("severity must be info, warn or critical")
        for name in ("source", "subject", "ref", "title"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non empty string")
        if not isinstance(self.detail, str):
            raise ValueError("detail must be a string")
        if not isinstance(self.evidence_sha256, str) or _HEX64.fullmatch(self.evidence_sha256) is None:
            raise ValueError("evidence_sha256 must be 64 lowercase hex characters")
        if isinstance(self.created_ms, bool) or not isinstance(self.created_ms, int) or self.created_ms < 0:
            raise ValueError("created_ms must be a non negative integer")
        if len(_body(self)) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"alert payload exceeds {MAX_PAYLOAD_BYTES} bytes; shorten detail")

    @property
    def dedupe_key(self):
        return self.source, self.subject, self.ref


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirects are not followed", headers, fp)


def _retryable(status):
    if status is None:
        return True
    return status in RETRYABLE_STATUS or 500 <= status < 600


def _retry_after(headers, now):
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except Exception:
        return None
    if value is None:
        return None
    value = str(value).strip()
    if value.isdigit():
        return float(int(value))
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - now)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _signature(timestamp, body, secret):
    signed = str(timestamp).encode("ascii") + b"." + body
    digest = hmac.new(secret, signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(signature_header, body, secret, now, window_s=SIGNATURE_WINDOW_S):
    if not isinstance(signature_header, str) or not isinstance(body, bytes):
        return False
    if isinstance(secret, str):
        try:
            secret = secret.encode("utf-8")
        except UnicodeEncodeError:
            return False
    if not isinstance(secret, bytes):
        return False
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return False
    if isinstance(window_s, bool) or not isinstance(window_s, (int, float)):
        return False
    try:
        now_s = float(now)
        window = float(window_s)
    except OverflowError:
        return False
    if not math.isfinite(now_s) or not math.isfinite(window) or window < 0:
        return False
    match = _SIGNATURE.fullmatch(signature_header)
    if match is None:
        return False
    try:
        timestamp = int(match.group(1))
    except ValueError:
        return False
    if abs(now_s - timestamp) > window:
        return False
    expected = _signature(timestamp, body, secret).partition(",v1=")[2]
    return hmac.compare_digest(match.group(2), expected)


class WebhookSink:
    def __init__(self, url, secret_path, *, log_path=None, opener=None, backoff=time.sleep, clock=time.time):
        parsed = urlsplit(url)
        if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None):
            raise ValueError("webhook URL must be HTTPS and must not include credentials")
        self.url = url
        self.secret_path = Path(secret_path)
        self.log_path = Path(log_path) if log_path is not None else Path.home() / ".shieldbot" / "alert-deliveries.jsonl"
        self.opener = opener if opener is not None else build_opener(_NoRedirectHandler())
        self.backoff = backoff
        self.clock = clock
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = parsed.port
        self.endpoint = f"https://{hostname}" + (f":{port}" if port is not None and port != 443 else "")
        self._log_lock = threading.Lock()

    def _log_attempt(self, attempt, ok, status, error):
        entry = {
            "timestamp": int(self.clock()),
            "endpoint": self.endpoint,
            "attempt": attempt,
            "ok": ok,
            "status": status,
            "error": error,
        }
        line = json.dumps(entry, separators=(",", ":"), ensure_ascii=True) + "\n"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_lock, self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def send(self, alert):
        body = _body(alert)
        try:
            secret = self.secret_path.read_bytes().strip()
        except OSError:
            return {"ok": False, "attempts": 0, "error": "secret_unavailable"}
        if not secret:
            return {"ok": False, "attempts": 0, "error": "secret_unavailable"}
        if len(secret) < MIN_SECRET_BYTES:
            return {"ok": False, "attempts": 0, "error": "secret_too_short"}

        timestamp = int(self.clock())
        request = Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-ShieldBot-Signature": _signature(timestamp, body, secret),
            },
        )
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = None
            status = None
            error = None
            retry_after = None
            try:
                response = self.opener.open(request, timeout=TIMEOUT_S)
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if not 200 <= status < 300:
                    headers = getattr(response, "headers", None)
                    retry_after = _retry_after(headers, self.clock())
                    raise HTTPError(self.url, status, "webhook returned a non-success status", headers, None)
            except Exception as exception:
                status = getattr(exception, "code", status)
                error = type(exception).__name__
                if retry_after is None:
                    retry_after = _retry_after(getattr(exception, "headers", None), self.clock())
            finally:
                if response is not None:
                    response.close()

            ok = error is None
            self._log_attempt(attempt, ok, status, error)
            if ok:
                return {"ok": True, "attempts": attempt, "status": status}
            if not _retryable(status):
                return {"ok": False, "attempts": attempt, "status": status, "error": error}
            if attempt < MAX_ATTEMPTS:
                delay = min(retry_after, MAX_RETRY_AFTER_S) if retry_after is not None else 2 ** (attempt - 1)
                self.backoff(delay)
        return {"ok": False, "attempts": MAX_ATTEMPTS, "status": status, "error": error}


class StdoutSink:
    def __init__(self, stream=None):
        self.stream = stream

    def send(self, alert):
        line = json.dumps(asdict(alert), ensure_ascii=False, separators=(",", ":"))
        with _STDOUT_LOCK:
            print(line, file=self.stream if self.stream is not None else sys.stdout)
        return {"ok": True, "attempts": 1}


def send(alert, sinks):
    results = []
    try:
        iterator = iter(sinks)
    except Exception as error:
        return [{"sink": "sinks", "ok": False, "attempts": 0, "error": type(error).__name__}]
    while True:
        try:
            sink = next(iterator)
        except StopIteration:
            return results
        except Exception as error:
            results.append({"sink": "sinks", "ok": False, "attempts": 0, "error": type(error).__name__})
            return results
        name = type(sink).__name__
        try:
            outcome = sink.send(alert)
            if not isinstance(outcome, dict):
                raise TypeError("sink result must be a dictionary")
            results.append({**outcome, "sink": name})
        except Exception as error:
            results.append({"sink": name, "ok": False, "attempts": 0, "error": type(error).__name__})
