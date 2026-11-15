# Label snapshot

Version: 1 (format `label-snapshot`)

## Purpose

A label snapshot is an export of the label store (`label-assertion-v1.md`) limited to a scope, for a consumer that reads labels offline, such as a wallet that looks a payee up before its user signs. It holds the current rows the scope selects, each exactly as the store holds it, so every row keeps its provenance class, its source and its age (`observed_at`, and the source's `fetched_at` or `block`) as the label assertion format defines them, and every row id can be recomputed. The document names the sha256 of the store file it came from and carries a content hash over its canonical bytes, so a consumer can check that the file is the one that was written. `python -m intel snapshot --scope <file> --out <file>` writes one from the committed store (`intel/__main__.py:_snapshot`); this page fixes the bytes and what a consumer may conclude from them.

## The scope file

A scope file is one JSON object in UTF-8 with exactly two keys (`intel/snapshot.py:61`).

| Key | JSON type | Constraint |
|---|---|---|
| `name` | string | non empty; a name for people, such as the consumer and the date; it selects nothing |
| `select` | array | non empty; each element a selector |

A selector is an object with one, two or three of these keys, each a non empty array of distinct non empty strings (`intel/snapshot.py:45`).

| Key | Matches the row field | Constraint on each entry |
|---|---|---|
| `chains` | `chain` | matches `^[a-z0-9]+$`, as a row's `chain` must |
| `categories` | `category` | one of the eight categories of `label-assertion-v1.md` |
| `subjects` | `subject` | any non empty string, compared byte for byte with the row's `subject` with no case folding, so an EVM address written in another case does not match |

The selection rule (`intel/snapshot.py:86`): a row is selected when its `superseded_by` is null and at least one selector matches it; a selector matches a row when, for every key the selector has, the row's field is one of the listed values. Within a selector the keys narrow the match; across selectors the matches add up. A scope that selects every refusal row and the accounts a wallet's users pay most is two selectors, `{"categories": ["denylist", "incident"]}` and `{"chains": ["near"], "subjects": [...]}`.

The normalized form (`intel/snapshot.py:61`): every array sorted by code point, the keys of each selector in code point order, and the selectors sorted by their canonical bytes. A scope that lists an entry twice, or two selectors that normalize to the same object, is refused. Two scope files that differ only in order normalize to the same scope and give the same snapshot bytes.

## The document

A snapshot is a JSON object with exactly these six keys (`intel/snapshot.py:23`).

| Key | JSON type | Content |
|---|---|---|
| `format` | string | `"label-snapshot"` |
| `version` | string | `"1"` |
| `scope` | object | the scope in normalized form |
| `store_sha256` | string | 64 lowercase hex: the sha256 of the bytes of the store file the rows were read from (`intel/labels.py:190`) |
| `rows` | array | the selected rows, sorted by `chain`, then `subject`, then `id`, each compared by code point; no id twice |
| `content_sha256` | string | 64 lowercase hex: the content hash below |

Each element of `rows` is a label assertion with exactly its 13 fields and the values the store holds (`label-assertion-v1.md`), with nothing added, removed or rewritten: its `id` recomputes from its own canonical body, and its `superseded_by` is null, since a superseded row is never selected. The document carries no time and no count of its own. A row's age is what the row carries, its `observed_at` and its source's `fetched_at` or `block`, and a reader works out how old a row is at the time it reads it.

## Canonical serialization and the content hash

The canonical bytes of a document, or of any part of it, are canonical JSON (see `README.md`, shared conventions): keys sorted by code point at every level, inside the rows too, compact, non ASCII unescaped, UTF-8, no trailing newline, and NaN and infinity refused (`intel/snapshot.py:35`). Numbers follow the label assertion page: `confidence` may be a float, written in the shortest form that reads back to the same double, and an integer stays an integer. In Python: `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`.

`content_sha256` is the sha256 hex of the canonical bytes of the document without the key `content_sha256` (`intel/snapshot.py:97`), the rule the label assertion id follows for `id`. A snapshot file holds exactly the canonical bytes of the whole document, `content_sha256` included (`intel/snapshot.py:113`), so its bytes are fixed by the store and the scope alone: the same store file and the same normalized scope give the same file on every run.

## Checking a snapshot

`intel.snapshot.check` (`intel/snapshot.py:122`) is the reference reader. It accepts the bytes only when all of these hold, and a reader in any language checks the same:

1. The bytes are UTF-8 JSON: an object with exactly the six keys, `format` `label-snapshot` and `version` `1`.
2. `scope` is in normalized form.
3. `store_sha256` is 64 lowercase hex.
4. Every row passes the label assertion rules, its id included (`intel/labels.py:97`), is current, is selected by the scope, and sorts after the row before it.
5. The bytes equal the canonical form of the document they parse to.
6. `content_sha256` equals the sha256 of the canonical bytes of the document without it.

Passing these shows that the file is consistent and unchanged since it was written. It does not show who wrote it: a snapshot is not signed. A consumer compares `content_sha256` with the value the operator gives it over a separate channel, and a consumer that holds the store file whose sha256 is `store_sha256` can rebuild the snapshot with `intel.snapshot.build` (`intel/snapshot.py:103`) and compare the bytes.

## What a consumer may and may not infer

A consumer may infer:

- Each row is the label assertion with its `id`, and states its fact on the authority of its source as `label-assertion-v1.md` and `docs/LABEL-RECORD.md` define it: a document by its URL, its content hash and its fetch time; a chain read by its RPC, method, arguments and block; an inferred row by its rule and the ids of its inputs.
- The rows are every current row of the store at `store_sha256` that the scope selects, and no other.
- A current row in category `denylist` or `incident` whose provenance is anything but `inferred` is a reason to refuse the payment, as the verdict service's `label_denylist` check refuses on the same row (`gate/rules.py:443-447`).

A consumer may not infer:

- That an absent account is cleared. An account the snapshot holds no row for carries no label in this snapshot and nothing more: it was not judged, and none of the verdict service's other checks (the account's existence and age, a lookalike name, a dust creation deposit, contract code, storage registration on the token) was run on it. Absence is not a pass and not an ALLOW, and a consumer that treats it as either, or skips a check because of it, does not conform to this version. A subject the scope names that no row carries is absent in the same sense. The label store covers the sources `docs/LABEL-RECORD.md` lists, not every account.
- That an inferred row refuses. A row with provenance `inferred` is an attribution hypothesis: a consumer shows it with its `rule_id` and its `source.inputs` and never refuses a payment on it alone. Its inputs need not be in the snapshot. In the store, a current inferred row in `denylist` or `incident` stands only on a subject that also carries a current `incident` row with provenance `third_party` or `onchain`, which the store checks when it loads (`intel/labels.py:181-189`), so a scope that selects both categories over every subject holds that row too; a narrower scope can hold the inferred row alone, and then the snapshot gives no reason to refuse that subject.
- That a row in another category clears its subject. A `catalog_token`, `bridge`, `treasury`, `infrastructure`, `organisation` or `watch` row says who the account is according to its source; it does not say that a payment to the account is harmless.
- A rule from `confidence`: it is carried as the store wrote it, and this version gives no rule that turns it into an outcome.
- Anything after `store_sha256`. A row added to the store, or superseded in it, after that state is not reflected; a consumer gets a newer snapshot to see it.
- Any refusal from a scope that selects neither `denylist` nor `incident` rows: such a snapshot holds no refusal information at all.

