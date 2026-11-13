"""Unsigned mainnet token checks with the evidence behind every outcome."""

import json
import time
import unicodedata

from gate import base58, canon, evidence, rules
from gate.rpc import EMPTY_CODE_HASH, MAINNET_RPC_URL, MalformedResponse
from intel import feed

# The byte length of a public key's base58 body in the NEAR "<curve>:<base58>" form, per curve.
KEY_BYTES = {"ed25519": 32, "secp256k1": 64}


def _mainnet(client):
    """True only for a JSON-RPC client of the repository's own mainnet endpoint; no environment variable
    can name another one."""
    return client.kind == "rpc" and client.url == MAINNET_RPC_URL


class _OneMetadata:
    """The rpc client with one ft_metadata read of the checked token, so ft_metadata_sane and
    symbol_impersonation judge the same response, or the same failure, and the source of that read."""

    def __init__(self, client, token):
        self._client, self._token, self._read = client, token, None

    def __getattr__(self, name):
        return getattr(self._client, name)

    def call_function(self, contract, method, args, **kwargs):
        if (contract, method, args, kwargs) != (self._token, "ft_metadata", {}, {}):
            return self._client.call_function(contract, method, args, **kwargs)
        if self._read is None:
            try:
                self._read = (self._client.call_function(contract, method, args), None, rules._source(self._client))
            except rules.UNAVAILABLE as error:
                self._read = (None, error, rules._source(self._client))
        value, error, source = self._read
        self._client.last_source = dict(source)
        if error is not None:
            raise error
        return value


def _normal(text):
    """The text after NFKC, case folding and collapsed whitespace; None when it is empty or still holds a
    non-ASCII character (a zero-width or other format character among them) or a control character."""
    folded = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return folded if folded and folded.isascii() and folded.isprintable() else None


def _strings(meta):
    """(symbol, name) of ft_metadata; MalformedResponse when either is not a non empty string."""
    if not (isinstance(meta, dict) and all(isinstance(meta.get(k), str) and meta[k] for k in ("symbol", "name"))):
        raise MalformedResponse("the token metadata has no name or symbol")
    return meta["symbol"], meta["name"]


def _recorded_symbol(assertion):
    """The symbol a catalog_token label records, or None for a label that records none."""
    if not assertion.label.startswith("symbol "):
        return None
    try:
        symbol = json.loads(assertion.label[len("symbol "):])
    except ValueError:
        return None
    return symbol if isinstance(symbol, str) else None


