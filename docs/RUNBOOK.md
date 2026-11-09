# Runbook

The operator's procedures for the testnet contracts. Every procedure says when to use it, the command and its dry run, the read backs that prove it worked, and how to undo it. Keys and their holders are in `docs/KEYS.md`; accounts and the recorded transactions are in `docs/TESTNET.md`; the script variables are in `scripts/README.md`; the redeploy and migration rules are in `docs/UPGRADES.md`.

Every script this runbook uses calls `require_testnet` and so refuses any network but testnet (`scripts/lib.sh:104-110`); check a script for that call before using it outside this runbook. These scripts print each command line before it runs it (`scripts/lib.sh:123-129`), and with `DRY_RUN=1` or `--dry-run` prints the plan and stops there: no key file is read and no transaction is sent (`scripts/lib.sh:7-15`, `:123-129`). A dry run still checks the required variables and every argument, so a dry run that exits 2 names the value to fix. Run the dry run first, every time, and read every printed line before the real run.

## Current implementation state

The verdict service reads `subject_summary` and applies `intel/reports.py` when `REPORTS_ACCOUNT_ID` is set (`gate/rules.py`, `docs/LADDER.md`). Reports source is 0.1.3, and testnet runs deployed Reports version `0.1.3` (`contracts/reports/Cargo.toml:3`, `res/BUILD-reports.md:7`, `fixtures/runs/reports-013-deploy-20261109.txt`). The 9 Nov threshold demo shows the owner's vote leaving report 1 pending, mod2's second vote accepting it, and mod3 resolving the dispute; the final summary is all zeros and `totals` is 2 reports and 2 disputes (`fixtures/runs/reports-threshold-demo-20261109.txt`). Current Gate source is 0.2.2, built but not deployed, and testnet runs Gate 0.2.1 (`res/BUILD-gate.md`). Gate and Reports have sandbox round-trip tests and previous artifacts; the Registry has no previous artifact (`docs/UPGRADES.md:16`).

The read backs below use the near-cli-rs view form `near contract call-function as-read-only <account> <method> json-args '<json>' network-config testnet now` (the form `scripts/lib.sh:189` uses), written here as "view `<method>`". Every transaction below goes through `call_transaction` (`scripts/lib.sh:252-255`) and ends with the summary block (`scripts/lib.sh:154-173`), which prints the account summary with its `code_hash` and the shortened transaction hashes.

## Deploy

When: a contract account that holds no contract state yet. `scripts/setup_testnet.sh` creates the token, registry and recorder sub accounts if missing and initializes the token, the Gate and the registry; it skips any contract that already answers its view (`scripts/setup_testnet.sh:93-98`, `:110-115`, `:123-128`). `scripts/deploy_reports.sh <reports_account> <wasm> <moderator[,moderator...]>` deploys and initializes the reports contract with `OWNER_ACCOUNT_ID` as its owner (`scripts/deploy_reports.sh:29-37`).

Command and dry run:

```
DRY_RUN=1 bash scripts/setup_testnet.sh
bash scripts/setup_testnet.sh
DRY_RUN=1 bash scripts/deploy_reports.sh <reports_account> <wasm> <moderator[,moderator...]>
bash scripts/deploy_reports.sh <reports_account> <wasm> <moderator[,moderator...]>
```

`setup_testnet.sh` checks every wasm file and that the Gate keeps at least 10 NEAR after the planned spend before it creates anything (`scripts/setup_testnet.sh:28-33,57-74`). `moderate.sh` and `dispute.sh --resolve` sign as `OWNER_ACCOUNT_ID`, so the reports moderator list passed to `deploy_reports.sh` must include the owner account. `report.sh` signs as `REPORTER_ACCOUNT_ID` and opening a dispute signs as `DISPUTER_ACCOUNT_ID`; each falls back to the owner with a warning when unset (`scripts/report.sh:8-10`, `scripts/dispute.sh:49-51`), and the contract refuses a moderator who moderates their own report.

Read backs:

- Each account's `code_hash`, printed by the summary block (`scripts/lib.sh:154-173`), equals the `code_hash` that `python -m gate codehash <wasm>` prints for the deployed file; `sha256sum -c res/gate.wasm.sha256 res/registry.wasm.sha256` checks the committed artifacts first (README, Try it offline).
- Gate: view `version`, view `get_policy` (`max_ttl_ms`, `paused` false, empty denylist, `contracts/gate/src/policy.rs:250-252`), view `get_verifier_keys` (one current key, `previous` null, `policy.rs:254-259`). This is the read back `docs/TESTNET.md:40` records after setup.
- Registry: view `version` and view `get_recorder` (`contracts/registry/src/lib.rs:243`, `:247`).
- Token: view `ft_metadata`, and `storage_balance_of` for the Gate, the vendor and the aged payee.
- Reports: views `version`, `report_deposit` and `totals` (`contracts/reports/src/lib.rs:267-269`, `:271-273`, `:824-829`).
- `python -m gate scan <gate account>` with `GATE_FIXTURES_MODE=off` lists the Gate's access keys and its verifier keys.

