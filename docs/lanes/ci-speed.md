# CI speed

Worktree: `siyab-ci-speed`, branch `feat/ci-speed`. Work began at the merge commit of PR 14, `f8b5a2f24`, after the GitHub API reported it merged on 23 Nov 2026 at 14:16:49 UTC. No commit, push, deployment, remote change, tag change, transaction or paid request is part of this lane.

## Workflow

The workflow accepts pushes to main, pull requests and manual dispatch. A push to a PR branch therefore starts only the pull request workflow. The existing job identifiers and check names remain: offline for Python 3.11 and 3.12, five rust-unit crates, three sandbox crates, three reproducible builds and hygiene. Eight new unit-test jobs identify both Python version and shard number. The unit checkouts retain full history because the unknown-rate tests read historical commits and the submission tag.

The hygiene job alone runs the repository scan, history scan, offline demo, Gate code hash, both wasm export checks and registry fixture verification. The existing offline checks wait for hygiene and all unit shards, run after failed dependencies, and refuse any unsuccessful dependency. They then use the read-only Actions jobs API to require exactly the eight expected unit jobs, each completed successfully. API failure, missing jobs, duplicate names, unfinished jobs and unsuccessful conclusions all refuse the gate. This avoids trusting a matrix aggregate when a child never received a runner. The gate token has only Actions read and contents read permissions.

Read-only protection checks on 23 Nov used the following endpoints:

- https://api.github.com/repos/Ridwannurudeen/siyab/branches/main/protection
- https://api.github.com/repos/Ridwannurudeen/siyab/rules/branches/main

Protection requires one approving review and dismisses stale approvals. Its response contains no required status checks. The branch rules response is an empty list. No protection setting was changed. The existing checks are preserved for compatibility with any later protection configuration.

## Module shards

`scripts/test_shards.py` uses normal unittest discovery and retains each module's complete suite, including load_tests hooks, class fixtures, import skips and empty modules. Import failures stop the runner. A package hook keeps its returned suite together. Before execution, every discovered module must occur in exactly one shard. The regression suite additionally compares the multiset of individual test identifiers with direct unittest discovery, so missing or duplicate test cases fail too.

`scripts/test_shard_timings.json` stores measured module milliseconds. Assignment takes the longest modules first, chooses the least loaded shard, breaks ties by module count and shard index, then sorts each shard by module name for execution. A new module without a recording receives a one second estimate and still enters the coverage guard. No module is omitted because its recording is missing.

Run one shard from the root:

```sh
python scripts/test_shards.py --shard 1 --shards 4
```

