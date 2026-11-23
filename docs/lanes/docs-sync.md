# Documentation sync, 22 Nov 2026

This lane reconciles the merged documentation plan with the current source and the owner's public run records. The work starts from the committed sign-in clock and budget fixes and enclave record on `feat/nep413-workspace`. No transaction, key read, paid completion, commit, remote change or deployment is part of this lane.

## Release scope

The hosted wording, "since 22 Nov 2026", describes the owner's release target after this branch merges and the hosted checkout serving main is deployed that day. It is not a deployment performed or observed by this lane. The enclave, attestation registry and Gate 0.3.0 remain undeployed; the committed testnet Gate artifact is 0.2.2, and the owner reports that the mainnet Gate remains paused with its agent key revoked (`res/BUILD-attest.md:5`; `res/BUILD-gate.md:7,13`; `fixtures/runs/oneclick-mainnet-20261122.txt:20`).

The 22 Nov settlement is an account transaction outside that Gate. The retained report ends ALLOW and settled, with 296020 Base USDC base units credited at block 60872158. Both quoted and actual withdrawal fees are 2400, so equality with actual amountOut supports the net reading for this run. The retained report contains receipt summaries, not the raw destination receipts and blocks needed to replay destination_receipt (`fixtures/runs/oneclick-mainnet-20261122.txt:5-22`; `tests/test_oneclick_mainnet_run.py:21-70`).

The same single FullAccess key signed the 15 Nov owner calls and the 22 Nov deposit. The owner derives a temporary file from the recovery phrase at a hidden prompt for each run and deletes that file afterwards. Deleting that file did not remove the on-chain key. These are owner-reported lifecycle facts, distinct from the signed quote checks (`fixtures/runs/gate-mainnet-20261115.txt:5,38,42`; `fixtures/runs/oneclick-mainnet-20261122.txt:5-11`).

## Plan coverage

| Plan area | Result and source |
|---|---|
| Settlement prerequisites | Added the public report copy, two quote signatures as signature_bytes, the run summary and five offline binding and summary tests. The judgement document is retained byte for byte (`tests/test_oneclick_mainnet_run.py:14-70`; `fixtures/runs/oneclick-mainnet-20261122.txt:21-22`). |
| README and claims | Distinguish preflight, local settlement, opt-in rules v3, the selected holdout, encrypted personal workspaces, reference adapters and undeployed attested source. Claims use the existing mirrored table contract (`tests/test_readme_claims.py:76-98`; `docs/CLAIMS.md`). |
| API and threat model | Document public CORS, current sign-in fields and resource bounds, encrypted envelope routes, settlement and derivation. Keep deployed payment trust separate from attested source (`docs/API.md`; `docs/THREAT-MODEL.md`). |
| Keys, coverage and workspaces | Document temporary owner files, the persistent FullAccess boundary, wallet recovery requirements, readable team records and encrypted personal contents (`docs/KEYS.md`; `docs/COVERAGE.md`; `docs/WORKSPACES.md`). |
| Operations and build records | Document proxy rollout, recovery, attested promotion and FullAccess code replacement limits. Preserve the deployed Gate record and the six pinned attestation registry inputs (`docs/RUNBOOK.md`; `docs/UPGRADES.md`; `docs/VERIFY.md`; `docs/TESTNET.md`; `docs/REGISTRY.md`; `res/BUILD-attest.md:3-9`). |
| Integration and chronology | Update MCP and reference adapter notes, append the 21 and 22 Nov chronology and refresh moved README citations (`mcp/README.md`; `integrations/langchain/README.md`; `integrations/wallet-selector/README.md`; `docs/LOG.md:103-112`; `docs/DEMO.md`; `docs/lanes/nearai.md`). |
| Scorecard and site | Regenerate the scorecard from the expanded claims and the site from renderer sources. Keep the optional footer change out. Preserve every animation and the five landing limits (`scripts/scorecard.py:318-325`; `intel/render.py`; `tests/test_landing_motion.py`). |
| OKX capture | Retain the supplied public capture. The new offline regression verifies its Ed25519 signature only with the exact callback; omitted, changed and trailing-slash callbacks fail (`tests/test_near_auth.py:NEP413VectorTest.test_okx_signature_verifies_only_with_the_exact_callback`). |

