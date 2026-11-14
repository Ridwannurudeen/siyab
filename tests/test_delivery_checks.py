import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from gate import rpc
from intel import delivery_checks, labels
from intel.delivery import TelegramDelivery, TelegramError
from intel.delivery_checks import BUSY, DAILY_LIMIT, HELP, TRANSACTION_NOTE, UNSIGNED, WORDING, TelegramChecks, parse, reply
from intel.workspace import SQLiteWorkspaceStore
from tests.test_delivery import FakeClient

ROOT = Path(__file__).resolve().parent.parent
FLAGS = {"SIYAB_CHECKS": "1", "SIYAB_TELEGRAM_CHECKS": "1"}
TX_HASH = "AtuTsBkuouMPd7jBmKVusKMt8uPWKymUKVpgrS43Ax5q"
RECORDED = ROOT / "fixtures/issuers/0e842edffd5a07ad0c9d66bc5b386d96ca836417eab18956a2cdefbb0f7c064f.json"
EVIDENCE_HASH = "ab" * 32


def answer(outcome="ALLOW", providers=("https://free.rpc.fastnear.com", "label store", "label store"), block=4,
           reasons=("first", "second", "third", "fourth")):
    document = {"checks": [{"id": f"c{index}", "status": "pass", "detail": "d",
                            "source": {"provider": provider, "request_sha256": None, "fetched_at": None}}
                           for index, provider in enumerate(providers)]}
    return {"outcome": outcome, "reasons": list(reasons), "evidence": json.dumps(document),
            "evidence_hash": EVIDENCE_HASH, "freshness": {"block_height": block}, "signing": "never", "verdict": None}


def recorded_transaction():
    """The signer, receiver and actions of the recorded mainnet transaction TX_HASH names."""
    [item] = [item["transaction"] for item in json.loads(RECORDED.read_text(encoding="utf-8"))["response"]["transactions"]
              if item["transaction"]["hash"] == TX_HASH]
    return {field: item[field] for field in ("signer_id", "receiver_id", "actions")}


def message(text, update_id=1, chat_id=555, chat_type="private", sender_id=None, is_bot=False):
    return {"update_id": update_id, "message": {"text": text, "chat": {"id": chat_id, "type": chat_type},
                                                "from": {"id": chat_id if sender_id is None else sender_id,
                                                         "is_bot": is_bot}}}


class DeliveryChecksTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1700000000.0
        self.store = SQLiteWorkspaceStore(Path(self.temp.name) / "workspace.sqlite3", clock=lambda: self.now)
        self.client = FakeClient()
        self.live = Mock()
        self.live.store = labels.load()
        self.live.handle.return_value = (200, answer(), {})
        self.live._rate_limit.return_value = None
        self.checks = TelegramChecks(self.store, self.live)
        flags = patch.dict(os.environ, FLAGS)
        flags.start()
        self.addCleanup(flags.stop)
        # The transaction path catches every exception, so a blocked read would read Not enough data; each test
        # also fails afterwards if anything tried the network.
        for target in ("gate.rpc.Client.post", "gate.rpc.Client.get", "gate.rpc._open_with_deadline", "urllib.request.urlopen"):
            blocked = patch(target, side_effect=AssertionError("checks tests must not open the network"))
            opened = blocked.start()
            self.addCleanup(blocked.stop)
            self.addCleanup(lambda opened=opened, target=target: self.assertFalse(opened.called, target))

    def sends(self):
        return [payload for method, payload in self.client.calls if method == "sendMessage"]

    def texts(self):
        return [payload["text"] for payload in self.sends()]

    def answer(self, *updates):
        self.checks.answer(self.client, list(updates), lambda: None)

    def test_flags_off_or_non_private_sender_send_nothing(self):
        for flags in ({}, {"SIYAB_CHECKS": "1"}, {"SIYAB_TELEGRAM_CHECKS": "1"}):
            with self.subTest(flags=flags), patch.dict(os.environ, {"SIYAB_CHECKS": "", "SIYAB_TELEGRAM_CHECKS": ""}):
                os.environ.update(flags)
                self.answer(message("wrap.near"))
        self.answer(message("wrap.near", chat_type="group"), message("wrap.near", chat_id=556, sender_id=557),
                    message("wrap.near", chat_id=558, is_bot=True), message("wrap.near", chat_id=0),
                    {"update_id": 9, "edited_message": message("wrap.near")["message"]},
                    message("/start " + "a" * 64, chat_id=559))
        self.assertEqual(self.sends(), [])
        self.live.handle.assert_not_called()
        delivery = TelegramDelivery(self.store, self.client, bot_username="siyabonnearbot", clock=lambda: self.now,
                                    checks=self.checks)
        self.client.updates = [message("wrap.near")]
        with patch.dict(os.environ, {"SIYAB_TELEGRAM_CHECKS": ""}):
            delivery.run_once()
        self.assertEqual(self.sends(), [])

    def test_parse_kinds_and_help(self):
        cases = {"/account wrap.near": ("account", "wrap.near"), "/token usdt.tether-token.near": ("token", "usdt.tether-token.near"),
                 "/tx " + TX_HASH: ("transaction", TX_HASH), "  " + TX_HASH + "\n": ("transaction", TX_HASH),
                 "wrap.near": ("account", "wrap.near"), "a" * 64: ("account", "a" * 64),
                 "/account x.testnet": ("account", "x.testnet"), "x.testnet": ("help", None),
                 "hello": ("help", None), "/help": ("help", None), "/start": ("help", None),
                 "/tx wrap.near": ("help", None), "/account " + TX_HASH: ("help", None), "/account": ("help", None),
                 "/account wrap.near extra": ("help", None), "Wrap.near": ("help", None),
                 "/token https://example.com": ("help", None), "wrap.near " * 25 + "x" * 7: ("help", None),
                 "a." + "b" * 254: ("help", None)}
        for text, expected in cases.items():
            with self.subTest(text=text[:40]):
                self.assertEqual(parse(text), expected)

    def test_wording_matches_mcp_server_source(self):
        tree = ast.parse((ROOT / "mcp" / "server.py").read_text(encoding="utf-8"))
        values = [ast.literal_eval(node.value) for node in ast.walk(tree) if isinstance(node, ast.Assign)
                  and [getattr(target, "id", None) for target in node.targets] == ["wording"]]
        self.assertEqual(values, [WORDING])

    def test_reply_lists_sources_and_unsigned_note(self):
        lines = reply("account", "wrap.near", answer("DENY")).split("\n")
        self.assertEqual(lines, ["Siyab account check: wrap.near", "Result: Flagged", "Reasons: first; second; third",
                                 "Sources: https://free.rpc.fastnear.com, label store", "Block: 4",
                                 "Evidence: " + EVIDENCE_HASH, UNSIGNED])
        lines = reply("account", "wrap.near", answer(reasons=("a\nb", "c\r\n d"))).split("\n")
        self.assertEqual(lines[2], "Reasons: a b; c d")
        lines = reply("transaction", TX_HASH, answer("ASK", block=None)).split("\n")
        self.assertEqual(lines[1], "Result: Not enough data")
        self.assertEqual(lines[4], "Block: unknown")
        self.assertEqual(lines[-2:], [TRANSACTION_NOTE, UNSIGNED])
        long = reply("transaction", TX_HASH, answer(reasons=("x" * 5000, "y")))
        self.assertEqual(len(long), 3500)
        lines = long.split("\n")
        self.assertTrue(lines[2].startswith("Reasons: xxx"))
        self.assertEqual(lines[3:], ["Sources: https://free.rpc.fastnear.com, label store", "Block: 4",
                                     "Evidence: " + EVIDENCE_HASH, TRANSACTION_NOTE, UNSIGNED])
        refusal = reply("account", "wrap.near", "z" * 5000).split("\n")
        self.assertEqual((len("\n".join(refusal)), refusal[-1]), (3500, UNSIGNED))
        self.answer(message("wrap.near"))
        self.live.handle.assert_called_once_with("/live/account-check/wrap.near", "telegram:555", {})
        [payload] = self.sends()
        self.assertEqual(set(payload), {"chat_id", "text", "entities"})
        self.assertEqual(payload["chat_id"], 555)
        self.assertEqual(payload["text"], reply("account", "wrap.near", answer()))
        self.assertEqual(payload["entities"], [{"type": "pre", "offset": 0, "length": len(payload["text"])}])
        self.assertEqual(reply("account", "a.near", "\U0001f600"), "Siyab account check: a.near\n\U0001f600\n" + UNSIGNED)
        self.client.calls.clear()
        self.checks.answer(self.client, [message("\U0001f600 hi")], lambda: None)
        self.assertEqual(self.sends()[0]["entities"][0]["length"], len(HELP))

    def test_failures_reply_not_enough_data(self):
        not_enough = "Result: Not enough data"
        for status in (404, 500, 503, 504):
            with self.subTest(status=status):
                self.live.handle.return_value = (status, {"error": "x", "message": "upstream detail"}, {})
                self.assertEqual(self.checks._check("account", "wrap.near", 555).split("\n")[1], not_enough)
        for status in (400, 429):
            with self.subTest(status=status):
                self.live.handle.return_value = (status, {"error": "x", "message": "The stated reason."}, {})
                self.assertEqual(self.checks._check("token", "wrap.near", 555).split("\n")[1], "The stated reason.")
        for failure in (rpc.MalformedResponse("bad"), TimeoutError(), rpc.HttpError(500, "url"), OSError(),
                        rpc.BudgetExhausted("spent", 60), RuntimeError("unexpected")):
            with self.subTest(failure=type(failure).__name__), patch.object(delivery_checks, "fetch_transaction",
                                                                             side_effect=failure):
                self.assertEqual(self.checks._check("transaction", TX_HASH, 555).split("\n")[1], not_enough)
        self.live._rate_limit.assert_called_with("telegram:555", 4)
        with patch.object(delivery_checks, "fetch_transaction", return_value={"signer_id": "sender.near",
                                                                             "receiver_id": "x.testnet", "actions": ["Stake"]}):
            self.assertEqual(self.checks._check("transaction", TX_HASH, 555).split("\n")[1], not_enough)
        transfer = {"signer_id": "sender.near", "receiver_id": "recipient.near", "actions": [{"Transfer": {"deposit": "1"}}]}
        with patch.object(delivery_checks, "fetch_transaction", return_value=transfer) as fetch:
            text = self.checks._check("transaction", TX_HASH, 555)
        self.assertEqual(fetch.call_args.args, (TX_HASH,))
        self.assertIn("Result: No flag found", text)
        self.assertIn(TRANSACTION_NOTE, text)
        self.live._rate_limit.return_value = (429, "rate_limited", "The client rate limit was reached.", 30)
        with patch.object(delivery_checks, "fetch_transaction") as fetch:
            self.assertEqual(self.checks._check("transaction", TX_HASH, 555).split("\n")[1],
                             "The client rate limit was reached.")
            self.live._rate_limit.return_value = (503, "busy", "The live API request ceiling was reached.", 30)
            self.assertEqual(self.checks._check("transaction", TX_HASH, 555).split("\n")[1], not_enough)
        fetch.assert_not_called()

    def test_daily_caps_survive_restart(self):
        for update_id in range(1, 21):
            self.answer(message("wrap.near", update_id=update_id))
        self.assertEqual(self.live.handle.call_count, 20)
        checks = TelegramChecks(self.store, self.live)
        with patch.object(delivery_checks, "fetch_transaction", return_value=recorded_transaction()) as fetch:
            checks.answer(self.client, [message("wrap.near", update_id=21), message("/tx " + TX_HASH, update_id=22, chat_id=556)],
                          lambda: None)
        self.assertEqual(fetch.call_args.args, (TX_HASH,))
        self.assertIn("Result: Not enough data", self.texts()[-1])
        self.assertIn(TRANSACTION_NOTE, self.texts()[-1])
        self.assertEqual(self.live.handle.call_count, 20)
        self.assertEqual(self.texts()[-2].split("\n")[1], DAILY_LIMIT)
        self.assertNotIn(DAILY_LIMIT, self.texts()[-1])
        with patch.object(delivery_checks, "MAX_DAY_TOTAL", 22):
            checks.answer(self.client, [message("wrap.near", update_id=23, chat_id=557),
                                        message("wrap.near", update_id=24, chat_id=558)], lambda: None)
        self.assertEqual(self.live.handle.call_count, 21)
        self.assertEqual(self.texts()[-1].split("\n")[1], DAILY_LIMIT)
        self.now += 86400
        TelegramChecks(self.store, self.live).answer(self.client, [message("wrap.near", update_id=25)], lambda: None)
        self.assertEqual(self.live.handle.call_count, 22)
        with self.store._connection() as connection:
            rows = [tuple(row) for row in connection.execute("SELECT day,chat_id,count FROM telegram_checks")]
        self.assertEqual(rows, [("2023-11-15", 555, 1)])

    def test_two_checks_per_tick_then_busy(self):
        renewals = []
        updates = [message("wrap.near", update_id=1, chat_id=600), message("hello", update_id=2, chat_id=600),
                   message("hello", update_id=3, chat_id=601)]
        updates += [message("wrap.near", update_id=10 + index, chat_id=610 + index) for index in range(12)]
        self.checks.answer(self.client, list(reversed(updates)), lambda: renewals.append(1))
        sends = self.sends()
        self.assertEqual([payload["chat_id"] for payload in sends], [600, 601] + list(range(610, 618)))
        self.assertEqual(self.live.handle.call_count, 2)
        self.assertNotIn(BUSY, sends[0]["text"])
        self.assertEqual(sends[1]["text"], HELP)
        self.assertNotIn(BUSY, sends[2]["text"])
        self.assertEqual([payload["text"] for payload in sends[3:]], [BUSY] * 7)
        self.assertEqual(len(renewals), 12)

    def linked_delivery(self):
        owner = self.store.register("owner", "owner-password-123")["user"]["id"]
        team = self.store.list_teams(owner)[0]["id"]
        self.store.add_watch(owner, team, "wrap.near")
        delivery = TelegramDelivery(self.store, self.client, bot_username="siyabonnearbot", clock=lambda: self.now,
                                    checks=self.checks)
        link = delivery.begin_link(owner, team)
        self.client.updates = [message("/start " + link["link"].split("start=")[1])]
        delivery.run_once()
        delivery.confirm_link(owner, team, link["generation"])
        return delivery

    def alert(self, identifier):
        self.now += 1
        self.store.ingest_event(identifier * 64, {"signal": "account_transfer", "subject": "wrap.near", "detail": "d",
                                                  "observed": {"timestamp_ms": int(self.now * 1000), "block_height": 4}},
                                notify=True)

    def next_poll_ms(self):
        with self.store._connection() as connection:
            return connection.execute("SELECT next_poll_ms FROM delivery_state WHERE singleton=1").fetchone()[0]

    def test_alert_cooldown_skips_checks_and_is_never_shortened(self):
        delivery = self.linked_delivery()
        self.alert("a")
        self.client.updates = [message("wrap.near", update_id=2, chat_id=777)]
        self.client.send_results = [TelegramError("provider_429", retryable=True, retry_after=300)]
        result = delivery.run_once()
        self.assertEqual((result["sent"], result["failed"]), (0, 1))
        self.assertEqual([payload["chat_id"] for payload in self.sends()], [555])
        self.live.handle.assert_not_called()
        self.assertEqual(self.next_poll_ms(), int(self.now * 1000) + 300000)
        self.assertEqual(delivery.run_once()["last_error"], "provider_cooldown")
        self.now += 301
        self.client.updates = [message("wrap.near", update_id=3, chat_id=777)]
        delivery.run_once()
        self.assertEqual([payload["chat_id"] for payload in self.sends()], [555, 555, 777])

        def cooled_then_limited():
            with self.store._connection(write=True) as connection:
                connection.execute("UPDATE delivery_state SET next_poll_ms=? WHERE singleton=1", (int(self.now * 1000) + 300000,))
            raise TelegramError("provider_429", retryable=True, retry_after=5)

        with patch.object(delivery, "_enqueue", side_effect=cooled_then_limited):
            self.assertEqual(delivery.run_once()["last_error"], "provider_429")
        self.assertEqual(self.next_poll_ms(), int(self.now * 1000) + 300000)

    def test_reply_failure_never_stalls_alerts(self):
        delivery = self.linked_delivery()
        self.alert("a")
        self.client.updates = [message("wrap.near", update_id=2, chat_id=777)]
        self.client.send_results = [None, TelegramError("provider_400")]
        result = delivery.run_once()
        self.assertEqual((result["sent"], result["last_error"]), (1, None))
        self.assertEqual([payload["chat_id"] for payload in self.sends()], [555, 777])
        self.alert("b")
        self.client.updates = [message("wrap.near", update_id=3, chat_id=777), message("wrap.near", update_id=4, chat_id=778)]
        self.client.send_results = [None, TelegramError("provider_429", retryable=True, retry_after=30)]
        result = delivery.run_once()
        self.assertEqual((result["sent"], result["last_error"]), (1, "provider_429"))
        self.assertEqual([payload["chat_id"] for payload in self.sends()], [555, 777, 555, 777])
        self.assertEqual(delivery.run_once()["last_error"], "provider_cooldown")
        self.now += 31
        self.alert("c")
        result = delivery.run_once()
        self.assertEqual((result["sent"], result["last_error"]), (1, None))
        self.assertEqual([payload["chat_id"] for payload in self.sends()], [555, 777, 555, 777, 555])


if __name__ == "__main__":
    unittest.main()
