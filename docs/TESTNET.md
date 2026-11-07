# Testnet accounts and deployment

Network: NEAR testnet. The four top level role accounts were created 29 Oct 2026 through the faucet service (10 NEAR each); the aged payee and the three sub accounts were funded by `fund-myself`. The Gate account was topped up by two transfers from the owner (6 NEAR) and the vendor (5 NEAR).

## Accounts

| Role | Account | Balance after setup (NEAR) |
|---|---|---|
| Owner | `kestrelridge-owner.testnet` | 4.0 |
| Gate | `kestrelridge-main.testnet` | 13.973 |
| Vendor | `kestrelridge-shop.testnet` | 5.0 |
| Demo counterparty, ours | `kestrelridge-mallory.testnet` | 8.992 |
| Aged payee | `lindenvale.testnet` (created 29 Oct from the demo counterparty account, 1 NEAR) | 1.0 |
| Token | `token.kestrelridge-main.testnet` | 3.001 |
| Registry | `registry.kestrelridge-main.testnet` | 2.999 |
| Recorder | `recorder.kestrelridge-main.testnet` | 1.0 |
| Reports | `reports.kestrelridge-main.testnet` (created 7 Nov from the Gate account, 3 NEAR) | 2.998 |

## Contracts

First built with `cargo near build non-reproducible-wasm` from a clean export of `main` at `89db0c6` and deployed by `scripts/setup_testnet.sh` on 29 Oct. On 2 Nov the Gate and the registry were redeployed by `scripts/redeploy.sh` (no init call, state kept) with the reproducible wasm of `res/` (source commit `eb54c8e`, `res/BUILD-gate.md`, `res/BUILD-registry.md`); the token keeps its 29 Oct build. Each `code_hash` read back from `view_account` equals base58 of sha256 of the wasm (MATCH).

| Contract | Account | code_hash | Deploy transaction |
|---|---|---|---|
| Gate | `kestrelridge-main.testnet` | `Hfdk3jGUFxQyP8wEEoejNUkZ81bE9gDmoUBfGRghR5iE` (29 Oct: `7NBhj8kM...DUW8`) | `GTHR62ER...Wik5sW` (29 Oct: `8VPBkPi2...c8EcE6`) |
| Registry | `registry.kestrelridge-main.testnet` | `3qGvVjTHVyvYVGQjJhqQGJMzmkUHwEbTCfWqHhXnW4VX` (29 Oct: `64jBn2DD...mrLU`) | `9GzMDrYe...UPBb66` (29 Oct: `4bhQViAx...ifcJU3`) |
| Test token (DMT, 6 decimals, supply 1000000 DMT to the Gate) | `token.kestrelridge-main.testnet` | `5uXqAX8C8fTupiJYwZQz1fC5voSnQsaD9xhxQ4R28rZw` | `A1To88Pg...s9dMs7` |
| Reports | `reports.kestrelridge-main.testnet` | `GFKs9k5P6raHNxN6suXJNJByjsYdXAZdGdJE5a5duZes` (MATCH, version 0.1.1, reproducible wasm of `res/reports.wasm`, source commit in `res/BUILD-reports.md`; first deployed as 0.1.0, code_hash `6QcaGeoCN7QXe2ZV8VAL4ZHTRYWTURJYQmdDD2tBqBpN`) | `59PSfJtQ...LzsErK` (7 Nov redeploy without an init call); first deploy `AMfe5SLG...nLCWCg` (7 Nov; account created in `Cza55oWj...gcDwKF`) |

The Gate 0.2.0 wasm in `res/gate.wasm`, code_hash `7fyhLcFm4nsof4gbfS5zN77cartyrjCBdKBtYRnSV3u9`, is built from the source commit in `res/BUILD-gate.md` and not yet deployed: the chain runs the 0.1.0 code_hash `Hfdk3jGUFxQyP8wEEoejNUkZ81bE9gDmoUBfGRghR5iE`, kept as `res/previous/gate.wasm`.

Read back after the reports deploy, 7 Nov: `version` `0.1.0`, `state_version` 1, `totals` 0 reports and 0 disputes, and `subject_summary` of `kestrelridge-shop.testnet` and of `kestrelridge-mallory.testnet` returns the six fields with zero counts, null ids and `deny` false. The owner `kestrelridge-owner.testnet` is the only moderator. No report has been filed yet. The verdict service consults this contract when `REPORTS_ACCOUNT_ID` is set.

Redeployed 7 Nov as version 0.1.1, which drops the dispute cap, layout preserving and without an init call (`docs/UPGRADES.md` rule 2), transaction `59PSfJtQ...LzsErK`. Read back after the redeploy: `version` `0.1.1`, `state_version` 1, `totals` 0 reports and 0 disputes, `subject_summary` of `kestrelridge-sh0p.testnet` and of `kestrelridge-mallory.testnet` with zero counts, null ids and `deny` false, and the account's code checksum equal to `res/reports.wasm.sha256`. Rollback is `res/previous/reports.wasm` redeployed with `EXPECT_VERSION=0.1.0`.

