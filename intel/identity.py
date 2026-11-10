"""Read-only account identity observations; reported metadata never establishes ownership."""

import dataclasses
import json
import os
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from gate import base58, rpc
from intel import labels
from intel.accounts import AccountInputError, AccountReadError, _account, _amount, _height, _text

READ_TIMEOUT_S = 20
MAX_VALIDATORS = 1000
MAX_RECORDS = 32
MAX_RECORD_BYTES = 16384
MAX_RECORD_TOTAL = 65536
ENTITIES_PATH = Path(__file__).resolve().parent / "entities.jsonl"
_UNAVAILABLE = (rpc.FixtureMiss, rpc.HttpError, rpc.TransportError, rpc.RateLimited,
                rpc.MalformedResponse, rpc.RpcError, OSError)


def _registry_entities(account):
    matches, total, truncated = [], 0, False
    with ENTITIES_PATH.open(encoding="utf-8") as source:
        for line in source:
            entity = json.loads(line)
            if account not in entity["members"]:
                continue
            size = len(line.encode("utf-8"))
            if len(matches) >= MAX_RECORDS or size > MAX_RECORD_BYTES or total + size > MAX_RECORD_TOTAL:
                truncated = True
                continue
            matches.append(entity)
            total += size
    return matches, truncated


def _token_metadata(value):
    if not isinstance(value, dict) or value.get("spec") != "ft-1.0.0":
        raise AccountReadError("Token metadata does not establish the supported NEP-148 schema.")
    if type(value.get("decimals")) is not int or not 0 <= value["decimals"] <= 255:
        raise AccountReadError("Token decimals are invalid.")
    return {"spec": value["spec"], "name": _text(value.get("name"), "token name", 256),
            "symbol": _text(value.get("symbol"), "token symbol", 64), "decimals": value["decimals"],
            "provenance": "contract_reported"}


def _contract_metadata(value):
    if not isinstance(value, dict) or not any(value.get(field) is not None for field in ("version", "link", "standards")):
        raise AccountReadError("Contract source metadata has no supported fields.")
    version = _text(value.get("version"), "contract version", 256, nullable=True)
    link = _text(value.get("link"), "source link", 2048, nullable=True)
    if link is not None:
        try:
            parsed = urllib.parse.urlsplit(link)
            valid = (parsed.scheme == "https" and parsed.hostname and parsed.username is None
                     and parsed.password is None and not any(char.isspace() for char in link))
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise AccountReadError("Contract source link must be an HTTPS URL without credentials.")
    standards = value.get("standards")
    if standards is not None:
        if not isinstance(standards, list) or len(standards) > 32:
            raise AccountReadError("Contract standards are invalid.")
        parsed_standards = []
        for standard in standards:
            if not isinstance(standard, dict) or set(standard) != {"standard", "version"}:
                raise AccountReadError("Contract standard fields are invalid.")
            parsed_standards.append({"standard": _text(standard.get("standard"), "standard name", 64),
                                     "version": _text(standard.get("version"), "standard version", 64)})
        standards = parsed_standards
    return {"version": version, "link": link, "standards": standards, "provenance": "contract_reported"}


def _validator(value, account):
    if not isinstance(value, dict):
        raise AccountReadError("The validator response is invalid.")
    start = _height(value.get("epoch_start_height"))
    epoch = _height(value["epoch_height"]) if "epoch_height" in value else None
    rows = value.get("current_validators")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_VALIDATORS:
        raise AccountReadError("The current validator list is missing or exceeds the read bound.")
    seen, matched = set(), None
    for row in rows:
        if not isinstance(row, dict):
            raise AccountReadError("A current validator entry is invalid.")
        try:
            subject = _account(row.get("account_id"))
        except AccountInputError:
            raise AccountReadError("A current validator identifier is invalid.") from None
        stake = _amount(row.get("stake"))
        if subject in seen:
            raise AccountReadError("The current validator response repeats an account.")
        seen.add(subject)
        if subject == account:
            matched = stake
    return {"status": "current" if matched is not None else "not_current", "stake_yocto": matched,
            "epoch_start_height": start, "epoch_height": epoch}


