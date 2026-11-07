# Label assertion

Version: 1

## Purpose

A label assertion states one fact about one address on one chain (who it belongs to, what it is) and names the source the fact comes from. The label store `intel/labels.jsonl` holds one assertion per line. An assertion's `id` is the sha256 of its canonical body, so anyone holding the row can recompute the id, and the registry contract records labels under that id (`assertion_hash`, see `registry-record-v1.md`). The prose description and the policy rules (which categories refuse a payment, how seeds are built) are in `docs/LABEL-RECORD.md`; this page fixes the bytes.

## Fields

A row is a JSON object with exactly these 13 keys, no more and no fewer (`intel/labels.py:15-16`, checked at `intel/labels.py:99`).

| Field | JSON type | Constraint |
|---|---|---|
| `id` | string | 64 lowercase hex; equals the id computed by the rule below (`intel/labels.py:127`) |
| `entity` | string | non empty (`intel/labels.py:101`) |
| `subject` | string | non empty; the address exactly as the source writes it, in any case; no account id check is made |
| `chain` | string | matches `^[a-z0-9]+$` (`intel/labels.py:28`, `:103`) |
| `label` | string | non empty |
| `category` | string | one of `treasury`, `bridge`, `catalog_token`, `infrastructure`, `incident`, `denylist`, `watch`, `organisation` (`intel/labels.py:17-18`) |
| `provenance` | string | one of `onchain`, `official_source`, `third_party`, `inferred`, `unknown` (`intel/labels.py:19`) |
| `source` | object | one of the three source shapes below; never null (`intel/labels.py:109`) |
| `observed_at` | string | matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$` with ASCII digits (`intel/labels.py:29`, `:120`); the pattern is all the code checks |
| `confidence` | number | integer or float from 0 to 1 inclusive; `true` and `false` are refused (`intel/labels.py:122`) |
| `rule_id` | string or null | for provenance `inferred`, a writer MUST provide a string equal to `source.rule_id`; null for every other provenance |
| `superseded_by` | string or null | null, or 64 lowercase hex (`intel/labels.py:125`) |
| `note` | string | non empty |

### Source shapes

The shape is chosen by the exact key set of `source` (`intel/labels.py:20`, `:68`).

| Shape | Keys | Writer requirements |
|---|---|---|
| document | `url`, `content_sha256`, `fetched_at` | `url` a string starting `https://`; a writer MUST provide `content_sha256` as a string of 64 lowercase hex; `fetched_at` the same time pattern as `observed_at` |
| onchain | `rpc`, `method`, `args`, `block` | `block` an integer of at least 1 (not a boolean); `rpc` starting `https://`; `method` non empty; `args` an object |
| inferred | `rule_id`, `inputs` | a writer MUST provide a non empty string `rule_id`; `inputs` a non empty list |

Reference validator behavior today: `intel/labels.py:80` applies the hex check to `str(value)`, so a JSON integer with 64 decimal digits passes despite the writer requirement. For inferred `rule_id`, `intel/labels.py:92` checks the source value for truthiness and `:116` checks equality with the row value, but the validator does not check either value's type. These type checks are gaps between the writer requirements and what the reference validator enforces.

Provenance `official_source` and `third_party` need the document shape, `onchain` the onchain shape, `inferred` the inferred shape, and `unknown` accepts any of the three (`intel/labels.py:25`, `:112`).

### Store rules

`intel/labels.load` (`intel/labels.py:161`) refuses the whole store when any row fails the rules above, when two rows have the same `id`, when a `superseded_by` names no row in the store, or when a row with provenance `inferred`, category `denylist` or `incident` and null `superseded_by` sits on a (`subject`, `chain`) pair that has no row with category `incident`, provenance `third_party` or `onchain`, and null `superseded_by`. Blank lines are skipped. A row is never edited except to set its `superseded_by`; readers use only rows whose `superseded_by` is null.

## Canonical serialization and the id

The canonical body is the row without the keys `id` and `superseded_by`, serialized as canonical JSON (see `README.md`, shared conventions): keys sorted by code point at every level, nested objects included, compact, non ASCII unescaped, UTF-8 (`intel/labels.py:53`). `id` is the sha256 hex of those bytes. `superseded_by` is left out because it is the one field that changes after a row is written.

Numbers. The body is serialized from the parsed values, not from the text of the line. An integer is written in decimal. A float is written in the shortest form that reads back to the same IEEE 754 double, as Python's `repr` writes it: `0.9`, `0.75`; a value below `0.0001` is written in exponent form (`1e-05`). An integer and a float are different values: `1` and `1.0` give different ids, and the store writes the integer `1` for full confidence. No NaN or infinity can pass the range check.

## The store line

A line of `intel/labels.jsonl` is the row as compact JSON with the top level keys in the order of the field table above and nested objects in the order they were written, non ASCII unescaped, followed by a newline (`intel/labels.row_line`, `intel/labels.py:132`). The line is not canonical and is not hashed; a reader must parse it and recompute the id from the canonical body.

## Worked example

The `onchain-store-row` vector is line 353 of the store. Its canonical body, the exact UTF-8 bytes that are hashed (shown on two lines here; the bytes are one line with no newline):

```
{"category":"infrastructure","chain":"near","confidence":1,"entity":"NEAR MPC","label":"MPC signer contract","note":"role from docs, UNVERIFIED; view_account shows the account exists with a contract","observed_at":"2026-10-29T09:21:29Z","provenance":"onchain","rule_id":null,
"source":{"args":{"account_id":"v1.signer"},"block":222105621,"method":"view_account","rpc":"https://free.rpc.fastnear.com"},"subject":"v1.signer"}
```

Its sha256, and so its `id`, is `5fc8db8f...` (the full value is `id` of that vector). In the store line the source keys keep their written order `rpc`, `method`, `args`, `block`; in the canonical body they are sorted.

The `synthetic-non-ascii-unknown-provenance` vector is not in the store. It pins non ASCII text (`Café Example Øst`, `€`), an escaped quote, `\n` and `\t` inside a string, a float confidence of `0.75` and provenance `unknown` with the document shape.

For each event in `fixtures/registry/labels-20261103.json`, `standard` is `shieldbot-near`, `version` is `1.0.0`, `event` is `label_recorded`, and `data` contains exactly one item.

## Vectors

| Path | What it pins | Reproduced by |
|---|---|---|
| `fixtures/spec/label-assertion-v1.json` | five rows, each with `row`, `canonical_body`, `id` and `line`: one `official_source`, one `onchain`, one `third_party` with confidence `0.9`, one `inferred`, and the synthetic row above; `in_store` says whether `line` is a line of `intel/labels.jsonl` | `intel.labels.validate`, `intel.labels.assertion_id`, `intel.labels.row_line` |
| `fixtures/registry/labels-20261103.json` | 43 `label_recorded` events from testnet; each `assertion_hash` is the `id` of a current store row with the same `subject`, `chain`, `label`, `category` and `provenance` | `intel.labels.load` |
| `intel/labels.jsonl` | every committed row validates and every id is its own body's sha256 (not under `fixtures/`, but the largest vector set) | `intel.labels.load` |

## Source of truth

- `intel/labels.py:15` the field list; `:17` categories; `:19` provenances; `:20` source shapes; `:53` `assertion_id`; `:97` `validate`; `:132` `row_line`; `:161` `load`.
- `docs/LABEL-RECORD.md` for what each category and provenance means and how rows enter the store.
