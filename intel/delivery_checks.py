"""Account, token and transaction checks answered in private Telegram chats, behind SIYAB_CHECKS=1 and
SIYAB_TELEGRAM_CHECKS=1."""

import json
import os
from datetime import UTC, datetime

from gate import canon, rpc, txcheck
from gate.txfetch import fetch_transaction, is_hash
from intel import accounts
from intel.delivery import _START, TelegramError

WORDING = {"ALLOW": "No flag found", "DENY": "Flagged", "ASK": "Not enough data"}
MAX_TEXT = 256
MAX_REPLY = 3500
MAX_TICK_CHECKS = 2
MAX_TICK_REPLIES = 10
MAX_DAY_CHAT = 20
MAX_DAY_TOTAL = 300
TRANSACTION_COST = 4
TRANSACTION_DEADLINE_S = 10
UNSIGNED = "Unsigned advice. Nothing was signed. Unknown is never a pass."
TRANSACTION_NOTE = "Actions as FastNEAR reports them; outcome not read. No flag found means no modelled risk found."
HELP = ("Send /account <account id>, /token <token id> or /tx <transaction hash>. A bare mainnet account id "
        "or transaction hash works too.\n" + UNSIGNED)
BUSY = "Siyab is busy."
DAILY_LIMIT = "The daily Telegram check limit was reached. Try again after 00:00 UTC."
_COMMANDS = {"/account": "account", "/token": "token", "/tx": "transaction"}


def _account(value):
    try:
        return canon.account_id(value, "account") == value
    except canon.CanonError:
        return False


def parse(text):
    """(kind, subject), kind account, token, transaction or help; subject None for help."""
    words = text.split() if isinstance(text, str) and len(text) <= MAX_TEXT else []
    if len(words) == 2 and words[0] in _COMMANDS:
        kind = _COMMANDS[words[0]]
        if is_hash(words[1]) if kind == "transaction" else _account(words[1]):
            return kind, words[1]
    if len(words) == 1 and is_hash(words[0]):
        return "transaction", words[0]
    # A bare word is an account only when it is a mainnet named account or an implicit account, so
    # ordinary words get help instead of a check.
    if (len(words) == 1 and _account(words[0]) and not words[0].endswith(".testnet")
            and ("." in words[0] or len(words[0]) == 64)):
        return "account", words[0]
    return "help", None


def reply(kind, subject, answer):
    """The reply text: answer is a check result, a refusal reason or None for a check that could not run."""
    lines, cut = [f"Siyab {kind} check: {subject}"], None
    if isinstance(answer, str):
        cut = len(lines)
        lines.append(answer)
    elif answer is None:
        lines.append("Result: Not enough data")
    else:
        document = json.loads(answer["evidence"])
        sources = dict.fromkeys(check["source"]["provider"] for check in document["checks"])
        block = answer["freshness"].get("block_height")
        wording = WORDING[answer["outcome"]]
        if answer["outcome"] == "ASK" and any(check["id"] == "account_age" and check["status"] == "signal"
                                             for check in document["checks"]):
            wording = "New account, confirm first"
        cut = len(lines) + 1
        lines += ["Result: " + wording, "Reasons: " + "; ".join(" ".join(reason.split()) for reason in answer["reasons"][:3]),
                  "Sources: " + ", ".join(sources), "Block: " + (str(block) if block is not None else "unknown"),
                  "Evidence: " + answer["evidence_hash"]]
        if kind == "transaction":
            lines.append(TRANSACTION_NOTE)
    lines.append(UNSIGNED)
    # Only the reasons, or a refusal reason, are cut, so every other field and the notes stay whole.
    over = len("\n".join(lines)) - MAX_REPLY
    if over > 0:
        lines[cut] = lines[cut][:len(lines[cut]) - over].rstrip()
    return "\n".join(lines)


