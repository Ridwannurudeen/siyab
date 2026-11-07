# Mainnet registry deploy script review - 2026-11-07

Scope: `scripts/deploy_registry_mainnet.sh` and the `scripts/lib.sh` helpers it calls. The review checked each guard and assertion against `docs/REGISTRY.md:48-50`. No script defect was found, so the script itself was not changed. Every documented refusal and assertion below passes source review; tests marked "POSIX pty" are present but skip on this Windows host because they require a controlling `/dev/tty`.

| Refusal or assertion | Script/helper lines | Matching doc sentence | Test coverage | Result |
|---|---|---|---|---|
| Require the mainnet network selector. | `deploy_registry_mainnet.sh:22`; `lib.sh:112-116` | `docs/REGISTRY.md:48`: "The script refuses (exit 2) unless `NETWORK=mainnet` and `MAINNET_DEPLOY_OK=1` are both set." | `test_refuses_without_network_mainnet` | PASS |
| Require the explicit mainnet approval marker. | `deploy_registry_mainnet.sh:22`; `lib.sh:117-120` | `docs/REGISTRY.md:48`: same sentence; helper error also requires all gates and owner approval. | `test_refuses_without_mainnet_deploy_ok` | PASS |
| Require the key directory before deriving the signer key path. | `deploy_registry_mainnet.sh:23`; `lib.sh:17-25`; `lib.sh:150-152` | `docs/REGISTRY.md:50`: the signer path is `$NEAR_KEYS_DIR/<registry_account>.json`. | `test_refuses_without_near_keys_dir` | PASS |
| Require exactly four positional arguments. | `deploy_registry_mainnet.sh:24-31` | Not called out separately; the documented invocation at `docs/REGISTRY.md:37` has four arguments. | `test_refuses_wrong_arity` | PASS |
| Require all three accounts to be named lowercase `.near` accounts, at most 64 characters. | `deploy_registry_mainnet.sh:34-42` | `docs/REGISTRY.md:48`: "The three accounts must be named accounts ending in `.near`, so an implicit account is refused." | `test_refuses_implicit_and_non_near_accounts` | PASS |
| Require recorder to differ from owner. | `deploy_registry_mainnet.sh:43-46` | `docs/REGISTRY.md:48`: "the recorder must differ from the owner." | `test_refuses_recorder_equal_to_owner` | PASS |
| Require `EXPECT_VERSION` and `EXPECT_CODE_HASH` when `REDEPLOY=1`. | `deploy_registry_mainnet.sh:47-49`; `lib.sh:17-25` | `docs/REGISTRY.md:50`: "`REDEPLOY=1` requires `EXPECT_VERSION` and `EXPECT_CODE_HASH`." | `test_redeploy_requires_both_expected_values` | PASS |
| Require the wasm file to exist. | `deploy_registry_mainnet.sh:51-58` | Not called out separately; the invocation at `docs/REGISTRY.md:37` names `<wasm>`. | `test_refuses_a_missing_wasm` | PASS |
| Require `python -m gate codehash` to return a valid base58 32-byte code hash. | `deploy_registry_mainnet.sh:60-78` | `docs/REGISTRY.md:48`: the script prints the expected code hash from `python -m gate codehash`. | `test_refuses_a_missing_code_hash_from_codehash_command`; `test_refuses_a_nonempty_malformed_code_hash_from_codehash_command` | PASS |
| Require wasm SHA-256 to equal the recorded digest. | `deploy_registry_mainnet.sh:79-83` | `docs/REGISTRY.md:48`: "Before any near call it computes the wasm's sha256 and refuses unless it equals the first field of `res/registry.wasm.sha256`." | `test_refuses_a_wasm_whose_sha256_does_not_match`; `test_dry_run_prints_provenance_and_the_read_back_assertions` | PASS |
| Require a 40-hex `Source commit:` line in the registry build record. | `deploy_registry_mainnet.sh:84-88` | `docs/REGISTRY.md:48`: the same sentence says `res/BUILD-registry.md` must have a 40-hex source commit line. | `test_refuses_a_build_record_without_a_40_hex_source_commit`; `test_dry_run_prints_provenance_and_the_read_back_assertions` | PASS |
| Require `EXPECT_CODE_HASH` to match this wasm for a redeploy. | `deploy_registry_mainnet.sh:92-95` | `docs/REGISTRY.md:50`: "refuses (exit 2) before any near call unless `EXPECT_CODE_HASH` equals the wasm's code_hash." | `test_redeploy_refuses_an_expected_code_hash_that_differs_from_the_wasm` | PASS |
| Require the typed registry account name to match before a live run proceeds. | `deploy_registry_mainnet.sh:97-107` | `docs/REGISTRY.md:48`: "Outside a dry run it prints a banner and reads the registry account name from the terminal, and stops unless it matches." | `test_live_guard_refuses_wrong_confirmation`; matching confirmation is exercised by the live-deploy tests below (POSIX pty). | PASS; pty tests skip on this host |
| Require the registry account to exist; report `UNKNOWN_ACCOUNT` as a mainnet missing account. | `deploy_registry_mainnet.sh:111-126`; `gate/rpc.py:180-189` (`code_hash` delegates to `view_account`) | `docs/REGISTRY.md:50`: "The registry account must already exist" and the named not-found refusal. | `test_an_unknown_account_stops_with_one_line`; `test_live_deploy_refuses_a_missing_registry_account` (POSIX pty) | PASS; integrated pty test skips on this host |
| Refuse existing code unless `REDEPLOY=1`. | `deploy_registry_mainnet.sh:155-170` | `docs/REGISTRY.md:50`: "it refuses an account that holds code unless `REDEPLOY=1`." | `test_live_guard_refuses_existing_code_without_redeploy` (POSIX pty) | PASS; pty test skips on this host |
| Refuse `REDEPLOY=1` when the account has no code. | `deploy_registry_mainnet.sh:171-174` | `docs/REGISTRY.md:50`: "`REDEPLOY=1` ... deploys without an init call, keeping the state." The script also requires pre-existing state. | `test_live_redeploy_refuses_an_empty_registry` (POSIX pty) | PASS; pty test skips on this host |
| On a first deploy, initialize with owner and recorder; on redeploy, omit initialization and retain state. | `deploy_registry_mainnet.sh:177-189`; `lib.sh:247-250` | `docs/REGISTRY.md:50`: first deploy uses `new` with both accounts; `REDEPLOY=1` deploys "without an init call, keeping the state." | `test_dry_run_prints_the_exact_deploy_line`; `test_dry_run_redeploy_deploys_without_an_init_call` | PASS |
| After deploy, require `get_recorder` to be readable and equal the requested recorder. | `deploy_registry_mainnet.sh:206-215`; `parse_view` at `132-153` | `docs/REGISTRY.md:50`: "After the deploy it asserts that `get_recorder` returns the recorder account." | `test_live_first_deploy_asserts_recorder_and_code_hash`; `test_live_first_deploy_refuses_a_different_recorder` (POSIX pty) | PASS; pty tests skip on this host |
| On redeploy, require the read-back version to equal `EXPECT_VERSION`. | `deploy_registry_mainnet.sh:216-229` | `docs/REGISTRY.md:50`: "asserts `version` against `EXPECT_VERSION` on a redeploy." | `test_live_redeploy_refuses_a_version_mismatch` (POSIX pty); `test_dry_run_redeploy_deploys_without_an_init_call` | PASS; live pty test skips on this host |
| Poll post-deploy `view_account` and require the code hash to equal the wasm hash or `EXPECT_CODE_HASH`. | `deploy_registry_mainnet.sh:230-244`; `read_code_hash` at `111-126` | `docs/REGISTRY.md:50`: "the account's code_hash equals the wasm's (or `EXPECT_CODE_HASH`)." | `test_live_first_deploy_asserts_recorder_and_code_hash`; `test_live_first_deploy_refuses_a_code_hash_mismatch` (POSIX pty) | PASS; pty tests skip on this host |
| Dry run prints planned commands/assertions and makes no `near` call. | `deploy_registry_mainnet.sh:156-163,191-203`; `lib.sh:7-15,123-148,154-173` | `docs/REGISTRY.md:50`: "`DRY_RUN=1` or `--dry-run` prints every command and assertion and makes no near call." | `test_dry_run_prints_provenance_and_the_read_back_assertions`; `test_dry_run_needs_no_confirmation`; `test_dry_run_flag_works_like_the_variable` | PASS; separate stubbed invocation below also returned `near stub called: False` |
| Print a summary without querying NEAR in dry-run mode. | `deploy_registry_mainnet.sh:191-203`; `lib.sh:154-173` | `docs/REGISTRY.md:50`: dry run makes no near call. | `test_dry_run_prints_provenance_and_the_read_back_assertions`; stubbed invocation below | PASS |

