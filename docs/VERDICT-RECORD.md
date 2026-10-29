# Verdict record

The format of a ShieldBot for NEAR verdict: the canonical message the verifier signs, the signature, the keys, expiry and ttl, the verdict id, the evidence document the message binds, the contract argument shapes and the shared test vectors. Code: `gate/canon.py` (the message and the vectors), `gate/ed25519.py` (the RFC 8032 verifier and the TEST keys), `gate/evidence.py` (the evidence document and its hash). The Gate contract in `contracts/gate` rebuilds the same message from its own arguments.

## Verdicts

- ALLOW: every check ran and none found a scam signal.
- DENY: a check found a signal. A signal wins over a check that could not run.
- UNKNOWN: a check could not run, and none found a signal.

Only ALLOW is signed. DENY and UNKNOWN carry no signature and no verdict id, so a refused verdict and an unreachable service look the same to the contract: no signature, no transfer. ALLOW is the absence of a known scam signal, not proof of identity.

## The canonical message

```
GATEv1|<gate_account_id>|<agent_pk>|<kind>|<token_id or ->|<receiver_id>|<amount>|<memo>|<issued_ms>|<expires_ms>|<verdict_id>|<evidence_sha256>|<key_id>|ALLOW
```

UTF-8, 14 fields joined by `|`, no trailing newline. It is not JSON, so Python and Rust produce the same bytes without a shared serializer.

| Field | Form |
|---|---|
| `gate_account_id` | the Gate contract's account id. The contract puts its own account id here, so a verdict signed for another Gate fails the signature check. |
| `agent_pk` | lowercase hex of the 32 raw bytes of the agent's ed25519 public key. The contract puts the key that signed the transaction here, so a verdict signed for another agent fails the signature check. |
| `kind` | `near` or `ft` |
| `token_id` | the NEP-141 token account for `ft`; `-` for `near` |
| `receiver_id` | the payee account id |
| `amount` | the decimal of the amount as a u128 in 1 to 2^128 minus 1, with no sign, spaces, separators or leading zeros. Both sides parse the amount and serialise it again, so `0100` is rejected. |
| `memo` | `-` when the memo is absent, else the sha256 hex of its UTF-8 bytes. The empty string is hashed like any other memo. |
| `issued_ms`, `expires_ms` | milliseconds since the Unix epoch, u64 decimals with no leading zeros; `expires_ms` is not earlier than `issued_ms` |
| `verdict_id` | 32 lowercase hex characters (16 random bytes) |
| `evidence_sha256` | 64 lowercase hex characters: the hash of the evidence document below |
| `key_id` | `cur` or `prev`: which verifier key signed |
| literal | always `ALLOW` on the signing path |

Account ids match `^(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$` and are 2 to 64 characters long. No field can hold `|`: account ids, lowercase hex, decimals and the literals exclude it, and `canonical_message` validates every field before it joins them (it raises `CanonError` otherwise). `parse_canonical` reads a message back and rejects any field that is not in canonical form, except the literal: by design it accepts any upper case literal in the last field, so a test vector signed over DENY parses. Only `canonical_message` is restricted to ALLOW.

## Digest and signature

`digest = sha256(message)`, 32 bytes. `signature = Ed25519(verifier key, digest)`, 64 bytes, written as 128 lowercase hex characters. The signature is pure Ed25519 over the sha256 digest of the canonical message; it is not Ed25519ph. A verifier rebuilds the message from the transfer and the verdict fields, hashes it with sha256 (`env::sha256` in the contract) and checks the signature against the key the verdict names (`env::ed25519_verify` in the contract, `gate.ed25519.verify` in Python). The rebuilt message ends with the verdict's own `literal` field, so a message signed over another literal (vector 08) rebuilds to exactly the bytes that were signed; the contract refuses any literal other than ALLOW as `not_allow` before it verifies the signature.

## Keys and rotation

