"""Capped 1Click settlement from wNEAR, with a check before funding and a receipt after it.

Only --deposit may sign, broadcast or notify 1Click. A fresh external report is reserved before
starting a live run and its transaction hash is flushed before broadcasting once. Resume only
reads the original transaction and swap; an uncertain deposit is never sent again. Sources and
the deliberately unsupported routes are recorded in docs/lanes/oneclick.md.
"""

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from gate import base58, canon, ed25519, intents, rpc, signer

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = "nep141:wrap.near"
TOKEN = "wrap.near"
MAX_AMOUNT = 250000000000000000000000
GAS = 50000000000000
POLL_SECONDS = 5
FINALITY_POLL_SECONDS = 30
CLOCK_SKEW_MS = 5000
INDEXER_MARGIN_BLOCKS = 10
DEADLINE_MARGIN_SECONDS = 120
DRY_TICK_BPS = 25
STORAGE_GAS = 30000000000000
MAX_STORAGE_DEPOSIT = 1250000000000000000000
TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa9"
                  "52ba7f163c4a11628f55a4df523b3ef")
MANAGER_PUBLIC_KEY = "ed25519:reYaWhvwu8Jzo3WUM3zhn6VrhuMEF4eADL17qtRVifc"
# Every value of GetExecutionStatusResponse.status in the recorded live OpenAPI v0.1.10.
STATUS_ACTIONS = {
    "KNOWN_DEPOSIT_TX": "poll the existing deposit; never deposit again",
    "PENDING_DEPOSIT": "a fresh deposit is permitted only after all predeposit checks",
    "INCOMPLETE_DEPOSIT": "stop UNKNOWN; never top up automatically",
    "PROCESSING": "poll the existing deposit; never deposit again",
    "SUCCESS": "verify actual amounts and a finalized destination receipt",
    "REFUNDED": "stop UNKNOWN; a provider report does not prove receipt of a refund",
    "FAILED": "stop UNKNOWN; failure does not prove that no funds moved",
}
_HEX_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)")
_QUOTE_FIELDS = {
    "depositAddress", "depositMemo", "chainDepositAddresses", "amountIn", "amountInFormatted",
    "amountInUsd", "minAmountIn", "amountOut", "amountOutFormatted", "amountOutUsd", "minAmountOut",
    "deadline", "timeWhenInactive", "timeEstimate", "virtualChainRecipient", "virtualChainRefundRecipient",
    "customRecipientMsg", "refundFee", "withdrawFee",
}


class SettlementPending(ValueError):
    """The destination receipt has not reached finality yet; poll the same swap."""


def _units(value, name, *, zero=False):
    if not isinstance(value, str) or not intents._DECIMAL.fullmatch(value) or len(value) > 39:
        raise ValueError(f"{name} is not a canonical base-unit amount")
    number = int(value)
    if not (0 if zero else 1) <= number <= canon.U128_MAX:
        raise ValueError(f"{name} is outside its base-unit bounds")
    return number


def _quantity(value, name):
    if not isinstance(value, str) or not _QUANTITY.fullmatch(value):
        raise ValueError(f"{name} is not an RPC quantity")
    return int(value, 16)


def _external(path, name):
    resolved = Path(path).resolve()
    if resolved.is_relative_to(ROOT):
        raise ValueError(f"{name} must be outside the repository")
    return resolved


def _report_key(path):
    resolved = str(Path(path).resolve())
    return resolved.casefold() if os.name == "nt" else resolved


def _sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def _run_registry(sender):
    directory = Path.home() / ".siyab-oneclick"
    path = directory / f"{sender}.sqlite3"
    if directory.is_symlink() or path.is_symlink():
        raise ValueError("the settlement registry cannot be a symbolic link")
    directory.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)
    _sync_directory(directory.parent)
    try:
        with closing(sqlite3.connect(path, timeout=5, isolation_level=None)) as connection:
            if os.name == "posix":
                path.chmod(0o600)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=EXTRA")
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("CREATE TABLE IF NOT EXISTS open_runs (report TEXT PRIMARY KEY)")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(open_runs)")}
            if "signing_attempted" not in columns:
                connection.execute("ALTER TABLE open_runs ADD COLUMN signing_attempted INTEGER")
            if "reserved_at_ms" not in columns:
                connection.execute("ALTER TABLE open_runs ADD COLUMN reserved_at_ms INTEGER")
            for (stored,) in connection.execute("SELECT report FROM open_runs").fetchall():
                canonical = _report_key(stored)
                if canonical != stored:
                    connection.execute("UPDATE open_runs SET report = ? WHERE report = ?", (canonical, stored))
            yield connection
            connection.commit()
            _sync_directory(directory)
    except sqlite3.Error as error:
        raise ValueError(f"the settlement registry is unavailable: {error}; do not redeposit yet") from error


