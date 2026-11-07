# Reporting and incident policy

This page is the published policy for community reports on ShieldBot for NEAR: who can file one, what it costs, who decides it, how fast, how the account a report names asks for review, and how a mistake is corrected. The contract behind it is `contracts/reports` (`docs/REPORTS.md` lists its methods and tests). The decisions it applies are entries 5 and 6 of `docs/DECISIONS.md`, and each rule below names the function in `contracts/reports/src/lib.rs` that enforces it.

The reports contract is deployed on testnet at `reports.kestrelridge-main.testnet`, its code hash matching the reproducible build (`res/BUILD-reports.md`, Deployed line; `docs/TESTNET.md`, the Reports rows). The read back after the deploy shows 0 reports and 0 disputes, and `kestrelridge-owner.testnet` as the only moderator (`docs/TESTNET.md`). No report has been filed, moderated or disputed under this policy yet. The source crate is version `0.1.1` (`contracts/reports/Cargo.toml`); testnet still runs `0.1.0` per `res/BUILD-reports.md`. Whether the verdict service reads reports is controlled by `REPORTS_ACCOUNT_ID` (see What a report does to a payment).

## Who can report, and how

Anyone with a NEAR account can report. `submit_report(subject, kind, outcome, evidence_sha256, evidence_url)` in `contracts/reports/src/lib.rs` keeps no list of reporters: the caller is the submitter. The contract refuses a deposit other than exactly `REPORT_DEPOSIT` (`REFUSED:bad_deposit`), a subject that is not a valid NEAR account id (`REFUSED:bad_subject`), an evidence URL that does not start with `https://` or is over 512 bytes (`REFUSED:bad_evidence_url`), an evidence hash that is not 64 lowercase hexadecimal characters (`REFUSED:bad_hash`), and a sixth pending report from the same submitter (`REFUSED:pending_limit`).

- `subject` is the account agents should not pay. It is never the victim (`docs/REPORTS.md`).
- `kind` and `outcome` take the values in `intel/record_enums.json`: kind is what happened, outcome is what it cost. The kind decides whether an accepted report denies; the outcome is recorded and never weighed (`docs/DECISIONS.md`, entry 4).
- `evidence_sha256` is the sha256 of one evidence document, and `evidence_url` is where a reader fetches it.

`scripts/report.sh <reports_account> <subject> <kind> <outcome> <evidence_sha256> <evidence_url>` makes the same call for the operator. It runs on testnet only (`require_testnet` in `scripts/lib.sh`), signs as `REPORTER_ACCOUNT_ID`, checks kind and outcome against `intel/record_enums.json`, attaches `REPORT_DEPOSIT` read from `contracts/reports/src/lib.rs`, and takes `--dry-run`. If `REPORTER_ACCOUNT_ID` is unset, it falls back to `OWNER_ACCOUNT_ID` and prints a warning (`scripts/report.sh:8-10,65`). It is the operator's tool, not the public path: a reporter can call `submit_report` from their own account with any NEAR client. The moderator should file reports from a separate account, never the moderator account; the independence rule below explains why.

A submitter can withdraw their own report while it is pending (`withdraw`, exactly 1 yoctoNEAR). After a decision it cannot be withdrawn.

## The deposit is a storage fee

Filing a report or a dispute costs `REPORT_DEPOSIT`, 0.023136 NEAR (23136000000000000000000 yoctoNEAR) (`docs/REPORTS.md` gives the measurement it is priced from). It is a non refundable storage fee, not a bond: the contract has no refund path in any state, the rows it pays for are permanent, and an accepted report, a withdrawn one and an overturned one all keep it (`docs/DECISIONS.md`, entry 5).

The deposit is attached exactly. `assert_report_deposit`, which `submit_report` and `dispute` call first, refuses any amount other than `REPORT_DEPOSIT`, less or more, with `REFUSED:bad_deposit`. `scripts/report.sh` and `scripts/dispute.sh` attach the exact amount. A reporter using another client attaches exactly `REPORT_DEPOSIT`.

## Who moderates

There is one moderator: Ridwan, the owner (`docs/DECISIONS.md`, entry 6: "The owner is the only moderator at the first deployment"). Every accept, reject and dispute decision on this deployment is one person's decision. The contract can enforce distinct accounts, not independent humans: `new` and `set_moderators` refuse a list that names the same account twice (`assert_distinct_moderators`, `REFUSED:duplicate_moderator`), and with one moderator there is no second account to enforce.

The moderator decides with `moderate(id, accept, reason_sha256)` (exactly 1 yoctoNEAR; `scripts/moderate.sh <reports_account> <report_id> <accept|reject> <reason_sha256>`). The contract records the moderator, the reason hash and the time on the report. `moderate` refuses a moderator deciding a report they submitted (`REFUSED:self_moderation`).

