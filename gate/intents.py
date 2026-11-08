"""The EVM leg: where NEAR money goes when a 1Click quote sends it to an EVM chain.

judge_quote takes its own dry 1Click quote (the body always carries "dry": true, so no funds can
be deposited from the request), reads the recipient, the destination chain and amountOut from it,
asks GoPlus address_security about the recipient, reads the recipient's code with eth_getCode, and
calls ShieldBot's POST /api/firewall only when SHIELDBOT_API_URL is set and the recipient has code.
The verdict is ALLOW, DENY or UNKNOWN with an evidence document of kind intents, and it is never
signed: it is a read-only preflight whose consumers verify the judged request before returning it.

The firewall's answer alone never makes an ALLOW: a classification in FIREWALL_SIGNALS is a
signal, any other one is recorded as quoted third party data, and the verdict still needs the
GoPlus check to have run. Every request goes through gate.rpc.Client, so fixtures replay offline:
fixtures/intents (1Click), fixtures/goplus (GoPlus) and fixtures/evm (eth_getCode and the firewall).

The document records the request judged. A dry quote has no deposit address, so its verdict binds
to the five BOUND_FIELDS in that request. When a caller supplies the address from a real quote,
bind_deposit_address reads its 1Click status and compares the quote request that created it.
An old timestamp reads UNKNOWN.
The quote's signature is not checked: no 1Click verifying key is recorded here.
"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gate import evidence, rpc
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
# recorded as third party data and are never a reason for ALLOW.
FIREWALL_SIGNALS = ("HIGH_RISK", "BLOCK_RECOMMENDED")
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
# The deadline of the recorded quotes, so the replayed request body matches the recorded one.
RECORDED_DEADLINE = "2026-10-30T00:00:00Z"
# A check that meets one of these could not run.
UNAVAILABLE = (rpc.FixtureMiss, rpc.HttpError, rpc.RateLimited, rpc.RpcError, rpc.TransportError, OSError,
               ValueError)
# The request fields a quote answer must echo as sent and verify_binding compares with the later
# request, with their subject keys. The deadline is left out: 1Click echoes it with milliseconds.
BOUND_FIELDS = (("originAsset", "origin_asset"), ("destinationAsset", "destination_asset"), ("amount", "amount"),
                ("recipient", "recipient"), ("recipientType", "recipient_type"))
_OMFT = ".omft.near"
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def _why(error):
    if isinstance(error, rpc.FixtureMiss):
        return "no recorded response for the request"
    if isinstance(error, rpc.HttpError):
        return f"HTTP {error.status}"
    return f"{type(error).__name__}: {error}"


def _quoted(text):
    """A third party string as quoted data, never as ShieldBot's own words."""
    return json.dumps(text, ensure_ascii=False)


def parse_destination(asset_id):
    """(chain code, contract address) of a 1Click asset id: nep141:<chain>-0x<address>.omft.near
    gives the chain and the address, nep141:<chain>.omft.near the chain's native asset (address
    None), and any other nep141:<account> the token account on NEAR."""
    if not isinstance(asset_id, str) or not asset_id.startswith("nep141:") or len(asset_id) == len("nep141:"):
        raise ValueError(f"not a nep141 asset id: {asset_id!r}")
    account = asset_id[len("nep141:"):]
    if not account.endswith(_OMFT):
        return "near", account
    bridged = account[: -len(_OMFT)]
    chain, separator, address = bridged.partition("-")
    if not separator:
        return chain, None
    if not (address.startswith("0x") and len(address) == 42 and all(c in "0123456789abcdefABCDEF" for c in address[2:])):
        raise ValueError(f"not an EVM contract address in {asset_id!r}")
    return chain, address


def quote_body(origin_asset, destination_asset, amount, recipient, recipient_type, deadline):
    """The 1Click quote request body; dry is always true."""
    return {"dry": True, "swapType": SWAP_TYPE, "slippageTolerance": SLIPPAGE_BPS, "originAsset": origin_asset,
            "depositType": DEPOSIT_TYPE, "destinationAsset": destination_asset, "amount": amount,
            "refundTo": REFUND_TO, "refundType": REFUND_TYPE, "recipient": recipient,
            "recipientType": recipient_type, "deadline": deadline}


