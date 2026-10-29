# Label record

The format of the ShieldBot for NEAR label store: one assertion per line of `intel/labels.jsonl`, each naming where it comes from. Code: `intel/labels.py` (loading and validation), `intel/seed.py` (the seeds). `python -m intel counts` prints the counts line from the committed file.

## An assertion

| Field | Content |
|---|---|
| `id` | the sha256 hex of the row without `id` and `superseded_by` (see below) |
| `entity` | who the subject belongs to or is operated by, as the source names it |
| `subject` | a NEAR account id, a contract id, an EVM address or another chain's address, exactly as the source writes it |
| `chain` | `near`, or the 1Click `blockchain` code of the subject's chain (`eth`, `base`, `arb`, `op`, `bsc`, `pol`, `hood` and so on) |
| `label` | a short human label |
| `category` | one of the categories below |
| `provenance` | one of the provenance classes below |
| `source` | where the assertion comes from, in the shape its provenance requires (below) |
| `observed_at` | when the source was read, UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `confidence` | a number from 0 to 1; 1 for an entity's own official statement or a chain read |
| `rule_id` | the rule that produced an `inferred` row; null for every other provenance |
| `superseded_by` | null, or the id of the assertion that replaces this one |
| `note` | what the source says, so a reader can check the row against it; every string taken from the source is written in double quotes as data |

A chain code that is not a 1Click code says so in the note: `aurora` (a chain the treasury page lists that the catalog has no code for) and `evm` (the page lists a refill address for "EVM Chains" without naming them).

## Source strings are data

A label or note repeats strings from its source (token symbols, page names, contract metadata) inside double quotes, as data with the row's source, never as ShieldBot's own words. ShieldBot's own text (verdict literals, check outcomes, reason and detail templates, the labels it writes) keeps to the word rules `scripts/check_hygiene.py` enforces over `gate/`, `intel/*.py`, `docs/`, `mcp/`, `scripts/`, `README.md`, `AGENTS.md` and `intel/seeds/README.md`. In those paths the barred word never appears, quoted or not. Source strings that carry it stay in the snapshots, fixtures and the label store, which hold source data.

## Categories

`treasury`, `bridge`, `catalog_token`, `infrastructure`, `incident`, `denylist`, `watch`. The verdict service treats a current `denylist` or `incident` assertion on the receiver or the token as a signal and copies its id into the evidence. The subjects on chain `near` of current `catalog_token`, `bridge`, `treasury` and `infrastructure` assertions join the allowlist as the account ids a receiver is compared with for lookalikes; token symbols are never compared with a receiver.

## Provenance classes and source shapes

| Provenance | Source shape |
|---|---|
| `official_source` | `{"url", "content_sha256", "fetched_at"}`: the entity's own page or API; `content_sha256` is the sha256 of the snapshot stored byte for byte under `intel/seeds/raw/` |
| `third_party` | `{"url", "content_sha256", "fetched_at"}`: a public report about the subject, stored the same way |
| `onchain` | `{"rpc", "method", "args", "block"}`: a read anyone can repeat; `block` is the height the read was made at and is required |
| `inferred` | `{"rule_id", "inputs"}`: a rule applied to the assertions whose ids `inputs` lists; the row's `rule_id` repeats the source's |
| `unknown` | any one of the shapes above |

Every row names a source; `intel/labels.py` refuses a row without one, a row whose source does not have the shape its provenance requires, an `onchain` source without a block, and any category or provenance outside the lists above. Accounts enter the store only when a cited public source names them or a chain read proves the fact; a role taken from documentation and not from the chain says "UNVERIFIED" in its note.

## The id rule

`id` is the sha256 hex of `json.dumps(row_without_id_and_superseded_by, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` encoded as UTF-8: keys sorted by code point, no whitespace, non ASCII characters unescaped. A confidence below 1 is a JSON number in its shortest form. Two rows with the same id are the same assertion, and the store refuses a duplicate.

## The supersession rule

A row is never edited or deleted. When a fact changes, a new row is appended, and the old row's `superseded_by` is set to the new row's id; that is the only field that changes after a row is written, which is why the id leaves it out. `superseded_by` must name an assertion in the store. Readers use only rows whose `superseded_by` is null; the verdict service ignores superseded rows.

## Seeds

`python -m intel seed treasuries`, `catalog` and `infrastructure` build rows from the snapshots and recorded reads; each run appends only rows with a new id and rewrites `intel/seeds/README.md`, which lists every snapshot with its sha256 and fetch time. In the default replay mode no seed touches the network; `GATE_FIXTURES_MODE=record` fetches a source once and stores its snapshot.

- `treasuries`: the NEAR Intents treasury page. The NEAR Intents and HOT Bridge EVM treasuries once per chain the EVM Chains table lists, `intents.near`, the Bitcoin and Zcash treasuries, the Solana and TON treasuries of both entities, and the EVM, Solana and TON refill addresses (the page does not say whose).
- `catalog`: one `catalog_token` per asset of the 1Click catalog snapshot. A NEP-141 token on NEAR is keyed by its NEAR account on chain `near`; any other asset by its contract address (or its asset id when it has none) on its own blockchain. The label is `symbol "<symbol>"` and the note holds the quoted asset id, symbol, blockchain and contract address and the decimals. Every asset of the snapshot has a row.
- `infrastructure`: `intents.near` and `omft.near` from `contract_source_metadata` (`onchain`); each distinct `.omft.near` account in the catalog as a `bridge` token account; `v1.signer` and the OutLayer accounts `outlayer.near`, `worker.outlayer.near`, `dao.outlayer.near` and `connectors.outlayer.near`, each only after `view_account` confirms it exists, with its role marked UNVERIFIED.

## Counts

`python -m intel counts` prints `L assertions over E subjects: onchain O, official_source F, third_party T, inferred I, unknown U`, where E counts distinct (chain, subject) pairs.