- A Gate holds a current verifier key and an optional previous key with a deadline in milliseconds. A verdict with `key_id` `cur` is checked against the current key. A verdict with `key_id` `prev` is checked against the previous key, and only until the deadline. To rotate, install the new key as current and the old key as previous, with a deadline at least one ttl ahead, so verdicts already issued stay valid until they expire.
- No private key is committed. The live verifier key is generated and kept outside the repository.
- The TEST verifier key is derived in `gate/ed25519.py` from the public seed `sha256(b"gate test verifier v1")`. Its public key is `fixtures/keys/test-verifier.pub`: line 1 is `ed25519:<base58>`, line 2 is the hex. Anyone can derive the TEST key, so it signs only the test vectors and offline runs and is never registered on testnet. The TEST agent keys come from the seeds `sha256(b"gate test agent v1")` and `sha256(b"gate test agent v2")`.
- `gate/ed25519.py` follows the Ed25519 Python illustration of RFC 8032 section 6, is used for verification everywhere and signs only with the TEST key. It is checked against the RFC 8032 section 7.1 test vectors and against PyNaCl in `tests/test_ed25519.py`.

## Expiry and ttl

A verdict is valid only while `expires_ms` is later than the block timestamp in milliseconds and `expires_ms - issued_ms` is at most the Gate's `max_ttl_ms` (set by the owner, default 300000, five minutes). An expired verdict is refused as `expired`, one with a longer ttl as `ttl_too_long`. A verifier compares `expires_ms` with `issued_ms` before it subtracts, so the unsigned subtraction cannot underflow; `canonical_message` and `parse_canonical` reject an expiry earlier than the issue time.

## Verdict id and replay

`verdict_id` is 16 random bytes in lowercase hex, drawn for each ALLOW. A Gate records every verdict id it accepts, with its expiry, and refuses a second use as `verdict_used`. The signature already binds the agent key, so the verdict id alone is the replay key. A used verdict stays used when the transfer then fails; the agent needs a fresh ALLOW. Ids whose expiry has passed can be pruned.

## The evidence document and its hash

A verdict's evidence is a JSON document (`gate/evidence.py`):

| Key | Content |
|---|---|
| `version` | `"1"` |
| `subject` | `{gate, agent_pk, kind, token, receiver, amount, memo_sha256}`; the memo is kept only as its sha256 hex, or null when absent |
| `checks` | a list of `{id, status, detail, source}`; `status` is `pass`, `signal` or `unknown`; `source` is `{provider, request_sha256, fetched_at}` |
| `labels` | the label assertion ids the rules consulted |
| `verdict` | `ALLOW`, `DENY` or `UNKNOWN` |
| `reasons` | the reasons for the verdict |
| `observed` | `{block_height, timestamp_ms}` of the chain state the checks read |
| `issued_ms`, `expires_ms`, `verdict_id` | set on ALLOW, null otherwise |
| `latency_ms` | the time the verdict took |

The canonical bytes are JSON written so that any language can reproduce them: object keys sorted by Unicode code point; no whitespace outside strings; non ASCII characters written as themselves in UTF-8, not escaped; inside strings, `"` and `\` escaped as `\"` and `\\`, and the control characters U+0000 to U+001F escaped as `\n`, `\t`, `\r`, `\b`, `\f` or `\u00XX` (lowercase hex); numbers are integers only, so no float, NaN or infinity ever appears; no trailing newline. In Python this is `json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` encoded as UTF-8, and `canonical_bytes` refuses a document that holds a float. The evidence hash is the sha256 hex of those bytes. `write` stores the document as `<hash>.json` holding exactly those bytes, so `sha256sum <hash>.json` prints the file's own name, and `check_file` recomputes the hash and the canonical form. The hash is a field of the signed message, so changing one byte of the document breaks the link between the document and the signature.

## Contract argument shapes

The agent calls `request_transfer(request, verdict)` with these JSON objects; the verdict is passed through exactly as the service produced it.

