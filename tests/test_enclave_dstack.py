import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from enclave import dstack


class DstackTests(unittest.TestCase):
    def exchange(self, value=None, raw=None, delay=0):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("AF_UNIX unavailable")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / "d.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(path)
        except OSError:
            server.close()
            self.skipTest("AF_UNIX bind unavailable")
        server.listen(1)
        server.settimeout(2)
        self.addCleanup(server.close)
        requests = []
        errors = []
        if raw is None:
            body = json.dumps(value).encode()
            raw = (f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                   "Connection: close\r\n\r\n").encode() + body

        def serve():
            try:
                with server.accept()[0] as connection:
                    connection.settimeout(2)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        request += connection.recv(4096)
                    head, body = request.split(b"\r\n\r\n", 1)
                    length = int(next(line.split(b":", 1)[1] for line in head.split(b"\r\n")
                                      if line.startswith(b"Content-Length:")))
                    while len(body) < length:
                        body += connection.recv(4096)
                    requests.append((head, json.loads(body)))
                    if delay:
                        time.sleep(delay)
                    connection.sendall(raw)
            except (BrokenPipeError, ConnectionResetError):
                if not delay:
                    errors.append("connection closed before reply")
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def finish():
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

        self.addCleanup(finish)
        return dstack.DstackClient(path), requests

    def test_default_socket(self):
        self.assertEqual(dstack.DstackClient().socket_path, "/var/run/dstack.sock")

    def test_quote_preserves_exact_report_data_without_hashing(self):
        report = bytes(range(64))
        value = {"quote": "abcd", "report_data": report.hex(), "event_log": "[]", "vm_config": "{}"}
        client, requests = self.exchange(value)
        self.assertEqual(client.get_quote(report), value)
        self.assertTrue(requests[0][0].startswith(b"POST /GetQuote HTTP/1.1\r\n"))
        self.assertEqual(requests[0][1], {"report_data": report.hex()})

    def test_info_retains_original_tcb_json_and_compose_bytes(self):
        tcb = json.dumps({"app_compose": " { \"name\": \"siyab\" } ", "event_log": []}, indent=2)
        value = {"tcb_info": tcb, "app_id": "abcd"}
        client, requests = self.exchange(value)
        self.assertEqual(client.info(), value)
        self.assertTrue(requests[0][0].startswith(b"POST /Info HTTP/1.1\r\n"))
        self.assertEqual(requests[0][1], {})

    def test_invalid_report_data_refused_before_socket(self):
        for value in (None, "ab" * 64, b"a" * 63, b"a" * 65, bytearray(64)):
            with self.subTest(value=type(value)), self.assertRaises(dstack.DstackError):
                dstack.DstackClient("missing").get_quote(value)

    def test_malformed_info_refused(self):
        for value in ({}, {"tcb_info": {}}, {"tcb_info": "[]"}, {"tcb_info": "{"}, {"error": "private detail"}):
            with self.subTest(value=value):
                client, _ = self.exchange(value)
                with self.assertRaises(dstack.DstackError):
                    client.info()

    def test_malformed_quote_refused(self):
        valid = {"quote": "abcd", "report_data": "00" * 64, "event_log": "[]"}
        for field, value in (("quote", ""), ("quote", "ABCDEF"), ("quote", "abc"),
                             ("report_data", "01" * 64), ("event_log", []), ("event_log", "{}")):
            with self.subTest(field=field, value=value):
                client, _ = self.exchange({**valid, field: value})
                with self.assertRaises(dstack.DstackError):
                    client.get_quote(bytes(64))

    def test_http_and_json_errors_refused(self):
        for raw in (b"HTTP/1.1 500 Bad\r\nContent-Length: 2\r\n\r\n{}",
                    b"HTTP/1.1 302 Redirect\r\nContent-Length: 2\r\n\r\n{}",
                    b"invalid\r\n\r\n{}", b"HTTP/1.1 200 OK\r\n\r\n[]",
                    b"HTTP/1.1 200 OK\r\n\r\n{\"x\":1,\"x\":2}",
                    b"HTTP/1.1 200 OK\r\n\r\n{\"x\":NaN}",
                    b"HTTP/1.1 200 OK\r\n\r\n\xff",
                    b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n{}",
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}trailing",
                    b"HTTP/1.1 200 OK\r\nContent-Length: -2\r\n\r\n{}",
                    b"HTTP/1.1 200 OK\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n{}",
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}",
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n{}",
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\n{}",
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}"):
            with self.subTest(raw=raw):
                client, _ = self.exchange(raw=raw)
                with self.assertRaises(dstack.DstackError):
                    client.info()

    def test_chunked_response(self):
        body = b'{"tcb_info":"{}"}'
        raw = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
               + f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
        client, _ = self.exchange(raw=raw)
        self.assertEqual(client.info(), {"tcb_info": "{}"})

    def test_response_bound(self):
        client, _ = self.exchange({"tcb_info": "{}", "padding": "x" * 512})
        with patch.object(dstack, "MAX_RESPONSE", 128), self.assertRaises(dstack.DstackError):
            client.info()

    def test_header_bound(self):
        raw = b"HTTP/1.1 200 OK\r\nX-Padding: " + b"x" * 256 + b"\r\n\r\n{}"
        client, _ = self.exchange(raw=raw)
        with patch.object(dstack, "MAX_HEADERS", 128), self.assertRaises(dstack.DstackError):
            client.info()

    def test_timeout(self):
        client, _ = self.exchange({"tcb_info": "{}"}, delay=0.1)
        with patch.object(dstack, "TIMEOUT", 0.02), self.assertRaises(dstack.DstackError):
            client.info()

    def test_missing_socket_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(dstack.DstackError):
                dstack.DstackClient(str(Path(directory) / "missing.sock")).info()
