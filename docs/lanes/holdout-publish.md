# Holdout publish

22 November 2026. Worktree siyab-eval-scorer, branch feat/judge-accounts,
base 7505f2cac. All changes remain in the working tree for independent review.

## Changes

* intel/judge_accounts.py refuses replay without an existing set.json. It
  refuses repository output paths except descendants of fixtures/holdout/,
  after resolving the path. Neither refusal creates an output file.
* Input must be sorted ascending by SHA256 digest bytes of each UTF-8 account
  id. That fixes which first 80 accounts may record NearBlocks reads without
  using the labels. Changed order or run identity cannot resume a checkpoint.
* Every selected replay pass tracks missing read fixtures and the rule checks
  that consume them. Only a missing read feeding an unresolved required check
  on an UNKNOWN judgement adds scoreable=false. An established DENY signal
  takes precedence over unavailable ancillary checks. A passing recorded age
  fallback also makes earlier missing age reads irrelevant, including when a
  different check has a recorded UNKNOWN. Tracking includes reads hidden by a
  combined check's single source, such as the two issuer lookups.
* Recorded legacy NearBlocks responses satisfy their v3 probes. A selected
  pinned judgement uses its own read dependencies; a rejected pinned attempt
  retains the first judgement and its dependencies. Recorded RPC and HTTP
  error responses are recorded reads, so their UNKNOWN can be scored.
* eval/score.py counts an explicit scoreable=false as NOT_JUDGED, keeps its
  account in duplicate detection and rejects nonboolean marker values. An
  absent marker preserves the historical decision.
* intel/__main__.py supplies the shared top_judged_block helper with an
  explicit path, the common recording database configuration and atomic JSON
  writes. The runner no longer copies those implementations. Malformed read
  envelopes are checked at their boundary; programming KeyError and TypeError
  propagate. Initial client mode and loop read mode have distinct names.
* Fix round 1 changes intel/judge_accounts.py, tests/test_judge_accounts.py,
  tests/test_holdout.py, docs/HOLDOUT.md, this file and judge-accounts.md.
  Three new command tests cover decisive label DENY, missing required age
  reads and optional misses after a recorded age fallback. The fallback
  case checks both ALLOW and a recorded issuer UNKNOWN scored as ASK.
  The checked holdout command reproduces all judgements and both scores.
* .gitattributes disables text conversion for fixtures/holdout/**. README.md
  has one link to docs/HOLDOUT.md. The command reference in judge-accounts.md
  describes the revised replay and ordering requirements.
* Fix round 2 adds --labels to the command and freezes the exact label store
  bytes from 7505f2cac in fixtures/holdout/2026-11-22/labels.jsonl. The digest
  in labels.sha256 matches run/set.json. Replay and the scorer's existing
  --labels option use this copy. The holdout tests refuse any live store load.
* The checked command test copies only recorded inputs, excluding judgements,
  and requires exactly the 252 newly written judgement files before comparing
  their bytes and scores. A no-op writer mutation suppressing all 252 writes
  makes that test fail on its output assertion, with no unexpected errors.
* Both replay and recording refuse --out equal to or inside any existing
  fixtures/holdout/*/run directory, after resolving paths. Tests cover the run
  itself, nested destinations and a parent-segment alias in both modes, with
  no credential access or file changes. The consecutive record blocks are
  merged without changing their order.

Round 2 changes intel/__main__.py, intel/judge_accounts.py,
tests/test_judge_accounts.py, tests/test_holdout.py, docs/HOLDOUT.md, this file,
judge-accounts.md, the new frozen labels.jsonl and labels.sha256, and the two
score files' line endings. eval/score.py already accepts --labels and needed
no further code change. README.md and .gitattributes retain their earlier lane
changes without any round 2 edit.

gate/rules.py, gate/verdict.py, gate/rpc.py and intel/labels.jsonl are unchanged.
The gate/rules.py Git blob is identical at fb7765763 and 7505f2cac. The
orchestrator supplied the holdout before this work; no accounts were drawn
or added to the label store here.

## Historical result and checked replay

[HOLDOUT](../HOLDOUT.md) describes the sources, selection, limits and result.
The draw stopped at 218 presumed good accounts at block 225602615. The file
contains 252 rows, including 34 labelled bad. Its digest matches the one in
both retained score files. All 1,652 protected receiver, input and run files
remain byte identical. The two score files change only from CRLF to LF.

| Result | Bad DENY | Bad ALLOW | Bad ASK | Bad NOT_JUDGED | Good DENY | Good ALLOW | Good ASK | Good NOT_JUDGED |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Historical score | 9 | 25 | 0 | 0 | 0 | 212 | 6 | 0 |
| Checked replay | 9 | 25 | 0 | 0 | 0 | 212 | 6 | 0 |