Undo: an init cannot be undone. A wrong value the owner can set is corrected with its owner method (`set_verifier_keys`, `set_max_ttl`, `set_recorder`, `set_moderators`; `docs/KEYS.md`). A wrong `owner_id` cannot be corrected, because no contract has a method that changes it; deploy to a new account instead.

## Redeploy, layout preserving

When: new code whose root struct and every stored type are unchanged, so the existing state reads back under the new code. On 2 Nov this ran for the Gate and the registry with the reproducible wasm then in `res/` (the historical Gate 0.1.0 source is `eb54c8e`; `docs/TESTNET.md:21`). A change to the stored layout is a migration, below; never run it through this procedure. `docs/UPGRADES.md` records the sandbox round trip required by rule 2, now completed for Gate and Reports (`fixtures/runs/cargo-gate-0.2.1-sandbox-20261107.txt`, `fixtures/runs/cargo-reports-0.1.1-20261107.txt`).

Command and dry run (`<contract>` is `gate`, `registry` or `reports`, `scripts/redeploy.sh:28-33`; `<version>` is the crate's new `version` and `<code_hash>` the base58 value `python -m gate codehash <wasm>` prints and `res/BUILD-<crate>.md` records):

```
DRY_RUN=1 GATE_STATE_FROZEN=1 EXPECT_VERSION=<version> EXPECT_CODE_HASH=<code_hash> bash scripts/redeploy.sh <contract> <wasm>
GATE_STATE_FROZEN=1 EXPECT_VERSION=<version> EXPECT_CODE_HASH=<code_hash> bash scripts/redeploy.sh <contract> <wasm>
```

The reports contract has a `version` view, so `EXPECT_VERSION` applies to `reports` as it does to the Gate and the registry. The reports account is `REPORTS_ACCOUNT_ID`, or `reports.$GATE_ACCOUNT_ID` when that is unset (`scripts/redeploy.sh:31`); source and testnet now run Reports version `0.1.3` (`contracts/reports/Cargo.toml:3`, `res/BUILD-reports.md:7`, `fixtures/runs/reports-013-deploy-20261109.txt`). After 4 Nov the script refuses without `GATE_STATE_FROZEN=1` (`scripts/redeploy.sh:34-35`). It deploys without an init call, signed by the contract account's own key (`scripts/redeploy.sh:38`, `:83`). The dry run prints the version view, the deploy line, the wasm sha256 and the codehash line, then one `would assert` line for each of `EXPECT_VERSION` and `EXPECT_CODE_HASH` that is set, and exits 0 (`scripts/redeploy.sh:85-90`, `:100-107`).

Read backs: the script prints `version before` and `version after`, the account summary with its `code_hash`, the wasm sha256 and the `python -m gate codehash` line (`scripts/redeploy.sh:77-98`). With `EXPECT_VERSION` set it exits 1 unless the `version` view after the deploy returns it (`scripts/redeploy.sh:109-119`); with `EXPECT_CODE_HASH` set it reads the account's `code_hash` from `view_account` up to five times, 2 s apart, and exits 1 unless it matches (`scripts/redeploy.sh:120-136`). Each passing assertion prints an `asserted:` line. Then read the state back: for the Gate, view `get_policy`, view `get_verifier_keys` and view `get_agent` for the agent key, each unchanged; for the registry, view `total_records`, `total_subjects`, `total_labels` and `get_recorder`, each unchanged (the 5 Nov values are in `docs/TESTNET.md:88`); for the reports contract, view `totals` unchanged.

Undo: rollback, below.

## Migrate

When: new code that changes the stored layout (`docs/UPGRADES.md:10`).

Command and dry run:

```
DRY_RUN=1 GATE_STATE_FROZEN=1 EXPECT_VERSION=<version> EXPECT_CODE_HASH=<code_hash> bash scripts/redeploy.sh <contract> <wasm> --migrate
GATE_STATE_FROZEN=1 EXPECT_VERSION=<version> EXPECT_CODE_HASH=<code_hash> bash scripts/redeploy.sh <contract> <wasm> --migrate
```

`--migrate` (`scripts/redeploy.sh:5-13`) deploys with an init call to `migrate` with args `{}`, 100 Tgas and no deposit, through `deploy_with_init` (`scripts/redeploy.sh:81`, `scripts/lib.sh:247-250`), signed by the contract account's key. The assertions and read backs are those of the redeploy above.

Not usable today: no contract on main has a `migrate` method (no `#[init(ignore_state)]` and no `fn migrate` exists in `contracts/`), so the init call would fail. A layout change first needs the migration `docs/UPGRADES.md` rule 4 describes, rehearsed in the sandbox on state written by the previous artifact. Until one exists, do not deploy a layout change to an account that holds state.

## Rollback

When: a layout preserving redeploy went wrong and the previous code must run again over the same state. Rollback is valid only after a layout preserving redeploy; after a migration it is not, unless a reverse migration was written and rehearsed (`docs/UPGRADES.md:9-10`).

Command: the redeploy procedure above, with `res/previous/<crate>.wasm` as `<wasm>` and the previous version and code hash as `EXPECT_VERSION` and `EXPECT_CODE_HASH` (`docs/UPGRADES.md:62-65`), and the same read backs: the state reads back unchanged.

Gate and Reports have previous artifacts in `res/previous/` and sandbox round-trip tests (`docs/UPGRADES.md:16`). Reports 0.1.2 is the previous artifact and rollback target; testnet now runs Reports 0.1.3 (`res/BUILD-reports.md:7`, `fixtures/runs/reports-013-deploy-20261109.txt`). Gate source is 0.2.2, built but not deployed; testnet runs Gate 0.2.1, whose artifact is preserved in `res/previous/gate.wasm` as rollback for 0.2.2 (`res/BUILD-gate.md:5-7`). The Registry has no previous artifact, so no Registry rollback is available (`res/BUILD-registry.md`, `docs/UPGRADES.md:16`).

## Rotate the verifier

When: on a schedule, or when the operator machine changes. When the verifier key may be exposed, use the exposed key procedure below instead.

What the script does: `scripts/rotate_verifier.sh <new_verifier_public_key>` needs `OWNER_ACCOUNT_ID`, `GATE_ACCOUNT_ID`, `NEAR_KEYS_DIR` and `VERIFIER_PUB` (the key now current, `scripts/rotate_verifier.sh:7`), takes `MAX_TTL_MS` with a default of 300000 (`:8`), refuses a new key equal to `VERIFIER_PUB` (`:28-31`), and calls `set_verifier_keys` with the new key as `current`, `VERIFIER_PUB` as `previous` and `previous_valid_until_ms` set to the operator machine's clock plus `MAX_TTL_MS` (`:45-68`), signed by the owner with 1 yoctoNEAR. `MAX_TTL_MS` must equal the Gate's `max_ttl_ms` (view `get_policy`; 300000 on testnet, `docs/TESTNET.md:40`).

What the grace slot does not do: the Gate picks the key by the verdict's `key_id`: `cur` is checked against the current key, and `prev` against the previous key only while the block time is at most `previous_valid_until_ms` (`contracts/gate/src/policy.rs:167-178`, `contracts/gate/src/verdict.rs:72-84`). Every signer in `gate/signer.py` defaults to `key_id` `cur` (`gate/signer.py:50`, `:61`, `:81`), and no shipped command passes another (`gate/__main__.py:63`, `gate/agent.py:76`). So an ALLOW signed with the old key before the rotation carries `cur`, is checked against the new current key after the rotation lands, and is refused with `REFUSED:bad_signature` (`verdict.rs:94-95`), not accepted through the previous slot. Likewise, from the moment the rotation lands until every verdict service has the new key file, every ALLOW those services sign is refused the same way. No funds move on a refused ALLOW; the agent requests a fresh verdict. The previous slot only serves a verdict signed with `key_id` `prev`, which no shipped command produces, but which anyone holding the old key can produce, since `key_id` is a field of the signed message (`gate/canon.py:3-4`).

Procedure:

1. Create the new key pair on the operator machine, outside the repository. Only its public key goes into the command.
2. Stop every verdict service that reads `VERIFIER_KEY_FILE` (`python -m gate verdict --live`, `python -m gate pay`, the MCP server with its live conditions, README Trust model), so no new ALLOW is signed with the old key.
3. Wait one `max_ttl_ms` (300000 ms, 5 minutes) so every ALLOW already signed is either submitted or expired (`verdict.rs:66-68`).
4. Read view `get_verifier_keys` and keep its output, which is the undo value, then run the dry run and the rotation:

```
DRY_RUN=1 VERIFIER_PUB=<current public key> bash scripts/rotate_verifier.sh <new public key>
VERIFIER_PUB=<current public key> bash scripts/rotate_verifier.sh <new public key>
```

5. Point `VERIFIER_KEY_FILE` at the new key file for every verdict service, set `VERIFIER_PUB` to the new public key before any later rotation (`scripts/README.md`), and start the services again.

Skipping steps 2 and 3 moves no funds, but it refuses every ALLOW in flight with `REFUSED:bad_signature`; the loss is bounded by the verdict lifetime, `max_ttl_ms`.

Read backs: view `get_verifier_keys` shows the new key as `current` (hex), and `previous` as the old key with `valid_until_ms` equal to the value the run printed (`contracts/gate/src/policy.rs:254-259`); the transaction carries the `verifier_keys_updated` event (`policy.rs:173-179`); `python -m gate scan <gate account>` with `GATE_FIXTURES_MODE=off` prints the verifier keys; one live ALLOW paid through `python -m gate pay` proves the service and the Gate agree.

Undo: the same procedure with the keys swapped, `VERIFIER_PUB=<new public key> bash scripts/rotate_verifier.sh <old public key>`, and `VERIFIER_KEY_FILE` pointed back. Do not undo a rotation made because the old key was exposed.

### When the verifier key may be exposed

`rotate_verifier.sh` always keeps the old key as `previous` for one `MAX_TTL_MS`, so for that long a verdict signed with the exposed key and `key_id` `prev` still verifies. No script sets `previous` to null. A paused Gate refuses every `request_transfer` before it checks a verdict (`contracts/gate/src/lib.rs:149-156`, the verdict check is at `:194-207`), so the pause covers the grace period:

```
DRY_RUN=1 bash scripts/set_paused.sh true
bash scripts/set_paused.sh true
DRY_RUN=1 VERIFIER_PUB=<exposed public key> bash scripts/rotate_verifier.sh <new public key>
VERIFIER_PUB=<exposed public key> bash scripts/rotate_verifier.sh <new public key>
```

Then point `VERIFIER_KEY_FILE` at the new key file and set `VERIFIER_PUB` to the new key. Read view `get_verifier_keys` and keep the Gate paused until `previous.valid_until_ms` is in the past, with one minute added for the difference between the operator machine's clock and the block time. Then resume:

```
DRY_RUN=1 bash scripts/set_paused.sh false
bash scripts/set_paused.sh false
```

Read backs: view `get_policy` shows `paused` true during the wait and false after; view `get_verifier_keys` as above. A submitted verdict from the exposed key still needs the agent key, because `request_transfer` must come from the Gate's own account (`contracts/gate/src/lib.rs:157-160`), and stays bounded by the caps.

## Rotate the recorder

When: on a schedule, before any mainnet registry (the recorder key leaves the build machine first, `docs/KEYS.md`, Rules), or when the recorder key may be exposed.

Two paths:

- Same account, new key (keeps every script working). No script in `scripts/` changes an account's keys, so these are near-cli commands, in the forms `docs/SPIKES.md:127-128` records. First read and save the current key list: `near account list-keys recorder.<gate account> network-config testnet now`. Add the new full access key, signed with the current recorder key: `near account add-key recorder.<gate account> grant-full-access use-manually-provided-public-key ed25519:<new public key> network-config testnet sign-with-access-key-file <current recorder key file> send`. Read the key list again: it now shows both keys. Move the new key file under `NEAR_KEYS_DIR` as `recorder.<gate account>.json`, then prove the new key signs with `DRY_RUN=1 bash scripts/record_verdicts.sh <args.json>` followed by one small real `record_batch` and its read back (`total_records` grew). Only then delete the old key, signed with the new one: `near account delete-keys recorder.<gate account> public-keys ed25519:<old public key> network-config testnet sign-with-access-key-file <new recorder key file> send`. Read the key list a last time: only the new key remains. near-cli has no dry run for these calls; the list-keys read before and after each step is the check. `scripts/record_verdicts.sh` derives the recorder as `recorder.$GATE_ACCOUNT_ID` (`scripts/record_verdicts.sh:21`), so this path needs no change anywhere else.
- New account: `scripts/rotate_recorder.sh <new_recorder_account>` calls `set_recorder` with args `{"recorder_id":"<new recorder account>"}` on `registry.$GATE_ACCOUNT_ID`, signed by the owner with 1 yoctoNEAR (`scripts/rotate_recorder.sh:7`, `:15`, `:25`; `contracts/registry/src/lib.rs:192`). After it, `scripts/record_verdicts.sh` still signs as `recorder.$GATE_ACCOUNT_ID` (`scripts/record_verdicts.sh:21`), which the registry now refuses; until that script takes the new account, use this path only when recording can stop.

Command and dry run for the new account path:

```
DRY_RUN=1 bash scripts/rotate_recorder.sh <new_recorder_account>
bash scripts/rotate_recorder.sh <new_recorder_account>
```

Read view `get_recorder` first and keep its output. `DRY_RUN=1 bash scripts/record_verdicts.sh <args.json>` shows which account the next recording signs as.

Read backs: view `get_recorder` names the expected account, the transaction carries the `recorder_updated` event with the previous and current accounts (`contracts/registry/src/lib.rs:198-203`), and `near account list-keys <recorder account> network-config testnet now` lists only the new key. The next `record_batch` lands and `total_records` grows by its count.

Undo: `bash scripts/rotate_recorder.sh <saved recorder account>`, or add the old key again. Do not undo a rotation made because the old key was exposed.

## Kill switch

When: the agent key or the verdict service may be compromised, or as a drill.

Command and dry run (`COLD_ACCOUNT_ID` set and storage registered on the token, `scripts/kill_switch.sh:7`, `:12-15`):

```
DRY_RUN=1 bash scripts/kill_switch.sh <agent_pub>
bash scripts/kill_switch.sh <agent_pub>
```

It runs `set_paused` true (`scripts/kill_switch.sh:123`), `revoke_agent` when `get_agent` finds an entry (`scripts/kill_switch.sh:137-146`), then `withdraw` of the Gate's whole token balance to the cold account (`scripts/kill_switch.sh:168-180`). On a failure after the pause it prints the partial state and the manual withdraw command (`scripts/kill_switch.sh:31-54`).

Read backs: view `get_policy` shows `paused` true; view `get_agent` for the key is null; `near account list-keys <gate account> network-config testnet now` shows no function call key for `request_transfer`, which is the only proof the kill switch is complete (`scripts/README.md:46`); `ft_balance_of` the Gate is 0 and the cold account holds the moved amount.

Undo (a drill; after a real compromise register a new agent key, never the revoked one): the order in `scripts/README.md:48-51`: send the tokens back from the cold account, `register_agent.sh`, `set_allowlist.sh`, and last `set_paused.sh false`. One agent can be stopped alone with `set_agent_enabled` false (`contracts/gate/src/policy.rs:127-136`).

## File and moderate a report

When: a report is filed against a subject, and later, when it is pending and a moderator has read its evidence. Reports version 0.1.3 is deployed on testnet (`res/BUILD-reports.md:7`, `fixtures/runs/reports-013-deploy-20261109.txt`).

File: `scripts/report.sh <reports_account> <subject> <kind> <outcome> <evidence_sha256> <evidence_url>` calls `submit_report` signed by `REPORTER_ACCOUNT_ID`, falling back to `OWNER_ACCOUNT_ID` with a warning when unset (`scripts/report.sh:8-10,65`). It checks `kind` and `outcome` against `intel/record_enums.json` (`:28-39`), the hash as 64 lowercase hex and the URL as `https://` of at most 512 bytes (`:26-27`, `scripts/lib.sh:48-68`), and attaches `REPORT_DEPOSIT` read from current source at run time (`contracts/reports/src/lib.rs:9`, `scripts/report.sh:53-65`), 0.03 NEAR. Testnet Reports 0.1.3 requires that exact deposit; another amount is refused with `REFUSED:bad_deposit` (`contracts/reports/src/lib.rs:853-858`, `fixtures/runs/reports-013-deploy-20261109.txt`).

```
DRY_RUN=1 bash scripts/report.sh <reports_account> <subject> <kind> <outcome> <evidence_sha256> <evidence_url>
bash scripts/report.sh <reports_account> <subject> <kind> <outcome> <evidence_sha256> <evidence_url>
```

Moderate: `scripts/moderate.sh <reports_account> <report_id> <accept|reject> <reason_sha256>` calls `moderate` with args `{"id":<report id>,"accept":<true|false>,"reason_sha256":"<hash>"}`, signed by `OWNER_ACCOUNT_ID` with exactly 1 yoctoNEAR (`scripts/moderate.sh:8-35`; `contracts/reports/src/lib.rs:425-427`). The owner must be on the moderator list (`assert_moderator`, `contracts/reports/src/lib.rs:846-851`).

```
DRY_RUN=1 bash scripts/moderate.sh <reports_account> <report_id> <accept|reject> <reason_sha256>
bash scripts/moderate.sh <reports_account> <report_id> <accept|reject> <reason_sha256>
```

Before the real run read view `get_report` for the id (`contracts/reports/src/lib.rs:766-768`): its state is `pending`, or the call fails with `REFUSED:not_pending` (`:433`), and its evidence is the one read.

Read backs: `report.sh` returns the new report id, and view `totals` grows by one report; after `moderate.sh`, view `get_report` shows `accepted` or `rejected`, the moderator and the reason hash; the transaction carries `report_moderated` (`docs/REPORTS.md`); view `subject_summary` for the subject shows the counts moved (`contracts/reports/src/lib.rs:808-817`).

Undo: a moderation is final; the only state changes are listed in `docs/REPORTS.md:34`. A wrong acceptance is corrected by a dispute and its resolution with `overturn`, below. A wrong rejection is corrected only by a new report.

## Open and resolve a dispute

When: a report is accepted and its acceptance is challenged; then, when the dispute is open and a moderator has read both evidence documents.

Open: `scripts/dispute.sh <reports_account> <report_id> <evidence_sha256> <evidence_url>` calls `dispute` signed by `DISPUTER_ACCOUNT_ID`, falling back to `OWNER_ACCOUNT_ID` with a warning when unset (`scripts/dispute.sh:49-53,80`), and attaches `REPORT_DEPOSIT` read from current source at run time (`contracts/reports/src/lib.rs:9`, `scripts/dispute.sh:66-80`). Testnet Reports 0.1.3 requires the exact 0.03 NEAR deposit; another amount is refused with `REFUSED:bad_deposit` (`contracts/reports/src/lib.rs:598-604`, `fixtures/runs/reports-013-deploy-20261109.txt`). Anyone can open a dispute on chain.

```
DRY_RUN=1 bash scripts/dispute.sh <reports_account> <report_id> <evidence_sha256> <evidence_url>
bash scripts/dispute.sh <reports_account> <report_id> <evidence_sha256> <evidence_url>
```

Resolve: `scripts/dispute.sh <reports_account> --resolve <dispute_id> <uphold|overturn> <reason_sha256>` calls `resolve_dispute` with args `{"dispute_id":<dispute id>,"overturn":<true|false>,"reason_sha256":"<hash>"}`, signed by `OWNER_ACCOUNT_ID` with exactly 1 yoctoNEAR (`scripts/dispute.sh:15-40`; `contracts/reports/src/lib.rs:662-665`). The owner must be on the moderator list (`assert_moderator`, `contracts/reports/src/lib.rs:846-851`).

```
DRY_RUN=1 bash scripts/dispute.sh <reports_account> --resolve <dispute_id> <uphold|overturn> <reason_sha256>
bash scripts/dispute.sh <reports_account> --resolve <dispute_id> <uphold|overturn> <reason_sha256>
```

Before resolving read view `get_dispute` (`contracts/reports/src/lib.rs:777-779`): it is open, or the call fails with `REFUSED:dispute_closed` (`:668`); and view `get_report` for its report: it is `contested`, or the call fails with `REFUSED:not_contested` (`:672-674`).

Read backs: after opening, view `get_report` shows `contested` and the transaction carries `report_disputed`; after resolving, view `get_dispute` shows it closed, view `get_report` shows `accepted` (upheld) or `overturned`, the transaction carries `dispute_resolved`, and view `subject_summary` shows `deny`, `latest_accepted_id` and `latest_deny_id` as expected (`docs/REPORTS.md:31`).

Undo: testnet runs Reports 0.1.3, which has no lifetime dispute cap (`contracts/reports/src/lib.rs:598-660`, `res/BUILD-reports.md:7`, `fixtures/runs/reports-013-deploy-20261109.txt`). One dispute may be open at a time, and each costs the exact deposit. An overturn is final.

## Live report beat

When: use this for the report life demonstration on testnet. The 9 Nov threshold run records the owner vote leaving report 1 pending, mod2's second vote accepting it, a dispute from lindenvale, mod3's overturn resolution call, an all-zero final subject summary and totals of 2 reports and 2 disputes (`fixtures/runs/reports-threshold-demo-20261109.txt`). No payment is sent: the verdict reads judge a payment and never send it. The report and dispute calls attach the exact 0.03 NEAR deposit required by Reports 0.1.3 (`contracts/reports/src/lib.rs:9`, `fixtures/runs/reports-013-deploy-20261109.txt`).

The verdict service reads the reports contract when `REPORTS_ACCOUNT_ID` is set. `scripts/live_beat.sh` checks that this switch is set before it runs the report and verdict sequence; pending reports are a count only and have no verdict effect.

`scripts/live_beat.sh` takes no arguments (`--dry-run` is accepted by `parse_dry_run`, `scripts/lib.sh:7-15`). Its variables: `OWNER_ACCOUNT_ID` (the configured moderator used for the beat's approval and resolution calls; the recorded run used scoped overrides for mod2's approval and mod3's resolution), `REPORTER_ACCOUNT_ID` (signs the report; must differ from the moderator account, which cannot moderate its own report), `DISPUTER_ACCOUNT_ID` (signs the dispute; must differ from the reporter and the moderator account), `REPORTS_ACCOUNT_ID` (`reports.kestrelridge-main.testnet`), `GATE_ACCOUNT_ID` (`kestrelridge-main.testnet`, for the verdict's `--gate` and `token.<gate>`), `COUNTERPARTY_ACCOUNT_ID` (the subject, `kestrelridge-mallory.testnet`; must differ from the three signers), `NEAR_KEYS_DIR`, and optional `EVIDENCE_CHECK` (`fetch` by default, or `local`; any other value exits 2, `scripts/live_beat.sh:37-41`). It checks them all before any command and exits 2 naming the one to fix. It files through `scripts/report.sh`, `scripts/moderate.sh` and `scripts/dispute.sh`, so each transaction is the one their sections above describe, with its deposit.

The script makes one approval call and then expects the report to be accepted (`scripts/live_beat.sh:369-376`). At the current two-approval threshold, one vote leaves it pending, so the unchanged script cannot complete. Before another run, either set the threshold to 1 and configure one moderator, or update the beat to collect a second approval and resolve the dispute with a moderator who did not vote to accept. With multiple configured moderators, the contract bars an accepting moderator and any accept voter from resolving the dispute (`contracts/reports/src/lib.rs:662-764`).

The evidence is `docs/demo/synthetic-report.md`, whose first line says the report is synthetic and the subject is ours. The script computes its sha256 from the file at run time and builds the URL `https://raw.githubusercontent.com/Ridwannurudeen/shieldbot-near/<HEAD>/docs/demo/synthetic-report.md` (`scripts/live_beat.sh:43-53`). The real run refuses when the file is not committed unchanged at HEAD (`:54-61`). By default (`EVIDENCE_CHECK=fetch`), step a fetches the URL before any transaction and refuses unless it serves the same sha256 (`scripts/live_beat.sh:310-332`): push HEAD to that repository first. While the repository is private before the public flip, that raw URL cannot be fetched yet: push HEAD to `origin/main`, run `git fetch` first so `refs/remotes/origin/main` is current locally (the script itself makes no git network call), and set `EVIDENCE_CHECK=local`. Step a then checks `git merge-base --is-ancestor <HEAD> refs/remotes/origin/main` and checks that `git show <HEAD>:docs/demo/synthetic-report.md` has the same sha256 as the working file, exiting 2 otherwise (`scripts/live_beat.sh:292-309`), and the run log records `evidence_check: local` and a note that the URL is readable once the repository is public (`scripts/live_beat.sh:278-281`). On flip day, once the repository is public, run `python scripts/public_flip_check.py --fetch-evidence`, which reads `evidence_url` and `evidence_sha256` from every `fixtures/runs/live-beat-*.txt`, fetches each URL with a 30 s timeout, and exits 1 naming the log if a fetch fails or a hash differs (`check_evidence`, `scripts/public_flip_check.py:113-138`, `:141-156`). The moderation and resolution reasons are fixed texts in the script; both texts and their sha256 are written at the top of the run log.

Before the public flip (`git fetch` first, then local evidence check):

```
git fetch origin
DRY_RUN=1 EVIDENCE_CHECK=local bash scripts/live_beat.sh
EVIDENCE_CHECK=local bash scripts/live_beat.sh
```

Runs after the public flip (fetch mode):

```
DRY_RUN=1 bash scripts/live_beat.sh
bash scripts/live_beat.sh
```

On flip day, to verify the recorded runs, run only the check; never run the beat again for this purpose, because after an overturn the subject summary is zero again and a second run files a second report and dispute:

```
python scripts/public_flip_check.py --fetch-evidence
```

A real run that exits 2 at step a has sent nothing but has already written a partial `fixtures/runs/live-beat-*.txt`; delete that log before the next run so the flip check reads only completed runs.

The dry run prints every command and every read in order and stops: no near call, no verdict run, no run log. It shows the report and dispute ids as 0; the real run reads them from `totals` before each filing (`totals.reports` is the next report id, `contracts/reports/src/lib.rs:824-829`) and stops with exit 1 unless the count grew by exactly one.

Read backs, each appended to `fixtures/runs/live-beat-<UTC timestamp>.txt` (ed25519 signature text removed and 128 hex strings shortened to first and last 8), and each compared with what it should read; the verdicts are `python3 -m gate verdict` for 1 DMT to the subject with `GATE_FIXTURES_MODE=off` and `REPORTS_ACCOUNT_ID` set, signed only with the TEST key, written to a temporary directory that is removed at exit:

| Step | `subject_summary` | `get_report` | Verdict `community_report` detail |
|---|---|---|---|
| a. before | all zero, ids null, `deny` false (else exit 2, nothing sent) | | `no community report; pending 0` (else exit 2, nothing sent) |
| b. `report.sh` theft near_miss | | `pending`, the reporter as submitter, the evidence hash and URL | |
| c. pending | `pending` 1, `deny` false | | `1 pending report(s) await moderation; no effect`; the outcome is whatever the other checks say |
| d. `moderate.sh` accept | | `accepted` | |
| e. accepted | `accepted` 1, `latest_accepted_id` and `latest_deny_id` the id, `deny` true | | `deny report <id> is accepted; 0 under dispute; pending 0`, outcome DENY |
| f. `dispute.sh` as the disputer | `contested` 1, `deny` still true | `contested` | `deny report <id> is accepted; 1 under dispute; pending 0`, outcome DENY |
| g. `dispute.sh --resolve` overturn | all zero, ids null, `deny` false | `overturned` | `no community report; pending 0` |

A read back that differs is logged as `MISMATCH` and the run continues to the overturn, then exits 1. A failed transaction or read stops the run; once anything was sent the script prints the step, the report and dispute ids, and what to finish by hand (a dispute and its resolution with `overturn`, in the sections above).

Undo: none needed. An overturned report is final (`docs/REPORTS.md`), it no longer counts in the subject's summary, and nothing else changed; the two deposits stay in the contract as for any report and dispute.

## The reports off switch

When: the reports contract gives a wrong answer, or its reads fail and turn verdicts Unknown.

The verdict service reads `REPORTS_ACCOUNT_ID` for the reports check (`gate/rules.py`, `gate/http.py`). To disable it, unset the variable in every verdict service environment and restart those services. When it is unset, no `subject_summary` call is made and no `community_report` check is added to a verdict.

Dry run: one verdict for a known payee with the variable unset and `GATE_FIXTURES_MODE=off`, `python -m gate verdict --kind ft --token <token> --receiver <account> --amount <base units> --gate <gate account>` (README, Integrate).

Read back: the verdict's `checks` hold no `community_report` entry. Nothing on chain changes, and the reports contract keeps its rows.

Undo: set `REPORTS_ACCOUNT_ID` again and restart.

## Owner withdraw escape

When: funds must leave the Gate outside the kill switch: the NEAR the kill switch leaves in place (`scripts/kill_switch.sh:181`), a token other than the test token, or a partial amount.

The owner method is `withdraw(asset, receiver_id, amount)` (`contracts/gate/src/policy.rs:231-244`) with 1 yoctoNEAR. No script in `scripts/` sends it alone; the hand call has the shape of the manual command `scripts/kill_switch.sh:41-51` prints, with args `{"asset":{"Ft":"<token account>"},"receiver_id":"<account>","amount":"<base units>"}` for a token, or `{"asset":"Near","receiver_id":"<account>","amount":"<yoctoNEAR>"}` for NEAR. A token receiver must be storage registered on the token. The Gate's NEAR stakes its own storage (`scripts/README.md:46`), so leave that much in place.

The command, for a token (the same form `scripts/kill_switch.sh:41-51` prints):

```
near contract call-function as-transaction <gate account> withdraw json-args '{"asset":{"Ft":"<token account>"},"receiver_id":"<account>","amount":"<base units>"}' prepaid-gas '100 Tgas' attached-deposit '1 yoctoNEAR' sign-as <owner account> network-config testnet sign-with-access-key-file <owner key file> send
```

For NEAR, the json-args are `'{"asset":"Near","receiver_id":"<account>","amount":"<yoctoNEAR>"}'` and every other argument is the same.

Dry run: near-cli has none for this call. Before it, read and save the balances: `near contract call-function as-read-only <token account> ft_balance_of json-args '{"account_id":"<gate account>"}' network-config testnet now` for the Gate and for the receiver, or `near account view-account-summary <gate account> network-config testnet now` for NEAR. Check the receiver is storage registered on the token: `near contract call-function as-read-only <token account> storage_balance_of json-args '{"account_id":"<account>"}' network-config testnet now` is not null.

Read backs: the Gate's balance fell by the amount and the receiver's rose by it; for a token, the token's `ft_transfer` receipt in the transaction succeeded.

Undo: the receiver sends the amount back: for a token, `ft_transfer` to the Gate as in `scripts/README.md:48`; for NEAR, a plain transfer to the Gate account.
