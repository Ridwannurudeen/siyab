"""Sourced native/FT valuations and retained, actual portfolio observations."""

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from urllib.parse import urlsplit

from gate import canon, rpc
from intel.accounts import AccountData, AccountReadError

PRICE_URL = "https://coins.llama.fi"
MAX_TOKENS = 100
MAX_HISTORICAL_TOKENS = 20
MAX_PRICE_AGE_S = 3600
MIN_PRICE_CONFIDENCE = Decimal("0.8")
MAX_SNAPSHOTS = 10000
MAX_RETAINED_BYTES = 128 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024
_HASH = re.compile(r"[a-f0-9]{64}")
_AMOUNT = re.compile(r"[0-9]{1,39}")
_UNAVAILABLE = (
    rpc.FixtureMiss,
    rpc.HttpError,
    rpc.TransportError,
    rpc.RateLimited,
    rpc.MalformedResponse,
    rpc.RpcError,
    AccountReadError,
    OSError,
)
_PRIVATE = {
    "password",
    "password_hash",
    "password_salt",
    "session_token",
    "csrf_token",
    "recovery_code",
    "authorization",
    "cookie",
    "notes",
    "watchlist",
    "team_id",
    "user_id",
}


class PortfolioInputError(ValueError):
    """An input does not identify a supported public mainnet portfolio read."""


class PortfolioReadError(ValueError):
    """A source cannot establish the requested portfolio observation."""


class PortfolioCapacityError(PortfolioReadError):
    """A durable portfolio observation or retention budget has been reached."""


def _account(value):
    try:
        result = canon.account_id(value, "account")
    except canon.CanonError:
        raise PortfolioInputError("Use a valid mainnet account identifier.") from None
    if result == "testnet" or result.endswith(".testnet"):
        raise PortfolioInputError("Portfolio reads support mainnet accounts only.")
    return result


def _number(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int, float))
        or len(str(value)) > 128
    ):
        raise PortfolioReadError("A provider number is invalid.")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise PortfolioReadError("A provider number is invalid.") from None
    if not result.is_finite() or result < 0 or abs(result.adjusted()) > 300:
        raise PortfolioReadError("A provider number is outside the supported range.")
    return result


def _amount(value):
    if (
        not isinstance(value, str)
        or not _AMOUNT.fullmatch(value)
        or int(value) > canon.U128_MAX
    ):
        raise PortfolioReadError("An asset balance is not an exact base-unit amount.")
    return value


