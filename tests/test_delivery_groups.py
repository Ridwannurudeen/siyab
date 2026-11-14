import copy
import hashlib
import unittest
from email.message import Message
from unittest.mock import patch

from intel.delivery import LEASE_MS, LINK_MS, REQUEST_SECONDS, TelegramClient, TelegramDelivery, TelegramError
from intel.workspace import WorkspaceError
from intel.workspace_api import WorkspaceAPI
from tests import test_delivery


UPDATE_TYPES = ["message", "edited_message", "channel_post", "edited_channel_post", "business_connection", "business_message",
                "edited_business_message", "deleted_business_messages", "message_reaction", "message_reaction_count",
                "inline_query", "chosen_inline_result", "callback_query", "shipping_query", "pre_checkout_query",
                "purchased_paid_media", "poll", "poll_answer", "my_chat_member", "chat_member", "chat_join_request",
                "chat_boost", "removed_chat_boost"]


class FakeClient(test_delivery.FakeClient):
    def __init__(self):
        super().__init__()
        self.member_results = []
        self.chat_errors = {}
        self.update_errors = []

    def call(self, method, payload):
        if method == "sendMessage" and payload["chat_id"] in self.chat_errors:
            self.calls.append((method, payload))
            raise self.chat_errors.pop(payload["chat_id"])
        if method == "getUpdates" and self.update_errors:
            self.calls.append((method, payload))
            raise self.update_errors.pop(0)
        if method == "getChatMember":
            self.calls.append((method, payload))
            result = self.member_results.pop(0) if self.member_results else {
                "status": "administrator", "user": {"id": payload["user_id"], "is_bot": False}}
            if isinstance(result, BaseException):
                raise result
            return result
        return super().call(method, payload)


