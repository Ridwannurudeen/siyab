"""Bounded access to the dstack guest's raw TDX quote and information API."""

import http.client
import io
import json
import re
import socket
import time
from types import SimpleNamespace


TIMEOUT = 10
MAX_RESPONSE = 2 * 1024 * 1024
MAX_HEADERS = 16 * 1024


class DstackError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DstackError("duplicate dstack JSON member")
        result[key] = value
    return result


def _constant(value):
    raise DstackError("invalid dstack JSON constant")


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_object,
                          parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DstackError("invalid dstack JSON") from error


class DstackClient:
    def __init__(self, socket_path="/var/run/dstack.sock"):
        self.socket_path = socket_path

    def _request(self, method, arguments):
        if not hasattr(socket, "AF_UNIX"):
            raise DstackError("dstack Unix sockets unavailable")
        body = json.dumps(arguments, separators=(",", ":")).encode("ascii")
        request = (f"POST /{method} HTTP/1.1\r\nHost: dstack\r\n"
                   "Content-Type: application/json\r\nConnection: close\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode("ascii") + body
        deadline = time.monotonic() + TIMEOUT
        raw = bytearray()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(TIMEOUT)
                connection.connect(self.socket_path)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DstackError("dstack timeout")
                connection.settimeout(remaining)
                connection.sendall(request)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DstackError("dstack timeout")
                    connection.settimeout(remaining)
                    chunk = connection.recv(min(65536, MAX_RESPONSE + MAX_HEADERS + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    header_end = raw.find(b"\r\n\r\n")
                    if (header_end < 0 and len(raw) > MAX_HEADERS) or header_end > MAX_HEADERS:
                        raise DstackError("dstack headers exceed bound")
                    if len(raw) > MAX_RESPONSE + MAX_HEADERS:
                        raise DstackError("dstack response exceeds bound")
            if raw.find(b"\r\n\r\n") < 0:
                raise DstackError("invalid dstack HTTP response")
            source = io.BytesIO(raw)
            response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda mode: source))
            response.begin()
            if not 200 <= response.status < 300:
                raise DstackError("dstack HTTP request refused")
            lengths = response.headers.get_all("Content-Length", [])
            transfers = response.headers.get_all("Transfer-Encoding", [])
            if (len(lengths) > 1 or len(transfers) > 1 or lengths and transfers
                    or lengths and not re.fullmatch(r"[0-9]+", lengths[0])
                    or transfers and transfers[0].lower() != "chunked"):
                raise DstackError("invalid dstack HTTP framing")
            if lengths and (len(lengths[0]) > len(str(MAX_RESPONSE)) or int(lengths[0]) > MAX_RESPONSE):
                raise DstackError("dstack response exceeds bound")
            payload = response.read(MAX_RESPONSE + 1)
            if len(payload) > MAX_RESPONSE:
                raise DstackError("dstack response exceeds bound")
            if lengths and (len(payload) != int(lengths[0])
                            or len(raw) - raw.find(b"\r\n\r\n") - 4 != int(lengths[0])):
                raise DstackError("truncated dstack response")
        except (OSError, http.client.HTTPException, OverflowError) as error:
            raise DstackError("dstack transport failed") from error
        result = _json(payload)
        if not isinstance(result, dict) or "error" in result:
            raise DstackError("invalid dstack response object")
        return result

    def info(self):
        result = self._request("Info", {})
        if not isinstance(result.get("tcb_info"), str) or not isinstance(_json(result["tcb_info"]), dict):
            raise DstackError("invalid dstack TCB information")
        return result

    def get_quote(self, report_data):
        if not isinstance(report_data, bytes) or len(report_data) != 64:
            raise DstackError("report data must be exactly 64 bytes")
        result = self._request("GetQuote", {"report_data": report_data.hex()})
        quote = result.get("quote")
        if (not isinstance(quote, str) or not re.fullmatch(r"(?:[0-9a-f]{2})+", quote)
                or result.get("report_data") != report_data.hex()
                or not isinstance(result.get("event_log"), str)
                or not isinstance(_json(result["event_log"]), list)):
            raise DstackError("invalid dstack quote response")
        return result
