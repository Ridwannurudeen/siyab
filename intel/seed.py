"""Seed the label store from verified official sources and on chain reads.

Sub commands: treasuries (the NEAR Intents treasury page), catalog (the 1Click asset catalog),
infrastructure (omft.near and intents.near source metadata, the .omft.near bridge token accounts,
v1.signer and the OutLayer accounts), rhea (the rekt.news Rhea Finance report, re-read on chain,
which also writes intel/incidents/rhea.json), sweat-hot (the Fireblocks SWEAT and HOT report and the
DeFiLlama hacks entry, with the HOT token contract re-read on chain, which also writes
intel/incidents/sweat-hot.json), intents-2026-10-01 (the DeFiLlama hacks entry only, which writes
intel/incidents/intents-2026-10-01.json and adds no row) and skyward (the rekt.news Skyward Finance
report, with the account it names and the transaction it links re-read on chain).
fake-wallet reads the December 2021 NEAR forum report and confirms its named collection transfer.
kangaroo confirms the batch in its post-mortem; telegram-scam confirms only the reported direct recipient.
GATE_FIXTURES_MODE decides the network: replay (default) reads the newest snapshot under
intel/seeds/raw/ and the recorded RPC fixtures; record fetches the page once, stores it byte for byte
as raw/<name>-<UTC time>.<ext> and records RPC reads.
Each run appends only rows whose id is new and rewrites intel/seeds/README.md.
"""

import hashlib
import html
import json
import os
import re
import time
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from gate import rpc, rules
from intel import labels

INTEL_DIR = Path(__file__).resolve().parent
RAW_DIR = INTEL_DIR / "seeds" / "raw"
README_PATH = INTEL_DIR / "seeds" / "README.md"
TREASURY_URL = "https://docs.near-intents.org/security-compliance/treasury-addresses.md"
CATALOG_URL = "https://1click.chaindefuser.com/v0/tokens"
REKT_RHEA_URL = "https://rekt.news/rhea-finance-rekt"
QUOTE_DOC_URL = "https://docs.near-intents.org/api-reference/oneclick/request-a-swap-quote.md"
FIREBLOCKS_URL = "https://www.fireblocks.com/blog/near-zero-day-sweat-hot-token-exploit"
DEFILLAMA_URL = "https://api.llama.fi/hacks"
REKT_SKYWARD_URL = "https://rekt.news/skyward-rekt"
FAKE_WALLET_URL = "https://gov.near.org/t/11112.json"
KANGAROO_URL = "https://gov.near.org/t/25445.json"
TELEGRAM_SCAM_URL = "https://gov.near.org/t/21725.json"
SNAPSHOTS = {"treasuries": ("near-intents-treasury-addresses", "md", TREASURY_URL),
             "catalog": ("1click-tokens", "json", CATALOG_URL),
             "rhea": ("rekt-rhea", "html", REKT_RHEA_URL),
             "quote_doc": ("intents-quote-doc", "md", QUOTE_DOC_URL),
             "fireblocks": ("fireblocks-sweat-hot", "html", FIREBLOCKS_URL),
             "defillama": ("defillama-hacks", "json", DEFILLAMA_URL),
             "skyward": ("rekt-skyward", "html", REKT_SKYWARD_URL),
             "fake-wallet": ("l2-forum-11112", "json", FAKE_WALLET_URL),
             "kangaroo": ("l2-forum-25445", "json", KANGAROO_URL),
             "telegram-scam": ("l2-forum-21725", "json", TELEGRAM_SCAM_URL),
             "telegram-scam-context": ("l2-telegram-final-post", "json", "https://gov.near.org/t/21725/21.json"),
             "aurora-bounties": ("l2-aurora-bounties", "json", "https://forum.aurora.dev/t/1257.json"),
             "chainabuse": ("l2-chainabuse-home", "html", "https://www.chainabuse.com/"),
             "wallet-warning": ("l2-forum-17380", "json", "https://gov.near.org/t/17380.json"),
             "unsupported-report": ("l2-forum-17616", "json", "https://gov.near.org/t/17616.json"),
             "name-squatting": ("l2-forum-17750", "json", "https://gov.near.org/t/17750.json"),
             "wallet-phishing": ("l2-forum-18659", "json", "https://gov.near.org/t/18659.json"),
             "airdrop-scam": ("l2-forum-19675", "json", "https://gov.near.org/t/19675.json"),
             "fraudster-report": ("l2-forum-2733", "json", "https://gov.near.org/t/2733.json"),
             "exchange-report": ("l2-forum-35032", "json", "https://gov.near.org/t/35032.json"),
             "wallet-theft": ("l2-forum-38862", "json", "https://gov.near.org/t/38862.json"),
             "btc-theft": ("l2-forum-42360", "json", "https://gov.near.org/t/42360.json"),
             "discord-postmortem": ("l2-near-discord", "html",
                                    "https://www.near.org/blog/post-mortem-near-discord-security-incident"),
             "wallet-update": ("l2-near-wallet-update", "html",
                               "https://www.near.org/blog/near-web-wallet-security-update"),
             "nearblocks-openapi": ("l2-nearblocks-openapi", "json", "https://api.nearblocks.io/openapi.json"),
             "quill-rhea": ("l2-quill-rhea", "html", "https://www.quillaudits.com/blog/hack-analysis/rhea-finance-hack"),
             "ref-postmortem-link": ("l2-ref-week22", "json", "https://gov.near.org/t/5823.json"),
             "rekt-intents": ("l2-rekt-intents", "html", "https://rekt.news/near-intents-rekt"),
             "uk-sanctions": ("l2-uk-sanctions-list-page", "html",
                              "https://www.gov.uk/government/publications/the-uk-sanctions-list")}
SOURCES = ("treasuries", "catalog", "infrastructure", "rhea", "sweat-hot", "intents-2026-10-01", "skyward",
           "fake-wallet", "kangaroo", "telegram-scam")
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
        request = urllib.request.Request(url, headers={"User-Agent": "siyab"})
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


