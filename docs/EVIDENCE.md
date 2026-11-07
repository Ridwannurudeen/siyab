# Recorded testnet evidence

## Allowed payment

The invoice 2 payment sent 10 DMT to `kestrelridge-shop.testnet` on 1 November. Transaction [`7agsRsZgeRm2vhK1LXug2qRQ2qudbAMjsLncuxGpvdwn`](https://testnet.nearblocks.io/txns/7agsRsZgeRm2vhK1LXug2qRQ2qudbAMjsLncuxGpvdwn) completed with `SuccessValue`.

The Gate emitted `EVENT_JSON` `transfer_scheduled` with request id 3, verdict id `1b29052381ecf3fc1937b36db2539678` and evidence hash `7e77187d...c88bb2cc`. The token receipt emitted `EVENT_JSON` `ft_transfer` from `kestrelridge-main.testnet` to `kestrelridge-shop.testnet`, amount `10000000`, memo `invoice 2`. The transaction also contains `transfer_completed`.

The first payment, invoice 1, is transaction `9XVcMjuS...ekVsyK`; it has no stored evidence document.

## Verdict latency

The committed latency fixture `fixtures/latency/verdict-runs.json` contains 20 runs for the allowlisted vendor `kestrelridge-shop.testnet`. All 20 returned ALLOW. These runs used tier 1 checks only. Tier 2 is skipped for an allowlisted receiver by design in `gate/rules.py`. The recorded p50 is 1843 ms and p95 is 2300 ms. The aged payee runs of 1 November are not committed because they measured a superseded code path. The counts block reads this file for its latency line; its second latency line, for a payee off the allowlist, reads `fixtures/latency/verdict-runs-tier2.json`.

The committed fixture `fixtures/latency/verdict-runs-tier2.json` contains 20 runs of 7 November (UTC) for `examples.testnet`, the payee off the allowlist that the unknown payee attack used (`docs/TESTNET.md`), recorded with `scripts/record_latency.py` on the archive first order. All 20 returned ALLOW through tier 1 and tier 2 checks, the account age dated from the access key nonce by an archival read; no run read NearBlocks. The recorded p50 is 2148 ms and p95 is 2765 ms. Each run read testnet RPC and the archival RPC with the policy the Gate's `get_agent` returned, and signed its ALLOW with the TEST key, which the testnet Gate does not trust; nothing was sent. The 20 runs ran in one process 3 s apart. The earlier 20 runs of 4 November, on the previous NearBlocks first order, measured p50 16219 ms and p95 17484 ms: each made two NearBlocks requests, the account keys and the creation transaction, which the client starts at least 10 s apart (`NEARBLOCKS_MIN_INTERVAL_S` in `gate/rpc.py`).

The counts block reads `fixtures/latency/verdict-runs-tier2.json` and the optional `fixtures/latency/verdict-runs-tier2-fallback.json`, which is not recorded: NearBlocks retired its v1 API on testnet on 5 November 2026, so on testnet a verdict that needs the NearBlocks fallback reads Unknown today.

`scripts/record_latency.py` writes either fixture from new live testnet verdicts, each row the receiver, run, verdict, `latency_ms` of the run's evidence document and the path its checks show, each ALLOW signed only with the TEST key and nothing written when a run fails: `GATE_FIXTURES_MODE=off python scripts/record_latency.py --receiver kestrelridge-shop.testnet --runs 20 --out fixtures/latency/verdict-runs.json --replace` for the allowlisted payee and `GATE_FIXTURES_MODE=off python scripts/record_latency.py --receiver examples.testnet --runs 20 --out fixtures/latency/verdict-runs-tier2.json --replace` for the payee off the allowlist.

## Receiver cohorts

The receiver lines of the counts block are a replay of the recorded mainnet receiver set, not testnet evidence: the second splits the verdicts by cohort with `cohort_line` in `intel/__main__.py`, the function `python -m intel receivers` prints. Replay regeneration changed 80 of the 84 judgement files only in `account_age.detail` and `dust_funded.detail`; verdicts, check statuses, the eight Unknown check statuses and counts did not change. Thirty `account_age` checks are dated by archival RPC without a NearBlocks request. The other 54 replay their recorded NearBlocks answers from 4 to 7 November behind a recorded miss of the archival access key list, then use the NearBlocks fallback. Each locked contract of the organisation cohort passes `account_age` on one archival `view_account` read at a block at least 7 days back, recorded with its block and access key list under `fixtures/receivers/rpc/` (`fixtures/receivers/judgements/`, `docs/SPIKES.md` S8).

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

- NearBlocks supplies explorer and account data. Its free plan is for personal use, and its terms prohibit third party apps from interacting with the service without prior written consent; none was sought or given. The source is a fallback whose terms are under review. The footer attribution is `Data: NearBlocks`.
- GoPlus supplies the address security response used by the EVM preflight.
- 1Click supplies the recorded dry quote. No deposit was made.
- FastNEAR supplies the RPC, archival RPC and transfer reads used here. It publishes no terms of service or rate limits for those interfaces; this dependency is unverified on those two points.
- `mainnet.neardata.xyz` supplies read-only NEAR data.
- The feed follower reads finalized heights sequentially from `mainnet.neardata.xyz`. It takes the final height's block from its read of the final block instead of fetching that height again, and once it reaches the final height it waits 1 second before it reads the final block again, so a follower that keeps up makes about one request per height. It paces keyed requests with a 0.1 second minimum interval and keyless requests with a 0.4 second minimum interval, sends the FastNEAR key from `FASTNEAR_API_KEY_FILE` as a Bearer header when that file holds one (keyless otherwise), and stores state and event blocks under `FEED_STATE_DIR`. It stamps `updated_at` in its state at every height and at every read of the final height while it waits there, and, once its run has done either, at the end of every transfer poll; a run that has done neither, such as a restart on a refused key or one whose first block fetch fails, never moves it. The read only API's `/feed` route and live feed page say the follower stopped, with its last update time and the lag unknown, once that stamp is more than 1200 seconds old; a state without one is judged by `last_time`, the time of the last height it walked, else by `started_at`, when the follower first started, and never by the state file's modification time, which the transfer poll rewrites. A follower event's `block_fixture` names a file under `<state dir>/event-blocks/`, or a key of a feed snapshot's `event_blocks`. A feed snapshot holds the state without its fresh accounts, the event documents and the blocks that raised them. The committed snapshot `intel/feed/follow-snapshot.json` was copied from the hosted follower's state on 4 Nov: following since 2026-11-03T18:55:31Z, 64697 blocks up to height 222975380 (2026-11-04T05:22:06Z), 102 skipped heights and 0 events; the counts block's feed line reads it.
- The feed follower asks the FastNEAR Transfers API every 600 seconds, or at the Retry-After time (60 seconds without one, 600 at most) after a 429, with 0.4 seconds between requests, for outgoing transfers from current incident and denylist accounts, with a minimum USD amount filter and system transfers omitted. The API reports an outbound transfer with a negative USD amount; the follower keeps a row when that amount is at least 1000 USD in magnitude, publishes the magnitude with the direction "outbound", and counts the rows it cannot read, such as one without a USD amount, in the state. Each window starts where the last complete one ended, so no transfer is in two windows; it reads up to 20 pages per account and window, and the follower state records a window that reached that cap and the time, status and type of the last failed poll. It reads the Bearer value from `FASTNEAR_API_KEY_FILE` at startup, keeps it in memory, and writes neither the header nor the key to fixtures or to the state. The transfer signal is disabled, with the reason in the state, when that variable is unset or names a file that cannot be read or is empty.
- The block stream reports successful contract deployments on current catalog token, infrastructure, bridge, treasury, organisation and watch accounts, including the deployed code hash.

The signature is pure Ed25519 over the sha256 digest of the canonical message; it is not Ed25519ph.
