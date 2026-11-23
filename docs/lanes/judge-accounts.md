# Judge accounts

22 November 2026. Branch feat/judge-accounts starts at current origin/main,
7505f2cac. Changes stay uncommitted for independent review.

## Command

```
python -m intel judge-accounts --accounts FILE --out DIR [--labels LABELS]
python -m intel judge-accounts --accounts FILE --out DIR [--labels LABELS] --record
```

FILE is UTF-8 with one canonical mainnet account id per line, sorted ascending
by the SHA256 digest bytes of that UTF-8 id. Unsorted input is refused before
keys, provider reads or output writes. A final newline is accepted. Blank
lines, comments, labels, JSON, surrounding whitespace,
duplicate ids and testnet ids are refused with physical line numbers before
provider reads or output writes. Canonical implicit accounts are accepted.
There is no account discovery, sampling, filtering or labelled input.

The command calls the receiver benchmark's _request, _judge and
_pinned_judgement helpers in intel/__main__.py with rules_version=3 on every
pass. Each request is a native transfer of one NEAR with no memo. Every
account is a receiver, including a catalog token account. The policy has an
empty allowlist and caps, exactly as the receiver benchmark. An off-allowlist
receiver gets tier 2 checks; that fact alone is not a denial. There is no TEST
policy or signing path. The default intel/labels.jsonl store and mainnet issuer
table are the same ones the benchmark reads. --labels selects a retained store
for a historical replay. Its byte digest must match set.json, just as the
default store must. The published holdout uses its own frozen labels.jsonl
copied from commit 7505f2cac rather than the changing default store.

The gate/rules.py Git blob matches the frozen fb7765763 blob at this base.
The lane does not change gate/rules.py, gate/verdict.py or gate/rpc.py. Version 3
returns UNKNOWN when account_age is the only signal, even if another check is
unavailable. A remaining unavailable required check also returns UNKNOWN in
the absence of another denial signal. The scorer reads UNKNOWN as ASK.

## Files and resume

* DIR/set.json retains the ordered account ids, judged_at_ms, rules version,
  label store digest and optional reports account. Changed inputs or store
  refuse resume. Use a new directory for a different run.
* DIR/rpc contains the client's ordinary request-hash JSON fixtures. Headers
  and credentials are never recorded. Existing complete fixtures are reused
  regardless of age. The inherited client may refresh an old incomplete block
  header when recording because the proof needs its hash and previous hash.
* DIR/judged-block.json retains the last block at or before judged_at_ms and
  the following block proving that boundary. It is discovered only when a
  recorded account still has unknown age. A saved proof is checked against
  archival fixtures again. A malformed or mismatching proof stops the run.
* DIR/judgements/ACCOUNT.json is the scorer input subtree. Each file has the
  benchmark's subject, as, request, verdict, checks, reasons, labels and
  observed fields, plus rules_version=3. A missing fixture for an unresolved
  required check on the selected UNKNOWN pass adds scoreable=false. The scorer counts
  that account NOT_JUDGED. Each completed account is replayed before
  publication. Checkpoint and judgement writes replace files atomically through
  the shared _write_json helper. Replay does not rewrite the checkpoint.

Malformed read fixture envelopes stop without publishing that account. The
runner validates that boundary separately from programming errors. KeyError
and TypeError propagate rather than being reported as malformed fixtures.
The command also reuses top_judged_block with an explicit run path and the
shared recording budget configuration helper.

