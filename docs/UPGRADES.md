# Upgrades and migrations

How a contract in this repository is redeployed or migrated, what proves the step keeps the state readable, and how it is undone. The rules cover the Gate (`contracts/gate`), the registry (`contracts/registry`) and the reports contract (`contracts/reports`). The script is `scripts/redeploy.sh`; its variables are in `scripts/README.md`.

## Rules

1. Every deploy bumps `version` in the crate's `Cargo.toml`. `scripts/redeploy.sh` asserts after the deploy that the `version` view returns `EXPECT_VERSION` when set and that the account's `code_hash` equals `EXPECT_CODE_HASH` when set, and exits 1 if either configured assertion fails.
2. A layout preserving redeploy, where the root struct and every stored type are unchanged, is deployed without an init call. It will be allowed only when a sandbox round trip proves it and `res/previous/<crate>.wasm` plus its sha256 are committed. The required round trip: deploy `res/previous/<crate>.wasm`, initialize, write state, redeploy the candidate without an init call, read the old state back, exercise one new call, redeploy the previous artifact again, read the state and make one more call. The candidate is the source build by default and the exact artifact when `<CRATE>_CANDIDATE_WASM` names it. The test must assert that the previous file's sha256 equals `res/previous/<crate>.wasm.sha256`.
3. Rollback is the previous artifact redeployed over the same state, and is claimed only for a rule 2 redeploy. Entries the candidate writes under new prefixes are harmless to the previous artifact, which does not read them (Gate 0.2.2 introduces no new prefixes and its 0.2.1 rollback uses the same prefixes). For Reports, a rollback to 0.1.2 over a report with votes but no decision leaves a plain pending report that one moderator decides alone, and the votes written under the new prefixes stay stored and unread; the sandbox round trip asserts the first.
4. A layout change ships a `#[init(ignore_state)] migrate()` that first asserts the layout it expects, then transforms the state and bumps the version. It asserts the stored `state_version` where the contract keeps one; a contract with no such field asserts the layout of the source commit its build record names, and its first migration adds the field. Reports stores `state_version: 1`. The migration runs through `scripts/redeploy.sh --migrate`, which deploys with an init call to `migrate` with args `{}` through `deploy_with_init` in `scripts/lib.sh`, signed by the contract account's key. It is rehearsed in the sandbox on state written by the previous artifact. Recovery is a tested reverse migration or a forward fix, also rehearsed in the sandbox; without one there is no rollback claim.
5. New state lives under new prefixes, and every prefix is listed in the registry below before the code that writes it is deployed.
6. The previous artifact lives at `res/previous/<crate>.wasm` with its `.sha256`, copied in the commit that swaps `res/<crate>.wasm`. That crate's `res/BUILD-<crate>.md` then names both source commits.
7. Each crate has one build record: `res/BUILD-gate.md`, `res/BUILD-registry.md`, `res/BUILD-reports.md`. Each has one `Source commit:` line (40 hex, or TBD before the first build) and one `Deployed:` line (`not deployed`, `pending`, or the transaction and MATCH). A record whose source commit is TBD has no `res/<crate>.wasm`, and a record with a source commit has the wasm and its `.sha256`; `tests/test_res.py` asserts this state for every crate. The `reproducible` job in `.github/workflows/ci.yml` runs once per crate: it reads the record, rebuilds the crate at that commit with `cargo near build reproducible-wasm` and compares the sha256 with `res/<crate>.wasm.sha256`. It skips only a crate whose record says TBD and whose wasm is absent, and fails on any other mismatch between the record and `res/`.
8. Provenance: no wasm in `res/` whose source commit is not on main, and no deployed code hash that no commit on main records. Between the commit that adds an artifact and the commit that records its MATCH, the record says `Deployed: pending`.