## Worked example

The scope file `fixtures/spec/snapshot-v1-scope.json` is the shape a wallet starts from: every `incident` and `denylist` row on any chain, and five NEAR accounts its users pay, one of which, `v2.ref-finance.near`, the store holds no row for. Over the committed store:

```
python -m intel snapshot --scope fixtures/spec/snapshot-v1-scope.json --out snapshot.json
```

writes the file and prints three lines: the row and subject counts with the path, then `store_sha256` and `content_sha256` in full. The counts and both hashes change whenever `intel/labels.jsonl` gains a row, so this page quotes none of them.

The vector `refusal-rows-and-payees` of `fixtures/spec/snapshot-v1.json` pins the bytes over a fixed store, `fixtures/spec/snapshot-v1-store.jsonl`, whose sha256 is `9fa00fcd...` (the full value is `store_sha256` of the vector file). Its scope lists the selectors, the categories and the subjects out of order; normalized, its canonical bytes are:

```
{"name":"every refusal row and five payees","select":[{"categories":["denylist","incident"]},{"chains":["near"],"subjects":["cafe-example.testnet","renamed-example.testnet","v1.signer","v2.ref-finance.near","wrap.near"]}]}
```

It selects 9 of the 11 rows: the four `incident` rows and the inferred `denylist` row on the two implicit accounts the rekt.news Rhea report names (`intel/incidents/rhea.json`), the `infrastructure` row of `v1.signer`, the `catalog_token` row of `wrap.near`, the synthetic non ASCII row of `cafe-example.testnet` and the newer of the two synthetic rows on `renamed-example.testnet`. It leaves out the `treasury` row on chain `arb`, which no selector matches, and the older synthetic row, which is superseded. `v2.ref-finance.near` has no row, so the snapshot says nothing about it. All nine rows are on chain `near`, and the two implicit accounts sort first because digits sort before letters. Its `content_sha256` is `36424b67...` (the full value is `content_sha256` of that vector; the whole document is its `document`).

The vector `denylist-only` selects the one inferred row alone, a snapshot on which a conforming consumer refuses nothing; `absent-subject` selects no row, and its `rows` is the empty array.

## Vectors

| Path | What it pins | Reproduced by |
|---|---|---|
| `fixtures/spec/snapshot-v1.json` | three vectors over the fixed store, each with its scope as a scope file holds it, its row count, its `content_sha256` and the canonical bytes of the whole document: `refusal-rows-and-payees` above, `denylist-only` and `absent-subject` | `intel.snapshot.build`, `intel.snapshot.check` |
| `fixtures/spec/snapshot-v1-store.jsonl` | the fixed store, 11 rows: 8 lines copied byte for byte from `intel/labels.jsonl` (an `official_source` treasury row, an `onchain` infrastructure row, an `official_source` catalog row, two `third_party` and two `onchain` incident rows, and the `inferred` denylist row whose inputs are those two `onchain` rows), the synthetic `unknown` row of `label-assertion-v1.md`, and a synthetic pair on `renamed-example.testnet` whose older row names the newer in `superseded_by`; each synthetic row says so in its `note` | `intel.labels.load` |
| `fixtures/spec/snapshot-v1-scope.json` | the scope file of the worked example; its output depends on the committed store, so `tests/test_snapshot.py` checks it against a recount instead of pinning its bytes | `intel.snapshot.read_scope` |

`tests/test_snapshot.py` also breaks each rule of the reader once (an edited row, a superseded row, a row outside the scope, rows out of order or twice, a dropped row, a changed content hash, bytes that are not canonical, an unnormalized scope) and checks that `check` refuses each.

## Source of truth

- `intel/snapshot.py:23` the document keys; `:35` `canonical_bytes`; `:45` the selector lists; `:61` `scope`; `:86` `in_scope`; `:97` `content_sha256`; `:103` `build`; `:113` `write`; `:122` `check`.
- `intel/__main__.py:_snapshot` the command.
- `label-assertion-v1.md` for the rows; `docs/LABEL-RECORD.md` for what each category and provenance means.
