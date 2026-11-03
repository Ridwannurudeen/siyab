"""Build label rows and entity groups from recorded NEAR registry reads."""

import argparse
import json
import sys
from pathlib import Path

from gate import rpc
from intel import seed

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "fixtures" / "registries"
ROWS_PATH = Path(__file__).resolve().parent / "registries" / "rows.jsonl"
HYPOTHESES_PATH = Path(__file__).resolve().parent / "registries" / "hypotheses.jsonl"
ENTITIES_PATH = Path(__file__).resolve().parent / "entities.jsonl"
BRIDGE_METHOD = "get_tokens_accounts"
REGISTRY_ORDER = ("sputnik_dao", "validators", "ref_whitelist", "bridge_factory")
EXCHANGE_ACCOUNTS = (
    "binancecold3.near", "binance.near", "binancecold1.near", "binancecold2.near",
    "okx.near", "okex.near", "bybit.near", "kraken.near", "coinbase.near", "kucoin.near",
    "gate.near", "gateio.near",
)


class RegistryError(ValueError):
    """A registry response does not match the expected view shape."""


def _client(mode="replay"):
    return rpc.Client(rpc.MAINNET_RPC_URL, mode, fixtures_dir=FIXTURES_DIR)


def _read(client):
    dao_count = client.call_function("sputnik-dao.near", "get_number_daos", {})
    dao_accounts = client.call_function("sputnik-dao.near", "get_dao_list", {})
    dao_source = {"block": client.last_block_height, "fetched_at": client.last_source["fetched_at"]}
    if not isinstance(dao_count, int) or isinstance(dao_count, bool) or not isinstance(dao_accounts, list):
        raise RegistryError("the Sputnik DAO views returned an unexpected shape")
    if len(dao_accounts) != dao_count or not all(isinstance(account, str) for account in dao_accounts):
        raise RegistryError("get_dao_list does not match get_number_daos")

    validator_answer = client.call("validators", [None])
    validator_source = {"fetched_at": client.last_source["fetched_at"]}
    if not isinstance(validator_answer, dict):
        raise RegistryError("validators returned an unexpected shape")
    epoch_height = validator_answer.get("epoch_start_height")
    validators = validator_answer.get("current_validators")
    if not isinstance(epoch_height, int) or isinstance(epoch_height, bool) or not isinstance(validators, list):
        raise RegistryError("validators has no epoch_start_height or current_validators list")
    if not all(isinstance(item, dict) and isinstance(item.get("account_id"), str) for item in validators):
        raise RegistryError("current_validators contains an unexpected entry")
    validator_source["block"] = epoch_height

    ref_accounts = client.call_function("v2.ref-finance.near", "get_whitelisted_tokens", {})
    ref_source = {"block": client.last_block_height, "fetched_at": client.last_source["fetched_at"]}
    if not isinstance(ref_accounts, list) or not all(isinstance(account, str) for account in ref_accounts):
        raise RegistryError("get_whitelisted_tokens returned an unexpected shape")

    bridge_accounts = client.call_function("factory.bridge.near", BRIDGE_METHOD, {})
    bridge_source = {"block": client.last_block_height, "fetched_at": client.last_source["fetched_at"]}
    if not isinstance(bridge_accounts, list) or not all(isinstance(account, str) for account in bridge_accounts):
        raise RegistryError(f"{BRIDGE_METHOD} returned an unexpected shape")

    rows = {"sputnik_dao": [], "validators": [], "ref_whitelist": [], "bridge_factory": []}
    for index, account in enumerate(dao_accounts, start=1):
        rows["sputnik_dao"].append(seed.make_row(
            entity=f"Sputnik DAO: {seed._quoted(account.removesuffix('.sputnik-dao.near'))}", subject=account, chain="near",
            label="DAO created by the Sputnik DAO factory", category="organisation", provenance="onchain",
            source={"rpc": rpc.MAINNET_RPC_URL, "method": "get_dao_list", "args": {},
                    "block": dao_source["block"]}, observed_at=dao_source["fetched_at"],
            note=f'listed by "sputnik-dao.near" get_dao_list at block {dao_source["block"]}; '
                 f"index {index} of {dao_count}"))

    for validator in validators:
        account = validator["account_id"]
        rows["validators"].append(seed.make_row(
            entity=f"validator {seed._quoted(account)}", subject=account, chain="near",
            label=f"current validator at epoch {epoch_height}", category="organisation", provenance="onchain",
            source={"rpc": rpc.MAINNET_RPC_URL, "method": "validators", "args": {"block_id": None},
                    "block": epoch_height}, observed_at=validator_source["fetched_at"],
            note=f'listed in "validators" current_validators at epoch start height {epoch_height}'))

    for account in ref_accounts:
        rows["ref_whitelist"].append(seed.make_row(
            entity="Ref Finance whitelist", subject=account, chain="near",
            label="token whitelisted on the Ref Finance v2 exchange contract", category="catalog_token",
            provenance="onchain",
            source={"rpc": rpc.MAINNET_RPC_URL, "method": "get_whitelisted_tokens", "args": {},
                    "block": ref_source["block"]}, observed_at=ref_source["fetched_at"],
            note=f'listed by "v2.ref-finance.near" get_whitelisted_tokens at block {ref_source["block"]}'))

    for account in bridge_accounts:
        rows["bridge_factory"].append(seed.make_row(
            entity="Rainbow Bridge token factory", subject=account, chain="near",
            label="token account listed by the Rainbow Bridge token factory", category="bridge",
            provenance="onchain",
            source={"rpc": rpc.MAINNET_RPC_URL, "method": BRIDGE_METHOD, "args": {},
                    "block": bridge_source["block"]}, observed_at=bridge_source["fetched_at"],
            note=f'listed by "factory.bridge.near" {BRIDGE_METHOD} at block {bridge_source["block"]}'))

    return rows