## Corrections to the plan

* Rules v3 keeps the verdict service outcome UNKNOWN and action ask; public account check receipts use ASK. The distinction is explicit (`gate/verdict.py:101-110`; `gate/account_check.py:240-256`).
* A guard can continue after a human confirms ASK. Only continuation without confirmation requires explicit ALLOW. DENY always blocks (`integrations/langchain/adapter.py:188-213`; `integrations/wallet-selector/adapter.mjs:175-202`).
* The server retains account, public key and envelope metadata alongside encrypted personal contents. "Only ciphertext" does not describe the entire database (`intel/private_workspace.py:28-36`).
* All nine refused holdout accounts already carried labels. The five Tether rows also have receiver_exists signals, so it would be inaccurate to attribute every refusal solely to a label (`docs/HOLDOUT.md:3,45-49`).
* A supplied Chain Signatures root derives one curve. A secp256k1 root gives Ethereum and Bitcoin addresses; an ed25519 root gives Solana. Live derivation also requires a domain id, predecessor and path (`gate/chainsig/__main__.py:18-50`).
* The attestation registry has no contract upgrade method, but retained FullAccess keys can still replace its code. Its configuration is immutable through its exposed methods, not through the account's keys (`docs/spec/attest-v1.md:154`; `docs/lanes/attest-registry.md:129-131`).
* The actual landing collector retains label and issuer signals under v3. The historical incident replay cited by the plan used an empty label store. The landing note names rules v1 without predicting a different result for this fixture (`intel/render.py:705-709`; `gate/rules.py:1092-1095`; `docs/lanes/rules-v3.md:41`).
* The external settlement report has no commit field and no override_reports field. The owner supplied run commit is explicitly attributed in the summary. No raw Base receipt replay is claimed (`fixtures/runs/oneclick-mainnet-20261122.txt:5,22`).
* The pinned 1Click SDK describes amountOut as net of withdrawal fees. This run supports that reading with a nonzero fee; one observation is not a guarantee for other routes (`docs/lanes/oneclick.md:139,152`).
* Current sign-in fields, clock checks and provider shares replace the older plan's generic times and budget descriptions (`intel/near_auth.py:NearAuth.issue`; `intel/near_auth.py:NearAuth.verify`; `docs/API.md`).
* Anthropic is the source default. No retained deployed provider setting was available, so the new documentation does not treat that setting as observed deployment evidence (`intel/ask.py:476-497`; `docs/API.md`).
* The legacy Intents preflight still keeps its accepted status set empty. The old global assertion that no status catalog is recorded is now false: the separate settlement path has a recorded catalog and authenticates the real quote economics (`gate/intents.py:34-39,149`; `gate/oneclick.py:42-50`; `fixtures/oneclick/api-contract.json:579-590`).
* The old Rhea account-state assertions were superseded by readings that record global contract hashes. This fixes their coverage description while retaining the default version 1 and version 3 local-code-hash limitation (`docs/COVERAGE.md:9`; `intel/labels.jsonl:394-396,3284-3286`; `gate/rules.py:976-982,1082-1087`).
* The generic settlement sender row does not grant every sender Gate owner authority. That authority belongs to the recorded siyabgate.near because it owns its Gate (`docs/KEYS.md:27`; `fixtures/runs/gate-mainnet-20261115.txt:5`).
* The mirrored personal workspace claim describes encrypted contents, since account, key and session metadata are also retained (`docs/CLAIMS.md:66`; `README.md:509`; `intel/private_workspace.py:28-36`). The build inventory citation is `tests/test_res.py:12`, not line 13 (`docs/UPGRADES.md:13`).

## Verified external sources

The sources establish the external interfaces. The run results, release target and key-file lifecycle come from the retained local record and the owner.

