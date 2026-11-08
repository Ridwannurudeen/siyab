# Reporting and incident policy

This page is the published policy for community reports on ShieldBot for NEAR: who can file one, what it costs, who decides it, how fast, how the account a report names asks for review, and how a mistake is corrected. The contract behind it is `contracts/reports` (`docs/REPORTS.md` lists its methods and tests). The decisions it applies are entries 5 and 6 of `docs/DECISIONS.md`, including the 2026-11-07 pricing note under entry 5; each rule below names the function in `contracts/reports/src/lib.rs` that enforces it.

The reports contract is deployed on testnet at `reports.kestrelridge-main.testnet`, its code hash matching the reproducible build (`res/BUILD-reports.md`, Deployed line; `docs/TESTNET.md`, the Reports rows). The read back after the 0.1.2 redeploy shows 0 reports and 0 disputes (`docs/TESTNET.md`), and the init call of the first deploy named `kestrelridge-owner.testnet` as the only moderator (`res/BUILD-reports.md`). No report has been filed, moderated or disputed under this policy yet. The source crate is version `0.1.3` (`contracts/reports/Cargo.toml`); testnet runs deployed Reports version `0.1.2` per `res/BUILD-reports.md`. Whether the verdict service reads reports is controlled by `REPORTS_ACCOUNT_ID` (see What a report does to a payment).

## Who can report, and how

Anyone with a NEAR account can report. `submit_report(subject, kind, outcome, evidence_sha256, evidence_url)` in `contracts/reports/src/lib.rs` keeps no list of reporters: the caller is the submitter. Testnet runs deployed Reports version `0.1.2`, which requires exactly `REPORT_DEPOSIT`, 0.03 NEAR (30000000000000000000000 yoctoNEAR; `contracts/reports/src/lib.rs:9`, `:853-858`). Any other amount is refused with `REFUSED:bad_deposit`. The contract also refuses a subject that is not a valid NEAR account id (`REFUSED:bad_subject`), an evidence URL that does not start with `https://` or is over 512 bytes (`REFUSED:bad_evidence_url`), an evidence hash that is not 64 lowercase hexadecimal characters (`REFUSED:bad_hash`), and a sixth pending report from the same submitter (`REFUSED:pending_limit`).

- `subject` is the account agents should not pay. It is never the victim (`docs/REPORTS.md`).
- `kind` and `outcome` take the values in `intel/record_enums.json`: kind is what happened, outcome is what it cost. The kind decides whether an accepted report denies; the outcome is recorded and never weighed (`docs/DECISIONS.md`, entry 4).
- `evidence_sha256` is the sha256 of one evidence document, and `evidence_url` is where a reader fetches it.

`scripts/report.sh <reports_account> <subject> <kind> <outcome> <evidence_sha256> <evidence_url>` makes the same call for the operator. It runs on testnet only (`require_testnet` in `scripts/lib.sh`), signs as `REPORTER_ACCOUNT_ID`, checks kind and outcome against `intel/record_enums.json`, attaches `REPORT_DEPOSIT` read from current source (`contracts/reports/src/lib.rs:9`, `scripts/report.sh:53-65`), and takes `--dry-run`. Testnet runs deployed Reports version `0.1.2`, so the script attaches the required 0.03 NEAR deposit (30000000000000000000000 yoctoNEAR); other amounts are refused with `REFUSED:bad_deposit` (`contracts/reports/src/lib.rs:853-858`). If `REPORTER_ACCOUNT_ID` is unset, it falls back to `OWNER_ACCOUNT_ID` and prints a warning (`scripts/report.sh:8-10,65`). It is the operator's tool, not the public path: a reporter can call `submit_report` from their own account with any NEAR client. The moderator should file reports from a separate account, never the moderator account; the independence rule below explains why.

A submitter can withdraw their own report while it is pending (`withdraw`, exactly 1 yoctoNEAR). After a decision it cannot be withdrawn.

## The deposit is a storage fee

Testnet runs deployed Reports version `0.1.2`, which fixes `REPORT_DEPOSIT` at 0.03 NEAR (30000000000000000000000 yoctoNEAR; `contracts/reports/src/lib.rs:9`, `:853-858`). It is a non-refundable storage fee, not a bond: the contract has no refund path in any state, the rows it pays for are permanent, and an accepted report, a withdrawn one and an overturned one all keep it. The owner fixed this public price in source 0.1.2 on the advisor's recommendation; the test keeps it between 1.2 and 2 times the measured worst case storage, and `report_deposit()` returns it (`docs/DECISIONS.md`, entry 5, 2026-11-07 note).

