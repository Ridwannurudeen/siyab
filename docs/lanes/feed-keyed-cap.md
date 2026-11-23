# Capped keyed block feed

23 November 2026, lane `feat/feed-keyed-cap`, based on `ef1fa2e`.

## Behavior

`intel/feed.py` sends the provider key named by `FASTNEAR_API_KEY_FILE`
for neardata block reads only when `intel/budget.py` reserves that read
under caller class `feed`. Its daily keyed allowance is 120,000 per UTC
day, with no hour or minute cap. The ordinary keyed pool remains 13,000
per UTC day. The feed does not consume that pool; the theoretical combined
maximum is 133,000 keyed calls a day. Every keyed feed reservation still
adds to the shared cumulative count, whose 300,000 alert is unchanged.
The credential file, budget database and feed state stay outside the
checkout.

An absent key, spent feed cap or active shared cooldown sends that block
read without the key. Keyed HTTP 401, 402, 403 or 429 retries the same read
keylessly. HTTP 402, 403 and 429 start the existing shared 900 second key
cooldown; 401 does not. Eligibility is checked on later requests, so a new
UTC day or expired cooldown restores keyed reads when the other condition
permits them. A keyed reservation stays counted even when its request
fails. A keyless retry is counted keylessly.

Keyed block requests retain the 0.1 second start interval and keyless
requests the 0.4 second interval. The follower walks in order across a
switch, keeps the final block body instead of fetching that height a
second time, retains the one second tip wait and retries keyless 429
responses without skipping the height. Existing state fields, restart
cursor, event evidence and first-seen coverage remain. The separate
Transfers API poll remains keyless. Failed or unread data is unavailable
or UNKNOWN, never ALLOW.

## Why keyed reads need a separate cap

The operator supplied the following VPS observations from 23 November
2026. This lane did not repeat them or access the VPS.

* The old 9 Nov copy under `/opt/shieldbot-near` sent the key on every
  neardata read at the keyed pace without a limit. It exhausted the key's
  credit, then stopped on HTTP 402 and restarted repeatedly for about
  nine hours.
* The second consecutive keyless neardata request returned 429. The
  keyless follower walked about 14 heights a minute while about 66 new
  blocks a minute arrived. Keyed it walked about 195 heights a minute.
* A follower at the tip needs about one request a height, roughly 80,000
  to 86,000 a day.

Those measurements explain the 120,000 allowance and the fallback. They
are observations of that host and provider at that time, not a throughput
promise. Leaving the old unmetered keyed path would permit another credit
drain. Making the follower permanently keyless would retain the measured
backlog growth. The separate feed pool permits the bounded keyed walk
without spending the ordinary readers' 13,000 daily share.

## Alerts and deployment boundary

Reservations at 60,000, 96,000 and 120,000 keyed feed calls create
`feed_50`, `feed_80` and `feed_100` rows respectively in the existing
`alerts` table and print stderr lines. Each threshold is recorded once
per UTC day. Existing ordinary pooled alerts and the cumulative alert are
unchanged. The key cooldown row is shared by feed and the other readers.

The supplied host facts say `siyab-fastnear-alert.timer` runs the installed
`/usr/local/bin/siyab-fastnear-check --alert`. That script is not in this
checkout, and no installed copy was supplied. Repository searches for its
name and its timer found no source. This lane therefore cannot confirm
that the installed script reads all alert kinds. Updating `/opt/siyab`
does not update `/usr/local/bin/siyab-fastnear-check`. Before rollout, the
operator must inspect that installed file. If it filters for known kinds
or reports a fixed caller list, update the installed script to include
`feed_50`, `feed_80`, `feed_100` and the feed counter. If it already reads
all alerts without a class filter, no script change is needed. Preserve
its notification deduplication and confirm the new kinds using a local
test database before relying on the timer. No notification was sent here.