def _symbol_impersonation(token, store, rpc, metadata_check, own, catalog, issuers):
    """(the check, the catalog assertion ids it matched). The token's symbol and name, normalised, are
    compared with every symbol a catalog row records for another contract and with the symbol and name of
    every other issuer token, the only references read. A token whose current symbol equals its own
    recorded catalog symbol does not have that symbol compared with a reference symbol of the same value,
    since bridged copies of one asset share it. A name equal to that exempt symbol, matched against a
    reference symbol of the same value, is a gap: the label store records no names to tell a copy from the
    same asset. Every other name match signals.
    A reference that cannot be normalised is a gap too.
    Without a match only a token with its own catalog row passes; any other reads unknown."""
    store_source = rules._local("label store", store.sha256)
    if metadata_check["status"] != "pass":
        return evidence.check("symbol_impersonation", "unknown", "could not compare unreadable or malformed "
                              "token metadata", metadata_check["source"]), []
    try:
        symbol, name = _strings(rpc.call_function(token, "ft_metadata", {}))
    except rules.UNAVAILABLE as error:
        return evidence.check("symbol_impersonation", "unknown", f"could not read token metadata: "
                              f"{rules._why(error)}", rules._source(rpc)), []
    metadata_source = rules._source(rpc)
    mine = {"symbol": _normal(symbol), "name": _normal(name)}
    own_symbols = {_normal(s) for s in map(_recorded_symbol, own) if s is not None} - {None}
    exempt = mine["symbol"] if mine["symbol"] in own_symbols else None
    held = []

    def matches(reference, field, holder):
        """The token's fields that equal this reference field. The current symbol, when an own catalog row
        records it, is exempt against a reference symbol of that value; a name equal to it there is held."""
        value = _normal(reference)
        if value is None or field == "symbol" and value == exempt:
            if value is not None and mine["name"] == value:
                held.append(holder)
            return []
        return [f"{key} {rules._quoted(text)}" for key, text in (("symbol", symbol), ("name", name))
                if mine[key] == value]

    recorded, unrecorded, skipped, gaps, gap_source = 0, set(), 0, [], None
    for assertion in catalog:
        if assertion.subject == token:
            continue
        recorded_symbol = _recorded_symbol(assertion)
        if recorded_symbol is None:
            unrecorded.add(assertion.subject)
            continue
        if _normal(recorded_symbol) is None:
            gaps.append(f"the catalog symbol {rules._quoted(recorded_symbol)} of {assertion.subject} cannot be "
                        "normalised")
            gap_source = gap_source or store_source
            continue
        recorded += 1
        skipped += _normal(recorded_symbol) == exempt
        found = matches(recorded_symbol, "symbol", assertion.subject)
        if found:
            return evidence.check("symbol_impersonation", "signal", f"{' and '.join(found)} match the catalog "
                                  f"symbol {rules._quoted(recorded_symbol)} of {assertion.subject} (assertion "
                                  f"{assertion.id})", store_source), [assertion.id]
    unrecorded -= {a.subject for a in catalog if _recorded_symbol(a) is not None}

    compared = 0
    for entry in issuers:
        reference = entry["token"]
        if reference == token:
            continue
        try:
            other_symbol, other_name = _strings(rpc.call_function(reference, "ft_metadata", {}))
        except rules.UNAVAILABLE as error:
            gaps.append(f"could not read the metadata of the issuer token {reference}: {rules._why(error)}")
            gap_source = gap_source or rules._source(rpc)
            continue
        compared += 1
        for key, other in (("symbol", other_symbol), ("name", other_name)):
            if _normal(other) is None:
                gaps.append(f"the {key} {rules._quoted(other)} of the issuer token {reference} cannot be normalised")
                gap_source = gap_source or rules._source(rpc)
        found = [f"{part} matches the issuer token {reference} {key} {rules._quoted(other)}"
                 for key, other in (("symbol", other_symbol), ("name", other_name))
                 for part in matches(other, key, reference)]
        if found:
            return evidence.check("symbol_impersonation", "signal", "; ".join(found), rules._source(rpc)), []

    if held:
        gaps.append(f"the name {rules._quoted(name)} equals its own catalog symbol, which "
                    f"{', '.join(dict.fromkeys(held))} also hold; the label store records no names to tell a copy "
                    "from the same asset")
        gap_source = gap_source or store_source
    unreadable = [f"{key} {rules._quoted(text)}" for key, text in (("symbol", symbol), ("name", name))
                  if mine[key] is None]
    if unreadable:
        return evidence.check("symbol_impersonation", "unknown", f"cannot compare the {' and '.join(unreadable)}: "
                              "empty, or a non-ASCII, zero-width, format or control character remains after "
                              "normalisation", metadata_source), []
    if gaps:
        return evidence.check("symbol_impersonation", "unknown", "could not compare every reference: "
                              + "; ".join(gaps), gap_source), []
    compared_text = (f"symbol {rules._quoted(symbol)} and name {rules._quoted(name)} match none of {recorded} "
                     f"recorded catalog symbols of other contracts and the symbol and name of {compared} issuer "
                     "tokens after normalisation")
    if not own:
        return evidence.check("symbol_impersonation", "unknown", f"{compared_text}, but {token} has no catalog row "
                              f"and {len(unrecorded)} catalog tokens record no symbol to compare",
                              store_source), []
    return evidence.check("symbol_impersonation", "pass", compared_text + (
        f"; its symbol is not compared with {skipped} catalog rows of other contracts recording its own "
        "catalog symbol, its name is" if skipped else ""),
        store_source), []


