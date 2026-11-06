# Verdict

Version: 1 (message prefix `GATEv1`)

## Purpose

A verdict is the verifier's signed permission for one transfer. The verdict service signs only ALLOW; DENY and UNKNOWN are never signed, so to the Gate contract a refused verdict and an unreachable service look the same. The signature covers the sha256 of a canonical message that binds the Gate, the agent's key, the transfer, the issue and expiry times, a random verdict id, the evidence document's hash (`evidence-v1.md`) and the id of the key that signed. The Gate contract rebuilds the message from its own account id, the key that signed the transaction and the call's arguments, so a verdict is valid for exactly one Gate, one agent and one transfer. `docs/VERDICT-RECORD.md` describes the policy around it (rotation, replay, ttl); this page fixes the bytes.

## The canonical message

The message is 14 fields joined by `|` (U+007C), encoded as UTF-8, with no trailing newline (`gate/canon.py:117`, `contracts/gate/src/verdict.rs:22`):

```
GATEv1|<gate>|<agent_pk>|<kind>|<token>|<receiver>|<amount>|<memo>|<issued_ms>|<expires_ms>|<verdict_id>|<evidence_sha256>|<key_id>|<literal>
```

| # | Field | Form | Code |
|---|---|---|---|
| 1 | prefix | the literal `GATEv1` | `gate/canon.py:21` |
| 2 | `gate` | the Gate contract's account id | `gate/canon.py:39` |
| 3 | `agent_pk` | the 32 raw bytes of the agent's ed25519 public key as 64 lowercase hex | `gate/canon.py:78` |
| 4 | `kind` | `near` or `ft` | `gate/canon.py:23` |
| 5 | `token` | for `ft`, the NEP-141 token account id; for `near`, `-` (a `near` transfer with a token is refused) | `gate/canon.py:88` |
| 6 | `receiver` | the payee account id | |
| 7 | `amount` | the amount in base units, an integer from 1 to 2^128 minus 1, written in decimal with ASCII digits, no sign, no spaces, no separators and no leading zero | `gate/canon.py:46` |
| 8 | `memo` | `-` when there is no memo; otherwise the sha256 of the memo's UTF-8 bytes as 64 lowercase hex. The empty memo is a memo and is hashed. | `gate/canon.py:59` |
| 9 | `issued_ms` | milliseconds since the Unix epoch, an integer from 0 to 2^64 minus 1 in decimal without leading zeros | `gate/canon.py:72` |
| 10 | `expires_ms` | the same form; not earlier than `issued_ms` | `gate/canon.py:98` |
| 11 | `verdict_id` | 32 lowercase hex: 16 random bytes drawn for each signed ALLOW | `gate/verdict.py:92` |
| 12 | `evidence_sha256` | 64 lowercase hex: the evidence hash (`evidence-v1.md`) | |
| 13 | `key_id` | `cur` or `prev` | `gate/canon.py:24` |
| 14 | `literal` | `ALLOW` | `gate/canon.py:22` |

An account id is 2 to 64 characters matching `^(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$` with ASCII digits (`gate/canon.py:29`). No field can contain `|`, so splitting on `|` gives the fields back. A builder validates every field before it joins them and refuses a value with no canonical form (`gate.canon.CanonError`); `gate/canon.py:84` is the validation.

The decimal forms are compared as strings, so `0100`, `+1`, `1.0` and `1e6` are not amounts. The Gate contract reads the amount as a JSON string into a u128 and writes it back in decimal; an amount sent in any other form rebuilds to different bytes and fails the signature check.

### Reading a message back

`gate.canon.parse_canonical` (`gate/canon.py:137`) splits a message and refuses one that is not 14 fields starting with `GATEv1` or whose fields are not in canonical form. By design it accepts any run of upper case ASCII letters as the literal, so vector 08, signed over `DENY`, parses; only the builder is restricted to `ALLOW`. The memo comes back as its hash.

## Digest and signature

```
digest    = sha256(message)                       32 bytes
signature = Ed25519_sign(verifier key, digest)    64 bytes, written as 128 lowercase hex
```

The signature is pure Ed25519 (RFC 8032 section 5.1) over the 32 byte digest; it is not Ed25519ph and the message is not signed directly (`gate/canon.py:132`, `contracts/gate/src/verdict.rs:90`). A verifier rebuilds the message, hashes it and checks the signature against the public key that `key_id` names.

## The Verdict object

The service returns the signed verdict as a JSON object, and the agent passes it unchanged to the Gate's `request_transfer(request, verdict)` (`gate/verdict.py:108`, `contracts/gate/src/types.rs:96`):

| Key | JSON type | Content |
|---|---|---|
| `literal` | string | `ALLOW` |
| `issued_ms` | number | message field 9 |
| `expires_ms` | number | message field 10 |
| `verdict_id` | string | message field 11 |
| `evidence_hash` | string | message field 12 |
| `key_id` | string | message field 13 |
| `signature` | string | 128 lowercase hex |

