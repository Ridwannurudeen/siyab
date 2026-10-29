"""Seed the label store from verified official sources and on chain reads.

Sub commands: treasuries (the NEAR Intents treasury page), catalog (the 1Click asset catalog) and
infrastructure (omft.near and intents.near source metadata, the .omft.near bridge token accounts,
v1.signer and the OutLayer accounts). GATE_FIXTURES_MODE decides the network: replay (default)
reads the newest snapshot under intel/seeds/raw/ and the recorded RPC fixtures; record fetches
the page once, stores it byte for byte as raw/<name>-<UTC time>.<ext> and records RPC reads.
Each run appends only rows whose id is new and rewrites intel/seeds/README.md.
"""

import hashlib
import json
import os
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from gate import rpc
from intel import labels

INTEL_DIR = Path(__file__).resolve().parent
RAW_DIR = INTEL_DIR / "seeds" / "raw"
README_PATH = INTEL_DIR / "seeds" / "README.md"
TREASURY_URL = "https://docs.near-intents.org/security-compliance/treasury-addresses.md"
CATALOG_URL = "https://1click.chaindefuser.com/v0/tokens"
SNAPSHOTS = {"treasuries": ("near-intents-treasury-addresses", "md", TREASURY_URL),
             "catalog": ("1click-tokens", "json", CATALOG_URL)}
SOURCES = ("treasuries", "catalog", "infrastructure")
_STAMP = "%Y%m%dT%H%M%SZ"
_TIME = "%Y-%m-%dT%H:%M:%SZ"
# Page names to 1Click blockchain codes. Aurora has no code in the catalog and keeps its name;
# the refill row names "EVM Chains" without a list, recorded as chain "evm".
CHAIN_CODES = {"Arbitrum": "arb", "Avalanche": "avax", "ADI": "adi", "Aurora": "aurora", "Base": "base",
               "Bera": "bera", "BNB": "bsc", "Ethereum": "eth", "Gnosis": "gnosis", "Optimism": "op",
               "Plasma": "plasma", "Polygon": "pol", "Bitcoin (BTC)": "btc", "Zcash (ZEC)": "zec",
               "NEAR": "near", "Solana (SOL)": "sol", "TON": "ton", "Solana": "sol", "EVM Chains": "evm"}
# The rows the treasury seed takes from the page: (section, row name).
TREASURY_ROWS = (("EVM Chains", "NEAR Intents"), ("EVM Chains", "HOT Bridge"),
                 ("Bitcoin & Forks", "Bitcoin (BTC)"), ("Bitcoin & Forks", "Zcash (ZEC)"),
                 ("Layer 1 Chains", "NEAR"), ("Layer 1 Chains", "Solana (SOL)"), ("Layer 1 Chains", "TON"),
                 ("Refill Addresses", "EVM Chains"), ("Refill Addresses", "Solana"),
                 ("Refill Addresses", "TON"))
# Catalog assets left out of the store: their symbol is the one word the honesty rule bars from
# every label and reason, so no row, and no lookalike reason, can carry it.
EXCLUDED_ASSETS = ("nep141:eth-0x5afe3855358e112b5647b952709e6165e1c1eeee.omft.near",
                   "nep141:gnosis-0x4d18815d14fe5c3304e87b3fa18318baa5c23820.omft.near")
# Accounts confirmed with view_account; the roles come from documentation, not from the chain.
DOCUMENTED_ACCOUNTS = (
    ("v1.signer", "NEAR MPC", "MPC signer contract"),
    ("outlayer.near", "OutLayer", "OutLayer contract"),
    ("worker.outlayer.near", "OutLayer", "OutLayer register contract"),
    ("dao.outlayer.near", "OutLayer", "OutLayer keystore DAO"),
    ("connectors.outlayer.near", "OutLayer", "OutLayer connectors account"),
)


class SeedError(Exception):
    """A snapshot or a read that does not hold what the seed expects."""


def _mode(mode):
    mode = mode or os.environ.get("GATE_FIXTURES_MODE") or "replay"
    if mode not in ("replay", "record"):
        raise SeedError(f"seeds run in replay or record mode, not {mode!r}")
    return mode


def snapshot(source, mode=None, raw_dir=RAW_DIR, clock=time.time):
    """(bytes, fetched_at, path) of the source's snapshot: fetched and stored in record mode, the
    newest stored one in replay mode."""
    name, ext, url = SNAPSHOTS[source]
    raw_dir = Path(raw_dir)
    if _mode(mode) == "record":
        request = urllib.request.Request(url, headers={"User-Agent": "shieldbot-near"})
        with urllib.request.urlopen(request, timeout=rpc.TIMEOUT_S) as reply:
            data = reply.read()
        now = datetime.fromtimestamp(clock(), UTC)
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / f"{name}-{now.strftime(_STAMP)}.{ext}"
        path.write_bytes(data)
    else:
        found = sorted(raw_dir.glob(f"{name}-*.{ext}"))
        if not found:
            raise SeedError(f"no snapshot {name}-*.{ext} under {raw_dir}")
        path = found[-1]
        data = path.read_bytes()
    stamp = path.name[len(name) + 1: -len(ext) - 1]
    fetched_at = datetime.strptime(stamp, _STAMP).strftime(_TIME)
    return data, fetched_at, path