def _account_state(account):
    """(code hash, block height) of view_account; MalformedResponse unless the code hash is base58 of
    exactly 32 bytes and the height a non negative integer."""
    code_hash, height = account["code_hash"], account.get("block_height")
    try:
        raw = base58.decode(code_hash)
    except ValueError as error:
        raise MalformedResponse(f"the code hash {rules._quoted(code_hash)} is not base58") from error
    if len(raw) != 32:
        raise MalformedResponse(f"the code hash {rules._quoted(code_hash)} is not 32 bytes")
    if type(height) is not int or height < 0:
        raise MalformedResponse(f"the block height {height!r} is not a non negative integer")
    return code_hash, height


def _full_access(key):
    """True for a full access key, False for a function call key; MalformedResponse for any partial record."""
    access = key.get("access_key") if isinstance(key, dict) else None
    public_key = key.get("public_key") if isinstance(key, dict) else None
    if not isinstance(public_key, str) or not isinstance(access, dict):
        raise MalformedResponse("an access key record lacks its public key or access key")
    curve, _, body = public_key.partition(":")
    try:
        if len(base58.decode(body)) != KEY_BYTES.get(curve):
            raise ValueError("wrong length")
    except ValueError as error:
        raise MalformedResponse(f"the public key {rules._quoted(public_key)} is not a NEAR key") from error
    nonce, permission = access.get("nonce"), access.get("permission")
    if type(nonce) is not int or not 0 <= nonce <= canon.U64_MAX:
        raise MalformedResponse(f"the access key {public_key} has no u64 nonce")
    if permission == "FullAccess":
        return True
    call = (permission.get("FunctionCall") if isinstance(permission, dict) and set(permission) == {"FunctionCall"}
            else None)
    allowance = call.get("allowance") if isinstance(call, dict) else None
    try:
        if not (isinstance(call, dict) and {"allowance", "receiver_id", "method_names"} <= set(call)
                and isinstance(call["receiver_id"], str)
                and (allowance is None or isinstance(allowance, str) and allowance.isascii() and allowance.isdigit()
                     and int(allowance) <= canon.U128_MAX)
                and isinstance(call["method_names"], list)
                and all(isinstance(method, str) and method for method in call["method_names"])):
            raise ValueError("partial")
        canon.account_id(call["receiver_id"], "receiver_id")
    except ValueError as error:
        raise MalformedResponse(f"the access key {public_key} has an unreadable permission") from error
    return False