## What a moderator checks

The review criteria, reason publication and response times below are the operator's policy; the contract enforces only its explicit checks in `contracts/reports/src/lib.rs`, including the exact `REPORT_DEPOSIT`, moderator membership, refusal of self-moderation, and its other `REFUSED:` conditions.

1. The evidence document is fetchable at `evidence_url` and hashes to `evidence_sha256`. A document that cannot be fetched, or does not hash to that value, is a rejection.
2. The document shows the subject account doing what the kind says, in a transaction, a contract or a public source a reader can check for themselves. A document that only asserts it is a rejection.
3. The subject is the account agents should not pay. A report whose subject is the account that lost funds is rejected, whatever else it shows.
4. The kind matches the evidence. A deception with no compromised key or contract is `social`. The moderator does not change a report's kind; a report with the wrong kind is rejected, and the submitter files a new one.
5. The evidence names no person and gives no motive. A report is about an account.

The reason for every decision is written as text, and `reason_sha256` is the sha256 of its UTF-8 bytes. The moderator publishes that text with the report id in `docs/LOG.md` when deciding, so anyone can hash it and compare it with the reason hash on chain. When an incident record exists for the report, the text is carried in it as well (`docs/RECORD.md`).

## Service times

These are the times one moderator can hold: the `acknowledge` step is 72 hours, the `decide` step is 168 hours, and the `dispute_decision` step is 168 hours. The machine readable copy is `docs/service_times.json`, and `tests/test_reporting_docs.py` fails when this prose, the table, and that file disagree.

| Step | Time | What starts and stops the clock |
|---|---|---|
| `acknowledge` | 72 hours | From the report's `submitted_ms` to a dated line in `docs/LOG.md` naming the report id. |
| `decide` | 168 hours | From the report's `submitted_ms` to its `report_moderated` event. |
| `dispute_decision` | 168 hours | From the dispute's `submitted_ms` to its `dispute_resolved` event. |

Each time is measured from an on chain timestamp and the log, so a reader can check it. The clock runs on calendar hours, every day, and there is no faster lane: with one moderator, nothing on this page promises a same day answer. When a decision will miss its time, the log says so before the time runs out, and a miss stays in the log as a miss.

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

Decided (`docs/DECISIONS.md`, entry 6), and enforced in the deployed code:

- A moderator cannot moderate a report they submitted. `moderate` refuses it with `REFUSED:self_moderation`, whatever the size of the moderator list.
- The moderator who accepted a report cannot resolve a dispute on it while the moderator list has more than one member. `resolve_dispute` refuses it with `REFUSED:same_moderator`.

With one moderator the second rule does not apply: `resolve_dispute` lets the only moderator decide a dispute on a report they accepted. A subject who disputes is asking the same person to look again, with new evidence. That is the limit of this deployment, and it ends only when a second moderator is named.

The first rule decides which account files. The only moderator is the owner, so a report filed from the owner account can never be decided: `moderate` refuses it with `REFUSED:self_moderation`. The moderator therefore files reports from a separate account. The moderator files disputes from a separate account too, so that no moderator decides a dispute they filed; that is policy, and the contract does not check it. `scripts/report.sh` signs as `REPORTER_ACCOUNT_ID` and `scripts/dispute.sh` opens a dispute as `DISPUTER_ACCOUNT_ID`; each falls back to `OWNER_ACCOUNT_ID` with a warning when unset (`scripts/report.sh:8-10`, `scripts/dispute.sh:49-51`), and a report filed that way cannot be moderated by the owner.

## The dispute cap

The source has no dispute cap: `dispute` refuses only a report that is not accepted (`REFUSED:not_accepted`), a second open dispute (`REFUSED:dispute_open`, `contracts/reports/src/lib.rs:486`) and a deposit that is not exact (`REFUSED:bad_deposit`). `resolve_dispute` adds one to `upheld_disputes` when it upholds the report, saturating at 255, and keeps it only as a statistic (`contracts/reports/src/lib.rs:575`). Testnet's deployed 0.1.0 still has the five-upheld-dispute cap recorded in `res/BUILD-reports.md`; source 0.1.1 removes it. An overturned report is terminal. The reason is in `docs/DECISIONS.md` (2026-11-07 note): a cap on upheld disputes counted exactly the baseless ones and let five of them close review of a report for good.

Source version `0.1.1` has no dispute cap. Testnet still runs version `0.1.0` per `res/BUILD-reports.md`; that deployed code refuses a dispute once the report has five upheld disputes, until redeploy.

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
