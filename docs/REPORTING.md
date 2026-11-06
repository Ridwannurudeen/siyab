# Reporting, corrections and appeals

This document is policy, not a description of code. It states what we commit to do with a report,
not what a function does. The response times here are the single source in `intel/service_times.json`,
and `tests/test_service_times.py` fails if this page and that file disagree. Nothing in this page is
an assurance that we will catch a particular payment.

The one line that matters to a buyer: a verdict is advisory. We tell an agent what we found and
where we found it, the agent's own policy decides, and a refusal is not a claim that the payee has
committed a crime.

## What a report is

A report says that an account should not be paid, and it carries evidence: one document with its
sha256 and an https url, six fields in a canonical body (see `docs/RECORD.md`). `contracts/reports`
stores the canonical body hash, the evidence hash, the evidence url and the report id, and the
readable record lives off chain. The older registry contract stores an evidence hash only; the two
are separate contracts, and this page is about reports.

Reports are permissionless. Anyone may file one against any account, for a storage deposit, and
nothing is expected in return. Filing is not publishing: a `pending` report has no effect on
payments.

We accept six kinds: `scam`, `theft`, `phishing`, `exploit`, `key_compromise` and `social`. The
first five may deny a payment once accepted. `social` only asks, because a social report names a
deception rather than a compromised contract. Four outcomes: `loss`, `attempt`, `near_miss` and
`unknown`.

## What we do with it

| State | What a payment may do |
|---|---|
| `pending` | Ask a human, never block. |
| `accepted` | Block for the five denial kinds, ask for `social`. |
| `contested` | Keep the block it held while the appeal runs. A dispute is not a switch and cannot create one: the contract accepts a dispute on an accepted report only. |
| `rejected` | Nothing. |
| `withdrawn` | Nothing. |
| `overturned` | Nothing, and the correction is published. |

Two moderators accept a report above our volume threshold, and below it one moderator is enough.
The threshold and the two-moderator step are policy: `contracts/reports` has a moderator list and
one moderator per decision, and it does not enforce a second signature above a volume threshold.
Every decision records its reason hash on chain and its readable reason in the published record, so
the person affected can read why.

## Response times

We hold ourselves to these in 09:00 to 17:00 UTC, Monday to Friday, excluding Nigerian public
holidays. The measured times are published monthly, on the incident desk page, including the months
where we miss.

| Step | Time |
|---|---|
| Acknowledge a new report | 1 business day |
| First decision, accept or reject | 3 business days |
| Acknowledge an appeal | 1 business day |
| Decide an appeal | 5 business days |
| Acknowledge an urgent block on a live payment | 4 business hours |

If a decision will take longer, we say so before the deadline rather than after it. A missed
published time is published as a miss.

## Corrections and takedown

Anyone may dispute a record about them. We treat three cases differently.

A facts correction: the timeline entry was wrong, so we publish a new record that supersedes the old
one and states what changed. The old bytes stay readable, because a record that can be edited is not
evidence.

A wrong decision: the record is overturned, payments stop being blocked, and the reason is published
against it. We do not delete the history of having been wrong.

A takedown request that is not about facts, for example a legal complaint: we answer it, and where we
remove a record we publish that it was removed and on what basis, without the disputed detail. What
we will not do is quietly drop a record and let the next reader think it never existed.

## Appeals

An appeal is filed against a record, not against a subject in general, and it moves the record to
`contested`. A block that was in place stays in place while we read the appeal, and a rejection
stays a rejection until we answer. We answer with one of three outcomes, each published: upheld,
rejected with reasons, or overturned.

An appeal may include a registration record, an operating history, a signed statement from the
account holder, or evidence that the account was not the one named in the report. We do not require
a lawyer and we do not charge for an appeal.

## What we do not do

We do not attribute crimes to people. We do not say who controls an account. We do not provide
sanctions advice. We do not tell a customer whether their own transaction is lawful, and we do not
take custody, so a refusal never moves anyone's money.

Publisher statistics are counts of reports, decisions and outcomes. They are not an accuracy rate,
and we do not call them one.

## If you think this policy is wrong

Tell us and we will publish the disagreement in the incident log rather than resolve it quietly. The
policy is versioned with this repository, so the version that applied to a decision is the version in
the commit that made it.