* [NEP-413](https://raw.githubusercontent.com/near/NEPs/master/neps/nep-0413.md): tagged Borsh payload, SHA-256 digest and FullAccess message signing.
* [OKX NEAR provider](https://web3.okx.com/onchainos/dev-docs/wallet/dapp-connect/chains/near/provider): injected provider paths and direct signed-message result. The exact callback behavior is checked by the retained signature rather than inferred from that page.
* [NEAR account RPC](https://docs.near.org/api/rpc/contracts): public account and access key views.
* [WebCrypto](https://www.w3.org/TR/webcrypto/): HKDF and AES-GCM APIs used by the browser vault.
* [NEAR Chain Signatures](https://docs.near.org/chain-abstraction/chain-signatures/implementation): signer accounts and the distinction between derivation and signing.
* [Fetch CORS](https://fetch.spec.whatwg.org/#http-access-control-allow-origin): wildcard origins and credentials. The exact route and preflight restrictions are this application's policy.
* [1Click Quote model](https://raw.githubusercontent.com/defuse-protocol/one-click-sdk-typescript/ae8b24e064785c8cba8081263e03e52fed4de089/src/models/Quote.ts) and [SwapDetails model](https://raw.githubusercontent.com/defuse-protocol/one-click-sdk-typescript/ae8b24e064785c8cba8081263e03e52fed4de089/src/models/SwapDetails.ts): output amount and withdrawal fee semantics.
* [1Click manager signature verifier](https://raw.githubusercontent.com/defuse-protocol/one-click-sdk-typescript/ae8b24e064785c8cba8081263e03e52fed4de089/src/quote-signature.ts): manager public key and signed quote hash.
* [1Click signature vectors](https://raw.githubusercontent.com/defuse-protocol/one-click-sdk-typescript/ae8b24e064785c8cba8081263e03e52fed4de089/src/__tests__/quote-signature.test.ts): dry, allocated and status signature examples. The live [OpenAPI URL](https://1click.chaindefuser.com/docs/v0/openapi.yaml) was unavailable through the web tool; its retained official extract was inspected at `fixtures/oneclick/api-contract.json`.
* [nginx method restrictions](https://nginx.org/en/docs/http/ngx_http_core_module.html#limit_except) and [proxy error interception](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_intercept_errors): the scoped proxy rollout keeps upstream application errors with the application.
* [Playwright browser installation](https://playwright.dev/python/docs/browsers#managing-browser-binaries): matching browser binaries and an isolated browser cache. The installed CLI supports --only-shell.

## Owner actions and bounds

Review and merge this working tree, then deploy the hosted main copy and apply the scoped proxy checks in the runbook. This lane does not authorize or perform that rollout. Installed Sender approval, a live vault unlock, recovery with the same key on a second device, proxy client bucket checks, attested testnet promotion, reviewed NEAR AI fleet enrollment and the 1Click D5 partner terms question remain separate owner work (`docs/RUNBOOK.md`; `docs/lanes/nep413.md:114,118,358`; `docs/lanes/gate-030.md:89-93`; `docs/lanes/nearai.md:112-150`; `docs/lanes/oneclick.md:168`). No funded rerun is needed for these documentation checks.

## Files changed

The 34 paths are:

```
README.md
demo/site/explore.html
demo/site/index.html
demo/site/top-receivers.html
docs/API.md
docs/CLAIMS.md
docs/COVERAGE.md
docs/DEMO.md
docs/KEYS.md
docs/LOG.md
docs/REGISTRY.md
docs/RUNBOOK.md
docs/SCORECARD.md
docs/TESTNET.md
docs/THREAT-MODEL.md
docs/UPGRADES.md
docs/VERIFY.md
docs/WORKSPACES.md
docs/lanes/docs-sync.md
docs/lanes/nearai.md
docs/lanes/oneclick.md
fixtures/near-auth/okx-callback-20261122.json
fixtures/oneclick/run-20261122/report.json
fixtures/runs/oneclick-mainnet-20261122.txt
integrations/langchain/README.md
integrations/wallet-selector/README.md
intel/render.py
mcp/README.md
res/BUILD-attest.md
scripts/scorecard.py
tests/test_attack_evidence.py
tests/test_landing.py
tests/test_near_auth.py
tests/test_oneclick_mainnet_run.py
```

## Complete plan checklist

The identifiers below follow the section numbers in the merged plan. Unnumbered entries are named by their file or purpose. Done includes an explicitly required preservation of existing bytes. The optional footer change is the only omitted edit. Release wording is prepared for the owner's rollout, as qualified above; no deployment was performed or observed.

| Plan item | State | Verified result |
|---|---|---|
| Owner question 1 | Done | Same FullAccess key on both dates, temporary file reconstructed at a hidden prompt and deleted after each run (`fixtures/runs/oneclick-mainnet-20261122.txt:6-7`; `docs/KEYS.md:3`). |
| Owner question 2 | Done | Final report, both 2400 fees, actual credit and owner supplied run commit retained (`fixtures/runs/oneclick-mainnet-20261122.txt:5-22`). |
| Owner question 3 | Done | Requested since 22 Nov 2026 release wording, qualified as the owner's rollout target (`README.md:31`). |
| Owner question 4 | Done | Supplied OKX capture retained and verified offline (`tests/test_near_auth.py:37-51`). |
| Prerequisite run summary | Done | Full transaction hash and report values, without secret forms (`fixtures/runs/oneclick-mainnet-20261122.txt:1-22`). |
| Prerequisite report copy | Done | Final ALLOW and settled snapshot, lossless signature_bytes, unchanged judgement document (`fixtures/oneclick/run-20261122/report.json:8-10,103,261,417,485-499`). |
| Prerequisite settlement tests | Done | Five binding, status, receipt-summary, record and fee tests; no raw receipt replay claimed (`tests/test_oneclick_mainnet_run.py:21-71`). |
| README 1 | Done | First-screen undeployed attested path and account-funded run (`README.md:13`). |
| README 2 | Done | Hosted release wording and undeployed components (`README.md:31`). |
| README 3 | Done | Settlement outside the guard and explicit deposit flag (`README.md:49`). |
| README 4 | Done | Default v1, opt-in v3, replay bounds and adapters (`README.md:127-129`). |
| README 5 | Done | Encrypted personal copy and retained metadata, Telegram sentence preserved (`README.md:131`). |
| README 6 | Done | Selected holdout and reproduction (`README.md:155`). |
| README 7 | Done | Offline, read-only network and funded command boundaries (`README.md:369-371`). |
| README 8 | Done | Preflight distinguished from the funded settlement and net reading (`README.md:393-394`). |
| README 9 | Done | All new architecture entries (`README.md:402-421`). |
| README 10 | Done | Rules selection, CORS and adapter integration (`README.md:431-437`). |
| README 11 | Done | Owner key and temporary file lifecycle (`README.md:445`). |
| README 12 | Done | Deployed service distinguished from built enclave (`README.md:502`). |
| README 13 | Done | New claims mirrored, with precise encrypted-contents wording (`README.md:507-514`; `docs/CLAIMS.md:64-71`). |
| README 14 | Done | Completed migration moved to chronology and next steps updated (`README.md:555-557,650`). |
| README 15 | Done | No enclave signing in service (`README.md:577`). |
| README 16 | Done | Optional NEAR AI verification and source default, without an unobserved hosted setting (`README.md:584`). |
| README 17 | Done | Gate source attested mode, timelock and absent artifact (`README.md:591`). |
| README 18 | Done | NearBlocks migration date and expiry-proof reads (`README.md:595`). |
| README 19 | Done | Second mainnet run remains outside the paused Gate (`README.md:598`). |
| README 20 | Done | Preflight and deposit commands distinguished (`README.md:599`). |
| README 21 | Done | Settlement, sign-in, holdout, adapter and derivation limits (`README.md:600-604`). |
| README 22 | Done | Additional dependencies (`README.md:607`). |
| README 23 | Done | Existing derivation distinguished from absent linking (`README.md:608`). |
| README 24 | Done | Reference adapter attribution (`README.md:610`). |
| README 25 | Done | Separate registry build and unpromoted Gate source (`README.md:632`). |
| README 26 | Done | Updated static-page description (`README.md:636`). |
| README 27 | Done | All post-deadline changes in chronology (`README.md:640-659`). |
| README 28 | Done | Expanded data sources (`README.md:669`). |
| CLAIMS 1 | Done | Preflight versus deposit (`docs/CLAIMS.md:43`). |
| CLAIMS 2 | Done | Built enclave with no deployed attested trust (`docs/CLAIMS.md:59`). |
| CLAIMS 3 | Done | Required rows and retained-settlement summary row (`docs/CLAIMS.md:64-71`). |
| CLAIMS 4 | Done | Regenerated scorecard with 66 claim rows (`docs/SCORECARD.md:24-25`). |
| Attack evidence expected set | Done | Removed obsolete unbuilt-enclave expectation (`tests/test_attack_evidence.py:185-189`). |
| Attack evidence directory check | Done | Existing absent-directory checks retained (`tests/test_attack_evidence.py:190-191`). |
| API 1 | Done | Account-age ASK wording and offline replay interface (`docs/API.md:66`). |
| API 2 | Done | Dry rule scoped to preflight (`docs/API.md:217`). |
| API 3 | Done | Separate settlement status catalog (`docs/API.md:224`). |
| API 4 | Done | Preflight signature limit and settlement manager verification (`docs/API.md:226`). |
| API 5 | Done | Provider defaults and attestation failure reason (`docs/API.md:408,482`). |
| API 6 | Done | Exact CORS routes, headers and preflight policy (`docs/API.md:498-502`). |
| API 7 | Done | Current NEP-413 fields, server clock, budgets and envelope routes (`docs/API.md:504-524`). |
| API 8 | Done | Settlement command, explicit funding, recovery and recorded result (`docs/API.md:526-532`). |
| API 9 | Done | Curve-specific derivation and required live arguments (`docs/API.md:534-536`). |
| THREAT-MODEL 1 | Done | Moved API citations refreshed (`docs/THREAT-MODEL.md:3,23,48,86`). |
| THREAT-MODEL 2 | Done | Scope includes the new surfaces (`docs/THREAT-MODEL.md:3`). |
| THREAT-MODEL 3 | Done | Attested source does not close deployed verifier trust (`docs/THREAT-MODEL.md:39`). |
| THREAT-MODEL 4 | Done | Dry rule scoped to preflight (`docs/THREAT-MODEL.md:50`). |
| THREAT-MODEL 5 | Done | Unsigned preflight signature limit (`docs/THREAT-MODEL.md:84`). |
| THREAT-MODEL 6 | Done | Persistent mainnet FullAccess authority (`docs/THREAT-MODEL.md:74`). |
| THREAT-MODEL 7 | Done | New sign-in, CORS and settlement entry points (`docs/THREAT-MODEL.md:27-29`). |
| THREAT-MODEL 8 | Done | Five threats, mitigations, residuals and ranked open items (`docs/THREAT-MODEL.md:51-55,88-92`). |
| KEYS 1 | Done | Same account key and temporary file lifecycle (`docs/KEYS.md:3`). |
| KEYS 2 | Done | Gate owner holder and surviving authority (`docs/KEYS.md:11`). |
| KEYS 3 | Done | Deleted verifier file row preserved (`docs/KEYS.md:26`). |
| KEYS 4 | Done | Four new roles and generic sender authority bounded (`docs/KEYS.md:27-30`). |
| COVERAGE 1 | Done | Local settlement coverage and lack of enforcement (`docs/COVERAGE.md:11`). |
| COVERAGE 2 | Done | Agent adapter and account-funded run (`docs/COVERAGE.md:19`). |
| COVERAGE 3 | Done | Wallet Selector guard and attribution (`docs/COVERAGE.md:9`). |
| COVERAGE 4 | Done | Derivation without signature or verdict (`docs/COVERAGE.md:14`). |
| COVERAGE 5 | Done | Encrypted personal copy (`docs/COVERAGE.md:16`). |
| LOG cross-origin reads | Done | Appended after the existing record (`docs/LOG.md:103`). |
| LOG derivation | Done | Read-only signer support (`docs/LOG.md:104`). |
| LOG optional NEAR AI | Done | Attestation and source default qualification (`docs/LOG.md:105`). |
| LOG rules v3 | Done | Opt-in policy and replay changes (`docs/LOG.md:106`). |
| LOG settlement commands | Done | Preview and explicit funding (`docs/LOG.md:107`). |
| LOG attested source | Done | Built components and deployment boundary (`docs/LOG.md:108`). |
| LOG adapters and holdout | Done | Selected measurement (`docs/LOG.md:109`). |
| LOG NEP-413 | Done | Exact OKX callback and outstanding wallet checks (`docs/LOG.md:110`). |
| LOG funded settlement | Done | Owner run and paused Gate (`docs/LOG.md:111`). |
| LOG hosted release wording | Done | Owner-requested since 22 Nov target; rollout not observed (`docs/LOG.md:112`). |
| WORKSPACES 1 | Done | Browser remains default and uploads remain explicit (`docs/WORKSPACES.md:9`). |
| WORKSPACES 2 | Done | Sign-in, unlock, metadata, recovery and team boundaries (`docs/WORKSPACES.md:15-21`). |
| RUNBOOK 1 | Done | Attested upgrade prerequisites and paused mainnet (`docs/RUNBOOK.md:9`). |
| RUNBOOK 2 | Done | Prefix-table citation moved with the new rows (`docs/RUNBOOK.md:75`). |
| RUNBOOK 3 | Done | Scoped nginx, CDN and client-bucket checks (`docs/RUNBOOK.md:281-287`). |
| RUNBOOK 4 | Done | Mainnet preview, funding and recovery procedure (`docs/RUNBOOK.md:321-325`). |
| UPGRADES 1 | Done | Immutable methods distinguished from FullAccess replacement (`docs/UPGRADES.md:3`). |
| UPGRADES 2 | Done | Attestation build inventory and corrected CRATES citation (`docs/UPGRADES.md:13`). |
| UPGRADES 3 | Done | Attestation prefixes added (`docs/UPGRADES.md:41-42`). |
| UPGRADES 4 | Done | Unpromoted Gate artifact and rollback target (`docs/UPGRADES.md:18`). |
| BUILD-attest | Done | TBD release source and verified six-input merge provenance (`res/BUILD-attest.md:3-7`). |
| BUILD-gate | Done | Existing deployed artifact record preserved (`res/BUILD-gate.md:7,13`). |
| VERIFY | Done | Source, artifacts and Docker rebuild distinguished (`docs/VERIFY.md:30`). |
| TESTNET | Done | Gate source updated in place on its cited line (`docs/TESTNET.md:50`). |
| REGISTRY | Done | Verdict and attestation registries distinguished (`docs/REGISTRY.md:3`). |
| DEMO | Done | Refreshed moved README citation (`docs/DEMO.md:53`). |
| Oneclick 1 | Done | Nonzero-fee net reading retained as one observation (`docs/lanes/oneclick.md:139`). |
| Oneclick 2 | Done | Pinned SDK semantics and exact reconciliation (`docs/lanes/oneclick.md:152`). |
| Oneclick 3 | Done | Owner-funded run section and open D5 question (`docs/lanes/oneclick.md:475-481`). |
| Nearai citations | Done | Three moved README references refreshed (`docs/lanes/nearai.md:265,267,313`). |
| MCP | Done | Age-only ASK wording and no shipped v3 selector (`mcp/README.md:11`). |
| LangChain | Done | Hosted v1 versus holdout v3 (`integrations/langchain/README.md:5`). |
| Wallet Selector | Done | Public CORS interface and release wording (`integrations/wallet-selector/README.md:25`). |
| Scorecard source | Done | Holdout reference and regenerated output (`scripts/scorecard.py:324-325`; `docs/SCORECARD.md:5`). |
| Renderer 1 | Done | Page inventory (`intel/render.py:7-10`). |
| Renderer 2 | Done | Settlement inside the existing five landing limits (`intel/render.py:838-848`). |
| Renderer 3 | Done | Recorded judgement names default rules v1 (`intel/render.py:729`). |
| Renderer 4 | Done | Three added capability cards and pinned copy (`intel/render.py:787-789`; `tests/test_landing.py:43-45`). |
| Renderer 5 | Done | Two mainnet records with local paths (`intel/render.py:533-542,606-607`). |
| Renderer 6 | Done | Holdout reads the committed score files (`intel/render.py:543-545,620-628`). |
| Renderer 7 | Done | Reference adapter and encrypted workspace cards (`intel/render.py:641-644`). |
| Renderer 8 | Done | Undeployed attested components and read-only derivation limit (`intel/render.py:650`). |
| Renderer 9 | Done | Fresh replays reproduce 850 ALLOW, 1 DENY and 139 UNKNOWN over 990 top receivers; ordinary 84 have no changes (`intel/render.py:559-561,912-915`; replay commands below). |
| Renderer 10 | Not done | Explicitly optional footer change omitted; existing NearBlocks attribution preserved (`intel/render.py:256`). |
| Renderer 11 | Done | Explore main digest updated and checked (`tests/test_landing.py:244-245`). |
| Renderer 12 | Done | Wallet panel and all motion sources preserved; no new installed-wallet success claim (`intel/render.py:2096-2103`; `tests/test_landing_motion.py:84-87`). |
| Conditional OKX test | Done | Exact callback succeeds; omitted, changed and trailing slash fail (`tests/test_near_auth.py:37-51`). |
| Final render | Done | Clean 5581-file render replaced demo/site wholesale and matched the interrupted run's generated bytes. |
| Final named tests | Done | All 453 cases passed, with no failures, errors or skips, in 1068.081 seconds; exact command below. |
| Final hygiene and citation scripts | Done | Both complete post-test scans exited 0, including the final records and checklist. |
| Final counts | Done | Existing CLI has no --check; running python scripts/counts.py --readme README.md kept the whole README byte exact (`scripts/counts.py:291-324`). |
| Requested full discovery | Done | Unittest reports 3949 tests in 4395.212 seconds, 91 skips, two failures and one error. Both affected modules were rerun alone. Details below. |
| Requested isolated failure checks | Done | Prefetch passed all 25 cases. Smoke ran four cases, with three passing and the same 1800-second timeout error reproduced. |

Fresh replay commands, run with TEMP and TMP set to C:\t\docs and new output directories outside the repository:

```text
python -m intel replay-receivers --set top --rules-version 3 --out EXTERNAL_TOP_DIRECTORY
python -m intel replay-receivers --set ordinary --rules-version 3 --out EXTERNAL_ORDINARY_DIRECTORY
```

The top replay produced 137 DENY to UNKNOWN changes and two UNKNOWN reason-only changes. The ordinary replay produced 70 ALLOW, 14 DENY and 0 UNKNOWN with no changes. Existing judgement fixtures were not rewritten. The new render was made with python -m intel render --out NEW_EMPTY_DIRECTORY, without --indexable. The replaced generated directory contains 5581 files and no stale assets.

## Validation on 23 Nov 2026

Every resumed test and script command uses TEMP and TMP set to C:\t\docs. Browser tests use PLAYWRIGHT_BROWSERS_PATH=C:\t\docs\playwright. Python is 3.12.10; the installed Playwright package is 1.60.0. Both matching Chromium binaries were present when the resumed run began, and python -m playwright install chromium completed without a download. The interrupted focused log's missing-browser error is historical, not a failure of this resumed run.

The required named module run passed 453 tests in 1068.081 seconds, with no failures, errors or skips:

```text
python -m unittest tests.test_render tests.test_landing tests.test_landing_motion tests.test_live_site_render tests.test_analytics_render tests.test_design_system tests.test_readme_claims tests.test_readme_limits tests.test_readme_counts tests.test_version_claims tests.test_attack_evidence tests.test_citations tests.test_readme_track_map tests.test_readme_prompt_map tests.test_hygiene tests.test_scorecard tests.test_api_doc tests.test_verify_guide tests.test_oneclick_mainnet_run tests.test_spec tests.test_mainnet_run tests.test_near_auth -v
```

This includes all five new settlement tests and the captured OKX signature regression. Separate direct checks passed those six cases, and the mirrored claim correction passed 23 README tests. These overlap the 453 and are not added to that total. Independent read-only review also passed nine overlapping checks.

Playwright MCP captured index, Explore and top receivers at 1440 and 375 pixels. All six measurements had equal document scroll and client widths. The landing retained five limits, the new record and holdout text was visible, and all motion sources remained byte exact. The static preview returned expected 404 responses for API routes because it served generated files without the API; this is a layout check, not a live workspace or payment test. Six screenshots and the browser records are retained under the ignored .codex/docs-sync-resume-20261123 directory.

The resumed citation scan passed after correcting the new checklist's oneclick range to the actual final line, 481. The resumed full-tree hygiene scan, scorecard freshness check and git diff --check also passed. The counts CLI has no --check option; python scripts/counts.py --readme README.md completed with the entire README SHA-256 unchanged (`scripts/counts.py:291-324`).

The requested full discovery ran once:

```text
python -m unittest discover -s tests
Ran 3949 tests in 4395.212s
FAILED (failures=2, errors=1, skipped=91)
```

These are the framework's exact counts, including 91 skip events. Class-level skip events are counted separately from testsRun, so no derived passing count is presented. A read-only loader inspection found 3973 case methods, including the 24-case optional SDK recording class with its setup skip (`tests/test_nearai.py:224-230`). The full output also reports four missing py-near skips; the default host does not cover every optional dependency path (`tests/test_agent.py:825-833`; `tests/test_ed25519.py:154`; `tests/test_enclave_service.py:148`).

Both failures were the prefetch SIGINT cases. Their stop paths returned code 0, but Python emitted "Exception ignored when trying to send to the signal wakeup fd" followed by WinError 10038, so the assertions expecting empty stderr failed (`tests/test_prefetch_follow.py:419-446,490-516`; `scripts/prefetch_follow.py:285-306`). Running python -m unittest tests.test_prefetch_follow -v alone then passed all 25 cases, including both failures, in 26.546 seconds with no skips, failures or errors. This establishes that the failures did not reproduce in a fresh module process. The origin of the invalid signal wakeup socket was not isolated, so the report does not attribute it to machine load or change the prefetch implementation.

The full-run error was subprocess.TimeoutExpired in the smoke test after its 1800-second subprocess limit (`tests/test_smoke.py:49-54`). That subprocess runs the full Python suite again with the smoke module excluded (`scripts/smoke.py:117-129`). Running python -m unittest tests.test_smoke -v alone then ran four tests in 1826.844 seconds: three passed, and the same TimeoutExpired error reproduced after 1800 seconds. No tests were excluded and the timeout was not extended. Neither smoke source nor prefetch source has been changed by this lane.

The full suite is not green. The nine-step smoke result could not be completed inside its existing limit, including when its module ran alone. The remaining validation work is the full-process Windows signal wakeup state and the nested suite's runtime limit. This lane records those results and preserves the implementations and test limits, rather than treating an isolated pass as a passing full run (`tests/test_prefetch_follow.py:439,511`; `tests/test_smoke.py:53-65`).

The retained test logs are C:\t\docs\docs-sync-resumed-focused.log, C:\t\docs\docs-sync-resumed-full.log, C:\t\docs\docs-sync-resumed-prefetch-alone.log and C:\t\docs\docs-sync-resumed-smoke-alone.log. All requested test commands and both isolated failure-module commands have completed. No transaction, paid completion, private key read, commit, push, deployment or remote change occurred.

Final post-test checks passed: python scripts/check_hygiene.py --all, python scripts/check_citations.py, python scripts/scorecard.py --check and git diff --check all exited 0. Running python scripts/counts.py --readme README.md again kept the entire README byte exact. The generated site's 5581-file manifest still matches the clean render after all tests. The 34-file inventory above is unchanged. HEAD and the submission tag target match their starting values; protected implementation paths, existing recordings and motion sources have no diff. Every required plan edit and validation command is accounted for above. The optional renderer footer change was omitted, and a passing full-suite or nine-step smoke result is not claimed.
