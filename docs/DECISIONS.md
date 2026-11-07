# Decisions

Dated entries, newest last. Each names what was decided, why, where it is enforced, and who decided. An entry is changed only by a later entry that cites it.

## 2026-11-06, entry 1: one summary shape, one ladder

Decided: `subject_summary` returns exactly six fields, `accepted`, `pending`, `contested`, `latest_accepted_id`, `latest_deny_id`, `deny`. `docs/LADDER.md` is the only ladder; `effect` in `intel/reports.py` is the only code that maps a summary to an effect, and it returns `{effect, report_id, detail}` with `effect` one of `deny`, `ask`, `none`, `unreadable`. The field set is one constant, `SUMMARY_FIELDS`. Any richer view is a new contract method; this view does not change after the first deployment.

Why: four competing summary shapes existed in one week. A fixed shape with a fail closed reader (`_readable`) means a contract change breaks a test, not a live verdict. A deny must cite the report that caused it, and `latest_accepted_id` can be a newer social report, so the view carries `latest_deny_id`.

Where: `contracts/reports/src/lib.rs` (`SubjectSummaryView`), `intel/reports.py`, `tests/test_reports_rules.py`, `docs/LADDER.md`.

Decided by: the orchestrator, from the critiques of 6 Nov.

## 2026-11-06, entry 2: pending reports are a count, not a signal

Decided: a pending report has no effect on a verdict at any amount. The count appears in the `community_report` detail. There is no separate pending check.

Why: five pending reports per cheap account would let anyone stall payments to any payee for the length of moderation. Judging reports by the submitter's history was rejected: it judges who reported, not what, and `publisher_stats.accepted` is a current count, not a history. The Gate's unknown payee caps already bound what can be sent to a payee off the allowlist. The open alternative, asking above an amount threshold measured against the agent's own unknown payee cap, is decided on two weeks of testnet pending counts.

Where: `docs/LADDER.md`, `intel/reports.py`, `gate/rules.py`.

Decided by: the orchestrator; the threshold question is open and dated for the end of November.

## 2026-11-06, entry 3: unreadable is its own outcome, and the service fails closed

Decided: a summary that cannot be read or is not in the fixed shape gives effect `unreadable`, status `unknown`, a verdict UNKNOWN, no signature, and a reason text distinct from the social ask. The off switch is unsetting `REPORTS_ACCOUNT_ID`; the owner's escape when nothing can sign is the Gate's owner `withdraw`. Both uses are logged in `docs/LOG.md`.

Why: every other check already reads unknown on a failed read, and the reports read uses the same RPC client as `receiver_exists`, so it adds no new provider dependency; the new dependency is the reports contract itself. Exempting allowlisted payees from a failed read would be a second ladder for outages.

Where: `intel/reports.py`, `gate/rules.py`, `tests/test_reports_rules.py` (`test_unset_reports_account_preserves_the_existing_checks`), `contracts/gate/src/policy.rs` (`withdraw`), `docs/LADDER.md`.

Decided by: the orchestrator; confirmed by the owner on 6 Nov.

## 2026-11-06, entry 4: deny is decided by kind, outcome is descriptive

Decided: a report is a deny report when its kind is scam, theft, phishing, exploit or key_compromise, whatever its outcome (loss, attempt, near_miss, unknown). Outcome is recorded and shown, never weighed. A social report never denies.

Why: a phishing attempt that failed is as much a reason not to pay the same account as one that succeeded. Weighing outcomes would need a second table and a second argument about every row.

Where: `contracts/reports/src/lib.rs` (`ReportKind::is_deny`), a unit test over every kind and outcome pair including the contested state, `docs/REPORTS.md`.

Decided by: the orchestrator.

## 2026-11-06, entry 5: deposits are a non refundable storage fee, attached exactly

Decided: `submit_report` and `dispute` require exactly `REPORT_DEPOSIT` attached; more or less is refused. Deposits are never refunded in any state, stay in the contract account, and fund the permanent rows; no method withdraws them. The amount is pinned to the measured storage with a 20 percent margin and re measured by cargo whenever a stored type changes.