def _decimal(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _time(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def _milliseconds(value):
    if not isinstance(value, str) or len(value) > 40:
        raise PortfolioInputError("An observation must name a UTC timestamp.")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError("not UTC")
        result = int(stamp.timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        raise PortfolioInputError("An observation must name a UTC timestamp.") from None
    if result < 0:
        raise PortfolioInputError(
            "An observation timestamp predates the supported range."
        )
    return result


def _public(value, depth=0):
    if depth > 24:
        raise PortfolioInputError("The observation exceeds the nesting limit.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in _PRIVATE:
                raise PortfolioInputError(
                    "Private workspace fields cannot enter a public portfolio observation."
                )
            _public(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _public(item, depth + 1)


def _body(value):
    _public(value)
    try:
        body = (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise PortfolioInputError(
            "A portfolio observation must contain valid UTF-8 JSON values."
        ) from None
    if len(body) > MAX_SNAPSHOT_BYTES:
        raise PortfolioInputError("The portfolio observation exceeds its byte limit.")
    return body


def _settled(value):
    if isinstance(value, dict):
        return {
            key: _settled(item)
            for key, item in value.items()
            if key not in ("observed_at", "fetched_at")
        }
    if isinstance(value, list):
        return [_settled(item) for item in value]
    return value


def _comparable(value):
    value = _settled(value)
    if value["kind"] == "current":
        value.pop("effective_at")
        for asset in value["assets"]:
            details = asset.get("details")
            if isinstance(details, dict):
                details.pop("block_height", None)
                if isinstance(details.get("metadata_source"), dict):
                    details["metadata_source"].pop("block_height", None)
                    details["metadata_source"].pop("block_hash", None)
    return _body(value)


def _metadata(value):
    if (
        not isinstance(value, dict)
        or type(value.get("decimals")) is not int
        or not 0 <= value["decimals"] <= 255
    ):
        return None
    if any(
        not isinstance(value.get(key), str) or len(value[key]) > maximum
        for key, maximum in (("name", 256), ("symbol", 64))
    ):
        return None
    return {
        "name": value["name"],
        "symbol": value["symbol"],
        "decimals": value["decimals"],
        "provenance": "contract_reported",
    }


class PortfolioData:
    def __init__(self, reader=None, *, client_factory=rpc.Client, clock=time.time):
        self.reader = reader if reader is not None else AccountData()
        self.client_factory, self.clock = client_factory, clock

    @staticmethod
    def _asset(identifier, balance, metadata, source, *, status, details=None):
        decimals = metadata["decimals"] if metadata is not None else None
        if balance is not None:
            balance = _amount(balance)
        with localcontext() as context:
            context.prec = 1024
            human = (
                _decimal(Decimal(balance).scaleb(-decimals))
                if balance is not None and decimals is not None
                else None
            )
        return {
            "asset_id": identifier,
            "contract_id": None if identifier == "native:near" else identifier[7:],
            "balance_base_units": balance,
            "decimals": decimals,
            "balance": human,
            "metadata": metadata,
            "balance_status": status,
            "balance_source": source,
            "details": details or {},
            "price": {
                "usd": None,
                "timestamp": None,
                "confidence": None,
                "status": "missing",
                "source": None,
            },
            "value_usd": None,
            "status": "balance_unavailable"
            if balance is None
            else "decimals_unavailable"
            if decimals is None
            else "price_unavailable",
        }

    def _native(self, account):
        try:
            data = self.reader.profile(account)
        except _UNAVAILABLE:
            return self._asset(
                "native:near",
                None,
                {
                    "name": "NEAR",
                    "symbol": "NEAR",
                    "decimals": 24,
                    "provenance": "protocol",
                },
                None,
                status="unavailable",
            ), None
        if (
            not isinstance(data, dict)
            or data.get("account_id") != account
            or data.get("network") != "mainnet"
        ):
            raise PortfolioReadError(
                "The native observation names a different account or network."
            )
        value = data.get("native")
        if data.get("exists") is not True or not isinstance(value, dict):
            return self._asset(
                "native:near",
                None,
                {
                    "name": "NEAR",
                    "symbol": "NEAR",
                    "decimals": 24,
                    "provenance": "protocol",
                },
                data.get("source"),
                status="account_absent",
            ), data.get("source")
        total = int(_amount(value.get("amount_yocto"))) + int(
            _amount(value.get("locked_yocto"))
        )
        if total > canon.U128_MAX:
            raise PortfolioReadError(
                "The combined native balance exceeds the protocol range."
            )
        return self._asset(
            "native:near",
            str(total),
            {
                "name": "NEAR",
                "symbol": "NEAR",
                "decimals": 24,
                "provenance": "protocol",
            },
            data.get("source"),
            status="finalized",
            details={
                "account_amount_yocto": value["amount_yocto"],
                "locked_yocto": value["locked_yocto"],
                "block_height": value.get("block_height"),
                "storage_usage": value.get("storage_usage"),
                "scope": "Account amount plus protocol-locked balance, counted once. Account amount includes storage backing.",
            },
        ), data.get("source")

    def _price(self, assets, target, historical):
        coins = {
            "coingecko:near"
            if asset["asset_id"] == "native:near"
            else "near:" + asset["contract_id"]: asset
            for asset in assets
        }
        path = (
            "/prices/"
            + ("historical/" + str(target) + "/" if historical else "current/")
            + ",".join(coins)
        )
        client = self.client_factory(PRICE_URL, "off", kind="http")
        source, values = None, {}
        try:
            response = client.get(path)
            if (
                not isinstance(response, dict)
                or not isinstance(response.get("coins"), dict)
                or len(response["coins"]) > 10000
            ):
                raise PortfolioReadError("The price response is invalid.")
            values = response["coins"]
            source = {
                "provider": "DefiLlama Coins API",
                "url": PRICE_URL + path,
                "fetched_at": _time(self.clock()),
                "request_sha256": (client.last_source or {}).get("request_sha256"),
                "response_sha256": hashlib.sha256(
                    json.dumps(
                        response, sort_keys=True, separators=(",", ":"), allow_nan=False
                    ).encode()
                ).hexdigest(),
                "mapping": "Native NEAR uses the Near chain gasTokenGeckoId; FT assets use exact near:contract identifiers.",
            }
        except (*_UNAVAILABLE, PortfolioReadError, ValueError, TypeError):
            for asset in assets:
                asset["price"]["status"] = "provider_unavailable"
            return None
        for coin, asset in coins.items():
            value = values.get(coin)
            price = asset["price"]
            price["source"] = source
            if value is None:
                continue
            try:
                if not isinstance(value, dict):
                    raise PortfolioReadError("A price entry is invalid.")
                usd, confidence = (
                    _number(value.get("price")),
                    _number(value.get("confidence")),
                )
                stamp = value.get("timestamp")
                if type(stamp) is not int or stamp < 0 or confidence > 1:
                    raise PortfolioReadError("A price observation is invalid.")
                price.update(
                    usd=_decimal(usd),
                    timestamp=stamp,
                    confidence=_decimal(confidence),
                    status="known",
                )
                if confidence < MIN_PRICE_CONFIDENCE:
                    price["status"] = "low_confidence"
                elif stamp > target + (0 if historical else 60):
                    price["status"] = "future"
                elif abs(target - stamp) > MAX_PRICE_AGE_S:
                    price["status"] = "stale"
                elif value.get("decimals") is not None and (
                    type(value["decimals"]) is not int
                    or value["decimals"] != asset["decimals"]
                ):
                    price["status"] = (
                        "decimals_mismatch"
                        if asset["decimals"] is not None
                        else "known"
                    )
                if price["status"] == "known" and asset["balance"] is not None:
                    with localcontext() as context:
                        context.prec = 1024
                        asset["value_usd"] = _decimal(Decimal(asset["balance"]) * usd)
                    asset["status"] = "priced"
            except PortfolioReadError:
                price.update(
                    usd=None, timestamp=None, confidence=None, status="invalid"
                )
        return source

    def _result(
        self,
        account,
        assets,
        sources,
        *,
        kind,
        target,
        inventory_complete,
        tokens_total,
        tokens_read,
        details=None,
    ):
        price_source = self._price(assets, target, kind == "historical")
        if price_source is not None:
            sources.append(price_source)
        priced = [asset for asset in assets if asset["value_usd"] is not None]
        with localcontext() as context:
            context.prec = 1024
            known = (
                _decimal(
                    sum((Decimal(asset["value_usd"]) for asset in priced), Decimal(0))
                )
                if priced
                else None
            )
        total = known if inventory_complete and len(priced) == len(assets) else None
        scope = hashlib.sha256(
            json.dumps(
                sorted(asset["asset_id"] for asset in assets), separators=(",", ":")
            ).encode()
        ).hexdigest()
        return {
            "account_id": account,
            "network": "mainnet",
            "kind": kind,
            "observed_at": _time(self.clock()),
            "effective_at": _time(target),
            "asset_scope_id": scope,
            "assets": assets,
            "summary": {
                "known_value_usd": known,
                "total_value_usd": total,
                "priced_assets": len(priced),
                "unpriced_assets": len(assets) - len(priced),
            },
            "sources": sources,
            "coverage": {
                "inventory_complete": inventory_complete,
                "tokens_total": tokens_total,
                "tokens_read": tokens_read,
                "history_complete": False,
                "prices_max_age_seconds": MAX_PRICE_AGE_S,
                "prices_min_confidence": str(MIN_PRICE_CONFIDENCE),
                "scope": "Native account balance plus observed FT wallet balances. USD values are source-based estimates, not spendable balances.",
                "excluded": "Delegated staking, NFTs, DeFi deposits and balances outside the observed token inventory are excluded.",
                "history": "Only actual retained observations or explicitly block-pinned balances are charted. No current balance is multiplied by an older price to invent wallet history.",
                **(details or {}),
            },
        }

    def inspect(self, account, max_tokens=40):
        account = _account(account)
        if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS:
            raise PortfolioInputError("Read from 1 to 100 indexed tokens.")
        with rpc.request_deadline(20):
            with rpc.request_deadline(14):
                native, native_source = self._native(account)
                assets, sources = [native], [native_source] if native_source else []
                offset, total, complete, tokens = 0, None, False, set()
                while len(tokens) < max_tokens:
                    limit = min(20, max_tokens - len(tokens))
                    try:
                        page = self.reader.holdings(account, offset=offset, limit=limit)
                    except _UNAVAILABLE:
                        break
                    if (
                        not isinstance(page, dict)
                        or page.get("account_id") != account
                        or page.get("network") != "mainnet"
                        or not isinstance(page.get("items"), list)
                        or len(page["items"]) > limit
                        or not isinstance(page.get("pagination"), dict)
                    ):
                        raise PortfolioReadError(
                            "The indexed portfolio page is invalid."
                        )
                    pagination = page["pagination"]
                    if (
                        type(pagination.get("total")) is not int
                        or not 0 <= pagination["total"] <= 10000
                    ):
                        raise PortfolioReadError(
                            "The indexed token inventory size is invalid."
                        )
                    if total is not None and total != pagination["total"]:
                        raise PortfolioReadError(
                            "The token inventory changed during pagination; retry the observation."
                        )
                    total = pagination["total"]
                    for row in page["items"]:
                        if not isinstance(row, dict):
                            raise PortfolioReadError("An indexed token row is invalid.")
                        contract = _account(row.get("contract_id"))
                        if contract in tokens:
                            raise PortfolioReadError(
                                "The indexed token inventory repeats a contract."
                            )
                        tokens.add(contract)
                        assets.append(
                            self._asset(
                                "nep141:" + contract,
                                row.get("balance"),
                                _metadata(row.get("metadata")),
                                page.get("source"),
                                status=row.get("balance_status", "unavailable"),
                                details={
                                    "last_update_block_height": row.get(
                                        "last_update_block_height"
                                    ),
                                    "metadata_source": row.get("metadata_source"),
                                },
                            )
                        )
                    if page.get("source") is not None:
                        sources.append(page["source"])
                    following = pagination.get("next_offset")
                    if following is None:
                        complete = len(tokens) == total
                        break
                    if (
                        type(following) is not int
                        or following <= offset
                        or following > 10000
                        or not page["items"]
                    ):
                        raise PortfolioReadError(
                            "The indexed portfolio cursor did not advance."
                        )
                    offset = following
            return self._result(
                account,
                assets,
                sources,
                kind="current",
                target=int(self.clock()),
                inventory_complete=complete,
                tokens_total=total,
                tokens_read=len(tokens),
                details={
                    "balances": "Native state is finalized; indexed FT balances and their metadata are separate provider observations."
                },
            )

    def historical(self, account, block_height, contracts=None):
        account = _account(account)
        if type(block_height) is not int or not 1 <= block_height <= canon.U64_MAX:
            raise PortfolioInputError("Use a positive historical block height.")
        contracts = [] if contracts is None else contracts
        if not isinstance(contracts, list) or len(contracts) > MAX_HISTORICAL_TOKENS:
            raise PortfolioInputError("Select at most 20 historical token contracts.")
        contracts = [_account(contract) for contract in contracts]
        if len(set(contracts)) != len(contracts):
            raise PortfolioInputError("Historical token contracts must be distinct.")
        endpoint = os.environ.get("NEAR_ARCHIVAL_RPC_URL") or rpc.ARCHIVAL_RPC_URL
        headers = {}
        parsed = urlsplit(endpoint)
        key_path = os.environ.get("FASTNEAR_API_KEY_FILE")
        if (
            key_path
            and parsed.scheme == "https"
            and parsed.netloc == "archival-rpc.mainnet.fastnear.com"
        ):
            try:
                key = Path(key_path).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise PortfolioReadError(
                    "The configured archival credential file could not be read."
                ) from None
            if not key or "\r" in key or "\n" in key:
                raise PortfolioReadError(
                    "The configured archival credential file is invalid."
                )
            headers = {"Authorization": "Bearer " + key}
        client = self.client_factory(endpoint, "off", headers=headers)
        with rpc.request_deadline(20):
            with rpc.request_deadline(14):
                try:
                    block = client.block_at(block_height)
                    header = block.get("header") if isinstance(block, dict) else None
                    if (
                        not isinstance(header, dict)
                        or header.get("height") != block_height
                        or type(header.get("timestamp")) is not int
                        or not 0 <= header["timestamp"] <= canon.U64_MAX
                    ):
                        raise PortfolioReadError(
                            "The historical block response does not match the requested height."
                        )
                    target = header["timestamp"] // 1000000000
                    if target > int(self.clock()):
                        raise PortfolioReadError(
                            "The historical block timestamp is in the future."
                        )
                    block_source = {
                        **(client.last_source or {}),
                        "read_kind": "archival_block",
                        "network": "mainnet",
                        "block_height": str(block_height),
                        "block_timestamp_ns": str(header["timestamp"]),
                        "block_hash": header.get("hash"),
                    }
                    native = client.view_account_at(account, block_height)
                    if (
                        not isinstance(native, dict)
                        or native.get("block_height") != block_height
                        or native.get("block_hash") != header.get("hash")
                    ):
                        raise PortfolioReadError(
                            "The historical account response names a different block."
                        )
                    amount = int(_amount(native.get("amount"))) + int(
                        _amount(native.get("locked"))
                    )
                    if amount > canon.U128_MAX:
                        raise PortfolioReadError(
                            "The combined historical native balance exceeds the protocol range."
                        )
                except PortfolioReadError:
                    raise
                except _UNAVAILABLE:
                    raise PortfolioReadError(
                        "The requested archival account and block could not be read."
                    ) from None
                source = {
                    **(client.last_source or {}),
                    "read_kind": "archival_rpc",
                    "network": "mainnet",
                    "block_height": str(block_height),
                }
                assets = [
                    self._asset(
                        "native:near",
                        str(amount),
                        {
                            "name": "NEAR",
                            "symbol": "NEAR",
                            "decimals": 24,
                            "provenance": "protocol",
                        },
                        source,
                        status="historical",
                        details={
                            "account_amount_yocto": native["amount"],
                            "locked_yocto": native["locked"],
                            "block_height": str(block_height),
                        },
                    )
                ]
                sources = [block_source, source]
                for contract in contracts:
                    balance, metadata, read_source, metadata_source = (
                        None,
                        None,
                        None,
                        None,
                    )
                    try:
                        balance = _amount(
                            client.call_function(
                                contract,
                                "ft_balance_of",
                                {"account_id": account},
                                block_id=block_height,
                            )
                        )
                        if client.last_block_height != block_height:
                            raise PortfolioReadError(
                                "The token balance response names a different historical block."
                            )
                        read_source = {
                            **(client.last_source or {}),
                            "read_kind": "archival_rpc",
                            "network": "mainnet",
                            "block_height": str(block_height),
                        }
                        metadata = _metadata(
                            client.call_function(
                                contract, "ft_metadata", {}, block_id=block_height
                            )
                        )
                        if client.last_block_height != block_height:
                            raise PortfolioReadError(
                                "The token metadata response names a different historical block."
                            )
                        metadata_source = {
                            **(client.last_source or {}),
                            "read_kind": "archival_metadata",
                            "network": "mainnet",
                            "block_height": str(block_height),
                        }
                    except _UNAVAILABLE:
                        metadata = None
                    if read_source is not None:
                        sources.append(read_source)
                    if metadata_source is not None:
                        sources.append(metadata_source)
                    assets.append(
                        self._asset(
                            "nep141:" + contract,
                            balance,
                            metadata,
                            read_source,
                            status="historical"
                            if balance is not None
                            else "unavailable",
                            details={"metadata_source": metadata_source},
                        )
                    )
            return self._result(
                account,
                assets,
                sources,
                kind="historical",
                target=target,
                inventory_complete=False,
                tokens_total=None,
                tokens_read=len(contracts),
                details={
                    "block_height": str(block_height),
                    "balances": "Block-pinned account and explicitly selected FT contracts only.",
                    "selected_contracts": contracts,
                    "excluded_historical_tokens": "Contracts not explicitly selected may have held balances at that block.",
                },
            )


class PortfolioHistory:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        if self.path.is_symlink():
            raise PortfolioInputError(
                "The portfolio database cannot be a symbolic link."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise PortfolioReadError(
                    "The portfolio database schema is unsupported."
                )
            if version == 0:
                connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS observations(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,account TEXT NOT NULL,kind TEXT NOT NULL,effective_ms INTEGER NOT NULL,observed_ms INTEGER NOT NULL,body BLOB NOT NULL);
                CREATE INDEX IF NOT EXISTS observation_account ON observations(account,effective_ms DESC,seq DESC);
                CREATE TABLE IF NOT EXISTS capture_budget(account TEXT NOT NULL,window INTEGER NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(account,window));
                PRAGMA user_version=1;
                COMMIT;
                """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self, write=False):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA max_page_count=65536")
        connection.execute("PRAGMA journal_size_limit=16777216")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _validate(payload):
        allowed = {
            "account_id",
            "network",
            "kind",
            "observed_at",
            "effective_at",
            "asset_scope_id",
            "assets",
            "summary",
            "sources",
            "coverage",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != allowed
            or payload.get("network") != "mainnet"
            or payload.get("kind") not in ("current", "historical")
            or not isinstance(payload.get("assets"), list)
            or not 1 <= len(payload["assets"]) <= MAX_TOKENS + 1
            or not isinstance(payload.get("summary"), dict)
            or not isinstance(payload.get("sources"), list)
            or not isinstance(payload.get("coverage"), dict)
            or payload["coverage"].get("history_complete") is not False
            or type(payload["coverage"].get("inventory_complete")) is not bool
        ):
            raise PortfolioInputError(
                "Retain a complete public portfolio observation with explicit coverage."
            )
        account = _account(payload["account_id"])
        observed, effective = (
            _milliseconds(payload["observed_at"]),
            _milliseconds(payload["effective_at"]),
        )
        values, identifiers = [], []
        for asset in payload["assets"]:
            if not isinstance(asset, dict) or "value_usd" not in asset:
                raise PortfolioInputError(
                    "A retained asset must include its valuation."
                )
            identifier = asset.get("asset_id")
            if not isinstance(identifier, str) or (
                identifier != "native:near"
                and not (identifier.startswith("nep141:") and _account(identifier[7:]))
            ):
                raise PortfolioInputError(
                    "A retained asset must name an exact mainnet identifier."
                )
            identifiers.append(identifier)
            value = asset["value_usd"]
            if value is not None:
                try:
                    if not isinstance(value, str) or len(value) > 1024:
                        raise ValueError("invalid value")
                    number = Decimal(value)
                    if (
                        not number.is_finite()
                        or number < 0
                        or abs(number.adjusted()) > 600
                    ):
                        raise ValueError("invalid value")
                except (ValueError, InvalidOperation):
                    raise PortfolioInputError(
                        "A retained asset value must be a finite nonnegative decimal."
                    ) from None
                price, decimals = asset.get("price"), asset.get("decimals")
                try:
                    if (
                        not isinstance(price, dict)
                        or price.get("status") != "known"
                        or asset.get("status") != "priced"
                        or type(decimals) is not int
                        or not 0 <= decimals <= 255
                        or not isinstance(price.get("usd"), str)
                        or len(price["usd"]) > 512
                    ):
                        raise ValueError("invalid priced asset")
                    usd = Decimal(price["usd"])
                    confidence = _number(price.get("confidence"))
                    stamp = price.get("timestamp")
                    target = effective // 1000
                    if (
                        not usd.is_finite()
                        or usd < 0
                        or abs(usd.adjusted()) > 300
                        or not MIN_PRICE_CONFIDENCE <= confidence <= 1
                        or type(stamp) is not int
                        or not target - MAX_PRICE_AGE_S
                        <= stamp
                        <= target + (0 if payload["kind"] == "historical" else 60)
                    ):
                        raise ValueError("invalid retained price")
                    with localcontext() as context:
                        context.prec = 1024
                        balance = Decimal(
                            _amount(asset.get("balance_base_units"))
                        ).scaleb(-decimals)
                        if asset.get("balance") != _decimal(
                            balance
                        ) or value != _decimal(balance * usd):
                            raise ValueError("inconsistent retained value")
                except (ValueError, InvalidOperation):
                    raise PortfolioInputError(
                        "A retained asset value must match its base units and usable source price."
                    ) from None
                values.append(number)
        scope = hashlib.sha256(
            json.dumps(sorted(identifiers), separators=(",", ":")).encode()
        ).hexdigest()
        if (
            len(set(identifiers)) != len(identifiers)
            or payload["asset_scope_id"] != scope
        ):
            raise PortfolioInputError(
                "The retained asset inventory does not match its scope identifier."
            )
        with localcontext() as context:
            context.prec = 2048
            known = _decimal(sum(values, Decimal(0))) if values else None
        total = (
            known
            if payload["coverage"]["inventory_complete"]
            and len(values) == len(identifiers)
            else None
        )
        expected = {
            "known_value_usd": known,
            "total_value_usd": total,
            "priced_assets": len(values),
            "unpriced_assets": len(identifiers) - len(values),
        }
        if payload["summary"] != expected:
            raise PortfolioInputError(
                "The retained summary does not match its asset values and coverage."
            )
        if payload["kind"] == "historical":
            height = payload["coverage"].get("block_height")
            blocks = [
                source
                for source in payload["sources"]
                if isinstance(source, dict)
                and source.get("read_kind") == "archival_block"
            ]
            if (
                payload["coverage"]["inventory_complete"]
                or not isinstance(height, str)
                or not height.isascii()
                or not height.isdigit()
                or len(height) > 20
                or not 1 <= int(height) <= canon.U64_MAX
                or len(blocks) != 1
            ):
                raise PortfolioInputError(
                    "Historical observations require an incomplete, explicit archival block scope."
                )
            block = blocks[0]
            stamp = block.get("block_timestamp_ns")
            if (
                block.get("network") != "mainnet"
                or block.get("block_height") != height
                or not isinstance(block.get("block_hash"), str)
                or not block["block_hash"]
                or not isinstance(stamp, str)
                or not stamp.isascii()
                or not stamp.isdigit()
                or len(stamp) > 20
                or int(stamp) > canon.U64_MAX
                or effective != int(stamp) // 1000000000 * 1000
            ):
                raise PortfolioInputError(
                    "The historical effective time must match its archival block source."
                )
            for asset in payload["assets"]:
                if asset.get("balance_base_units") is None:
                    continue
                source = asset.get("balance_source")
                if (
                    not isinstance(source, dict)
                    or source.get("read_kind") != "archival_rpc"
                    or source.get("network") != "mainnet"
                    or source.get("block_height") != height
                ):
                    raise PortfolioInputError(
                        "Historical balances must name the same archival block."
                    )
        return account, observed, effective

    def _checked_row(self, row):
        if (
            not isinstance(row["body"], bytes)
            or hashlib.sha256(row["body"]).hexdigest() != row["id"]
        ):
            raise PortfolioReadError(
                "The portfolio observation does not match its hash."
            )
        try:
            document = json.loads(row["body"])
            account, observed, effective = self._validate(document)
            if _body(document) != row["body"]:
                raise ValueError("non-canonical retained observation")
            _comparable(document)
            for asset in document["assets"]:
                base, decimals = asset["balance_base_units"], asset["decimals"]
                if base is not None:
                    _amount(base)
                if decimals is not None and (
                    type(decimals) is not int or not 0 <= decimals <= 255
                ):
                    raise ValueError("invalid retained decimals")
                with localcontext() as context:
                    context.prec = 1024
                    balance = (
                        _decimal(Decimal(base).scaleb(-decimals))
                        if base is not None and decimals is not None
                        else None
                    )
                if asset["balance"] != balance:
                    raise ValueError("inconsistent retained balance")
        except (ValueError, TypeError, KeyError, RecursionError):
            raise PortfolioReadError(
                "The retained portfolio observation is invalid."
            ) from None
        if (row["account"], row["kind"], row["effective_ms"], row["observed_ms"]) != (
            account,
            document["kind"],
            effective,
            observed,
        ):
            raise PortfolioReadError(
                "The retained portfolio index does not match its hashed observation."
            )
        return document

    def save(self, payload):
        account, observed, effective = self._validate(payload)
        body = _body(payload)
        identifier = hashlib.sha256(body).hexdigest()
        window = int(self.clock()) // 60
        with self._connection(write=True) as connection:
            retained = connection.execute(
                "SELECT * FROM observations WHERE id=?", (identifier,)
            ).fetchone()
            if retained is None:
                latest = (
                    connection.execute(
                        "SELECT * FROM observations WHERE account=? AND kind=? ORDER BY seq DESC LIMIT 1",
                        (account, payload["kind"]),
                    )
                    if payload["kind"] == "current"
                    else connection.execute(
                        "SELECT * FROM observations WHERE account=? AND kind=? AND effective_ms=? ORDER BY seq DESC LIMIT 1",
                        (account, payload["kind"], effective),
                    )
                ).fetchone()
                try:
                    reusable = latest is not None and _comparable(
                        self._checked_row(latest)
                    ) == _comparable(payload)
                except PortfolioReadError:
                    reusable = False
                if reusable:
                    return {
                        "id": latest["id"],
                        "url": "/live/portfolio-data/" + latest["id"] + ".json",
                    }
            if retained is not None:
                self._checked_row(retained)
                if retained["body"] != body:
                    raise PortfolioReadError(
                        "The retained portfolio bytes disagree with the duplicate observation."
                    )
            else:
                count, size = connection.execute(
                    "SELECT COUNT(*),COALESCE(SUM(LENGTH(body)),0) FROM observations"
                ).fetchone()
                if count >= MAX_SNAPSHOTS or size + len(body) > MAX_RETAINED_BYTES:
                    raise PortfolioCapacityError(
                        "Portfolio retention capacity reached; existing observations remain available."
                    )
                connection.execute(
                    "DELETE FROM capture_budget WHERE window<?", (window,)
                )
                global_count = connection.execute(
                    "SELECT COALESCE(SUM(count),0) FROM capture_budget WHERE window=?",
                    (window,),
                ).fetchone()[0]
                row = connection.execute(
                    "SELECT count FROM capture_budget WHERE account=? AND window=?",
                    (account, window),
                ).fetchone()
                if global_count >= 120 or row is not None and row[0] >= 2:
                    raise PortfolioCapacityError(
                        "Portfolio capture budget reached; retry after one minute."
                    )
                connection.execute(
                    "INSERT INTO capture_budget VALUES(?,?,1) ON CONFLICT(account,window) DO UPDATE SET count=count+1",
                    (account, window),
                )
                connection.execute(
                    "INSERT INTO observations(id,account,kind,effective_ms,observed_ms,body) VALUES(?,?,?,?,?,?)",
                    (identifier, account, payload["kind"], effective, observed, body),
                )
        return {"id": identifier, "url": "/live/portfolio-data/" + identifier + ".json"}

    def get_bytes(self, identifier):
        if not isinstance(identifier, str) or not _HASH.fullmatch(identifier):
            raise PortfolioInputError(
                "A portfolio observation ID is a lowercase SHA-256 identifier."
            )
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM observations WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            return None
        self._checked_row(row)
        return row["body"]

    def list_records(self, account, before=0, limit=100, asset_id=None):
        account = _account(account)
        if (
            type(before) is not int
            or not 0 <= before <= 2**63 - 1
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise PortfolioInputError(
                "Use a nonnegative portfolio cursor and a limit from 1 to 100."
            )
        if asset_id is not None and (
            not isinstance(asset_id, str)
            or asset_id != "native:near"
            and not (asset_id.startswith("nep141:") and _account(asset_id[7:]))
        ):
            raise PortfolioInputError(
                "Select a native or exact NEP-141 asset identifier."
            )
        with self._connection() as connection:
            parameters, clause = [account], ""
            if before:
                anchor = connection.execute(
                    "SELECT * FROM observations WHERE seq=? AND account=?",
                    (before, account),
                ).fetchone()
                if anchor is None:
                    raise PortfolioInputError(
                        "The portfolio cursor does not belong to this account."
                    )
                self._checked_row(anchor)
                clause = " AND (effective_ms<? OR (effective_ms=? AND seq<?))"
                parameters += [anchor["effective_ms"], anchor["effective_ms"], before]
            rows = connection.execute(
                "SELECT * FROM observations WHERE account=?"
                + clause
                + " ORDER BY effective_ms DESC,seq DESC LIMIT ?",
                [*parameters, limit + 1],
            ).fetchall()
            count = connection.execute(
                "SELECT COUNT(*) FROM observations WHERE account=?", (account,)
            ).fetchone()[0]
        items = []
        documents = [self._checked_row(row) for row in rows]
        for row, document in zip(rows[:limit], documents[:limit]):
            point = {
                "id": row["id"],
                "url": "/live/portfolio-data/" + row["id"] + ".json",
                "kind": document["kind"],
                "observed_at": document["observed_at"],
                "effective_at": document["effective_at"],
                "asset_scope_id": document["asset_scope_id"],
                "assets_count": len(document["assets"]),
                **document["summary"],
            }
            if asset_id is not None:
                asset = next(
                    (
                        asset
                        for asset in document["assets"]
                        if asset["asset_id"] == asset_id
                    ),
                    None,
                )
                point.update(
                    asset_id=asset_id,
                    balance_base_units=asset["balance_base_units"] if asset else None,
                    decimals=asset["decimals"] if asset else None,
                    balance=asset["balance"] if asset else None,
                    asset_value_usd=asset["value_usd"] if asset else None,
                )
            items.append(point)
        return {
            "account_id": account,
            "network": "mainnet",
            "items": items,
            "pagination": {
                "limit": limit,
                "next_before": rows[limit - 1]["seq"] if len(rows) > limit else None,
            },
            "coverage": {
                "stored_observations": count,
                "history_complete": False,
                "asset_id": asset_id,
                "scope": "Actual retained observations only. Different asset_scope_id values describe different selected inventories; gaps and scopes must not be joined into complete wallet history.",
            },
        }
