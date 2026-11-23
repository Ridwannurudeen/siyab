"""The EVM leg: where NEAR money goes when a 1Click quote sends it to an EVM chain.

judge_quote takes its own dry 1Click quote (the body always carries "dry": true, so no funds can
be deposited from the request), reads the recipient, the destination chain and amountOut from it,
asks GoPlus address_security about the recipient, reads the recipient's code with eth_getCode, and
calls ShieldBot's POST /api/firewall only when SHIELDBOT_API_URL is set and the recipient has code.
The verdict is ALLOW, DENY or UNKNOWN with an evidence document of kind intents, and it is never
signed: it is a read-only preflight whose consumers verify the judged request before returning it.

The firewall's answer alone never makes an ALLOW: a classification in FIREWALL_SIGNALS is a
signal; otherwise the recipient passes only on a complete scan whose classification is one of the
known others in FIREWALL_CLASSIFICATIONS, and is unknown on anything else. The verdict still needs
the GoPlus check to have run. Every request goes through gate.rpc.Client, so fixtures replay
offline: fixtures/intents (1Click), fixtures/goplus (GoPlus) and fixtures/evm (eth_getCode and the
firewall).

The document records the request judged, including the refund account and any app fees the caller
names. The dry answer must echo every sent field (the deadline as the same instant), carry the
named app fees plus at most the service fee SERVICE_FEE_FILE records, and name nothing _unjudged
reports, or quote_dry is a signal; when that file cannot be read, one unnamed fee makes quote_dry
unknown instead. Every quote request is held to the one quote request schema (_request_schema) and
the dry answer to the quote answer schema (_answer_schema): the caller's request outside it is
refused before any request, a dry answer or an echo outside it makes quote_dry unknown (an echoed
field that differs from the sent one is a signal first), a later request outside it is refused by
verify_binding and a status quote request outside it makes deposit_address_status unknown. A judged
deadline that is not after the judge's clock reads UNKNOWN (quote_deadline); one that is not a
complete date-time with an explicit zone is refused before any request. A dry quote has no deposit
address, so verify_binding requires every judged field but dry in the caller's request, the
deadline as the same instant (_request_binding), and refuses an ALLOW whose judged deadline is not
after its own clock or whose quote is then older than MAX_QUOTE_AGE_MS (_expired). When a caller
supplies the address from a real quote, bind_deposit_address reads its 1Click status, requires dry
false there, compares the quote request that created it with the judged one, requires the real
quote's depositAddress to be exactly the supplied address and the status answer's status to be one
of DEPOSIT_STATUSES (_status_binding); the status deadline, the real quote's own deadline and
timestamp, the judged deadline and the quote age are then judged at one clock reading taken after
that read. An old timestamp, its age taken at the same clock as the deadline, reads UNKNOWN. The
legacy binder does not authenticate a real quote's economics, so DEPOSIT_STATUSES is empty and a supplied
deposit address makes deposit_address_status unknown whatever its status. gate.oneclick handles
authenticated allocation and settlement using the recorded status catalog. A 1Click, GoPlus or firewall
answer with a lone surrogate (text with no UTF-8 form) makes its check unknown (_encodable); a caller's
request with one is refused before any request, a deposit address before the status request. The
quote's signature is checked only when the caller supplies quote_verifier.
"""

import hashlib
import http.client
import json
import math
import re
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gate import canon, evidence, rpc
from intel import sanctions

FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "fixtures"
ONECLICK_URL = "https://1click.chaindefuser.com"
GOPLUS_URL = "https://api.gopluslabs.io"
CHAIN_IDS = {"eth": 1, "bsc": 56, "base": 8453, "arb": 42161, "op": 10, "pol": 137, "hood": 4663}
INTENTS_OFAC_ASSETS = {
    "arb": ("ARB",),
    "bch": ("BCH",),
    "bsc": ("BNB", "BSC"),
    "btc": ("XBT",),
    "dash": ("DASH",),
    "doge": ("DOGE",),
    "eth": ("ETH",),
    "ltc": ("LTC",),
    "sol": ("SOL",),
    "tron": ("TRX",),
    "xrp": ("XRP",),
    "zec": ("ZEC",),
}
# Public JSON-RPC endpoints for eth_getCode. Base is the one the recorded fixtures use; the others
# are the chains' published public endpoints and are UNVERIFIED until a fixture is recorded on them.
EVM_RPC_URLS = {
    "eth": "https://ethereum-rpc.publicnode.com",
    "bsc": "https://bsc-dataseed.bnbchain.org",
    "base": "https://mainnet.base.org",
    "arb": "https://arb1.arbitrum.io/rpc",
    "op": "https://mainnet.optimism.io",
    "pol": "https://polygon-rpc.com",
    "hood": "https://rpc.mainnet.chain.robinhood.com",
}
# The NEAR Intents shared EVM treasury from the treasury page, documented as the address that sends
# 1Click withdrawals; used as the firewall's sender. A choice, not a protocol fact.
INTENTS_EVM_TREASURY = "0x2CfF890f0378a11913B6129B2E97417a2c302680"
# The firewall classifications that are a signal. The other two (CAUTION and the lowest band) are
# third party strings, so they are data in FIREWALL_CLASSIFICATIONS; they pass only on a scan whose
# status is FIREWALL_COMPLETE, partial false and failed_sources an empty list, and are never a reason
# for ALLOW.
FIREWALL_SIGNALS = ("HIGH_RISK", "BLOCK_RECOMMENDED")
FIREWALL_CLASSIFICATIONS = FIXTURES_ROOT / "firewall" / "classifications.json"
FIREWALL_COMPLETE = "ok"
# Copied verbatim from ShieldBot (baseline, commit 9e2a6ac) services/counterparty_service.py:27-41,
# the one copied baseline constant: the GoPlus address_security labels that count when "1".
THEFT_LABELS = (
    "phishing_activities",
    "stealing_attack",
    "blacklist_doubt",
    "honeypot_related_address",
    "fake_token",
    "cybercrime",
    "money_laundering",
    "financial_crime",
    "blackmail_activities",
    "sanctioned",
    "malicious_mining_activities",
    "mixer",
    "darkweb_transactions",
)
# The quote fields the request-a-swap-quote reference marks required, besides the four inputs.
SWAP_TYPE = "EXACT_INPUT"
SLIPPAGE_BPS = 100
DEPOSIT_TYPE = "ORIGIN_CHAIN"
# The refund account of the quote: a NEAR account chosen by us; a dry quote refunds nothing.
REFUND_TO = "intents.near"
REFUND_TYPE = "ORIGIN_CHAIN"
# The refundType enum of the quote request schema in intel/seeds/raw/intents-quote-doc-20261029T114539Z.md.
REFUND_TYPES = ("ORIGIN_CHAIN", "INTENTS", "CONFIDENTIAL_INTENTS")
# The other enums of that schema and the fields it requires.
SWAP_TYPES = ("EXACT_INPUT", "EXACT_OUTPUT", "FLEX_INPUT", "ANY_INPUT")
DEPOSIT_TYPES = ("ORIGIN_CHAIN", "INTENTS", "CONFIDENTIAL_INTENTS")
RECIPIENT_TYPES = ("DESTINATION_CHAIN", "INTENTS", "CONFIDENTIAL_INTENTS")
DEPOSIT_MODES = ("SIMPLE", "MEMO")
CONFIDENTIALITY_MODES = ("public", "basic", "advanced")
REQUEST_REQUIRED = ("dry", "swapType", "slippageTolerance", "originAsset", "depositType", "destinationAsset",
                    "amount", "refundTo", "refundType", "recipient", "recipientType", "deadline")