def _document_source(url, data, fetched_at):
    return {"url": url, "content_sha256": hashlib.sha256(data).hexdigest(), "fetched_at": fetched_at}


def make_row(*, entity, subject, chain, label, category, provenance, source, observed_at, note,
             confidence=1, rule_id=None):
    """A validated label row with its id."""
    row = {"id": None, "entity": entity, "subject": subject, "chain": chain, "label": label,
           "category": category, "provenance": provenance, "source": source, "observed_at": observed_at,
           "confidence": confidence, "rule_id": rule_id, "superseded_by": None, "note": note}
    row["id"] = labels.assertion_id(row)
    labels.validate(row)
    return row


def _address(cell):
    start = cell.find("`")
    end = cell.find("`", start + 1)
    return cell[start + 1: end] if start != -1 and end != -1 else None


def _tables(text):
    """{(section, row name): [the backticked address of each later cell, or None]}."""
    section, rows = None, {}
    for line in text.splitlines():
        if line.startswith("#"):
            section = line.lstrip("#").strip()
        elif line.startswith("| **") and section:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows[(section, cells[0].strip("*"))] = [_address(cell) for cell in cells[1:]]
    return rows


def _evm_chains(text):
    lines = text.splitlines()
    if "### EVM Chains" not in lines:
        raise SeedError("the treasury page has no EVM Chains section")
    following = [line for line in lines[lines.index("### EVM Chains") + 1:] if line.strip()]
    return [name.strip() for name in following[0].split(",")]


def treasuries(mode=None, raw_dir=RAW_DIR):
    """One treasury assertion per address and chain the page lists, for the rows TREASURY_ROWS names."""
    data, fetched_at, _ = snapshot("treasuries", mode, raw_dir)
    text = data.decode("utf-8")
    tables = _tables(text)
    missing = [key for key in TREASURY_ROWS if key not in tables]
    if missing:
        raise SeedError(f"the treasury page lacks the rows {missing}")
    source = _document_source(TREASURY_URL, data, fetched_at)
    common = {"category": "treasury", "provenance": "official_source", "source": source,
              "observed_at": fetched_at}
    rows = []
    for name in _evm_chains(text):
        for entity in ("NEAR Intents", "HOT Bridge"):
            rows.append(make_row(entity=entity, subject=tables[("EVM Chains", entity)][0],
                                 chain=CHAIN_CODES[name], label=f"{entity} treasury",
                                 note=f"EVM Chains table, listed for {name}", **common))
    for section, name in TREASURY_ROWS[2:7]:
        for entity, address in zip(("NEAR Intents", "HOT Bridge"), tables[(section, name)]):
            if address:
                rows.append(make_row(entity=entity, subject=address, chain=CHAIN_CODES[name],
                                     label=f"{entity} treasury", note=f"{section} table, row {name}",
                                     **common))
    for section, name in TREASURY_ROWS[7:]:
        rows.append(make_row(entity="NEAR Intents or HOT Bridge (the page does not say which)",
                             subject=tables[(section, name)][0], chain=CHAIN_CODES[name],
                             label="refill address", note=f"{section} table, row {name}; the page says "
                             "these addresses are used for operational refills across networks", **common))
    return rows


def _near_token(asset_id):
    """The NEP-141 token account on NEAR named by a 1Click asset id, or None."""
    for prefix in ("nep141:", "1cs_v1:near:nep141:"):
        if asset_id.startswith(prefix):
            return asset_id[len(prefix):]
    return None


def _catalog_assets(mode, raw_dir):
    data, fetched_at, _ = snapshot("catalog", mode, raw_dir)
    assets = json.loads(data)
    if not isinstance(assets, list) or not all(isinstance(a, dict) and a.get("assetId") for a in assets):
        raise SeedError("the catalog snapshot is not a list of assets")
    listed = [a for a in assets if a["assetId"] not in EXCLUDED_ASSETS]
    return listed, _document_source(CATALOG_URL, data, fetched_at), fetched_at


def catalog(mode=None, raw_dir=RAW_DIR):
    """One catalog_token assertion per asset in the 1Click catalog snapshot except EXCLUDED_ASSETS.
    A NEP-141 token on NEAR
    is keyed by its NEAR account on chain near; any other asset by its contract address (or its
    asset id when it has none) on its own blockchain."""
    assets, source, fetched_at = _catalog_assets(mode, raw_dir)
    rows = []
    for asset in assets:
        account = _near_token(asset["assetId"])
        subject, chain = ((account, "near") if account
                          else (asset.get("contractAddress") or asset["assetId"], asset["blockchain"]))
        note = (f"assetId {asset['assetId']}; symbol {asset['symbol']}; blockchain {asset['blockchain']}; "
                f"contractAddress {asset.get('contractAddress') or 'none'}; decimals {asset['decimals']}")
        rows.append(make_row(entity="NEAR Intents 1Click catalog", subject=subject, chain=chain,
                             label=asset["symbol"], category="catalog_token", provenance="official_source",
                             source=source, observed_at=fetched_at, note=note))
    return rows