The shared helpers were reviewed at their call sites: `parse_dry_run` preserves non-flag arguments (`scripts/lib.sh:7-15`); `require_vars` checks required environment values (`17-25`); `require_mainnet_deploy_ok` applies both mainnet guards (`112-121`); `run` prints commands and returns before invoking them in dry-run mode (`123-148`); `account_key` constructs the registry signer path (`150-152`); `summary_block` avoids NEAR calls in dry-run mode (`154-173`); and `deploy_with_init` builds the initializer call (`247-250`). `json_string` is not called by this script.

The complete `tests.test_registry_mainnet` run reported `Ran 32 tests` and `OK (skipped=9)`. The nine skips are the terminal-dependent tests that require POSIX pty support; their test cases remain in the suite. No mainnet or other network call was made.

## Stubbed dry-run output

The script was run once with `DRY_RUN=1`, `NETWORK=mainnet`, `MAINNET_DEPLOY_OK=1`, and a `near` stub at the front of `PATH`. The stub writes a marker and exits 99 if invoked. The 64-hex SHA-256 below is shortened to its first and last eight characters.

```text
source commit: eb54c8e11c2916201af1d13ce7b92e1c8c8c473b
wasm sha256: 2a154191...f0ec0cfa (equals res/registry.wasm.sha256)
expected code_hash: 3qGvVjTHVyvYVGQjJhqQGJMzmkUHwEbTCfWqHhXnW4VX

MAINNET registry deploy to registry.example.near
Run this only when all six registry mainnet gates in docs/REGISTRY.md are met and the owner approves.
  owner: owner.example.near
  recorder: recorder.example.near
  redeploy: 0

would check: code_hash of registry.example.near from view_account on https://free.rpc.fastnear.com equals the empty code hash 11111111111111111111111111111111
would refuse if registry.example.near holds code, since REDEPLOY is not 1
+ near contract deploy registry.example.near use-file <checkout>/res/registry.wasm with-init-call new json-args \{\"owner_id\":\"owner.example.near\"\,\"recorder_id\":\"recorder.example.near\"\} prepaid-gas 100.0\ Tgas attached-deposit 0\ NEAR network-config mainnet sign-with-access-key-file <tmp>\\keys/registry.example.near.json send
+ near contract call-function as-read-only registry.example.near get_recorder json-args \{\} network-config mainnet now
+ near contract call-function as-read-only registry.example.near version json-args \{\} network-config mainnet now
would assert: get_recorder of registry.example.near equals recorder.example.near
would print: version of registry.example.near
would assert: code_hash of registry.example.near equals 3qGvVjTHVyvYVGQjJhqQGJMzmkUHwEbTCfWqHhXnW4VX
owner: not read back, the registry has no owner view

Summary
  network: mainnet
  account: registry.example.near
  code_hash: unavailable during dry run
  transaction_hashes: unavailable during dry run
near stub called: False
```