class TelegramChecks:
    def __init__(self, store, live):
        self.store, self.live = store, live
        with store._connection(write=True) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS telegram_checks (day TEXT NOT NULL,chat_id INTEGER NOT NULL,"
                               "count INTEGER NOT NULL,PRIMARY KEY(day,chat_id))")

    def _count(self, chat_id):
        """Count one check against the UTC day's caps before it runs; False when a cap is reached."""
        day = datetime.fromtimestamp(self.store.clock(), UTC).strftime("%Y-%m-%d")
        with self.store._connection(write=True) as connection:
            connection.execute("DELETE FROM telegram_checks WHERE day<?", (day,))
            total = connection.execute("SELECT COALESCE(SUM(count),0) FROM telegram_checks WHERE day=?", (day,)).fetchone()[0]
            row = connection.execute("SELECT count FROM telegram_checks WHERE day=? AND chat_id=?", (day, chat_id)).fetchone()
            if total >= MAX_DAY_TOTAL or row is not None and row["count"] >= MAX_DAY_CHAT:
                return False
            connection.execute("INSERT INTO telegram_checks(day,chat_id,count) VALUES(?,?,1) "
                               "ON CONFLICT(day,chat_id) DO UPDATE SET count=count+1", (day, chat_id))
        return True

    def _check(self, kind, subject, chat_id):
        key = "telegram:" + str(chat_id)
        if not self._count(chat_id):
            return reply(kind, subject, DAILY_LIMIT)
        if kind != "transaction":
            status, body, _ = self.live.handle(f"/live/{kind}-check/{subject}", key, {})
            if status == 200:
                return reply(kind, subject, body)
            return reply(kind, subject, body["message"] if status in (400, 429) else None)
        refused = self.live._rate_limit(key, TRANSACTION_COST)
        if refused is not None:
            return reply(kind, subject, refused[2] if refused[0] == 429 else None)
        try:
            with rpc.caller_class("anonymous"), rpc.request_deadline(TRANSACTION_DEADLINE_S):
                transaction = fetch_transaction(subject, client=rpc.Client(accounts.TX_URL, "off", kind="http"))
                result = txcheck.check_transaction(transaction, store=self.live.store)
            return reply(kind, subject, result)
        except Exception:  # noqa: BLE001 (as in LiveAPI._attempt, a check that could not run reads Not enough data)
            return reply(kind, subject, None)

    def answer(self, client, updates, renew):
        """Reply at most once to the first message of each private chat in updates. The poll already
        consumed them, so a failed reply is never sent again."""
        if os.environ.get("SIYAB_CHECKS") != "1" or os.environ.get("SIYAB_TELEGRAM_CHECKS") != "1":
            return
        messages = {}
        for update in sorted(updates, key=lambda item: item["update_id"]):
            message = update.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("text"), str):
                continue
            chat, sender = message.get("chat"), message.get("from")
            if (not isinstance(chat, dict) or chat.get("type") != "private"
                    or type(chat.get("id")) is not int or not 1 <= chat["id"] < 2 ** 52
                    or not isinstance(sender, dict) or sender.get("is_bot") is not False
                    or type(sender.get("id")) is not int or sender["id"] != chat["id"]):
                continue
            if _START.fullmatch(message["text"]) is None:
                messages.setdefault(chat["id"], message["text"])
        checks = 0
        for chat_id, text in list(messages.items())[:MAX_TICK_REPLIES]:
            kind, subject = parse(text)
            if kind == "help":
                text = HELP
            elif checks >= MAX_TICK_CHECKS:
                text = BUSY
            else:
                checks += 1
                renew()
                text = self._check(kind, subject, chat_id)
            renew()
            try:
                client.call("sendMessage", {"chat_id": chat_id, "text": text,
                                            "entities": [{"type": "pre", "offset": 0,
                                                          "length": len(text.encode("utf-16-le")) // 2}]})
            except TelegramError as error:
                if error.code == "provider_429":
                    raise