Read back after setup: deployed Gate and registry `version` `0.1.0` (`res/BUILD-gate.md`, `res/BUILD-registry.md`). Gate source is now `0.2.0` (`contracts/gate/Cargo.toml`), which adds queue pruning; the deployed 0.1.0 Gate has no queue pruning. Registry source remains `0.1.0` (`contracts/registry/Cargo.toml`). Gate policy `max_ttl_ms` 300000, not paused, empty denylist; one verifier key, no previous key; the registry recorder is `recorder.kestrelridge-main.testnet`; token storage registered for the Gate, the vendor and the aged payee (0.00129 NEAR each). At that point the Gate held one full access key and no agent key; `register_agent` added the agent's `request_transfer` key on 29 Oct (the Gate scan of 2 Nov, `fixtures/scan/`, shows both).

The agent registration, 29 Oct, transaction `HNVuBL2N...K2wmdu` (`fixtures/runs/register_agent-20261029T193356Z.txt`): label `demo agent`, key allowance 1 NEAR, caps on the token of 10 DMT per call and 50 DMT per 24 h window, unknown payee caps of 1 DMT per call and 1 DMT per 24 h window. `set_allowlist` then set its allowlist to the vendor (transaction `CtiKJbks...rW9Ecg`); the recorded `get_agent` answer (`fixtures/demo/policy.json`) shows the label, the caps and the allowlist `["kestrelridge-shop.testnet"]`.

## Payments

| Payment | Verdict | Transaction |
|---|---|---|
| 10 DMT to the vendor, memo `invoice 1`, 29 Oct | ALLOW | `9XVcMjuS...ekVsyK` |
| 10 DMT to the vendor, memo `invoice 2`, 1 Nov | ALLOW, 1794 ms | `7agsRsZg...Gpvdwn` |
| 10 DMT to the vendor, memo `invoice 3`, 2 Nov, after the redeploy | ALLOW, 1754 ms | `DU55HxkN...zXNwGh` |
| 10 DMT to the vendor, memo `invoice 4`, 2 Nov, request id 5, paid with `python -m gate pay --verdict-file` | ALLOW signed with the live key by the MCP server (`GATE_LIVE_SIGNING=1`) | `AADLzYbq...8d7rTJ` |

Each emitted `transfer_scheduled`, the token's `ft_transfer` and `transfer_completed`. The evidence document of the first payment was lost with a scratch copy, so the registry holds no record of it; the second, third and fourth are recorded (see Registry).

## Attack runs, 29 Oct to 5 Nov

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
| unknown-payee, 2 Nov (2 DMT to `examples.testnet`, not on the allowlist, ALLOW from the service, unknown payee cap 1 DMT per call) | Gate, `REFUSED:unknown_payee_cap` | `H6WqJYSb...aLe416` |
| injected, 5 Nov (scripted agent; testnet verdict DENY) | Gate, `REFUSED:no_verdict` | `8M5NqTdA...r6yVMS` |

The unknown payee attack ran on 2 Nov with `examples.testnet` as the receiver, an account with no contract code created 1825 days before the run, because the accounts the team controls, `lindenvale.testnet` among them, were created 29 Oct and were under 7 days old. Its verdict was ALLOW in 13477 ms, every check run and none with a signal (evidence `9471fea5...66112f27`); nothing was sent to it. Before the attack the Gate registered `examples.testnet` on the token with `storage_deposit`, 0.00129 NEAR from the Gate's balance, as setup did for the vendor and the aged payee (transaction `Bd62DYEp...7gtz49`, `fixtures/runs/storage_deposit-examples-20261102T134415Z.txt`). The live verdict for the lookalike is DENY: a lookalike of the vendor, not on the allowlist, not registered on the token.

On 5 Nov the scripted agent followed an instruction planted in a vendor message and submitted a transfer to the collector account named in the Rhea Lend incident report without a verdict. The Gate refused it with `REFUSED:no_verdict` in transaction `8M5NqTdA...r6yVMS`; the testnet verdict's first signal was that the account does not exist on testnet.

## Registry