The `request` argument is the TransferRequest `{"kind": "near" or "ft", "token_id": account id or null, "receiver_id": account id, "amount": decimal string, "memo": string or null}` (`contracts/gate/src/types.rs:86`); its fields fill message fields 4 to 8. For DENY and UNKNOWN the service writes no Verdict, only `{"literal", "evidence_hash"}` for the record (`docs/VERDICT-RECORD.md`).

## Keys

- `cur` names the Gate's current verifier key; `prev` names its previous key, accepted only while the block time in milliseconds is not later than the previous key's deadline (`contracts/gate/src/verdict.rs:72`). Any other key id, or `prev` with no previous key or after its deadline, is refused as `bad_key_id`.
- The TEST verifier key is derived from the 32 byte seed `sha256("gate test verifier v1")` (ASCII bytes) by RFC 8032 key generation (`gate/ed25519.py:183`). Its public key is `fixtures/keys/test-verifier.pub`: line 1 is `ed25519:` and the base58 of the 32 bytes, line 2 the 64 lowercase hex, each line ending in a newline. Anyone can derive it, so it signs only test vectors.
- The TEST agent keys come from the seeds `sha256("gate test agent v1")` and `sha256("gate test agent v2")` (`gate/ed25519.py:184`).

## What the Gate checks

`contracts/gate/src/verdict.rs:53`, in this order, each failure refused as `REFUSED:<code>`: `not_allow` (the literal is not `ALLOW`); `expired` (`expires_ms` is not later than the block time, or is earlier than `issued_ms`); `ttl_too_long` (`expires_ms - issued_ms` is above the Gate's `max_ttl_ms`, 300000 in the tests); `bad_key_id`; `bad_request` (`verdict_id`, `evidence_hash` or `signature` not lowercase hex of 16, 32 or 64 bytes); `bad_signature`. After the signature, a verdict id the Gate has already accepted is refused as `verdict_used` (`contracts/gate/src/lib.rs:126`).

## Worked example

Vector `04-ft-happy-unit` (`fixtures/canon/04-ft-happy-unit.json`): gate `gate.testnet`, TEST agent 1, `ft` of `1000000` base units of `token.gate.testnet` to `vendor.testnet`, memo `invoice 1`, issued 1700000000000, expires 1700000300000, key `cur`. With the 64 hex values shortened to their first 8 characters, the message is:

```
GATEv1|gate.testnet|29eb5de0...|ft|token.gate.testnet|vendor.testnet|1000000|55cd62f7...|1700000000000|1700000300000|4d9184c6b4ec99800e259aaf3fec0489|3fe3f068...|cur|ALLOW
```

Its sha256 is `0a244c38...` (`digest_sha256`), and the TEST verifier's signature over that digest is `4e83bd4c...` (`signature_hex`). The vector holds the full message, digest, public key and signature.

## Vectors

| Path | What it pins | Reproduced by |
|---|---|---|
| `fixtures/canon/` | 13 vectors, `01-ft-happy-sandbox.json` to `13-unknown-payee-over-cap-sandbox.json`. Each holds the inputs, `literal`, the exact `message`, `digest_sha256`, `verifier_pub_hex`, `signature_hex`, `expect_valid` (whether the signature verifies over the vector's own message) and `expect_contract` (`OK` or the `REFUSED:<code>` the Gate answers). The cases are listed in `docs/VERDICT-RECORD.md`, Test vectors. | `gate.canon.build_vectors` (regenerates every file's content), `gate.canon.canonical_message`, `gate.canon.digest`, `gate.ed25519.verify` |
| `fixtures/keys/test-verifier.pub` | the TEST verifier public key in both forms | `gate.ed25519.public_from_seed`, `gate.base58.encode_key` |
| `fixtures/evidence/` | the evidence document of each canon vector, under its hash | `gate.evidence.check_file` |
| `fixtures/registry/sample-allow-verdict.json` | a Verdict object whose signature verifies over the message rebuilt from `fixtures/registry/sample-allow-evidence.json` | `gate.verify.verify` |

Vector 07 has one signature bit flipped (`expect_valid` false); vector 08 is signed over `DENY` and the Gate refuses its literal before it checks the signature. A conformant verifier must verify every vector whose `expect_valid` is true and refuse 07.

## Source of truth

- `gate/canon.py:117` `canonical_message`; `:84` field validation; `:132` `digest`; `:137` `parse_canonical`; `:314` `build_vectors`.
- `contracts/gate/src/verdict.rs:22` the contract's message; `:53` the contract's checks; `contracts/gate/src/types.rs:86` and `:96` the argument shapes.
- `gate/ed25519.py:183` the TEST seeds; `gate/verdict.py:89` when a verdict is signed.