Why: refundable escrow would need a settlement path and an accounting view for every terminal state; a fee priced at storage is the simplest rule that both bounds griefing and keeps the rows paid for. Exact attachment removes a silent retention of excess.

Where: `contracts/reports/src/lib.rs` (`assert_report_deposit`, the storage pin test), `contracts/reports/tests/sandbox.rs` (both deposits remain after an overturn), `docs/REPORTS.md`.

Decided by: the orchestrator; confirmed by the owner on 6 Nov.

## 2026-11-06, entry 6: moderation governance the contract can enforce

Decided, before the first deployment: a moderator cannot moderate a report they submitted; the moderator who accepted a report cannot resolve a dispute on it while the moderator list has more than one member; the dispute cap per report counts only disputes resolved against the disputer; `set_moderators` stays owner only with one yoctoNEAR and emits `moderators_updated`. Deferred: a threshold of distinct moderator approvals per accept, owner settable, default 1, added by a migration when a second moderator exists.

Why: a contract can enforce distinct accounts, not independent humans. With one moderator, independence is not enforceable and the README says so. Counting only upheld disputes stops five frivolous disputes from making a false accept permanent.

Where: `contracts/reports/src/lib.rs` (`moderate`, `resolve_dispute`, `dispute`), `docs/REPORTS.md`, README Trust model.

Decided by: the orchestrator; confirmed by the owner on 6 Nov. The owner is the only moderator at the first deployment; the owner names the second moderator later.

2026-11-07 note: the upheld only dispute cap is withdrawn. A counted dispute is by definition one the moderator judged baseless, and an overturn is terminal, so the cap counted exactly the frivolous disputes and let five of them, 0.11568 NEAR in total and fileable by the reporter's allies, close review of a report for ever. A subject exemption does not repair it: a contract with its keys removed cannot dispute from its own account. `MAX_DISPUTES` and `REFUSED:dispute_limit` are removed; the brakes are one open dispute per report and the exact deposit per dispute. The moderator may leave a baseless dispute open at no cost to the signal, since `deny` holds while the report is contested. `upheld_disputes` stays stored as a statistic and saturates at 255. Layout preserving; redeployed under UPGRADES rule 2 once `res/previous/` and the round trip test exist. Raised by the builder on 7 Nov.

Decided by: proposed by the plan reviewer and agreed by the interface owner on 7 Nov; the change is reviewed independently before merge.

## 2026-11-06, entry 7: upgrades, migrations and rollback

Decided: every deployment bumps the crate version and the redeploy script asserts the version and code hash it expects. A redeploy over existing state without an init call is allowed only when the root struct and every stored type are unchanged, proven by a sandbox round trip that deploys `res/previous/<crate>.wasm`, writes state, redeploys the candidate, reads the state back, exercises one new call, and redeploys the previous artifact again. Rollback by previous artifact is claimed only for such redeploys. Any layout change goes through a `migrate` init method that first asserts the stored state version, rehearsed in the sandbox on state written by the previous artifact, with a tested reverse migration or forward fix; the redeploy script runs it with an init call. New state lives under new, documented prefixes. The previous artifact of every crate is committed under `res/previous/` with its sha256.

Why: a path on one machine is not a rollback; a rollback over migrated state is not a rollback at all; and a test that reads the current artifact as "old" proves nothing once the artifact is swapped.

Where: `docs/UPGRADES.md`, `scripts/redeploy.sh`, `contracts/gate/tests/sandbox.rs`, `res/previous/`.

Decided by: the orchestrator; the owner decided that the Gate is redeployed over its existing state with the bounded prune, done right, not reopened.

## 2026-11-06, entry 8: build provenance per crate

Decided: one build record per crate, `res/BUILD-gate.md`, `res/BUILD-registry.md`, `res/BUILD-reports.md`, each with one `Source commit:` line that names a commit already on main and one `Deployed:` line that reads `pending` until the on chain code hash matches. The reproducible build job runs per crate from each record. A crate whose record says TBD has no wasm in `res/`, and a crate with a wasm has a 40 hex source commit and a matching sha256 file; the test states which, and never skips silently. No wasm enters `res/` whose source commit is not on main, and no deployed code hash exists that no commit on main records.

