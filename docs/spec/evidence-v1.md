# Evidence document

Version: 1

## Purpose

An evidence document records what the verdict service checked for one transfer, what each check found, which label assertions it consulted and what it decided. Its sha256 is the `evidence_sha256` field of the signed verdict message (`verdict-v1.md`) and the `evidence_hash` of a registry record (`registry-record-v1.md`), so the document cannot change after it is signed or recorded. The document is stored as `<hash>.json` holding exactly its canonical bytes.

## Fields

A document is a JSON object with exactly these 11 keys (`gate/evidence.py:50-67`), except the intents variant below, which has a twelfth.

| Key | JSON type | Content |
|---|---|---|
| `version` | string | `"1"` (`gate/evidence.py:26`) |
| `subject` | object | the transfer the verdict is about; the subject block below |
| `checks` | array | the check objects below, in the order the rules ran them |
| `labels` | array of strings | the ids of the label assertions the rules consulted (`label-assertion-v1.md`) |
| `verdict` | string | `ALLOW`, `DENY` or `UNKNOWN` |
| `reasons` | array of strings | the reasons for the verdict, in ShieldBot's words |
| `observed` | object | `{"block_height": integer or null, "timestamp_ms": integer or null}`: the chain state the checks read (`gate/rules.py:737`) |
| `issued_ms` | integer or null | milliseconds since the Unix epoch |
| `expires_ms` | integer or null | milliseconds since the Unix epoch |
| `verdict_id` | string or null | 32 lowercase hex |
| `latency_ms` | integer or null | the time the judgement took, measured with a monotonic clock (`gate/verdict.py:110`) |

`issued_ms`, `expires_ms` and `verdict_id` are set only when the outcome is ALLOW and the service signs it; they are null on DENY, on UNKNOWN and on an ALLOW the service passes to a human instead of signing (`gate/verdict.py:106-109`). A document with these three set is the document of a signed verdict, and the same three values are in the signed message.

### The subject block (kinds `near` and `ft`)

`gate/evidence.py:30-40`. The service validates the transfer with `gate.canon.validate_transfer` before it builds the block (`gate/verdict.py:96-97`), so every value is already in canonical form.

| Key | JSON type | Content |
|---|---|---|
| `gate` | string | the Gate contract's account id |
| `agent_pk` | string | the agent's ed25519 public key, 64 lowercase hex |
| `kind` | string | `near` or `ft` |
| `token` | string or null | the NEP-141 token account for `ft`; null for `near` |
| `receiver` | string | the payee account id |
| `amount` | string | the amount as its canonical decimal (`verdict-v1.md`) |
| `memo_sha256` | string or null | the sha256 hex of the memo's UTF-8 bytes, or null when there is no memo; the empty memo is hashed. The memo itself is never stored. |

### The intents variant

An EVM leg preflight (`gate/intents.py:254-260`) writes a document whose `subject` is `{"kind": "intents", "origin_asset", "destination_asset", "amount", "recipient", "recipient_type", "quote"}`, where `quote` is `{"correlation_id", "timestamp", "amount_out", "min_amount_out"}` from the dry quote answer, or null when no quote was read (`gate/intents.py:187-189`), and which has a twelfth top level key, `"kind": "intents"`. It is never signed: `issued_ms`, `expires_ms`, `verdict_id`, `latency_ms` and `observed.block_height` are null, and `labels` is empty. The registry refuses to record it (`gate/registry.py:49-51`). No committed vector pins this variant.

### A check

`gate/evidence.py:43-47`. A check is an object with exactly four keys.

| Key | JSON type | Content |
|---|---|---|
| `id` | string | the check's name, such as `receiver_exists`, `label_denylist`, `token_in_catalog`, `ft_metadata_sane`, `storage_registered`, `memo_injection`, `issuer_blacklist`, `account_age`, `dust_funded`, `lookalike`, `code_without_metadata`, or for the intents variant `quote_dry`, `destination_supported`, `ofac_sanctioned`, `recipient_theft_label`, `recipient_code`. The list is the code's current set, not a closed enumeration: a reader must accept an id it does not know. |
| `status` | string | `pass`, `signal` or `unknown`; any other value is refused (`gate/evidence.py:27`, `:45-47`) |
| `detail` | string | what the check found, in ShieldBot's words; third party strings appear inside double quotes |
| `source` | object | `{"provider": string, "request_sha256": string or null, "fetched_at": string or null}` |

