import copy
import dataclasses
import http.client
import io
import time
import unittest
import urllib.error
from email.message import Message
from unittest.mock import Mock, patch
from urllib.response import addinfourl

from enclave import labels as enclave_labels
from gate import rpc
from intel import labels
from tests.test_labels import base_fields, raw


class DeltaFetchTest(unittest.TestCase):
    def response(self, body, *, status=200, location=None, length=None):
        headers = Message()
        if location is not None:
            headers["Location"] = location
        if length is not None:
            headers["Content-Length"] = str(length)
        response = addinfourl(io.BytesIO(body), headers, "https://labels.example/delta", status)
        response.msg = "test response"
        return response

    def test_exact_limit_is_accepted(self):
        body = b" " * enclave_labels.MAX_DELTA_BYTES
        with patch.object(rpc._DeadlineHTTPSHandler, "https_open", return_value=self.response(body)):
            self.assertEqual(enclave_labels.fetch_delta("https://labels.example/delta"), body)

    def test_oversized_body_is_refused_without_relying_on_length(self):
        for length in (None, enclave_labels.MAX_DELTA_BYTES + 1):
            response = self.response(b" " * (enclave_labels.MAX_DELTA_BYTES + 1), length=length)
            with self.subTest(length=length), patch.object(rpc._DeadlineHTTPSHandler, "https_open",
                                                          return_value=response):
                with self.assertRaisesRegex(ValueError, "too large"):
                    enclave_labels.fetch_delta("https://labels.example/delta")

    def test_chunked_oversized_body_is_refused(self):
        body = b" " * (enclave_labels.MAX_DELTA_BYTES + 1)
        wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                + f"{len(body):x}\r\n".encode("ascii") + body + b"\r\n0\r\n\r\n")
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(wire)
        response = http.client.HTTPResponse(connection)
        response.begin()
        response.url = "https://labels.example/delta"
        with patch.object(rpc._DeadlineHTTPSHandler, "https_open", return_value=response):
            with self.assertRaisesRegex(ValueError, "too large"):
                enclave_labels.fetch_delta("https://labels.example/delta")

    def test_read_is_bounded_and_uses_the_existing_deadline(self):
        response = self.response(b"{}")
        read = response.read
        with rpc.request_deadline(0.5):
            deadline = rpc._REQUEST_DEADLINE.get()

            def checked_read(size):
                self.assertEqual(size, enclave_labels.MAX_DELTA_BYTES + 1)
                self.assertEqual(rpc._REQUEST_DEADLINE.get(), deadline)
                return read(size)

            with patch.object(rpc._DeadlineHTTPSHandler, "https_open", return_value=response), patch.object(
                    response, "read", side_effect=checked_read):
                self.assertEqual(enclave_labels.fetch_delta("https://labels.example/delta"), b"{}")
            self.assertEqual(rpc._REQUEST_DEADLINE.get(), deadline)

    def test_fetch_installs_total_deadline_through_body_read(self):
        response = self.response(b"{}")
        read = response.read
        started = time.monotonic()

        def checked_read(size):
            self.assertLessEqual(rpc._REQUEST_DEADLINE.get(), time.monotonic() + enclave_labels.DELTA_TIMEOUT_S)
            self.assertGreaterEqual(rpc._REQUEST_DEADLINE.get(), started + enclave_labels.DELTA_TIMEOUT_S)
            return read(size)

        with patch.object(rpc._DeadlineHTTPSHandler, "https_open", return_value=response), patch.object(
                response, "read", side_effect=checked_read):
            self.assertEqual(enclave_labels.fetch_delta("https://labels.example/delta"), b"{}")
        self.assertIsNone(rpc._REQUEST_DEADLINE.get())

    def test_all_redirects_are_refused_without_a_second_request(self):
        for status in (301, 302, 303, 307, 308):
            for target in ("https://labels.example/other", "https://other.example/delta",
                           "http://labels.example/delta"):
                response = self.response(b"", status=status, location=target)
                with self.subTest(status=status, target=target), patch.object(
                        rpc._DeadlineHTTPSHandler, "https_open", return_value=response) as opened:
                    with self.assertRaises(ValueError):
                        enclave_labels.fetch_delta("https://labels.example/delta")
                    self.assertEqual(opened.call_count, 1)
                self.assertTrue(response.closed)

    def test_only_measured_https_url_is_accepted(self):
        urls = ("http://labels.example/delta", "file:///delta", "https:///delta",
                "https://user:password@labels.example/delta", "https://labels.example/delta?q=x",
                "https://labels.example/delta#fragment")
        with patch.object(rpc._DeadlineHTTPSHandler, "https_open") as opened:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    enclave_labels.fetch_delta(url)
            opened.assert_not_called()

    def test_non_success_and_transport_errors_are_refused(self):
        with patch.object(rpc._DeadlineHTTPSHandler, "https_open", return_value=self.response(b"", status=204)):
            with self.assertRaises(ValueError):
                enclave_labels.fetch_delta("https://labels.example/delta")
        for error in (urllib.error.URLError("offline"), TimeoutError("late"),
                      http.client.IncompleteRead(b"cut"), http.client.BadStatusLine("bad")):
            with self.subTest(error=type(error).__name__), patch.object(
                    rpc._DeadlineHTTPSHandler, "https_open", side_effect=error):
                with self.assertRaises(ValueError):
                    enclave_labels.fetch_delta("https://labels.example/delta")

    def test_truncated_length_is_refused(self):
        with patch.object(rpc._DeadlineHTTPSHandler, "https_open",
                          return_value=self.response(b"{}", length=20)):
            with self.assertRaisesRegex(ValueError, "truncated"):
                enclave_labels.fetch_delta("https://labels.example/delta")

    def test_invalid_or_duplicate_length_is_refused(self):
        for length in ("bad", "-1", "999999999999999"):
            with self.subTest(length=length), patch.object(rpc._DeadlineHTTPSHandler, "https_open",
                                                          return_value=self.response(b"{}", length=length)):
                with self.assertRaises(ValueError):
                    enclave_labels.fetch_delta("https://labels.example/delta")
        response = self.response(b"{}", length=2)
        response.headers["Content-Length"] = "2"
        with patch.object(rpc._DeadlineHTTPSHandler, "https_open", return_value=response):
            with self.assertRaises(ValueError):
                enclave_labels.fetch_delta("https://labels.example/delta")

    def test_body_read_failure_closes_response(self):
        for error in (TimeoutError("late"), http.client.IncompleteRead(b"cut")):
            response = self.response(b"{}")
            with self.subTest(error=type(error).__name__), patch.object(
                    rpc._DeadlineHTTPSHandler, "https_open", return_value=response), patch.object(
                    response, "read", side_effect=error):
                with self.assertRaises(ValueError):
                    enclave_labels.fetch_delta("https://labels.example/delta")
            self.assertTrue(response.closed)