```
TransferRequest {"kind": "near" | "ft", "token_id": <account id or null>, "receiver_id": <account id>,
                 "amount": "<decimal string>", "memo": <string or null>}
Verdict         {"literal": "ALLOW", "issued_ms": <u64 number>, "expires_ms": <u64 number>,
                 "verdict_id": "<32 lowercase hex>", "evidence_hash": "<64 lowercase hex>",
                 "key_id": "cur" | "prev", "signature": "<128 lowercase hex>"}
```

## Test vectors

`python -m gate canon-vectors --write fixtures/canon [--gate-root <root>]` writes one JSON file per vector to `fixtures/canon/`, each vector's evidence document to `fixtures/evidence/<hash>.json` and the TEST verifier public key to `fixtures/keys/test-verifier.pub`. Every field is fixed and Ed25519 signing is deterministic, so a test regenerates the set into a temporary directory and compares it byte for byte with the committed files. The Python tests and the Rust contract tests read the same files. Regenerating with another gate root removes the evidence documents of the earlier vectors and leaves every other evidence document alone. A vector whose literal is not ALLOW (08) has an evidence document with null `issued_ms`, `expires_ms` and `verdict_id`, as every non ALLOW document does; its message still carries them, because the contract reads them from the verdict.

A vector holds `name`, `gate_account_id`, `agent_pk_hex`, `kind`, `token_id` (string or null), `receiver_id`, `amount` (decimal string), `memo` (string or null), `issued_ms`, `expires_ms`, `verdict_id`, `evidence_sha256`, `key_id`, `literal`, `message` (the exact UTF-8 string), `digest_sha256`, `verifier_pub_hex`, `signature_hex`, `expect_valid` (whether the signature verifies over this vector's own message with `verifier_pub_hex`), `expect_contract` (`OK`, or the `REFUSED:<code>` the contract answers when the vector is submitted to the Gate and by the agent the table below names) and `note`.

Vectors named `*-unit` use gate `gate.testnet` and a contract block timestamp of 1700000100000 ms. Vectors named `*-sandbox` use gate `gate.<root>` with timestamps in the year 2100 and a ttl of 300000 ms; their notes say whether the sandbox root account id is measured. Fields a row does not name are those of 01 (sandbox) or 04 (unit); the agent is TEST agent 1 unless named.

| Vector | Change | `expect_contract` |
|---|---|---|
| `01-ft-happy-sandbox` | FT to `vendor.<root>`, token `token.gate.<root>`, amount 1000000, memo `invoice 1` | `OK` |
| `02-near-happy-sandbox` | 1 NEAR to `vendor.<root>`, no token, no memo | `OK` |
| `03-memo-empty-sandbox` | the empty memo | `OK` |
| `04-ft-happy-unit` | FT to `vendor.testnet` inside its ttl | `OK` |
| `05-expired-unit` | expired before the block timestamp | `REFUSED:expired` |
| `06-ttl-too-long-unit` | ttl 600000 ms | `REFUSED:ttl_too_long` |
| `07-bad-signature-unit` | 04 with one signature bit flipped; `expect_valid` false | `REFUSED:bad_signature` |
| `08-wrong-literal-unit` | `DENY` in place of `ALLOW`, signed by a test only path | `REFUSED:not_allow` |
| `09-prev-key-unit` | `key_id` `prev` | `OK` while the TEST key is the previous key and its deadline holds |
| `10-wrong-contract-unit` | signed for `other.testnet`, submitted to `gate.testnet` | `REFUSED:bad_signature` |
| `11-wrong-agent-unit` | signed for TEST agent 2, submitted by TEST agent 1 | `REFUSED:bad_signature` |
| `12-unknown-payee-sandbox` | `stranger.<root>` (not on the allowlist), amount 1 | `OK` within the unknown payee cap |
| `13-unknown-payee-over-cap-sandbox` | `stranger.<root>`, amount 1000000 | `REFUSED:unknown_payee_cap` |

## Why sha256

Verdict records on NEAR hash with sha256 because `env::sha256` is on chain and the Python standard library has no keccak; the EVM registries use keccak256.