In `source`, `provider` names where the answer came from (an RPC host, `label store`, `local patterns`, `local rules`, or a committed fixture path); `request_sha256` is the 64 hex key of the recorded request (`gate/rpc.py:101-104`), the sha256 of the label store file, or for `ofac_sanctioned` the sha256 of the named fixture's JSON bytes. It is null for a local rule; `fetched_at` is the UTC time `YYYY-MM-DDTHH:MM:SSZ` the response was fetched, or null when nothing was fetched (`gate/rpc.py:305-306`, `gate/rules.py:110-111,114-115`). The OFAC check detail and match reason carry the source XML hash recorded inside that fixture.

How check statuses become a verdict: any `signal` gives DENY; otherwise any `unknown` gives UNKNOWN; otherwise ALLOW (`gate/rules.py:734`, `gate/intents.py:252`).

## Canonical serialization

The canonical bytes are canonical JSON (see `README.md`, shared conventions) with one restriction: every number is an integer. A float anywhere in the document, at any depth, is refused before serialization, and NaN and infinity cannot be written (`gate/evidence.py:70-79`, `:81-85`). In Python: `json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`. There is no trailing newline.

## Hashing and storage

The evidence hash is the sha256 hex of the canonical bytes (`gate/evidence.py:90`). The document is stored as `<hash>.json` containing exactly the canonical bytes (`gate/evidence.py:95-100`), so `sha256sum <hash>.json` prints the file's own name. A stored file conforms when its sha256 equals its name and its bytes equal the canonical form of the JSON they parse to (`gate/evidence.check_file`, `gate/evidence.py:103-112`).

## Worked example

The document of canon vector `04-ft-happy-unit` (`fixtures/canon/04-ft-happy-unit.json`) is stored under `fixtures/evidence/` as `3fe3f068...json`, the vector's `evidence_sha256`. Its canonical bytes, with the two 64 hex values elided here and one line break added for reading:

```
{"checks":[],"expires_ms":1700000300000,"issued_ms":1700000000000,"labels":[],"latency_ms":null,"observed":{"block_height":null,"timestamp_ms":null},"reasons":["canonical message test vector: no check ran, so this document records no judgement"],
"subject":{"agent_pk":"29eb5de0...","amount":"1000000","gate":"gate.testnet","kind":"ft","memo_sha256":"55cd62f7...","receiver":"vendor.testnet","token":"token.gate.testnet"},"verdict":"ALLOW","verdict_id":"4d9184c6b4ec99800e259aaf3fec0489","version":"1"}
```

`memo_sha256` is the sha256 of the UTF-8 bytes of `invoice 1`. A document with checks, such as the testnet vendor's `fixtures/evidence/7e77187d...json`, holds check objects of this form (request hash elided):

```
{"detail":"kestrelridge-shop.testnet exists","id":"receiver_exists","source":{"fetched_at":"2026-11-01T20:32:04Z","provider":"test.rpc.fastnear.com","request_sha256":"91918b33..."},"status":"pass"}
```

## Vectors

| Path | What it pins | Reproduced by |
|---|---|---|
| `fixtures/evidence/` | 20 stored documents, each named by the sha256 of its bytes and in canonical form, among them signed ALLOWs with checks, DENYs and the documents of the canon vectors | `gate.evidence.check_file` |
| `fixtures/canon/` | each vector's `evidence_sha256` is the hash of the document `gate.canon.build_vectors` builds for it, which is the file of that name under `fixtures/evidence/` | `gate.canon.build_vectors`, `gate.evidence.evidence_hash` |
| `fixtures/registry/sample-allow-evidence.json` | an evidence document stored under another name, whose bytes hash to the sample record's `evidence_hash` | `gate.verify.verify` |
| `fixtures/spec/label-assertion-v1.json` | the same canonical JSON with non ASCII text and the `\n`, `\t` and `\"` escapes (the label body allows a float; evidence does not) | `intel.labels.assertion_id` |

The escaping and key order rules are also pinned byte for byte by `tests/test_evidence.py` (`test_exact_bytes_and_hash`, `test_control_characters_are_escaped`, `test_keys_sort_by_code_point`, `test_floats_are_rejected`).

## Source of truth

- `gate/evidence.py:30` `subject`; `:43` `check`; `:50` `document`; `:81` `canonical_bytes`; `:88` `evidence_hash`; `:93` `write`; `:103` `check_file`.
- `gate/verdict.py:106-109` when the issue time, expiry and verdict id are set; `gate/intents.py:254-260` the intents variant.