The deposit is attached exactly for the version receiving the call. Testnet runs deployed Reports version `0.1.2`, which checks for 0.03 NEAR (30000000000000000000000 yoctoNEAR) in `assert_report_deposit` (`contracts/reports/src/lib.rs:853-858`). Any other amount is refused with `REFUSED:bad_deposit`. The scripts attach the current source amount, and a caller using another client must attach the exact amount required by the deployed version.

## Who moderates

There is one moderator: Ridwan, the owner (`docs/DECISIONS.md`, entry 6: "The owner is the only moderator at the first deployment"). Every accept, reject and dispute decision on this deployment is one person's decision. The contract can enforce distinct accounts, not independent humans: `new` and `set_moderators` refuse a list that names the same account twice (`assert_distinct_moderators`, `REFUSED:duplicate_moderator`), and with one moderator there is no second account to enforce.

The moderator decides with `moderate(id, accept, reason_sha256)` (exactly 1 yoctoNEAR; `scripts/moderate.sh <reports_account> <report_id> <accept|reject> <reason_sha256>`). The contract records the moderator, the reason hash and the time on the report. Testnet runs deployed Reports version `0.1.2`, which refuses a moderator who submitted the report or is its subject (`REFUSED:self_moderation`; `contracts/reports/src/lib.rs:431-432`).

## What a moderator checks

The review criteria, reason publication and response times below are the operator's policy; the contract enforces only its explicit checks in `contracts/reports/src/lib.rs`, including the exact `REPORT_DEPOSIT`, moderator membership, refusal of self-moderation, and its other `REFUSED:` conditions.

1. The evidence document is fetchable at `evidence_url` and hashes to `evidence_sha256`. A document that cannot be fetched, or does not hash to that value, is a rejection.
2. The document shows the subject account doing what the kind says, in a transaction, a contract or a public source a reader can check for themselves. A document that only asserts it is a rejection.
3. The subject is the account agents should not pay. A report whose subject is the account that lost funds is rejected, whatever else it shows.
4. The kind matches the evidence. A deception with no compromised key or contract is `social`. The moderator does not change a report's kind; a report with the wrong kind is rejected, and the submitter files a new one.
5. The evidence names no person and gives no motive. A report is about an account.

The reason for every decision is written as text, and `reason_sha256` is the sha256 of its UTF-8 bytes. The moderator publishes that text with the report id in `docs/LOG.md` when deciding, so anyone can hash it and compare it with the reason hash on chain. When an incident record exists for the report, the text is carried in it as well (`docs/RECORD.md`).

## Service times

The `acknowledge` step is 72 hours, the `decide` step is 168 hours, and the `dispute_decision` step is 168 hours for the one moderator. The `correction` step is 72 hours for the single registry operator. The machine readable copy is `docs/service_times.json`, and `tests/test_reporting_docs.py` fails when this prose, the table, and that file disagree.

| Step | Time | What starts and stops the clock |
|---|---|---|
| `acknowledge` | 72 hours | From the report's `submitted_ms` to a dated line in `docs/LOG.md` naming the report id. |
| `correction` | 72 hours | From the dated `docs/LOG.md` line that records confirmation to the correcting transaction or commit. |
| `decide` | 168 hours | From the report's `submitted_ms` to its `report_moderated` event. |
| `dispute_decision` | 168 hours | From the dispute's `submitted_ms` to its `dispute_resolved` event. |

Report times use the on-chain timestamps and log entries in their rows; a correction uses the dated confirmation line and correcting transaction or commit. The windows run on calendar hours, every day. There is one moderator and one correction operator; there is no faster lane or same-day promise. When a report decision will miss its time, the log says so before the time runs out, and a miss stays in the log as a miss.

`resolve_dispute` overwrites the report's `moderated_ms` with the time of the dispute decision, so the time of the first decision is read from the `report_moderated` event, not from `get_report`.

## How a subject asks for review

