# FastNEAR exhausted credit cooldown

23 November 2026. Changes remain uncommitted on `feat/nep413-workspace`.
The existing uncommitted documentation sync is preserved.

## Change

`intel/budget.py:KEY_FAILURE_STATUSES` now contains 402, 403 and 429.
A metered keyed FastNEAR HTTP 402 starts the existing shared 900 second
cooldown, stores status 402 in `key_cooldown`, records the existing
`key_cooldown` alert and writes the existing status message. The failed
request still fails. Subsequent ordinary metered requests use the keyless
budget and omit Authorization until the cooldown expires. No immediate
retry, key rotation or favorable verdict is introduced. Unavailable reads
remain unavailable or UNKNOWN under the existing rules.

The transport already calls `budget.key_failure` after a keyed FastNEAR
HTTP error. Its runtime code is unchanged. Its module docstring now names
402 alongside 403 and 429. The recording caller still stops on keyed
401, 402, 403 or 429 and never falls back to keyless recording. Status 401
does not start this shared cooldown.

FastNEAR documents HTTP 402 with `quota_exceeded` when credit is used up.
The provider's 429 rate limit response remains distinct. See the primary
sources below. No provider credential was read or sent during this lane.

## Classification audit

Searched `gate/`, `intel/` and `scripts/` for 403, 429, `key_failure`,
`KEY_FAILURE_STATUSES` and keyed rejection paths.

* `gate/rpc.py:Client._metered`, `_exchange` and `_fetch` already identify
  the FastNEAR provider classes, track whether Authorization was actually
  sent and call `key_failure` for HTTP 402. Non-FastNEAR hosts receive no
  FastNEAR Authorization header. The recording branch already stops on
  401, 402, 403 and 429. Its stop behavior is unchanged.
* `intel/accounts.py` and `intel/portfolio.py` pass keyed regular and
  archival reads through that client. `scripts/prefetch_follow.py` also
  delegates its RPC calls to it. None has a separate key rejection list.
* `intel/top_receivers.py` uses the same client for neardata. Its existing
  HTTP error handling stops on every status below 500, including 402 and
  403. `intel/judge_accounts.py` and `intel/__main__.py` already propagate
  recording key rejection as a stopped run.
* `intel/feed.py` retries 429 only and propagates 402 and 403. Its keyless
  polling path already treats 401, 402 and 403 as unavailable. The follower
  never reads or sends the provider key.
* `intel/issuers.py`, `intel/trace.py` and `intel/cluster.py` retry 429 only
  and already propagate both 402 and 403. Their retry handling is unchanged.
* NearBlocks spacing and retries, Telegram and webhook retries, NEAR AI
  errors, workspace authentication, CORS and API request limits retain
  their existing status handling. None uses the FastNEAR key cooldown.
  The collector in `tests/data/nearblocks_parity/collect.py` is also
  NearBlocks specific and unchanged.

The shared status tuple was the only runtime classification requiring a
change. A documentation search also covered README, docs, enclave, MCP
and integration documents. Every statement found about the 403 or 429
key cooldown now includes 402 and explains exhausted credit.

## Regressions and mutation check

Two tests were added to `tests/test_budget.py`:

* `BudgetWindowTest.test_a_402_starts_a_shared_cooldown_that_strips_the_key_and_expires`
  checks shared SQLite state, alert creation, stored status, keyless
  accounting across caller and provider classes and exact expiry.
* `MeteredClientTest.test_a_keyed_402_starts_the_cooldown_and_the_next_rpc_request_is_keyless`
  checks the actual RPC transport boundary. The rejected request carries
  Authorization, the next archival request omits it while retaining
  Accept, and Authorization returns at expiry. A 402 from another provider
  does not start another cooldown.

Both tests failed before the fix. Both passed after it. Removing only 402
from the tuple made both fail again. Restoring the source made both pass.
The mutation was restored before the enclave builds and final tests.
The four runs are retained under `C:\t\k402` as
`regressions-before-fix.log`, `regressions-after-fix.log`,
`regressions-mutated.log` and `regressions-restored.log`.

| Command | Result |
| --- | --- |
| `python -m unittest tests.test_budget tests.test_rpc -v` | 107 tests passed in 8.502 seconds, no skips |
| `python -m unittest tests.test_top_receiver_recording tests.test_judge_accounts tests.test_top_receivers tests.test_holdout tests.test_near_auth -v` | 160 tests passed in 150.420 seconds, no skips |
| `python -m unittest tests.test_enclave_build -v` | 6 host tests passed in 0.257 seconds, no skips |
| `python -m unittest tests.test_enclave_build -v` in the rebuilt image | 6 tests passed in 0.297 seconds, no skips |

