# Testnet accounts and deployment

Network: NEAR testnet. The four top level role accounts were created 29 Oct 2026 through the faucet service (10 NEAR each); the aged payee and the three sub accounts were funded by `fund-myself`. The Gate account was topped up by two transfers from the owner (6 NEAR) and the vendor (5 NEAR).

## Accounts

| Role | Account | Balance after setup (NEAR) |
|---|---|---|
| Owner | `kestrelridge-owner.testnet` | 4.0 |
| Gate | `kestrelridge-main.testnet` | 13.973 |
| Vendor | `kestrelridge-shop.testnet` | 5.0 |
| Attacker | `kestrelridge-mallory.testnet` | 8.992 |
| Aged payee | `lindenvale.testnet` (created 29 Oct from the attacker account, 1 NEAR) | 1.0 |
| Token | `token.kestrelridge-main.testnet` | 3.001 |
| Registry | `registry.kestrelridge-main.testnet` | 2.999 |
| Recorder | `recorder.kestrelridge-main.testnet` | 1.0 |

## Contracts

First built with `cargo near build non-reproducible-wasm` from a clean export of `main` at `89db0c6` and deployed by `scripts/setup_testnet.sh` on 29 Oct. On 2 Nov the Gate and the registry were redeployed by `scripts/redeploy.sh` (no init call, state kept) with the reproducible wasm of `res/` (source commit `eb54c8e`, `res/BUILD.md`); the token keeps its 29 Oct build. Each `code_hash` read back from `view_account` equals base58 of sha256 of the wasm (MATCH).

| Contract | Account | code_hash | Deploy transaction |
|---|---|---|---|
| Gate | `kestrelridge-main.testnet` | `Hfdk3jGUFxQyP8wEEoejNUkZ81bE9gDmoUBfGRghR5iE` (29 Oct: `7NBhj8kM...DUW8`) | `GTHR62ER...Wik5sW` (29 Oct: `8VPBkPi2...c8EcE6`) |
| Registry | `registry.kestrelridge-main.testnet` | `3qGvVjTHVyvYVGQjJhqQGJMzmkUHwEbTCfWqHhXnW4VX` (29 Oct: `64jBn2DD...mrLU`) | `9GzMDrYe...UPBb66` (29 Oct: `4bhQViAx...ifcJU3`) |
| Test token (DMT, 6 decimals, supply 1000000 DMT to the Gate) | `token.kestrelridge-main.testnet` | `5uXqAX8C8fTupiJYwZQz1fC5voSnQsaD9xhxQ4R28rZw` | `A1To88Pg...s9dMs7` |

Read back after setup: Gate and registry `version` `0.1.0`; Gate policy `max_ttl_ms` 300000, not paused, empty denylist; one verifier key, no previous key; the registry recorder is `recorder.kestrelridge-main.testnet`; token storage registered for the Gate, the vendor and the aged payee (0.00129 NEAR each). At that point the Gate held one full access key and no agent key; `register_agent` added the agent's `request_transfer` key on 29 Oct (the Gate scan of 2 Nov, `fixtures/scan/`, shows both).

## Payments

| Payment | Verdict | Transaction |
|---|---|---|
| 10 DMT to the vendor, memo `invoice 1`, 29 Oct | ALLOW | `9XVcMjuS...ekVsyK` |
| 10 DMT to the vendor, memo `invoice 2`, 1 Nov | ALLOW, 1794 ms | `7agsRsZg...Gpvdwn` |
| 10 DMT to the vendor, memo `invoice 3`, 2 Nov, after the redeploy | ALLOW, 1754 ms | `DU55HxkN...zXNwGh` |

Each emitted `transfer_scheduled`, the token's `ft_transfer` and `transfer_completed`. The evidence document of the first payment was lost with a scratch copy, so only the second is in the registry.

## Attack run, 29 Oct

| Attack | Refused by | Transaction |
|---|---|---|
| no-verdict (to the lookalike `kestrelridge-sh0p.testnet`) | Gate, `REFUSED:no_verdict` | `6kfxEFCR...v48qK7` |
| forged-signature | Gate, `REFUSED:bad_signature` | `Gv9P2py5...rhb9A9` |
| replay (first send pays, second refused) | Gate, `REFUSED:verdict_used` | `HWXiWZKc...d8jiRu`, `2FFnYUrX...yZG6Pn` |
| expired | Gate, `REFUSED:expired` | `5KG3sPSq...bNvQ7H` |
| over-cap (11 DMT, per call cap 10) | Gate, `REFUSED:per_call_cap` | `2vsukFAz...r8nWgU` |
| callback (`on_transfer` with the agent key) | protocol, `MethodNameMismatch` | none (rejected before execution) |
| token-direct (`ft_transfer` on the token with the agent key) | protocol, `ReceiverMismatch` | none (rejected before execution) |
| service-down (no verifier key) | Gate, `REFUSED:no_verdict` | `CTm9yDGH...vt9wnk` |
| one-yocto (1 yoctoNEAR attached) | protocol, `DepositWithFunctionCall` | none (rejected before execution) |

The unknown payee attack waits until the aged payee is 7 days old: it was created 29 Oct, 18:20 UTC, and its key landed in block 275336351 (18:20:16 UTC), so the account age passes from 5 Nov, 18:20:16 UTC. The live verdict for the lookalike is DENY: a lookalike of the vendor, not on the allowlist, not registered on the token.

## Registry

`record_batch` by the recorder, 1 Nov, transaction `CW5TWeRy...JdPRhW`: three records (the second payment's ALLOW for the vendor, the lookalike's DENY, the aged payee's DENY as younger than 7 days). `total_records` reads 3, and `python -m gate verify` passes for each `latest` view against its evidence document, the ALLOW also against its signature with the live verifier key (handoff record `testnet-runs/registry/verify-20261101T210132Z.txt`).

## Latency

Live verdicts on 1 Nov: 20 for the vendor (allowlisted, all ALLOW), p50 1843 ms, p95 2300 ms; 10 for the aged payee on the superseded archival account read with the step down (all DENY), p50 6639 ms, p95 15486 ms; the extra time is the NearBlocks calls and the archival reads. The account age now comes from the oldest access key nonce (one key list read and one archival block read); its latency is not measured yet. Before the step down, 9 of 20 aged payee runs read Unknown (see `docs/SPIKES.md` S8).