def dry_quote(origin_asset, destination_asset, amount, recipient, recipient_type="DESTINATION_CHAIN", *,
              client, dry=True, deadline=RECORDED_DEADLINE):
    """The JSON of POST /v0/quote for a dry quote. Anything but dry=True raises ValueError before a
    request is built."""
    if dry is not True:
        raise ValueError("the EVM leg takes dry quotes only; dry must be True")
    body = quote_body(origin_asset, destination_asset, amount, recipient, recipient_type, deadline)
    if body["dry"] is not True:
        raise ValueError("the quote body must carry dry true")
    return client.post("/v0/quote", body)


def goplus_address(address, *, client):
    """{status, labels, data_source, record, source, reason}: status ok only when GoPlus answers
    code 1 with a non empty result; labels are the THEFT_LABELS whose value is "1"."""
    try:
        body = client.get(f"/api/v1/address_security/{address}")
    except UNAVAILABLE as error:
        return {"status": "unknown", "labels": [], "data_source": None, "record": None,
                "source": dict(client.last_source), "reason": _why(error)}
    result = body.get("result") if isinstance(body, dict) else None
    if not (isinstance(body, dict) and body.get("code") == 1 and isinstance(result, dict) and result):
        code = body.get("code") if isinstance(body, dict) else None
        return {"status": "unknown", "labels": [], "data_source": None, "record": result,
                "source": dict(client.last_source), "reason": f"GoPlus answered code {code!r} without a record"}
    return {"status": "ok", "labels": [label for label in THEFT_LABELS if result.get(label) == "1"],
            "data_source": result.get("data_source"), "record": result, "source": dict(client.last_source),
            "reason": None}


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


def _same(field, judged, named):
    if field == "recipient":
        return isinstance(judged, str) and isinstance(named, str) and named.lower() == judged.lower()
    return named is not None and named == judged


def _quote_check(response, asked, chain, source):
    quote = response.get("quote") if isinstance(response, dict) else None
    request = response.get("quoteRequest") if isinstance(response, dict) else None
    if not isinstance(quote, dict) or not isinstance(request, dict) or not quote.get("amountOut"):
        raise rpc.MalformedResponse("the quote answer has no quote, quoteRequest or amountOut")
    if request.get("dry") is not True:
        raise rpc.MalformedResponse("the quote answer does not echo dry true")
    binding = {"correlation_id": response.get("correlationId"), "timestamp": response.get("timestamp"),
               "amount_out": quote["amountOut"], "min_amount_out": quote.get("minAmountOut")}
    differ = [field for field, _ in BOUND_FIELDS if not _same(field, asked[field], request.get(field))]
    if differ:
        named = "; ".join(f"{field} {_quoted(request.get(field))}, not {_quoted(asked[field])}" for field in differ)
        return evidence.check("quote_dry", "signal", f"the dry quote names {named}", source), quote, binding
    return _quote_dry_check(quote, request, chain, source), quote, binding


