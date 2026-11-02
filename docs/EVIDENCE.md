# Recorded testnet evidence

## Allowed payment

The invoice 2 payment sent 10 DMT to `kestrelridge-shop.testnet` on 1 November. Transaction [`7agsRsZgeRm2vhK1LXug2qRQ2qudbAMjsLncuxGpvdwn`](https://testnet.nearblocks.io/txns/7agsRsZgeRm2vhK1LXug2qRQ2qudbAMjsLncuxGpvdwn) completed with `SuccessValue`.

The Gate emitted `EVENT_JSON` `transfer_scheduled` with request id 3, verdict id `1b29052381ecf3fc1937b36db2539678` and evidence hash `7e77187d...c88bb2cc`. The token receipt emitted `EVENT_JSON` `ft_transfer` from `kestrelridge-main.testnet` to `kestrelridge-shop.testnet`, amount `10000000`, memo `invoice 2`. The transaction also contains `transfer_completed`.

## Verdict latency

The committed latency fixture contains 20 runs for the allowlisted vendor `kestrelridge-shop.testnet`. All 20 returned ALLOW. These runs used tier 1 checks only. Tier 2 is skipped for an allowlisted receiver by design in `gate/rules.py`. The recorded p50 is 1843 ms and p95 is 2300 ms. The aged payee runs are not committed because they measured a superseded code path.

The first payment, invoice 1, is transaction `9XVcMjuS...ekVsyK`; it has no stored evidence document.

## Refused receipt

The lookalike payment without a verdict was refused as `REFUSED:no_verdict`. Its failed receipt is [recorded on testnet](https://testnet.nearblocks.io/txns/6kfxEFCR2uuoiM6cpj8CmGAfmRjKTYNtFq4igkv48qK7). The request targeted `kestrelridge-sh0p.testnet`; the stored record is `fixtures/rpc/attacks/01-no-verdict.json`.

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

The signature is pure Ed25519 over the sha256 digest of the canonical message; it is not Ed25519ph.