# The deadline of the recorded quotes, so the replayed request body matches the recorded one.
RECORDED_DEADLINE = "2026-10-30T00:00:00Z"
# A check that meets one of these could not run.
UNAVAILABLE = (rpc.FixtureMiss, rpc.HttpError, rpc.RateLimited, rpc.RpcError, rpc.TransportError, OSError,
               ValueError)
# The request fields verify_binding compares with the later request, with their subject keys. A quote
# answer must echo every sent field, these included; the deadline is compared as an instant, because
# 1Click echoes it with milliseconds.
BOUND_FIELDS = (("originAsset", "origin_asset"), ("destinationAsset", "destination_asset"), ("amount", "amount"),
                ("recipient", "recipient"), ("recipientType", "recipient_type"))
REFUND_FIELDS = ("refundTo", "refundType")
# The 1Click quoteRequest keys beyond the judged request, in a dry echo or a status answer, from the
# quote request schema in intel/seeds/raw/intents-quote-doc-20261029T114539Z.md and the recorded
# echo. Each of STATUS_DEFAULTS must be absent or its default, each of STATUS_ABSENT absent or empty,
# and STATUS_INFORMATIONAL is not compared (dry is, by its own checks). _unjudged names any other key.
STATUS_DEFAULTS = {"depositMode": "SIMPLE", "insured": False, "confidentiality": "public"}
STATUS_ABSENT = ("virtualChainRecipient", "virtualChainRefundRecipient", "customRecipientMsg", "rebates")
STATUS_INFORMATIONAL = ("dry", "connectedWallets", "sessionId", "referral", "quoteWaitingTimeMs")
# The legacy binder does not authenticate a real quote's economics, so it accepts no status.
# gate.oneclick uses the recorded catalog and checks the complete signed allocation instead.
DEPOSIT_STATUSES = ()
# The one app fee a dry echo may carry beyond the named ones: the fee 1Click added by itself to the
# recorded dry echo of a request that named no app fee, kept as data in fixtures/oneclick/service-fee.json
# with the name of the fixture it comes from. When that file cannot be read, or holds anything but
# {recipient, fee} with a whole fee, one unnamed fee is unknown, since missing local data cannot tell
# it from the service fee. Any other unnamed fee is a signal, and a later request or status quote
# request binds only the named fees.
SERVICE_FEE_FILE = FIXTURES_ROOT / "oneclick" / "service-fee.json"
# The date-time format of the quote request schema: a complete date and time with an explicit zone,
# month 01 to 12, day 01 to 31, hour 00 to 23, minute and second 00 to 59, and Z or an offset of hours
# 00 to 23 and minutes 00 to 59, checked before parsing so no component out of range is normalised.
_DATE_TIME = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
                        r"(\.[0-9]+)?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])")
_OMFT = ".omft.near"
_CHAIN_CODE = re.compile(r"[a-z][a-z0-9]*")
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
# An amountOut or minAmountOut of a quote answer: a decimal without sign, spaces or leading zeros.
_DECIMAL = re.compile(r"0|[1-9][0-9]*")


def _why(error):
    if isinstance(error, rpc.FixtureMiss):
        return "no recorded response for the request"
    if isinstance(error, rpc.HttpError):
        return f"HTTP {error.status}"
    return f"{type(error).__name__}: {error}"


def _quoted(text):
    """A third party string as quoted data, never as Siyab's own words."""
    return json.dumps(text, ensure_ascii=False)


def _dry_quote_why(error):
    reason = _why(error)
    cause = error.__cause__
    if not isinstance(error, rpc.HttpError) or not isinstance(cause, urllib.error.HTTPError):
        return reason
    try:
        rpc.check_request_deadline()
        body = cause.read(65536)
        rpc.check_request_deadline()
        answer = json.loads(body)
        message = answer.get("message") if isinstance(answer, dict) else None
        if isinstance(message, str):
            _encodable(message, "1Click error message")
            return f"{reason}, 1Click says {_quoted(message[:200])}"
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return reason
    return reason