The account a report names asks for review by filing a dispute against the accepted report: `dispute(report_id, evidence_sha256, evidence_url)` with `REPORT_DEPOSIT` attached, or `scripts/dispute.sh <reports_account> <report_id> <evidence_sha256> <evidence_url>` for the operator. The evidence document says why the decision is wrong: a source the report misread, a transaction that shows the subject is not the account that acted, or a statement from the account holder.

- Only an accepted report can be disputed (`REFUSED:not_accepted`). A pending report has no effect on a payment, so there is nothing yet to review. A rejected report is final; a new report starts again.
- One dispute is open at a time per report (`REFUSED:dispute_open`).
- There is no lifetime cap on disputes in the source (see The dispute cap).
- A disputed report stays counted as accepted until it is overturned (`docs/REPORTS.md`). Filing a dispute does not switch a report off.
- A dispute costs the same non refundable storage fee as a report.
- Anyone may file a dispute, not only the subject.

The moderator resolves it with `resolve_dispute(dispute_id, overturn, reason_sha256)` (exactly 1 yoctoNEAR; `scripts/dispute.sh <reports_account> --resolve <dispute_id> <uphold|overturn> <reason_sha256>`). Upheld, the report returns to accepted. Overturned, the report leaves the accepted counts for good; overturned is terminal. The reason text is published as for any decision.

## The independence rule, and its limit

Decided (`docs/DECISIONS.md`, entry 6). Testnet runs deployed Reports version `0.1.2`; it enforces the following:

- Reports 0.1.2 refuses a moderator who submitted the report or is its subject (`REFUSED:self_moderation`; `contracts/reports/src/lib.rs:431-432`). During dispute resolution, it always refuses the report subject as moderator (`:670`).
- With more than one moderator, Reports 0.1.2 checks the original accepter stored under prefix `ab`; it falls back to `report.moderator` only when `ab` is absent and `upheld_disputes` is 0, and writes that fallback to `ab` before changing `report.moderator` (`contracts/reports/src/lib.rs:675-716`, `:686-709`). A missing entry after an upheld dispute can remain only when the report already had at least one upheld dispute under 0.1.1 before the 7 Nov redeploy; that legacy state fails closed with `REFUSED:same_moderator` while multiple moderators are configured (`:686-691`). It also refuses the report submitter and dispute submitter as resolvers (`:692-696`). With one moderator, those accepter and submitter checks are skipped, but the report-subject check still applies.

With one moderator the accepter and submitter independence checks do not apply: `resolve_dispute` lets the only moderator decide a dispute on a report they accepted, unless that moderator is also the report subject. A subject who disputes is asking the same person to look again, with new evidence. That is the limit of this deployment, and it ends only when a second moderator is named.

The first rule decides which account files. The only moderator is the owner, so a report filed from the owner account can never be decided: `moderate` refuses it with `REFUSED:self_moderation`. The moderator therefore files reports from a separate account. The moderator files disputes from a separate account too, so that no moderator decides a dispute they filed; this is policy on the one-moderator testnet deployment, while source 0.1.2 refuses dispute submitters as resolvers only when more than one moderator is configured. `scripts/report.sh` signs as `REPORTER_ACCOUNT_ID` and `scripts/dispute.sh` opens a dispute as `DISPUTER_ACCOUNT_ID`; each falls back to `OWNER_ACCOUNT_ID` with a warning when unset (`scripts/report.sh:8-10`, `scripts/dispute.sh:49-51`), and a report filed that way cannot be moderated by the owner.

## The dispute cap

Testnet runs deployed Reports version `0.1.2`; it has no lifetime dispute cap (`docs/REPORTS.md`).

Testnet runs deployed Reports version `0.1.2` per `res/BUILD-reports.md`; it has no lifetime dispute cap.

## Corrections

Nothing is edited. A report row and its body hash are permanent; the contract has no edit and no delete.

- A wrong decision is corrected by a dispute and an overturn, with the reason published.
- A wrong fact in a report is corrected by a new report. The old one is withdrawn while pending, or disputed once accepted.
- A wrong fact in an incident record is corrected by a new record whose `supersedes` names the old one (`docs/RECORD.md`). The old record keeps its bytes and its id.
- A wrong label store row is corrected by a new row and the old row's `superseded_by` (`docs/LABEL-RECORD.md`).

## What a report does to a payment

