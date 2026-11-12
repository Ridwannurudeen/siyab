"""Bounded mainnet account reads with explicit RPC and indexed-data provenance."""

import dataclasses
import hashlib
import json
import math
import os
import re
import time
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path

from gate import canon, rpc
from intel import labels, trace

API_URL = "https://api.fastnear.com"
TX_URL = "https://tx.main.fastnear.com"
TRANSFERS_URL = "https://transfers.main.fastnear.com"
READ_TIMEOUT_S = 20
MAX_GRAPH_ACCOUNTS = 6
GRAPH_PAGE_LIMIT = 20
MAX_HOLDINGS_PAGE = 20
_FASTNEAR_HOSTS = {"api.fastnear.com", "tx.main.fastnear.com", "transfers.main.fastnear.com",
                   "free.rpc.fastnear.com", "rpc.mainnet.fastnear.com"}
_UNAVAILABLE = (rpc.FixtureMiss, rpc.HttpError, rpc.TransportError, rpc.RateLimited,
                rpc.MalformedResponse, rpc.RpcError, OSError)
_DECIMAL = re.compile(r"[0-9]{1,39}", re.ASCII)
_SIGNED = re.compile(r"-?[0-9]{1,39}", re.ASCII)
_TIMESTAMP = re.compile(r"[0-9]{1,20}", re.ASCII)


class AccountInputError(ValueError):
    """Account investigation inputs are outside the supported mainnet scope."""


class AccountReadError(ValueError):
    """A provider response cannot establish the requested account data."""

    def __init__(self, message, *, status=None, retry_after=None, now=None):
        super().__init__(message)
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.retry_at_ms = None
        if self.status is None or not isinstance(retry_after, str) or len(retry_after) > 128:
            return
        value = retry_after.strip(" \t")
        if not value or any(ord(char) < 32 or ord(char) > 126 for char in value):
            return
        current = math.ceil((time.time() if now is None else now) * 1000)
        if re.fullmatch(r"[0-9]+", value):
            self.retry_at_ms = min(2 ** 63 - 1, current + int(value) * 1000)
            return
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            self.retry_at_ms = min(2 ** 63 - 1, max(current, math.ceil(parsed.timestamp() * 1000)))
        except (TypeError, ValueError, OverflowError, OSError):
            self.retry_at_ms = None


def _account(value):
    try:
        account = canon.account_id(value, "account")
    except canon.CanonError:
        raise AccountInputError("Enter a valid NEAR mainnet account identifier.") from None
    if account.endswith(".testnet") or account == "testnet":
        raise AccountInputError("Account investigations read mainnet accounts only.")
    return account


def _provider_account(value):
    try:
        return _account(value)
    except AccountInputError:
        raise AccountReadError("The provider returned an invalid account identifier.") from None