Gate and Reports have their rule 2 round trips in `contracts/gate/tests/sandbox.rs` and `contracts/reports/tests/sandbox.rs`, with previous artifacts in `res/previous/`; the 7 Nov runs that passed are `fixtures/runs/cargo-gate-0.2.1-sandbox-20261107.txt` (Gate 0.2.1, with its unit tests in `fixtures/runs/cargo-gate-0.2.1-unit-20261107.txt`) and `fixtures/runs/cargo-reports-0.1.1-20261107.txt` (Reports 0.1.1, `sandbox_reports_layout_preserving_round_trip`). Gate source is `0.2.2`, and testnet runs Gate `0.2.2` after the 9 Nov layout preserving redeploy per `res/BUILD-gate.md`; Reports source is `0.1.3`, and testnet runs deployed Reports version `0.1.3`, deployed 9 Nov 2026 (`fixtures/runs/reports-013-deploy-20261109.txt`, `res/BUILD-reports.md`). Version 0.1.2 remains the rollback target. A crate without both its previous artifact and sandbox round trip does not qualify under rule 2, and no rollback is claimed for it.

The 9 Nov Gate 0.2.2 exact-artifact round trip uses the preserved deployed 0.2.1 wasm and passes with the corrected queue-head wait in harness commit `9d4b6342` (`fixtures/runs/cargo-gate-0.2.2-sandbox-20261109.txt`). It checks the native memo and unknown-token refusals without a scheduled transfer, used verdict or cap debit. The reproducible artifact remains pinned to source `57860c96`, whose runtime and manifests the harness correction does not change (`res/BUILD-gate.md`).

## Prefix registry

Each storage prefix a contract writes is either initialized in `new` or defined by a named prefix constant in its `src/lib.rs`. A new collection takes a prefix that is not in this table, and the table is updated in the same commit.

| Contract | Prefix | Collection |
|---|---|---|
| Gate | `a` | `agents` |
| Gate | `d` | `denylist` |
| Gate | `v` | `used_verdicts` |
| Gate | `p` | `pending` |
| Gate | `uq:c` | used verdict expiry queue cursor, from source version `0.2.0` |
| Gate | `uq:e` | used verdict expiry queue entries, from source version `0.2.0` |
| Gate | `cg` | agent cap generations, from source version `0.2.1` |
| Gate | `pg` | pending request cap generations, from source version `0.2.1` |
| Registry | `r` | `latest` |
| Registry | `l` | `labels` |
| Reports | `r` | `reports` |
| Reports | `d` | `disputes` |
| Reports | `sc` | `subject_report_counts` |
| Reports | `si` | `subject_report_ids` |
| Reports | `pc` | `submitter_report_counts` |
| Reports | `pi` | `submitter_report_ids` |
| Reports | `pp` | `pending_by_submitter` |
| Reports | `ss` | `subject_summaries` |
| Reports | `ps` | `publisher_stats` |
| Reports | `ab` | accepting moderator per report id (`ACCEPTING_MODERATOR_PREFIX`, `contracts/reports/src/lib.rs:10`), from source version `0.1.2` |
| Reports | `mt` | approvals required, one value key, from source version `0.1.3` |
| Reports | `mv` | report moderation votes, keyed by report id, from source version `0.1.3` |

Prefixes are per account, so the same letter in two contracts does not collide.

## Commands

Set `GATE_ACCOUNT_ID`, `NEAR_KEYS_DIR` and `GATE_STATE_FROZEN=1` (required after 4 Nov). The reports account is `reports.$GATE_ACCOUNT_ID` unless `REPORTS_ACCOUNT_ID` names another. Run a dry pass first: `DRY_RUN=1` or `--dry-run` prints every near command line and each assertion it would make, and sends nothing.

A layout preserving redeploy (rule 2):

```sh
DRY_RUN=1 EXPECT_VERSION=<version> EXPECT_CODE_HASH=<code_hash> bash scripts/redeploy.sh gate res/gate.wasm
EXPECT_VERSION=<version> EXPECT_CODE_HASH=<code_hash> bash scripts/redeploy.sh gate res/gate.wasm
```

A migration (rule 4):

```sh
DRY_RUN=1 EXPECT_VERSION=<version> EXPECT_CODE_HASH=<code_hash> bash scripts/redeploy.sh gate res/gate.wasm --migrate
EXPECT_VERSION=<version> EXPECT_CODE_HASH=<code_hash> bash scripts/redeploy.sh gate res/gate.wasm --migrate
```

Replace `gate` with `registry` or `reports` and the wasm path to match. `<code_hash>` is the base58 form that `python -m gate codehash res/<crate>.wasm` prints and the build record states. A rollback (rule 3) is the first form with `res/previous/<crate>.wasm` and the previous version and code hash.