If `REPORTS_ACCOUNT_ID` is unset, `gate/rules.py` makes no `subject_summary` call and adds no `community_report` check. The verdict CLI, agent commands and MCP server pass this setting into verdict requests (`gate/__main__.py`, `gate/agent.py`, `mcp/server.py`). When it is set, `gate/rules.py` reads `subject_summary` for the receiver and applies `effect` in `intel/reports.py`. An accepted or contested deny report yields a signal and `DENY`; an accepted social report asks for human review (`UNKNOWN`) off the allowlist and has no report effect on it. The `pending` field is a count only and a pending-only summary has no effect. A failed or unreadable summary yields `UNKNOWN` (`gate/rules.py`, `intel/reports.py`; see `docs/LADDER.md` for the effect ladder).

## What this policy does not cover

A report says an account should not be paid, with evidence. It does not say who controls the account, and it is not a finding that anyone committed a crime. No document under this policy names an account as a hostile party or a victim without a cited source (`docs/DECISIONS.md`, entry 12); every example in these pages is synthetic and says so. There is no contact channel off chain on main: the contract calls above are the only intake.

## Approvals threshold in source 0.1.3

The source keeps `state_version` at 1 and leaves the Reports root state and existing stored records unchanged. The threshold is stored under a new key and defaults to 1 when that key is absent. Votes are stored by report id under a separate new prefix.

The owner sets `set_approvals_required(n)` with exactly 1 yoctoNEAR. The value must be at least 1 and no greater than the configured moderator count. When there is more than one moderator, it must be strictly less than that count; otherwise the call is refused as `REFUSED:bad_threshold`. `approvals_required()` returns the current value. `new` and `set_moderators` refuse more than 7 moderators as `REFUSED:too_many_moderators`; `set_moderators` refuses a list shorter than the threshold as `REFUSED:threshold_above_moderators` and refuses a list equal to a threshold above 1 as `REFUSED:bad_threshold`. The owner lowers the threshold before shrinking the list. With a threshold above 1, the strict inequality leaves a current moderator outside the votes needed to accept a report, available to resolve a dispute under the existing resolver rules.

With a threshold of 1, `moderate` retains its existing behavior and event payloads, and it writes no vote entry. The Reports event envelope version is now `1.1.0` for every event; the threshold-1 payloads are unchanged, and `intel.reconcile` accepts both `1.0.0` and `1.1.0`. With a higher threshold, each `moderate` call records or replaces that moderator's vote and reason hash, then tallies only votes from the current moderator list. A removed moderator's vote stops counting and counts again if the account is re-added. The report changes from pending only when the current votes reach the threshold on one side. `report_vote` records `report_id`, `moderator`, `side`, `reason_sha256`, `accept_votes`, `reject_votes` and `approvals_required`; a completed decision also emits the existing `report_moderated` event. `report_votes(id)` shows the stored votes.

Moderation vote storage is paid from the contract balance, not the reporter's deposit, so the operator funds moderation storage. Testnet still runs deployed Reports version `0.1.2` with one moderator and emits event envelopes with version `1.0.0`. The threshold remains 1 until a second moderator exists and the owner deploys source `0.1.3`, which emits version `1.1.0` envelopes.

With more than one moderator, `resolve_dispute` refuses a moderator who has a recorded accept vote on the report, with `REFUSED:same_moderator`, in addition to refusing the accepting moderator under `ab`. The strict threshold rule leaves at least one current moderator outside the votes needed to accept a threshold-based decision, so a dispute can be resolved without changing the threshold. With one moderator, the existing rule still lets that moderator resolve a dispute on a report they accepted, unless the moderator is also the report subject. The owner can keep the threshold at 1 and the list at one account; this protects the contract from a moderator key that is not the owner's.

## Pending count recordings

`python -m intel reports-pending-counts` reads `REPORTS_ACCOUNT_ID` through `gate.rpc.Client` and appends one JSON line to `fixtures/reports/pending-counts.jsonl`. Each line records the UTC time, the block height, contract `totals`, and each subject's pending count from its reports. No view lists subjects, so the recorder walks `get_report(id)` from id 0 and checks each distinct subject with `subject_summary`; it records both pending counts and flags any mismatch. The scan is capped at 500 report ids and marks a truncated scan in the row.

The testnet contract holds no reports, so these lines are expected to read zero until outside reporters exist. Two weeks of lines are the data for the pending policy decision in `docs/LADDER.md`; no decision has been taken yet. Empty lines may not be used as evidence for a value threshold.
