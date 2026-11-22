"""Unsigned mainnet account checks over the receiver rules and recorded evidence."""

import json
import time
from pathlib import Path

from gate import base58, canon, evidence, rules
from gate.rpc import MalformedResponse, request_deadline
from intel import feed

# The rules.judge checks an account check keeps; memo_injection has no memo to read. label_stale and
# code_changed_recently come only from rules version 2.
KEPT_CHECKS = ("receiver_exists", "label_denylist", "label_stale", "issuer_blacklist", "account_age", "dust_funded",
               "lookalike", "code_without_metadata", "code_changed_recently")
CODE_HASH_BYTES = 32
# The key types a NEAR access key list names, with their public key length in bytes.
KEY_BYTES = {"ed25519": 32, "secp256k1": 64}
# An access key nonce is a u64 and a FunctionCall allowance a u128 in the NEAR protocol.
NONCE_MAX = 2**64 - 1
ALLOWANCE_MAX = 2**128 - 1
# Pages of the paged access key list read for a contract whose whole list RPC refuses as
# TOO_MANY_ACCESS_KEYS, and the seconds those reads may take together, well inside the live route's
# 25 s deadline (intel/live.py); a read past either bound leaves the check unknown.
KEY_PAGES_MAX = 5
KEY_PAGES_S = 10


class _OneRead:
    """The rpc client with one view_account read of the checked account, so judge and the account check
    see one snapshot: the same code hash at the same block, or the same failure, and the source of that read."""

    def __init__(self, client, account_id):
        self._client, self._account_id, self._read = client, account_id, None

    def __getattr__(self, name):
        return getattr(self._client, name)

    def view_account(self, account_id):
        if account_id != self._account_id:
            return self._client.view_account(account_id)
        if self._read is None:
            try:
                self._read = (self._client.view_account(account_id), None, rules._source(self._client))
            except rules.UNAVAILABLE as error:
                self._read = (None, error, rules._source(self._client))
        account, error, source = self._read
        self._client.last_source = dict(source)
        if error is not None:
            raise error
        return account


def _decodes_to(text, length):
    """True when text is a string of base58 for exactly length bytes."""
    if not isinstance(text, str):
        return False
    try:
        return len(base58.decode(text)) == length
    except ValueError:
        return False


def _valid_public_key(text):
    kind, _, body = text.partition(":") if isinstance(text, str) else ("", "", "")
    return kind in KEY_BYTES and _decodes_to(body, KEY_BYTES[kind])


def _full_access_count(keys):
    """The number of full access keys; MalformedResponse unless every entry is complete."""
    count = 0
    for key in keys:
        access = key.get("access_key") if isinstance(key, dict) else None
        if not (isinstance(key, dict) and _valid_public_key(key.get("public_key"))
                and isinstance(access, dict) and type(access.get("nonce")) is int and 0 <= access["nonce"] <= NONCE_MAX):
            raise MalformedResponse("an access key entry has no valid public_key or no u64 nonce")
        permission = access.get("permission")
        if permission == "FullAccess":
            count += 1
            continue
        call = (permission["FunctionCall"] if isinstance(permission, dict) and set(permission) == {"FunctionCall"}
                else None)
        allowance = call.get("allowance", "") if isinstance(call, dict) else ""
        if not (isinstance(call, dict) and isinstance(call.get("receiver_id"), str)
                and isinstance(call.get("method_names"), list)
                and all(isinstance(name, str) for name in call["method_names"])
                and (allowance is None or (isinstance(allowance, str) and allowance.isascii()
                                           and allowance.isdigit() and int(allowance) <= ALLOWANCE_MAX))):
            raise MalformedResponse("an access key has no complete FullAccess or FunctionCall permission")
        try:
            canon.account_id(call["receiver_id"], "receiver_id")
        except ValueError as error:
            raise MalformedResponse(f"an access key names an invalid receiver_id: {error}") from error
    return count


