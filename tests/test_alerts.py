import hashlib
import hmac
import io
import json
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request
from unittest.mock import patch

import intel.alerts as alerts
from intel import __main__ as intel_main
from intel.alerts import Alert, StdoutSink, WebhookSink, send, verify


class FakeResponse:
    def __init__(self, status=204):
        self.status = status
        self.closed = False

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RedirectOpener:
    def __init__(self, handlers):
        self.handler = next(handler for handler in handlers if isinstance(handler, HTTPRedirectHandler))
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return self.handler.redirect_request(request, io.BytesIO(), 302, "Found", {},
                                             "https://other.example.test/next")


SECRET = b"test-webhook-secret-0123456789abcdef"


def sample_alert(detail="Receiver requires review"):
    return Alert(
        source="near-feed",
        severity="warn",
        subject="vendor.testnet",
        ref="block-17",
        title="New receiver activity",
        detail=detail,
        evidence_sha256=hashlib.sha256(b"alert test evidence").hexdigest(),
        created_ms=1700000000123,
    )


class AlertsTests(unittest.TestCase):
    def test_alert_has_a_dedupe_key(self):
        alert = sample_alert()
        self.assertEqual(alert.dedupe_key, ("near-feed", "vendor.testnet", "block-17"))

    def test_signature_format_and_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(SECRET)
            opener = FakeOpener()
            sink = WebhookSink(
                "https://alerts.example.test/hook?auth=query-secret",
                secret_path,
                log_path=Path(directory) / "delivery.jsonl",
                opener=opener,
                clock=lambda: 1700000000,
            )

            result = sink.send(sample_alert())

            self.assertTrue(result["ok"])
            request, timeout = opener.calls[0]
            self.assertEqual(timeout, 5)
            signature = next(value for key, value in request.header_items()
                             if key.lower() == "x-shieldbot-signature")
            body = request.data
            expected = hmac.new(SECRET, b"1700000000." + body, hashlib.sha256).hexdigest()
            self.assertEqual(signature, f"t=1700000000,v1={expected}")
            self.assertTrue(verify(signature, body, SECRET, 1700000000))

    def test_verify_rejects_a_replay_outside_the_window(self):
        body = b'{"event":"alert"}'
        secret = SECRET
        digest = hmac.new(secret, b"1700000000." + body, hashlib.sha256).hexdigest()
        signature = f"t=1700000000,v1={digest}"

        self.assertFalse(verify(signature, body, secret, 1700000301))
        self.assertFalse(verify(signature, body, secret, 1699999699))
        self.assertTrue(verify(signature, body, secret, 1700000300))

    def test_redirect_is_refused_by_the_opener_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(SECRET)
            fake_openers = []

            def build_fake_opener(*handlers):
                opener = RedirectOpener(handlers)
                fake_openers.append(opener)
                return opener

            with patch.object(alerts, "build_opener", side_effect=build_fake_opener):
                sink = WebhookSink(
                    "https://alerts.example.test/hook",
                    secret_path,
                    log_path=Path(directory) / "delivery.jsonl",
                    backoff=lambda delay: None,
                )
                result = sink.send(sample_alert())

            self.assertFalse(result["ok"])
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(len(fake_openers[0].calls), 1)

    def test_http_url_is_refused(self):
        with self.assertRaises(ValueError):
            WebhookSink("http://alerts.example.test/hook", "secret")

    def test_payload_cap_refuses_an_oversized_alert_at_construction(self):
        from dataclasses import replace

        self.assertLessEqual(len(alerts._body(sample_alert())), alerts.MAX_PAYLOAD_BYTES)
        with self.assertRaises(ValueError):
            sample_alert("x" * (16 * 1024))
        with self.assertRaises(ValueError):
            replace(sample_alert(), detail="x" * (16 * 1024))

    def test_a_forbidden_answer_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = FakeOpener([HTTPError("https://alerts.example.test/hook", 403, "Forbidden", {}, None)])
            delays = []
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(SECRET)
            sink = WebhookSink(
                "https://alerts.example.test/hook",
                secret_path,
                log_path=Path(directory) / "delivery.jsonl",
                opener=opener,
                backoff=delays.append,
            )

            result = sink.send(sample_alert())

            self.assertFalse(result["ok"])
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["status"], 403)
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(delays, [])

    def test_a_non_transport_failure_ends_delivery_after_one_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = FakeOpener([ValueError("opener misconfigured")])
            delays = []
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(SECRET)
            sink = WebhookSink(
                "https://alerts.example.test/hook",
                secret_path,
                log_path=Path(directory) / "delivery.jsonl",
                opener=opener,
                backoff=delays.append,
            )

            result = sink.send(sample_alert())

            self.assertFalse(result["ok"])
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["error"], "ValueError")
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(delays, [])

    def test_a_refused_certificate_ends_delivery_after_one_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = FakeOpener([
                URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
            ])
            delays = []
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(SECRET)
            sink = WebhookSink(
                "https://alerts.example.test/hook",
                secret_path,
                log_path=Path(directory) / "delivery.jsonl",
                opener=opener,
                backoff=delays.append,
            )

            result = sink.send(sample_alert())

            self.assertFalse(result["ok"])
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["error"], "URLError")
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(delays, [])

    def test_a_timeout_is_still_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = FakeOpener([TimeoutError("timed out"), FakeResponse(204)])
            delays = []
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(SECRET)
            sink = WebhookSink(
                "https://alerts.example.test/hook",
                secret_path,
                log_path=Path(directory) / "delivery.jsonl",
                opener=opener,
                backoff=delays.append,
            )

            result = sink.send(sample_alert())

            self.assertTrue(result["ok"])
            self.assertEqual(result["attempts"], 2)
            self.assertEqual(delays, [1])

    def test_a_rate_limit_is_retried_and_retry_after_is_honoured(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = FakeOpener([
                HTTPError("https://alerts.example.test/hook", 429, "Too Many Requests",
                          {"Retry-After": "7"}, None),
                FakeResponse(204),
            ])
            delays = []
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(SECRET)
            sink = WebhookSink(
                "https://alerts.example.test/hook",
                secret_path,
                log_path=Path(directory) / "delivery.jsonl",
                opener=opener,
                backoff=delays.append,
                clock=lambda: 1700000000,
            )

            result = sink.send(sample_alert())

            self.assertTrue(result["ok"])
            self.assertEqual(result["attempts"], 2)
            self.assertEqual(delays, [7.0])

    def test_a_server_fault_is_retried_with_plain_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = FakeOpener([
                HTTPError("https://alerts.example.test/hook", 503, "Unavailable", {}, None),
                HTTPError("https://alerts.example.test/hook", 500, "Server Error", {}, None),
                FakeResponse(202),
            ])
            delays = []
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(SECRET)
            sink = WebhookSink(
                "https://alerts.example.test/hook",
                secret_path,
                log_path=Path(directory) / "delivery.jsonl",
                opener=opener,
                backoff=delays.append,
                clock=lambda: 1700000000,
            )

            result = sink.send(sample_alert())

            self.assertTrue(result["ok"])
            self.assertEqual(result["attempts"], 3)
            self.assertEqual(delays, [1, 2])

    def test_a_short_secret_is_refused_before_any_request(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = FakeOpener()
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(b"one byte secret")
            sink = WebhookSink(
                "https://alerts.example.test/hook",
                secret_path,
                log_path=Path(directory) / "delivery.jsonl",
                opener=opener,
            )

            result = sink.send(sample_alert())

            self.assertFalse(result["ok"])
            self.assertEqual(result["attempts"], 0)
            self.assertEqual(result["error"], "secret_too_short")
            self.assertEqual(opener.calls, [])

    def test_an_alert_carries_a_usable_evidence_hash_and_named_fields(self):
        from dataclasses import replace

        for field, value in (
            ("evidence_sha256", "not-a-hash"),
            ("evidence_sha256", "A" * 64),
            ("source", ""),
            ("subject", "  "),
            ("ref", ""),
            ("title", ""),
            ("created_ms", -5),
            ("created_ms", True),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    replace(sample_alert(), **{field: value})

    def test_retries_are_capped_and_backoff_is_injectable(self):
        with tempfile.TemporaryDirectory() as directory:
            opener = FakeOpener([URLError("offline"), URLError("offline"), URLError("offline")])
            delays = []
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(SECRET)
            sink = WebhookSink(
                "https://alerts.example.test/hook",
                secret_path,
                log_path=Path(directory) / "delivery.jsonl",
                opener=opener,
                backoff=delays.append,
            )

            result = sink.send(sample_alert())

            self.assertFalse(result["ok"])
            self.assertEqual(result["attempts"], 3)
            self.assertEqual(len(opener.calls), 3)
            self.assertEqual(delays, [1, 2])
            self.assertEqual(len((Path(directory) / "delivery.jsonl").read_text().splitlines()), 3)

    def test_secret_and_full_url_query_are_absent_from_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "delivery.jsonl"
            secret_path = Path(directory) / "webhook-secret"
            secret_path.write_bytes(b"private-test-secret-0123456789abcdef")
            sink = WebhookSink(
                "https://alerts.example.test/hook?token=query-secret",
                secret_path,
                log_path=log_path,
                opener=FakeOpener(),
                clock=lambda: 1700000000,
            )

            result = sink.send(sample_alert())
            log = log_path.read_text(encoding="utf-8")
            entry = json.loads(log)

            self.assertTrue(result["ok"])
            self.assertEqual(entry["attempt"], 1)
            self.assertNotIn("private-test-secret", log)
            self.assertNotIn("query-secret", log)
            self.assertNotIn("?token=", log)

    def test_send_never_raises_for_a_sink_failure(self):
        class BrokenSink:
            def send(self, alert):
                raise RuntimeError("sink failed")

        result = send(sample_alert(), [BrokenSink()])

        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["ok"])

    def test_configured_sinks_are_off_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(alerts, "WebhookSink") as webhook_sink, \
                    patch.object(alerts, "StdoutSink") as stdout_sink:
                self.assertEqual(alerts.configured_sinks(), [])

        webhook_sink.assert_not_called()
        stdout_sink.assert_not_called()

    def test_stdout_flag_configures_the_stdout_sink(self):
        import contextlib

        output = io.StringIO()
        with patch.dict(os.environ, {"SHIELDBOT_ALERT_STDOUT": "1"}, clear=True):
            with contextlib.redirect_stdout(output):
                result = send(sample_alert(), alerts.configured_sinks())

        self.assertTrue(result[0]["ok"])
        self.assertEqual(json.loads(output.getvalue())["subject"], "vendor.testnet")

    def test_alert_test_cli_sends_one_alert_through_the_webhook_sink(self):
        import contextlib

        output = io.StringIO()
        with patch.dict(os.environ, {"SHIELDBOT_ALERT_SECRET_FILE": "unused-test-path"}):
            with patch.object(intel_main.alerts, "WebhookSink") as webhook_sink:
                with patch.object(intel_main.alerts, "send", return_value=[{"ok": True}]) as send_alert:
                    with contextlib.redirect_stdout(output):
                        result = intel_main.main([
                            "alert-test",
                            "--url",
                            "https://alerts.example.test/hook",
                        ])

        self.assertEqual(result, 0)
        webhook_sink.assert_called_once_with("https://alerts.example.test/hook", "unused-test-path")
        alert, sinks = send_alert.call_args.args
        self.assertEqual(alert.subject, "alert-test")
        self.assertEqual(sinks, [webhook_sink.return_value])
        self.assertEqual(output.getvalue(), "test alert sent\n")

    def test_stdout_sink_writes_one_json_alert(self):
        import contextlib

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = StdoutSink().send(sample_alert())

        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(output.getvalue())["subject"], "vendor.testnet")


class ConfiguredSinksTest(unittest.TestCase):
    def test_a_url_file_path_that_cannot_be_opened_gives_no_sink_and_no_error(self):
        for path in ("bad\x00path", "missing/does-not-exist.url"):
            with self.subTest(path=path):
                environ = {"SHIELDBOT_ALERT_URL_FILE": path, "SHIELDBOT_ALERT_SECRET_FILE": "secret"}
                self.assertEqual(alerts.configured_sinks(environ), [])

if __name__ == "__main__":
    unittest.main()
