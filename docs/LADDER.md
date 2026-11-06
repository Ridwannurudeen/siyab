# The reports ladder

This page fixes how a community report changes a payment verdict. `intel/reports.py` `effect(summary, payee_on_allowlist)` is the only code that maps a report summary to an effect, and it must match this table row for row. No other module may map report states to outcomes.

## The summary shape

The reports contract's `subject_summary(subject)` view is the source of truth. It returns exactly these fields:

| Field | Type | Meaning |
|---|---|---|
| `accepted` | u64 | Accepted reports on the subject, plus contested ones. A dispute does not un-accept a report; only an overturn does. |
| `pending` | u64 | Reports waiting for moderation. |
| `contested` | u64 | Accepted reports with an open dispute. Always at most `accepted`. |
| `latest_accepted_id` | u64 or null | The report counter id of the newest accepted or contested report. Null exactly when `accepted` is 0. |
| `deny` | bool | True when any accepted or contested report has kind scam, theft, phishing, exploit or key_compromise. |

A summary that is missing a field, has an extra field, has a field of the wrong type, has `contested` above `accepted`, or has `deny` true or an id present while `accepted` is 0 is unreadable. A failed read is also unreadable.

## The ladder

Deny beats unknown, and unknown beats no effect. The allowlist suppresses only an ask. It never lifts a deny and never forgives an unreadable summary.

| Summary | Payee allowlisted | Effect | Why |
|---|---|---|---|
| unreadable | either | unknown | A read we cannot trust is never a pass. |
| `deny` true | either | deny | An accepted or contested report of a deny kind blocks the payment, whatever the allowlist says. |
| `deny` false, `accepted` above 0 | no | unknown | Every accepted report is then social. A human was persuaded, so a human decides. |
| `deny` false, `accepted` above 0 | yes | none, with a detail line | The owner already vouched for this payee, and the allowlist suppresses an ask. |
| `pending` above 0 only | either | none, with a detail line | A pending report is unmoderated. It is shown, not acted on, until the contract can count only pending reports from submitters with an accepted history. |
| nothing | either | none | |

## Why pending reports do not ask yet

A report costs a deposit of 0.023004 NEAR, and accounts are cheap. If a pending report made every payment to a payee off the allowlist ask a human, anyone could stall payments to any vendor for as long as moderation takes. The planned fix is a separate view that counts only pending reports from submitters with at least one accepted report. Until that view exists and the rules read it, a pending report is a detail line with no effect.

## Changing this page

The table, the contract view and `effect` change together, in one pull request, with the matrix test in `tests/test_reports_rules.py` updated to cover every row.