The hosted profile comes from the Python 3.12 unit log of [PR 14's successful workflow](https://github.com/Ridwannurudeen/siyab/actions/runs/45180857905), job 137738891535. Module boundaries in that log cover 3951 test lines and 174 modules in 1429.771 seconds. Its smoke module takes 730.778 seconds because it runs the suite again. The final profile retains hosted measurements for unchanged modules. The Windows full run refreshed nearai to 686 ms, smoke with its flag to 37092 ms, and the shard guard to 568 ms. Prefetch uses the median of the 50 final complete module runs, 34055 ms. The guard measurement is warm, with modules already imported, matching the runner's discover-then-execute order; its separate cold process took 2.277 seconds for 20 tests.

This is a runtime profile, not a module-count split. Its mixed provenance is deliberate. The local Python has Playwright and runs optional browser checks; the hosted log explicitly skips those because the browser package is not importable. Replacing their hosted weights with local browser durations would model a different workload. The full local measurements remain in `C:\t\ci\siyab-ci-speed\local-module-times.json`. Module boundary measurements may attribute deferred fixture teardown to the next module; they are scheduling estimates. Refresh all weights from a successful new hosted run when available.

| Shard | Modules | Discovered case methods | Estimated test milliseconds |
| --- | --- | --- | --- |
| 1 | 43 | 824 | 186568 |
| 2 | 43 | 1067 | 186568 |
| 3 | 44 | 847 | 186568 |
| 4 | 45 | 1266 | 186568 |

The coverage guard compares all 175 modules and 4004 case methods with direct discovery. Actual unittest testsRun differs because a 24-method optional SDK class skips at setup; skip events are reported separately without deriving a passing count.

## Smoke

Without a flag, `python scripts/smoke.py` still runs all nine steps and ends with `9 of 9 PASS` only if every step passes. CI sets `SIYAB_SMOKE_SKIP_SUITE=1` because its unit shards already run the complete suite. Only the nested suite step is skipped; the other eight retain their original numbering and behavior. It prints:

```text
2. suite: skipped, run by the CI unit-test step
8 of 8 PASS (1 skipped)
```

The second line is the successful final summary. A failure in another step still exits 1. Unset, empty, 0 and true do not enable the skip. Both modes retain replay, refusal of network reads, isolated subprocesses, temporary output and cleanup. The real smoke subprocess test still compares every repository file hash before and after and checks for empty stderr. Unit tests also prove the no-flag path calls the suite and keeps the nine step summary. `env.example` documents the flag.

## SIGINT causes and repairs

Three independent defects were reproduced.

On Windows, three existing NEAR AI runtime tests followed by the two SIGINT cases ran five tests and produced two failures in 4.878 seconds. Both commands returned 0, but stderr contained the signal wakeup descriptor diagnostic and WinError 10038. The runtime created its Proactor event loop on the main thread, installing a process signal socket, then closed it on its worker. CPython only clears that socket when closure runs on the main thread. A direct read showed the closed runtime socket still registered after shutdown.

`gate/nearai.py` now creates and closes the loop in its worker. A Future transfers readiness or the construction exception to the caller before it can queue a coroutine. It does not change the main thread's signal descriptor. Three regressions cover thread ownership, absence of a signal descriptor replacement and propagation of a loop construction failure. Two failed against the original implementation. The same original five-test sequence passes after this production fix, before any SIGINT test changes: 5 tests in 4.725 seconds, with no errors or failures.

On Linux, the original active-download case failed on this machine under WSL Ubuntu 24.04 and Python 3.12.3: 1 failure in 2 tests, 5.076 seconds overall, with 2.687 seconds exceeding the 2.5 second bound. The timer began before final-block reads, rate limiting and filesystem work. A separate 0.8 second startup-delay probe failed that same bound even though shutdown after SIGINT took only 0.187 seconds. The bound must measure shutdown from the signal and the download must already be blocked when the signal is sent. The stress and final repair evidence are recorded below.

The SIGINT tests now run in isolated child processes, retain empty stdout and stderr assertions around the child, synchronize the active download with a condition, and measure the original 2.5 and 3 second shutdown bounds from the signal. The blocked-download case requires the worker to remain active when the command returns. Both cases check the exact persisted cursor, request count, lag, output shape, restored handler and worker cleanup. The child retains SystemRoot on Windows because the fixture clears the parent environment. The direct Windows probe without that variable failed while importing asyncio with WinError 10106; restoring it fixed the child without inheriting credentials.

Review also reproduced a request accounting defect. A retry increments feed.Fetcher.requests before LimitedClient can refuse it at the rate limiter. When SIGINT stops that retry and the completed future is consumed, the old code can print a negative unused request count. Each LimitedClient now counts only attempts that pass both stop checks. Worker results and direct reads use that client's count delta. A deterministic real signal at the retry limiter and direct reads stopped before or during acquisition produced three failures across two tests before this fix; both tests pass after it. Completed retries and abandoned active downloads have separate regressions. The retry output must be nonnegative and agree with actual recorded attempts.

The authoritative Windows repetitions ran the complete module 50 times before and 50 times after the fix:

| Version | Module runs | Tests | Assertion failures | Errors | Aggregate test seconds |
| --- | --- | --- | --- | --- | --- |
| Original | 42 passed, 8 failed | 1250 | 9 | 0 | 1584.666 |
| Reviewed final source | 50 passed, 0 failed | 1400 | 0 | 0 | 1825.135 |

The final module retains all original cases and adds three regressions. Aggregate wall times were 1613.574 seconds before and 1857.986 seconds after. The original loop used `python -m unittest discover -s tests -p test_prefetch_follow.py -v`. Its fixture cleared TEMP before the first allocation, so its temporary files used the Windows fallback `C:\tmp` despite TEMP and TMP being set. The final loop initialized `tempfile.gettempdir()` before discovery, retaining `C:\t\ci` when the fixture cleared the environment:

```powershell
python -c "import tempfile,unittest; tempfile.gettempdir(); suite=unittest.defaultTestLoader.discover('tests',pattern='test_prefetch_follow.py'); result=unittest.TextTestRunner(verbosity=2).run(suite); raise SystemExit(not result.wasSuccessful())"
```

Every authoritative log and both summaries were checked. They remain under `C:\t\ci\siyab-ci-speed\prefetch-before` and `C:\t\ci\siyab-ci-speed\prefetch-after-accounting`. Two intermediate loops were stopped for review findings and are excluded: eight completed runs of the earlier 25-case version and five of the 26-case version. The final source then passed all four real SIGINT cases through root-level discovery, 4 tests in 12.028 seconds, and the focused Linux check under WSL, 5 tests in 10.267 seconds. Linux received focused checks rather than a second 50-run loop. The original nested Linux CI failure did not retain its underlying assertion traceback, so the Linux timing reproduction does not identify that historical assertion conclusively.

## Full local validation

Windows Python 3.12.10 ran normal discovery with TEMP and TMP set to `C:\t\ci` and `SIYAB_SMOKE_SKIP_SUITE=1`. A temporary guarded timing launcher initialized the tempfile cache before discovery and wrapped intact module suites. Independent source review confirmed it preserves unittest module and class fixtures and supports Windows multiprocessing. No module or case was excluded. The serial result was 3980 tests in 2463.355 seconds, zero failures, one error and 91 skip events.

That error was an existing rendering test requesting full Chromium while only Playwright's headless shell was installed. After installing the full matching browser, a direct launch exposed WinError 14001. The event log named the browser's versioned assembly, and Path.resolve showed that the desktop host redirected the AppData cache into a physical package cache. Selecting that resolved cache with PLAYWRIGHT_BROWSERS_PATH made the same browser launch. The affected test then passed, 1 test in 57.207 seconds. No browser test or assertion was changed.

All four final shards then ran concurrently through the actual `scripts.test_shards.main` entry point, with the same temp and smoke settings and the resolved browser cache. The temporary `run_shard.py` launcher only primed tempfile, added the checkout to sys.path and called that entry point under the Windows main guard. Each command used `python C:\t\ci\siyab-ci-speed\run_shard.py --shard N --shards 4`, for N from 1 through 4.

| Shard | TestsRun | Skip events | Test seconds | Exit |
| --- | --- | --- | --- | --- |
| 1 | 800 | 33 | 538.664 | 0 |
| 2 | 1067 | 42 | 320.380 | 0 |
| 3 | 847 | 10 | 263.876 | 0 |
| 4 | 1266 | 6 | 715.449 | 0 |
| Complete suite | 3980 | 91 | 1838.369 aggregate | 0 |

All four summaries report zero failures and errors. The longest runner took 715.449 seconds with local browser checks enabled. Logs are `C:\t\ci\siyab-ci-speed\shard-1.log` through `shard-4.log`; `final-shards-summary.json` records the aggregate. Earlier serial attempts interrupted for the stdin multiprocessing launcher, browser setup and accounting review are not passing full runs and are excluded from this final shard result. Temporary launchers are removed after validation; logs and JSON evidence remain.

Other completed checks, some overlapping the full suite:

| Command or check | Result |
| --- | --- |
| `python -m unittest tests.test_test_shards -v`, after the final profile refresh | 20 tests, 2.277 seconds, passed |
| `python -m unittest tests.test_nearai tests.test_ask tests.test_hygiene tests.test_injected_model -v` | 186 tests, 40.426 seconds, passed with 1 skip event |
| `python -m unittest tests.test_smoke -v`, with the skip flag | 7 tests, 34.204 seconds, passed |
| `python -m unittest discover -s tests -p test_prefetch_follow.py -v`, initial final source | 28 tests, 30.852 seconds, passed |
| Accounting regressions against original source | 2 tests, 2.665 seconds, 3 failures |
| Accounting regressions after repair | 2 tests, 2.318 seconds, passed |

The real flagged smoke test still checks empty stderr, removal of its temporary output and unchanged hashes for the entire repository tree. The no-flag nine step control flow is covered by its regression; the complete validation avoids running the same suite a second time.

Final repository checks passed: `python scripts/check_hygiene.py --all` exited 0, `python scripts/check_citations.py` exited 0, and `git diff --check` passed. PyYAML 6.0.3 parsed the workflow and explicit checks confirmed the event filters, all fourteen legacy matrix check names, all eight new shards, full unit checkout history and the read-only gate permissions. All eight changed Python files parsed with Python 3.11 grammar. No protected lane file changed.

## Expected hosted job time

These estimates exclude runner queues. Python 3.12 uses the final profile above. Applying the same assignments to PR 14's measured Python 3.11 module boundaries gives 263.588, 245.735, 212.744 and 231.478 test seconds; its original full boundaries total 1854.736 seconds. The estimates below allow roughly 30 to 60 seconds for checkout, Python setup and discovery, plus runtime variance. They are forecasts, not a new hosted run.

| Job | Expected time |
| --- | --- |
| unit-tests, Python 3.12, each of shards 1 through 4 | 3 to 5 minutes |
| unit-tests, Python 3.11, shard 1 | 5 to 6 minutes |
| unit-tests, Python 3.11, shard 2 | 4.5 to 5.5 minutes |
| unit-tests, Python 3.11, shard 3 | 4 to 5 minutes |
| unit-tests, Python 3.11, shard 4 | 4 to 5 minutes |
| offline, each Python version | 20 to 60 seconds after all unit shards and hygiene complete |
| hygiene | 1 to 2 minutes |
| rust-unit | Unchanged; last cached jobs: test-token 26 s, test-attest-mock 28 s, registry 34 s, reports 36 s, gate 41 s |
| sandbox | Unchanged; last cached jobs: reports 61 s, registry 65 s, gate 158 s |
| reproducible | Unchanged; last cached jobs: reports 106 s, registry 106 s, gate 128 s |

With warm Rust caches and runners available, the Python path should determine a workflow near 6 minutes. Cold toolchains, cargo-near installation, sandbox downloads, Docker pulls and queues can dominate instead. There are 22 jobs per new PR run, retaining all 14 old check names and adding eight unit shards, compared with 28 checks across the two old runs on a PR push. No hosted run was triggered from this uncommitted worktree.

## Documentation integration

`docs/RUNBOOK.md` has the CI, shard and smoke procedures. README and LOG belong to the orchestrator under the lane rules and remain untouched.

For `README.md`, keep the nine step local instructions and expected `9 of 9 PASS` output. In Try it offline, after the smoke timing paragraph, add the CI flag, its explicit skip line and eight-pass summary, and the full-suite command with that flag. Replace the fixed local 50 minute timing claim with the measured validation and a note that machine load and duplicate local suite execution affect duration. Extend the smoke claim row to describe both modes without changing its existing test citation. Add the four-shard matrix and retained offline gates to any CI section.

Ready to paste after the smoke example: "CI runs Python 3.11 and 3.12 in four module shards each. It sets SIYAB_SMOKE_SKIP_SUITE=1 because those shards already run the complete suite. Smoke prints suite: skipped, run by the CI unit-test step and requires the other eight checks to pass, ending with 8 of 8 PASS (1 skipped). Without the flag the local nine step behavior remains. To run the full local suite without that duplicate inner suite, set the same flag and run python -m unittest discover -s tests -v. Runtime depends on platform, optional dependencies and machine load; the dated measurements and CI estimates are in docs/lanes/ci-speed.md."

The orchestrator's LOG entry is: 2026-11-23: Split Python CI into four measured module shards per version, run PR workflows once, keep the eight other smoke checks and repair SIGINT timing and Windows event-loop ownership.

Two existing numeric citations also need integration by their owners. README line 607 names `gate/nearai.py:17`; the API base is now at `gate/nearai.py:18`. The SDK class and its setup skip cited at `docs/lanes/docs-sync.md:265` moved from test lines 224 to 230 to `tests/test_nearai.py:265-271`. The citation scanner checks existence and bounds, so these semantic line movements are recorded explicitly.

The historical prefetch and smoke observations at docs-sync lines 267, 269 and 271 describe the baseline. Pin their old numeric source ranges to commit `f8b5a2f24876fb4f2c7b2df26e5fb5ec96a9e3f1`, using GitHub blob links, rather than treating their old line numbers as current assertions.

## Changed files and handoff

- `.github/workflows/ci.yml`: event filters, eight unit shards, retained offline gates and common checks.
- `scripts/test_shards.py`, `scripts/test_shard_timings.json`, `tests/test_test_shards.py`: runner, measured profile, discovery and job-status guards.
- `scripts/smoke.py`, `tests/test_smoke.py`, `env.example`: explicit single-step skip and unchanged-tree coverage.
- `gate/nearai.py`, `tests/test_nearai.py`: event-loop ownership and readiness regressions.
- `scripts/prefetch_follow.py`, `tests/test_prefetch_follow.py`: actual-start accounting and synchronized real-signal regressions.
- `docs/RUNBOOK.md`, `docs/lanes/ci-speed.md`: procedures, evidence, timing estimates and protected documentation fold-ins.

The orchestrator must review the working tree and fold the staged README, LOG and citation changes into the files it owns. It can then observe one PR workflow after its approved integration. Python 3.11 is not installed locally; the changed Python files receive 3.11 grammar validation, while execution on that version remains a hosted matrix check. actionlint is unavailable on Windows and WSL, so workflow validation uses PyYAML 6.0.3 and explicit trigger, matrix, history and legacy-job checks. No owner transaction, paid call or secret is needed. No commit or outbound publication was performed.

## Primary sources

- [GitHub workflow triggers](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows): branch filters select main pushes; pull requests and manual dispatch are separate events.
- [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax): job names, matrix fail-fast, dependencies, always conditions, explicit bash pipeline behavior and job permissions.
- [Actions checkout v4](https://raw.githubusercontent.com/actions/checkout/v4/README.md): full-depth checkout includes the history and tags required by existing tests.
- [Workflow jobs API](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run): job names, status, conclusions, latest attempt filtering, pagination and Actions read permission. A live read of run 45180857905 confirmed latest returns fourteen distinct jobs with earlier successful siblings carried into the partial rerun; all returns twenty-eight executions.
- [GitHub CLI API command](https://cli.github.com/manual/gh_api): paginate reads every response page and slurp returns the pages in one JSON array. Installed CLI 2.92.0 also exposes both options.
- [GitHub CLI in workflows](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-github-cli): hosted runners include gh and steps authenticate through GH_TOKEN.
- [Python 3.12 unittest](https://docs.python.org/3.12/library/unittest.html#unittest.TestLoader.discover) and [Python 3.11 module loading](https://docs.python.org/3.11/library/unittest.html#unittest.TestLoader.loadTestsFromModule): discovery and load_tests behavior.
- [CPython 3.12.10 Proactor source](https://raw.githubusercontent.com/python/cpython/v3.12.10/Lib/asyncio/proactor_events.py): constructor and close use main-thread checks around the signal descriptor. The installed Python 3.12.10 source matches this behavior.
- [CPython signal implementation](https://raw.githubusercontent.com/python/cpython/v3.12.10/Modules/signalmodule.c) and [signal descriptor API](https://docs.python.org/3.12/library/signal.html#signal.set_wakeup_fd): delivery writes to the process descriptor and reports a failed write.
- [Future API](https://docs.python.org/3.12/library/concurrent.futures.html#concurrent.futures.Future): result waits for completion and rethrows a recorded exception.
- [Condition objects](https://docs.python.org/3.12/library/threading.html#condition-objects): wait releases its lock while blocked, allowing the signal sender to prove the download entered its wait.
- [Subprocess environment](https://docs.python.org/3.12/library/subprocess.html#subprocess.Popen): an explicit Windows environment must retain a valid SystemRoot for required assemblies.
- [Playwright headless shell](https://playwright.dev/python/docs/browsers#chromium-headless-shell) and [browser retention](https://playwright.dev/python/docs/browsers#stale-browser-removal): the installed Playwright 1.60.0 needed headless shell 1223. Installing only that shell with browser garbage collection disabled retained other lanes' existing browser versions.
- [Playwright browser cache](https://playwright.dev/python/docs/browsers#managing-browser-binaries): PLAYWRIGHT_BROWSERS_PATH selects the browser cache for a test process.
- [Packaged desktop filesystem behavior](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes) and [resolved paths](https://docs.python.org/3.12/library/pathlib.html#pathlib.Path.resolve): Windows may redirect AppData writes. The local browser cache resolved to a physical package cache; using that path fixed the reproduced WinError 14001 browser launch failure.