class DeliveryGroupsTest(unittest.TestCase):
    event = test_delivery.DeliveryTest.event
    sends = test_delivery.DeliveryTest.sends
    activate = test_delivery.DeliveryTest.activate
    start_update = test_delivery.DeliveryTest.start_update
    legacy_store = test_delivery.DeliveryTest.legacy_store

    def setUp(self):
        flag = patch.dict("os.environ", {"SIYAB_TELEGRAM_GROUPS": "1"})
        flag.start()
        self.addCleanup(flag.stop)
        test_delivery.DeliveryTest.setUp(self)
        self.client = FakeClient()
        self.delivery = TelegramDelivery(self.store, self.client, clock=lambda: self.now)
        self.update_id = 0

    def group_update(self, link, *, chat_id=-1001234, chat_type="supergroup", sender_id=555):
        self.update_id += 1
        return {"update_id": self.update_id, "message": {
            "text": "/start@siyabonnearbot " + link["link"].split("=", 1)[1],
            "chat": {"id": chat_id, "type": chat_type, "title": "Team alerts"},
            "from": {"id": sender_id, "is_bot": False, "username": "groupadmin"}}}

    def observe(self, link):
        self.client.updates = [self.group_update(link)]
        result = self.delivery.run_once()
        self.assertIsNone(result["last_error"])
        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "observed")

    def test_flag_off_refuses_group_link_and_private_flow_is_unchanged(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(WorkspaceError) as caught:
                self.delivery.begin_link(self.owner, self.team, "group")
            self.assertEqual((caught.exception.status, caught.exception.code), (404, "not_found"))
            status = self.delivery.status(self.owner, self.team)
            self.assertEqual((status["kind"], status["groups_enabled"]), ("private", False))
            self.assertFalse(status["verified"])
            self.activate()
            self.event()
            self.assertEqual(self.delivery.run_once()["sent"], 1)
            self.assertEqual(self.sends()[0]["chat_id"], 555)
        self.assertEqual([method for method, _ in self.client.calls if method == "getChatMember"], [])
        with patch.dict("os.environ", {"SIYAB_TELEGRAM_GROUPS": "true"}):
            with self.assertRaises(WorkspaceError) as caught:
                self.delivery.begin_link(self.owner, self.team, "group")
            self.assertEqual(caught.exception.status, 404)

    def test_refused_group_start_burns_the_nonce(self):
        for reason in ("wrong_kind", "private_chat", "channel", "missing_suffix", "wrong_suffix", "sender_chat", "bot", "missing_sender"):
            with self.subTest(reason=reason):
                kind = "private" if reason == "wrong_kind" else "group"
                link = self.delivery.begin_link(self.owner, self.team, kind)
                update = self.group_update(link)
                message = update["message"]
                if reason == "private_chat":
                    message["chat"] = {"id": 555, "type": "private"}
                elif reason == "channel":
                    message["chat"]["type"] = "channel"
                elif reason == "missing_suffix":
                    message["text"] = message["text"].replace("@siyabonnearbot", "")
                elif reason == "wrong_suffix":
                    message["text"] = message["text"].replace("@siyabonnearbot", "@anotherbot")
                elif reason == "sender_chat":
                    message["sender_chat"] = {"id": -1001234, "type": "supergroup"}
                elif reason == "bot":
                    message["from"]["is_bot"] = True
                elif reason == "missing_sender":
                    message.pop("from")
                valid = self.group_update(link, chat_id=555 if kind == "private" else -1001234, chat_type=kind)
                self.client.updates = [update, valid]
                self.delivery.run_once()
                status = self.delivery.status(self.owner, self.team)
                self.assertEqual((status["state"], status["verified"], status["observed_chat_label"]), ("expired", False, None))
                with self.store._connection() as connection:
                    binding = connection.execute("SELECT nonce_hash,nonce_expires_ms FROM delivery_bindings WHERE team_id=?", (self.team,)).fetchone()
                    self.assertIsNone(binding["nonce_hash"])
                    self.assertLessEqual(binding["nonce_expires_ms"], int(self.now * 1000))
                self.client.updates = [self.group_update(link, chat_id=555 if kind == "private" else -1001234, chat_type=kind)]
                self.delivery.run_once()
                self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "expired")
                with self.assertRaises(WorkspaceError) as caught:
                    self.delivery.confirm_link(self.owner, self.team, link["generation"])
                self.assertEqual(caught.exception.status, 409)

    def test_groups_flag_off_stops_group_confirm_verify_and_delivery(self):
        for phase in ("confirm", "verify", "delivery"):
            with self.subTest(phase=phase):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                self.observe(link)
                if phase != "verify":
                    self.delivery.run_once()
                if phase == "delivery":
                    self.delivery.confirm_link(self.owner, self.team, link["generation"])
                    self.event()
                    self.delivery._enqueue()
                calls = len(self.client.calls)
                with patch.dict("os.environ", {"SIYAB_TELEGRAM_GROUPS": "0"}):
                    if phase == "confirm":
                        with self.assertRaises(WorkspaceError) as caught:
                            self.delivery.confirm_link(self.owner, self.team, link["generation"])
                        self.assertEqual(caught.exception.status, 409)
                        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "observed")
                    elif phase == "verify":
                        self.delivery.run_once()
                        self.assertNotIn("getChatMember", [method for method, _ in self.client.calls[calls:]])
                        self.assertFalse(self.delivery.status(self.owner, self.team)["verified"])
                    else:
                        self.assertIsNone(self.delivery._claim())
                        self.assertEqual(self.delivery.run_once()["sent"], 0)
                        row = self.delivery.list_deliveries(self.owner, self.team)["items"][0]
                        self.assertEqual((row["state"], row["attempts"]), ("pending", 0))
                if phase == "delivery":
                    self.assertEqual(self.delivery.run_once()["sent"], 1)

    def test_trailing_whitespace_start_burns_the_nonce(self):
        for suffix in (" ", "\n", "\r\n", "\t"):
            with self.subTest(suffix=suffix):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                update = self.group_update(link)
                update["message"]["text"] += suffix
                self.client.updates = [update]
                self.delivery.run_once()
                status = self.delivery.status(self.owner, self.team)
                self.assertEqual((status["state"], status["verified"], status["observed_chat_label"]), ("expired", False, None))
                with self.store._connection() as connection:
                    binding = connection.execute("SELECT nonce_hash,nonce_expires_ms FROM delivery_bindings WHERE team_id=?", (self.team,)).fetchone()
                    self.assertIsNone(binding["nonce_hash"])
                    self.assertLessEqual(binding["nonce_expires_ms"], int(self.now * 1000))

    def test_refused_start_cannot_be_replayed_by_another_group_and_sender(self):
        for pattern in ("/start@siyabonnearbot {}\n", " /start@siyabonnearbot {}",
                        "/start@x {}", "/start@siyabonnearbot\t{}",
                        "/start@siyabonnearbot  {}", "/start@siyabonnearbot {} extra",
                        "/start@siyabonnearbot a{}"):
            with self.subTest(pattern=pattern):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                refused = self.group_update(link)
                refused["message"]["text"] = pattern.format(link["link"].split("=", 1)[1])
                self.client.updates = [refused]
                self.delivery.run_once()
                self.client.updates = [self.group_update(link, chat_id=-1009999, sender_id=777)]
                self.delivery.run_once()
                self.delivery = TelegramDelivery(self.store, self.client, clock=lambda: self.now)
                self.delivery.run_once()
                with self.assertRaises(WorkspaceError) as caught:
                    self.delivery.confirm_link(self.owner, self.team, link["generation"])
                self.assertEqual((caught.exception.status, caught.exception.code), (409, "confirmation_unavailable"))
                status = self.delivery.status(self.owner, self.team)
                self.assertEqual((status["state"], status["verified"], status["observed_chat_label"]), ("expired", False, None))
        self.assertEqual([method for method, _ in self.client.calls if method == "getChatMember"], [])

    def test_refused_start_burns_every_exposed_nonce(self):
        other_team = self.store.list_teams(self.other)[0]["id"]
        links = [self.delivery.begin_link(self.owner, self.team, "group"),
                 self.delivery.begin_link(self.other, other_team, "group")]
        update = self.group_update(links[0])
        update["message"]["text"] += "\n/start@siyabonnearbot " + links[1]["link"].split("=", 1)[1]
        self.client.updates = [update]
        self.delivery.run_once()
        for actor, team in ((self.owner, self.team), (self.other, other_team)):
            status = self.delivery.status(actor, team)
            self.assertEqual((status["state"], status["verified"]), ("expired", False))

    def assert_burned_and_not_replayable(self, link, kind="group"):
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["verified"], status["observed_chat_label"]), ("expired", False, None))
        with self.store._connection() as connection:
            self.assertIsNone(connection.execute("SELECT nonce_hash FROM delivery_bindings WHERE team_id=?", (self.team,)).fetchone()[0])
        self.client.updates = [self.group_update(link, chat_id=-1009999, sender_id=777) if kind == "group" else
                               self.group_update(link, chat_id=777, chat_type="private", sender_id=777)]
        self.delivery.run_once()
        self.delivery.run_once()
        with self.assertRaises(WorkspaceError) as caught:
            self.delivery.confirm_link(self.owner, self.team, link["generation"])
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "confirmation_unavailable"))
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["verified"], status["observed_chat_label"]), ("expired", False, None))

    def test_caption_start_cannot_be_replayed_by_another_group_and_sender(self):
        for kind in ("group", "private"):
            with self.subTest(kind=kind):
                link = self.delivery.begin_link(self.owner, self.team, kind)
                update = self.group_update(link) if kind == "group" else self.group_update(link, chat_id=555, chat_type="private")
                update["message"]["caption"] = update["message"].pop("text")
                update["message"]["photo"] = [{"file_id": "photo", "width": 1, "height": 1}]
                self.client.updates = [update]
                self.delivery.run_once()
                self.assert_burned_and_not_replayable(link, kind)
        self.assertEqual([method for method, _ in self.client.calls if method == "getChatMember"], [])

    def test_edited_message_start_cannot_be_replayed_by_another_group_and_sender(self):
        for field in ("text", "caption"):
            with self.subTest(field=field):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                update = self.group_update(link)
                message = update.pop("message")
                message[field] = message.pop("text")
                message["edit_date"] = int(self.now)
                update["edited_message"] = message
                self.client.updates = [update]
                self.delivery.run_once()
                self.assert_burned_and_not_replayable(link)
        self.assertEqual([method for method, _ in self.client.calls if method == "getChatMember"], [])

    def test_channel_post_start_cannot_be_replayed_by_another_group_and_sender(self):
        for shape in ("channel_post", "edited_channel_post"):
            for field in ("text", "caption"):
                with self.subTest(shape=shape, field=field):
                    link = self.delivery.begin_link(self.owner, self.team, "group")
                    update = self.group_update(link, chat_id=-1005555, chat_type="channel")
                    message = update.pop("message")
                    message[field] = message.pop("text")
                    message.pop("from")
                    message["sender_chat"] = {"id": -1005555, "type": "channel", "title": "Channel"}
                    update[shape] = message
                    self.client.updates = [update]
                    self.delivery.run_once()
                    self.assert_burned_and_not_replayable(link)
        self.assertEqual([method for method, _ in self.client.calls if method == "getChatMember"], [])

    def test_only_a_plain_message_update_is_ever_observed(self):
        for shape in ("edited_message", "channel_post", "edited_channel_post", "business_message", "unknown_future_update"):
            with self.subTest(shape=shape):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                update = self.group_update(link)
                update[shape] = update.pop("message")
                self.client.filter_updates = shape != "unknown_future_update"
                self.client.updates = [update]
                self.delivery.run_once()
                self.client.filter_updates = True
                self.assert_burned_and_not_replayable(link)
        self.assertEqual([method for method, _ in self.client.calls if method == "getChatMember"], [])

    def test_nonce_exposed_anywhere_in_an_update_is_burned(self):
        command = "/start@siyabonnearbot {}"
        shapes = {
            "text_link_entity": lambda nonce: {"text": "open this", "entities": [
                {"type": "text_link", "offset": 0, "length": 4, "url": "https://t.me/siyabonnearbot?startgroup=" + nonce}]},
            "caption_entity": lambda nonce: {"caption": "open this", "caption_entities": [
                {"type": "text_link", "offset": 0, "length": 4, "url": "https://example.invalid/" + nonce}]},
            "reply_to_message": lambda nonce: {"text": "what is this?", "reply_to_message": {"text": command.format(nonce)}},
            "quote": lambda nonce: {"text": "look", "quote": {"text": nonce, "position": 0}},
            "external_reply": lambda nonce: {"text": "look", "external_reply": {"origin": {"type": "hidden_user", "sender_user_name": nonce}}},
            "pinned_message": lambda nonce: {"pinned_message": {"caption": command.format(nonce)}},
            "poll": lambda nonce: {"poll": {"question": "code?", "options": [{"text": "no"}, {"text": nonce}]}},
            "new_chat_title": lambda nonce: {"new_chat_title": nonce},
            "document_name": lambda nonce: {"document": {"file_id": "file", "file_name": nonce + ".txt"}},
            "contact": lambda nonce: {"contact": {"phone_number": "1", "first_name": nonce}},
            "link_preview": lambda nonce: {"text": "x", "link_preview_options": {"url": "https://t.me/siyabonnearbot?start=" + nonce}},
            "inline_keyboard": lambda nonce: {"text": "x", "reply_markup": {"inline_keyboard": [[{"text": "go", "callback_data": nonce}]]}},
            "deeply_nested": lambda nonce: {"text": "x", "future": [[{"field": [{"deeper": {"value": nonce}}]}]]},
            "upper_case": lambda nonce: {"text": command.format(nonce.upper())},
            "spaced_out": lambda nonce: {"text": "/start@siyabonnearbot " + " ".join(nonce)},
            "split_lines": lambda nonce: {"text": nonce[:20] + "\n" + nonce[20:45] + "-" + nonce[45:]},
            "zero_width": lambda nonce: {"text": "\u200b".join(nonce)},
            "full_width": lambda nonce: {"text": "".join(chr(ord(character) + 0xfee0) for character in nonce)},
            "inside_longer_hex": lambda nonce: {"text": "0123456789abcdef" + nonce + "fedcba9876543210"},
        }
        for name, shape in shapes.items():
            with self.subTest(shape=name):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                update = self.group_update(link)
                update["message"].pop("text")
                update["message"].update(shape(link["link"].split("=", 1)[1]))
                self.client.updates = [update]
                self.delivery.run_once()
                self.assert_burned_and_not_replayable(link)
        for field in ("title", "first_name", "username"):
            with self.subTest(field=field):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                update = self.group_update(link, chat_id=-1004444, sender_id=444)
                update["message"]["text"] = "hello"
                update["message"]["chat" if field == "title" else "from"][field] = link["link"].split("=", 1)[1]
                self.client.updates = [update]
                self.delivery.run_once()
                self.assert_burned_and_not_replayable(link)
        for shape in ("inline_query", "chosen_inline_result", "callback_query", "chat_join_request"):
            with self.subTest(update=shape):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                nonce = link["link"].split("=", 1)[1]
                self.update_id += 1
                self.client.updates = [{"update_id": self.update_id, shape: {
                    "id": "1", "from": {"id": 777, "is_bot": False}, "query": nonce, "data": nonce, "bio": nonce}}]
                self.delivery.run_once()
                self.assert_burned_and_not_replayable(link)
        self.assertEqual([method for method, _ in self.client.calls if method == "getChatMember"], [])

    def test_private_nonce_exposed_in_a_group_burns_with_groups_off(self):
        with patch.dict("os.environ", {}, clear=True):
            for shape in ("caption", "edited_message", "channel_post"):
                with self.subTest(shape=shape):
                    link = self.delivery.begin_link(self.owner, self.team)
                    update = self.group_update(link)
                    if shape == "caption":
                        update["message"]["caption"] = update["message"].pop("text")
                    else:
                        update[shape] = update.pop("message")
                    self.client.updates = [update]
                    self.delivery.run_once()
                    self.assert_burned_and_not_replayable(link, "private")
            link = self.delivery.begin_link(self.owner, self.team)
            self.update_id += 1
            self.client.updates = [self.start_update(link["link"].split("=", 1)[1], update_id=self.update_id)]
            self.delivery.run_once()
            self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "observed")
            self.delivery.confirm_link(self.owner, self.team, link["generation"])
            self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "active")

    def test_poll_requests_every_update_shape_it_scans(self):
        for flag in ("1", "0"):
            with self.subTest(flag=flag), patch.dict("os.environ", {"SIYAB_TELEGRAM_GROUPS": flag}):
                self.delivery.run_once()
                requests = [payload for method, payload in self.client.calls if method == "getUpdates"]
                self.assertEqual(requests[-1]["allowed_updates"], UPDATE_TYPES)

    def test_every_requested_text_carrying_update_burns_and_cannot_be_replayed(self):
        sender = {"id": 777, "is_bot": False, "first_name": "Other"}
        group = {"id": -1009999, "type": "supergroup", "title": "Other group"}
        member = {"status": "member", "user": sender}
        shapes = {
            "edited_message": lambda nonce: {"message_id": 1, "date": 1, "edit_date": 2, "chat": group, "from": sender, "text": nonce},
            "channel_post": lambda nonce: {"message_id": 1, "date": 1, "chat": {**group, "type": "channel"}, "text": nonce},
            "edited_channel_post": lambda nonce: {"message_id": 1, "date": 1, "edit_date": 2, "chat": {**group, "type": "channel"}, "caption": nonce},
            "business_connection": lambda nonce: {"id": "c", "user": {**sender, "first_name": nonce}, "user_chat_id": 777, "date": 1, "is_enabled": True},
            "business_message": lambda nonce: {"message_id": 1, "date": 1, "business_connection_id": "c", "chat": group, "from": sender, "text": nonce},
            "edited_business_message": lambda nonce: {"message_id": 1, "date": 1, "business_connection_id": "c", "chat": group, "from": sender, "text": nonce},
            "deleted_business_messages": lambda nonce: {"business_connection_id": "c", "chat": {"id": 777, "type": "private", "first_name": nonce}, "message_ids": [1]},
            "message_reaction": lambda nonce: {"chat": {**group, "title": nonce}, "message_id": 1, "user": sender, "date": 1,
                                               "old_reaction": [], "new_reaction": [{"type": "custom_emoji", "custom_emoji_id": nonce}]},
            "message_reaction_count": lambda nonce: {"chat": {**group, "title": nonce}, "message_id": 1, "date": 1, "reactions": []},
            "inline_query": lambda nonce: {"id": "1", "from": sender, "query": nonce, "offset": ""},
            "chosen_inline_result": lambda nonce: {"result_id": "1", "from": sender, "query": nonce},
            "callback_query": lambda nonce: {"id": "1", "from": sender, "chat_instance": "1", "data": nonce},
            "shipping_query": lambda nonce: {"id": "1", "from": sender, "invoice_payload": nonce, "shipping_address": {
                "country_code": "NG", "state": "", "city": "x", "street_line1": "x", "street_line2": "", "post_code": "1"}},
            "pre_checkout_query": lambda nonce: {"id": "1", "from": sender, "currency": "XTR", "total_amount": 1, "invoice_payload": nonce},
            "purchased_paid_media": lambda nonce: {"from": sender, "paid_media_payload": nonce},
            "poll": lambda nonce: {"id": "1", "question": nonce, "options": [{"text": "a", "voter_count": 0}], "total_voter_count": 0},
            "poll_answer": lambda nonce: {"poll_id": "1", "user": {**sender, "first_name": nonce}, "option_ids": [0]},
            "my_chat_member": lambda nonce: {"chat": {**group, "title": nonce}, "from": sender, "date": 1,
                                             "old_chat_member": member, "new_chat_member": member},
            "chat_member": lambda nonce: {"chat": group, "from": sender, "date": 1, "old_chat_member": member, "new_chat_member": member,
                                          "invite_link": {"invite_link": "https://t.me/+x", "name": nonce}},
            "chat_join_request": lambda nonce: {"chat": group, "from": sender, "user_chat_id": 777, "date": 1, "bio": nonce},
            "chat_boost": lambda nonce: {"chat": {**group, "title": nonce}, "boost": {"boost_id": "1", "add_date": 1, "expiration_date": 2,
                                                                                  "source": {"source": "premium", "user": sender}}},
            "removed_chat_boost": lambda nonce: {"chat": group, "boost_id": "1", "remove_date": 1,
                                                 "source": {"source": "premium", "user": {**sender, "first_name": nonce}}},
        }
        self.assertEqual(sorted(shapes), sorted(set(UPDATE_TYPES) - {"message"}))
        for kind in ("group", "private"):
            for shape, build in shapes.items():
                with self.subTest(kind=kind, update=shape):
                    link = self.delivery.begin_link(self.owner, self.team, kind)
                    self.update_id += 1
                    self.client.updates = [{"update_id": self.update_id, shape: build(link["link"].split("=", 1)[1])}]
                    self.delivery.run_once()
                    self.assert_burned_and_not_replayable(link, kind)
        self.assertEqual([method for method, _ in self.client.calls if method == "getChatMember"], [])

    def test_updates_are_only_hashed_while_a_link_is_pending(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                if pending:
                    self.delivery.begin_link(self.owner, self.team, "group")
                self.update_id += 1
                self.client.updates = [{"update_id": self.update_id, "message": {
                    "text": "0123456789abcdef" * 256, "chat": {"id": -1001234, "type": "supergroup"},
                    "from": {"id": 555, "is_bot": False}}}]
                with patch("intel.delivery.hashlib.sha256", wraps=hashlib.sha256) as digest:
                    self.assertIsNone(self.delivery.run_once()["last_error"])
                self.assertEqual(digest.call_count > 0, pending)
                with self.store._connection() as connection:
                    self.assertEqual(connection.execute("SELECT update_offset FROM delivery_state").fetchone()[0], self.update_id + 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_bindings WHERE nonce_hash IS NOT NULL").fetchone()[0], int(pending))

    def test_oversized_update_batch_is_read_in_smaller_pages(self):
        self.activate()
        self.update_id = 1
        original = self.client.call
        for fits, limits, identifier in ((10, [100, 10], "c"), (1, [100, 10, 1], "d"), (0, [100, 10, 1], "e")):
            with self.subTest(fits=fits):
                link = self.delivery.begin_link(self.other, self.store.list_teams(self.other)[0]["id"], "group")
                self.client.updates = [self.group_update(link)]
                self.event(identifier)
                requested = []
                def paged(method, payload):
                    if method == "getUpdates":
                        requested.append(payload["limit"])
                        if payload["limit"] > fits:
                            raise TelegramError("response_too_large", retryable=True, uncertain=True)
                    return original(method, payload)
                with patch.object(self.client, "call", side_effect=paged):
                    result = self.delivery.run_once()
                self.assertEqual(requested, limits)
                self.assertEqual((result["sent"], result["last_error"]), (1, None) if fits else (0, "response_too_large"))
                with self.store._connection() as connection:
                    self.assertEqual(connection.execute("SELECT update_offset FROM delivery_state").fetchone()[0], self.update_id + bool(fits))
                self.now += 5

    def test_group_label_drops_quotes_brackets_and_invisible_characters(self):
        for title, shown in (('Team alerts" (ID ending 9999), sent by @owner\u202e\u200b\n\x00\u2066 x',
                              "Team alerts ID ending 9999, sent by @owner x"),
                             ("Team\u201d \uff08ID ending 9999\uff09, sent by @owner \u201c\u00ab\u300c\uff02'`\u2033[x]{y}",
                              "Team ID ending 9999, sent by @owner xy"), ('"\u202e"', "Telegram group")):
            with self.subTest(title=title):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                update = self.group_update(link)
                update["message"]["chat"]["title"] = title
                self.client.updates = [update]
                self.delivery.run_once()
                self.assertEqual(self.delivery.status(self.owner, self.team)["observed_chat_label"],
                                 'Group "' + shown + '" (ID ending 1234), sent by @groupadmin')

    def test_group_send_cooldown_pauses_only_that_group(self):
        self.activate()
        self.update_id = 1
        other_team = self.store.list_teams(self.other)[0]["id"]
        self.store.add_watch(self.other, other_team, "wrap.near")
        link = self.delivery.begin_link(self.other, other_team, "group")
        self.client.updates = [self.group_update(link)]
        self.delivery.run_once()
        self.delivery.run_once()
        self.delivery.confirm_link(self.other, other_team, link["generation"])
        self.event("a")
        self.client.chat_errors[-1001234] = TelegramError("provider_429", retryable=True, retry_after=60)
        started = self.now
        results = [self.delivery.run_once() for _ in range(2)]
        self.assertEqual([sum(result[key] for result in results) for key in ("sent", "failed")], [1, 1])
        self.assertEqual(sorted(send["chat_id"] for send in self.sends()), [-1001234, 555])
        with self.store._connection() as connection:
            self.assertEqual(tuple(connection.execute("SELECT next_poll_ms,next_send_ms FROM delivery_state").fetchone()), (0, 0))
            self.assertEqual(tuple(connection.execute("SELECT chat_id,send_after_ms FROM delivery_chat_pacing").fetchone()),
                             (-1001234, int(started * 1000) + 60000))
        self.event("b")
        results = [self.delivery.run_once() for _ in range(2)]
        self.assertEqual((sum(result["sent"] for result in results), results[1]["last_error"]), (1, None))
        self.now = started + 59.999
        self.assertEqual(sum(self.delivery.run_once()["sent"] for _ in range(2)), 0)
        self.now = started + 60
        self.assertEqual(self.delivery.run_once()["sent"], 1)
        self.assertEqual(sorted(send["chat_id"] for send in self.sends()), [-1001234, -1001234, 555, 555])

    def test_group_warning_clears_after_disconnect(self):
        for action in ("revoke", "expire"):
            with self.subTest(action=action):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                self.observe(link)
                self.delivery.run_once()
                self.assertEqual(self.delivery.status(self.owner, self.team)["kind"], "group")
                if action == "revoke":
                    self.delivery.confirm_link(self.owner, self.team, link["generation"])
                    self.delivery.revoke(self.owner, self.team)
                else:
                    self.now += LINK_MS / 1000
                status = self.delivery.status(self.owner, self.team)
                self.assertEqual(status["state"], "disconnected" if action == "revoke" else "expired")
                self.assertEqual(status["kind"], "private")

    def test_group_verify_honours_retry_after_and_lease(self):
        teams = [self.team, self.store.create_team(self.owner, "Group two")["id"],
                 self.store.create_team(self.owner, "Group three")["id"]]
        for phase in ("cooldown", "lease"):
            with self.subTest(phase=phase):
                links = [self.delivery.begin_link(self.owner, team, "group") for team in teams]
                self.client.updates = [self.group_update(link, chat_id=-1001234 - index) for index, link in enumerate(links)]
                self.delivery.run_once()
                original = self.client.call
                calls = []
                started = self.now
                def verify(method, payload):
                    calls.append(method)
                    if method == "getChatMember":
                        if phase == "cooldown":
                            raise TelegramError("provider_429", retryable=True, retry_after=120)
                        self.now += REQUEST_SECONDS
                        raise TelegramError("provider_503", retryable=True)
                    if method == "getUpdates" and phase == "lease":
                        self.now += REQUEST_SECONDS
                    return original(method, payload)
                with patch.object(self.client, "call", side_effect=verify):
                    self.assertIsNone(self.delivery.run_once()["last_error"])
                    if phase == "cooldown":
                        self.assertEqual(calls.count("getChatMember"), 1)
                        self.assertIn("getUpdates", calls)
                        with self.store._connection() as connection:
                            meta = connection.execute("SELECT next_poll_ms,next_send_ms,next_verify_ms FROM delivery_state").fetchone()
                            self.assertEqual(tuple(meta), (0, 0, int(started * 1000) + 120000))
                        self.now += 119
                        self.assertIsNone(self.delivery.run_once()["last_error"])
                        self.assertEqual(calls.count("getChatMember"), 1)
                    else:
                        self.assertLess((self.now - started) * 1000, LEASE_MS)
                        self.assertGreater(calls.count("getChatMember"), 0)
                        self.assertLess(calls.count("getChatMember"), 3)
                if phase == "cooldown":
                    self.now += 1
                    self.delivery.run_once()
                    self.assertTrue(all(self.delivery.status(self.owner, team)["verified"] for team in teams))
                else:
                    self.assertTrue(all(not self.delivery.status(self.owner, team)["verified"] for team in teams))

    def test_nonce_is_bound_to_its_kind(self):
        for kind, wrong_kind in (("private", "group"), ("group", "private")):
            with self.subTest(kind=kind):
                link = self.delivery.begin_link(self.owner, self.team, kind)
                update = self.group_update(link, chat_id=555 if wrong_kind == "private" else -1001234,
                                           chat_type=wrong_kind)
                self.client.updates = [update]
                self.delivery.run_once()
                self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "expired")
                update = self.group_update(link, chat_id=555 if kind == "private" else -1001234, chat_type=kind)
                self.client.updates = [update]
                self.delivery.run_once()
                self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "expired")
                link = self.delivery.begin_link(self.owner, self.team, kind)
                self.client.updates = [self.group_update(link, chat_id=555 if kind == "private" else -1001234, chat_type=kind)]
                self.delivery.run_once()
                self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "observed")
                self.client.updates = [self.group_update(link, chat_id=999 if kind == "private" else -9999, chat_type=kind,
                                                         sender_id=999)]
                self.delivery.run_once()
                label = self.delivery.status(self.owner, self.team)["observed_chat_label"]
                self.assertIn("1234" if kind == "group" else "555", label)
                self.now += LINK_MS / 1000
                with self.assertRaises(WorkspaceError) as caught:
                    self.delivery.confirm_link(self.owner, self.team, link["generation"])
                self.assertEqual(caught.exception.code, "confirmation_unavailable")

    def conflict(self):
        status = self.delivery.status(self.owner, self.team)
        return status["link_conflict"], status["link_conflict_chats"]

    def binding_fields(self):
        with self.store._connection() as connection:
            return dict(connection.execute("SELECT nonce_hash,nonce_expires_ms,observed_chat_id,observed_label,observed_ms,"
                                           "observed_sender_id,verified_ms,active FROM delivery_bindings WHERE team_id=?",
                                           (self.team,)).fetchone())

    def test_second_chat_sighting_warns_and_keeps_the_first_chat(self):
        for batch in (False, True):
            with self.subTest(same_batch=batch):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                self.assertEqual(self.conflict(), (False, 0))
                if batch:
                    self.client.updates = [self.group_update(link), self.group_update(link, chat_id=-1009999, sender_id=777)]
                    self.delivery.run_once()
                    self.delivery.run_once()
                else:
                    self.observe(link)
                    self.delivery.run_once()
                    self.assertEqual(self.conflict(), (False, 0))
                    self.client.updates = [self.group_update(link, chat_id=-1009999, sender_id=777)]
                    self.delivery.run_once()
                self.assertEqual(self.conflict(), (True, 1))
                before = self.binding_fields()
                start = self.group_update(link, chat_id=777, chat_type="private", sender_id=777)
                start["message"]["text"] = "/start " + link["link"].split("=", 1)[1]
                self.client.updates = [self.group_update(link, chat_id=-1008888, sender_id=888), start,
                                       self.group_update(link, chat_id=-1009999, sender_id=999)]
                self.delivery.run_once()
                self.assertEqual(self.conflict(), (True, 3))
                after = self.binding_fields()
                self.assertEqual(after, before)
                self.assertEqual((after["nonce_hash"], after["observed_chat_id"], after["observed_sender_id"], after["active"]),
                                 (None, -1001234, 555, 0))
                status = self.delivery.status(self.owner, self.team)
                self.assertEqual((status["state"], status["verified"]), ("observed", True))
                self.assertIn("(ID ending 1234), sent by @groupadmin", status["observed_chat_label"])
                self.assertEqual({payload["chat_id"] for method, payload in self.client.calls if method == "getChatMember"}, {-1001234})
                self.assertEqual(self.sends(), [])
                self.delivery.begin_link(self.owner, self.team, "group")
                self.assertEqual(self.conflict(), (False, 0))
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        self.client.updates = [self.group_update(link, chat_id=-1000000 - index, sender_id=700 + index) for index in range(25)]
        self.delivery.run_once()
        self.assertEqual(self.conflict(), (True, 20))
        self.delivery.revoke(self.owner, self.team)
        self.assertEqual(self.conflict(), (False, 0))

    def test_conflicting_sighting_never_activates_verifies_or_confirms(self):
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.client.updates = [self.group_update(link), self.group_update(link, chat_id=-1009999, sender_id=777)]
        self.client.member_results = [{"status": "member", "user": {"id": 555, "is_bot": False}}]
        self.delivery.run_once()
        self.delivery.run_once()
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["verified"], status["link_conflict"]), ("expired", False, False))
        with self.assertRaises(WorkspaceError):
            self.delivery.confirm_link(self.owner, self.team, link["generation"])
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        self.client.updates = [self.group_update(link, chat_id=-1009999, sender_id=777)]
        self.delivery.run_once()
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["verified"], status["link_conflict"]), ("observed", True, True))
        self.delivery.confirm_link(self.owner, self.team, link["generation"])
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["link_conflict"], status["link_conflict_chats"]), ("active", False, 0))
        with self.store._connection() as connection:
            self.assertEqual(tuple(connection.execute("SELECT observed_nonce_hash,observed_chat_id FROM delivery_bindings").fetchone()),
                             (None, -1001234))
        self.event()
        self.assertEqual(self.delivery.run_once()["sent"], 1)
        self.assertEqual([send["chat_id"] for send in self.sends()], [-1001234])

    def test_same_chat_repeating_the_start_does_not_warn(self):
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        self.client.updates = [self.group_update(link), self.group_update(link, sender_id=777)]
        self.delivery.run_once()
        self.assertEqual(self.conflict(), (False, 0))
        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "observed")

    def test_sighting_outside_the_exact_start_does_not_warn(self):
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        nonce = link["link"].split("=", 1)[1]
        updates = [self.group_update(link, chat_id=-1009999, sender_id=777) for _ in range(4)]
        updates[0]["message"]["text"] = "/start@anotherbot " + nonce
        updates[1]["message"]["text"] = "look " + nonce
        updates[2]["edited_message"] = updates[2].pop("message")
        updates[3]["message"]["caption"] = updates[3]["message"].pop("text")
        self.client.updates = updates
        self.delivery.run_once()
        self.assertEqual(self.conflict(), (False, 0))
        self.now += LINK_MS / 1000
        self.client.updates = [self.group_update(link, chat_id=-1009999, sender_id=777)]
        self.delivery.run_once()
        self.assertEqual(self.conflict(), (False, 0))

    def test_conflict_survives_reload_and_old_rows_migrate_without_it(self):
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        self.client.updates = [self.group_update(link, chat_id=-1009999, sender_id=777)]
        self.delivery.run_once()
        self.delivery = TelegramDelivery(self.store, self.client, clock=lambda: self.now)
        self.assertEqual(self.conflict(), (True, 1))
        self.client.updates = [self.group_update(link, chat_id=-1009999, sender_id=777)]
        self.delivery.run_once()
        self.assertEqual(self.conflict(), (True, 1))
        self.store.join_team(self.other, self.store.invite_member(self.owner, self.team, "other", "viewer")["token"])
        self.assertNotIn("link_conflict", self.delivery.status(self.other, self.team))
        store, owner, member, team = self.legacy_store("groups-conflict-old.sqlite3")
        for _ in range(2):
            delivery = TelegramDelivery(store, self.client, clock=lambda: self.now)
        with store._connection() as connection:
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(delivery_bindings)")]
            for column in ("observed_nonce_hash", "conflict_chats", "conflict_keys"):
                self.assertEqual(columns.count(column), 1)
            row = connection.execute("SELECT observed_nonce_hash,conflict_chats,conflict_keys FROM delivery_bindings").fetchone()
            self.assertEqual(tuple(row), (None, 0, ""))
        status = delivery.status(owner, team)
        self.assertEqual((status["state"], status["link_conflict"], status["link_conflict_chats"]), ("active", False, 0))
        self.assertNotIn("link_conflict", delivery.status(member, team))

    def test_dedupe_keys_are_dropped_once_the_link_ends(self):
        self.store.join_team(self.other, self.store.invite_member(self.owner, self.team, "other", "editor")["token"])
        for end in ("expire", "remove_owner"):
            with self.subTest(end=end):
                if end == "remove_owner":
                    self.store.set_role(self.owner, self.team, self.other, "owner")
                link = self.delivery.begin_link(self.owner, self.team, "group")
                self.observe(link)
                self.client.updates = [self.group_update(link, chat_id=-1009999, sender_id=777)]
                self.delivery.run_once()
                with self.store._connection() as connection:
                    row = connection.execute("SELECT observed_nonce_hash,conflict_keys FROM delivery_bindings").fetchone()
                    self.assertIsNotNone(row["observed_nonce_hash"])
                    self.assertEqual(len(row["conflict_keys"]), 16)
                if end == "expire":
                    self.now += LINK_MS / 1000
                else:
                    self.store.remove_member(self.other, self.team, self.owner)
                self.delivery.run_once()
                with self.store._connection() as connection:
                    self.assertEqual(tuple(connection.execute("SELECT observed_nonce_hash,conflict_keys FROM delivery_bindings").fetchone()), (None, ""))

    def conflict_keys(self):
        with self.store._connection() as connection:
            return tuple(connection.execute("SELECT observed_nonce_hash,conflict_keys FROM delivery_bindings").fetchone())

    def sighted_link(self):
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        self.client.updates = [self.group_update(link, chat_id=-1009999, sender_id=777)]
        self.delivery.run_once()
        self.assertEqual(self.conflict(), (True, 1))
        self.assertEqual(len(self.conflict_keys()[1]), 16)
        return link

    def test_dedupe_keys_are_dropped_when_polls_fail_or_cooldown_holds(self):
        self.sighted_link()
        self.now += LINK_MS / 1000
        self.client.update_errors = [TelegramError("network_error", retryable=True)]
        self.assertEqual(self.delivery.run_once()["last_error"], "network_error")
        self.assertEqual(self.client.update_errors, [])
        self.assertEqual(self.conflict_keys(), (None, ""))
        self.now += 10
        self.sighted_link()
        self.client.update_errors = [TelegramError("rate_limited", retryable=True, retry_after=3600)]
        self.assertEqual(self.delivery.run_once()["last_error"], "rate_limited")
        self.assertIsNotNone(self.conflict_keys()[0])
        self.now += LINK_MS / 1000
        self.assertEqual(self.delivery.run_once()["last_error"], "provider_cooldown")
        self.assertEqual(self.conflict_keys(), (None, ""))

    def test_dedupe_keys_are_dropped_on_owner_disconnect_without_a_poll(self):
        self.store.join_team(self.other, self.store.invite_member(self.owner, self.team, "other", "editor")["token"])
        self.store.set_role(self.owner, self.team, self.other, "owner")
        for end in ("demote", "remove"):
            with self.subTest(end=end):
                self.sighted_link()
                polls = len([method for method, _ in self.client.calls if method == "getUpdates"])
                if end == "demote":
                    self.store.set_role(self.other, self.team, self.owner, "editor")
                else:
                    self.store.remove_member(self.other, self.team, self.owner)
                self.assertEqual(self.conflict_keys(), (None, ""))
                self.assertEqual(len([method for method, _ in self.client.calls if method == "getUpdates"]), polls)
                status = self.delivery.status(self.other, self.team)
                self.assertEqual((status["state"], status["link_conflict"], status["link_conflict_chats"]), ("disconnected", False, 0))
                if end == "demote":
                    self.store.set_role(self.other, self.team, self.owner, "owner")

    def test_conflict_is_reported_only_while_the_link_is_observed(self):
        self.sighted_link()
        self.now += LINK_MS / 1000
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["link_conflict"], status["link_conflict_chats"]), ("expired", False, 0))
        link = self.sighted_link()
        self.delivery.confirm_link(self.owner, self.team, link["generation"])
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["link_conflict"], status["link_conflict_chats"]), ("active", False, 0))
        self.store.join_team(self.other, self.store.invite_member(self.owner, self.team, "other", "editor")["token"])
        self.store.set_role(self.owner, self.team, self.other, "owner")
        self.sighted_link()
        self.store.remove_member(self.other, self.team, self.owner)
        status = self.delivery.status(self.other, self.team)
        self.assertEqual((status["state"], status["link_conflict"], status["link_conflict_chats"]), ("disconnected", False, 0))
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT conflict_chats FROM delivery_bindings").fetchone()[0], 1)

    def test_private_link_other_chat_start_does_not_warn(self):
        with patch.dict("os.environ", {}, clear=True):
            link = self.delivery.begin_link(self.owner, self.team)
            nonce = link["link"].split("=", 1)[1]
            self.update_id += 1
            self.client.updates = [self.start_update(nonce, update_id=self.update_id)]
            self.delivery.run_once()
            self.update_id += 1
            self.client.updates = [self.start_update(nonce, update_id=self.update_id, chat_id=999)]
            self.delivery.run_once()
            status = self.delivery.status(self.owner, self.team)
            self.assertEqual((status["state"], status["link_conflict"], status["link_conflict_chats"]), ("observed", False, 0))
            self.assertIn("555", status["observed_chat_label"])
            with self.store._connection() as connection:
                self.assertIsNone(connection.execute("SELECT observed_nonce_hash FROM delivery_bindings").fetchone()[0])
            self.delivery.confirm_link(self.owner, self.team, link["generation"])
            self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "active")

    def test_observation_needs_suffix_human_sender_and_group_chat(self):
        link = self.delivery.begin_link(self.owner, self.team, "group")
        valid = self.group_update(link)
        invalid = []
        for text in ("/start ", "/start@anotherbot "):
            update = copy.deepcopy(valid)
            update["message"]["text"] = text + link["link"].split("=", 1)[1]
            invalid.append(update)
        for field, values in (("type", ("channel", "private", None)),
                              ("id", (0, 1234, -(2 ** 52), -(2 ** 52) - 1, True, "-1234"))):
            for value in values:
                update = copy.deepcopy(valid)
                update["message"]["chat"][field] = value
                invalid.append(update)
        for sender in (None, {"id": 555, "is_bot": True}, {"id": 555},
                       {"id": True, "is_bot": False}, {"id": -1, "is_bot": False},
                       {"id": 2 ** 52, "is_bot": False}):
            update = copy.deepcopy(valid)
            update["message"]["from"] = sender
            invalid.append(update)
        update = copy.deepcopy(valid)
        update["message"]["sender_chat"] = {"id": -1001234, "type": "supergroup"}
        invalid.append(update)
        for update in invalid:
            with self.subTest(message=update["message"]):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                update["message"]["text"] = update["message"]["text"].split(" ", 1)[0] + " " + link["link"].split("=", 1)[1]
                self.update_id += 1
                update["update_id"] = self.update_id
                self.client.updates = [update]
                self.delivery.run_once()
                self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "expired")
        for chat_type in ("group", "supergroup"):
            link = self.delivery.begin_link(self.owner, self.team, "group")
            update = self.group_update(link, chat_type=chat_type)
            update["message"]["chat"]["title"] = "T" * 70
            self.client.updates = [update]
            self.delivery.run_once()
            observed = self.delivery.status(self.owner, self.team)
            self.assertEqual(observed["observed_chat_label"],
                             'Group "' + "T" * 64 + '" (ID ending 1234), sent by @groupadmin')
            self.assertFalse(observed["verified"])

    def test_admin_or_creator_verifies_and_member_ends_the_link(self):
        for member_status in ("administrator", "creator", "member", "restricted", "left", "kicked"):
            with self.subTest(status=member_status):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                self.observe(link)
                self.client.member_results = [{"status": member_status, "user": {"id": 555, "is_bot": False}}]
                self.delivery.run_once()
                status = self.delivery.status(self.owner, self.team)
                allowed = member_status in ("administrator", "creator")
                self.assertEqual((status["state"], status["verified"]), ("observed" if allowed else "expired", allowed))
                self.assertEqual(self.client.calls[-2], ("getChatMember", {"chat_id": -1001234, "user_id": 555}))
                if allowed:
                    self.delivery.confirm_link(self.owner, self.team, link["generation"])
                    checks = sum(method == "getChatMember" for method, _ in self.client.calls)
                    self.delivery.run_once()
                    self.assertEqual(sum(method == "getChatMember" for method, _ in self.client.calls), checks)
                else:
                    self.assertIsNone(status["observed_chat_label"])
                    with self.assertRaises(WorkspaceError):
                        self.delivery.confirm_link(self.owner, self.team, link["generation"])
                    self.client.updates = [self.group_update(link)]
                    self.delivery.run_once()
                    self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "expired")

    def test_malformed_member_answer_is_not_verification(self):
        answers = (None, [], {}, {"status": "administrator"},
                   {"status": "administrator", "user": {"id": 555}},
                   {"status": "administrator", "user": {"id": 555, "is_bot": True}},
                   {"status": "administrator", "user": {"id": "555", "is_bot": False}},
                   {"status": "administrator", "user": {"id": True, "is_bot": False}},
                   {"status": "administrator", "user": {"id": 556, "is_bot": False}})
        for answer in answers:
            with self.subTest(answer=answer):
                link = self.delivery.begin_link(self.owner, self.team, "group")
                self.observe(link)
                self.client.member_results = [answer]
                result = self.delivery.run_once()
                self.assertIsNone(result["last_error"])
                status = self.delivery.status(self.owner, self.team)
                self.assertEqual((status["state"], status["verified"], status["observed_chat_label"]), ("expired", False, None))

    def test_malformed_confirmation_names_the_link_kind(self):
        for kind, wording in (("group", "Confirm the currently observed Telegram group."),
                              ("private", "Confirm the currently observed Telegram account.")):
            with self.subTest(kind=kind):
                self.delivery.begin_link(self.owner, self.team, kind)
                with self.assertRaises(WorkspaceError) as caught:
                    self.delivery.confirm_link(self.owner, self.team, 0)
                self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.message),
                                 (400, "invalid_confirmation", wording))

    def test_confirm_refused_until_verified(self):
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        with self.assertRaises(WorkspaceError) as caught:
            self.delivery.confirm_link(self.owner, self.team, link["generation"])
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "confirmation_unavailable"))
        self.delivery.run_once()
        self.assertTrue(self.delivery.status(self.owner, self.team)["verified"])
        self.assertEqual(self.sends(), [])
        self.delivery.confirm_link(self.owner, self.team, link["generation"])
        self.assertEqual(self.delivery.status(self.owner, self.team)["state"], "active")
        self.delivery.revoke(self.owner, self.team)
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["verified"]), ("disconnected", False))
        with self.store._connection() as connection:
            row = connection.execute("SELECT observed_sender_id,verified_ms FROM delivery_bindings").fetchone()
            self.assertEqual(tuple(row), (None, None))

    def test_verification_failure_never_stalls_delivery(self):
        self.activate()
        self.update_id = 1
        other_team = self.store.list_teams(self.other)[0]["id"]
        for retryable in (True, False):
            with self.subTest(retryable=retryable):
                link = self.delivery.begin_link(self.other, other_team, "group")
                self.client.updates = [self.group_update(link)]
                self.delivery.run_once()
                self.event("a" if retryable else "b")
                self.client.member_results = [TelegramError("provider_503" if retryable else "provider_403", retryable=retryable)]
                result = self.delivery.run_once()
                self.assertEqual((result["sent"], result["last_error"]), (1, None))
                status = self.delivery.status(self.other, other_team)
                self.assertEqual((status["state"], status["verified"]), ("observed" if retryable else "expired", False))
                if retryable:
                    self.now += LINK_MS / 1000
                    checks = sum(method == "getChatMember" for method, _ in self.client.calls)
                    self.delivery.run_once()
                    self.assertEqual(sum(method == "getChatMember" for method, _ in self.client.calls), checks)
                    self.assertEqual(self.delivery.status(self.other, other_team)["state"], "expired")

    def test_group_link_replaces_private_destination(self):
        self.activate()
        self.event("a")
        self.delivery._enqueue()
        self.event("b")
        self.delivery._enqueue()
        self.delivery._claim()
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.assertEqual(self.delivery.status(self.owner, self.team)["counts"], {"cancelled": 2})
        self.update_id = 1
        self.observe(link)
        self.delivery.run_once()
        self.delivery.confirm_link(self.owner, self.team, link["generation"])
        self.event("c")
        self.assertEqual(self.delivery.run_once()["sent"], 1)
        self.assertEqual([send["chat_id"] for send in self.sends()], [-1001234])
        self.assertNotIn("PRIVATE", self.sends()[0]["text"])
        self.assertIn("c" * 64, self.sends()[0]["text"])
        with self.store._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_bindings WHERE team_id=?", (self.team,)).fetchone()[0], 1)

    def test_group_verify_backoff_never_stalls_private_delivery(self):
        self.activate()
        self.update_id = 1
        other_team = self.store.list_teams(self.other)[0]["id"]
        link = self.delivery.begin_link(self.other, other_team, "group")
        self.client.updates = [self.group_update(link)]
        self.delivery.run_once()
        self.client.member_results = [TelegramError("provider_503", retryable=True, retry_after=120)]
        started = self.now
        self.assertIsNone(self.delivery.run_once()["last_error"])
        checks = sum(method == "getChatMember" for method, _ in self.client.calls)
        self.delivery = TelegramDelivery(self.store, self.client, clock=lambda: self.now)
        for identifier in "ab":
            self.event(identifier)
            result = self.delivery.run_once()
            self.assertEqual((result["sent"], result["last_error"]), (1, None))
            self.assertFalse(self.delivery.status(self.other, other_team)["verified"])
            self.assertEqual(sum(method == "getChatMember" for method, _ in self.client.calls), checks)
        self.assertEqual([send["chat_id"] for send in self.sends()], [555, 555])
        self.now = started + 119.999
        self.delivery.run_once()
        self.assertEqual(sum(method == "getChatMember" for method, _ in self.client.calls), checks)
        self.now = started + 120
        self.delivery.run_once()
        self.assertTrue(self.delivery.status(self.other, other_team)["verified"])

    def test_group_sends_are_spaced(self):
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        self.delivery.run_once()
        self.delivery.confirm_link(self.owner, self.team, link["generation"])
        for identifier in "abc":
            self.event(identifier)
        self.assertEqual(self.delivery.run_once()["sent"], 1)
        self.now += 2.999
        self.assertEqual(self.delivery.run_once()["sent"], 0)
        self.now += .001
        self.assertEqual(self.delivery.run_once()["sent"], 1)
        self.now += 3
        self.assertEqual(self.delivery.run_once()["sent"], 1)

    def test_removing_linking_owner_disconnects_group(self):
        self.store.join_team(self.other, self.store.invite_member(self.owner, self.team, "other", "editor")["token"])
        self.store.set_role(self.owner, self.team, self.other, "owner")
        for action in ("remove", "demote"):
            with self.subTest(action=action):
                if action == "demote":
                    self.store.join_team(self.owner, self.store.invite_member(self.other, self.team, "owner", "editor")["token"])
                    self.store.set_role(self.other, self.team, self.owner, "owner")
                link = self.delivery.begin_link(self.owner, self.team, "group")
                self.observe(link)
                self.delivery.run_once()
                self.delivery.confirm_link(self.owner, self.team, link["generation"])
                self.event("a" if action == "remove" else "b")
                self.delivery._enqueue()
                if action == "remove":
                    self.store.remove_member(self.other, self.team, self.owner)
                else:
                    self.store.set_role(self.other, self.team, self.owner, "editor")
                status = self.delivery.status(self.other, self.team)
                self.assertEqual((status["state"], status["verified"]), ("disconnected", False))
                self.assertEqual(status["counts"].get("pending", 0), 0)
                self.assertEqual(self.delivery.run_once()["sent"], 0)

    def test_group_send_spacing_is_shared_across_teams(self):
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        self.delivery.run_once()
        self.delivery.confirm_link(self.owner, self.team, link["generation"])
        other_team = self.store.list_teams(self.other)[0]["id"]
        self.store.add_watch(self.other, other_team, "wrap.near")
        link = self.delivery.begin_link(self.other, other_team, "group")
        self.client.updates = [self.group_update(link)]
        self.delivery.run_once()
        self.delivery.run_once()
        self.delivery.confirm_link(self.other, other_team, link["generation"])
        self.event()
        self.assertEqual(self.delivery.run_once()["sent"], 1)
        self.assertEqual(self.delivery.run_once()["sent"], 0)
        with self.store._connection() as connection:
            sent_team = connection.execute("SELECT team_id FROM delivery_queue WHERE state='sent'").fetchone()[0]
        self.delivery.revoke(self.owner if sent_team == self.team else self.other, sent_team)
        self.assertEqual(self.delivery.run_once()["sent"], 0)
        self.now += 2.999
        self.assertEqual(self.delivery.run_once()["sent"], 0)
        self.now += .001
        self.assertEqual(self.delivery.run_once()["sent"], 1)
        self.assertEqual([send["chat_id"] for send in self.sends()], [-1001234, -1001234])

    def test_group_pacing_survives_binding_disconnect(self):
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        self.delivery.run_once()
        self.delivery.confirm_link(self.owner, self.team, link["generation"])
        self.event()
        self.assertEqual(self.delivery.run_once()["sent"], 1)
        sent_at = self.now
        self.delivery.revoke(self.owner, self.team)
        self.delivery = TelegramDelivery(self.store, self.client, clock=lambda: self.now)
        other_team = self.store.list_teams(self.other)[0]["id"]
        self.store.add_watch(self.other, other_team, "wrap.near")
        link = self.delivery.begin_link(self.other, other_team, "group")
        self.client.updates = [self.group_update(link)]
        self.delivery.run_once()
        self.delivery.run_once()
        self.delivery.confirm_link(self.other, other_team, link["generation"])
        self.store.ingest_event("b" * 64, {"signal": "account_transfer", "subject": "wrap.near", "detail": "Observed transfer",
                                "observed": {"timestamp_ms": int(self.now * 1000)}}, notify=True)
        self.assertEqual(self.now, sent_at)
        self.assertEqual(self.delivery.run_once()["sent"], 0)
        self.now = sent_at + 2.999
        self.assertEqual(self.delivery.run_once()["sent"], 0)
        self.now = sent_at + 3
        self.assertEqual(self.delivery.run_once()["sent"], 1)
        self.assertEqual([send["chat_id"] for send in self.sends()], [-1001234, -1001234])

    def test_old_group_pacing_migrates_from_bindings(self):
        store, owner, member, team = self.legacy_store("groups-pacing-old.sqlite3")
        until = int(self.now * 1000) + 3000
        with store._connection(write=True) as connection:
            connection.execute("ALTER TABLE delivery_bindings ADD COLUMN kind TEXT NOT NULL DEFAULT 'private'")
            connection.execute("UPDATE delivery_bindings SET kind='group',observed_chat_id=-1001234,send_after_ms=?", (until,))
        for _ in range(2):
            TelegramDelivery(store, self.client, clock=lambda: self.now)
        with store._connection() as connection:
            row = connection.execute("SELECT chat_id,send_after_ms FROM delivery_chat_pacing").fetchone()
            self.assertEqual(tuple(row), (-1001234, until))
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(delivery_state)")]
            self.assertEqual(columns.count("next_verify_ms"), 1)
            self.assertEqual(connection.execute("SELECT next_verify_ms FROM delivery_state").fetchone()[0], 0)
        with store._connection(write=True) as connection:
            connection.execute("UPDATE delivery_chat_pacing SET send_after_ms=?", (until + 3000,))
        TelegramDelivery(store, self.client, clock=lambda: self.now)
        with store._connection() as connection:
            self.assertEqual(connection.execute("SELECT send_after_ms FROM delivery_chat_pacing").fetchone()[0], until + 3000)

    def test_old_schema_gains_group_columns(self):
        store, owner, member, team = self.legacy_store("groups-old.sqlite3")
        for _ in range(2):
            delivery = TelegramDelivery(store, self.client, clock=lambda: self.now)
        with store._connection() as connection:
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(delivery_bindings)")]
            for column in ("kind", "observed_sender_id", "verified_ms"):
                self.assertEqual(columns.count(column), 1)
            row = connection.execute("SELECT kind,observed_sender_id,verified_ms,observed_chat_id,active FROM delivery_bindings").fetchone()
            self.assertEqual(tuple(row), ("private", None, None, 555, 1))
        status = delivery.status(member, team)
        self.assertEqual((status["kind"], status["groups_enabled"]), ("private", True))
        self.assertNotIn("verified", status)
        self.assertEqual(delivery.status(owner, team)["state"], "active")

    def test_link_group_route_is_owner_only(self):
        self.store.join_team(self.other, self.store.invite_member(self.owner, self.team, "other", "viewer")["token"])
        api = WorkspaceAPI(self.store, delivery=self.delivery)
        sessions = {actor: self.store.login(name, name + "-password-123")
                    for actor, name in ((self.owner, "owner"), (self.other, "other"))}
        for actor, code in ((self.other, 403), (self.owner, 201)):
            session = sessions[actor]
            headers = Message()
            headers["Cookie"] = "__Host-siyab-session=" + session["session_token"]
            headers["Content-Type"] = "application/json"
            headers["Origin"] = "https://siyab.xyz"
            headers["X-CSRF-Token"] = session["csrf_token"]
            result = api.handle("POST", "/workspace-api/teams/" + self.team + "/delivery/link-group",
                                "203.0.113.1", headers, b"{}")
            self.assertEqual(result[0], code, result[1])
            if actor == self.owner:
                self.assertTrue(result[1]["link"].startswith("https://t.me/siyabonnearbot?startgroup="))
            else:
                with self.store._connection() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM delivery_bindings").fetchone()[0], 0)
        member = self.delivery.status(self.other, self.team)
        self.assertEqual((member["kind"], member["groups_enabled"]), ("group", True))
        for field in ("generation", "verified", "observed_chat_label"):
            self.assertNotIn(field, member)
        with self.assertRaises(WorkspaceError) as caught:
            self.delivery.confirm_link(self.other, self.team, 1)
        self.assertEqual(caught.exception.status, 403)

    def test_verification_is_bounded_outside_transactions_and_ignores_stale_answers(self):
        teams = [self.team, self.store.list_teams(self.other)[0]["id"],
                 self.store.create_team(self.owner, "Group three")["id"], self.store.create_team(self.owner, "Group four")["id"]]
        links = [self.delivery.begin_link(self.other if team == teams[1] else self.owner, team, "group") for team in teams]
        self.client.updates = [self.group_update(link, chat_id=-1001234 - index) for index, link in enumerate(links)]
        self.delivery.run_once()
        self.assertEqual(sum(method == "getChatMember" for method, _ in self.client.calls), 0)
        self.delivery.run_once()
        self.assertEqual(sum(method == "getChatMember" for method, _ in self.client.calls), 3)
        self.assertFalse(self.delivery.status(self.owner, teams[-1])["verified"])
        self.delivery.run_once()
        self.assertEqual(sum(method == "getChatMember" for method, _ in self.client.calls), 4)
        self.assertTrue(self.delivery.status(self.owner, teams[-1])["verified"])
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        original = self.client.call
        replacement = []
        def relink(method, payload):
            if method == "getChatMember":
                replacement.append(self.delivery.begin_link(self.owner, self.team, "group"))
            return original(method, payload)
        with patch.object(self.client, "call", side_effect=relink):
            self.assertIsNone(self.delivery.run_once()["last_error"])
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["verified"], status["observed_chat_label"]), ("pending", False, None))
        self.assertEqual(status["generation"], replacement[0]["generation"])
        self.observe(replacement[0])
        def expire(method, payload):
            if method == "getChatMember":
                self.now += LINK_MS / 1000
            return original(method, payload)
        with patch.object(self.client, "call", side_effect=expire):
            self.assertIsNone(self.delivery.run_once()["last_error"])
        status = self.delivery.status(self.owner, self.team)
        self.assertEqual((status["state"], status["verified"]), ("expired", False))
        with self.store._connection() as connection:
            self.assertIsNone(connection.execute("SELECT verified_ms FROM delivery_bindings WHERE team_id=?", (self.team,)).fetchone()[0])

    def test_member_client_contract_and_group_send_failures_do_not_redirect(self):
        answer = {"status": "administrator", "user": {"id": 555, "is_bot": False}}
        opener = test_delivery.FakeOpener({"ok": True, "result": answer})
        client = TelegramClient(lambda: "123456:" + "a" * 35, opener=opener)
        self.assertEqual(client.call("getChatMember", {"chat_id": -1001234, "user_id": 555}), answer)
        self.assertTrue(opener.requests[0].full_url.endswith("/getChatMember"))
        link = self.delivery.begin_link(self.owner, self.team, "group")
        self.observe(link)
        self.delivery.run_once()
        self.delivery.confirm_link(self.owner, self.team, link["generation"])
        for index, failure in enumerate(({"ok": False, "error_code": 403},
                                          {"ok": False, "error_code": 400, "parameters": {"migrate_to_chat_id": -1009999}})):
            with self.subTest(failure=failure):
                self.now += 3
                self.event(str(index))
                self.delivery._enqueue()
                row = self.delivery._claim()
                opener = test_delivery.FakeOpener(failure)
                self.delivery.client = TelegramClient(lambda: "123456:" + "a" * 35, opener=opener)
                self.assertFalse(self.delivery._send(row))
                stored = self.delivery.list_deliveries(self.owner, self.team)["items"][0]
                self.assertEqual(stored["state"], "dead")
                self.assertEqual(len(opener.requests), 1)
                with self.store._connection() as connection:
                    self.assertEqual(connection.execute("SELECT observed_chat_id FROM delivery_bindings").fetchone()[0], -1001234)


if __name__ == "__main__":
    unittest.main()