def _cursor(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 4096 or any(not 33 <= ord(char) <= 126 for char in value):
        raise AccountInputError("The history cursor is invalid.")
    return value


def _limit(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise AccountInputError("Page size must be between 1 and 100.")
    return value


def _height(value):
    if type(value) is not int or not 0 <= value <= canon.U64_MAX:
        raise AccountReadError("The provider returned an invalid block height.")
    return str(value)


def _amount(value, *, signed=False):
    expression = _SIGNED if signed else _DECIMAL
    if not isinstance(value, str) or not expression.fullmatch(value) or abs(int(value)) > canon.U128_MAX:
        raise AccountReadError("The provider returned an invalid base-unit amount.")
    return value


def _stamp(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value) or int(value) > canon.U64_MAX:
        raise AccountReadError("The provider returned an invalid nanosecond timestamp.")
    return value


def _text(value, field, maximum=256, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise AccountReadError("The provider returned invalid " + field + ".")
    return value


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 128:
        raise AccountReadError("The provider returned an invalid display amount.")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise AccountReadError("The provider returned an invalid display amount.") from None
    if not number.is_finite() or abs(number.adjusted()) > 1000:
        raise AccountReadError("The provider returned an invalid display amount.")
    return str(abs(number))


def _next_cursor(data, current):
    if "resume_token" not in data:
        raise AccountReadError("The provider omitted its pagination cursor.")
    try:
        value = _cursor(data.get("resume_token"))
    except AccountInputError:
        raise AccountReadError("The provider returned an invalid pagination cursor.") from None
    if value is not None and value == current:
        raise AccountReadError("The provider repeated the current pagination cursor.")
    return value


class AccountData:
    def __init__(self, store=None, *, client_factory=rpc.Client, clock=time.time):
        self.store = store if store is not None else labels.load()
        self.client_factory = client_factory
        self.clock = clock
        self._headers = {}
        key_path = os.environ.get("FASTNEAR_API_KEY_FILE")
        if key_path:
            try:
                key = Path(key_path).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                raise AccountReadError("The configured FastNEAR credential file could not be read.") from None
            if not key or "\r" in key or "\n" in key:
                raise AccountReadError("The configured FastNEAR credential file is invalid.")
            self._headers = {"Authorization": "Bearer " + key}

    def _client(self, url, *, kind="rpc"):
        parsed = urllib.parse.urlsplit(url)
        headers = self._headers if parsed.scheme == "https" and parsed.netloc in _FASTNEAR_HOSTS else {}
        return self.client_factory(url, "off", kind=kind, headers=headers)

    def _observed(self):
        return datetime.fromtimestamp(self.clock(), UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _source(client, kind):
        return dict(client.last_source, read_kind=kind, recorded=False, network="mainnet")

    def profile(self, account):
        account = _account(account)
        client = self._client(os.environ.get("NEAR_MAINNET_RPC_URL") or rpc.MAINNET_RPC_URL)
        exists = True
        with rpc.request_deadline(READ_TIMEOUT_S):
            try:
                value = client.view_account(account)
            except rpc.RpcError as error:
                if error.cause != "UNKNOWN_ACCOUNT":
                    raise AccountReadError("The finalized account state could not be read.") from None
                value = None
                exists = False
            except TimeoutError:
                raise
            except _UNAVAILABLE:
                raise AccountReadError("The finalized account state could not be read.") from None
        native = None
        if exists:
            if not isinstance(value, dict) or type(value.get("storage_usage")) is not int or not 0 <= value["storage_usage"] <= canon.U64_MAX:
                raise AccountReadError("The finalized account state is incomplete.")
            native = {"amount_yocto": _amount(value.get("amount")), "locked_yocto": _amount(value.get("locked")),
                      "storage_usage": value["storage_usage"], "code_hash": _text(value.get("code_hash"), "code hash", 100),
                      "block_height": _height(value.get("block_height")),
                      "block_hash": _text(value.get("block_hash"), "block hash", 100)}
        current_labels = [dataclasses.asdict(row) for row in self.store.by_subject(account, "near") if row.superseded_by is None]
        return {"account_id": account, "network": "mainnet", "exists": exists, "native": native,
                "labels": current_labels, "source": self._source(client, "final_rpc"), "observed_at": self._observed(),
                "coverage": {"scope": "Finalized account state; account balance includes storage backing."
                             " Delegated staking and token holdings are separate reads.",
                             "labels": "Committed assertions with their own observation dates; not a payment verdict."}}

    def holdings(self, account, offset=0, limit=20):
        account, limit = _account(account), _limit(limit)
        if limit > MAX_HOLDINGS_PAGE:
            raise AccountInputError("Holdings page size must not exceed 20.")
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise AccountInputError("Holdings offset must be between 0 and 10000.")
        index = self._client(API_URL, kind="http")
        with rpc.request_deadline(READ_TIMEOUT_S):
            try:
                data = index.get("/v1/account/" + account + "/ft")
            except TimeoutError:
                raise
            except _UNAVAILABLE:
                raise AccountReadError("The indexed token inventory could not be read.") from None
            if not isinstance(data, dict) or data.get("account_id") != account or not isinstance(data.get("tokens"), list) or len(data["tokens"]) > 10000:
                raise AccountReadError("The indexed token inventory is incomplete.")
            tokens = []
            for row in data["tokens"]:
                if not isinstance(row, dict) or not {"contract_id", "balance", "last_update_block_height"} <= row.keys():
                    raise AccountReadError("The indexed token inventory is malformed.")
                contract = _provider_account(row.get("contract_id"))
                balance = row.get("balance")
                status = "pending" if balance is None else "unavailable" if balance == "" else "indexed"
                balance = None if balance in (None, "") else _amount(balance)
                height = row.get("last_update_block_height")
                tokens.append({"contract_id": contract, "balance": balance, "balance_status": status,
                               "last_update_block_height": None if height is None else _height(height)})
            if len({row["contract_id"] for row in tokens}) != len(tokens):
                raise AccountReadError("The indexed token inventory contains duplicate contracts.")
            tokens.sort(key=lambda row: row["contract_id"])
            selected = tokens[offset:offset + limit]
            timed_out = False
            for row in selected:
                row.update(metadata=None, metadata_source=None)
                if timed_out:
                    continue
                client = self._client(os.environ.get("NEAR_MAINNET_RPC_URL") or rpc.MAINNET_RPC_URL)
                try:
                    metadata = client.call_function(row["contract_id"], "ft_metadata", {})
                    if not isinstance(metadata, dict) or type(metadata.get("decimals")) is not int or not 0 <= metadata["decimals"] <= 255:
                        raise AccountReadError("Token metadata is incomplete.")
                    row["metadata"] = {"name": _text(metadata.get("name"), "token name"),
                                       "symbol": _text(metadata.get("symbol"), "token symbol", 64),
                                       "decimals": metadata["decimals"]}
                    row["metadata_source"] = dict(self._source(client, "final_rpc"), block_height=_height(client.last_block_height))
                except TimeoutError:
                    timed_out = True
                except (*_UNAVAILABLE, AccountReadError):
                    row.update(metadata=None, metadata_source=None)
        return {"account_id": account, "network": "mainnet", "items": selected,
                "pagination": {"offset": offset, "limit": limit, "total": len(tokens),
                               "next_offset": offset + limit if offset + limit < len(tokens) else None},
                "source": self._source(index, "indexed_inventory"), "observed_at": self._observed(),
                "coverage": {"scope": "Indexed FT inventory and last-known balances; not a block-pinned portfolio."
                             " Inventory may change between pages. Metadata is a separate finalized RPC read.",
                             "metadata_unavailable": sum(row["metadata"] is None for row in selected),
                             "metadata_timed_out": timed_out}}

    def transactions(self, account, cursor=None, limit=25):
        account, cursor, limit = _account(account), _cursor(cursor), _limit(limit)
        client = self._client(TX_URL, kind="http")
        body = {"account_id": account, "desc": True, "limit": limit}
        if cursor is not None:
            body["resume_token"] = cursor
        with rpc.request_deadline(READ_TIMEOUT_S):
            try:
                data = client.post("/v0/account", body)
            except TimeoutError:
                raise
            except rpc.HttpError as error:
                raise AccountReadError("The indexed transaction history could not be read.", status=error.status, retry_after=error.retry_after, now=self.clock()) from None
            except _UNAVAILABLE:
                raise AccountReadError("The indexed transaction history could not be read.") from None
        if not isinstance(data, dict) or not isinstance(data.get("account_txs"), list) or len(data["account_txs"]) > limit:
            raise AccountReadError("The indexed transaction page is malformed.")
        items = []
        flags = ("is_signer", "is_delegated_signer", "is_real_signer", "is_any_signer", "is_predecessor",
                 "is_explicit_refund_to", "is_receiver", "is_real_receiver", "is_function_call", "is_action_arg", "is_event_log", "is_success")
        for row in data["account_txs"]:
            if not isinstance(row, dict) or row.get("account_id") != account:
                raise AccountReadError("The indexed transaction page names a different account.")
            stamp = _stamp(row.get("tx_block_timestamp"))
            item = {"account_id": account, "transaction_hash": _text(row.get("transaction_hash"), "transaction hash", 100),
                    "tx_block_height": _height(row.get("tx_block_height")), "tx_block_timestamp": stamp,
                    "timestamp_ms": int(stamp) // 1000000}
            if type(row.get("tx_index")) is not int or not 0 <= row["tx_index"] <= 2 ** 32 - 1:
                raise AccountReadError("The indexed transaction position is invalid.")
            item["tx_index"] = row["tx_index"]
            for flag in flags:
                if type(row.get(flag)) is not bool:
                    raise AccountReadError("The indexed transaction participation flag is invalid.")
                item[flag] = row[flag]
            items.append(item)
        return {"account_id": account, "network": "mainnet", "items": items,
                "pagination": {"limit": limit, "next_cursor": _next_cursor(data, cursor)},
                "source": self._source(client, "indexed_history"), "observed_at": self._observed(),
                "coverage": {"scope": "One indexed transaction page, newest first. Participation flags do not establish a transfer amount or counterparty."}}

    def _transfer_page(self, account, cursor, limit, *, until=None, since=None):
        client = self._client(TRANSFERS_URL, kind="http")
        body = {"account_id": account, "desc": True, "limit": limit}
        if cursor is not None:
            body["resume_token"] = cursor
        if until is not None:
            body["to_timestamp_ms"] = until
        if since is not None:
            body["from_timestamp_ms"] = since
        try:
            data = client.post("/v0/transfers", body)
        except TimeoutError:
            raise
        except rpc.HttpError as error:
            raise AccountReadError("The indexed transfer history could not be read.", status=error.status, retry_after=error.retry_after, now=self.clock()) from None
        except _UNAVAILABLE:
            raise AccountReadError("The indexed transfer history could not be read.") from None
        if not isinstance(data, dict) or not isinstance(data.get("transfers"), list) or len(data["transfers"]) > limit:
            raise AccountReadError("The indexed transfer page is malformed.")
        source = self._source(client, "indexed_transfers")
        items = []
        for row in data["transfers"]:
            if not isinstance(row, dict) or row.get("account_id") != account:
                raise AccountReadError("The indexed transfer page names a different account.")
            amount = _amount(row.get("amount"), signed=True)
            stamp = _stamp(row.get("block_timestamp"))
            height = row.get("block_height")
            if not isinstance(height, str) or not _TIMESTAMP.fullmatch(height) or int(height) > canon.U64_MAX:
                raise AccountReadError("The indexed transfer block is invalid.")
            other = row.get("other_account_id")
            if other is not None:
                other = _provider_account(other)
            if type(row.get("transfer_index")) is not int or not 0 <= row["transfer_index"] <= 2 ** 32 - 1:
                raise AccountReadError("The indexed transfer position is invalid.")
            outgoing = amount.startswith("-")
            item = {"account_id": account, "other_account_id": other, "from": account if outgoing else other,
                    "to": other if outgoing else account, "amount": str(abs(int(amount))), "signed_amount": amount,
                    "asset_id": _text(row.get("asset_id"), "asset identifier"),
                    "human_amount": _number(row.get("human_amount")), "usd_amount": _number(row.get("usd_amount")),
                    "transaction_hash": _text(row.get("transaction_id"), "transaction hash", 100, nullable=True),
                    "receipt_id": _text(row.get("receipt_id"), "receipt identifier", 100),
                    "transfer_index": row["transfer_index"], "block_height": height, "block_timestamp": stamp,
                    "timestamp_ms": int(stamp) // 1000000,
                    "method_name": _text(row.get("method_name"), "method name", 256, nullable=True), "source": source}
            item["amount_decimals"] = trace.asset_decimals(item["asset_id"])
            identity = {key: item[key] for key in ("receipt_id", "transfer_index", "asset_id", "from", "to", "amount")}
            item["id"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            items.append(item)
        return {"account_id": account, "network": "mainnet", "items": items,
                "pagination": {"limit": limit, "next_cursor": _next_cursor(data, cursor)},
                "source": source, "observed_at": self._observed(),
                "coverage": {"scope": "One indexed transfer page, both directions, newest first. Missing counterparties and USD values remain unknown."}}

    def transfers(self, account, cursor=None, limit=25):
        account, cursor, limit = _account(account), _cursor(cursor), _limit(limit)
        with rpc.request_deadline(READ_TIMEOUT_S):
            return self._transfer_page(account, cursor, limit)

    def history_page(self, account, kind, cursor=None, limit=100, *, from_timestamp_ms=None, to_timestamp_ms=None):
        account, cursor, limit = _account(account), _cursor(cursor), _limit(limit)
        if kind not in ("transactions", "transfers"):
            raise AccountInputError("Historical pages contain transactions or transfers.")
        for value in (from_timestamp_ms, to_timestamp_ms):
            if value is not None and (type(value) is not int or not 0 <= value <= canon.U64_MAX // 1000000):
                raise AccountInputError("Historical timestamps must be integer milliseconds within the chain timestamp range.")
        if from_timestamp_ms is not None and to_timestamp_ms is not None and from_timestamp_ms >= to_timestamp_ms:
            raise AccountInputError("The historical date window must have increasing bounds.")
        with rpc.request_deadline(READ_TIMEOUT_S):
            result = (self._transfer_page(account, cursor, limit, since=from_timestamp_ms, until=to_timestamp_ms)
                      if kind == "transfers" else self.transactions(account, cursor=cursor, limit=limit))
        result["coverage"]["requested_window"] = {"from_timestamp_ms": from_timestamp_ms, "to_timestamp_ms": to_timestamp_ms}
        result["coverage"]["date_filter"] = "provider_time_bounds" if kind == "transfers" else "worker_filters_bounded_newest_first_scan"
        return result

    def neighborhood(self, account, depth=1):
        account = _account(account)
        if type(depth) is not int or not 1 <= depth <= 2:
            raise AccountInputError("Neighborhood depth must be 1 or 2.")
        depths, frontier, queried = {account: 0}, [account], []
        records, seen, grouped, sources = [], set(), {}, []
        more_pages = False
        unknown = 0
        until = int(self.clock() * 1000)
        with rpc.request_deadline(READ_TIMEOUT_S):
            for level in range(depth):
                following = []
                for subject in frontier:
                    if len(queried) >= MAX_GRAPH_ACCOUNTS:
                        break
                    page = self._transfer_page(subject, None, GRAPH_PAGE_LIMIT, until=until)
                    queried.append(subject)
                    sources.append(page["source"])
                    more_pages = more_pages or page["pagination"]["next_cursor"] is not None
                    for item in page["items"]:
                        if item["from"] is None or item["to"] is None:
                            unknown += 1
                            continue
                        for endpoint in (item["from"], item["to"]):
                            if endpoint not in depths:
                                depths[endpoint] = level + 1
                                following.append(endpoint)
                        if item["id"] in seen:
                            continue
                        seen.add(item["id"])
                        record = dict(item, block=int(item["block_height"]), depth=level + 1,
                                      provenance="onchain", confidence=None)
                        records.append(record)
                        key = (item["from"], item["to"], item["asset_id"])
                        if key not in grouped:
                            grouped[key] = {"id": "edge-" + str(len(grouped)), "from": key[0], "to": key[1],
                                            "asset_id": key[2], "record_ids": [], "provenance": "onchain", "confidence": None}
                        grouped[key]["record_ids"].append(item["id"])
                frontier = following
        return {"kind": "flow", "root": account,
                "nodes": [{"id": subject, "label": subject, "url": "/investigate.html?account=" + subject,
                           "depth": level} for subject, level in depths.items()],
                "edges": list(grouped.values()), "records": records,
                "total_nodes": len(depths), "total_edges": len(grouped), "total_records": len(records),
                "bounds": {"requested_depth": depth, "reached_depth": max(depths.values()),
                           "window": {"to_timestamp_ms": until, "rule": "Before the observation time, latest indexed transfers only"}},
                "coverage": {"truncated": True, "completeness": {"reason": "bounded live observation; not complete account history"},
                             "filters": {"limit_per_account": GRAPH_PAGE_LIMIT, "to_timestamp_ms": until},
                             "terminal_accounts": [], "queried_accounts": queried, "requests": len(queried),
                             "more_provider_pages": more_pages, "unknown_counterparty_rows": unknown,
                             "unexpanded_accounts": [subject for subject, level in depths.items() if level < depth and subject not in queried]},
                "source": {"provider": "FastNEAR Transfers API", "fetched_at": self._observed(), "recorded": False,
                           "network": "mainnet", "reads": sources}, "assertion_ids": [], "shares": {}, "state_at_block": []}