def _encodable(answer, name):
    """Raise MalformedResponse, a ValueError, when answer, a parsed provider answer or a caller's
    request, holds a key or string with a lone surrogate: valid JSON as an escape, but text with no
    UTF-8 form, so no evidence document can record it. The message never repeats the text. An answer
    nested too deeply to encode raises it too."""
    try:
        json.dumps(answer, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as error:
        raise rpc.MalformedResponse(f"the {name} holds a lone surrogate, text with no UTF-8 form") from error
    except RecursionError as error:
        raise rpc.MalformedResponse(f"the {name} is nested too deeply to encode") from error


def parse_destination(asset_id):
    """(chain code, contract address) of a 1Click asset id: nep141:<chain>-0x<address>.omft.near
    gives the chain and the address, nep141:<chain>.omft.near the chain's native asset (address
    None), and any other nep141:<account> the token account on NEAR."""
    if not isinstance(asset_id, str) or not asset_id.startswith("nep141:") or len(asset_id) == len("nep141:"):
        raise ValueError(f"not a nep141 asset id: {asset_id!r}")
    account = asset_id[len("nep141:"):]
    if not account.endswith(_OMFT):
        return "near", canon.account_id(account, "destination asset account")
    bridged = account[: -len(_OMFT)]
    chain, separator, address = bridged.partition("-")
    if not _CHAIN_CODE.fullmatch(chain):
        raise ValueError(f"not a valid 1Click chain code in {asset_id!r}")
    if not separator:
        return chain, None
    if not (address.startswith("0x") and len(address) == 42 and all(c in "0123456789abcdefABCDEF" for c in address[2:])):
        raise ValueError(f"not an EVM contract address in {asset_id!r}")
    return chain, address


def quote_body(origin_asset, destination_asset, amount, recipient, recipient_type, deadline, *,
               refund_to=REFUND_TO, refund_type=REFUND_TYPE, app_fees=None):
    """The 1Click quote request body; dry is always true. appFees is sent only when app_fees is named."""
    body = {"dry": True, "swapType": SWAP_TYPE, "slippageTolerance": SLIPPAGE_BPS, "originAsset": origin_asset,
            "depositType": DEPOSIT_TYPE, "destinationAsset": destination_asset, "amount": amount,
            "refundTo": refund_to, "refundType": refund_type, "recipient": recipient,
            "recipientType": recipient_type, "deadline": deadline}
    if app_fees is not None:
        body["appFees"] = app_fees
    return body


def dry_quote(origin_asset, destination_asset, amount, recipient, recipient_type="DESTINATION_CHAIN", *,
              client, dry=True, deadline=RECORDED_DEADLINE, refund_to=REFUND_TO, refund_type=REFUND_TYPE,
              app_fees=None):
    """The JSON of POST /v0/quote for a dry quote. Anything but dry=True raises ValueError before a
    request is built."""
    if dry is not True:
        raise ValueError("the EVM leg takes dry quotes only; dry must be True")
    body = quote_body(origin_asset, destination_asset, amount, recipient, recipient_type, deadline,
                      refund_to=refund_to, refund_type=refund_type, app_fees=app_fees)
    if body["dry"] is not True:
        raise ValueError("the quote body must carry dry true")
    return client.post("/v0/quote", body)


def goplus_address(address, *, client):
    """{status, labels, data_source, record, source, reason}: status ok only when GoPlus answers
    code 1, an integer and not true or 1.0, with a non empty result in which each THEFT_LABELS key
    present is "0" or "1", the shape of the recorded answers, or at least one is "1"; a theft label
    may be absent. labels are the THEFT_LABELS whose value is "1"."""
    try:
        body = client.get(f"/api/v1/address_security/{address}")
        _encodable(body, "GoPlus answer")
    except UNAVAILABLE as error:
        return {"status": "unknown", "labels": [], "data_source": None, "record": None,
                "source": dict(client.last_source), "reason": _why(error)}
    result = body.get("result") if isinstance(body, dict) else None
    code = body.get("code") if isinstance(body, dict) else None
    if not (type(code) is int and code == 1 and isinstance(result, dict) and result):
        return {"status": "unknown", "labels": [], "data_source": None, "record": result,
                "source": dict(client.last_source),
                "reason": f"GoPlus answered code {code!r}, not the success code 1 with a record"}
    labels = [label for label in THEFT_LABELS if result.get(label) == "1"]
    malformed = [f"{label} {_quoted(result[label])}" for label in THEFT_LABELS
                 if label in result and result[label] not in ("0", "1")]
    if malformed and not labels:
        return {"status": "unknown", "labels": [], "data_source": result.get("data_source"), "record": result,
                "source": dict(client.last_source),
                "reason": f'GoPlus answered theft labels that are not "0" or "1": {", ".join(malformed)}'}
    return {"status": "ok", "labels": labels, "data_source": result.get("data_source"), "record": result,
            "source": dict(client.last_source), "reason": None}


def eth_get_code(chain_id, address, *, client=None):
    """The hex code at address on the chain with this id ("0x" when there is none)."""
    if client is None:
        chain = {number: name for name, number in CHAIN_IDS.items()}[chain_id]
        client = rpc.Client(EVM_RPC_URLS[chain], fixtures_dir=FIXTURES_ROOT / "evm")
    code = client.call("eth_getCode", [address, "latest"])
    if not isinstance(code, str) or not code.startswith("0x"):
        raise rpc.MalformedResponse("eth_getCode did not return hex")
    return code


def firewall(chain_id, recipient, value, *, client):
    """ShieldBot's POST /api/firewall for a plain transfer of value base units to recipient, sent
    from the NEAR Intents EVM treasury."""
    return client.post("/api/firewall", {"from": INTENTS_EVM_TREASURY, "to": recipient, "value": str(value),
                                         "data": "0x", "chainId": chain_id})


def _instant(text):
    """The datetime of a complete date-time with an explicit zone (_DATE_TIME), or None when text is
    not one: a date alone, a time without a zone or any other ISO form is not."""
    if not isinstance(text, str) or not _DATE_TIME.fullmatch(text):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _json(value):
    return json.dumps(value, sort_keys=True)


def _same(field, judged, named):
    if field == "recipient":
        return isinstance(judged, str) and isinstance(named, str) and named.lower() == judged.lower()
    if field == "deadline":
        return _instant(named) is not None and _instant(named) == _instant(judged)
    if field == "appFees":
        return _json(named) == _json(judged)
    return named is not None and type(named) is type(judged) and named == judged


def _answer_schema(response):
    """(quote, quoteRequest) of a dry quote answer, or MalformedResponse naming the first field this
    leg reads that the quote answer schema does not allow: quote and quoteRequest objects, amountOut
    and minAmountOut in quote decimal strings without sign, spaces or leading zeros, correlationId
    and timestamp strings. The schema requires each, so absent or null is malformed too; the
    timestamp's date-time format is judged by quote_age."""
    quote = response.get("quote") if isinstance(response, dict) else None
    request = response.get("quoteRequest") if isinstance(response, dict) else None
    if not isinstance(quote, dict) or not isinstance(request, dict):
        raise rpc.MalformedResponse("the quote answer has no quote or quoteRequest object")
    for name, holder in (("amountOut", quote), ("minAmountOut", quote), ("correlationId", response),
                         ("timestamp", response)):
        if name not in holder:
            raise rpc.MalformedResponse(f"the quote answer has no {name}")
        if not isinstance(holder[name], str):
            raise rpc.MalformedResponse(f"the quote answer {name} {_quoted(holder[name])} is not a string")
        if holder is quote and not _DECIMAL.fullmatch(quote[name]):
            raise rpc.MalformedResponse(f"the quote answer {name} {_quoted(quote[name])} is not a decimal string")
    return quote, request


def _quote_check(response, asked, chain, source):
    quote, request = _answer_schema(response)
    if request.get("dry") is not True:
        raise rpc.MalformedResponse("the quote answer does not echo dry true")
    binding = {"correlation_id": response["correlationId"], "timestamp": response["timestamp"],
               "amount_out": quote["amountOut"], "min_amount_out": quote["minAmountOut"]}
    differ = [f"{field} {_quoted(request.get(field))}, not {_quoted(asked[field])}" for field in asked
              if field != "appFees" and not _same(field, asked[field], request.get(field))]
    named_fees = asked.get("appFees", [])
    service, unreadable = _service_fee()
    added = _added_fees(named_fees, request, service)
    unnamed = None
    if added is None and service is None and isinstance(request.get("appFees"), list):
        unnamed = next((fee for fee in request["appFees"]
                        if _whole_fee(fee) and _added_fees(named_fees, request, fee) == [fee]), None)
    if added is None and unnamed is None:
        allowed = (f"with at most the 1Click service fee {_json(service)}" if service else
                   f"and no service fee, because the recorded 1Click service fee could not be read: {unreadable}")
        differ.append(f"appFees {_quoted(request.get('appFees'))}, not {_quoted(named_fees)} {allowed}")
    differ.extend(_unjudged(asked, request))
    if differ:
        return (evidence.check("quote_dry", "signal", f"the dry quote names {'; '.join(differ)}", source), quote,
                binding)
    try:
        _request_schema(request)
    except ValueError as error:
        return (evidence.check("quote_dry", "unknown", f"the dry quote echo is not a quote request the schema "
                               f"allows: {error}", source), quote, binding)
    if unnamed is not None:
        return (evidence.check("quote_dry", "unknown", f"the dry quote echo adds the unnamed app fee {_json(unnamed)} "
                               f"to the named appFees; it may be the 1Click service fee, whose record could not be "
                               f"read: {unreadable}", source), quote, binding)
    return _quote_dry_check(quote, request, chain, added, source), quote, binding


def _service_fee():
    """(fee, None) with the {recipient, fee} SERVICE_FEE_FILE records, or (None, reason) when the file
    cannot be read or holds anything else, so that no unnamed fee passes."""
    try:
        fee = json.loads(SERVICE_FEE_FILE.read_text(encoding="utf-8"))["service_fee"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        return None, f"{type(error).__name__}: {error}"
    if not _whole_fee(fee):
        return None, f"{SERVICE_FEE_FILE.name} holds {_json(fee)}, not a {{recipient, fee}} with a whole fee"
    return fee, None


def _whole_fee(fee):
    """True for a {recipient, fee} with a non empty recipient and a whole fee."""
    return (isinstance(fee, dict) and set(fee) == {"recipient", "fee"} and isinstance(fee["recipient"], str)
            and bool(fee["recipient"]) and type(fee["fee"]) is int and fee["fee"] >= 0)


def _added_fees(named, request, service):
    """The app fees a dry echo adds to the named ones: [] or [service], or None when its appFees is
    anything else (compared as JSON, so 20.0 or true is not 20). An absent appFees adds none, and a
    None service allows only []."""
    if "appFees" not in request:
        return [] if not named else None
    echoed = request["appFees"]
    if not isinstance(echoed, list):
        return None
    for added in ([], [service]) if service else ([],):
        if sorted(map(_json, echoed)) == sorted(map(_json, named + added)):
            return added
    return None


def _unjudged(judged, request):
    """What request carries that the judged request does not decide, one phrase each: keys this leg
    does not know, STATUS_DEFAULTS away from their default and STATUS_ABSENT set. Empty when none."""
    known = set(judged) | {"appFees"} | set(STATUS_DEFAULTS) | set(STATUS_ABSENT) | set(STATUS_INFORMATIONAL)
    unknown_keys = sorted(key for key in request if key not in known)
    problems = [f"keys this check does not know: {', '.join(unknown_keys)}"] if unknown_keys else []
    problems += [f"{field} {_quoted(request[field])}, not the default {_quoted(default)}"
                 for field, default in STATUS_DEFAULTS.items()
                 if field in request and _json(request[field]) != _json(default)]
    problems += [f"{field} {_quoted(request[field])} set, a route the judged request does not have"
                 for field in STATUS_ABSENT if request.get(field) not in (None, "", [])]
    return problems


def _number(value):
    """True for a JSON number: an int or a finite float, never a bool."""
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _string(value):
    return isinstance(value, str)


def _boolean(value):
    return type(value) is bool


def _one_of(values):
    return (lambda value: isinstance(value, str) and value in values,
            f"one of {', '.join(values[:-1])} or {values[-1]}")


def _entries(value, key, allowed):
    """True for a list of objects, each with a string recipient and a key whose value allowed accepts."""
    return isinstance(value, list) and all(isinstance(item, dict) and _string(item.get("recipient"))
                                           and key in item and allowed(item[key]) for item in value)


# Each field of the quote request schema in intel/seeds/raw/intents-quote-doc-20261029T114539Z.md, with
# a test of its type, enum or format and what that test requires; _request_schema applies it.
REQUEST_SCHEMA = {
    "dry": (_boolean, "a boolean"),
    "depositMode": _one_of(DEPOSIT_MODES),
    "swapType": _one_of(SWAP_TYPES),
    "slippageTolerance": (_number, "a number"),
    "originAsset": (_string, "a string"),
    "depositType": _one_of(DEPOSIT_TYPES),
    "destinationAsset": (_string, "a string"),
    "amount": (lambda value: _string(value) and bool(_DECIMAL.fullmatch(value)),
               "an integer string without sign, spaces or leading zeros"),
    "refundTo": (_string, "a string"),
    "refundType": _one_of(REFUND_TYPES),
    "recipient": (_string, "a string"),
    "connectedWallets": (lambda value: isinstance(value, list) and all(map(_string, value)), "a list of strings"),
    "sessionId": (_string, "a string"),
    "virtualChainRecipient": (_string, "a string"),
    "virtualChainRefundRecipient": (_string, "a string"),
    "customRecipientMsg": (_string, "a string"),
    "recipientType": _one_of(RECIPIENT_TYPES),
    "deadline": (lambda value: _instant(value) is not None, "a complete date-time with an explicit zone"),
    "confidentiality": _one_of(CONFIDENTIALITY_MODES),
    "referral": (_string, "a string"),
    "rebates": (lambda value: _entries(value, "share", lambda share: _number(share) and 0 <= share <= 100),
                "a list of {recipient, share} with a string recipient and a share from 0 to 100"),
    "quoteWaitingTimeMs": (_number, "a number"),
    "appFees": (lambda value: _entries(value, "fee", _number),
                "a list of {recipient, fee} with a string recipient and a number fee"),
    "insured": (_boolean, "a boolean"),
}


def _request_schema(request):
    """Raise ValueError naming the first field of request, a quote request object, that the quote
    request schema does not allow: a REQUEST_REQUIRED field missing, or a REQUEST_SCHEMA field
    present, null included, whose value fails its test. Keys outside the schema are _unjudged's."""
    for field in REQUEST_REQUIRED:
        if field not in request:
            raise ValueError(f"{field} is missing; the quote request schema requires it")
    for field, (allowed, kind) in REQUEST_SCHEMA.items():
        if field in request and not allowed(request[field]):
            raise ValueError(f"{field} {_quoted(request[field])} is not {kind}")


def _request_shape(amount, refund_type):
    """Raise ValueError unless amount is a decimal string from 1 to 2**128 - 1 without sign, spaces or
    leading zeros (canon.amount_field, so "0" is refused) and refund_type is one of REFUND_TYPES."""
    if not isinstance(amount, str):
        raise ValueError(f"amount: not a decimal string: {amount!r}")
    canon.amount_field(amount)
    if refund_type not in REFUND_TYPES:
        raise ValueError(f"refund_type: not ORIGIN_CHAIN, INTENTS or CONFIDENTIAL_INTENTS: {refund_type!r}")


def judge_quote(origin_asset, destination_asset, amount, recipient, recipient_type="DESTINATION_CHAIN", *,
                mode=None, fixtures_root=FIXTURES_ROOT, firewall_url=None, clock=time.time,
                deadline=RECORDED_DEADLINE, refund_to=REFUND_TO, refund_type=REFUND_TYPE, app_fees=None,
                quote_verifier=None, clock_skew_ms=0):
    """The EVM leg verdict for one quote: {verdict, checks, reasons, quote, document, evidence_hash}.
    Unsigned on every outcome. firewall_url is SHIELDBOT_API_URL or None. refund_to, refund_type and
    app_fees ([{recipient, fee}], sent only when named) go into the judged request. An optional
    quote_verifier must accept the raw response before it can pass. The quote age and
    the deadline are judged at clock, read once after the last external read and recorded as
    observed: an age over MAX_QUOTE_AGE_MS or a deadline that is not after it reads UNKNOWN.
    clock_skew_ms optionally permits up to 5000 ms at each quote age boundary, with zero by default. A
    recipient that is not 0x and 40 hex characters, a deadline that is not a complete date-time with
    an explicit zone, an amount or refund_type _request_shape refuses, a malformed refund account or
    app fee, any other field of the request _request_schema refuses, or a request holding a lone
    surrogate (_encodable), raises ValueError before any request."""
    if not isinstance(recipient, str) or not _EVM_ADDRESS.fullmatch(recipient):
        raise ValueError(f"recipient: not 0x and 40 hex characters: {recipient!r}")
    if _instant(deadline) is None:
        raise ValueError(f"deadline: not a complete date-time with an explicit zone: {deadline!r}")
    for name, value in (("refund_to", refund_to), ("refund_type", refund_type)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name}: not a non empty string: {value!r}")
    if app_fees is not None and not (isinstance(app_fees, list) and all(
            isinstance(fee, dict) and set(fee) == {"recipient", "fee"} and isinstance(fee["recipient"], str)
            and fee["recipient"] and type(fee["fee"]) is int and fee["fee"] >= 0 for fee in app_fees)):
        raise ValueError(f"app_fees: not a list of {{recipient, fee}} with a whole fee: {app_fees!r}")
    _request_shape(amount, refund_type)
    asked = quote_body(origin_asset, destination_asset, amount, recipient, recipient_type, deadline,
                       refund_to=refund_to, refund_type=refund_type, app_fees=app_fees)
    _request_schema(asked)
    _encodable(asked, "quote request")
    root = Path(fixtures_root)
    quote_client = rpc.Client(ONECLICK_URL, mode, kind="http", fixtures_dir=root / "intents")
    goplus_client = rpc.Client(GOPLUS_URL, mode, kind="http", fixtures_dir=root / "goplus")
    checks, quote, binding, chain_error = [], None, None, None
    try:
        chain, _ = parse_destination(destination_asset)
    except ValueError as error:
        chain, chain_error = None, str(error)
    try:
        response = dry_quote(origin_asset, destination_asset, amount, recipient, recipient_type,
                             client=quote_client, dry=True, deadline=deadline, refund_to=refund_to,
                             refund_type=refund_type, app_fees=app_fees)
        _encodable(response, "1Click dry quote answer")
        if quote_verifier is not None and not quote_verifier(response):
            raise rpc.MalformedResponse("the 1Click dry quote signature did not verify")
        check, quote, binding = _quote_check(response, asked, chain, dict(quote_client.last_source))
        checks.append(check)
    except UNAVAILABLE as error:
        checks.append(evidence.check("quote_dry", "unknown", f"could not get a dry quote: {_dry_quote_why(error)}",
                                     dict(quote_client.last_source)))
    quote_source = dict(quote_client.last_source)

    local = {"provider": "local rules", "request_sha256": None, "fetched_at": None}
    if chain is None:
        checks.append(evidence.check("destination_supported", "unknown", chain_error, local))
    elif recipient_type != "DESTINATION_CHAIN":
        checks.append(evidence.check("destination_supported", "unknown", f"recipientType {recipient_type} does "
                                     "not leave NEAR Intents for a destination chain", local))
    elif chain not in CHAIN_IDS:
        checks.append(evidence.check("destination_supported", "unknown", f"destination chain {chain} is not one of "
                                     f"the EVM chains this leg judges ({', '.join(CHAIN_IDS)})", local))
    else:
        checks.append(evidence.check("destination_supported", "pass", f"{chain}, chain id {CHAIN_IDS[chain]}",
                                     local))

    checks.append(_ofac_check(chain, recipient, chain_error))

    goplus = goplus_address(recipient, client=goplus_client)
    fetched = goplus["source"]["fetched_at"]
    if goplus["status"] != "ok":
        checks.append(evidence.check("recipient_theft_label", "unknown", f"GoPlus address_security could not run: "
                                     f"{goplus['reason']}", goplus["source"]))
    elif goplus["labels"]:
        checks.append(evidence.check("recipient_theft_label", "signal", f"GoPlus address_security labels "
                                     f"{', '.join(goplus['labels'])}; data_source {_quoted(goplus['data_source'])}; "
                                     f"fetched {fetched}", goplus["source"]))
    else:
        checks.append(evidence.check("recipient_theft_label", "pass", f"GoPlus address_security lists none of the "
                                     f"{len(THEFT_LABELS)} theft labels; data_source "
                                     f"{_quoted(goplus['data_source'])}; fetched {fetched}", goplus["source"]))

    checks.append(_code_check(chain, recipient, quote, root, mode, firewall_url))
    # The quote age and the deadline are judged after the last external read, at the clock the document
    # records, in their places after quote_dry.
    now = clock()
    checks[1:1] = [_quote_age_check(None if binding is None else binding["timestamp"], quote_source, lambda: now,
                                  clock_skew_ms=clock_skew_ms),
                   _deadline_check(deadline, lambda: now)]
    statuses = {check["status"] for check in checks}
    verdict = "DENY" if "signal" in statuses else "UNKNOWN" if "unknown" in statuses else "ALLOW"
    reasons = _reasons(verdict, checks, recipient, goplus)
    document = evidence.document(
        subject={"kind": "intents", "origin_asset": origin_asset, "destination_asset": destination_asset,
                 "amount": amount, "recipient": recipient, "recipient_type": recipient_type,
                 "quote_request": asked, "quote": binding},
        checks=checks, labels=[], verdict=verdict, reasons=reasons,
        observed={"block_height": None, "timestamp_ms": int(now * 1000)}, issued_ms=None, expires_ms=None,
        verdict_id=None, latency_ms=None)
    document["kind"] = "intents"
    return {"verdict": verdict, "checks": checks, "reasons": reasons, "quote": quote, "document": document,
            "evidence_hash": evidence.evidence_hash(document)}


def verify_binding(answer, quote_request, *, clock=time.time, clock_skew_ms=0):
    """Raise ValueError unless answer, a judge_quote result or the check_intents_quote answer, is intact
    and judged the request quote_request makes: its evidence hash recomputes from its document, its
    verdict is the document's, its subject records the judged quote_request (a document from before
    request binding has none and is refused whatever its verdict), its judged amount and refundType pass
    _request_shape, the BOUND_FIELDS and REFUND_FIELDS of quote_request (the quoteRequest of the quote
    being judged) are present and equal the judged ones, and it passes _request_binding with the
    deadline: every judged field but dry present, the deadline the same instant as the judged one,
    appFees equal to the named ones (none when none were named, absent meaning none), slippageTolerance
    no higher, any other judged field equal, and nothing _unjudged names, and then _request_schema. dry
    is not compared: a later real quote is not dry. An ALLOW is refused when _expired names a bound it
    fails at one reading of clock: its judged deadline not after it, or its quote older than
    MAX_QUOTE_AGE_MS, allowing the optional clock_skew_ms at each age boundary. An intact UNKNOWN or
    DENY answer that passes these checks is not refused, whatever
    the clock: its verdict says it is no pass. A supplied deposit address is checked separately by
    bind_deposit_address. The answer is unsigned, so this refuses an edited or reused answer, not one
    forged whole with its hash recomputed."""
    document = answer.get("document") if isinstance(answer, dict) else None
    try:
        intact = isinstance(document, dict) and evidence.evidence_hash(document) == answer.get("evidence_hash")
    except TypeError:
        intact = False
    if not intact:
        raise ValueError("the evidence hash does not match the answer's document")
    subject = document.get("subject")
    if document.get("kind") != "intents" or not isinstance(subject, dict) or subject.get("kind") != "intents":
        raise ValueError("the answer is not an intents verdict")
    if answer.get("verdict") != document.get("verdict"):
        raise ValueError(f"the answer's verdict {_quoted(answer.get('verdict'))} is not the document's "
                         f"{_quoted(document.get('verdict'))}")
    if not isinstance(quote_request, dict):
        raise ValueError("quote_request: not an object")
    judged_request = subject.get("quote_request")
    if not isinstance(judged_request, dict):
        raise ValueError("the evidence document predates request binding: its subject has no quote_request, so "
                         "the request it judged cannot be compared")
    _request_shape(judged_request.get("amount"), judged_request.get("refundType"))
    for field, key in BOUND_FIELDS:
        if not _same(field, subject.get(key), quote_request.get(field)):
            raise ValueError(f"{field} {_quoted(quote_request.get(field))} is not the judged "
                             f"{_quoted(subject.get(key))}")
        if not _same(field, subject.get(key), judged_request.get(field)):
            raise ValueError(f"the evidence quote request does not match the judged {field}")
    for field in REFUND_FIELDS:
        if not _same(field, judged_request.get(field), quote_request.get(field)):
            raise ValueError(f"{field} {_quoted(quote_request.get(field))} is not the judged "
                             f"{_quoted(judged_request.get(field))}")
    _request_binding(judged_request, quote_request, "quote_request", deadline=True)
    _request_schema(quote_request)
    if answer.get("verdict") == "ALLOW":
        expired = _expired(judged_request, subject.get("quote"), clock(), clock_skew_ms=clock_skew_ms)
        if expired:
            raise ValueError(f"{expired}: an ALLOW ends at its quote deadline and when its quote is older than "
                             f"{MAX_QUOTE_AGE_MS} ms")


def _expired(judged, quote, now, *, clock_skew_ms=0):
    """The detail of the first time bound an answer fails at now, one clock reading in seconds: the
    judged deadline not after now (_deadline_check), then the timestamp of quote, the document's
    {correlation_id, timestamp, ...}, missing, invalid or more than MAX_QUOTE_AGE_MS old at now
    (_quote_age_check). None when both hold."""
    timestamp = quote.get("timestamp") if isinstance(quote, dict) else None
    local = {"provider": "local rules", "request_sha256": None, "fetched_at": None}
    for check in (_deadline_check(judged.get("deadline"), lambda: now),
                  _quote_age_check(timestamp, local, lambda: now, clock_skew_ms=clock_skew_ms)):
        if check["status"] != "pass":
            return check["detail"]
    return None


def _request_binding(judged, request, name, *, deadline):
    """Check request, a later quote request, against the judged one and return the judged fields it
    compares exactly: every judged field but dry, deadline, slippageTolerance and appFees present and
    equal, the deadline the same instant as the judged one when deadline is true, appFees equal to
    the named ones (absent meaning none), slippageTolerance a number no higher than the judged one,
    and nothing _unjudged names, or this raises ValueError naming the field. A judged
    slippageTolerance that is not a number raises ValueError too."""
    ceiling = judged.get("slippageTolerance")
    if type(ceiling) not in (int, float):
        raise ValueError(f"the judged slippageTolerance {_quoted(ceiling)} is not a number")
    problems = _unjudged(judged, request)
    if problems:
        raise ValueError(f"the {name} names {'; '.join(problems)}")
    exact = [field for field in judged if field not in ("dry", "deadline", "slippageTolerance", "appFees")]
    for field in exact + (["deadline"] if deadline else []) + ["appFees"]:
        if not _same(field, judged.get(field, []) if field == "appFees" else judged.get(field),
                     request.get(field, []) if field == "appFees" else request.get(field)):
            raise ValueError(f"{field} {_quoted(request.get(field))} is not the judged {_quoted(judged.get(field))}")
    slippage = request.get("slippageTolerance")
    if type(slippage) not in (int, float) or not 0 <= slippage <= ceiling:
        raise ValueError(f"slippageTolerance {_quoted(slippage)} is not a number of basis points at most the "
                         f"judged {ceiling}")
    return exact


def _status_binding(judged, response, deposit_address, lifecycle, now):
    """(status, detail) of a 1Click status quoteResponse for deposit_address against the judged
    request at now, a clock reading in seconds taken after the status read: its quoteRequest must
    have dry false, since a deposit address comes only from a real quote, then pass _request_binding;
    a request _request_schema refuses, or a deadline not after now, gives unknown. So does the real
    quote's own time: a quote that is not an object, a quote deadline, from which the deposit address
    is inactive, that is not a complete date-time with an explicit zone or not after now, or a
    timestamp _quote_age_check does not pass at now. So does a real quote whose depositAddress is
    missing or not exactly deposit_address, and lifecycle, the status answer's status, unless it is a
    string in DEPOSIT_STATUSES."""
    status = response["quoteRequest"]
    if status.get("dry") is not False:
        raise ValueError(f"dry {_quoted(status.get('dry'))} is not false; a deposit address comes only from a real "
                         "quote")
    exact = _request_binding(judged, status, "status quote request", deadline=False)
    try:
        _request_schema(status)
    except ValueError as error:
        return "unknown", f"the status quote request is not one the quote request schema allows: {error}"
    slippage = status["slippageTolerance"]
    deadline, at = status["deadline"], datetime.fromtimestamp(now, UTC)
    if _instant(deadline) <= at:
        return "unknown", (f"the status quote request deadline {_quoted(deadline)} is not after "
                           f"{at.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    real = response.get("quote")
    if not isinstance(real, dict):
        return "unknown", "the status answer has no real quote object, quoteResponse.quote, with its deadline"
    real_deadline = real.get("deadline")
    if _instant(real_deadline) is None:
        return "unknown", (f"the real quote deadline {_quoted(real_deadline)} is not a complete date-time with an "
                           "explicit zone")
    if _instant(real_deadline) <= at:
        return "unknown", (f"the real quote deadline {_quoted(real_deadline)} is not after "
                           f"{at.strftime('%Y-%m-%dT%H:%M:%SZ')}; from it the deposit address is inactive")
    local = {"provider": "local rules", "request_sha256": None, "fetched_at": None}
    age = _quote_age_check(response.get("timestamp"), local, lambda: now)
    if age["status"] != "pass":
        return "unknown", f"the real quote timestamp {_quoted(response.get('timestamp'))}: {age['detail']}"
    if "depositAddress" not in real:
        return "unknown", ("the real quote has no depositAddress, so it is not bound to the supplied "
                           f"{_quoted(deposit_address)}")
    if real["depositAddress"] != deposit_address:
        return "unknown", (f"the real quote depositAddress {_quoted(real['depositAddress'])} is not the supplied "
                           f"{_quoted(deposit_address)}")
    if not isinstance(lifecycle, str):
        return "unknown", f"the status answer's status {_quoted(lifecycle)} is missing or not a string"
    if lifecycle not in DEPOSIT_STATUSES:
        accepted = (f"only {' or '.join(DEPOSIT_STATUSES)} is" if DEPOSIT_STATUSES else
                    "no recorded source lists the 1Click status values, so none is")
        return "unknown", (f"the status {_quoted(lifecycle)} is not one this leg accepts as waiting for a deposit: "
                           f"{accepted} accepted")
    named = ", ".join(f"{field} {_quoted(status[field])}" for field in exact)
    return "pass", (f"1Click status confirms this deposit address was created for the judged quote request: "
                    f"{named}; slippageTolerance {slippage} at most the judged {judged['slippageTolerance']}; "
                    f"appFees {_json(status.get('appFees', []))}; deadline {_quoted(deadline)} in the future; "
                    f"no {', '.join(STATUS_ABSENT)}; real quote deadline {_quoted(real_deadline)} in the future and "
                    f"{age['detail']}; real quote depositAddress {_quoted(deposit_address)}, as supplied; status "
                    f"{_quoted(lifecycle)} waiting for a deposit")


def bind_deposit_address(answer, quote_request, deposit_address, *, mode=None, fixtures_root=FIXTURES_ROOT,
                         clock=time.time):
    """Read the 1Click status for a supplied real quote address and bind it to this judged request,
    after verify_binding at clock: one reading of clock taken after the status read judges
    _status_binding and then _expired, so a status deadline, real quote deadline, real quote age,
    judged deadline or judged quote age bound that passes during the read makes
    deposit_address_status unknown."""
    verify_binding(answer, quote_request, clock=clock)
    if not isinstance(deposit_address, str) or not deposit_address:
        raise ValueError("depositAddress must be a non empty string")
    _encodable(deposit_address, "deposit address")
    document = answer["document"]
    judged_request = document["subject"]["quote_request"]
    root = Path(fixtures_root)
    client = rpc.Client(ONECLICK_URL, mode, kind="http", fixtures_dir=root / "intents")
    try:
        response = client.get("/v0/status", params={"depositAddress": deposit_address}, record_http_errors=(404,))
        _encodable(response, "1Click status answer")
    except rpc.HttpError as error:
        if error.status == 404:
            raise ValueError(f"depositAddress {_quoted(deposit_address)} is unknown to 1Click") from error
        check = evidence.check("deposit_address_status", "unknown",
                              f"1Click status lookup failed: {_why(error)}", dict(client.last_source))
    except UNAVAILABLE as error:
        check = evidence.check("deposit_address_status", "unknown",
                              f"1Click status lookup failed: {_why(error)}", dict(client.last_source))
    else:
        now = clock()
        quote_response = response.get("quoteResponse") if isinstance(response, dict) else None
        status_request = quote_response.get("quoteRequest") if isinstance(quote_response, dict) else None
        if not isinstance(status_request, dict):
            check = evidence.check("deposit_address_status", "unknown",
                                  "1Click status answer has no quoteResponse.quoteRequest",
                                  dict(client.last_source))
        else:
            outcome, detail = _status_binding(judged_request, quote_response, deposit_address, response.get("status"),
                                              now)
            expired = _expired(judged_request, document["subject"].get("quote"), now)
            if outcome == "pass" and expired:
                outcome, detail = "unknown", f"{expired}, at the clock read after the status lookup"
            check = evidence.check("deposit_address_status", outcome, detail, dict(client.last_source))
    answer["checks"] = document["checks"]
    answer["checks"].append(check)
    if check["status"] == "unknown":
        reason = f"deposit_address_status could not run: {check['detail']}."
        if answer["verdict"] == "ALLOW":
            answer["verdict"] = document["verdict"] = "UNKNOWN"
            answer["reasons"] = document["reasons"] = [reason]
        elif answer["verdict"] == "UNKNOWN":
            answer["reasons"].append(reason)
    answer["evidence_hash"] = evidence.evidence_hash(document)
    return check


def _code_check(chain, recipient, quote, root, mode, firewall_url):
    if chain not in CHAIN_IDS:
        return evidence.check("recipient_code", "unknown", "not run: the destination chain is not supported",
                              {"provider": "local rules", "request_sha256": None, "fetched_at": None})
    evm = rpc.Client(EVM_RPC_URLS[chain], mode, fixtures_dir=root / "evm")
    try:
        code = eth_get_code(CHAIN_IDS[chain], recipient, client=evm)
    except UNAVAILABLE as error:
        return evidence.check("recipient_code", "unknown", f"could not read eth_getCode: {_why(error)}",
                              dict(evm.last_source))
    if code == "0x":
        return evidence.check("recipient_code", "pass", f"no contract code at {recipient} on {chain}; the "
                              "firewall is not called", dict(evm.last_source))
    if not firewall_url:
        return evidence.check("recipient_code", "unknown", f"contract code at {recipient} on {chain}; "
                              "SHIELDBOT_API_URL is unset, so the firewall did not run", dict(evm.last_source))
    client = rpc.Client(firewall_url, mode, kind="http", fixtures_dir=root / "evm")
    value = quote.get("amountOut") if isinstance(quote, dict) else None
    try:
        if not value:
            raise ValueError("no amountOut from the quote to send as the value")
        answer = firewall(CHAIN_IDS[chain], recipient, value, client=client)
        _encodable(answer, "firewall answer")
        classification = answer.get("classification") if isinstance(answer, dict) else None
        if not isinstance(classification, str):
            raise rpc.MalformedResponse("the firewall answer has no classification")
    except UNAVAILABLE as error:
        return evidence.check("recipient_code", "unknown", f"contract code at {recipient} on {chain}; the "
                              f"firewall could not run: {_why(error)}", dict(client.last_source or evm.last_source))
    source = dict(client.last_source)
    detail = (f"contract code at {recipient} on {chain}; judged as a native value transfer of amountOut base "
              f"units by the ShieldBot firewall at {firewall_url} "
              f"(fetched {source['fetched_at']}) classification {_quoted(classification)}, evidence_hash "
              f"{_quoted(answer.get('evidence_hash'))}, evidence_url {_quoted(answer.get('evidence_url'))}")
    if classification in FIREWALL_SIGNALS:
        return evidence.check("recipient_code", "signal", detail, source)
    failed = answer.get("failed_sources")
    if answer.get("status") != FIREWALL_COMPLETE or answer.get("partial") is not False or not (
            isinstance(failed, list) and not failed):
        return evidence.check("recipient_code", "unknown", f"{detail}; the scan is incomplete: status "
                              f"{_quoted(answer.get('status'))}, partial {_quoted(answer.get('partial'))}, "
                              f"failed_sources {_quoted(answer.get('failed_sources'))}", source)
    try:
        known = json.loads(FIREWALL_CLASSIFICATIONS.read_text(encoding="utf-8"))["non_signals"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        return evidence.check("recipient_code", "unknown", f"{detail}; could not read the firewall classification "
                              f"list: {type(error).__name__}: {error}", source)
    if not isinstance(known, list) or classification not in known:
        return evidence.check("recipient_code", "unknown", f"{detail}; the classification is not one this leg "
                              "knows", source)
    return evidence.check("recipient_code", "pass", detail, source)


def _ofac_check(chain, recipient, chain_error):
    local = {"provider": "fixtures/sanctions/ofac-sdn-addresses-*.json", "request_sha256": None, "fetched_at": None}
    if chain is None:
        return evidence.check("ofac_sanctioned", "unknown", f"could not identify the destination chain: "
                              f"{chain_error}", local)
    try:
        document = sanctions.load()
        source_document = document["source"]
        fetched_at = source_document["fetched_at"]
        fixture_name = (f"{sanctions.FIXTURE_NAME}-"
                        f"{fetched_at.replace('-', '').replace(':', '')}.json")
        fixture_path = sanctions.FIXTURE_DIR / fixture_name
        source = {"provider": f"fixtures/sanctions/{fixture_name}",
                  "request_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
                  "fetched_at": fetched_at}
        list_counts = sanctions.counts(document)
        ofac_codes = [code for code in INTENTS_OFAC_ASSETS.get(chain, ()) if code in list_counts["assets"]]
        evm_address = bool(_EVM_ADDRESS.fullmatch(recipient))
        if not ofac_codes and not evm_address:
            return evidence.check("ofac_sanctioned", "pass", f"the OFAC list publishes no address for chain "
                                  f"{chain}; list published {document['publish_date']}", source)
        if evm_address:
            matches = sanctions.is_sanctioned(chain, recipient, document)
        else:
            matches = [match for code in ofac_codes
                       for match in sanctions.is_sanctioned(code, recipient, document)]
    except (sanctions.SanctionsError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return evidence.check("ofac_sanctioned", "unknown", f"could not read the local OFAC sanctions fixture: "
                              f"{type(error).__name__}: {error}", local)
    if matches:
        detail = "; ".join(f"SDN entry uid {match['uid']}, name {_quoted(match['name'])}, programs "
                            f"{', '.join(match['programs'])}, asset {match['asset']} address "
                            f"{_quoted(match['address'])}; list published {match['publish_date']}; "
                            f"source file SHA-256 {match['source_sha256']}" for match in matches)
        return evidence.check("ofac_sanctioned", "signal", detail, source)
    if evm_address:
        detail = (f"no address match using the normalized EVM address key across "
                  f"{list_counts['addresses']} distinct addresses; list published {document['publish_date']}")
    else:
        checked_addresses = sum(list_counts["assets"][code] for code in ofac_codes)
        detail = (f"no address match under OFAC asset code(s) {', '.join(ofac_codes)}; checked "
                  f"{checked_addresses} distinct code-address keys; list published {document['publish_date']}")
    return evidence.check("ofac_sanctioned", "pass", detail, source)


def _reasons(verdict, checks, recipient, goplus):
    by_id = {check["id"]: check for check in checks}
    when = f"GoPlus data_source {_quoted(goplus['data_source'])}, fetched {goplus['source']['fetched_at']}"
    if verdict == "ALLOW":
        return ["Every check ran and none found a signal.",
                f"GoPlus lists no theft label for {recipient} ({when})."]
    if verdict == "UNKNOWN":
        return [f"{c['id']} could not run: {c['detail']}." for c in checks if c["status"] == "unknown"]
    reasons = []
    if by_id["recipient_theft_label"]["status"] == "signal":
        reasons.append(f"The recipient {recipient} carries the GoPlus theft labels {', '.join(goplus['labels'])} "
                       f"({when}).")
    if by_id["recipient_code"]["status"] == "signal":
        reasons.append(f"ShieldBot's firewall refuses a transfer to {recipient}: {by_id['recipient_code']['detail']}.")
    if by_id["ofac_sanctioned"]["status"] == "signal":
        reasons.append(f"The OFAC SDN list names {recipient}: {by_id['ofac_sanctioned']['detail']}.")
    if by_id["quote_dry"]["status"] == "signal":
        reasons.append(f"{by_id['quote_dry']['detail']}.")
    return reasons


def live_deadline(clock=time.time):
    """A deadline ten minutes ahead, for a live dry quote."""
    return (datetime.fromtimestamp(clock(), UTC) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _deadline_check(deadline, clock):
    """quote_deadline: pass only on a judged deadline, a complete date-time with an explicit zone,
    after clock; from its deadline 1Click refunds a quote instead of swapping it."""
    local = {"provider": "local rules", "request_sha256": None, "fetched_at": None}
    at = datetime.fromtimestamp(clock(), UTC)
    now = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    if _instant(deadline) <= at:
        return evidence.check("quote_deadline", "unknown", f"the judged deadline {_quoted(deadline)} is not after "
                              f"{now}", local)
    return evidence.check("quote_deadline", "pass", f"the judged deadline {_quoted(deadline)} is after {now}", local)


MAX_QUOTE_AGE_MS = 300000
# The 1Click timestamp is already recorded in evidence; keep its age within the Gate's default expiry.
def _quote_age_check(timestamp, source, clock, *, clock_skew_ms=0):
    if type(clock_skew_ms) is not int or not 0 <= clock_skew_ms <= 5000:
        raise ValueError("clock skew must be from 0 to 5000 ms")
    if not isinstance(timestamp, str):
        return evidence.check("quote_age", "unknown", "quote age is unknown because timestamp is missing", source)
    quoted_at = _instant(timestamp)
    if quoted_at is None:
        return evidence.check("quote_age", "unknown", "quote age is unknown because timestamp is invalid", source)
    # The bounds compare the exact age; the detail rounds it away from the bound it fails.
    age = (datetime.fromtimestamp(clock(), UTC) - quoted_at) / timedelta(milliseconds=1)
    if age < -clock_skew_ms:
        return evidence.check("quote_age", "unknown", f"quote age is {math.floor(age)} ms because timestamp is in "
                              "the future", source)
    maximum = MAX_QUOTE_AGE_MS + clock_skew_ms
    if age > maximum:
        return evidence.check("quote_age", "unknown", f"quote age is {math.ceil(age)} ms, over {maximum} ms",
                              source)
    skew = f", allowing {clock_skew_ms} ms clock skew" if clock_skew_ms else ""
    return evidence.check("quote_age", "pass", f"quote age is {math.floor(age)} ms, within {maximum} ms{skew}",
                          source)


def _quote_dry_check(quote, request, chain, added, source):
    service = (f"the echo adds the 1Click service fee {_json(added[0])} to the named appFees" if added
               else "the echo adds no 1Click service fee")
    return evidence.check("quote_dry", "pass", f"dry quote answered: recipient {request['recipient']}, chain {chain}, "
                          f"amountOut {quote['amountOut']}; {service}", source)
