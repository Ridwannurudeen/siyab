# The reports ladder

This page fixes how a community report changes a payment verdict. One function, `effect(summary, payee_on_allowlist)` in `intel/reports.py`, maps a report summary to an effect, and it must match this page row for row and string for string. No other module may map report states to outcomes. `effect` and its matrix test, `tests/test_reports_rules.py`, are implemented in the current source.

## The summary shape

`latest_deny_id` was added to the view before the contract's first deployment, in the same change as this page.

The contract's view (`subject_summary(subject)` in `contracts/reports/src/lib.rs`) is the source of truth. It returns exactly these six fields, named in `intel/reports.py` as `SUMMARY_FIELDS`:

| Field | Type | Meaning |
|---|---|---|
| `accepted` | u64 | Accepted reports on the subject, plus contested ones. A dispute does not un-accept a report; only an overturn does. |
| `pending` | u64 | Reports waiting for moderation. |
| `contested` | u64 | Accepted reports with an open dispute. Never above `accepted`. |
| `latest_accepted_id` | u64 or null | The id of the newest accepted or contested report of any kind. Null exactly when `accepted` is 0, and present exactly when `accepted` is above 0. |
| `latest_deny_id` | u64 or null | The id of the newest accepted or contested report whose kind is scam, theft, phishing, exploit or key_compromise. Null exactly when `deny` is false. |
| `deny` | bool | True while the contract's `accepted_deny` counter is above 0. The counter rises when a report of a deny kind is accepted and falls when such a report is overturned (`moderate` and `resolve_dispute` in `contracts/reports/src/lib.rs`). |

A summary is unreadable when any of these holds: it is not an object; a field is missing or extra; a count is not a non negative integer; `deny` is not a boolean; `contested` is above `accepted`; `latest_accepted_id` is null while `accepted` is above 0, or present while `accepted` is 0; `latest_deny_id` is null while `deny` is true, or present while `deny` is false; `deny` is true while `accepted` is 0. A failed read is also unreadable. `_readable` in `intel/reports.py` checks every clause, and the matrix test has one case per clause.

## What `effect` returns

`effect(summary, payee_on_allowlist)` returns a dict with exactly three keys:

| Key | Value |
|---|---|
| `effect` | one of the strings `deny`, `ask`, `none`, `unreadable` |
| `report_id` | the integer id the detail cites (`latest_deny_id` for `deny`, `latest_accepted_id` for the social rows), or null |
| `detail` | the detail string from the table below, verbatim, with the counts filled in |

The rules module (`gate/rules.py`) builds the `community_report` check from it: status `signal` for `deny`, `unknown` for `ask` and `unreadable`, `pass` for `none`; the check detail is `reports for <receiver>: ` followed by the `detail` string. A failed read never reaches `effect`; the rules module records effect `unreadable` with the detail `subject_summary could not be read: <why>` and the same status. There is no separate pending check.

## The ladder

Deny beats ask, ask beats none, and unreadable is its own outcome so that "we could not check" is never confused with "we checked and a human must decide". The allowlist suppresses only an ask. It never lifts a deny and never forgives an unreadable summary.

| Summary | Payee allowlisted | Effect | Detail string | Why |
|---|---|---|---|---|
| unreadable | either | `unreadable` | `the summary is not in the fixed shape; pending unknown` | A read we cannot trust is never a pass. The verdict is UNKNOWN and unsigned, with the reason text "community reports could not be checked". |
| `deny` true | either | `deny` | `deny report <latest_deny_id> is accepted; <contested> under dispute; pending <pending>` | An accepted or contested report of a deny kind blocks the payment, whatever the allowlist says. The cited id is the deny report, never a newer social one. |
| `deny` false, `accepted` above 0 | no | `ask` | `<accepted> accepted social report(s), latest id <latest_accepted_id>; a human decides; pending <pending>` | Every accepted report is social. A human was persuaded, so a human decides. The verdict is UNKNOWN and unsigned, with the reason text "community reports need a human decision". |
| `deny` false, `accepted` above 0 | yes | `none` | `<accepted> accepted social report(s), latest id <latest_accepted_id>; the payee is on the allowlist, so no ask; pending <pending>` | The owner already vouched for this payee, and the allowlist suppresses an ask. |
| `pending` above 0 only | either | `none` | `<pending> pending report(s) await moderation; no effect` | A pending report is unmoderated. It is shown, not acted on, at every amount. |
| nothing | either | `none` | `no community report; pending 0` | |

`report_id` is `latest_deny_id` in the deny row, `latest_accepted_id` in the two social rows, and null otherwise.

## Why pending reports have no effect

The `pending` field is a count only; pending reports do not change a verdict.

The source contract at version 0.1.2 requires a non-refundable deposit of exactly `REPORT_DEPOSIT`, 30000000000000000000000 yoctoNEAR (0.03 NEAR; `contracts/reports/src/lib.rs:9`, `:705-710`). Testnet still runs deployed Reports version 0.1.1 (`res/BUILD-reports.md:7`), which requires 23136000000000000000000 yoctoNEAR (0.023136 NEAR; `docs/REPORTS.md:3`); the source changes that deployed amount. Each account may hold five pending reports at a time (`MAX_PENDING_REPORTS` in `contracts/reports/src/lib.rs`); accounts are cheap. If a pending report made a payment ask a human, five reports per cheap account could stall payments to any payee for as long as moderation takes. The Gate already bounds what an agent can send to a payee off its allowlist through the unknown payee caps (`request_transfer` in `contracts/gate/src/lib.rs`), so an unmoderated report adds information, not protection. A rule that counts only pending reports from submitters with accepted history was considered and dropped: it judges who reported rather than what was reported, and the contract's `publisher_stats.accepted` is a current count, not a history. Whether a pending report should ask above an amount threshold is decided on testnet data; the entry in `docs/DECISIONS.md` records it.

## Availability

The reports read uses the same RPC client as the other tier 1 reads, so an RPC outage blocks every payment already; an outage of the reports contract alone makes every verdict UNKNOWN. That is the design: UNKNOWN is unsigned, and the Gate pays only on a signed ALLOW. The operator's off switch is to unset `REPORTS_ACCOUNT_ID`, after which no community report check runs (`test_unset_reports_account_preserves_the_existing_checks` in `tests/test_reports_rules.py`); the owner can also move funds through the Gate's owner `withdraw` without a verdict (`contracts/gate/src/policy.rs`). Both are logged in `docs/LOG.md` when used.

## Changing this page

The table, the contract view, `SUMMARY_FIELDS`, `effect` and the matrix test change together, in one pull request. A richer view is a new contract method with a new name; `subject_summary` keeps these six fields.
