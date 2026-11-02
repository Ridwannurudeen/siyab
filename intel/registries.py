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
ENTITIES_PATH = Path(__file__).resolve().parent / "entities.jsonl"
BRIDGE_METHOD = "get_tokens_accounts"
REGISTRY_ORDER = ("sputnik_dao", "validators", "ref_whitelist", "bridge_factory")


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
            entity=f"Sputnik DAO: {account.removesuffix('.sputnik-dao.near')}", subject=account, chain="near",
            label="DAO created by the Sputnik DAO factory", category="organisation", provenance="onchain",
            source={"rpc": rpc.MAINNET_RPC_URL, "method": "get_dao_list", "args": {},
                    "block": dao_source["block"]}, observed_at=dao_source["fetched_at"],
            note=f'listed by "sputnik-dao.near" get_dao_list at block {dao_source["block"]}; '
                 f"index {index} of {dao_count}"))

    for validator in validators:
        account = validator["account_id"]
        rows["validators"].append(seed.make_row(
            entity=f"validator {account}", subject=account, chain="near",
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
            label="token deployed by the Rainbow Bridge token factory", category="bridge", provenance="onchain",
            source={"rpc": rpc.MAINNET_RPC_URL, "method": BRIDGE_METHOD, "args": {},
                    "block": bridge_source["block"]}, observed_at=bridge_source["fetched_at"],
            note=f'listed by "factory.bridge.near" {BRIDGE_METHOD} at block {bridge_source["block"]}'))

    return rows, {"sputnik_dao": dao_count, "validators": len(validators), "ref_whitelist": len(ref_accounts),
                  "bridge_factory": len(bridge_accounts)}


def rows():
    """Build registry rows from the recorded JSON-RPC reads."""
    return _read(_client())[0]


def reads():
    """Return the counts carried by the recorded registry answers."""
    return _read(_client())[1]


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
    by_registry, _ = _read(_client("record"))
    flat_rows = [row for name in REGISTRY_ORDER for row in by_registry[name]]
    _write_jsonl(ROWS_PATH, flat_rows)
    _write_jsonl(ENTITIES_PATH, entities(by_registry))
    return by_registry


def append():
    """Append registry rows whose ids are absent from the label store."""
    return seed.append([row for group in rows().values() for row in group])


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m intel.registries")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("record", help="record registry reads and write rows and entities")
    commands.add_parser("rows", help="print row counts by registry from fixtures")
    commands.add_parser("append", help="append registry rows that are not in the label store")
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
    added = append()
    print(f"{added} new registry assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