def infrastructure(mode=None, raw_dir=RAW_DIR, client=None):
    """omft.near and intents.near from their source metadata, every .omft.near catalog token
    account as a bridge account, and the documented accounts that view_account confirms."""
    client = client or rpc.Client(rpc.MAINNET_RPC_URL, _mode(mode))
    rows = []
    for account, label in (("intents.near", "NEAR Intents contract"),
                           ("omft.near", "parent account of the .omft.near bridge token accounts")):
        metadata = client.call_function(account, "contract_source_metadata", {})
        standards = ", ".join(f"{s['standard']} {s['version']}" for s in metadata.get("standards", []))
        rows.append(make_row(
            entity="NEAR Intents", subject=account, chain="near", label=label, category="infrastructure",
            provenance="onchain", observed_at=client.last_source["fetched_at"],
            source={"rpc": client.url, "method": "contract_source_metadata", "args": {},
                    "block": client.last_block_height},
            note=f"contract_source_metadata: version {metadata.get('version')}, link {metadata.get('link')}, "
                 f"standards {standards}"))

    assets, source, fetched_at = _catalog_assets(mode, raw_dir)
    bridged = {}
    for asset in assets:
        if asset["assetId"].endswith(".omft.near"):
            bridged.setdefault(asset["assetId"].rsplit(":", 1)[-1], []).append(asset)
    for account, group in bridged.items():
        rows.append(make_row(
            entity="NEAR Intents omft bridge", subject=account, chain="near",
            label=f"bridged token account for {group[0]['symbol']}", category="bridge",
            provenance="official_source", source=source, observed_at=fetched_at,
            note="1Click assets " + "; ".join(f"{a['assetId']} ({a['blockchain']})" for a in group)))

    for account, entity, label in DOCUMENTED_ACCOUNTS:
        try:
            view = client.view_account(account)
        except rpc.RpcError as error:
            if error.cause == "UNKNOWN_ACCOUNT":
                continue
            raise
        code = "no contract" if view["code_hash"] == rpc.EMPTY_CODE_HASH else "a contract"
        rows.append(make_row(
            entity=entity, subject=account, chain="near", label=label, category="infrastructure",
            provenance="onchain", observed_at=client.last_source["fetched_at"],
            source={"rpc": client.url, "method": "view_account", "args": {"account_id": account},
                    "block": client.last_block_height},
            note=f"role from docs, UNVERIFIED; view_account shows the account exists with {code}"))
    return rows


def append(rows, store_path=labels.STORE_PATH):
    """Append the rows whose id the store does not hold yet; returns how many were added."""
    store_path = Path(store_path)
    existing = {a.id for a in labels.load(store_path).assertions} if store_path.exists() else set()
    new = [row for row in rows if row["id"] not in existing]
    if new:
        with store_path.open("a", encoding="utf-8", newline="\n") as handle:
            for row in new:
                handle.write(labels.row_line(row) + "\n")
    return len(new)


def readme_text(store_path=labels.STORE_PATH, raw_dir=RAW_DIR):
    """The seeds README: every snapshot under raw/ that a row cites, with its sha256 and fetch time."""
    store = labels.load(store_path)
    cited = {}
    for assertion in store.assertions:
        source = assertion.source
        if "content_sha256" in source:
            key = (source["content_sha256"], source["url"], source["fetched_at"])
            cited[key] = cited.get(key, 0) + 1
    files = {hashlib.sha256(path.read_bytes()).hexdigest(): path.name
             for path in sorted(Path(raw_dir).iterdir()) if path.is_file()}
    lines = [
        "# Seed snapshots",
        "",
        ("Each file under `raw/` is the body a source returned, stored byte for byte and named with its "
         "fetch time in UTC. The label rows cite each snapshot by its full sha256, which is shortened "
         "here to its first and last four hex characters. `python -m intel seed <source>` rewrites this "
         "file, and `tests/test_labels.py` checks it against the snapshots and the rows."),
        "",
        "| Snapshot | Source | sha256 | Fetched (UTC) | Rows |",
        "|---|---|---|---|---|",
    ]
    for (digest, url, fetched_at), count in sorted(cited.items(), key=lambda item: item[0][2]):
        if digest not in files:
            raise SeedError(f"no snapshot under {raw_dir} has the sha256 {digest} that rows cite")
        lines.append(f"| `raw/{files[digest]}` | {url} | {digest[:4]}...{digest[-4:]} | {fetched_at} | {count} |")
    return "\n".join(lines) + "\n"


def run(source, mode=None, store_path=labels.STORE_PATH, raw_dir=RAW_DIR, readme_path=README_PATH):
    """Seed one source into the store and rewrite the README; returns the number of new rows."""
    rows = {"treasuries": treasuries, "catalog": catalog, "infrastructure": infrastructure}[source](
        mode=mode, raw_dir=raw_dir)
    added = append(rows, store_path)
    Path(readme_path).write_bytes(readme_text(store_path, raw_dir).encode("utf-8"))
    return added