Why: both wasm files embed the source commit, so one record for two crates forces a pointless registry rebuild at every Gate change. The window between an artifact commit and its deployment must be honest at every instant, not only at the end.

Where: `res/BUILD-*.md`, `.github/workflows/ci.yml`, `tests/test_res.py`, `docs/TESTNET.md`.

Decided by: the orchestrator.

## 2026-11-06, entry 9: the Gate's bounded replay set

Decided: used verdict ids are pruned from an insertion order queue, at most four per `request_transfer`, with an owner method for older ids and a view that lists stored ids. Pruning an expired id is replay proof because the verifier refuses an expired verdict before the used check runs. The verifier also refuses a verdict whose `issued_ms` is more than 30 seconds in the future, so insertion order is expiry order to within that skew plus the maximum TTL, and no queue head can block pruning forever.

Why: the replay set grew without bound; the fix must keep the stored layout, keep replay refusal exact, and bound its own gas.

Where: `contracts/gate/src/lib.rs`, `contracts/gate/src/verdict.rs`, `contracts/gate/src/policy.rs`, `docs/VERDICT-RECORD.md`, `docs/UPGRADES.md`.

Decided by: the orchestrator.

## 2026-11-06, entry 10: the verdict service signs only what it would send

Decided: a Verdict object is produced only when the outcome is ALLOW and the action is `sign`. An ALLOW whose action is `ask` is returned unsigned with its reasons. The attack harness may request a signed ALLOW for the Gate's own policy refusals, and nothing else may.

Why: a signature the Gate would refuse is still a signature; a consumer that trusts it has been misled.

Where: `gate/verdict.py`, `gate/agent.py`, `gate/demo.py`, `mcp/server.py`, `tests/test_verdict.py`, `tests/test_mcp.py`.

Decided by: the orchestrator.

## 2026-11-06, entry 11: the consumer interface

Decided: the verdict API shape is what `gate.verdict.request` returns: `outcome`, `action`, `ask`, `verdict`, `request`, `checks`, `reasons`, `evidence_hash`, `evidence_path`, `latency_ms`, `message`. Each check is `{id, status, detail, source}` with status `pass`, `signal` or `unknown`; a check that did not run is absent, and `docs/API.md` lists which checks run per tier. Adapters translate `action` into the host's vocabulary and never re decide.

Why: one shape for every adapter; a second ladder in an adapter is the failure mode to avoid.

Where: `gate/verdict.py`, `gate/evidence.py`, `docs/VERDICT-RECORD.md`, `docs/API.md` when written.

Decided by: the orchestrator.

## 2026-11-06, entry 12: accounts are never named as hostile parties

Decided: no document, fixture, test or demo names an account as a hostile party or as a victim. Demo accounts are ours and are labelled as such; every demo report is synthetic and the page its evidence URL points at says so. A label row whose category is incident or denylist names a hostile role in its note only together with a source URL, checked per parsed row.

Why: a subject is an account agents should not pay; an accusation without a source is a liability, and a demo that reads as an accusation misleads a reader.

Where: `docs/TESTNET.md`, `scripts/check_hygiene.py`, `intel/labels.py`, `fixtures/`.

Decided by: the orchestrator.

## 2026-11-06, entry 13: evidence for every merge

Decided: a merge of a change under `contracts/` names a cargo log committed under `fixtures/runs/`, checked with `scripts/check_hygiene.py` before it is committed (that directory carries the 128 hex rule); a merge of a Python change carries the test runner's Ran and OK line in its message. No review verdict stands without that evidence. Until the repository is public, local logs are the evidence; the first public CI run is checked the same day the repository goes public.

Why: a review that did not run the tests is a reading, not a verdict; `docs/` is not exempt from the hex rule, so logs live under `fixtures/runs/`.

Where: `fixtures/runs/`, commit messages, `docs/LOG.md`.

Decided by: the orchestrator.