def check_token(token_id, *, store, rpc, now_ms):
    """Check a mainnet token without signing; unavailable reads remain unknown."""
    canon.account_id(token_id, "token_id")
    if token_id.endswith(".testnet") or not _mainnet(rpc):
        raise ValueError("token checks require the mainnet JSON-RPC endpoint")
    started = time.perf_counter()
    rpc = _OneMetadata(rpc, token_id)
    own = rules._current(store, token_id)
    catalog = [a for a in store.assertions if a.chain == "near" and a.category == "catalog_token"
               and a.superseded_by is None]
    own_catalog = [a for a in own if a.category == "catalog_token"]
    checks = [evidence.check("token_in_catalog", "pass" if own_catalog else "unknown",
                            f"{token_id} is a catalog_token in the label store" if own_catalog
                            else f"{token_id} has no current catalog_token in the label store",
                            rules._local("label store", store.sha256))]
    metadata_check = rules._ft_metadata_check(rpc, token_id)
    checks.append(metadata_check)
    issuers = rules.issuers_for("mainnet")
    impersonation, matched = _symbol_impersonation(token_id, store, rpc, metadata_check, own_catalog, catalog,
                                                   issuers)
    checks.append(impersonation)
    for entry in issuers:
        if entry["token"] == token_id:
            checks.append(evidence.check("issuer_known", "pass", "issuer entry " + json.dumps(entry, sort_keys=True),
                                         rules._local("issuer table", evidence.evidence_hash(issuers))))

    block_height = None
    try:
        account = rpc.view_account(token_id)
        account_source = rules._source(rpc)
        code_hash, block_height = _account_state(account)
    except rules.UNAVAILABLE as error:
        detail = f"could not read contract code on {token_id}: {rules._why(error)}"
        checks += [evidence.check(check_id, "unknown", detail, rules._source(rpc))
                   for check_id in ("contract_control", "incident_code")]
    else:
        if code_hash != EMPTY_CODE_HASH:
            try:
                listing = rpc.call("query", {"request_type": "view_access_key_list", "block_id": block_height,
                                             "account_id": token_id})
                keys = listing.get("keys") if isinstance(listing, dict) else None
                height = listing.get("block_height") if isinstance(listing, dict) else None
                if not isinstance(keys, list):
                    raise MalformedResponse("the view_access_key_list result has no keys list")
                if type(height) is not int or height != block_height:
                    raise MalformedResponse(f"the access key list is at block {height!r}, not at block "
                                            f"{block_height} of the account read")
                full_access = sum(_full_access(key) for key in keys)
                status = "unknown" if full_access else "pass"
                detail = (f"{full_access} full access key(s) can replace the contract code" if full_access
                          else f"none of {len(keys)} access keys is a full access key that can replace the "
                               "contract code")
            except rules.UNAVAILABLE as error:
                status, detail = "unknown", f"could not read contract access keys: {rules._why(error)}"
            checks.append(evidence.check("contract_control", status, detail, rules._source(rpc)))
            try:
                code_hashes = feed.Context(store, feed.INCIDENTS_DIR).code_hashes
                if not code_hashes:
                    raise MalformedResponse("no incident page reports a code hash")
            except (OSError, ValueError, KeyError, TypeError) as error:
                checks.append(evidence.check("incident_code", "unknown", "could not read the incident code "
                                             f"hashes: {rules._why(error)}", rules._local("incident pages")))
            else:
                incident = code_hashes.get(code_hash)
                detail = (f"code hash {code_hash} matches incident {incident}" if incident
                          else f"code hash {code_hash} matches none of {len(code_hashes)} reported incident "
                               "code hashes")
                checks.append(evidence.check("incident_code", "signal" if incident else "pass", detail,
                                             account_source))
        else:
            checks.append(evidence.check("incident_code", "pass", "no contract code", account_source))

    statuses = {check["status"] for check in checks}
    verdict = "DENY" if "signal" in statuses else "UNKNOWN" if "unknown" in statuses else "ALLOW"
    reasons = [check["detail"] for check in checks if check["status"] == (
        "signal" if verdict == "DENY" else "unknown" if verdict == "UNKNOWN" else "pass")]
    observed = {"block_height": block_height, "timestamp_ms": now_ms}
    latency_ms = int((time.perf_counter() - started) * 1000)
    consulted = list(dict.fromkeys([a.id for a in own] + matched))
    document = evidence.document(subject={"network": "mainnet", "kind": "token", "token": token_id},
                                 checks=checks, labels=consulted, verdict=verdict, reasons=reasons,
                                 observed=observed, issued_ms=None, expires_ms=None, verdict_id=None,
                                 latency_ms=latency_ms)
    outcome, action = {"ALLOW": ("ALLOW", "sign"), "DENY": ("DENY", "refuse"),
                       "UNKNOWN": ("ASK", "ask")}[verdict]
    return {"outcome": outcome, "action": action, "reasons": reasons,
            "checks": [{"name": check["id"], "status": check["status"], "detail": check["detail"]}
                       for check in checks],
            "evidence": evidence.canonical_bytes(document).decode("utf-8"),
            "evidence_hash": evidence.evidence_hash(document), "freshness": observed, "verdict": None,
            "signing": "never", "latency_ms": latency_ms}