`record_batch` by the recorder, 1 Nov, transaction `CW5TWeRy...JdPRhW`: three records (the second payment's ALLOW for the vendor, the lookalike's DENY, the aged payee's DENY as younger than 7 days). `total_records` read 3 that day, and `python -m gate verify` passed for each `latest` view against its evidence document, the ALLOW also against its signature with the live verifier key (`fixtures/runs/verify-20261101T210132Z.txt`).

`record_batch` by the recorder, 3 Nov, transaction `HmxrLXBe...j5Ugyo`: three more records, in this order: the third payment's ALLOW for the vendor (evidence `cfca1a63...44a13ed6`), the fourth payment's ALLOW for the vendor (evidence `4b99f7ed...fc84bf29`) and the unknown payee attack's ALLOW for `examples.testnet` (evidence `9471fea5...66112f27`). The Gate refused that transfer on its unknown payee cap, so this record holds the verdict service's answer, not a payment; `is_allowed` for `examples.testnet` answers allow while the record is younger than the caller's `max_age_ms`. Then 43 `record_label` transactions by the recorder, one per label: the 43 rows of category `incident` in `intel/labels.jsonl`, the Rhea incident rows (15 subjects; provenance `onchain` 28, `third_party` 15; chain `near` 42, `eth` 1), each with its assertion id as `assertion_hash`. In label order: `FzfXoR2v...HVbcmq`, `2JPDfjgr...yZkPHs`, `8MKxy7n1...mx9FY8`, `4YwbsFGd...fhXGVC`, `EKQHzcDs...VYE1Dz`, `HdX2qoEH...skqM7g`, `4pC9tY2v...qgU6YZ`, `Ghga7Hy6...w7HdBC`, `C31oZRbK...47iQLx`, `D6qq2q3K...7Sjpxu`, `6BsLay64...mGYaju`, `DD88NiWp...Y2TqJ1`, `4j4A3afa...xNAR19`, `2r9QDL9r...fo8YEk`, `CEL5o7kD...YHG15K`, `G27Z4LgQ...JQHpFm`, `4sefuD4E...VnNWgM`, `HJvxJQdX...pXB8Cu`, `JCAP3HfF...cYHoDb`, `DVLpEBn2...HtTh63`, `D5jKAE6n...4WeQs3`, `A3i9ieuZ...SS85FD`, `CiRTvmdu...arf52Q`, `6J2WfAgv...Vh6vk5`, `AgFNbuao...qx4S5j`, `CZdpJ8Py...EmcQRZ`, `FQg9jYCh...aboU8t`, `6e8iDDf5...noM8tW`, `Dsqov4K7...J1hwUu`, `EwMPNCiS...6Hes23`, `H3wP7CFU...MDKvoo`, `D36Vvaka...NjQuKp`, `3ocgsWJ8...g7hsqs`, `F2cc7CPj...E2ZJEZ`, `8j3uDcQf...UFAwZt`, `9xjBmAUw...PoGgDi`, `Fgtf26Um...g7z1KT`, `5sEd98Lt...6bwreP`, `4isQNoSe...e8FexH`, `8EsLMoXg...Zc6zqp`, `2a9tWPmF...wuoc6V`, `HcNYr4sp...ZgWKSa`, `BgdRcnAg...Az1FEM`.

Read back after the run (run record `totals-20261103T143037Z.txt`): `total_records` 6, `total_subjects` 4, `total_labels` 43. The six records are the second, third and fourth payments' ALLOWs, the `examples.testnet` ALLOW, the lookalike's DENY and the aged payee's DENY. Two payments have no record: the first payment and the replay attack's first send (`HWXiWZKc...d8jiRu`, 10 DMT to the vendor). Their evidence documents were not kept, so no record can be built for them.

The `latest` views after the run are in `fixtures/registry/testnet-latest-<subject>.json` for `kestrelridge-shop`, `examples`, `kestrelridge-sh0p` and `lindenvale`. The vendor's is now the fourth payment (observed block 275907012): the third was recorded first in the same batch and was replaced at once, so it shows only in its `verdict_recorded` event (`fixtures/registry/event-087cff-20261103.json`). The lookalike's and the aged payee's views are unchanged since 1 Nov. The 43 `label_recorded` events, as logged, are in `fixtures/registry/labels-20261103.json`. `python -m gate verify` passes for the vendor's and `examples.testnet`'s `latest` views and for the third payment's event, each against its evidence document and against its Verdict's signature with the live verifier key, and for the lookalike's and the aged payee's views against their evidence documents (`fixtures/runs/verify-o2-20261103.txt`). As on 1 Nov, the Verdict files the signature checks read are kept outside the repository.

`record_batch` by the recorder, 5 Nov, transaction `6nVYg8BK...fUt3Ay`: one record, the DENY for the collector account named in the Rhea Lend incident report, `31ac7a27...e724a540` (evidence `5371ec35...bdaab7a7`). Its observed block 276319568 is the block of the verdict's own `view_account` read of the collector, which answered that the account does not exist on testnet; the refused submission landed in block 276319582. The record was recorded at 2026-11-05 09:25:56 UTC.

Read back after the 5 Nov record (`fixtures/runs/registry-readback-20261105.txt`): `total_records` 7, `total_subjects` 5, `total_labels` 43. The collector's latest view is `fixtures/registry/testnet-latest-rhea-incident-collector.json`. `python -m gate verify` passes for this view against the live evidence document, `fixtures/evidence/5371ec35*.json` (`fixtures/runs/verify-injected-20261105.txt`).

## Latency

The allowlisted vendor's tier 1 latency is measured over 20 runs: all returned ALLOW, with p50 1843 ms and p95 2300 ms. The tier 2 latency is measured over 20 runs on 4 Nov for `examples.testnet`, off the allowlist (the unknown payee attack's receiver above): all returned ALLOW, with p50 16219 ms and p95 17484 ms (`fixtures/latency/verdict-runs-tier2.json`); the aged payee, created 29 Oct, was under 7 days old that day. The aged payee runs of 1 Nov measured a superseded code path; before the step down, 9 of 20 read Unknown (see `docs/SPIKES.md` S8).