def _save(state, handle):
    if handle is not None:
        data = json.dumps(state, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=handle.parent,
                                             prefix=".oneclick-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, handle)
            _sync_directory(handle.parent)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def quote_hash(response):
    """The pinned official SDK's selected, merged payload, hashed then encoded as Base58 text."""
    request, quote = response["quoteRequest"], response["quote"]
    payload = {name: request[name] for name in intents.REQUEST_REQUIRED}
    for name in ("quoteWaitingTimeMs", "referral", "virtualChainRecipient", "virtualChainRefundRecipient",
                 "customRecipientMsg"):
        if request.get(name):
            payload[name] = request[name]
    for name in ("amountIn", "amountInFormatted", "amountInUsd", "minAmountIn", "amountOut",
                 "amountOutFormatted", "amountOutUsd", "minAmountOut"):
        if not isinstance(quote[name], str):
            raise ValueError("a signed quote amount is not a string")
        payload[name] = quote[name]
    if request["dry"] is False:
        for name in ("depositAddress", "depositMemo", "deadline", "timeWhenInactive", "timeEstimate",
                     "refundFee", "withdrawFee"):
            if quote.get(name):
                payload[name] = quote[name]
    payload["timestamp"] = response["timestamp"]
    # All numeric fields on this route are bounded integers, for identical JS and Python JSON.
    if any(type(value) is float for value in payload.values()):
        raise ValueError("non-integer signed quote numbers are unsupported")
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return base58.encode(hashlib.sha256(data.encode("utf-8")).digest())


def verify_quote_signature(response):
    """Verify the public manager's signature over the SDK quote hash, not over status details."""
    try:
        signature = response["signature"]
        if not isinstance(signature, str):
            return False
        raw = base58.decode(signature.removeprefix("ed25519:"))
        return ed25519.verify(base58.decode_key(MANAGER_PUBLIC_KEY), quote_hash(response).encode("utf-8"), raw)
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        return False


def _normal_request(request):
    # These nulls occur in recorded status responses and the official SDK's signed status vector.
    result = {name: value for name, value in request.items()
              if not (name in ("virtualChainRecipient", "virtualChainRefundRecipient", "referral") and value is None)}
    return {**intents.STATUS_DEFAULTS, "appFees": [], "quoteWaitingTimeMs": 0, **result}


def _quote_shape(response, *, status=False):
    intents._encodable(response, "1Click quote")
    if not isinstance(response, dict):
        raise ValueError("the 1Click quote is not an object")
    if not verify_quote_signature(response):
        raise ValueError("the 1Click quote signature did not verify")
    checked = {"correlationId": "status", **response} if status else response
    quote, request = intents._answer_schema(checked)
    if set(response) - {"correlationId", "timestamp", "signature", "quoteRequest", "quote"}:
        raise ValueError("the quote response has unknown fields")
    if set(quote) - _QUOTE_FIELDS:
        raise ValueError("the quote has unknown fields")
    if not checked["correlationId"] or request.get("dry") is not False:
        raise ValueError("a non-dry quote with a correlationId is required")
    request = _normal_request(request)
    intents._request_schema(request)
    if request["originAsset"] != ORIGIN or request["swapType"] != "EXACT_INPUT":
        raise ValueError("settlement requires EXACT_INPUT from wNEAR")
    if (request["depositType"] != "ORIGIN_CHAIN" or request["refundType"] != "ORIGIN_CHAIN"
            or request["recipientType"] != "DESTINATION_CHAIN"):
        raise ValueError("settlement requires origin-chain funding and refunds and destination-chain delivery")
    if intents._unjudged(intents.quote_body(ORIGIN, request["destinationAsset"], request["amount"],
                                          request["recipient"], "DESTINATION_CHAIN", request["deadline"],
                                          refund_to=request["refundTo"]), request):
        raise ValueError("the quote has unjudged routing fields")
    if not intents._EVM_ADDRESS.fullmatch(request["recipient"]):
        raise ValueError("the recipient is not an EVM address")
    if _units(request["amount"], "amount") > MAX_AMOUNT:
        raise ValueError("the quote exceeds the 0.25 wNEAR cap")
    canon.account_id(request["refundTo"], "refund account")
    address = canon.account_id(quote.get("depositAddress"), "deposit address")
    if address.startswith("0x") or address.endswith(".testnet") or request["refundTo"].endswith(".testnet"):
        raise ValueError("1Click uses mainnet accounts")
    if any(quote.get(field) not in (None, "", []) for field in (
            "depositMemo", "chainDepositAddresses", "virtualChainRecipient", "virtualChainRefundRecipient",
            "customRecipientMsg")):
        raise ValueError("memo, multiple-address and custom-message deposits are unsupported")
    for field in ("amountIn", "minAmountIn", "amountOut", "minAmountOut"):
        _units(quote.get(field), field)
    if quote["amountIn"] != request["amount"] or quote["minAmountIn"] != request["amount"]:
        raise ValueError("the quote input amounts differ from the capped exact input")
    if int(quote["minAmountOut"]) > int(quote["amountOut"]):
        raise ValueError("minAmountOut exceeds amountOut")
    slippage = request["slippageTolerance"]
    if type(slippage) is not int or not 0 <= slippage <= intents.SLIPPAGE_BPS:
        raise ValueError("the slippage exceeds the judged ceiling")
    if int(quote["minAmountOut"]) < int(quote["amountOut"]) * (10000 - slippage) // 10000:
        raise ValueError("minAmountOut does not enforce the judged slippage")
    for field in ("refundFee", "withdrawFee"):
        _units(quote.get(field), field, zero=True)
    for field, holder in (("deadline", request), ("deadline", quote), ("timestamp", response)):
        if intents._instant(holder.get(field)) is None:
            raise ValueError(f"the quote {field} is missing or invalid")
    if intents._instant(response["timestamp"]) >= intents._instant(quote["deadline"]):
        raise ValueError("the address deadline is not after quote creation")
    if "timeWhenInactive" in quote and intents._instant(quote["timeWhenInactive"]) is None:
        raise ValueError("timeWhenInactive is invalid")
    return quote, request


def check_quote(judged, response, *, min_amount_out, clock=time.time):
    """Bind the wet quote to the intact recipient verdict, fee limits and owner's output floor."""
    quote, request = _quote_shape(response)
    if judged.get("verdict") != "ALLOW":
        raise ValueError("the recipient verdict is not ALLOW")
    service, _ = intents._service_fee()
    named = judged["document"]["subject"]["quote_request"].get("appFees", [])
    if intents._added_fees(named, request, service) is None:
        raise ValueError("the real quote has unjudged app fees")
    # The approved provider-added fee is checked above. verify_binding compares caller-named fees.
    bound = dict(request, appFees=named)
    now = clock()
    intents.verify_binding(judged, bound, clock=lambda: now, clock_skew_ms=CLOCK_SKEW_MS)
    old = judged.get("quote")
    if not isinstance(old, dict):
        raise ValueError("the judged quote amounts and fees are missing")
    for field in ("refundFee", "withdrawFee"):
        if int(quote[field]) > _units(old.get(field), f"judged {field}", zero=True):
            raise ValueError(f"{field} exceeds the judged fee")
    dry_floor = _units(old.get("minAmountOut"), "judged minimum output")
    dry_floor = (dry_floor * (10000 - DRY_TICK_BPS) + 9999) // 10000
    floor = _units(min_amount_out, "minimum output")
    if int(quote["minAmountOut"]) < dry_floor:
        raise ValueError("the real quote output is below the judged minimum")
    if int(quote["minAmountOut"]) - int(quote["withdrawFee"]) < floor:
        raise ValueError("the real quote output is below the judged or owner minimum")
    at = datetime.fromtimestamp(now, UTC)
    for deadline in (request["deadline"], quote["deadline"], quote.get("timeWhenInactive", quote["deadline"])):
        if (intents._instant(deadline) - at).total_seconds() < DEADLINE_MARGIN_SECONDS + CLOCK_SKEW_MS / 1000:
            raise ValueError("the refund deadline, address expiry or cold time needs 120 seconds plus clock skew")
    if intents._quote_age_check(response["timestamp"], {}, lambda: now, clock_skew_ms=CLOCK_SKEW_MS)["status"] != "pass":
        raise ValueError("the real quote timestamp is stale or in the future")
    return quote


def check_status(quote_response, response, *, before_deposit=False, clock=time.time):
    """Require the exact allocated quote on every status read; only PENDING_DEPOSIT can be funded."""
    intents._encodable(response, "1Click status")
    if not isinstance(response, dict) or set(response) != {
            "correlationId", "quoteResponse", "status", "updatedAt", "swapDetails"}:
        raise ValueError("the status response does not have the documented fields")
    lifecycle = response["status"]
    if not isinstance(lifecycle, str) or lifecycle not in STATUS_ACTIONS:
        raise ValueError("the status is not a documented 1Click status")
    nested = response["quoteResponse"]
    _quote_shape(nested, status=True)
    # The signature and display fields are not used to establish settlement. All economic fields,
    # deadlines, routing and the allocated correlation id must remain exactly the allocated quote.
    fields = ("depositAddress", "depositMemo", "amountIn", "minAmountIn", "amountOut", "minAmountOut",
              "deadline", "timeWhenInactive", "refundFee", "withdrawFee")
    if (quote_hash(nested) != quote_hash(quote_response)
            or nested["timestamp"] != quote_response["timestamp"]
            or _normal_request(nested["quoteRequest"]) != _normal_request(quote_response["quoteRequest"])
            or any(nested["quote"].get(field) != quote_response["quote"].get(field) for field in fields)):
        raise ValueError("the status quote differs from the allocated quote")
    updated = intents._instant(response["updatedAt"])
    if (not isinstance(response["correlationId"], str) or not response["correlationId"] or updated is None
            or updated.timestamp() > clock() + CLOCK_SKEW_MS / 1000
            or updated.timestamp() < intents._instant(quote_response["timestamp"]).timestamp() - CLOCK_SKEW_MS / 1000):
        raise ValueError("the status correlation or updatedAt is invalid")
    details = response["swapDetails"]
    if not isinstance(details, dict):
        raise ValueError("swapDetails is not an object")
    for name in ("intentHashes", "nearTxHashes", "originChainTxHashes", "destinationChainTxHashes"):
        rows = details.get(name)
        if not isinstance(rows, list) or len(rows) > 16:
            raise ValueError(f"{name} is not a bounded list")
        if name in ("intentHashes", "nearTxHashes"):
            if any(not isinstance(row, str) or not row for row in rows):
                raise ValueError(f"{name} has a malformed hash")
        elif any(not isinstance(row, dict) or not isinstance(row.get("hash"), str) or not row["hash"]
                 or not isinstance(row.get("explorerUrl"), str) for row in rows):
            raise ValueError(f"{name} has a malformed transaction")
    if before_deposit:
        if lifecycle != "PENDING_DEPOSIT":
            raise ValueError("the address is not awaiting a fresh deposit")
        if any(details[name] for name in ("intentHashes", "nearTxHashes", "originChainTxHashes",
                                         "destinationChainTxHashes")):
            raise ValueError("the pending address already has transactions")
        for name in ("depositedAmount", "amountIn", "amountOut", "refundedAmount"):
            if details.get(name) is not None and _units(details[name], name, zero=True) != 0:
                raise ValueError("the pending address already has funds")
    return lifecycle


def _chain(client, chain):
    if _quantity(client.call("eth_chainId", []), "chain id") != intents.CHAIN_IDS[chain]:
        raise ValueError("the destination RPC is on another chain")


def destination_receipt(quote_response, status_response, *, client):
    """Reconcile the provider's output with native transfers or ERC20 credits in finalized receipts."""
    quote, request = _quote_shape(quote_response)
    if status_response.get("status") != "SUCCESS":
        raise ValueError("the swap is not SUCCESS")
    details = status_response["swapDetails"]
    amount = _units(details.get("amountOut"), "settled output")
    if details.get("amountIn") != request["amount"]:
        raise ValueError("the settled input differs from the deposit")
    if amount < int(quote["minAmountOut"]):
        raise ValueError("the settled output is below the judged minimum")
    if details.get("depositedAmount") not in (None, request["amount"]):
        raise ValueError("the deposited amount differs from the judged input")
    if (details.get("refundedAmount") not in (None, "0") or details.get("refundReason") not in (None, "")):
        raise ValueError("SUCCESS carries a refund")
    for field in ("withdrawFee", "refundFee"):
        if details.get(field) is not None and _units(details[field], f"settled {field}", zero=True) > int(quote[field]):
            raise ValueError(f"the settled {field} exceeds the judged fee")
    chain, token = intents.parse_destination(request["destinationAsset"])
    if chain not in intents.CHAIN_IDS:
        raise ValueError("the settlement chain is unsupported")
    _chain(client, chain)
    finalized = client.call("eth_getBlockByNumber", ["finalized", False])
    height = _quantity(finalized.get("number"), "finalized block")
    rows = details.get("destinationChainTxHashes")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("SUCCESS has no bounded destination transactions")
    hashes = [row.get("hash") for row in rows if isinstance(row, dict)]
    if (len(hashes) != len(rows) or any(not isinstance(h, str) or not _HEX_HASH.fullmatch(h) for h in hashes)
            or len({h.lower() for h in hashes}) != len(hashes)):
        raise ValueError("the destination transaction hashes are malformed or repeated")
    recipient, received, receipts = request["recipient"].lower(), 0, []
    for tx_hash in hashes:
        try:
            receipt = client.call("eth_getTransactionReceipt", [tx_hash])
        except rpc.MalformedResponse as error:
            if error.args != ("the JSON-RPC body has no result",):
                raise
            raise SettlementPending("the destination receipt is not available") from error
        if receipt is None:
            raise SettlementPending("the destination receipt is not available")
        if not isinstance(receipt, dict):
            raise ValueError("the destination receipt is malformed")
        if receipt.get("status") != "0x1" or receipt.get("transactionHash", "").lower() != tx_hash.lower():
            raise ValueError("the destination receipt failed or names another transaction")
        number = _quantity(receipt.get("blockNumber"), "receipt block")
        block_hash = receipt.get("blockHash")
        if not isinstance(block_hash, str) or not _HEX_HASH.fullmatch(block_hash):
            raise ValueError("the receipt has no block hash")
        if number > height:
            raise SettlementPending("the destination receipt is not finalized")
        block = client.call("eth_getBlockByNumber", [hex(number), False])
        if block.get("hash", "").lower() != block_hash.lower() or _quantity(block.get("number"), "block") != number:
            raise ValueError("the receipt block is not canonical")
        timestamp = _quantity(block.get("timestamp"), "receipt block timestamp")
        if timestamp < math.ceil(intents._instant(quote_response["timestamp"]).timestamp()):
            raise ValueError("the destination payment predates the allocated quote")
        if token is None:
            tx = client.call("eth_getTransactionByHash", [tx_hash])
            if (not isinstance(tx, dict) or tx.get("hash", "").lower() != tx_hash.lower()
                    or tx.get("blockHash", "").lower() != block_hash.lower()
                    or tx.get("to", "").lower() != recipient or tx.get("input") != "0x"
                    or receipt.get("to", "").lower() != recipient):
                raise ValueError("the native withdrawal is not a direct transfer to the judged recipient")
            received += _quantity(tx.get("value"), "native output")
        else:
            logs = receipt.get("logs")
            if not isinstance(logs, list) or len(logs) > 1000:
                raise ValueError("the destination logs are missing or too numerous")
            indices = set()
            for log in logs:
                if not isinstance(log, dict):
                    raise ValueError("a destination log is malformed")
                if log.get("address", "").lower() != token.lower():
                    continue
                topics = log.get("topics")
                if not isinstance(topics, list) or not topics or topics[0] != TRANSFER_TOPIC:
                    continue
                if (len(topics) != 3 or any(not isinstance(t, str) or not _HEX_HASH.fullmatch(t) for t in topics)
                        or not isinstance(log.get("data"), str) or not _HEX_HASH.fullmatch(log["data"])
                        or log.get("removed") is not False
                        or log.get("transactionHash", "").lower() != tx_hash.lower()
                        or log.get("blockHash", "").lower() != block_hash.lower()):
                    raise ValueError("a token transfer log is malformed or removed")
                index = _quantity(log.get("logIndex"), "log index")
                if index in indices or any(int(t[2:26], 16) != 0 for t in topics[1:]):
                    raise ValueError("a token transfer log is duplicated or has malformed addresses")
                indices.add(index)
                value = int(log["data"], 16)
                if "0x" + topics[2][-40:].lower() == recipient:
                    received += value
                if "0x" + topics[1][-40:].lower() == recipient:
                    received -= value
        receipts.append({"transaction_hash": tx_hash, "block_hash": block_hash, "block_number": number,
                         "block_timestamp": timestamp})
    if received != amount:
        raise ValueError("the finalized receipt amount or recipient differs from the reported output")
    return {"recipient": request["recipient"], "asset": request["destinationAsset"], "amount": str(received),
            "receipts": receipts, "rpc": client.url}


def _funding(sender, quote_response, client):
    quote = quote_response["quote"]
    storage = client.call_function(TOKEN, "storage_balance_of", {"account_id": sender})
    if not isinstance(storage, dict) or _units(storage.get("total"), "sender storage balance") == 0:
        raise ValueError("the sender is not registered on wrap.near")
    storage = client.call_function(TOKEN, "storage_balance_of", {"account_id": quote["depositAddress"]})
    registration = 0
    if storage is None:
        bounds = client.call_function(TOKEN, "storage_balance_bounds", {})
        if not isinstance(bounds, dict):
            raise ValueError("wrap.near storage bounds are missing")
        registration = _units(bounds.get("min"), "storage minimum")
        if registration > MAX_STORAGE_DEPOSIT or bounds.get("max") != bounds["min"]:
            raise ValueError("wrap.near registration exceeds the cap or has unexpected storage bounds")
    elif not isinstance(storage, dict) or _units(storage.get("total"), "receiver storage balance") == 0:
        raise ValueError("the deposit address storage balance is malformed")
    balance = client.call_function(TOKEN, "ft_balance_of", {"account_id": sender})
    if _units(balance, "wNEAR balance", zero=True) < int(quote["amountIn"]):
        raise ValueError("the sender has insufficient wNEAR; wrap funds separately")
    account = client.view_account(sender)
    if _units(account.get("amount"), "native balance", zero=True) < 10**22 + registration:
        raise ValueError("the sender needs native NEAR for registration plus the 0.01 NEAR gas reserve")
    return registration


def _signed_deposit(sender, quote_response, *, client, tx_signer):
    """One V0 wrap.near transaction: capped receiver registration if needed, then ft_transfer."""
    quote, request = _quote_shape(quote_response)
    if request["refundTo"] != sender:
        raise ValueError("the refund account is not the sender")
    registration = _funding(sender, quote_response, client)
    public = bytes.fromhex(tx_signer.public_key_hex)
    if len(public) != 32:
        raise ValueError("the signer is not Ed25519")
    key = client.call("query", {"request_type": "view_access_key", "account_id": sender,
                               "public_key": base58.encode_key(public), "finality": "final"})
    nonce = key.get("nonce")
    block_hash = base58.decode(key.get("block_hash", ""))
    height = key.get("block_height")
    if key.get("permission") != "FullAccess":
        raise ValueError("the deposit requires an ordinary FullAccess key")
    if (type(nonce) is not int or not 0 <= nonce < canon.U64_MAX or len(block_hash) != 32
            or type(height) is not int or not 0 <= height <= canon.U64_MAX):
        raise ValueError("the access-key nonce, block hash or height is invalid")
    calls = []
    if registration:
        calls.append(("storage_deposit", {"account_id": quote["depositAddress"], "registration_only": True},
                      STORAGE_GAS, registration))
    calls.append(("ft_transfer", {"receiver_id": quote["depositAddress"], "amount": quote["amountIn"]}, GAS, 1))
    actions = b""
    for method, args, gas, deposit in calls:
        method = method.encode("ascii")
        encoded = json.dumps(args, separators=(",", ":")).encode("utf-8")
        actions += (b"\x02" + len(method).to_bytes(4, "little") + method
                    + len(encoded).to_bytes(4, "little") + encoded
                    + gas.to_bytes(8, "little") + deposit.to_bytes(16, "little"))
    account = sender.encode("utf-8")
    transaction = (len(account).to_bytes(4, "little") + account + b"\x00" + public + (nonce + 1).to_bytes(8, "little")
                   + len(TOKEN).to_bytes(4, "little") + TOKEN.encode("ascii") + block_hash
                   + len(calls).to_bytes(4, "little") + actions)
    digest = hashlib.sha256(transaction).digest()
    signature = tx_signer.sign(digest)
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("the signer returned a malformed signature")
    reference = {"block_hash": key["block_hash"], "block_height": height,
                 "public_key": base58.encode_key(public), "nonce": nonce + 1}
    return base58.encode(digest), base64.b64encode(transaction + b"\x00" + signature).decode("ascii"), registration, reference


def _deposit_result(state, result):
    tx = result.get("transaction") if isinstance(result, dict) else None
    if (not isinstance(tx, dict) or tx.get("hash") != state["deposit_hash"]
            or tx.get("signer_id") != state["sender"] or tx.get("receiver_id") != TOKEN
            or result.get("final_execution_status") != "FINAL"):
        raise ValueError("the deposit is not a matching finalized transaction")
    actions = tx.get("actions")
    registration = _units(state.get("storage_deposit", "0"), "recorded storage deposit", zero=True)
    if registration > MAX_STORAGE_DEPOSIT:
        raise ValueError("the recorded storage deposit exceeds the cap")
    if (not isinstance(actions, list) or len(actions) != 1 + bool(registration)
            or any(not isinstance(action, dict) or set(action) != {"FunctionCall"} for action in actions)):
        raise ValueError("the deposit has unexpected actions")
    if registration:
        storage = actions[0]["FunctionCall"]
        if (not isinstance(storage, dict) or storage.get("method_name") != "storage_deposit"
                or storage.get("deposit") != str(registration) or storage.get("gas") != STORAGE_GAS):
            raise ValueError("the deposit has unexpected storage registration")
        try:
            args = json.loads(base64.b64decode(storage.get("args", ""), validate=True))
        except (ValueError, TypeError) as error:
            raise ValueError("the storage registration arguments are malformed") from error
        if (args != {"account_id": state["quote_response"]["quote"]["depositAddress"], "registration_only": True}
                or args.get("registration_only") is not True):
            raise ValueError("the registration does not name only the deposit receiver")
    call = actions[-1].get("FunctionCall")
    if not isinstance(call, dict) or call.get("method_name") != "ft_transfer" or call.get("deposit") != "1" or call.get("gas") != GAS:
        raise ValueError("the deposit is not the capped token transfer")
    try:
        args = json.loads(base64.b64decode(call.get("args", ""), validate=True))
    except (ValueError, TypeError) as error:
        raise ValueError("the deposit arguments are malformed") from error
    if args != {"receiver_id": state["quote_response"]["quote"]["depositAddress"], "amount": state["amount"]}:
        raise ValueError("the deposit arguments differ from the judged transfer")
    outcome = result.get("status")
    if isinstance(outcome, dict) and set(outcome) == {"Failure"} and isinstance(outcome["Failure"], dict) and outcome["Failure"]:
        conversion = result.get("transaction_outcome", {})
        emitted = conversion.get("outcome", {}).get("status", {})
        receipts = result.get("receipts_outcome")
        if conversion.get("id") != state["deposit_hash"] or not isinstance(receipts, list):
            raise ValueError("the failed deposit has no matching transaction outcome")
        if emitted == outcome:
            if receipts or conversion["outcome"].get("executor_id") != state["sender"]:
                raise ValueError("the failed transaction conversion has unexpected receipts or executor")
        else:
            receipt_id = emitted.get("SuccessReceiptId")
            if not isinstance(receipt_id, str) or len(base58.decode(receipt_id)) != 32:
                raise ValueError("the failed deposit has no original receipt id")
            roots = [row for row in receipts if isinstance(row, dict) and row.get("id") == receipt_id]
            if (len(roots) != 1 or roots[0].get("outcome", {}).get("executor_id") != TOKEN
                    or roots[0]["outcome"].get("status") != outcome):
                raise ValueError("the original wrap.near receipt did not fail; a later failure cannot prove rollback")
        state.update(verdict="ASK", phase="deposit_failed", deposit_result=result,
                     reason=f"Deposit {state['deposit_hash']} finalized with {json.dumps(outcome, sort_keys=True)}. "
                            "The deposit failed on chain; no wNEAR was deposited. Gas may have been charged. "
                            "Start a fresh run with a new report.")
        return False
    if outcome != {"SuccessValue": ""}:
        raise ValueError("the deposit does not have a successful finalized outcome")
    return True


def _transaction_read(state, client):
    tx_hash = state.get("deposit_hash")
    if not isinstance(tx_hash, str) or not 32 <= len(tx_hash) <= 44 or len(base58.decode(tx_hash)) != 32:
        raise ValueError("the deposit hash is malformed")
    params = {"tx_hash": state["deposit_hash"], "sender_account_id": state["sender"], "wait_until": "NONE"}
    body = {"jsonrpc": "2.0", "id": "gate", "method": "tx", "params": params}
    response = client._exchange(client.url, "tx", params, body)
    error = response.get("error", {}) if isinstance(response, dict) else {}
    cause = error.get("cause", {}) if isinstance(error, dict) else {}
    if not isinstance(cause, dict) or cause.get("name") != "UNKNOWN_TRANSACTION":
        rpc.response_gate(response, "tx", params, request_id="gate")
        if not isinstance(response.get("result"), dict):
            raise ValueError("the deposit discovery has no transaction status")
        return response
    # The shared RPC validator does not yet recognize this documented transaction error.
    if (set(response) != {"jsonrpc", "id", "error"} or response["jsonrpc"] != "2.0" or response["id"] != "gate"
            or set(error) - {"name", "cause", "code", "message", "data"}
            or error.get("name") != "HANDLER_ERROR" or type(error.get("code")) is not int or error["code"] != -32000
            or not isinstance(error.get("message"), str)
            or "data" in error and not isinstance(error["data"], str)
            or set(cause) != {"name", "info"} or cause["info"] != {"requested_transaction_hash": state["deposit_hash"]}):
        raise ValueError("the unknown transaction error does not match the original deposit")
    return response


def _broadcast_result(state, client):
    """Discover the original hash, read its final outcome or prove that it expired unused."""
    response = _transaction_read(state, client)
    params = {"tx_hash": state["deposit_hash"], "sender_account_id": state["sender"], "wait_until": "FINAL"}
    if "error" not in response:
        return _deposit_result(state, client.call("tx", params))
    reference = state.get("deposit_reference")
    if not isinstance(reference, dict):
        raise ValueError("the broadcast report has no signed reference block and nonce; expiry cannot be proved")
    height, nonce = reference.get("block_height"), reference.get("nonce")
    if (type(height) is not int or not 0 <= height <= canon.U64_MAX
            or type(nonce) is not int or not 1 <= nonce <= canon.U64_MAX
            or len(base58.decode(reference.get("block_hash", ""))) != 32
            or len(base58.decode_key(reference.get("public_key", ""))) != 32):
        raise ValueError("the signed reference block or nonce is malformed")
    final = client.call("block", {"finality": "final"})["header"]
    final_height, final_hash = final.get("height"), final.get("hash")
    if (type(final_height) is not int or not 0 <= final_height <= canon.U64_MAX
            or len(base58.decode(final_hash)) != 32):
        raise ValueError("the finalized origin block is malformed")
    config = client.call("EXPERIMENTAL_protocol_config", {"block_id": final_hash})
    period = config.get("transaction_validity_period")
    if config.get("chain_id") != "mainnet" or type(period) is not int or not 0 < period <= canon.U64_MAX:
        raise ValueError("the mainnet transaction validity period could not be read")
    if final_height <= height + period:
        raise ValueError("the unknown deposit is still within its transaction validity window")
    archival = rpc.Client(rpc.ARCHIVAL_RPC_URL, client.mode, fixtures_dir=client.fixtures_dir)
    try:
        base = archival.call("block", {"block_id": reference["block_hash"]})["header"]
        canonical = archival.call("block", {"block_id": height})["header"]
    except intents.UNAVAILABLE as error:
        raise ValueError(f"the archival reference block lookup at {archival.url} failed: {type(error).__name__}: {error}") from error
    if any(header.get("hash") != reference["block_hash"] or header.get("height") != height for header in (base, canonical)):
        raise ValueError("the signed reference block is not the recorded canonical block")
    key = client.call("query", {"request_type": "view_access_key", "account_id": state["sender"],
                                "public_key": reference["public_key"], "block_id": final_hash})
    if (key.get("permission") != "FullAccess" or type(key.get("nonce")) is not int or not nonce - 1 <= key["nonce"] <= canon.U64_MAX
            or key.get("block_hash") != final_hash or key.get("block_height") != final_height):
        raise ValueError("the final access-key nonce or permission cannot exclude an earlier deposit")
    proof = {"unknown_transaction": response, "signed_reference": reference, "reference_rpc": archival.url,
             "final_block": final, "transaction_validity_period": period, "access_key": key}
    nonce_reason = "the key nonce is unchanged"
    if key["nonce"] >= nonce:
        try:
            archived = archival.call("block", {"block_id": final_hash})["header"]
            if archived.get("hash") != final_hash or archived.get("height") != final_height:
                raise ValueError("the archival RPC does not confirm the finalized origin block")
            archived = _transaction_read(state, archival)
        except intents.UNAVAILABLE as error:
            raise ValueError(f"the archival transaction lookup at {archival.url} failed: {type(error).__name__}: {error}") from error
        if "error" not in archived:
            return _deposit_result(state, archival.call("tx", params))
        proof["archival_lookup"] = {"rpc": archival.url, "params": dict(params, wait_until="NONE"),
                                    "response": archived}
        independent = rpc.Client(rpc.NEARBLOCKS_URL, client.mode, kind="http", fixtures_dir=client.fixtures_dir)
        latest_path = "/v3/blocks/latest"
        try:
            latest = independent.get(latest_path, params={"limit": 1})
        except intents.UNAVAILABLE as error:
            raise ValueError(f"the independent NearBlocks latest block lookup at {independent.url}{latest_path} failed: "
                             f"{type(error).__name__}: {error}") from error
        if (not isinstance(latest, dict) or set(latest) - {"data", "meta"}
                or not isinstance(latest.get("data"), list) or len(latest["data"]) != 1
                or not isinstance(latest["data"][0], dict)):
            raise ValueError("the NearBlocks latest indexed block response is malformed")
        block = latest["data"][0]
        indexed_height = _units(block.get("block_height"), "NearBlocks indexed height")
        block_hash = block.get("block_hash")
        if (indexed_height > canon.U64_MAX or not isinstance(block_hash, str) or not 32 <= len(block_hash) <= 44
                or len(base58.decode(block_hash)) != 32):
            raise ValueError("the NearBlocks latest indexed block height or hash is malformed")
        required_height = height + period + INDEXER_MARGIN_BLOCKS
        if indexed_height < required_height:
            raise ValueError(f"the NearBlocks indexed height {indexed_height} has not reached required height "
                             f"{required_height} (reference plus validity period plus {INDEXER_MARGIN_BLOCKS} blocks); "
                             "do not redeposit yet")
        proof["independent_indexed_block"] = {"url": independent.url + latest_path, "params": {"limit": 1},
                                              "response": latest, "required_height": required_height}
        path = f"/v3/txns/{state['deposit_hash']}"
        try:
            found = independent.get(path)
        except intents.UNAVAILABLE as error:
            raise ValueError(f"the independent NearBlocks lookup at {independent.url}{path} failed: "
                             f"{type(error).__name__}: {error}") from error
        if isinstance(found, dict) and isinstance(found.get("data"), dict):
            if found["data"].get("transaction_hash") == state["deposit_hash"]:
                raise ValueError("the independent NearBlocks lookup found the original deposit while "
                                 "the archival RPC returned UNKNOWN_TRANSACTION; do not redeposit yet")
            raise ValueError("the independent NearBlocks lookup returned an unrelated transaction; do not redeposit yet")
        if found != {"data": None}:
            raise ValueError("the independent NearBlocks absence response is malformed; expected exactly data: null")
        proof["independent_lookup"] = {"url": independent.url + path, "response": found}
        nonce_reason = (f"the key nonce is consumed ({key['nonce']} >= {nonce}) and the archival RPC "
                        f"{archival.url} also returns UNKNOWN_TRANSACTION for the original hash, "
                        f"with independent NearBlocks absence at {independent.url}{path} "
                        f"after indexed height {indexed_height} reached {required_height}")
    state.update(verdict="ASK", phase="deposit_expired",
                 deposit_expiry=proof,
                 reason=f"Deposit {state['deposit_hash']} is UNKNOWN_TRANSACTION at finalized height {final_height}, "
                        f"past reference height {height} plus validity period {period}; {nonce_reason}. "
                        "The deposit expired without inclusion. Start a fresh run with a new report.")
    return False


def _poll(state, client, evm, *, clock, sleep, polls, handle):
    for index in range(polls):
        delay = POLL_SECONDS
        response = client.get("/v0/status", params={"depositAddress": state["quote_response"]["quote"]["depositAddress"]})
        lifecycle = check_status(state["quote_response"], response, clock=clock)
        state["status"] = response
        state["phase"] = "polling"
        _save(state, handle)
        if lifecycle in ("FAILED", "REFUNDED", "INCOMPLETE_DEPOSIT"):
            raise ValueError(f"{lifecycle}: {STATUS_ACTIONS[lifecycle]}")
        if lifecycle == "SUCCESS":
            origins = response["swapDetails"]["originChainTxHashes"]
            if state["deposit_hash"] not in [row["hash"] for row in origins]:
                raise ValueError("SUCCESS does not name the original deposit transaction")
            try:
                receipt = destination_receipt(state["quote_response"], response, client=evm)
            except SettlementPending:
                delay = FINALITY_POLL_SECONDS
                if index + 1 == polls:
                    raise
            else:
                state.update(verdict="ALLOW", phase="settled", settlement=receipt,
                             reason="The original deposit and finalized destination receipt match the judged quote.")
                return state
        if index + 1 < polls:
            sleep(delay)
    raise ValueError("polling ended without a proven settlement; resume the original report, never redeposit")


def _catalog(client, destination):
    chain, contract = intents.parse_destination(destination)
    if chain not in intents.CHAIN_IDS:
        raise ValueError("the destination is not a supported EVM chain")
    tokens = client.get("/v0/tokens")
    if not isinstance(tokens, list):
        raise ValueError("the token catalog is not a list")
    for asset, expected_chain, expected_contract in ((ORIGIN, "near", TOKEN), (destination, chain, contract)):
        matches = [token for token in tokens if isinstance(token, dict) and token.get("assetId") == asset]
        if len(matches) != 1:
            raise ValueError("the exact asset is missing or repeated in the provider catalog")
        token = matches[0]
        address = token.get("contractAddress")
        if (token.get("blockchain") != expected_chain or type(token.get("decimals")) is not int
                or not 0 <= token["decimals"] <= 24
                or (address.lower() if isinstance(address, str) else address) != expected_contract
                or asset == ORIGIN and token["decimals"] != 24):
            raise ValueError("the provider catalog disagrees with the asset binding")
    return chain


def settle(sender, destination, amount, recipient, *, min_amount_out, live=False, key_file=None, out=None,
           mode="off", fixtures_root=intents.FIXTURES_ROOT, firewall_url=None, clock=time.time,
           sleep=time.sleep, polls=60, override_reports=None):
    """Prepare the entire swap; fund it once only with live=True. Never retries a deposit."""
    canon.account_id(sender, "sender")
    if sender.endswith(".testnet"):
        raise ValueError("1Click settlement uses mainnet")
    chain, _ = intents.parse_destination(destination)
    if chain not in intents.CHAIN_IDS:
        raise ValueError("the destination is not a supported EVM chain")
    if _units(amount, "amount") > MAX_AMOUNT:
        raise ValueError("amount exceeds the hard cap of 0.25 wNEAR")
    _units(min_amount_out, "minimum output")
    if not isinstance(recipient, str) or not intents._EVM_ADDRESS.fullmatch(recipient):
        raise ValueError("the recipient is not an EVM address")
    if type(polls) is not int or not 1 <= polls <= 120:
        raise ValueError("polls must be from 1 to 120")
    if type(live) is not bool or live and (mode != "off" or not key_file or not out):
        raise ValueError("--deposit requires mode off, an external key file and a new external report")
    if live:
        key_file = _external(key_file, "key file")
    report = _external(out, "report") if out else None
    if override_reports and not live:
        raise ValueError("--override-report requires --deposit")
    overrides = {_external(path, "override report") for path in (override_reports or [])}
    override_keys = {_report_key(path) for path in overrides}
    if live:
        if report.exists():
            raise FileExistsError(f"the report already exists: {report}")
        report_key = _report_key(report)
        with _run_registry(sender) as registry:
            reservations = {_report_key(path): (path, attempted, reserved) for path, attempted, reserved in registry.execute(
                "SELECT report, signing_attempted, reserved_at_ms FROM open_runs")}
            pending = set(reservations)
            for path in report.parent.iterdir():
                if not path.is_file() or path.resolve() == key_file:
                    continue
                previous = json.loads(path.read_text(encoding="utf-8"))
                if (isinstance(previous, dict) and (previous.get("phase") in (
                        "prepared", "broadcast", "deposited", "submitted", "polling")
                        or previous.get("version") == 1 and any(previous.get(field) for field in (
                            "deposit_hash", "deposit_reference", "signed_tx_base64", "signed_transaction", "signed_tx"))
                        and previous.get("phase") not in ("settled", "deposit_failed", "deposit_expired"))):
                    pending.add(_report_key(path))
            if override_keys - pending:
                raise ValueError("--override-report must name an unresolved report in the account registry or --out directory")
            if pending - override_keys:
                names = ", ".join(sorted(pending - override_keys))
                raise ValueError(f"unresolved settlement reports: {names}; do not redeposit yet; "
                                 "use intents-resume for a bound deposit, intents-release --sender "
                                 f"{sender} --report PATH for an unsigned or proven failed/expired run, "
                                 "or check the chain before deliberately bypassing each with --override-report")
            for path in sorted(override_keys):
                stored, attempted, _ = reservations.get(path, (None, None, None))
                if attempted == 0:
                    deleted = registry.execute("DELETE FROM open_runs WHERE report = ? AND signing_attempted = 0",
                                               (stored,)).rowcount
                    if deleted != 1:
                        raise ValueError("could not revoke the overridden unsigned settlement reservation; nothing was signed")
            registry.execute("INSERT INTO open_runs (report, signing_attempted, reserved_at_ms) VALUES (?, 0, ?)",
                             (report_key, int(clock() * 1000)))
            report.touch(exist_ok=False)
    state = {"version": 1, "sender": sender, "amount": amount, "destination": destination,
             "recipient": recipient, "min_amount_out": min_amount_out, "verdict": "UNKNOWN",
             "phase": "started", "deposit_hash": None, "reason": "The swap has not been checked."}
    if overrides:
        state["override_reports"] = [str(path) for path in sorted(overrides)]
    if report and not live:
        report.touch(exist_ok=False)
    handle = report
    signing_attempted, resolved = False, False
    try:
        _save(state, handle)
        if live:
            if overrides:
                print("WARNING: a Windows power cut can roll a broadcast report back to ready. "
                      "Check the chain before any override; a recent transaction page cannot prove non-inclusion. "
                      "The old deposit may still execute or settle.", flush=True)
                risks = []
                for path in sorted(overrides):
                    _, attempted, _ = reservations.get(_report_key(path), (None, None, None))
                    if attempted != 0:
                        risks.append(f"signing was attempted or is unknown for {path}")
                try:
                    recent = _recent_transactions(sender, mode=mode, fixtures_root=fixtures_root)
                    state["override_lookup"] = recent
                except intents.UNAVAILABLE as error:
                    detail = f"{type(error).__name__}: {error}"
                    state["override_lookup"] = {"error": detail}
                    risks.append(f"the sender transaction lookup failed: {detail}")
                else:
                    for path in sorted(overrides):
                        _, _, reserved = reservations.get(_report_key(path), (None, None, None))
                        if type(reserved) is not int or reserved < 0:
                            risks.append(f"the old reservation time is unknown for {path}")
                            continue
                        for row in recent["response"]["data"]:
                            if int(row["block_timestamp"]) >= (reserved - CLOCK_SKEW_MS) * 1000000:
                                risks.append(f"transaction {row['transaction_hash']} from {sender} to {TOKEN} "
                                             f"is at or after reservation {path} ({reserved} ms) "
                                             f"minus {CLOCK_SKEW_MS} ms clock skew")
                if risks:
                    state["override_confirmation_required"] = risks
                    print("Override requires confirmation: " + "; ".join(risks), flush=True)
                    _save(state, handle)
                    if not sys.stdin.isatty():
                        raise ValueError("override requires typed interactive confirmation before key loading; "
                                         "non-interactive stdin refuses; nothing was signed")
                    confirmation = f"OVERRIDE {sender}"
                    try:
                        answer = input(f"Type '{confirmation}' to accept a possible duplicate payment: ")
                    except EOFError as error:
                        raise ValueError("override confirmation input ended; nothing was signed") from error
                    if answer != confirmation:
                        raise ValueError("override confirmation did not match; nothing was signed")
                    state["override_confirmed"] = True
                _save(state, handle)
            key_document = json.loads(key_file.read_text(encoding="utf-8"))
            if (not isinstance(key_document, dict)
                    or "account_id" in key_document and key_document["account_id"] != sender):
                raise ValueError("the external key file does not name the sender")
            tx_signer = signer.NaClSigner(key_file)
        root = Path(fixtures_root)
        client = rpc.Client(intents.ONECLICK_URL, mode, kind="http", fixtures_dir=root / "oneclick")
        near = rpc.Client(rpc.MAINNET_RPC_URL, mode, fixtures_dir=root / "oneclick")
        if near.call("status", []).get("chain_id") != "mainnet":
            raise ValueError("the origin RPC is not mainnet")
        chain = _catalog(client, destination)
        evm = rpc.Client(intents.EVM_RPC_URLS[chain], mode, fixtures_dir=root / "oneclick")
        _chain(evm, chain)
        # Check support for finalized reads before making a deposit.
        finalized = evm.call("eth_getBlockByNumber", ["finalized", False])
        _quantity(finalized.get("number"), "finalized block")
        deadline = intents.live_deadline(clock)
        judged = intents.judge_quote(ORIGIN, destination, amount, recipient, mode=mode, fixtures_root=root,
                                     firewall_url=firewall_url, clock=clock, deadline=deadline, refund_to=sender,
                                     quote_verifier=verify_quote_signature, clock_skew_ms=CLOCK_SKEW_MS)
        state["judgement"] = judged
        if judged["verdict"] != "ALLOW":
            state.update(verdict=judged["verdict"], reason="The recipient check did not ALLOW the transfer.")
            return state
        request = dict(judged["document"]["subject"]["quote_request"], dry=False)
        response = client.post("/v0/quote", request)
        check_quote(judged, response, min_amount_out=min_amount_out, clock=clock)
        state["quote_response"] = response
        status = client.get("/v0/status", params={"depositAddress": response["quote"]["depositAddress"]})
        check_status(response, status, before_deposit=True, clock=clock)
        registration = _funding(sender, response, near)
        check_quote(judged, response, min_amount_out=min_amount_out, clock=clock)
        quote = response["quote"]
        print(f"deposit: {amount} wNEAR base units from {sender} to {quote['depositAddress']}", flush=True)
        print(f"receive: {quote['amountOut']} of {destination} at {recipient}; minimum {quote['minAmountOut']}", flush=True)
        print(f"refund: {sender}; refund fee {quote['refundFee']} origin base units; withdrawal fee "
              f"{quote['withdrawFee']} destination base units; app fees {json.dumps(response['quoteRequest'].get('appFees', []))}", flush=True)
        print(f"refund deadline: {response['quoteRequest']['deadline']}; address expiry: {quote['deadline']}; "
              f"cold time: {quote.get('timeWhenInactive', 'not supplied')}; cap: 0.25 wNEAR; "
              f"gas: 50 Tgas plus {30 if registration else 0} Tgas for registration; "
              f"registration: {registration} yoctoNEAR; transfer attachment: 1 yoctoNEAR", flush=True)
        state.update(verdict="ASK", phase="ready", storage_deposit=str(registration),
                     reason="Ready; --deposit is required to fund. Nothing was signed or deposited.")
        _save(state, handle)
        if not live:
            return state
        with _run_registry(sender) as registry:
            updated = registry.execute("UPDATE open_runs SET signing_attempted = 1 WHERE report = ? AND signing_attempted = 0",
                                       (report_key,)).rowcount
            if updated != 1:
                raise ValueError("the settlement reservation was released before signing or its signing marker is not zero; "
                                 "nothing was signed")
        signing_attempted = True
        tx_hash, signed, registration, reference = _signed_deposit(sender, response, client=near, tx_signer=tx_signer)
        state.update(verdict="UNKNOWN", phase="prepared", deposit_hash=tx_hash, storage_deposit=str(registration),
                     deposit_reference=reference,
                     reason="A deposit was signed. Resume this report; do not redeposit yet, even if it says prepared.")
        _save(state, handle)
        status = client.get("/v0/status", params={"depositAddress": quote["depositAddress"]})
        check_status(response, status, before_deposit=True, clock=clock)
        check_quote(judged, response, min_amount_out=min_amount_out, clock=clock)
        state.update(phase="broadcast", reason="The deposit may execute. Resume this report; never redeposit.")
        _save(state, handle)
        result = near.call("send_tx", {"signed_tx_base64": signed, "wait_until": "FINAL"})
        if not _deposit_result(state, result):
            resolved = True
            return state
        state["phase"] = "deposited"
        _save(state, handle)
        submitted = client.post("/v0/deposit/submit", {"depositAddress": quote["depositAddress"], "txHash": tx_hash,
                                                     "nearSenderAccount": sender})
        lifecycle = check_status(response, submitted, clock=clock)
        state["status"] = submitted
        state["phase"] = "submitted"
        _save(state, handle)
        if lifecycle in ("FAILED", "REFUNDED", "INCOMPLETE_DEPOSIT"):
            raise ValueError(f"{lifecycle}: {STATUS_ACTIONS[lifecycle]}")
        result = _poll(state, client, evm, clock=clock, sleep=sleep, polls=polls, handle=handle)
        resolved = True
        return result
    except (*intents.UNAVAILABLE, signer.SignerError, TypeError, KeyError, AttributeError, RecursionError) as error:
        state.update(verdict="UNKNOWN", reason=f"{type(error).__name__}: {error}")
        if signing_attempted:
            state["reason"] += "; the deposit may have executed. Resume this report; do not redeposit yet; never redeposit without proof."
        return state
    finally:
        saved = False
        try:
            _save(state, handle)
            saved = True
        finally:
            if live and (not signing_attempted or resolved and saved):
                with _run_registry(sender) as registry:
                    registry.execute("DELETE FROM open_runs WHERE report = ? AND (signing_attempted = 0 OR ?)",
                                     (report_key, signing_attempted))


def _bound_report(state):
    if not isinstance(state, dict) or not isinstance(state.get("quote_response"), dict):
        raise ValueError("the report has no allocated quote")
    response = state["quote_response"]
    quote, request = _quote_shape(response)
    if (state.get("version") != 1 or request["refundTo"] != state.get("sender")
            or request["amount"] != state.get("amount") or request["recipient"] != state.get("recipient")
            or request["destinationAsset"] != state.get("destination") or not state.get("deposit_hash")):
        raise ValueError("the report is not a bound deposit report")
    if (not isinstance(state["deposit_hash"], str) or not 32 <= len(state["deposit_hash"]) <= 44
            or len(base58.decode(state["deposit_hash"])) != 32):
        raise ValueError("the deposit hash is malformed")
    if int(quote["minAmountOut"]) < _units(state.get("min_amount_out"), "minimum output"):
        raise ValueError("the quote no longer enforces the owner's output floor")
    # Only integrity and request binding apply after a deposit. Quote expiry cannot hide a later refund.
    judged = state.get("judgement")
    if not isinstance(judged, dict):
        raise ValueError("the report has no recipient judgement")
    reference = max(judged["document"]["observed"]["timestamp_ms"] / 1000,
                    intents._instant(response["timestamp"]).timestamp())
    intents.verify_binding(judged, judged["document"]["subject"]["quote_request"],
                           clock=lambda: reference, clock_skew_ms=CLOCK_SKEW_MS)
    check_quote(judged, response, min_amount_out=state["min_amount_out"],
                clock=lambda: reference)
    return state


def _recent_transactions(sender, *, mode, fixtures_root):
    path = f"/v3/accounts/{sender}/txns"
    params = {"signer": sender, "receiver": TOKEN, "limit": 10}
    client = rpc.Client(rpc.NEARBLOCKS_URL, mode, kind="http", fixtures_dir=Path(fixtures_root) / "oneclick")
    print(f"recent transaction lookup: {client.url}{path}?signer={sender}&receiver={TOKEN}&limit=10", flush=True)
    response = client.get(path, params=params)
    if isinstance(response, dict) and set(response) - {"data", "meta"}:
        raise ValueError("the sender's recent transaction lookup contains provider errors or unexpected fields")
    rows = response.get("data") if isinstance(response, dict) else None
    if not isinstance(rows, list) or len(rows) > 10:
        raise ValueError("the sender's recent transaction lookup has no bounded data list")
    for row in rows:
        if (not isinstance(row, dict) or row.get("signer_account_id") != sender or row.get("receiver_account_id") != TOKEN
                or not isinstance(row.get("transaction_hash"), str)
                or len(base58.decode(row["transaction_hash"])) != 32):
            raise ValueError("the sender's recent transaction lookup has a malformed or unrelated transaction")
        _units(row.get("block_timestamp"), "recent transaction timestamp")
        if len(row["block_timestamp"]) != 19:
            raise ValueError("the recent transaction timestamp is not a 19 digit nanosecond timestamp")
    print(json.dumps(response, sort_keys=True, ensure_ascii=True), flush=True)
    return {"url": client.url + path, "params": params, "response": response}


def release(sender, path, *, mode="off", fixtures_root=intents.FIXTURES_ROOT):
    """Read proof, then release exactly one reservation. Never signs or sends a transaction."""
    canon.account_id(sender, "sender")
    if sender.endswith(".testnet"):
        raise ValueError("1Click settlement uses mainnet")
    report = _external(path, "report")
    report_key = _report_key(report)
    with _run_registry(sender) as registry:
        row = registry.execute("SELECT signing_attempted FROM open_runs WHERE report = ?", (report_key,)).fetchone()
        if row is None:
            raise ValueError("the report is not reserved in this sender's settlement registry")
        state = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") != 1 or state.get("sender") != sender:
            raise ValueError("the report does not name this sender and settlement version")
        if state.get("deposit_hash") is None:
            if (row[0] != 0 or state.get("phase") not in ("started", "ready")
                    or any(state.get(field) is not None for field in ("deposit_reference", "signed_tx_base64",
                                                                     "signed_transaction", "signed_tx"))):
                raise ValueError("cannot prove this run stopped before signing; restore the signed report for "
                                 "intents-resume or intents-release, or check the chain before --override-report")
            state.update(verdict="ASK", phase="released", deposit_hash=None,
                         reason="The registry proves no signing was attempted and the report has no signed transaction. "
                                "Released this reservation; a fresh run requires a new report.")
        else:
            state = _bound_report(state)
            near = rpc.Client(rpc.MAINNET_RPC_URL, mode, fixtures_dir=Path(fixtures_root) / "oneclick")
            if _broadcast_result(state, near):
                raise ValueError("the original deposit finalized successfully; use intents-resume to reconcile "
                                 "its settlement; this release command cannot prove that no funds moved")
        state["registry_released"] = True
        _save(state, report)
        registry.execute("DELETE FROM open_runs WHERE report = ?", (report_key,))
    return state


def resume(path, *, mode="off", fixtures_root=intents.FIXTURES_ROOT, clock=time.time, sleep=time.sleep, polls=60):
    """Read an existing deposit and poll it. This path cannot create a quote, sign or broadcast."""
    if type(polls) is not int or not 1 <= polls <= 120:
        raise ValueError("polls must be from 1 to 120")
    report = _external(path, "report")
    report_key = _report_key(report)
    state = _bound_report(json.loads(report.read_text(encoding="utf-8")))
    state.pop("registry_released", None)
    if mode == "off":
        with _run_registry(state["sender"]) as registry:
            registry.execute("INSERT OR IGNORE INTO open_runs (report) VALUES (?)", (report_key,))
    chain, _ = intents.parse_destination(state["destination"])
    root = Path(fixtures_root)
    client = rpc.Client(intents.ONECLICK_URL, mode, kind="http", fixtures_dir=root / "oneclick")
    near = rpc.Client(rpc.MAINNET_RPC_URL, mode, fixtures_dir=root / "oneclick")
    evm = rpc.Client(intents.EVM_RPC_URLS[chain], mode, fixtures_dir=root / "oneclick")
    resolved = False
    try:
        state.update(verdict="UNKNOWN", reason="The original deposit is being checked. Resume this report; do not redeposit yet.")
        if state.get("phase") in ("prepared", "broadcast", "deposit_expired"):
            if not _broadcast_result(state, near):
                resolved = True
                return state
        else:
            result = near.call("tx", {"tx_hash": state["deposit_hash"], "sender_account_id": state["sender"],
                                      "wait_until": "FINAL"})
            if not _deposit_result(state, result):
                resolved = True
                return state
        result = _poll(state, client, evm, clock=clock, sleep=sleep, polls=polls, handle=report)
        resolved = True
        return result
    except (*intents.UNAVAILABLE, TypeError, KeyError, AttributeError, RecursionError) as error:
        state.update(verdict="UNKNOWN", reason=f"{type(error).__name__}: {error}; resume this report; "
                                               "do not redeposit yet; never redeposit without proof.")
        return state
    finally:
        _save(state, report)
        if mode == "off" and resolved:
            with _run_registry(state["sender"]) as registry:
                registry.execute("DELETE FROM open_runs WHERE report = ?", (report_key,))