class AddOnlyLabelTest(unittest.TestCase):
    def setUp(self):
        self.floor = labels.load()
        self.row = raw(base_fields(subject="vendor.near", category="denylist", provenance="third_party"))

    def test_addition_keeps_floor_and_enforces_threat(self):
        merged = enclave_labels.merge(self.floor, {"add": [self.row]})
        self.assertEqual(merged.assertions[:-1], self.floor.assertions)
        self.assertEqual(merged.by_subject("vendor.near", "near")[-1].category, "denylist")
        self.assertNotEqual(merged.sha256, self.floor.sha256)
        self.assertEqual(enclave_labels.merge(merged, {"add": []}).sha256, merged.sha256)

    def test_empty_delta_is_idempotent(self):
        self.assertEqual(enclave_labels.merge(self.floor, {"add": []}).sha256, self.floor.sha256)

    def test_duplicate_identical_addition_cannot_grow_store(self):
        first = enclave_labels.merge(self.floor, {"add": [self.row]})
        second = enclave_labels.merge(first, {"add": [self.row]})
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.assertions, second.assertions)

    def test_removal_or_unknown_operation_is_refused(self):
        for delta in ({"remove": []}, {"add": [], "remove": []}, {"replace": []}, [], None):
            with self.subTest(delta=delta), self.assertRaises(labels.LabelError):
                enclave_labels.merge(self.floor, delta)

    def test_relaxation_supersession_and_mutation_are_refused(self):
        non_threat = raw(base_fields(category="treasury"))
        superseded = {**self.row, "superseded_by": self.floor.assertions[0].id}
        mutation = dataclasses.asdict(self.floor.assertions[0])
        mutation["superseded_by"] = self.row["id"]
        for row in (non_threat, superseded, mutation):
            with self.subTest(category=row["category"]), self.assertRaises(labels.LabelError):
                enclave_labels.merge(self.floor, {"add": [row]})

    def test_inferred_threat_cannot_create_refusal_without_cited_incident(self):
        row = raw(base_fields(subject="vendor.near", category="denylist", provenance="inferred",
                              rule_id="test", source={"rule_id": "test", "inputs": [self.row["id"]]}))
        with self.assertRaises(labels.LabelError):
            enclave_labels.merge(self.floor, {"add": [row]})

    def test_bad_row_never_mutates_floor(self):
        original = copy.deepcopy(self.floor.assertions)
        with self.assertRaises(labels.LabelError):
            enclave_labels.merge(self.floor, {"add": [self.row, {"id": "bad"}]})
        self.assertEqual(self.floor.assertions, original)


if __name__ == "__main__":
    unittest.main()