The feed must use
`SIYAB_PROVIDER_BUDGET_DB=/var/lib/shieldbot-near/siyab/provider-budget.sqlite3`,
the same database as the Siyab units. Its state remains under
`FEED_STATE_DIR=/var/lib/shieldbot-near/feed`. The supplied unit's
`ReadWritePaths=/var/lib/shieldbot-near` already covers the state directory,
budget database and SQLite sidecars. Both paths remain writable to
`shieldbot-near`, and the supplied database is owned by that user with
mode 0600. The uncommitted root deploy notes describe the code-directory
switch, the two existing drop-ins, state preservation and keyless rollback.
No VPS change was made.

## Documentation fold-ins

This lane leaves README, API, LOG and AGENTS unchanged. The orchestrator must apply
these exact semantic updates when folding the lane into the shared docs.

* `AGENTS.md:81`: replace the assertion that the follower never reads or
  sends `FASTNEAR_API_KEY_FILE` with capped metered block reads under
  `feed`, 120,000 per UTC day and keyless fallback. Add the common external
  budget database path to its `SIYAB_PROVIDER_BUDGET_DB` description.
* `README.md:171`: follow the recording paragraph with the feed's 120,000
  daily share, separate ordinary pool, fallback and shared budget path.
  The separate Transfers API poll remains keyless. After a successful
  rollout, update `README.md:609`, which currently describes the hosted
  feed as running from `/opt/shieldbot-near`, to record the verified new
  code directory. Do not claim that rollout has occurred before readback.
* `docs/API.md:311` and `docs/API.md:315`: change the feed row from zero keyed allowance
  to 120,000 per UTC day across neardata block reads, with no hour or
  minute cap. Explain that the ordinary 13,000 pooled cap excludes feed,
  but the cumulative counter and cooldown include it. Add `feed_50`,
  `feed_80` and `feed_100` at 60,000, 96,000 and 120,000, and replace its
  closing never-sends-key sentence with the fallback and recovery rules.
* `gate/rpc.py`: document the keyed feed cooldown refusal alongside the
  recording refusal in the module docstring. The follower reserves its
  keyless retry separately after pacing, while recording still stops.
* LOG proposed line: 23 Nov, after the deadline: capped the keyed block
  follower at 120,000 calls per UTC day in the shared provider budget,
  with keyless fallback, automatic recovery, feed alerts and ordered
  walk regressions; retained the feed state and separate ordinary pool.

The older `docs/lanes/fastnear-402.md` describes its historical change
and test evidence. Its statement that the follower was keyless describes
that earlier lane, before this one. Its shared cooldown remains in force.

## Verified external facts

Primary sources were read on 23 November 2026. No provider credential was
read or sent, and no paid provider request was made.

* One FastNEAR credential works across RPC and API endpoints. Production
  backends should send it with an Authorization Bearer header rather
  than a query string: https://docs.fastnear.com/auth
* Neardata mainnet is `https://mainnet.neardata.xyz`, accepts the same
  Bearer credential and rejects an invalid key with HTTP 403 before
  redirect helpers run: https://docs.fastnear.com/neardata
* The finalized tip helper is GET `/v0/last_block/final` and redirects to
  the latest finalized block document:
  https://docs.fastnear.com/neardata/last-block-final
* FastNEAR charges one credit per request, grants 500,000 trial credits
  once, documents exhausted credit as HTTP 402 with `quota_exceeded`
  and rate limiting as HTTP 429: https://docs.fastnear.com/pricing
* systemd applies UnsetEnvironment after the other environment sources;
  an empty assignment resets its list. ReadWritePaths permits the listed
  paths under ProtectSystem strict, subject to ordinary file permissions:
  https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
* An empty ExecStart assignment clears the inherited start command:
  https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html

The 401 fallback is an explicit defensive requirement for this follower,
not a claim that the neardata documentation promises that response.

## Implementation and regressions

The follower uses the existing RPC transport and its budget reservation,
header protection and key-failure handling. A small follow-only client
paces and counts each actual neardata attempt. The general Fetcher still
handles 429 backoff and record-mode request limits. Recording and the
prefetch backfill retain their existing credential behavior.

A keyed feed reservation refuses during shared cooldown, like recording,
so the follower can finish keyless spacing before reserving its fallback.
Other ordinary callers still go keyless inside the budget reservation.
Refused reservations count nothing. The follower rechecks eligibility
after a spacing wait, including a wait across UTC midnight, then reserves
exactly one actual read. A forced keyless retry lasts one attempt. A
subsequent 429 retry checks eligibility again, so a height that remains
throttled can recover keyed access when cooldown expires.