def _quoted(text):
    """A string from a source, rendered as quoted data rather than as our own words."""
    return json.dumps(text, ensure_ascii=False)


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
                                 note=f"EVM Chains table, listed for {_quoted(name)}", **common))
    for section, name in TREASURY_ROWS[2:7]:
        for entity, address in zip(("NEAR Intents", "HOT Bridge"), tables[(section, name)]):
            if address:
                rows.append(make_row(entity=entity, subject=address, chain=CHAIN_CODES[name],
                                     label=f"{entity} treasury", note=f"{section} table, row {_quoted(name)}",
                                     **common))
    for section, name in TREASURY_ROWS[7:]:
        rows.append(make_row(entity="NEAR Intents or HOT Bridge (the page does not say which)",
                             subject=tables[(section, name)][0], chain=CHAIN_CODES[name],
                             label="refill address", note=f"{section} table, row {_quoted(name)}; the page says "
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
    return assets, _document_source(CATALOG_URL, data, fetched_at), fetched_at


def catalog(mode=None, raw_dir=RAW_DIR):
    """One catalog_token assertion per asset in the 1Click catalog snapshot. A NEP-141 token on NEAR
    is keyed by its NEAR account on chain near; any other asset by its contract address (or its
    asset id when it has none) on its own blockchain."""
    assets, source, fetched_at = _catalog_assets(mode, raw_dir)
    rows = []
    for asset in assets:
        account = _near_token(asset["assetId"])
        subject, chain = ((account, "near") if account
                          else (asset.get("contractAddress") or asset["assetId"], asset["blockchain"]))
        note = (f"assetId {_quoted(asset['assetId'])}; symbol {_quoted(asset['symbol'])}; blockchain "
                f"{_quoted(asset['blockchain'])}; contractAddress {_quoted(asset.get('contractAddress'))}; "
                f"decimals {asset['decimals']}")
        rows.append(make_row(entity="NEAR Intents 1Click catalog", subject=subject, chain=chain,
                             label=f"symbol {_quoted(asset['symbol'])}", category="catalog_token",
                             provenance="official_source",
                             source=source, observed_at=fetched_at, note=note))
    return rows


def _account_code(view):
    """The local or global contract description, or None when neither is present."""
    if view.get("global_contract_hash"):
        return f"global contract with hash {view['global_contract_hash']}"
    if view.get("global_contract_account_id"):
        return f"global contract at {view['global_contract_account_id']}"
    if view["code_hash"] != rpc.EMPTY_CODE_HASH:
        return f"contract code hash {view['code_hash']}"
    return None


def infrastructure(mode=None, raw_dir=RAW_DIR, client=None):
    """omft.near and intents.near from their source metadata, every .omft.near catalog token
    account as a bridge account, and the documented accounts that view_account confirms."""
    client = client or rpc.Client(rpc.MAINNET_RPC_URL, _mode(mode))
    rows = []
    for account, label in (("intents.near", "NEAR Intents contract"),
                           ("omft.near", "parent account of the .omft.near bridge token accounts")):
        metadata = client.call_function(account, "contract_source_metadata", {})
        standards = ", ".join(_quoted(f"{s['standard']} {s['version']}") for s in metadata.get("standards", []))
        rows.append(make_row(
            entity="NEAR Intents", subject=account, chain="near", label=label, category="infrastructure",
            provenance="onchain", observed_at=client.last_source["fetched_at"],
            source={"rpc": client.url, "method": "contract_source_metadata", "args": {},
                    "block": client.last_block_height},
            note=f"contract_source_metadata: version {_quoted(metadata.get('version'))}, link "
                 f"{_quoted(metadata.get('link'))}, "
                 f"standards {standards}"))

    assets, source, fetched_at = _catalog_assets(mode, raw_dir)
    bridged = {}
    for asset in assets:
        if asset["assetId"].endswith(".omft.near"):
            bridged.setdefault(asset["assetId"].rsplit(":", 1)[-1], []).append(asset)
    for account, group in bridged.items():
        rows.append(make_row(
            entity="NEAR Intents omft bridge", subject=account, chain="near",
            label=f"bridged token account, symbol {_quoted(group[0]['symbol'])}", category="bridge",
            provenance="official_source", source=source, observed_at=fetched_at,
            note="1Click assets " + "; ".join(f"{_quoted(a['assetId'])} on {_quoted(a['blockchain'])}"
                                             for a in group)))

    for account, entity, label in DOCUMENTED_ACCOUNTS:
        try:
            view = client.view_account(account)
        except rpc.RpcError as error:
            if error.cause == "UNKNOWN_ACCOUNT":
                continue
            raise
        code = "a contract" if _account_code(view) else "no contract"
        rows.append(make_row(
            entity=entity, subject=account, chain="near", label=label, category="infrastructure",
            provenance="onchain", observed_at=client.last_source["fetched_at"],
            source={"rpc": client.url, "method": "view_account", "args": {"account_id": account},
                    "block": client.last_block_height},
            note=f"role from docs, UNVERIFIED; view_account shows the account exists with {code}"))
    return rows


RHEA_SOURCE_ID = "rekt-rhea"
RHEA_CONFIDENCE = 0.9
RHEA_INCIDENT_PATH = INTEL_DIR / "incidents" / "rhea.json"
RHEA_ATTACKER = "the unnamed party behind the Rhea Lend incident, as rekt.news reports it"
_HEX_ACCOUNT = re.compile(r"[0-9a-f]{64}")
_BASE58_HASH = re.compile(r"[1-9A-HJ-NP-Za-km-z]{43,44}")
_ETH_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_MULTICA = re.compile(r"rhea[0-9]+[.]multica[.]near")
_POOLS = re.compile(r"pools ([0-9]+) through ([0-9]+)")
# The page's headings for the accounts it lists: (role key, heading, how many accounts it names).
RHEA_LISTS = (("collector", "Collector account:", 1), ("master", "Master account:", 1),
              ("worker", "Worker accounts:", 5), ("token", "Fake Token Contracts:", 4))
# Each role as the report words it, in Siyab's text; the report's own sentence goes in the quote.
RHEA_ROLES = {
    "collector": "the collector account, which the report also prints as the attacker NEAR wallet",
    "master": "the master account",
    "worker": "one of the five worker accounts",
    "token": "one of the four token contracts that shared the code hash it names",
    "multica": ("an account within Rhea's own Multichain Account system that became active and later "
                "received funds traceable to the subject wallet"),
    "intents.near": "the account through which the subject wallet was funded within five minutes",
    "zec.omft.near": "the account whose ft_withdraw routed ZEC directly into Zcash Unified Addresses",
    "eth": "the attacker ETH wallet where the bridged USDC was deposited into Aave",
}
# The exit accounts, each with the words that find the sentence naming it in the report.
RHEA_EXITS = {"intents.near": "funded through intents.near", "zec.omft.near": "ft_withdraw on zec.omft.near"}
# The exit accounts keep the category of their existing official or on chain rows (treasury page and
# contract_source_metadata for intents.near, the catalog's .omft.near bridge row for zec.omft.near).
RHEA_EXIT_CATEGORIES = {"intents.near": "infrastructure", "zec.omft.near": "bridge"}
RHEA_ONCHAIN_NOTE = "current mainnet state read for the Rhea incident, not a historical block replay"
RHEA_STATE_BLOCK_HASH = "AnNmtmo3psRnQbAD2QNQzXZhADHD76oNsJrn3488bTyk"
RHEA_NOT_PUBLIC = (
    "The report names no person, and this page names none.",
    "The exploit transaction list beyond the transactions the report prints is not reproduced here.",
    "The 123 token contracts the report counts are not listed by it; only the four it names are here.",
    "Which party controls each account is not stated beyond the roles the report gives.",
)
RHEA_STANDARD = ("Only accounts named in the cited report or provable on chain by a read anyone can "
                 "repeat; no person, no motive; the report's own words appear only inside quote fields "
                 "with their source id; failed checks read Unknown.")


def _report_lines(data):
    """The article text of a rekt.news page, one paragraph per line."""
    text = data.decode("utf-8")
    start, end = text.find("<article"), text.find("</article>")
    if start == -1 or end == -1:
        raise SeedError("the report snapshot has no article element")
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", text[start:end], flags=re.DOTALL)
    body = re.sub(r"<br */?>|</p>|</li>|</h[1-6]>", "\n", body)
    body = html.unescape(re.sub(r"<[^>]+>", "", body))
    return [line.strip() for line in body.splitlines() if line.strip()]


def _after(lines, heading, pattern, count):
    """(the values pattern finds after the heading, the quoted text): the heading's own line, or the
    heading line and the next one when the values sit on the next line."""
    for number, line in enumerate(lines):
        if line.startswith(heading):
            found = pattern.findall(line[len(heading):])
            quote = line
            if not found and number + 1 < len(lines):
                found = pattern.findall(lines[number + 1])
                quote = f"{line} {lines[number + 1]}"
            if len(found) != count:
                raise SeedError(f"the report lists {len(found)} values after {heading!r}, not {count}")
            return found, quote
    raise SeedError(f"the report has no line {heading!r}")


def _line_with(lines, needle):
    for line in lines:
        if needle in line:
            return line
    raise SeedError(f"the report has no line naming {needle!r}")


def rhea_report(data):
    """What the rekt.news Rhea report names, read from the snapshot: the accounts in page order as
    (role key, subject, chain, quote), the shared code hash, the Ref pool ids, each with its quote."""
    lines = _report_lines(data)
    accounts = []
    for role, heading, count in RHEA_LISTS:
        found, quote = _after(lines, heading, _HEX_ACCOUNT, count)
        accounts += [(role, subject, "near", quote) for subject in found]
    multica_line = _line_with(lines, "multica.near")
    accounts += [("multica", subject, "near", multica_line) for subject in _MULTICA.findall(multica_line)]
    for exit_account, needle in RHEA_EXITS.items():
        accounts.append((exit_account, exit_account, "near", _line_with(lines, needle)))
    [eth], eth_quote = _after(lines, "Attacker ETH wallet:", _ETH_ADDRESS, 1)
    accounts.append(("eth", eth, "eth", eth_quote))
    if accounts[0][1] not in _after(lines, "Attacker NEAR wallet:", _HEX_ACCOUNT, 1)[0]:
        raise SeedError("the collector account is not the attacker NEAR wallet the report prints")
    hash_line = _line_with(lines, "shared the same code hash:")
    [code_hash] = _BASE58_HASH.findall(hash_line.split("code hash:", 1)[1])
    pools_line = _line_with(lines, "pools 8514")
    first, last = (int(n) for n in _POOLS.search(pools_line).groups())
    return {"accounts": accounts, "code_hash": (code_hash, hash_line),
            "pools": (list(range(first, last + 1)), pools_line)}


def _result_sha256(value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _error_block(error):
    """The block height an RPC error reports in its cause info, or None."""
    cause = error.error.get("cause") if isinstance(error.error, dict) else None
    info = cause.get("info") if isinstance(cause, dict) else None
    return info.get("block_height") if isinstance(info, dict) else None


def _rhea_reads(client, nearblocks, report):
    """The on chain facts: [(subject, label, rpc, method, args, block, observed_at, result_sha256, kind)]."""
    code_hash = report["code_hash"][0]
    facts = []
    for role, subject, chain, _ in report["accounts"]:
        if chain != "near":
            continue
        try:
            view = client.view_account(subject)
            hash_now = view["code_hash"]
            code = _account_code(view)
            if view.get("global_contract_hash") or view.get("global_contract_account_id"):
                label = f"account runs the {code}"
            elif role == "token":
                label = ("code hash equals the hash named in the Rhea report" if hash_now == code_hash
                         else f"code hash {hash_now}, not the hash named in the Rhea report")
            elif code is None:
                label = "account exists with no contract code"
            else:
                label = f"account exists with {code}"
            block, result = client.last_block_height, view
            kind = "hash_equal" if role == "token" and hash_now == code_hash else "view"
        except rpc.RpcError as error:
            if error.cause != "UNKNOWN_ACCOUNT" or _error_block(error) is None:
                raise
            label, block, result, kind = ("account does not exist at this block (deleted or never created)",
                                          _error_block(error), error.error, "view")
        facts.append((subject, label, client.url, "view_account", {"account_id": subject}, block,
                      client.last_source["fetched_at"], _result_sha256(result), kind))
        if role != "token":
            continue
        for method in ("ft_metadata", "contract_source_metadata"):
            try:
                answer = client.call_function(subject, method, {})
                label = f"{method} answers {_quoted(json.dumps(answer, sort_keys=True))}"
                block, result, kind = client.last_block_height, answer, "answer"
            except rpc.RpcError as error:
                # A contract execution failure comes back inside the result, which carries the block.
                block = _error_block(error) or client.last_block_height
                missing = (error.cause in rules.MISSING_CODE_CAUSES
                           or any(marker in json.dumps(error.error) for marker in rules.MISSING_METHOD_MARKERS))
                if missing:
                    label = f"no {method}"
                elif isinstance(error.error, str):
                    label = f"{method} fails on the contract: {_quoted(error.error)}"
                else:
                    raise
                if block is None:
                    raise
                result = error.error
                kind = "ft_metadata_fails" if method == "ft_metadata" else "answer"
            facts.append((subject, label, client.url, method, {}, block, client.last_source["fetched_at"],
                          _result_sha256(result), kind))
    for role, subject, _, _ in report["accounts"]:
        if role not in ("collector", "worker"):
            continue
        try:
            keys = nearblocks.get(f"/v1/account/{subject}/keys")
            created = sorted((int(k["created"]["block_timestamp"]), k["created"]["transaction_hash"])
                             for k in keys.get("keys", [])
                             if isinstance(k.get("created"), dict) and k["created"].get("block_timestamp"))
            if not created:
                continue
            transaction_hash = created[0][1]
            txn = nearblocks.get(f"/v1/txns/{transaction_hash}")["txns"][0]
        except (rpc.FixtureMiss, rpc.HttpError, rpc.RateLimited, rpc.TransportError, KeyError, IndexError):
            continue
        block = int(txn["block"]["block_height"])
        deposit = Decimal(str(txn["actions_agg"]["deposit"]))
        near = format((deposit / rules.YOCTO_PER_NEAR).normalize(), "f")
        label = f"created at block {block} by transaction {transaction_hash} with a deposit of {near} NEAR"
        facts.append((subject, label, nearblocks.url, "GET /v1/account/{account_id}/keys and /v1/txns/{transaction_hash}",
                      {"account_id": subject, "transaction_hash": transaction_hash}, block,
                      nearblocks.last_source["fetched_at"], _result_sha256(txn), "created"))
    return facts


def _exit_note(store, subject, category):
    """Where an exit account's category comes from: its first official or on chain row in the store
    with that category that this seed did not write, cited by id and label."""
    for assertion in store.by_subject(subject, "near"):
        if (assertion.category == category and assertion.superseded_by is None
                and assertion.provenance in ("official_source", "onchain") and assertion.note != RHEA_ONCHAIN_NOTE):
            return (f"the category comes from the existing row {assertion.id}, labelled {_quoted(assertion.label)}, "
                    "not from the report")
    raise SeedError(f"no official or on chain {category} row for {subject} in the store")


def _rhea_rows(mode, raw_dir, client, nearblocks, store_path=labels.STORE_PATH):
    """(rows, report, facts, snapshot source) for the Rhea incident."""
    store = labels.load(store_path)
    data, fetched_at, _ = snapshot("rhea", mode, raw_dir)
    report = rhea_report(data)
    report_source = _document_source(REKT_RHEA_URL, data, fetched_at)
    rows, category = [], {}
    for role, subject, chain, quote in report["accounts"]:
        category[subject] = RHEA_EXIT_CATEGORIES.get(role, "incident")
        entity = ("NEAR Intents" if role in RHEA_EXITS else "Rhea Finance Multichain Account system"
                  if role == "multica" else RHEA_ATTACKER)
        rows.append(make_row(entity=entity, subject=subject, chain=chain,
                             label=f"named in the rekt.news Rhea Finance report as {RHEA_ROLES[role]}",
                             category=category[subject], provenance="third_party", source=report_source,
                             observed_at=fetched_at, confidence=RHEA_CONFIDENCE,
                             note=f"quote {_quoted(quote)}"
                             + (f"; {_exit_note(store, subject, category[subject])}" if role in RHEA_EXITS else "")))
    entities = {row["subject"]: row["entity"] for row in rows}
    facts = _rhea_reads(client, nearblocks, report)
    by_kind = {}
    for subject, label, url, method, args, block, observed_at, _, kind in facts:
        source = {"rpc": url, "method": method, "args": args, "block": block}
        # Archived no-code assertions keep their ids; the dated readings supersede them.
        for assertion in store.by_subject(subject, "near"):
            if (assertion.provenance == "onchain" and assertion.note == RHEA_ONCHAIN_NOTE
                    and assertion.label == "account exists with no contract code"
                    and assertion.source == source and assertion.observed_at == observed_at):
                label = assertion.label
                break
        row = make_row(entity=entities[subject], subject=subject, chain="near", label=label,
                       category=category[subject], provenance="onchain", observed_at=observed_at,
                       source=source,
                       note=RHEA_ONCHAIN_NOTE)
        rows.append(row)
        by_kind.setdefault((subject, kind), row)
    for role, subject, _, _ in report["accounts"]:
        inputs = [by_kind.get((subject, "hash_equal")), by_kind.get((subject, "ft_metadata_fails"))]
        if role != "token" or None in inputs:
            continue
        rows.append(make_row(
            entity=entities[subject], subject=subject, chain="near",
            label=("token contract whose ft_metadata call fails and whose code hash equals the hash named in the "
                   "Rhea report"),
            category="denylist", provenance="inferred", rule_id="fake_token_shared_hash",
            source={"rule_id": "fake_token_shared_hash", "inputs": [r["id"] for r in inputs]},
            observed_at=max(r["observed_at"] for r in inputs), confidence=RHEA_CONFIDENCE,
            note="derived from the two onchain assertions in source.inputs"))
    return rows, report, facts, report_source


def _clients(mode, client, nearblocks):
    return (client or rpc.Client(rpc.MAINNET_RPC_URL, _mode(mode)),
            nearblocks or rpc.Client(rpc.NEARBLOCKS_URL, _mode(mode), kind="http"))


def rhea(mode=None, raw_dir=RAW_DIR, client=None, nearblocks=None, *, include_state=False):
    """The Rhea Lend incident rows: one third_party row per account the rekt.news report names, one
    onchain row per fact re-read on chain, and one inferred denylist row per token contract whose
    code hash equals the reported hash and which has no ft_metadata. include_state appends the
    dated state readings that supersede the earlier no-code rows."""
    client, nearblocks = _clients(mode, client, nearblocks)
    rows = _rhea_rows(mode, raw_dir, client, nearblocks)[0]
    state_rows = rhea_state_rows(mode, raw_dir, client, nearblocks, rows=rows)
    replacements = {row["subject"]: row["id"] for row in state_rows}
    for row in rows:
        if row["provenance"] == "onchain" and row["label"] == "account exists with no contract code":
            row["superseded_by"] = replacements[row["subject"]]
    return rows + state_rows if include_state else rows


def rhea_state_rows(mode=None, raw_dir=RAW_DIR, client=None, nearblocks=None, *, rows=None):
    """Dated state readings for the Rhea accounts previously read as holding no code."""
    client, nearblocks = _clients(mode, client, nearblocks)
    if rows is None:
        rows = _rhea_rows(mode, raw_dir, client, nearblocks)[0]
    readings = []
    for row in rows:
        if row["provenance"] != "onchain" or row["label"] != "account exists with no contract code":
            continue
        subject = row["subject"]
        view = client.view_account_at(subject, RHEA_STATE_BLOCK_HASH)
        block, code_hash = view["block_height"], view["code_hash"]
        args = {"account_id": subject, "block_id": RHEA_STATE_BLOCK_HASH}
        if view.get("global_contract_hash"):
            state = f"runs the global contract with hash {view['global_contract_hash']}"
        elif view.get("global_contract_account_id"):
            state = f"runs the global contract at {view['global_contract_account_id']}"
        else:
            state = ("holds no contract code and no global contract" if code_hash == rpc.EMPTY_CODE_HASH
                     else f"holds contract code with code hash {code_hash}")
        readings.append(make_row(
            entity=row["entity"], subject=subject, chain="near", label=f"at block {block} the account {state}",
            category="watch", provenance="onchain", observed_at=client.last_source["fetched_at"],
            source={"rpc": client.url, "method": "view_account", "args": args, "block": block},
            note="mainnet state observed at the cited block; incident role remains sourced to the report"))
    return readings


def rhea_incident(mode=None, raw_dir=RAW_DIR, store_path=labels.STORE_PATH, client=None, nearblocks=None):
    """The incident document for intel/incidents/rhea.json; rules_today runs gate.rules.judge in replay
    over the committed fixtures and the store at store_path."""
    reports_account = os.environ.get("REPORTS_ACCOUNT_ID")
    rows, report, facts, source = _rhea_rows(mode, raw_dir, *_clients(mode, client, nearblocks), store_path)
    ids = {}
    for row in rows:
        ids.setdefault((row["subject"], row["chain"]), []).append(row["id"])
    accounts = [{"subject": subject, "chain": chain, "role_as_reported": RHEA_ROLES[role],
                 "quote": {"source": RHEA_SOURCE_ID, "text": quote}, "assertion_ids": ids[(subject, chain)]}
                for role, subject, chain, quote in report["accounts"]]
    onchain_facts = [{"subject": subject, "method": method, "args": args, "block": block,
                      "result_sha256": digest, "summary": label}
                     for subject, label, _, method, args, block, _, digest, _ in facts]
    store = labels.load(store_path)
    replay = (rpc.Client(rpc.MAINNET_RPC_URL, "replay"), rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http"))
    now_ms = int(datetime.strptime(max(f[6] for f in facts), _TIME).replace(tzinfo=UTC).timestamp() * 1000)
    rules_today = []
    for role, subject, chain, _ in report["accounts"]:
        if chain != "near":
            continue
        if role == "token":
            request = {"kind": "ft", "token_id": subject, "receiver_id": "intents.near", "amount": "1000000",
                       "memo": None}
        else:
            request = {"kind": "near", "token_id": None, "receiver_id": subject,
                       "amount": str(rules.YOCTO_PER_NEAR), "memo": None}
        judgement = rules.judge(request, {"allowlist": [], "caps": []}, store, *replay, now_ms=now_ms,
                                injection_scanner=None, issuers=rules.issuers_for("mainnet"),
                                reports_account=reports_account)
        rules_today.append({"subject": subject, "as": "token" if role == "token" else "receiver",
                            "request": request, "verdict": judgement.verdict, "checks": judgement.checks,
                            "reasons": judgement.reasons, "labels": judgement.labels})
    code_hash, hash_quote = report["code_hash"]
    pools, pools_quote = report["pools"]
    return {
        "id": "rhea",
        "title": "Rhea Lend margin trading route exploit",
        "date": "2026-04-16",
        "chains": ["near", "eth"],
        "sources": [{"id": RHEA_SOURCE_ID, "publisher": "rekt.news", **source}],
        "accounts": accounts,
        "code_hash_as_reported": {"value": code_hash, "quote": {"source": RHEA_SOURCE_ID, "text": hash_quote}},
        "ref_pools_as_reported": {"ids": pools, "quote": {"source": RHEA_SOURCE_ID, "text": pools_quote}},
        "onchain_facts": onchain_facts,
        "route_subjects": list(RHEA_EXITS),
        "onchain_state": "current state of the contracts, not a historical block replay",
        "rules_today": rules_today,
        "rules_today_at_ms": now_ms,
        "not_public": list(RHEA_NOT_PUBLIC),
        "standard": RHEA_STANDARD,
    }


FIREBLOCKS_SOURCE_ID = "fireblocks"
DEFILLAMA_SOURCE_ID = "defillama"
INCIDENT_CONFIDENCE = 0.9
INCIDENTS_DIR = INTEL_DIR / "incidents"
SWEAT_HOT_INCIDENT_PATH = INCIDENTS_DIR / "sweat-hot.json"
INTENTS_INCIDENT_PATH = INCIDENTS_DIR / "intents-2026-10-01.json"
_NEARBLOCKS_LINK = re.compile(r'<p[^>]*>(?:(?!</p>).)*?href="https://nearblocks[.]io/(token|txns)/([^"]+)"(?:(?!</p>).)*?</p>',
                              re.DOTALL)
CURRENT_STATE = "current state of the contract, not a historical block replay"
SWEAT_HOT_ROLE = ("the HOT token contract, in which the report detected the same unguarded ft_resolve_transfer "
                  "callback, patched the same day")
SWEAT_HOT_NOT_PUBLIC = (
    ("The report names no SWEAT attacker account, no victim account and no SWEAT exploit transaction; this page "
    "names none."),
    "The report names no person, and this page names none.",
    "The only account the report names is the HOT token contract, and the only transaction is the HOT patch.",
    "The SWEAT token contract is not named by the report; its catalog row is not cited here.",
)
INTENTS_NOT_PUBLIC = (
    ("No NEAR account and no EVM address appears in the source this page cites, so no address is asserted as "
    "drained."),
    ("The NEAR Intents treasury page lists the shared EVM treasury, but no cited source says which address was "
    "drained."),
    ("The Block's report of 1 Oct 2026 answered HTTP 403 to the one fetch made on 29 Oct 2026; nothing from it is "
    "used here."),
    "The Bitquery investigation page was fetched on 29 Oct 2026: fetched, no addresses.",
    "The report names no person, and this page names none.",
)
INCIDENT_STANDARD = ("Only accounts named in the cited sources or provable on chain by a read anyone can repeat; "
                     "no person, no motive; the sources' own words appear only inside quote fields with their "
                     "source id; failed checks read Unknown.")


def _paragraph_text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def fireblocks_report(data):
    """What the Fireblocks report names: [(kind, value, quote)] for each nearblocks.io token and
    transaction link in its article, in page order."""
    text = data.decode("utf-8")
    start, end = text.find("<article"), text.find("</article>")
    if start == -1 or end == -1:
        raise SeedError("the Fireblocks snapshot has no article element")
    found = [(kind, value, _paragraph_text(match.group(0))) for match in _NEARBLOCKS_LINK.finditer(text[start:end])
             for kind, value in [match.groups()]]
    kinds = [kind for kind, _, _ in found]
    if kinds.count("token") != 1 or kinds.count("txns") != 1:
        raise SeedError(f"the Fireblocks report links {kinds}, not one token and one transaction")
    return found


def defillama_entry(data, name):
    """(the DeFiLlama hacks entry for the name, its compact JSON as the quote)."""
    entries = [entry for entry in json.loads(data) if entry.get("name") == name]
    if len(entries) != 1:
        raise SeedError(f"the DeFiLlama snapshot holds {len(entries)} entries named {name!r}")
    return entries[0], json.dumps(entries[0], sort_keys=True, ensure_ascii=False)


def _entry_date(entry):
    return datetime.fromtimestamp(entry["date"], UTC).strftime("%Y-%m-%d")


def _sweat_hot_reads(client, nearblocks, token, transaction_hash):
    """The on chain facts: [(label, rpc, method, args, block, observed_at, result_sha256)]."""
    facts = []
    view = client.view_account(token)
    facts.append((f"account exists with contract code hash {view['code_hash']}", client.url, "view_account",
                  {"account_id": token}, client.last_block_height, client.last_source["fetched_at"],
                  _result_sha256(view)))
    try:
        metadata = client.call_function(token, "contract_source_metadata", {})
        label = f"contract_source_metadata answers {_quoted(json.dumps(metadata, sort_keys=True))}"
        block, result = client.last_block_height, metadata
    except rpc.RpcError as error:
        block = _error_block(error) or client.last_block_height
        if block is None:
            raise
        missing = (error.cause in rules.MISSING_CODE_CAUSES
                   or any(marker in json.dumps(error.error) for marker in rules.MISSING_METHOD_MARKERS))
        label = ("no contract_source_metadata" if missing
                 else f"contract_source_metadata fails on the contract: {_quoted(json.dumps(error.error, sort_keys=True))}")
        result = error.error
    facts.append((label, client.url, "contract_source_metadata", {}, block, client.last_source["fetched_at"],
                  _result_sha256(result)))
    txn = nearblocks.get(f"/v1/txns/{transaction_hash}")["txns"][0]
    actions = ", ".join(_quoted(action["action"]) for action in txn["actions"])
    facts.append(((f"transaction {transaction_hash} at block {txn['block']['block_height']}, signer "
                  f"{_quoted(txn['signer_account_id'])}, receiver {_quoted(txn['receiver_account_id'])}, actions "
                  f"{actions}"), nearblocks.url, "GET /v1/txns/{transaction_hash}", {"transaction_hash": transaction_hash},
                  int(txn["block"]["block_height"]), nearblocks.last_source["fetched_at"], _result_sha256(txn)))
    return facts


def _sweat_hot_parts(mode, raw_dir, client, nearblocks):
    """(rows, report links, facts, the two sources with their ids, the DeFiLlama entry and quote)."""
    data, fetched_at, _ = snapshot("fireblocks", mode, raw_dir)
    links = fireblocks_report(data)
    fireblocks = _document_source(FIREBLOCKS_URL, data, fetched_at)
    llama_data, llama_at, _ = snapshot("defillama", mode, raw_dir)
    entry, entry_quote = defillama_entry(llama_data, "Sweat Foundation")
    [(_, token, token_quote)] = [link for link in links if link[0] == "token"]
    [(_, transaction_hash, _)] = [link for link in links if link[0] == "txns"]
    entity = "HOT token contract, as the Fireblocks report names it"
    rows = [make_row(entity=entity, subject=token, chain="near",
                     label=f"named in the Fireblocks SWEAT and HOT report as {SWEAT_HOT_ROLE}", category="watch",
                     provenance="third_party", source=fireblocks, observed_at=fetched_at,
                     confidence=INCIDENT_CONFIDENCE, note=f"quote {_quoted(token_quote)}")]
    facts = _sweat_hot_reads(client, nearblocks, token, transaction_hash)
    for label, url, method, args, block, observed_at, _ in facts:
        rows.append(make_row(entity=entity, subject=token, chain="near", label=label, category="watch",
                             provenance="onchain", source={"rpc": url, "method": method, "args": args, "block": block},
                             observed_at=observed_at, note=CURRENT_STATE))
    sources = [{"id": FIREBLOCKS_SOURCE_ID, "publisher": "Fireblocks", **fireblocks},
               {"id": DEFILLAMA_SOURCE_ID, "publisher": "DeFiLlama hacks API",
                **_document_source(DEFILLAMA_URL, llama_data, llama_at)}]
    return rows, links, facts, sources, (entry, entry_quote)


def sweat_hot(mode=None, raw_dir=RAW_DIR, client=None, nearblocks=None):
    """The SWEAT and HOT rows: one third_party row for the HOT token contract the Fireblocks report
    names, and one onchain row per fact re-read for it (category watch, not a refusal category: the
    report says the contract was patched)."""
    return _sweat_hot_parts(mode, raw_dir, *_clients(mode, client, nearblocks))[0]


def _rules_today(subject, store, now_ms, reports_account=None):
    """gate.rules.judge in replay for a token subject, as the token of a transfer to intents.near."""
    replay = (rpc.Client(rpc.MAINNET_RPC_URL, "replay"), rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http"))
    request = {"kind": "ft", "token_id": subject, "receiver_id": "intents.near", "amount": "1000000", "memo": None}
    judgement = rules.judge(request, {"allowlist": [], "caps": []}, store, *replay, now_ms=now_ms,
                            injection_scanner=None, issuers=rules.issuers_for("mainnet"),
                            reports_account=reports_account)
    return {"subject": subject, "as": "token", "request": request, "verdict": judgement.verdict,
            "checks": judgement.checks, "reasons": judgement.reasons, "labels": judgement.labels}


def sweat_hot_incident(mode=None, raw_dir=RAW_DIR, store_path=labels.STORE_PATH, client=None, nearblocks=None):
    """The incident document for intel/incidents/sweat-hot.json."""
    reports_account = os.environ.get("REPORTS_ACCOUNT_ID")
    client, nearblocks = _clients(mode, client, nearblocks)
    rows, links, facts, sources, (entry, entry_quote) = _sweat_hot_parts(mode, raw_dir, client, nearblocks)
    [(_, token, token_quote)] = [link for link in links if link[0] == "token"]
    [(_, transaction_hash, patch_quote)] = [link for link in links if link[0] == "txns"]
    if client.mode == "record":
        # Record the view calls the rules make for the token; NearBlocks is not spent here.
        rules.judge({"kind": "ft", "token_id": token, "receiver_id": "intents.near", "amount": "1000000",
                     "memo": None}, {"allowlist": [], "caps": []}, labels.load(store_path), client,
                    rpc.Client(rpc.NEARBLOCKS_URL, "replay", kind="http"), now_ms=0, injection_scanner=None,
                    issuers=rules.issuers_for("mainnet"), reports_account=reports_account)
    now_ms = int(datetime.strptime(max(f[5] for f in facts), _TIME).replace(tzinfo=UTC).timestamp() * 1000)
    return {
        "id": "sweat-hot",
        "title": "SWEAT token ft_resolve_transfer exploit and the HOT token patch",
        "date": _entry_date(entry),
        "chains": ["near"],
        "sources": sources,
        "accounts": [{"subject": token, "chain": "near", "role_as_reported": SWEAT_HOT_ROLE,
                      "quote": {"source": FIREBLOCKS_SOURCE_ID, "text": token_quote},
                      "assertion_ids": [row["id"] for row in rows]}],
        "patch_transaction_as_reported": {"value": transaction_hash,
                                          "quote": {"source": FIREBLOCKS_SOURCE_ID, "text": patch_quote}},
        "loss_as_reported": {"value": f"{entry['amount']} USD on {_entry_date(entry)}",
                             "quote": {"source": DEFILLAMA_SOURCE_ID, "text": entry_quote}},
        "onchain_facts": [{"subject": token, "method": method, "args": args, "block": block, "result_sha256": digest,
                           "summary": label} for label, _, method, args, block, _, digest in facts],
        "onchain_state": CURRENT_STATE,
        "rules_today": [_rules_today(token, labels.load(store_path), now_ms, reports_account)],
        "rules_today_at_ms": now_ms,
        "not_public": list(SWEAT_HOT_NOT_PUBLIC),
        "standard": INCIDENT_STANDARD,
    }


def intents_incident(mode=None, raw_dir=RAW_DIR):
    """The incident document for intel/incidents/intents-2026-10-01.json: the DeFiLlama entry only,
    since no cited source names an address."""
    data, fetched_at, _ = snapshot("defillama", mode, raw_dir)
    entry, entry_quote = defillama_entry(data, "NEAR Intents")
    return {
        "id": "intents-2026-10-01",
        "title": "NEAR Intents exploit",
        "date": _entry_date(entry),
        "chains": [chain.lower() for chain in entry["chain"]],
        "sources": [{"id": DEFILLAMA_SOURCE_ID, "publisher": "DeFiLlama hacks API",
                     **_document_source(DEFILLAMA_URL, data, fetched_at)}],
        "accounts": [],
        "loss_as_reported": {"value": f"{entry['amount']} USD on {_entry_date(entry)}",
                             "quote": {"source": DEFILLAMA_SOURCE_ID, "text": entry_quote}},
        "onchain_facts": [],
        "rules_today": [],
        "not_public": list(INTENTS_NOT_PUBLIC),
        "standard": INCIDENT_STANDARD,
    }

SKYWARD_ENTITY = "the unnamed party behind the Skyward Finance treasury drain, as rekt.news reports it"
SKYWARD_ROLE = "the one account whose address it prints, together with the one transaction it links"
SKYWARD_ONCHAIN_NOTE = "current mainnet state read for the Skyward Finance incident, not a historical block replay"
SKYWARD_TX_NOTE = "the transaction the report links, as the NearBlocks transaction API returns it"
_EXPLORER_TX_LINK = re.compile(r'href="https://explorer[.]near[.]org/transactions/([1-9A-HJ-NP-Za-km-z]{43,44})"')


def skyward_report(data):
    """What the rekt.news Skyward Finance report names: (the account, the quote of its line and the next
    one, which prints the transaction, and the transaction hash from the link the report gives)."""
    lines = _report_lines(data)
    [account], account_quote = _after(lines, "Exploiter’s address:", _HEX_ACCOUNT, 1)
    text = data.decode("utf-8")
    article = text[text.find("<article"): text.find("</article>")]
    hashes = sorted(set(_EXPLORER_TX_LINK.findall(article)))
    if len(hashes) != 1:
        raise SeedError(f"the Skyward report links {len(hashes)} transactions, not one")
    tx_line = lines[lines.index(account_quote) + 1]
    printed = tx_line.split(":", 1)[1].strip().rstrip("…") if tx_line.startswith("Attack tx:") else None
    if not printed or not hashes[0].startswith(printed):
        raise SeedError("the line after the account is not the linked transaction the report prints")
    return account, f"{account_quote} {tx_line}", hashes[0]


def skyward(mode=None, raw_dir=RAW_DIR, client=None, nearblocks=None):
    """The Skyward Finance rows: one third_party incident row for the account the rekt.news report
    names, one onchain row for its current view_account and one for the attack transaction the
    report links, whose signer must be that account."""
    client, nearblocks = _clients(mode, client, nearblocks)
    data, fetched_at, _ = snapshot("skyward", mode, raw_dir)
    account, quote, transaction_hash = skyward_report(data)
    rows = [make_row(entity=SKYWARD_ENTITY, subject=account, chain="near",
                     label=f"named in the rekt.news Skyward Finance report as {SKYWARD_ROLE}", category="incident",
                     provenance="third_party", source=_document_source(REKT_SKYWARD_URL, data, fetched_at),
                     observed_at=fetched_at, confidence=INCIDENT_CONFIDENCE,
                     note=f"quote {_quoted(quote)}")]
    try:
        view = client.view_account(account)
        code = _account_code(view)
        if view.get("global_contract_hash") or view.get("global_contract_account_id"):
            label = f"account runs the {code}"
        else:
            label = "account exists with no contract code" if code is None else f"account exists with {code}"
        block = client.last_block_height
    except rpc.RpcError as error:
        if error.cause != "UNKNOWN_ACCOUNT" or _error_block(error) is None:
            raise
        label, block = "account does not exist at this block (deleted or never created)", _error_block(error)
    rows.append(make_row(entity=SKYWARD_ENTITY, subject=account, chain="near", label=label, category="incident",
                         provenance="onchain", observed_at=client.last_source["fetched_at"],
                         source={"rpc": client.url, "method": "view_account", "args": {"account_id": account},
                                 "block": block}, note=SKYWARD_ONCHAIN_NOTE))
    txns = nearblocks.get(f"/v1/txns/{transaction_hash}")["txns"]
    if len(txns) != 1:
        raise SeedError(f"the transaction lookup for the Skyward report returned {len(txns)} transactions, not one")
    txn = txns[0]
    if txn["signer_account_id"] != account:
        raise SeedError("the attack transaction the report links is not signed by the account it names")
    # The action kinds are left out: the upper case function call kind reads as a tool call to the
    # injection scanner; the method names say the same and are what the report describes.
    methods = ", ".join(_quoted(action["method"]) for action in txn["actions"] if action.get("method"))
    if not methods:
        raise SeedError("the attack transaction the report links calls no method")
    rows.append(make_row(
        entity=SKYWARD_ENTITY, subject=account, chain="near",
        label=(f"signer of transaction {transaction_hash} at block {txn['block']['block_height']}, receiver "
               f"{_quoted(txn['receiver_account_id'])}, methods called {methods}"),
        category="incident", provenance="onchain", observed_at=nearblocks.last_source["fetched_at"],
        source={"rpc": nearblocks.url, "method": "GET /v1/txns/{transaction_hash}",
                "args": {"transaction_hash": transaction_hash}, "block": int(txn["block"]["block_height"])},
        note=SKYWARD_TX_NOTE))
    return rows


def fake_wallet(mode=None, raw_dir=RAW_DIR, nearblocks=None):
    """The reported fake wallet collection account, only after its cited transfer is confirmed."""
    data, fetched_at, _ = snapshot("fake-wallet", mode, raw_dir)
    posts = {post["post_number"]: post["cooked"] for post in json.loads(data)["post_stream"]["posts"]}
    paragraphs = [_paragraph_text(p) for p in re.findall(r"<p>.*?</p>", posts[1], re.DOTALL)]
    quotes = [p for p in paragraphs if p.startswith("This fake wallet request for pass phrase")]
    accounts = re.findall(r"account called ([a-z0-9_.-]+[.]near)", " ".join(quotes))
    hashes = sorted(set(_EXPLORER_TX_LINK.findall(posts[1])))
    victims = re.findall(r"wallet used to stole the tokens from ([a-z0-9_.-]+[.]near)",
                         _paragraph_text(posts[5]))
    if len(quotes) != 1 or len(accounts) != 1 or len(hashes) != 1 or len(victims) != 1:
        raise SeedError("the fake wallet report must name one collection account, transfer and sending account")
    [account], [transaction_hash], [victim] = accounts, hashes, victims
    if account + " this is that wallet" not in _paragraph_text(posts[5]):
        raise SeedError("the two fake wallet reports name different collection accounts")
    nearblocks = nearblocks or rpc.Client(rpc.NEARBLOCKS_URL, _mode(mode), kind="http")
    txns = nearblocks.get(f"/v1/txns/{transaction_hash}")["txns"]
    if len(txns) != 1:
        raise SeedError("the fake wallet transfer lookup must return exactly one transaction")
    txn = txns[0]
    if (txn["transaction_hash"] != transaction_hash or txn["signer_account_id"] != victim
            or txn["receiver_account_id"] != account or txn["outcomes"]["status"] is not True
            or txn["actions"] != [{"action": "TRANSFER", "method": None, "args": None}]
            or int(txn["block"]["block_height"]) < 1):
        raise SeedError("the cited transaction does not confirm the reported collection transfer")
    common = {"entity": "the collection account reported in the December 2021 fake NEAR Wallet thread",
              "subject": account, "chain": "near", "category": "incident", "confidence": 0.7,
              "note": f"quote {_quoted(quotes[0])}"}
    rows = [make_row(label="reported collection account for the fake NEAR Wallet app",
                     provenance="third_party", source=_document_source(FAKE_WALLET_URL, data, fetched_at),
                     observed_at=fetched_at, **common),
            make_row(label=(f"receiver of the reported transfer {transaction_hash} from {_quoted(victim)} "
                            f"at block {txn['block']['block_height']}"),
                     provenance="onchain", observed_at=nearblocks.last_source["fetched_at"],
                     source={"rpc": nearblocks.url, "method": "GET /v1/txns/{transaction_hash}",
                             "args": {"transaction_hash": transaction_hash},
                             "block": int(txn["block"]["block_height"])}, **common)]
    replacement = dict(rows[0], label="reported collection account in the NEAR Wallet impersonation report")
    replacement["id"] = labels.assertion_id(replacement)
    labels.validate(replacement)
    rows[0]["superseded_by"] = replacement["id"]
    rows.append(replacement)
    for row in rows[1:]:
        replacement = dict(row, entity="the collection account reported in the December 2021 NEAR Wallet impersonation thread")
        replacement["id"] = labels.assertion_id(replacement)
        labels.validate(replacement)
        row["superseded_by"] = replacement["id"]
        rows.append(replacement)
    return rows


def kangaroo(mode=None, raw_dir=RAW_DIR, nearblocks=None):
    """The account named by the Kangaroo post-mortem, confirmed against its example batch."""
    data, fetched_at, _ = snapshot("kangaroo", mode, raw_dir)
    post = json.loads(data)["post_stream"]["posts"][0]
    paragraphs = [_paragraph_text(p) for p in re.findall(r"<p>.*?</p>", post["cooked"], re.DOTALL)]
    quotes = [p for p in paragraphs if p.startswith("Link to the NEAR address of the attacker")]
    accounts = sorted(set(_HEX_ACCOUNT.findall(" ".join(quotes))))
    batches = [_paragraph_text(p) for p in re.findall(r"<pre><code[^>]*>.*?</code></pre>",
                                                      post["cooked"], re.DOTALL)
               if "Batch Transaction" in p]
    if len(quotes) != 1 or len(accounts) != 1 or len(batches) != 1:
        raise SeedError("the Kangaroo report must name one account and one example batch")
    [account], [batch] = accounts, batches
    calls = re.findall(r"Called method: '([a-z_]+)' in contract: ([a-z0-9_.-]+[.]near)", batch)
    methods = [method for method, _ in calls]
    receivers = {receiver for _, receiver in calls}
    if (not batch.startswith(f"Batch Transaction by {account} ") or len(receivers) != 1
            or methods != ["deposit", "play", "play", "retrieve_credits"]):
        raise SeedError("the Kangaroo example is not the reported four-call batch by the named account")
    [receiver] = receivers
    created = datetime.fromisoformat(post["created_at"].replace("Z", "+00:00"))
    before = str(int(created.timestamp()) * 1000000000 + created.microsecond * 1000)
    nearblocks = nearblocks or rpc.Client(rpc.NEARBLOCKS_URL, _mode(mode), kind="http")
    page = nearblocks.get(f"/v3/accounts/{account}/txns",
                          params={"receiver": receiver, "before_ts": before, "limit": 25})
    matches = [txn for txn in page["data"]
               if txn["signer_account_id"] == account and txn["receiver_account_id"] == receiver
               and txn["outcomes"]["status"] is True and int(txn["block_timestamp"]) < int(before)
               and [action.get("method") for action in txn["actions"]] == methods]
    if not matches:
        raise SeedError("no successful matching Kangaroo batch in the bounded account page")
    transaction_hash = matches[0]["transaction_hash"]
    txns = nearblocks.get(f"/v1/txns/{transaction_hash}")["txns"]
    if len(txns) != 1:
        raise SeedError("the Kangaroo batch lookup must return exactly one transaction")
    txn = txns[0]
    if (txn["transaction_hash"] != transaction_hash or txn["signer_account_id"] != account
            or txn["receiver_account_id"] != receiver or txn["outcomes"]["status"] is not True
            or [action.get("method") for action in txn["actions"]] != methods
            or int(txn["block"]["block_height"]) < 1):
        raise SeedError("the transaction does not confirm the Kangaroo report's named account and batch")
    common = {"entity": "the account named in the Kangaroo Coin Flip exploit post-mortem",
              "subject": account, "chain": "near", "category": "incident", "confidence": 0.8,
              "note": f"quote {_quoted(quotes[0])}; example batch {_quoted(batch)}"}
    return [make_row(label="named as the attacker account in the Kangaroo Coin Flip post-mortem",
                     provenance="third_party", source=_document_source(KANGAROO_URL, data, fetched_at),
                     observed_at=fetched_at, **common),
            make_row(label=(f"signer of matching batch {transaction_hash} at block {txn['block']['block_height']}, "
                            f"receiver {_quoted(receiver)}, methods called " + ", ".join(_quoted(m) for m in methods)),
                     provenance="onchain", observed_at=nearblocks.last_source["fetched_at"],
                     source={"rpc": nearblocks.url, "method": "GET /v1/txns/{transaction_hash}",
                             "args": {"transaction_hash": transaction_hash},
                             "block": int(txn["block"]["block_height"])}, **common)]


def telegram_scam(mode=None, raw_dir=RAW_DIR, nearblocks=None):
    """Only the reported direct collection account, with the claimed transfer independently read."""
    data, fetched_at, _ = snapshot("telegram-scam", mode, raw_dir)
    post = json.loads(data)["post_stream"]["posts"][0]
    paragraphs = [_paragraph_text(p) for p in re.findall(r"<p>.*?</p>", post["cooked"], re.DOTALL)]
    quotes = [p for p in paragraphs if p.startswith("*** the thief put my funds to this address")]
    accounts = _HEX_ACCOUNT.findall(" ".join(quotes))
    victims = re.findall(r"personal wallet[.] - ([a-z0-9_.-]+[.]near)", _paragraph_text(post["cooked"]))
    if len(quotes) != 1 or len(accounts) != 3 or len(victims) != 1:
        raise SeedError("the Telegram report must name a direct collection account and one sending account")
    account, [victim] = accounts[0], victims
    updated = datetime.fromisoformat(post["updated_at"].replace("Z", "+00:00"))
    before = int(updated.timestamp()) * 1000000000 + updated.microsecond * 1000
    nearblocks = nearblocks or rpc.Client(rpc.NEARBLOCKS_URL, _mode(mode), kind="http")
    page = nearblocks.get(f"/v3/accounts/{account}/txns", params={"signer": victim, "limit": 25})
    matches = [txn for txn in page["data"]
               if txn["signer_account_id"] == victim and txn["receiver_account_id"] == account
               and txn["outcomes"]["status"] is True and int(txn["block_timestamp"]) < before
               and [action["action"] for action in txn["actions"]] == ["TRANSFER"]]
    if not matches:
        raise SeedError("no successful direct transfer from the reported sending account in the bounded page")
    transaction_hash = matches[0]["transaction_hash"]
    txns = nearblocks.get(f"/v1/txns/{transaction_hash}")["txns"]
    if len(txns) != 1:
        raise SeedError("the Telegram collection transfer lookup must return exactly one transaction")
    txn = txns[0]
    if (txn["transaction_hash"] != transaction_hash or txn["signer_account_id"] != victim
            or txn["receiver_account_id"] != account or txn["outcomes"]["status"] is not True
            or txn["actions"] != [{"action": "TRANSFER", "method": None, "args": None}]
            or int(txn["block"]["block_height"]) < 1):
        raise SeedError("the transaction does not confirm the reported direct collection transfer")
    common = {"entity": "the direct collection account in the May 2022 Telegram impersonation report",
              "subject": account, "chain": "near", "category": "incident", "confidence": 0.7,
              "note": f"quote {_quoted(quotes[0])}"}
    return [make_row(label="reported direct collection account for a Telegram impersonation theft",
                     provenance="third_party", source=_document_source(TELEGRAM_SCAM_URL, data, fetched_at),
                     observed_at=fetched_at, **common),
            make_row(label=(f"receiver of the reported direct transfer {transaction_hash} from {_quoted(victim)} "
                            f"at block {txn['block']['block_height']}"),
                     provenance="onchain", observed_at=nearblocks.last_source["fetched_at"],
                     source={"rpc": nearblocks.url, "method": "GET /v1/txns/{transaction_hash}",
                             "args": {"transaction_hash": transaction_hash},
                             "block": int(txn["block"]["block_height"])}, **common)]


def write_incident(document, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def append(rows, store_path=labels.STORE_PATH):
    """Append new ids and apply supersession links; returns how many rows were added."""
    store_path = Path(store_path)
    existing = {a.id: a for a in labels.load(store_path).assertions} if store_path.exists() else {}
    new = [row for row in rows if row["id"] not in existing]
    superseded = {row["id"]: row["superseded_by"] for row in rows
                  if row["id"] in existing and row["superseded_by"] is not None
                  and row["superseded_by"] != existing[row["id"]].superseded_by}
    if superseded:
        stored = [json.loads(line) for line in store_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in stored:
            if row["id"] in superseded:
                row["superseded_by"] = superseded[row["id"]]
        store_path.write_text("".join(labels.row_line(row) + "\n" for row in stored + new),
                              encoding="utf-8", newline="\n")
        labels.load(store_path)
    elif new:
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
             for path in sorted(Path(raw_dir).iterdir()) if path.is_file() and path.name != "NOTICE.md"}
    lines = [
        "# Seed snapshots",
        "",
        ("Each source snapshot under `raw/` is the body a source returned, stored byte for byte and named with its "
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
    # A snapshot no row cites (a reference page the code was written against) is listed with 0 rows.
    cited_digests = {digest for digest, _, _ in cited}
    for digest, name in sorted(files.items(), key=lambda item: item[1]):
        if digest in cited_digests:
            continue
        for prefix, ext, url in SNAPSHOTS.values():
            if name.startswith(f"{prefix}-") and name.endswith(f".{ext}"):
                stamp = name[len(prefix) + 1: -len(ext) - 1]
                fetched_at = datetime.strptime(stamp, _STAMP).strftime(_TIME)
                lines.append(f"| `raw/{name}` | {url} | {digest[:4]}...{digest[-4:]} | {fetched_at} | 0 |")
                break
        else:
            raise SeedError(f"the snapshot {name} under {raw_dir} matches no known source")
    return "\n".join(lines) + "\n"


def run(source, mode=None, store_path=labels.STORE_PATH, raw_dir=RAW_DIR, readme_path=README_PATH,
        incident_path=RHEA_INCIDENT_PATH):
    """Seed one source into the store and rewrite the README (and for rhea the incident file);
    returns the number of new rows."""
    if source == "rhea":
        rows = rhea(mode=mode, raw_dir=raw_dir, include_state=True)
    else:
        rows = {"treasuries": treasuries, "catalog": catalog, "infrastructure": infrastructure,
                "sweat-hot": sweat_hot, "intents-2026-10-01": lambda mode, raw_dir: [],
                "skyward": skyward, "fake-wallet": fake_wallet, "kangaroo": kangaroo,
                "telegram-scam": telegram_scam}[source](mode=mode, raw_dir=raw_dir)
    added = append(rows, store_path)
    Path(readme_path).write_bytes(readme_text(store_path, raw_dir).encode("utf-8"))
    if source == "rhea":
        write_incident(rhea_incident(mode, raw_dir, store_path), incident_path)
    elif source == "sweat-hot":
        write_incident(sweat_hot_incident(mode, raw_dir, store_path), SWEAT_HOT_INCIDENT_PATH)
    elif source == "intents-2026-10-01":
        write_incident(intents_incident(mode, raw_dir), INTENTS_INCIDENT_PATH)
    return added
