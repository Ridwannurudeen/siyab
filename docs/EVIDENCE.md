# Recorded testnet evidence

## Allowed payment

The invoice 2 payment sent 10 DMT to `kestrelridge-shop.testnet` on 1 November. Transaction [`7agsRsZgeRm2vhK1LXug2qRQ2qudbAMjsLncuxGpvdwn`](https://testnet.nearblocks.io/txns/7agsRsZgeRm2vhK1LXug2qRQ2qudbAMjsLncuxGpvdwn) completed with `SuccessValue`.

The Gate emitted `EVENT_JSON` `transfer_scheduled` with request id 3, verdict id `1b29052381ecf3fc1937b36db2539678` and evidence hash `7e77187d...c88bb2cc`. The token receipt emitted `EVENT_JSON` `ft_transfer` from `kestrelridge-main.testnet` to `kestrelridge-shop.testnet`, amount `10000000`, memo `invoice 2`. The transaction also contains `transfer_completed`.

## Verdict latency

The committed latency fixture contains 20 runs for the allowlisted vendor `kestrelridge-shop.testnet`. All 20 returned ALLOW. These runs used tier 1 checks only. Tier 2 is skipped for an allowlisted receiver by design in `gate/rules.py`. The recorded p50 is 1843 ms and p95 is 2300 ms. The aged payee runs are not committed because they measured a superseded code path.

The first payment, invoice 1, is transaction `9XVcMjuS...ekVsyK`; it has no stored evidence document.

## Refused receipt

The lookalike payment without a verdict was refused as `REFUSED:no_verdict`. Its failed receipt is [recorded on testnet](https://testnet.nearblocks.io/txns/6kfxEFCR2uuoiM6cpj8CmGAfmRjKTYNtFq4igkv48qK7). The request targeted `kestrelridge-sh0p.testnet`; the stored record is `fixtures/rpc/attacks/01-no-verdict.json`.

The unknown payee payment was refused as `REFUSED:unknown_payee_cap` on 2 November. Its failed receipt is [recorded on testnet](https://testnet.nearblocks.io/txns/H6WqJYSbwBPECSVqADRHAGA1qV1a2ictujUiBqaLe416). The request was for 2 DMT to `examples.testnet`, which is not on the allowlist, against an unknown payee cap of 1 DMT per call; the stored record is `fixtures/rpc/attacks/09-unknown-payee.json`. The service had answered ALLOW in 13477 ms, every check run and none with a signal, and its evidence document is `fixtures/evidence/9471fea5*.json` (evidence hash `9471fea5...66112f27`, verdict id `946f0003a1996939f1c81eb879d59a09`).

## Registry records and verification

The registry recorded three verdicts in transaction [`CW5TWeRyvLngggXiBY5Y7xSZTD3BPWerFtsXczJdPRhW`](https://testnet.nearblocks.io/txns/CW5TWeRyvLngggXiBY5Y7xSZTD3BPWerFtsXczJdPRhW). Its `verdict_recorded` events contain each subject, verdict, evidence hash, observed block and verdict id. The corresponding read views and unchanged evidence documents are committed locally.

| Subject | Record | Evidence hash | Verify command | Expected output |
|---|---|---|---|---|
| `kestrelridge-shop.testnet` | `fixtures/registry/testnet-vendor.json` | `7e77187d...c88bb2cc` | `python -m gate verify fixtures/registry/testnet-vendor.json fixtures/evidence/7e77187d*.json` | Both evidence comparisons print `ok`, then `verified` |
| `kestrelridge-sh0p.testnet` | `fixtures/registry/testnet-lookalike.json` | `c5d02152...c94297b7` | `python -m gate verify fixtures/registry/testnet-lookalike.json fixtures/evidence/c5d02152*.json` | Both evidence comparisons print `ok`, then `verified` |
| `lindenvale.testnet` | `fixtures/registry/testnet-aged-payee.json` | `ff5a89a2...0db727c3` | `python -m gate verify fixtures/registry/testnet-aged-payee.json fixtures/evidence/ff5a89a2*.json` | Both evidence comparisons print `ok`, then `verified` |

Each evidence document is stored by its sha256 filename under `fixtures/evidence/`. The vendor record is ALLOW; the lookalike and the then-young aged payee records are DENY. The verify command recomputes each evidence sha256 and compares it and the verdict with the registry view. The orchestrator's recorded registry verification also checked the vendor ALLOW signature against the live verifier key (`fixtures/runs/verify-20261101T210132Z.txt`).

## EVM preflight

The EVM leg is a preflight before signing; no transaction was sent on any chain. The recorded 1Click quote has `dry: true`, and the recorded GoPlus response marks the GoPlus field `"stealing_attack"` as `1`. The source files are under `fixtures/intents/` and `fixtures/goplus/`.

## Source notes

- NearBlocks supplies testnet explorer and account data under its free tier: up to 6 requests a minute and 333 a day, personal use, with a 24 hour cache. The footer attribution is `Data: NearBlocks`.
- GoPlus supplies the address security response used by the EVM preflight.
- 1Click supplies the recorded dry quote. No deposit was made.
- FastNEAR supplies the read-only transaction and transfer data used by the collector.
- `mainnet.neardata.xyz` supplies read-only NEAR data.
- The feed follower reads finalized heights sequentially from `mainnet.neardata.xyz`, paces requests with a 0.4 second minimum interval, sends the FastNEAR key from `FASTNEAR_API_KEY_FILE` as a Bearer header when that file holds one (keyless otherwise), and stores state and event blocks under `FEED_STATE_DIR`. A follower event's `block_fixture` names a file under `<state dir>/event-blocks/`, or a key of a feed snapshot's `event_blocks`. A feed snapshot, when recorded, holds the state without its fresh accounts, the event documents and the blocks that raised them.
- The feed follower asks the FastNEAR Transfers API every 600 seconds, or at the Retry-After time (60 seconds without one) after a 429, for outgoing transfers from current incident and denylist accounts, with a minimum USD amount filter and system transfers omitted. Each window starts where the last complete one ended, so no transfer is in two windows; it reads up to 20 pages per account and window, and the follower state records a window that reached that cap and the time, status and type of the last failed poll. It reads the Bearer value from `FASTNEAR_API_KEY_FILE` at startup, keeps it in memory, and writes neither the header nor the key to fixtures or to the state. The transfer signal is disabled, with the reason in the state, when that variable is unset or names a file that cannot be read or is empty.
- The block stream reports successful contract deployments on current catalog token, infrastructure, bridge, treasury, organisation and watch accounts, including the deployed code hash.

The signature is pure Ed25519 over the sha256 digest of the canonical message; it is not Ed25519ph.