The new transport module has 16 tests. It uses the real RPC and SQLite
boundary with synthetic HTTP responses and credentials, virtual time and
existing test helpers. Eight new budget tests cover the shared cap,
concurrent processes, independent pools, daily reset, threshold alerts,
cumulative counts, cooldown and unreadable state. Existing feed tests now
check that only metered neardata receives the synthetic header and that
more than 600 keyless retries remain permitted. The prefetch test checks
its explicit keyless client directly instead of comparing the order of
the follower's client constructors.

The initial 13 transport cases ran before the feed implementation and
reported eight assertion failures and four errors. Three later review
regressions all failed before their fixes: a cap switch across midnight,
cooldown expiry during fallback spacing and repeated keyless 429s beyond
cooldown on the same height. All 16 transport cases pass after the fixes.
The obsolete prefetch constructor comparison failed alone before its
replacement; all 28 prefetch tests pass afterward.

Final source mutation checks ran only in child-process memory. Raising
the feed cap to 120,001 made both the budget boundary and transport switch
tests fail. Removing 402 from the follower's keyed rejection tuple made
the transport regression error on HTTP 402. The unmodified source passed
the six boundary and review regressions afterward. These runs are retained
under `C:\t\feedcap` as `feed-cap-final-mutant.log`,
`feed-402-fallback-final-mutant.log` and `regressions-final-clean.log`.
No mutated source file was written.

An independent read-only review found no further correctness or security
issue after these fixes. The existing shard guard passed without adding
a timing entry for the new test module.

## Reproducible enclave image

The feed module is not copied into the enclave image. The changed budget
module is copied and pinned, so its final source was rebuilt twice in
WSL Ubuntu 24.04. Both builds used separate private BuildKit containers
and empty cache volumes, disabled cache, pinned BuildKit v0.35.1, verified
Buildx v0.39.1 and Docker 29.1.3. Build arguments, dependencies and all
other image inputs are unchanged.

The final builds took 31.797 and 31.000 seconds. Both produced manifest
prefix `aa7d5f9d1007`, config `a276cda44af5` and index `f2a6ccf46c6a`.
All 12 layer descriptors and all 14 blobs in each archive matched, with
descriptor hashes and sizes verified and no unreferenced blob. The
sorted 26-file USTAR context, prefix `cda7b8d44e1b`, was independently
recreated byte for byte. All source hashes matched after both builds and
runtime validation. The final budget source prefix is `22e504bb6e04`.
Full hashes are recorded in `enclave/build-record.json`.

Compose was generated twice as identical 826-byte UTF-8 output with LF
endings, prefix `191d0354a0b1`. The six host build tests and six in-image
build tests passed without skips, failures or errors. The separate baked
runtime smoke passed without a source mount or network, verifying all
25 copied input hashes, fixed configuration, uid and gid 65532, absent
shells and relay, registry refusal, both daily pools, cumulative accounting
and actual 402 cooldown refusal, separately reserved keyless access and
keyed eligibility at exact expiry.

Final artifacts remain in `.codex/feedcap-enclave-repro/r2`. The local
image is `siyab-feedcap-r2-enc:final-20261123`. Both private final builders
and their cache volumes were removed and their absence verified. An
earlier comparison before the cooldown timing repair is retained as
historical evidence in `.codex/feedcap-enclave-repro`.

The following primary build sources were also checked:

* Official Buildx release checksum:
  https://github.com/docker/buildx/releases/download/v0.39.1/checksums.txt
* Flags supported by the pinned Buildx version:
  https://github.com/docker/buildx/blob/v0.39.1/docs/reference/buildx_build.md
* Private builder containers and volumes:
  https://docs.docker.com/build/builders/drivers/docker-container/
* OCI exporter:
  https://docs.docker.com/build/exporters/oci-docker/
* Fixed epoch and timestamp rewriting:
  https://github.com/moby/buildkit/blob/v0.35.1/docs/build-repro.md

## Files and owner actions