def rows():
    """Build registry rows from the recorded JSON-RPC reads."""
    return _read(_client())


def entities(by_registry=None):
    """Group registry rows into DAO, validator and token-list entities."""
    by_registry = by_registry if by_registry is not None else rows()
    result = []
    dao_rows = by_registry["sputnik_dao"]
    for row in dao_rows:
        account = row["subject"]
        members = [account]
        suffix = f".{account}"
        members.extend(candidate["subject"] for candidate in dao_rows if candidate["subject"].endswith(suffix))
        result.append({"id": f"dao-{account}", "name": row["entity"], "kind": "dao", "members": members,
                       "source": row["source"], "observed_at": row["observed_at"]})

    for row in by_registry["validators"]:
        account = row["subject"]
        result.append({"id": f"validator-{account}", "name": row["entity"], "kind": "validator",
                       "members": [account], "source": row["source"], "observed_at": row["observed_at"]})

    ref_rows = by_registry["ref_whitelist"]
    if ref_rows:
        row = ref_rows[0]
        result.append({"id": "ref-finance-whitelist", "name": row["entity"], "kind": "token_list",
                       "members": [entry["subject"] for entry in ref_rows], "source": row["source"],
                       "observed_at": row["observed_at"]})

    bridge_rows = by_registry["bridge_factory"]
    if bridge_rows:
        row = bridge_rows[0]
        result.append({"id": "rainbow-bridge-token-factory", "name": row["entity"],
                       "kind": "bridge_factory", "members": [entry["subject"] for entry in bridge_rows],
                       "source": row["source"], "observed_at": row["observed_at"]})
    return result


def _write_jsonl(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
                              for value in values), encoding="utf-8")


def record():
    """Fetch permitted registry views, then write the generated rows and entities."""
    by_registry = _read(_client("record"))
    flat_rows = [row for name in REGISTRY_ORDER for row in by_registry[name]]
    _write_jsonl(ROWS_PATH, flat_rows)
    _write_jsonl(ENTITIES_PATH, entities(by_registry))
    return by_registry


def append():
    """Append registry rows whose ids are absent from the label store."""
    return seed.append([row for group in rows().values() for row in group])


