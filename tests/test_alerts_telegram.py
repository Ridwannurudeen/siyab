import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from intel import __main__ as intel_main, alerts, feed, labels
from intel.delivery import TelegramClient, TelegramError
from tests.test_delivery import FakeClient, FakeOpener


CHAT = "-1001234567890"
FOOTER = "Unsigned advice. A signal is an observation, not a verdict. Unknown is never a pass."


class TelegramAlertsTest(unittest.TestCase):
    def setUp(self):
        self.now = 1700000000.0
        self.client = FakeClient()
        self.alert = alerts.Alert("feed", "warn", "wrap.near", "watch_account_key_added",
                                  "Key added", "A key was observed.", hashlib.sha256(b"evidence").hexdigest(),
                                  1700000000123)
        state = patch.object(alerts, "_TELEGRAM_USE", {"next_send": 0, "day": None, "count": 0}, create=True)
        state.start()
        self.addCleanup(state.stop)
        network = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def sink(self, client=None):
        return alerts.TelegramSink(CHAT, self.client if client is None else client, clock=lambda: self.now)

    def test_unset_or_invalid_chat_leaves_sinks_as_today(self):
        with patch("intel.delivery.configured_client", return_value=self.client) as factory:
            for chat in (None, "", "1234567890", "-10012345", "-100" + "1" * 14, CHAT + "\n"):
                with self.subTest(chat=chat):
                    env = {} if chat is None else {"SIYAB_TELEGRAM_BROADCAST_CHAT": chat}
                    self.assertEqual(alerts.configured_sinks(env), [])
                    sinks = alerts.configured_sinks({**env, "SHIELDBOT_ALERT_STDOUT": "1"})
                    self.assertEqual([type(sink).__name__ for sink in sinks], ["StdoutSink"])
            factory.assert_not_called()
            sinks = alerts.configured_sinks({"SIYAB_TELEGRAM_BROADCAST_CHAT": CHAT})
            self.assertEqual([type(sink).__name__ for sink in sinks], ["TelegramSink"])

    def test_username_chat_is_refused(self):
        with patch("intel.delivery.configured_client", return_value=self.client) as factory:
            self.assertEqual(alerts.configured_sinks({"SIYAB_TELEGRAM_BROADCAST_CHAT": "@siyabchannel"}), [])
            factory.assert_not_called()
        with self.assertRaises(ValueError):
            alerts.TelegramSink("@siyabchannel", self.client)

    def test_configured_sinks_survives_bad_token_path(self):
        with tempfile.TemporaryDirectory() as directory:
            url = Path(directory) / "hook.url"
            url.write_text("https://alerts.example.test/hook", encoding="utf-8")
            for error in (OSError, ValueError, RuntimeError):
                for settings, expected in (({"SHIELDBOT_ALERT_STDOUT": "1"}, "StdoutSink"),
                                           ({"SHIELDBOT_ALERT_URL_FILE": str(url),
                                             "SHIELDBOT_ALERT_SECRET_FILE": "unused-path"}, "WebhookSink")):
                    with self.subTest(error=error.__name__, sink=expected), \
                            patch("intel.delivery.configured_client", side_effect=error("bad token path")):
                        sinks = alerts.configured_sinks({**settings, "SIYAB_TELEGRAM_BROADCAST_CHAT": CHAT})
                        self.assertEqual([type(sink).__name__ for sink in sinks], [expected])

    def test_configured_sinks_uses_supplied_token_path(self):
        env = {"SIYAB_TELEGRAM_BROADCAST_CHAT": CHAT, "SIYAB_TELEGRAM_TOKEN_FILE": "supplied-token-path"}
        with patch.dict(os.environ, {"SIYAB_TELEGRAM_TOKEN_FILE": "ambient-token-path"}, clear=True), \
                patch("intel.delivery.Path", wraps=Path) as paths:
            sinks = alerts.configured_sinks(env)
        self.assertEqual([type(sink).__name__ for sink in sinks], ["TelegramSink"])
        paths.assert_called_once_with("supplied-token-path")

    def test_replay_walk_never_reaches_telegram_sink(self):
        root = Path(__file__).resolve().parent.parent
        block = json.loads((root / "fixtures" / "feed" / "blocks" / "synthetic-watch-addkey.json").read_text(encoding="utf-8"))
        context = feed.Context(labels.load())
        env = {"SIYAB_TELEGRAM_BROADCAST_CHAT": CHAT, "SHIELDBOT_ALERT_STDOUT": "1"}
        for mode in ("replay", "record", None, "follow"):
            source = {"url": "test"} if mode is None else {"url": "test", "mode": mode}
            output = io.StringIO()
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, env, clear=True), \
                    patch("intel.delivery.configured_client", return_value=self.client), \
                    patch.object(alerts, "_TELEGRAM_USE", {"next_send": 0, "day": None, "count": 0}), \
                    contextlib.redirect_stdout(output):
                paths = feed.apply(block, feed.new_state(), context, source, events_dir=Path(directory) / "events")
                self.assertEqual(len(paths), 1)
                self.assertEqual(json.loads(output.getvalue())["ref"], "watch_account_key_added")
                self.assertEqual(len(self.client.calls), 1 if mode == "follow" else 0)
            self.client.calls.clear()

    def test_webhook_and_telegram_sinks_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            url = Path(directory) / "hook.url"
            url.write_text("https://alerts.example.test/hook", encoding="utf-8")
            env = {"SIYAB_TELEGRAM_BROADCAST_CHAT": CHAT, "SHIELDBOT_ALERT_URL_FILE": str(url),
                   "SHIELDBOT_ALERT_SECRET_FILE": "unused-path", "SHIELDBOT_ALERT_STDOUT": "1"}
            with patch("intel.delivery.configured_client", return_value=self.client):
                sinks = alerts.configured_sinks(env)
                self.assertEqual([type(sink).__name__ for sink in sinks], ["WebhookSink", "TelegramSink"])
                sinks[1].clock = lambda: self.now
                with patch.object(sinks[0], "send", return_value={"ok": False, "error": "secret_unavailable"}):
                    results = alerts.send(self.alert, sinks)
                self.assertFalse(results[0]["ok"])
                self.assertEqual(results[0]["error"], "secret_unavailable")
                self.assertTrue(results[1]["ok"])
                self.now += 3
                self.client.send_results = [TelegramError("provider_403")]
                with patch.object(sinks[0], "send", return_value={"ok": True, "attempts": 1}):
                    results = alerts.send(self.alert, [sinks[1], sinks[0]])
                self.assertFalse(results[0]["ok"])
                self.assertEqual(results[0]["error"], "provider_403")
                self.assertTrue(results[1]["ok"])
                for content in ("", "http://alerts.example.test/hook", "https://one.test\nhttps://two.test"):
                    url.write_text(content, encoding="utf-8")
                    self.assertEqual([type(sink).__name__ for sink in alerts.configured_sinks(env)], ["TelegramSink"])
                url.unlink()
                self.assertEqual([type(sink).__name__ for sink in alerts.configured_sinks(env)], ["TelegramSink"])
                env.pop("SHIELDBOT_ALERT_URL_FILE")
                self.assertEqual([type(sink).__name__ for sink in alerts.configured_sinks(env)], ["StdoutSink", "TelegramSink"])

    def test_payment_alert_is_never_broadcast(self):
        sink = self.sink()
        for source in ("payment", "siyab-cli", "near-feed"):
            self.assertEqual(sink.send(replace(self.alert, source=source)),
                             {"ok": True, "attempts": 0, "skipped": "source"})
        self.assertEqual(self.client.calls, [])
        self.assertTrue(sink.send(self.alert)["ok"])

    def test_feed_message_fields_entity_and_footer(self):
        detail = "https://example.test @someone 😀 " + "x" * 650
        alert = replace(self.alert, detail=detail)
        self.assertEqual(self.sink().send(alert), {"ok": True, "attempts": 1})
        method, payload = self.client.calls[0]
        head = ("Siyab feed signal: watch_account_key_added\nAccount: wrap.near\n" + detail[:600]
                + "\nObserved UTC: 2023-11-14T22:13:20.123Z\nEvidence: " + alert.evidence_sha256)
        self.assertEqual(method, "sendMessage")
        self.assertEqual(payload, {"chat_id": int(CHAT), "text": head + "\nhttps://siyab.xyz/feed.html\n" + FOOTER,
                                   "entities": [{"type": "pre", "offset": 0, "length": len(head.encode("utf-16-le")) // 2}]})

    def test_one_attempt_and_429_sets_cooldown(self):
        self.client.send_results = [TelegramError("provider_429", retryable=True, retry_after=30)]
        self.assertEqual(self.sink().send(self.alert), {"ok": False, "attempts": 1, "error": "provider_429"})
        self.now += 29
        self.assertEqual(self.sink().send(self.alert), {"ok": False, "attempts": 0, "error": "broadcast_cooldown"})
        self.assertEqual(len(self.client.calls), 1)
        self.now += 1
        self.assertTrue(self.sink().send(self.alert)["ok"])
        self.now += 3
        self.client.send_results = [TelegramError("provider_503", retryable=True)]
        self.assertEqual(self.sink().send(self.alert), {"ok": False, "attempts": 1, "error": "provider_503"})
        self.assertEqual(len(self.client.calls), 3)

        self.now += 3
        token = "123456:" + "a" * 35
        opener = FakeOpener({"ok": False, "error_code": 429, "parameters": {"retry_after": 15}})
        client = TelegramClient(lambda: token, opener=opener)
        with patch.object(opener, "open", wraps=opener.open) as request:
            self.assertEqual(self.sink(client).send(self.alert), {"ok": False, "attempts": 1, "error": "provider_429"})
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs, {"timeout": 15})

    def test_interval_and_daily_cap_refuse_without_sending(self):
        self.assertTrue(self.sink().send(self.alert)["ok"])
        self.now += 2.999
        self.assertEqual(self.sink().send(self.alert), {"ok": False, "attempts": 0, "error": "broadcast_cooldown"})
        self.assertEqual(len(self.client.calls), 1)
        self.now = 1700000003.0
        for _ in range(199):
            self.assertTrue(self.sink().send(self.alert)["ok"])
            self.now += 3
        self.assertEqual(self.sink().send(self.alert), {"ok": False, "attempts": 0, "error": "broadcast_budget"})
        self.assertEqual(len(self.client.calls), 200)
        self.now = (int(self.now) // 86400 + 1) * 86400
        self.assertTrue(self.sink().send(self.alert)["ok"])

    def test_wrong_chat_in_reply_is_a_failure(self):
        for response in ({"message_id": 42, "chat": {"id": -1009999999999}}, {}, [],
                         {"message_id": 42, "chat": {"id": CHAT}},
                         {"message_id": 42, "chat": {"id": -1001234567890.0}},
                         {"message_id": 42, "chat": {"id": True}},
                         {"message_id": 42.0, "chat": {"id": int(CHAT)}},
                         {"message_id": "42", "chat": {"id": int(CHAT)}},
                         {"message_id": True, "chat": {"id": int(CHAT)}}):
            with self.subTest(response=response):
                self.now += 3
                self.client.send_results = [response]
                self.assertEqual(self.sink().send(self.alert), {"ok": False, "attempts": 1, "error": "response_unreadable"})

    def test_results_never_carry_the_token(self):
        token = "123456:" + "a" * 35
        for response in ({"ok": False, "error_code": 429, "description": token}, RuntimeError(token)):
            client = TelegramClient(lambda: token, opener=FakeOpener(response))
            with self.assertLogs("intel.alerts", level="WARNING") as logs:
                result = alerts.send(self.alert, [self.sink(client)])
            self.assertFalse(result[0]["ok"])
            self.assertNotIn(token, json.dumps(result) + "".join(logs.output))
            self.now += 3
        for client, error in ((TelegramClient(), "credentials_missing"),
                              (TelegramClient(lambda: "invalid"), "credentials_invalid")):
            self.assertEqual(self.sink(client).send(self.alert), {"ok": False, "attempts": 0, "error": error})
        def unreadable():
            raise OSError(token)
        self.assertEqual(self.sink(TelegramClient(unreadable)).send(self.alert),
                         {"ok": False, "attempts": 0, "error": "credentials_unavailable"})

    def test_incident_broadcast_sends_once_and_fails_closed(self):
        root = Path(__file__).resolve().parent.parent
        page = (root / "intel" / "incidents" / "rhea.json").read_bytes()
        incident = json.loads(page)
        sink_type = alerts.TelegramSink
        env = {"SIYAB_TELEGRAM_BROADCAST_CHAT": CHAT, "SHIELDBOT_ALERT_STDOUT": "1"}
        with patch.dict(os.environ, env, clear=True), \
                patch("intel.delivery.configured_client", return_value=self.client), \
                patch.object(intel_main.time, "time", side_effect=lambda: self.now), \
                patch.object(alerts, "TelegramSink", side_effect=lambda chat, client: sink_type(chat, client, clock=lambda: self.now)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(intel_main.main(["incident-broadcast", "rhea"]), 0)
            self.assertEqual(len(self.client.calls), 1)
            message = self.client.calls[0][1]["text"]
            self.assertTrue(message.startswith("Siyab incident: " + incident["title"] + "\n"))
            self.assertIn(incident["date"], message)
            self.assertIn(incident["sources"][0]["publisher"], message)
            self.assertIn(hashlib.sha256(page).hexdigest(), message)
            self.assertTrue(message.endswith("https://siyab.xyz/incidents/rhea.html\n" + FOOTER))
            self.now += 3
            self.assertEqual(intel_main.main(["incident-broadcast", "rhea"]), 0)
            self.assertEqual(len(self.client.calls), 2)
            self.now += 3
            self.client.send_results = [TelegramError("provider_403")]
            self.assertEqual(intel_main.main(["incident-broadcast", "rhea"]), 1)
            self.assertEqual(len(self.client.calls), 3)
            for identifier in ("../rhea", "Rhea", "x" * 65, "missing-incident"):
                self.assertEqual(intel_main.main(["incident-broadcast", identifier]), 1)
            self.assertEqual(len(self.client.calls), 3)
            for chat in ("", "@siyabchannel"):
                os.environ["SIYAB_TELEGRAM_BROADCAST_CHAT"] = chat
                self.assertEqual(intel_main.main(["incident-broadcast", "rhea"]), 2)
            os.environ["SIYAB_TELEGRAM_BROADCAST_CHAT"] = CHAT
            with patch("intel.delivery.configured_client", return_value=TelegramClient()):
                self.assertEqual(intel_main.main(["incident-broadcast", "rhea"]), 2)
        self.assertEqual(len(self.client.calls), 3)

    def test_incident_broadcast_bad_token_path_returns_2(self):
        env = {"SIYAB_TELEGRAM_BROADCAST_CHAT": CHAT}
        for error in (OSError, ValueError, RuntimeError):
            output = io.StringIO()
            with self.subTest(error=error.__name__), patch.dict(os.environ, env, clear=True), \
                    patch("intel.delivery.configured_client", side_effect=error("bad token path")), \
                    contextlib.redirect_stderr(output):
                self.assertEqual(intel_main.main(["incident-broadcast", "rhea"]), 2)
                self.assertIn("unconfigured", output.getvalue())
                self.assertNotIn("bad token path", output.getvalue())
        self.assertEqual(self.client.calls, [])

    def test_incident_message_labels_posting_time(self):
        alert = replace(self.alert, source="incident", ref="rhea", detail="2026-04-16; recorded source")
        self.assertTrue(self.sink().send(alert)["ok"])
        message = self.client.calls[0][1]["text"]
        self.assertIn("2026-04-16; recorded source", message)
        self.assertIn("Posted UTC: 2023-11-14T22:13:20.123Z", message)
        self.assertNotIn("Observed UTC:", message)


if __name__ == "__main__":
    unittest.main()