All nine DENY accounts already had labels: five Tether blacklisted, two HAPI
and two publicly reported accounts. Zero of the 25 bad accounts absent from
the label store were refused. Zero of the 218 presumed good accounts were
refused; six received ASK, or 2.8%. Precision of 9 of 9 rests on nine refusals.
Five DENY judgements have unknown age. Four lack recorded age reads and the
account beginning f712 has a recorded response with no creation time. The
rules' signal precedence makes those reads unnecessary to establish DENY. All 252 judgement
files and both score files reproduce byte for byte through the checked command.

All five Tether blacklist judgements include issuer_blacklist signals for
the native NEAR requests. The rule calls the issuer check for native transfers
when issuer entries are supplied. All five also have receiver_exists signals
reporting nonexistent mainnet accounts, so they would be refused without
their labels. The detection gap is the 25 unlabelled bad
ALLOW answers; a later rules version needs fresh holdout data.

gate/rules.py still runs tier 2 after tier 1. Its verdict dependency is the
short circuit: any signal requires DENY despite unknown checks, except that
version 3 returns UNKNOWN when account_age is the only signal. Missing reads
produce unknown checks, never signals. The runner maps account and age reads,
issuer views, contract metadata and reports views to their consuming checks.
The marker tests those dependencies against the selected unresolved checks.

The score files and judgements use LF. Only the score line endings were
normalized in round 2; every JSON value remains unchanged. The reproduction
test gives score.main an explicit UTF-8 LF stdout sink so both score files
reproduce byte for byte on either platform. A plain CLI's newline bytes follow
its stdout configuration. Git's text=unset rule preserves the stored bytes on
checkout and checkin. A full clone containing fb7765763 in local history is
required; the scorer resolves that historical commit for its metadata.

## Validation

TEMP and TMP are C:\t\hp3. The command tests use synthetic fixtures and dummy
provider credentials. The holdout tests block network, provider budget and
key access and compare the supplied files again at cleanup.

| Command | Result |
| --- | --- |
| python -m unittest tests.test_judge_accounts -v | 32 passed, zero failures, errors or skips. |
| python -m unittest tests.test_holdout -v | 5 passed, zero failures, errors or skips. |
| python -m unittest tests.test_eval_score tests.test_citations tests.test_spec -v | 85 passed: 57 scorer, 11 citation and 17 spec; zero failures, errors or skips. |
| python scripts/check_hygiene.py --all | Exit 0, zero findings. |
| python scripts/check_citations.py | Exit 0, zero findings. |
| git diff --check | Exit 0, zero findings. |

Fix round 2 validation covers 122 distinct requested tests on Windows with
Python 3.12.10. Before the fix, two new regression methods produced eight
published-path subtest failures and one missing --labels parser error. The
no-op writer mutation separately produced one expected failure after the fix,
with 252 suppressed writes and zero new judgements. The table reports the
passing production runs. No full suite or Linux rerun was made for this small
fix, as requested.

The holdout module checks the exact input order and label store digest,
compares all 252 historical judgement files, reproduces both historical
scores byte for byte through both receiver helpers and the command, and checks Git
text attributes for all 1,657 holdout files, including the frozen label copy
and digest file. The protected-file checksum matches the pre-edit snapshot.

## Verified sources

The command uses the existing receiver interfaces without introducing a new
external API. These primary sources were checked again on 22 November 2026.
The source and draw checks are also listed in HOLDOUT.md.

* [NEAR account and contract RPC](https://docs.near.org/api/rpc/contracts):
  account queries and read-only contract calls, including UNKNOWN_ACCOUNT
  meaning an account does not exist. The five retained Tether view_account
  responses carry that cause. The four absent age fixtures and the f712
  recorded empty keys response were checked against the retained judgements.
* [FastNEAR neardata server](https://github.com/fastnear/neardata-server) and
  [its OpenAPI](https://raw.githubusercontent.com/fastnear/neardata-server/main/openapi/openapi.yaml):
  block and receipt data used by the recorded draw, whose omissions are
  disclosed in RULE.md.
* [Git attributes](https://git-scm.com/docs/gitattributes): unsetting text
  disables end-of-line conversion on checkout and checkin, regardless of the
  inherited eol value. git check-attr confirms text is unset for every supplied
  holdout file.

## Owner steps and bounds

The orchestrator must review and commit the working tree and run the full
suite. No new record run, paid model call, transfer or deployment is needed
to reproduce these retained results. This lane made no commit, push, PR,
remote or tag change, transaction, paid call or deployment.

For a new measurement, draw a fresh holdout, retain its rule before judging,
sort the account ids as specified and choose a new output directory. An
intentional --record run needs the existing external provider token file
and shared budget database described in [judge-accounts.md](judge-accounts.md).
Do not record into the frozen 22 November run. This holdout is spent once its
accounts enter the label store, and its now public results cannot be used
as unseen evidence for a later rule change.