def hypotheses(facts=None, mode="replay"):
    """The exchange named account facts from recorded account and FastNEAR reads, then the cautious
    exchange name hypotheses those facts support."""
    if facts is None:
        client = rpc.Client(rpc.MAINNET_RPC_URL, mode, fixtures_dir=FIXTURES_DIR)
        http = rpc.Client("https://api.fastnear.com", mode, fixtures_dir=FIXTURES_DIR, kind="http")
        top_url = "https://api.fastnear.com/v1/ft/usdt.tether-token.near/top"
        top = http.get(top_url)
        if not isinstance(top, dict) or not isinstance(top.get("accounts"), list):
            raise RegistryError("FastNEAR top holders returned an unexpected shape")
        top_source = seed._document_source(
            top_url, json.dumps(top, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            http.last_source["fetched_at"])
        top_accounts = {entry.get("account_id") for entry in top["accounts"] if isinstance(entry, dict)}
        facts = []
        for account in EXCHANGE_ACCOUNTS:
            view = client.view_account(account)
            block = client.last_block_height
            observed_at = client.last_source["fetched_at"]
            if not isinstance(block, int) or not isinstance(observed_at, str):
                raise RegistryError(f"view_account has no block or fetch time for {account}")
            balance = int(view["amount"]) + int(view["locked"])
            balance_class = "zero" if balance == 0 else "under 8 NEAR" if balance < 8 * 10**24 else "8 NEAR or more"
            has_code = view["code_hash"] != rpc.EMPTY_CODE_HASH
            facts.append(seed.make_row(
                entity=f"NEAR account named {seed._quoted(account)}", subject=account, chain="near",
                label=f"account exists; {'contract code present' if has_code else 'no contract code'}; "
                      f"balance class {balance_class}", category="organisation", provenance="onchain",
                source={"rpc": rpc.MAINNET_RPC_URL, "method": "view_account",
                        "args": {"account_id": account}, "block": block}, observed_at=observed_at,
                note=f'view_account for {seed._quoted(account)} at block {block}'))

            full_url = f"https://api.fastnear.com/v1/account/{account}/full"
            full = http.get(full_url)
            if not isinstance(full, dict) or full.get("account_id") != account or not isinstance(full.get("pools"), list):
                raise RegistryError(f"FastNEAR full account returned an unexpected shape for {account}")
            full_source = seed._document_source(
                full_url, json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                http.last_source["fetched_at"])
            evidence = []
            if account in top_accounts:
                evidence.append(seed.make_row(
                    entity=f"FastNEAR top holder data for {seed._quoted(account)}", subject=account, chain="near",
                    label='listed in FastNEAR top holders for "usdt.tether-token.near"', category="organisation",
                    provenance="third_party", source=top_source, observed_at=top_source["fetched_at"],
                    note="FastNEAR top holder response"))
            account_name = account.split(".", 1)[0]
            brand = account_name.removesuffix("cold1").removesuffix("cold2").removesuffix("cold3")
            matching_pools = [pool["pool_id"] for pool in full["pools"]
                              if isinstance(pool, dict) and isinstance(pool.get("pool_id"), str)
                              and pool["pool_id"].split(".", 1)[0].startswith(brand)]
            if matching_pools:
                evidence.append(seed.make_row(
                    entity=f"FastNEAR pool data for {seed._quoted(account)}", subject=account, chain="near",
                    label="staking pool name repeats the named account pattern", category="organisation",
                    provenance="third_party", source=full_source, observed_at=full_source["fetched_at"],
                    note=f"FastNEAR lists pool account names {seed._quoted(', '.join(matching_pools))}"))
            facts.extend(evidence)

    by_subject = {}
    for fact in facts:
        if fact["subject"] in EXCHANGE_ACCOUNTS:
            by_subject.setdefault(fact["subject"], []).append(fact)

    result = []
    for account, account_facts in by_subject.items():
        chain_facts = [fact for fact in account_facts if fact["provenance"] == "onchain"]
        support = [fact for fact in account_facts if fact["provenance"] == "third_party"]
        if not chain_facts or not support:
            continue
        inputs = [fact["id"] for fact in chain_facts + support]
        evidence_text = "; ".join(fact["label"] for fact in support)
        result.append(seed.make_row(
            entity=f"exchange named account hypothesis for {seed._quoted(account)}", subject=account, chain="near",
            label=f"attribution hypothesis: exchange name pattern; {evidence_text}", category="organisation",
            provenance="inferred", source={"rule_id": "exchange_name_pattern", "inputs": inputs},
            observed_at=max(fact["observed_at"] for fact in account_facts), confidence=0.6,
            rule_id="exchange_name_pattern",
            note="attribution hypothesis; no official exchange address list for NEAR was found"))
    return facts + result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m intel.registries")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("record", help="record registry reads and write rows and entities")
    commands.add_parser("rows", help="print row counts by registry from fixtures")
    commands.add_parser("append", help="append registry rows that are not in the label store")
    commands.add_parser("hypotheses", help="write the exchange account facts and hypotheses from fixtures")
    args = parser.parse_args(argv)
    if args.command == "record":
        result = record()
        for name in REGISTRY_ORDER:
            print(f"{name}: {len(result[name])}")
        return 0
    if args.command == "rows":
        result = rows()
        for name in REGISTRY_ORDER:
            print(f"{name}: {len(result[name])}")
        return 0
    if args.command == "hypotheses":
        result = hypotheses()
        _write_jsonl(HYPOTHESES_PATH, result)
        for provenance in ("onchain", "third_party", "inferred"):
            print(f"{provenance}: {sum(row['provenance'] == provenance for row in result)}")
        return 0
    added = append()
    print(f"{added} new registry assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