Use an output directory outside the checkout, or a descendant of
fixtures/holdout/. An existing fixtures/holdout/*/run directory and every path
inside it are refused, including resolved aliases, to protect published runs.
Every other repository destination is refused after path resolution. The input
ids and provider reads are retained there. Replay
requires a saved recorded run at DIR/set.json and refuses a new directory
without creating files. It never accesses the network, reads a provider key or
reserves budget, even if GATE_FIXTURES_MODE=off.

Read tracking covers all requests on the selected pass, including requests
hidden by a combined check's single source field. A recorded legacy NearBlocks
response can satisfy its v3 probe. When a complete pinned judgement replaces
the first pass, discarded first-pass misses do not make it not scoreable.
A rejected pinned attempt leaves the first pass and its missing-read status
in place. An established DENY signal takes precedence over unavailable
ancillary checks. An ALLOW has every required check passing, including when a
recorded fallback supplies age evidence after another source is missing.
Neither gets a missing-fixture marker. Missing required reads that leave
UNKNOWN are not scoreable. Recorded RPC or HTTP error responses are still
recorded reads; they can produce a scoreable UNKNOWN without that marker.

Interrupted recording retains completed judgements and successful read
fixtures. Repeat the exact command with the same FILE and DIR to resume. A
budget refusal or keyed HTTP rejection exits 1 before publishing that account.
Existing completed files remain available. A successful run exits 0 and prints
the rules version, judged count and verdict counts. Exit 0 includes UNKNOWN
answers and does not mean every check succeeded.

For scoring, pass DIR/judgements to eval/score.py. Its recursive JSON loader
does not ignore checkpoints or read fixtures, so passing DIR itself is invalid.
The producing commit is caller-supplied scorer metadata, not independently
verified provenance. After the orchestrator commits the reviewed command, retain
that commit alongside the run and name it explicitly:

```
python eval/score.py --verdicts DIR/judgements --holdout HOLDOUT --rules-commit COMMIT --json
python eval/score.py --verdicts DIR/judgements --holdout HOLDOUT --rules-commit COMMIT --labels LABELS --exclude-labelled --json
```

The orchestrator supplied the now public 22 November holdout and its record
run. Its retained results and limits are described in [HOLDOUT](../HOLDOUT.md).
Use the same retained LABELS for replay and exclusion scoring. Reproducing the
published scores requires a full clone with fb7765763 in local Git history.

## Recording configuration

The owner must supply these existing external resources before --record:

| Variable | Requirement |
| --- | --- |
| FASTNEAR_API_KEY_FILE | A UTF-8 file containing the FastNEAR provider token, not a wallet key. Empty, unreadable or multiline values refuse before any fetch. The value is a path, never the token itself. |
| SIYAB_PROVIDER_BUDGET_DB | An external SQLite provider budget path shared with other Siyab processes using the same key. |
| FEED_STATE_DIR | Alternative when SIYAB_PROVIDER_BUDGET_DB is unset. The database is provider-budget.sqlite3 inside this external directory. No default under intel/feed is used. |
| REPORTS_ACCOUNT_ID | Optional mainnet reports contract, matching the top receiver command. When set it is bound in the checkpoint and adds subject_summary reads. Leave it unset for the normal benchmark without community reports. |

Only FASTNEAR_API_KEY_FILE and one of the two budget locations are required.
No verifier key, agent key, wallet login, PyNaCl, py-near, model credential or
SIYAB_CHECKS flag is used. The Python standard library is sufficient.
GATE_FIXTURES_MODE, NEAR_MAINNET_RPC_URL, NEAR_ARCHIVAL_RPC_URL,
NEARBLOCKS_API_URL and SIYAB_VERDICT_NEARBLOCKS do not select this command's
clients. It explicitly uses the benchmark's fixed mainnet endpoints and modes.

New requests are counted under caller class recording. Its FastNEAR ceiling
is 3,000 keyed requests per UTC day across provider classes, with no hourly
or minute cap, and it also shares the 13,000 keyed request daily pool.
Fixtures cost no reservations. Exhausted budget, an unreadable budget database
or key cooldown stops the run even if the rules catch the original refusal.
There is no fallback to keyless FastNEAR reads. Keyed HTTP 401, 402, 403 and
429 stop immediately; 402, 403 and 429 also start the existing shared 15 minute cooldown.
FastNEAR returns HTTP 402 with `quota_exceeded` when credit is used up ([pricing](https://docs.fastnear.com/pricing)). Error output contains status codes, never the provider token.

Only the first 80 accounts in FILE's required hash order may record new
NearBlocks age evidence. This eligibility is independent of labels.
Later accounts can reuse NearBlocks fixtures already in DIR but cannot fetch
new ones. This is an account cap, not a promise that 80 lookups will succeed.
NearBlocks receives no FastNEAR Authorization header. Its unkeyed unavailable
responses become UNKNOWN under the frozen rules, as in the top receiver path.
The client spaces actual NearBlocks fetches by 10 seconds and caps them at
300 per process per UTC day. These are repository limits, not a statement
about a provider subscription.

## Read calls and 250 accounts

The fixed mainnet client endpoint is https://free.rpc.fastnear.com and the
archival endpoint is https://archival-rpc.mainnet.fastnear.com. NearBlocks
uses https://api.nearblocks.io. The following counts are cache misses; hits
make no network request.

| Path per account | Reads |
| --- | --- |
| Normal first pass | Four mainnet query calls: view_account, get_blacklist_status on the configured USDT issuer, is_blocklisted on the configured USDC issuer, and view_access_key_list. |
| Contract receiver | One additional contract_source_metadata view when the receiver has code. |
| Reports enabled | One additional subject_summary view on REPORTS_ACCOUNT_ID. |
| Old nonce below the block encoding threshold | No archival age call. |
| Encoded old access key nonce | Normally one archival block call to date that key. |
| State or boundary age proof | Up to 21 block calls and three historical view_account calls per age evaluation, bounded by the inherited 18 second account-age deadline. |
| NearBlocks fallback, first 80 only | Keys GET /v3/accounts/{account}/keys and, when needed, GET /v3/txns/{hash}, normally zero to two distinct reads. Existing legacy fixtures are replayed through the client adapter. |
| Age still unknown | The same judgement runs through archival RPC pinned to the saved block. It replaces the first result only when the receiver exists there and age resolves there. |

Pinned reads may additionally fetch block headers to validate query hashes.
The one shared judged-block discovery uses the existing bounded binary search
over archival blocks, then verifies the next existing block and header linkage.
Its limits are 64 search steps and up to 64 skipped-height probes per step;
proving the next block probes at most 64 heights. Reads already recorded by
another account or pass are reused.

For 250 existing wallets with encoded old nonces, no code, no reports and no
pinned fallback, the ordinary estimate is 250 times five, or 1,250 fresh
FastNEAR requests before shared cache savings. That fits an unused 3,000 daily
recording budget. There is no keyed minute cap from which to derive a fixed
duration. At an assumed average HTTP latency of 0.25 to 1 second, the serial
read time would be about 5 minutes 13 seconds to 20 minutes 50 seconds, plus
local work. This is a calculation, not a measured live run.

If the first 80 accounts each need one successful NearBlocks request, spacing
alone requires at least 13 minutes 10 seconds; two each means 160 requests and
at least 26 minutes 30 seconds, before latency. The account-age deadline does
not wait for a busy NearBlocks slot: it returns unavailable and can leave age
UNKNOWN instead. A creation-deposit read outside that deadline may wait. Failed
unrecorded reads can add attempts on the pinned pass or later resume.

Complex boundary proofs and pinned passes can spend the 3,000 call budget
before account 250. In that case the owner resumes after the next UTC reset
or after a shared cooldown ends. Completion can take more than one UTC day;
no complete-history or fixed-duration claim follows from the caps.

## Verified sources

Checked on 22 November 2026. No account list or live account data was fetched.

* NEAR query shapes, finality or block_id, access keys and contract views:
  https://docs.near.org/api/rpc/contracts
* NEAR block lookup by height and finality:
  https://docs.near.org/api/rpc/block-chunk
* FastNEAR regular and archival RPCs support Bearer authorization. The command
  retains the existing benchmark's endpoint constants:
  https://docs.fastnear.com/rpc
* The live NearBlocks OpenAPI was fetched read only with User-Agent siyab. It
  defines both v3 GET routes above, a keys limit default of 25, a keys data
  array or null with created.block.block_timestamp and transaction_hash, and
  a transaction data object or null with actions, actions_agg and outcomes:
  https://api.nearblocks.io/openapi.json

Version, policy, cap, deadline, fixture reuse and stop behavior were verified
against gate/rules.py, gate/rpc.py, gate/incident_replay.py, intel/budget.py,
intel/top_receivers.py, intel/issuers.json and the receiver helpers. No new
provider protocol or third party dependency is introduced.

## Validation and remaining work

The review fixes and holdout reproduction checks are recorded in
[holdout-publish.md](holdout-publish.md), including exact test counts, the
reproduced historical scores and owner steps. The command tests
use synthetic accounts, dummy provider tokens, local SQLite budgets and offline
fixtures. The holdout test uses the supplied recorded reads with the network
blocked. The full suite remains with the orchestrator as requested.