class IdentityData:
    def __init__(self, store=None, *, client_factory=rpc.Client, clock=time.time):
        self.store = store if store is not None else labels.load()
        self.client_factory = client_factory
        self.clock = clock

    @staticmethod
    def _source(client, method, args, *, block_height=None):
        if not isinstance(client.last_source, dict):
            return None
        return {**client.last_source, "method": method, "args": args, "block_height": block_height,
                "recorded": False, "network": "mainnet", "read_kind": "live_rpc"}

    def inspect(self, account):
        account = _account(account)
        client = self.client_factory(os.environ.get("NEAR_MAINNET_RPC_URL") or rpc.MAINNET_RPC_URL, "off")
        contract = {"status": "unknown", "code_hash": None, "block_height": None, "source": None}
        token, metadata = None, None
        validator = {"status": "unknown", "stake_yocto": None, "epoch_start_height": None,
                     "epoch_height": None, "source": None}
        sources = []
        coverage = {"contract": "unknown", "token_metadata": "unknown", "contract_source": "unknown",
                    "validator": "unknown",
                    "scope": "Mainnet RPC observations; metadata is contract-reported, not verified brand or ownership.",
                    "registry": "Committed registry memberships and assertions retain original dates and sources; not current ownership.",
                    "source_metadata": "Version, HTTPS link and standards only; build reproducibility is not verified."}
        with rpc.request_deadline(READ_TIMEOUT_S):
            try:
                value = client.view_account(account)
                if not isinstance(value, dict):
                    raise AccountReadError("Account state is invalid.")
                code_hash = _text(value.get("code_hash"), "code hash", 44)
                if len(base58.decode(code_hash)) != 32:
                    raise AccountReadError("Account code hash is invalid.")
                block = _height(value.get("block_height"))
                source = self._source(client, "view_account", {"account_id": account, "finality": "final"}, block_height=block)
                contract = {"status": "none" if code_hash == rpc.EMPTY_CODE_HASH else "deployed",
                            "code_hash": code_hash, "block_height": block, "source": source}
                coverage["contract"] = "known"
            except TimeoutError:
                raise
            except (*_UNAVAILABLE, ValueError):
                source = self._source(client, "view_account", {"account_id": account, "finality": "final"})
                contract["source"] = source
            if source is not None:
                sources.append(source)
            if contract["status"] == "deployed":
                for method, parse, field in (("ft_metadata", _token_metadata, "token_metadata"),
                                             ("contract_source_metadata", _contract_metadata, "contract_source")):
                    parsed = None
                    client.last_source = None
                    client.last_block_height = None
                    try:
                        value = client.call_function(account, method, {}, block_id=int(contract["block_height"]))
                        if _height(client.last_block_height) != contract["block_height"]:
                            raise AccountReadError("Metadata response does not match the requested finalized block.")
                        parsed = parse(value)
                        coverage[field] = "known"
                    except TimeoutError:
                        raise
                    except (*_UNAVAILABLE, AccountReadError):
                        coverage[field] = "unknown"
                    source = self._source(client, method, {"account_id": account, "args": {},
                                                          "block_id": int(contract["block_height"])},
                                          block_height=str(client.last_block_height) if type(client.last_block_height) is int else None)
                    if source is not None:
                        sources.append(source)
                    if parsed is not None:
                        parsed["source"] = source
                    if field == "token_metadata":
                        token = parsed
                    else:
                        metadata = parsed
            elif contract["status"] == "none":
                coverage.update(token_metadata="not_applicable", contract_source="not_applicable")
            client.last_source = None
            client.last_block_height = None
            try:
                value = client.call("validators", [None])
                validator = {**_validator(value, account), "source": None}
                coverage["validator"] = "known"
            except TimeoutError:
                raise
            except (*_UNAVAILABLE, AccountReadError):
                coverage["validator"] = "unknown"
            source = self._source(client, "validators", [None])
            validator["source"] = source
            if source is not None:
                sources.append(source)
        registry, truncated = _registry_entities(account)
        assertions, total = [], 0
        coverage["assertions_truncated"] = False
        for row in self.store.by_subject(account, "near"):
            if row.superseded_by is not None:
                continue
            record = dataclasses.asdict(row)
            size = len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            if len(assertions) >= MAX_RECORDS or size > MAX_RECORD_BYTES or total + size > MAX_RECORD_TOTAL:
                coverage["assertions_truncated"] = True
                continue
            assertions.append(record)
            total += size
        coverage["registry_truncated"] = truncated
        return {"account_id": account, "network": "mainnet",
                "observed_at": datetime.fromtimestamp(self.clock(), UTC).isoformat().replace("+00:00", "Z"),
                "contract": contract, "token_metadata": token, "contract_source": metadata, "validator": validator,
                "registry_entities": registry, "assertions": assertions, "sources": sources, "coverage": coverage}