def judge_quote(origin_asset, destination_asset, amount, recipient, recipient_type="DESTINATION_CHAIN", *,
                mode=None, fixtures_root=FIXTURES_ROOT, firewall_url=None, clock=time.time,
                deadline=RECORDED_DEADLINE):
    """The EVM leg verdict for one quote: {verdict, checks, reasons, quote, document, evidence_hash}.
    Unsigned on every outcome. firewall_url is SHIELDBOT_API_URL or None. A recipient that is not
    0x and 40 hex characters raises ValueError before any request."""
    if not isinstance(recipient, str) or not _EVM_ADDRESS.fullmatch(recipient):
        raise ValueError(f"recipient: not 0x and 40 hex characters: {recipient!r}")
    root = Path(fixtures_root)
    quote_client = rpc.Client(ONECLICK_URL, mode, kind="http", fixtures_dir=root / "intents")
    goplus_client = rpc.Client(GOPLUS_URL, mode, kind="http", fixtures_dir=root / "goplus")
    checks, quote, binding, chain_error = [], None, None, None
    try:
        chain, _ = parse_destination(destination_asset)
    except ValueError as error:
        chain, chain_error = None, str(error)
    asked = quote_body(origin_asset, destination_asset, amount, recipient, recipient_type, deadline)
    try:
        response = dry_quote(origin_asset, destination_asset, amount, recipient, recipient_type,
                             client=quote_client, dry=True, deadline=deadline)
        check, quote, binding = _quote_check(response, asked, chain, dict(quote_client.last_source))
        checks.extend((check, _quote_age_check(binding.get("timestamp"), dict(quote_client.last_source), clock)))
    except UNAVAILABLE as error:
        checks.extend((evidence.check("quote_dry", "unknown", f"could not get a dry quote: {_why(error)}",
                                        dict(quote_client.last_source)),
                       _quote_age_check(None, dict(quote_client.last_source), clock)))

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
    statuses = {check["status"] for check in checks}
    verdict = "DENY" if "signal" in statuses else "UNKNOWN" if "unknown" in statuses else "ALLOW"
    reasons = _reasons(verdict, checks, recipient, goplus)
    document = evidence.document(
        subject={"kind": "intents", "origin_asset": origin_asset, "destination_asset": destination_asset,
                 "amount": amount, "recipient": recipient, "recipient_type": recipient_type,
                 "quote_request": asked, "quote": binding},
        checks=checks, labels=[], verdict=verdict, reasons=reasons,
        observed={"block_height": None, "timestamp_ms": int(clock() * 1000)}, issued_ms=None, expires_ms=None,
        verdict_id=None, latency_ms=None)
    document["kind"] = "intents"
    return {"verdict": verdict, "checks": checks, "reasons": reasons, "quote": quote, "document": document,
            "evidence_hash": evidence.evidence_hash(document)}


def verify_binding(answer, quote_request):
    """Raise ValueError unless answer, a judge_quote result or the check_intents_quote answer, is
    intact and judged the request quote_request makes: its evidence hash recomputes from its
    document, its verdict is the document's, and the BOUND_FIELDS of quote_request (the quoteRequest
    of the quote being judged) equal the judged ones. A supplied deposit address is checked
    separately by bind_deposit_address. The answer is unsigned, so this refuses an edited or reused
    answer, not one forged whole with its hash recomputed."""
    document = answer.get("document") if isinstance(answer, dict) else None
    if not isinstance(document, dict) or evidence.evidence_hash(document) != answer.get("evidence_hash"):
        raise ValueError("the evidence hash does not match the answer's document")
    subject = document.get("subject")
    if document.get("kind") != "intents" or not isinstance(subject, dict) or subject.get("kind") != "intents":
        raise ValueError("the answer is not an intents verdict")
    if answer.get("verdict") != document.get("verdict"):
        raise ValueError(f"the answer's verdict {_quoted(answer.get('verdict'))} is not the document's "
                         f"{_quoted(document.get('verdict'))}")
    unknown_status = next((check for check in document.get("checks", [])
                           if check.get("id") == "deposit_address_status" and check.get("status") == "unknown"), None)
    if unknown_status:
        raise ValueError(f"UNKNOWN deposit address status is refused: {unknown_status['detail']}")
    if answer.get("verdict") == "UNKNOWN":
        raise ValueError("UNKNOWN verdict is refused: " + "; ".join(document["reasons"]))
    if not isinstance(quote_request, dict):
        raise ValueError("quote_request: not an object")
    judged_request = subject.get("quote_request")
    if not isinstance(judged_request, dict) and answer.get("verdict") == "ALLOW":
        raise ValueError("the evidence document has no judged quote request")
    for field, key in BOUND_FIELDS:
        if not _same(field, subject.get(key), quote_request.get(field)):
            raise ValueError(f"{field} {_quoted(quote_request.get(field))} is not the judged "
                             f"{_quoted(subject.get(key))}")
        if isinstance(judged_request, dict) and not _same(field, subject.get(key), judged_request.get(field)):
            raise ValueError(f"the evidence quote request does not match the judged {field}")


