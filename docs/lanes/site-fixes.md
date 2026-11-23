# Site fixes

23 Nov 2026. Branch `fix/site-check-findings`, based on the supplied worktree at `f8b5a2f24876fb4f2c7b2df26e5fb5ec96a9e3f1`.

## Round 2

The owner clarified that the round 1 stop rule protects recorded chain and RPC fixtures and evidence documents. Intentional whole-page render goldens and navigation tests must follow the requested page changes. The Explore golden now hashes the changed main content with the same normalization and whole-content assertion. Landing navigation tests assert the shared header and open its Explore and Tools menus before checking their destinations. The theme-order test inspects the shared base control rule. All original target-size, overflow, destination and fragment assertions remain.

The shared brand link had less than 44px height on phones. Its existing 480px breakpoint now sets a 44px minimum width and height. Above that breakpoint the CSS is unchanged. `DESIGN.md` now says the landing uses the shared application header and navigation.

Full offline validation completed with no failures or errors. The owner explicitly authorized one local commit after validation, with no push.

## Changes

The feed health grid has narrow auto-fit columns. The skipped-height, transfer-page-limit and polling-gap lists now use full-width native disclosures. Each closed summary shows the count. Skipped heights also show the lowest and highest height. Opening a disclosure shows every original value, in its original order, including duplicates, separated by a comma and a space. Long values wrap. Values are inserted as text, so provider strings cannot become markup. Empty lists remain omitted.

The swap destination form says `Amount in NEAR (sent as wNEAR)` and requests a decimal keyboard. Its submit handler calls the existing `paymentAmount(value, "near")` helper. Conversion therefore retains the same exact integer arithmetic, 24 decimal place limit, positive u128 bounds and rejection of signs, exponents and leading zeros as the existing NEAR payment forms. For example, `1.25` sends `1250000000000000000000000`. The `/live/intents` API still accepts base units, and its origin asset remains `nep141:wrap.near`.

A failed 1Click dry quote reads the original HTTP response through the existing `HttpError.__cause__`. Only `gate/intents.py` reads it. The read is capped at 65,536 bytes and checked against the existing request deadline. A JSON string `message` is limited to 200 characters and passed through `_quoted`, producing a reason such as `could not get a dry quote: HTTP 400, 1Click says "No liquidity available"`. Missing, malformed, non-string, unencodable or unreadable messages retain the previous HTTP status reason. The check remains unknown. No RPC caller behavior, successful quote evidence, fixture format or verdict rule changed.

The landing page now uses the same desktop navigation, Explore and Tools menus, mobile navigation, search and theme controls as the other pages. The separate landing navigation and its exclusive header CSS were deleted. The landing content, hero, account search, keyframes, motion declarations and motion tokens are unchanged.

## Files

- `gate/intents.py`
- `intel/render.py`
- `intel/ui/feed.js`
- `intel/ui/feed-verify.css`
- `intel/ui/index.js`
- `intel/ui/site.css`
- `DESIGN.md`
- `tests/test_intents.py`
- `tests/test_live_site_render.py`
- `tests/test_feed_workspace.py`
- `tests/feed_verify_runtime.cjs`
- `tests/test_payment_units.py`
- `tests/payment_units_runtime.cjs`
- `tests/test_landing.py`
- `tests/test_landing_motion.py`
- `tests/test_render.py`
- `demo/site/index.html`, `demo/site/explore.html`, `demo/site/feed.html`, `demo/site/style.css`, and the regenerated content-hashed `index.js` asset. Its obsolete hash was removed.
- `docs/lanes/site-fixes.md`

## Verification

Full offline discovery covered 3,982 methods in 174 nonempty modules. Final results contain 3,846 successful methods and 136 skipped methods, with zero failures and zero errors. Unittest reported 3,958 tests run and 113 skip events: 112 individual methods and one class containing 24 methods. The external runner counted successful methods directly and verified that the collected test identifiers matched all 3,982 identifiers from the original discovery, with no missing module or duplicate identifier.

The full run used Python 3.12.3 in Ubuntu 24.04. The external `round2/run_modules.py` loaded the original module suites and ran three intact module suites concurrently. The original four-case `tests.test_smoke` module then passed in 1,453.266 seconds. Its unchanged subprocess completed all nine offline steps, including the serial suite, and verified that its source tree and temporary directory were unchanged. No test assertion, timeout or skip condition was weakened.

The smoke module ran against a verified byte-identical native temporary copy to avoid repeated WSL filesystem bridge reads. Both source trees contained the same 17,869 files and 271,228,240 bytes, excluding only `__pycache__` as the smoke test itself does. Relative paths and every file hash matched before and after the run. Git reads used the original HEAD and tag through an external read-only path adapter. Logs, the complete discovery manifest, module results and hash manifests remain in the validation directory.