def _paged_full_access(client, account_id):
    """(full access keys on the page that held the first one, keys read, whether the list was read to its
    end) over at most KEY_PAGES_MAX pages, every page after the first pinned to the first page's block."""
    after, block, read = None, None, 0
    for _ in range(KEY_PAGES_MAX):
        page = client.view_access_key_page(account_id, after_key=after, block_id=block)
        if block is not None and page.get("block_height") != block:
            raise MalformedResponse(f"a page of the access key list was read at block {page.get('block_height')}, "
                                    f"not block {block}")
        keys = page["keys"]
        if after is not None and any(isinstance(key, dict) and key.get("public_key") == after for key in keys):
            raise MalformedResponse("a page of the access key list repeats the key it was read after")
        full = _full_access_count(keys)
        read += len(keys)
        last = page.get("last_key")
        if full or last is None:
            return full, read, last is None
        if not keys or last != keys[-1]["public_key"]:
            raise MalformedResponse("the page cursor is not the last key of its page")
        after, block = last, page["block_height"]
    return 0, read, False


def _incident_code_hashes():
    """(valid reported code hash: incident id, failures) over every incident page. A page without
    code_hash_as_reported reports no hash. A page that cannot be read, is not an object with an id, or
    carries that field without a value of 32 bytes of base58 is a failure, and so is a corpus that
    reports no code hash at all."""
    reported, failures = {}, []
    for path in sorted(Path(feed.INCIDENTS_DIR).glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            failures.append(f"could not read the incident page {path.name}: {rules._why(error)}")
            continue
        if not (isinstance(document, dict) and isinstance(document.get("id"), str) and document["id"]):
            failures.append(f"the incident page {path.name} is not an object with an id")
            continue
        if "code_hash_as_reported" not in document:
            continue
        field = document["code_hash_as_reported"]
        value = field.get("value") if isinstance(field, dict) else None
        if not _decodes_to(value, CODE_HASH_BYTES):
            failures.append(f"the incident page {document['id']} reports a code hash that is not "
                            f"{CODE_HASH_BYTES} bytes of base58")
            continue
        reported.setdefault(value, document["id"])
    if not reported and not failures:
        failures.append("no incident page reports a code hash")
    return reported, failures


def check_account(account_id, *, store, rpc, nearblocks, archival, issuers, now_ms, rules_version=1):
    canon.account_id(account_id, "account_id")
    if account_id == "testnet" or account_id.endswith(".testnet"):
        raise ValueError("account checks accept mainnet accounts only")
    started = time.perf_counter()
    request = {"kind": "near", "token_id": None, "receiver_id": account_id,
               "amount": str(rules.YOCTO_PER_NEAR), "memo": None}
    client = _OneRead(rpc, account_id)
    judgement = rules.judge(request, {"allowlist": [], "caps": []}, store, client, nearblocks,
                            now_ms=now_ms, injection_scanner=None, archival=archival, issuers=issuers,
                            rules_version=rules_version)
    checks = [check for check in judgement.checks if check["id"] in KEPT_CHECKS]
    extra = []
    try:
        account = client.view_account(account_id)
    except rules.UNAVAILABLE as error:
        extra.append(evidence.check("incident_code", "unknown",
                                    f"could not read the account code hash: {rules._why(error)}",
                                    rules._source(client)))
    else:
        account_source = rules._source(client)
        code_hash = account["code_hash"]
        global_hash = account.get("global_contract_hash")
        global_account = account.get("global_contract_account_id")
        if global_hash is not None or global_account is not None:
            checks = [check for check in checks if check["id"] != "code_without_metadata"]
        if not _decodes_to(code_hash, CODE_HASH_BYTES):
            extra.append(evidence.check("incident_code", "unknown",
                                        f"view_account answers the code hash {rules._quoted(code_hash)}, which is "
                                        f"not {CODE_HASH_BYTES} bytes of base58", account_source))
        elif code_hash == rules.EMPTY_CODE_HASH and global_hash is None and global_account is None:
            extra.append(evidence.check("incident_code", "pass", "no contract code", account_source))
        elif global_account is not None:
            extra.append(evidence.check("contract_control", "unknown",
                                        f"runs the global contract published by {global_account}; its control "
                                        "was not checked", account_source))
            extra.append(evidence.check("incident_code", "unknown",
                                        f"runs the global contract published by {global_account}; its hash was "
                                        "not compared", account_source))
        else:
            code_hash = global_hash if global_hash is not None else code_hash
            try:
                full_access = _full_access_count(client.view_access_key_list(account_id)["keys"])
            except rules.UNAVAILABLE as error:
                if not (isinstance(error, rules.RpcError) and error.cause == "TOO_MANY_ACCESS_KEYS"):
                    extra.append(evidence.check("contract_control", "unknown",
                                                f"could not read the access key list: {rules._why(error)}",
                                                rules._source(client)))
                else:
                    try:
                        info = error.error["cause"].get("info")
                        limit = info.get("limit") if isinstance(info, dict) else None
                        if type(limit) is not int or limit < 1:
                            raise MalformedResponse("the access key list refusal has no positive integer limit")
                        with request_deadline(KEY_PAGES_S):
                            full_access, read, complete = _paged_full_access(client, account_id)
                    except rules.UNAVAILABLE as paged_error:
                        status = "unknown"
                        detail = f"could not read the paged access key list: {rules._why(paged_error)}"
                    else:
                        status, detail = (
                            ("unknown", f"at least {full_access} full access key{'s' if full_access > 1 else ''} "
                                        f"among the first {read} keys of the paged access key list; whoever holds "
                                        "one can replace the code") if full_access
                            else ("unknown", f"the paged access key list ends after {read} keys, but RPC refused "
                                             f"the whole list as holding more than {limit}, so it was not read in "
                                             "full") if complete and read <= limit
                            else ("pass", f"contract code with no full access key among the {read} keys of the "
                                          "complete paged access key list") if complete
                            else ("unknown", f"no full access key among the first {read} keys of the paged access "
                                             "key list, and the rest was not read"))
                    extra.append(evidence.check("contract_control", status, detail, rules._source(client)))
            else:
                status = "unknown" if full_access else "pass"
                detail = (f"{full_access} full access key{'s' if full_access > 1 else ''}; whoever holds one "
                          "can replace the code"
                          if full_access else "contract code with no full access key")
                extra.append(evidence.check("contract_control", status, detail, rules._source(client)))
            reported, failures = _incident_code_hashes()
            if code_hash in reported:
                extra.append(evidence.check("incident_code", "signal",
                                            f"code hash {code_hash} equals the code hash the incident page "
                                            f"{reported[code_hash]} reports",
                                            rules._local("incident pages", evidence.evidence_hash(reported))))
            elif failures:
                extra.append(evidence.check("incident_code", "unknown",
                                            f"code hash {code_hash} was not compared: {'; '.join(failures)}",
                                            rules._local("incident pages")))
            else:
                extra.append(evidence.check("incident_code", "pass",
                                            f"code hash {code_hash} matches no code hash reported by an incident page",
                                            account_source))
    checks += extra
    statuses = {check["status"] for check in checks}
    verdict = "DENY" if "signal" in statuses else "UNKNOWN" if "unknown" in statuses else "ALLOW"
    if (rules_version == 3 and judgement.verdict == "UNKNOWN"
            and {check["id"] for check in checks if check["status"] == "signal"} == {"account_age"}):
        verdict = "UNKNOWN"
    status = "signal" if verdict == "DENY" else "unknown"
    if verdict == judgement.verdict:
        reasons = judgement.reasons + [check["detail"] for check in extra if check["status"] == status]
    else:
        reasons = [check["detail"] for check in checks if check["status"] == status]
    latency_ms = int((time.perf_counter() - started) * 1000)
    document = evidence.document(
        subject={"network": "mainnet", "kind": "account", "account": account_id},
        checks=checks, labels=judgement.labels, verdict=verdict, reasons=reasons,
        observed=judgement.observed, issued_ms=None, expires_ms=None, verdict_id=None, latency_ms=latency_ms)
    outcome, action = {"ALLOW": ("ALLOW", "sign"), "DENY": ("DENY", "refuse"), "UNKNOWN": ("ASK", "ask")}[verdict]
    return {"outcome": outcome, "action": action, "reasons": reasons,
            "checks": [{"name": check["id"], "status": check["status"], "detail": check["detail"]}
                       for check in checks],
            "evidence_hash": evidence.evidence_hash(document), "freshness": dict(judgement.observed),
            "verdict": None, "signing": "never", "latency_ms": latency_ms,
            "evidence": evidence.canonical_bytes(document).decode("utf-8")}