Host validation uses TEMP and TMP at `C:\t\k402`. In-image validation uses
ephemeral `/tmp`, disabled networking and a read only repository mount.
The separate baked runtime smoke has no source mount and passed in
1.922 seconds. It checked all 25 copied file hashes, fixed configuration,
uid and gid 65532, imports, absent shell and relay and registry refusal.

## Reproducible image

`enclave/build-record.json` now records 23 November 2026 and the changed
`intel/budget.py` and `gate/rpc.py` input hashes. The Dockerfile, dependencies,
build arguments and all other source inputs are unchanged.

Two successful uncached builds in WSL Ubuntu-24.04 used distinct private
BuildKit containers and empty cache volumes. They used Docker 29.1.3,
verified Buildx v0.39.1, digest pinned BuildKit v0.35.1, Linux amd64,
`SOURCE_DATE_EPOCH=1795304440`, timestamp rewriting and disabled provenance
and SBOM. Both consumed one sorted 26 file USTAR context with mode 0644
and the fixed epoch. Build times were 127.343 and 453.297 seconds.

Both OCI index byte hashes, image manifests, configs and all 12 layer
descriptors matched. All 14 blobs in each archive matched their hashes and
sizes and were accounted for by the index and manifest. The manifest
prefix is `6a1ef07ba339`, config `a0f99a694534`, index `5671f09d1107`
and context `b9eb974b9950`. Full digests and all 26 input hashes are in the
build record. Source hashes were rechecked after each build and validation.

The deterministic 826 byte LF compose file uses the example repository
`registry.example/siyab`; its SHA256 prefix is `542c9bac331e`. This is a
local review artifact. No image was published and no CVM or complete
deployment measurement was created.

Proofs, commands, tool checksum verification, context and OCI archives are
retained in `.codex/k402-enclave-repro`. A failed second build encountered
a PyPI wheel download timeout. Its evidence is retained in `failed-two/`.
The successful retry used a newly recreated empty second builder. Both
private builders and their cache volumes were removed and their absence
verified. Earlier build proofs and existing Docker resources are preserved.
The loaded local image is `siyab-k402-enc:final-20261123`.

## Render and public reads

`python -m intel render --out C:\t\k402\site` generated 5,581 files.
A complete SHA256 manifest comparison found zero added, removed or changed
files against `demo/site`. No rendered text required a repository change.

At 11:53:11 UTC on 23 November, keyless public probes returned HTTP 200
with a JSON-RPC result from both regular and archival mainnet RPC hosts.
The neardata last final block helper returned HTTP 302 with Location
`/v0/block/225850964`. No Authorization header was sent and the redirect
was not followed. These observations are retained in
`C:\t\k402\keyless-probes.json`; they do not promise future availability.

## Files and owner steps

This lane changes `intel/budget.py`, the `gate/rpc.py` module docstring,
`tests/test_budget.py` and `enclave/build-record.json`. Documentation
changes are in `README.md`, `docs/API.md`, `docs/KEYS.md`, `docs/RUNBOOK.md`,
one dated line in `docs/LOG.md`, `docs/lanes/judge-accounts.md`,
`enclave/BUILD.md`, `docs/lanes/nep413-enclave-build.md`,
`docs/lanes/attest-enclave.md` and this report.

No owner credential, transaction or paid call is required to verify this
fix. Independent review and the orchestrator's commit remain. Any later
publication or deployment requires separate approval. No commit, push,
remote change, PR, deployment or tag change was made in this lane.

## Primary sources

Verified on 23 November 2026:

* Exhausted credit is HTTP 402 with `quota_exceeded`; rate limiting is 429:
  https://docs.fastnear.com/pricing
* Bearer authentication and the shared provider key:
  https://docs.fastnear.com/auth
* Regular and archival RPC hostnames:
  https://docs.fastnear.com/rpc
* Neardata authentication and invalid key handling:
  https://docs.fastnear.com/neardata
* Last final block helper route and redirect:
  https://docs.fastnear.com/neardata/last-block-final
* The read only probes used these public endpoints:
  https://rpc.mainnet.fastnear.com
  https://archival-rpc.mainnet.fastnear.com
  https://mainnet.neardata.xyz/v0/last_block/final
* Buildx release checksum and uncached build option:
  https://github.com/docker/buildx/releases/download/v0.39.1/checksums.txt
  https://github.com/docker/buildx/blob/v0.39.1/docs/reference/buildx_build.md
* Private builder and OCI exporter settings:
  https://docs.docker.com/build/builders/drivers/docker-container/
  https://docs.docker.com/build/exporters/oci-docker/
* Fixed epoch and timestamp rewriting:
  https://github.com/moby/buildkit/blob/v0.35.1/docs/build-repro.md