def bind_deposit_address(answer, quote_request, deposit_address, *, mode=None, fixtures_root=FIXTURES_ROOT):
    """Read the 1Click status for a supplied real quote address and bind it to this judged request."""
    verify_binding(answer, quote_request)
    if not isinstance(deposit_address, str) or not deposit_address:
        raise ValueError("depositAddress must be a non empty string")
    document = answer["document"]
    subject = document["subject"]
    judged_request = subject.get("quote_request")
    if not isinstance(judged_request, dict):
        raise ValueError("the evidence document has no judged quote request")
    root = Path(fixtures_root)
    client = rpc.Client(ONECLICK_URL, mode, kind="http", fixtures_dir=root / "intents")
    try:
        response = client.get("/v0/status", params={"depositAddress": deposit_address}, record_http_errors=(404,))
    except rpc.HttpError as error:
        if error.status == 404:
            raise ValueError(f"depositAddress {_quoted(deposit_address)} is unknown to 1Click") from error
        check = evidence.check("deposit_address_status", "unknown",
                              f"1Click status lookup failed: {_why(error)}", dict(client.last_source))
    except UNAVAILABLE as error:
        check = evidence.check("deposit_address_status", "unknown",
                              f"1Click status lookup failed: {_why(error)}", dict(client.last_source))
    else:
        quote_response = response.get("quoteResponse") if isinstance(response, dict) else None
        status_request = quote_response.get("quoteRequest") if isinstance(quote_response, dict) else None
        if not isinstance(status_request, dict):
            check = evidence.check("deposit_address_status", "unknown",
                                  "1Click status answer has no quoteResponse.quoteRequest",
                                  dict(client.last_source))
        else:
            for field, _ in BOUND_FIELDS:
                if not _same(field, judged_request.get(field), status_request.get(field)):
                    raise ValueError(f"{field} {_quoted(status_request.get(field))} is not the judged "
                                     f"{_quoted(judged_request.get(field))}")
            check = evidence.check("deposit_address_status", "pass",
                                  "1Click status confirms this deposit address was created for the judged quote request",
                                  dict(client.last_source))
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
        classification = answer.get("classification") if isinstance(answer, dict) else None
        if not isinstance(classification, str):
            raise rpc.MalformedResponse("the firewall answer has no classification")
    except UNAVAILABLE as error:
        return evidence.check("recipient_code", "unknown", f"contract code at {recipient} on {chain}; the "
                              f"firewall could not run: {_why(error)}", dict(client.last_source))
    source = dict(client.last_source)
    detail = (f"contract code at {recipient} on {chain}; judged as a native value transfer of amountOut base "
              f"units by the ShieldBot firewall at {firewall_url} "
              f"(fetched {source['fetched_at']}) classification {_quoted(classification)}, evidence_hash "
              f"{_quoted(answer.get('evidence_hash'))}, evidence_url {_quoted(answer.get('evidence_url'))}")
    return evidence.check("recipient_code", "signal" if classification in FIREWALL_SIGNALS else "pass", detail,
                          source)


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


MAX_QUOTE_AGE_MS = 300000
# The 1Click timestamp is already recorded in evidence; keep its age within the Gate's default expiry.
def _quote_age_check(timestamp, source, clock):
    if not isinstance(timestamp, str):
        return evidence.check("quote_age", "unknown", "quote age is unknown because timestamp is missing", source)
    try:
        quoted_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return evidence.check("quote_age", "unknown", "quote age is unknown because timestamp is invalid", source)
    if quoted_at.tzinfo is None:
        quoted_at = quoted_at.replace(tzinfo=UTC)
    age_ms = int((datetime.fromtimestamp(clock(), UTC) - quoted_at.astimezone(UTC)).total_seconds() * 1000)
    if age_ms < 0:
        return evidence.check("quote_age", "unknown", f"quote age is {age_ms} ms because timestamp is in the future", source)
    if age_ms > MAX_QUOTE_AGE_MS:
        return evidence.check("quote_age", "unknown", f"quote age is {age_ms} ms, over {MAX_QUOTE_AGE_MS} ms", source)
    return evidence.check("quote_age", "pass", f"quote age is {age_ms} ms, within {MAX_QUOTE_AGE_MS} ms", source)


def _quote_dry_check(quote, request, chain, source):
    return evidence.check("quote_dry", "pass", f"dry quote answered: recipient {request['recipient']}, chain {chain}, "
                          f"amountOut {quote['amountOut']}", source)