The committed lane changes are `intel/feed.py`, `intel/budget.py`,
`tests/test_feed.py`, `tests/test_budget.py`,
`tests/test_feed_keyed_cap.py`, `tests/test_prefetch_follow.py`,
`env.example`, `docs/RUNBOOK.md`, `docs/KEYS.md`, `docs/WORKSPACES.md`,
`docs/EVIDENCE.md`, `enclave/BUILD.md`, `enclave/build-record.json`
and this report. Root `deploy-notes.md` is deliberately uncommitted.

No funded run, key rotation or transaction is needed to validate this
change. The owner must review the installed alert script, apply the
protected-document fold-ins above and approve a coordinated rollout.
The deploy notes give release, state, drop-in, shared-budget and keyless
rollback commands. They enable shell fail-fast before preflight checks.
No VPS access, deployment, push, remote change, image publication, paid
model call or blockchain transaction was performed. The submission tag
was not changed.

## Final validation

Host tests used Python 3.12.10, TEMP and TMP at `C:\t\feedcap`, and
`SIYAB_SMOKE_SKIP_SUITE=1`, so the smoke test did not rediscover the
whole suite inside itself. Every one of the 176 discovered modules was
assigned to exactly one shard. All 4,035 discovered methods are accounted
for: 3,921 passed and 114 skipped, with no failure or error in the final
runs. Unittest reports 4,011 testsRun and 91 skip events: 90 individual
method skips and one class skip covering 24 optional NEAR AI SDK methods.

| Command | Result | Seconds |
| --- | --- | --- |
| `python scripts/test_shards.py --shard 1 --shards 4` | 755 testsRun, one class skip covering 24 methods, passed | 813.215 |
| `python scripts/test_shards.py --shard 2 --shards 4` | 1,232 testsRun, 42 method skips, passed | 871.939 |
| `python scripts/test_shards.py --shard 3 --shards 4` | 913 testsRun, 43 method skips, passed | 295.571 |
| `python scripts/test_shards.py --shard 4 --shards 4` | 1,111 testsRun, five method skips, passed | 336.822 |
| `python -m unittest tests.test_feed_keyed_cap tests.test_feed tests.test_feed_incremental tests.test_feed_behavior_signal tests.test_budget tests.test_test_shards -v` | 179 passed, no skips | 33.673 |
| `python -m unittest tests.test_prefetch_follow -v` | 28 passed, no skips | 31.947 |
| `python -m unittest tests.test_feed_keyed_cap -v` after the final docstring change | 16 passed, no skips | 7.865 |
| `python -m unittest tests.test_budget -v` after the cooldown repair | 36 passed, no skips | 4.316 |
| `python -m unittest tests.test_enclave_build -v` on the host | Six passed, no skips | 0.325 |
| Same build module inside the final image, network disabled | Six passed, no skips | 0.396 |
| Baked runtime smoke, no source mount or network | Passed | 0.860 |

The first shard 4 run found only the obsolete prefetch constructor
comparison described above. The repaired module passed before the whole
shard was rerun successfully. The first shard 1 run found one Chromium
launch error in an existing rendering test. Windows reported error 14001
and an unresolved side-by-side assembly in the existing browser cache.
A fresh private installation of the same Playwright 1.60.0 browser,
Chrome for Testing 148.0.7778.96, launched successfully. The complete final
shard 1 run used `PLAYWRIGHT_BROWSERS_PATH=C:\t\feedcap\playwright` and
passed, including that browser test. No rendering test or global browser
installation was changed. The supported private browser path and install
command are documented by Playwright:
https://playwright.dev/python/docs/browsers

The successful final logs are `C:\t\feedcap\shard-1-final.log`,
`C:\t\feedcap\shard-2.log`, `C:\t\feedcap\shard-3.log` and
`C:\t\feedcap\shard-4-final.log`. The earlier failed logs are retained
separately. Both `python scripts/check_hygiene.py --all` and
`python scripts/check_citations.py` passed. The six rollout shell blocks
passed Bash syntax checks and the embedded Python block compiled. The
rollout shell enables fail-fast before its first check. Final diff
whitespace and all 26 pinned source hashes also passed verification.