Linux skips comprise 63 browser methods, 43 PyNaCl methods, four py-near methods, one unselected launch-record case, one langchain-core case and the 24-method optional NEAR AI SDK class. The corresponding packages or runtime were unavailable, or the existing launch-record skip condition applied. Every one of the 63 browser methods ran successfully in the Windows checks below. Optional signing and SDK checks were not forced to run without their dependencies.

Initial Linux setup failures are retained separately from the final results. An external `python` alias resolved the missing command used by `tests.test_incident_replay`; its complete 59-case rerun passed. Adding the missing main guard to the external runner resolved multiprocessing bootstrap failures; all 11 workspace migration tests and all nine Ask ledger tests passed on complete reruns. The final 87-case render-module rerun passed with one browser skip after the stale landing theme selector was corrected. The command alias and runner main guard were external setup changes that did not alter repository code or skip tests.

The new quote and render regressions were observed failing before their fixes. The feed and amount runtime regressions likewise failed against the previous implementation before passing.

- `python -m unittest tests.test_intents -v`: 122 tests passed.
- `python -m unittest tests.test_live_site_render -v`: 31 tests passed.
- The two new render tests plus `tests.test_render.AppShellTest`: nine tests passed after the final CSS change.
- `python -m unittest tests.test_feed_workspace tests.test_payment_units -v`: ten tests passed, including 13 feed/verifier runtime scenarios, six payment form scenarios, and live feed script execution from both rendered pages. A browser regression also checks both pages at 375 and 1440 pixels with realistic nine-digit heights. It first failed on both phone pages, then passed with the existing data text token while retaining 44px disclosure targets.
- Playwright 1.60.0 with Chromium 148.0.7778.96: 16 page and viewport cases passed. Landing, Feed and Explore were checked at 375 and 1440 pixels. Landing navigation was also checked at 320, 900, 1000, 1239 and 1240 pixels in both themes. The checks covered page overflow, closed disclosures, full-width coverage rows, every expanded value, both navigation modes, exact swap amount submission and quoted error display. Six before screenshots and corresponding after screenshots were captured with stubbed network responses. No live quote was requested.
- Round 2 shared header checks passed for 40 combinations: Landing, Feed, Explore and the record index, widths 320, 375, 480, 481 and 1440, and both themes. The smallest phone brand link measured 77.90625 by 44px. All 16 combinations above 480px retained identical header geometry. No page overflow was present. Before and after screenshots and mobile-menu screenshots were inspected.
- A Windows run of 22 source and importer modules executed 487 tests, with no assertion failures and two errors: the stale landing-only theme selector and a Chromium launch through the redirected cache path. After correcting the selector and using the physically resolved browser cache, all 130 tests in `tests.test_render`, `tests.test_component_boundaries`, `tests.test_live_site_render` and `tests.test_registry_overview` passed in 260.737 seconds. Browser assertions were unchanged. These runs cover all 487 distinct methods successfully across the initial run and corrected rerun.
- `python -m intel render --out <external fresh-site directory>`: 5,581 files rendered. The regenerated `demo/site` matches every file and byte of that fresh output. Only the three requested pages, shared stylesheet and changed script asset differ from the previous site.
- `python scripts/check_hygiene.py --all`, `python scripts/counts.py --readme README.md`, the offline demo, gate code hash, gate and registry wasm export inspection, both CI registry fixture verification commands, and `git diff --check` passed.
- Independent read-only reviews of the final source, generated assets, navigation tests, motion preservation and screenshots found no remaining issue. The design detector reported one existing 2px caution border in an unchanged rule outside this task. No Playwright MCP tool was available in this session; browser checks used the installed Python Playwright package.

Recorded fixtures and the protected source and documentation files are unchanged. No remote, deployment, transaction, paid model call, private key or submission tag was changed or used.

## Verified primary sources

- https://docs.near-intents.org/api-reference/oneclick/request-a-swap-quote documents `POST /v0/quote`, the smallest-unit integer string `amount`, dry quote behavior and the JSON error message.
- https://docs.near-intents.org/api-reference/oneclick/request-a-swap-quote.md embeds the quote schema and its HTTP 400 `BadRequestResponse` with a required string `message`.
- https://rpc.mainnet.near.org answered a repeated read-only `query` of `wrap.near.ft_metadata` at final block 225881218. It returned symbol `wNEAR` and `decimals: 24`. The request used `request_type: call_function`, `finality: final`, `account_id: wrap.near`, `method_name: ft_metadata` and `args_base64: e30=`. No signing or transaction was involved.

## Owner follow-up

No credentials or funded run are needed. Deployment remains outside this task.

The design sidecar has pre-existing drift; the `document` workflow can refresh it separately.

Validation logs, screenshots, helper scripts and fresh render output remain in the sibling `siyab-site-fixes-validation-p9ygzngn` directory, including the `round2` subdirectory. They are retained as requested for the lead to remove.

The native smoke copy and its temporary parent also remain at `/tmp/siyab-site-fixes-smoke-_hjjhbh6` in Ubuntu 24.04. The Windows validation directory contains its location and before and after hash results in `round2/native-snapshot.json` and `round2/native-snapshot-result.json`.
