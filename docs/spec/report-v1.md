# Community report

Version: 1 (events `shieldbot-near` `1.1.0`)

## Purpose

The reports contract (`contracts/reports`) stores community reports about NEAR accounts, moderation of those reports, and disputes against accepted ones. Each report's body is hashed when it is submitted, so a reader can check that the stored report is the one submitted. The `subject_summary` view condenses a subject's reports into six fields, and the verdict service reads only that view, through one rule, the ladder (`docs/LADDER.md`). `docs/REPORTS.md` lists the methods and their tests; this page fixes the shapes.

`subject` means the account agents should not pay; never a victim.

## Enumerations

In JSON every enumeration value is a lowercase string (`contracts/reports/src/lib.rs:21-28`, `:42-47`, `:52-59`; the canonical list is `intel/record_enums.json`, compared with the contract by the unit test `rust_enum_values_match_the_canonical_json`).

| Name | Values |
|---|---|
| kind | `scam`, `theft`, `phishing`, `exploit`, `key_compromise`, `social` |
| outcome | `loss`, `attempt`, `near_miss`, `unknown` |
| state | `pending`, `accepted`, `rejected`, `withdrawn`, `contested`, `overturned` |

A deny kind is any kind except `social` (`contracts/reports/src/lib.rs:31-35`). The outcome is recorded and never weighed.

## The report body and `body_sha256`

`submit_report(subject, kind, outcome, evidence_sha256, evidence_url)` (`contracts/reports/src/lib.rs:323-358`) checks its arguments, then hashes a body of exactly six string fields (`contracts/reports/src/lib.rs:216-223`, `:349-358`):

| Key | Content | Constraint at submission |
|---|---|---|
| `evidence_sha256` | the sha256 of the reporter's evidence | 64 lowercase hex, else `REFUSED:bad_hash` (`:848-856`) |
| `evidence_url` | where the evidence is published | starts with `https://` and is at most 512 bytes, else `REFUSED:bad_evidence_url` (`:840-846`) |
| `kind` | a kind value | one of the kind values (refused by argument parsing otherwise) |
| `outcome` | an outcome value | one of the outcome values |
| `subject` | the reported account | a valid NEAR account id, else `REFUSED:bad_subject` (`:282-284`) |
| `submitter` | the account that called `submit_report` | the caller; not an argument |

The body is serialized as compact JSON with the keys in exactly the order of this table, which is code point order, and no key is ever null or absent. The contract writes it with `serde_json::to_vec` over `CanonicalBody`, whose fields are declared in that order (`:216-223`, `:357-358`); the bytes follow the canonical JSON conventions in `README.md`: non-ASCII text is unescaped UTF-8, and quotes, backslashes, and control characters are escaped. `body_sha256` is the sha256 of those bytes as 64 lowercase hex (`:358`). The URL is the only field that can hold characters that need escaping.

Testnet runs deployed Reports version `0.1.3`, which requires the attached deposit to equal `REPORT_DEPOSIT`, fixed at 30000000000000000000000 yoctoNEAR (0.03 NEAR); any other amount is refused as `REFUSED:bad_deposit` (`:9`, `:853-858`). The fixed amount is readable through `report_deposit() -> U128`. The deposit is checked before any argument. A submitter may hold at most 5 pending reports, else `REFUSED:pending_limit` (`:14`, `:343`). Deposits are never refunded: they stay in the contract account in every state and pay for the permanent rows. In Reports source 0.1.3, `report_and_dispute_storage_measurement_is_pinned` keeps the worst-case measurements pinned at 2,046 bytes for one maximum-size report through acceptance and 734 bytes for one maximum-size dispute, then asserts `measured * byte_cost * 120 / 100 <= REPORT_DEPOSIT <= measured * byte_cost * 2`, with `measured` as the larger byte count, and pins `REPORT_DEPOSIT` at the fixed amount. This amount was chosen to cover at least 1.2 times the measured worst-case storage, so it does not move with each storage change. Report ids start at 0 and increase by 1 (`:295-298`).

## Stored records

`get_report(id)` returns null or a report with these keys in this order (`contracts/reports/src/lib.rs:102-117`): `id` (number), `body_sha256` (string), `submitter`, `subject` (account ids), `kind`, `outcome`, `evidence_sha256`, `evidence_url`, `submitted_ms` (number, block time in milliseconds), `state`, `moderator` (account id or null), `reason_sha256` (64 lowercase hex or null), `moderated_ms` (number or null), `dispute_ids` (array of numbers). `moderator`, `reason_sha256` and `moderated_ms` are set by `moderate` and again by `resolve_dispute`.

`get_dispute(id)` returns null or `{"id", "report_id", "submitter", "evidence_sha256", "evidence_url", "submitted_ms", "open"}` in that order, `open` a boolean (`contracts/reports/src/lib.rs:152-161`). A dispute needs the same exact deposit and the same evidence checks as a report (`:591-599`); dispute ids are a separate counter starting at 0.

`publisher_stats(account)` returns `{"submitted", "accepted", "rejected", "withdrawn", "overturned"}` (`:181-187`); `totals()` returns `{"reports", "disputes"}` (`:189-193`).

The source crate version `0.1.3` is listed in `contracts/reports/Cargo.toml`; testnet runs deployed Reports version `0.1.3` per `res/BUILD-reports.md:7` and the deployment log (`fixtures/runs/reports-013-deploy-20261109.txt`). `version()` returns the source crate version (`contracts/reports/src/lib.rs:267-269`), and the deployed contract reports `0.1.3`. `state_version()` returns the stored root state version, initially 1 (`:233`, `:275-277`); the deployment readbacks retain 1 (`fixtures/runs/reports-013-deploy-20261109.txt`), and `version_views_return_the_crate_and_state_versions` checks the source crate and state versions. Reports version 0.1.3 has no lifetime cap on disputes (`docs/REPORTS.md:3`). Neither version view is a format version: the formats on this page are versioned by this page and by the events' `1.1.0`.

### State changes in source 0.1.3

The source transitions are (`contracts/reports/src/lib.rs:425`, `:561`, `:598`, `:662`):

| From | To | By | Refusal otherwise |
|---|---|---|---|
| `pending` | `accepted` or `rejected` | `moderate(id, accept, reason_sha256)`, a moderator other than the report submitter or subject, exactly 1 yoctoNEAR | `REFUSED:not_moderator` (`:425-428`, `:846-851`), `REFUSED:bad_hash` (`:990-999`), `REFUSED:self_moderation` (`:431-432`), `REFUSED:not_pending` (`:433`) |
| `pending` | `withdrawn` | `withdraw(id)`, the submitter, exactly 1 yoctoNEAR | `REFUSED:not_submitter`, `REFUSED:not_pending` |
| `accepted` | `contested` | `dispute(report_id, evidence_sha256, evidence_url)`, anyone, exactly the source deposit | `REFUSED:bad_deposit` (`:598-604`), `REFUSED:dispute_open` (`:611`), `REFUSED:not_accepted` (`:614`); there is no lifetime dispute cap |
| `contested` | `accepted` or `overturned` | `resolve_dispute(dispute_id, overturn, reason_sha256)`, a moderator other than the report subject, exactly 1 yoctoNEAR; with more than one moderator, not the original accepter stored under prefix `ab` (or `report.moderator` if that entry is absent and `upheld_disputes == 0`), the report submitter, or the dispute submitter | `REFUSED:not_moderator` (`:662-665`, `:846-851`), `REFUSED:bad_hash` (`:990-999`), `REFUSED:dispute_closed` (`:668`), `REFUSED:not_contested` (`:672-674`), `REFUSED:self_moderation` (`:670`, `:692-696`), `REFUSED:same_moderator` (`:686-709`) |

A missing id is refused as `REFUSED:report_missing` or `REFUSED:dispute_missing`. Rows are never deleted. An upheld dispute returns the report to `accepted` and increments its upheld-dispute count, saturating at 255; `get_report` does not show that count (`:75`, `:614`).

`moderate` refuses a moderator who is either the report submitter or report subject (`:431-432`). `resolve_dispute` always refuses the report subject as moderator (`:670`). With multiple moderators, it checks the original accepter stored under prefix `ab`; it falls back to `report.moderator` only when the entry is absent and `upheld_disputes == 0`, then writes that fallback to `ab` before changing `report.moderator` (`:675-686`, `:711-716`), and refuses the report submitter and dispute submitter (`:692-696`). A report can still lack an `ab` entry after an upheld dispute only when it already had at least one upheld dispute under 0.1.1 before the 7 Nov redeploy; with multiple moderators, such a legacy report fails closed with `REFUSED:same_moderator`. Recovery is through the owner's `set_moderators` method with a one-moderator list (`docs/REPORTS.md:36`). The testnet demo now records two reports and two disputes after the 9 Nov threshold run, with an all-zero Mallory summary after mod3's overturn call (`fixtures/runs/reports-threshold-demo-20261109.txt`). Before the 7 Nov redeploy, testnet Reports 0.1.1 refused a moderator only for moderating their own report and checked the current `report.moderator` for multi-moderator resolution; it had no report-subject or submitter refusals on resolution (`docs/REPORTS.md:3`).

### Moderators

The owner sets the moderator list with `new(owner_id, moderators)` and `set_moderators(moderators)`; `set_moderators` takes exactly 1 yoctoNEAR and refuses any caller but the owner as `REFUSED:not_owner` (`:228-246`, `:261-270`, `:701-706`). Both refuse a list that names the same account twice as `REFUSED:duplicate_moderator` (`:229`, `:264`, `:807-812`), so the one-moderator exception of `resolve_dispute` counts distinct accounts. A contract can enforce distinct accounts, not independent people (`docs/DECISIONS.md`, entry 6).

### Approvals in source 0.1.3

`approvals_required()` returns the threshold as a `u32`; the new threshold key is absent by default, which reads as 1. `set_approvals_required(n)` is owner-only, requires exactly 1 yoctoNEAR, accepts values from 1 through the current moderator count, and emits `approvals_required_updated` with `previous` and `current`. When there is more than one moderator, the threshold must be strictly less than the list length; equality is refused as `REFUSED:bad_threshold`. It never transitions a report. `new` and `set_moderators` refuse more than 7 moderators as `REFUSED:too_many_moderators`. `set_moderators` refuses a list shorter than the threshold as `REFUSED:threshold_above_moderators` and refuses a list equal to a threshold above 1 as `REFUSED:bad_threshold`, so the owner lowers the threshold before shrinking the list. This keeps a current moderator outside the votes needed to accept a threshold-based decision, available to resolve a dispute under the existing resolver rules.

With threshold 1, `moderate` keeps the existing event payloads, emits no `report_vote`, and writes no vote entry. The event envelope version is `1.1.0` for all events; threshold-1 payloads are unchanged. With a higher threshold, it stores one vote per moderator and report, replacing that moderator's earlier vote on a repeat call. Each entry has `moderator`, `side` (`accept` or `reject`) and `reason_sha256`. A tally runs only during `moderate` and counts only voters on the current list; a removed moderator's vote stops counting and counts again if re-added. `report_votes(id)` returns the stored vote entries. When one side reaches the threshold, the deciding moderator and reason hash are written on the report, followed by the existing `report_moderated` event. Every threshold vote emits `report_vote` with `report_id`, `moderator`, `side`, `reason_sha256`, `accept_votes`, `reject_votes` and `approvals_required`. Vote storage is paid from the contract balance, not the report deposit.

Testnet runs deployed Reports version `0.1.3`. The 9 Nov demo shows the owner's first vote left report 1 pending, mod2's second vote reached the two-approval threshold and changed the summary to accepted 1 with `deny` true, and mod3's overturn call returned the summary to zero counts with `deny` false. The demo ended with totals of 2 reports and 2 disputes. The deployment readbacks show the existing report 0 and dispute 0 unchanged, totals unchanged at 1 each across the upgrade, and `state_version()` unchanged at 1 (`fixtures/runs/reports-013-deploy-20261109.txt`, `fixtures/runs/reports-threshold-demo-20261109.txt`). The three moderator accounts used are controlled by the ShieldBot team; the demo proves the contract rule, not independent review.

With more than one moderator, `resolve_dispute` also refuses any resolver with a recorded accept vote on the report as `REFUSED:same_moderator`, in addition to refusing the accepting moderator stored under `ab`. The strict threshold rule leaves a current moderator outside the votes needed to accept a threshold-based decision, available to resolve its dispute under the existing resolver rules. With one moderator, the existing behavior still permits that moderator to resolve a dispute on a report they accepted, unless they are also the report subject. The owner can set the threshold to 1 and the list to one account; the threshold defends against a moderator key that is not the owner's.

## Events

Every event is one log line: `EVENT_JSON:` followed by compact JSON with the keys in the order `standard`, `version`, `event`, `data`; `standard` is `shieldbot-near`, `version` is `1.1.0`, `data` a one element array (`contracts/reports/src/events.rs`). Keys are written in the order listed here, not sorted: the contract's `serde_json` keeps insertion order (`contracts/reports/Cargo.lock` pulls `indexmap` into `serde_json`). `state` is a state value.

| Event | `data[0]` keys, in order | Emitted at |
|---|---|---|
| `moderators_updated` | `moderators` (array of account ids) | `:315-319` |
| `report_submitted` | `report_id`, `subject`, `state`, `body_sha256` | `:411-420` |
| `report_moderated` | `report_id`, `subject`, `state`, `body_sha256`, `moderator`, `reason_sha256` | `:546-560` |
| `report_withdrawn` | `report_id`, `subject`, `state`, `body_sha256` | `:585-594` |
| `report_disputed` | `report_id`, `dispute_id`, `subject`, `state`, `body_sha256` | `:647-656` |
| `dispute_resolved` | `report_id`, `dispute_id`, `subject`, `state`, `body_sha256`, `moderator`, `reason_sha256` | `:751-760` |
| `approvals_required_updated` | `previous`, `current` | `:296-299` |
| `report_vote` | `report_id`, `moderator`, `side`, `reason_sha256`, `accept_votes`, `reject_votes`, `approvals_required` | `:465-474` |

## The `subject_summary` view

`subject_summary(subject)` (`contracts/reports/src/lib.rs:808`, shape at `:188-196`) returns exactly these six keys in this order:

| Key | JSON type | Meaning |
|---|---|---|
| `accepted` | number (u64) | reports on the subject in state `accepted` or `contested` |
| `pending` | number (u64) | the count of reports in state `pending`; pending is a count only and has no verdict effect |
| `contested` | number (u64) | reports in state `contested` |
| `latest_accepted_id` | number or null | the id of the most recently accepted report still `accepted` or `contested`, of any kind |
| `latest_deny_id` | number or null | the id of the most recently accepted report of a deny kind still `accepted` or `contested` |
| `deny` | boolean | true while at least one report of a deny kind is `accepted` or `contested` |

"Most recently accepted" is the order of acceptance, kept by linked lists over the reports (`append_accepted`, `append_deny`, `remove_accepted` and `remove_deny`, `contracts/reports/src/lib.rs:888`, `:899`, `:910`, `:927`); overturning a report unlinks it, and when it was the latest the previous remaining one becomes the latest. A dispute leaves a report counted in `accepted` and in the lists; only an overturn removes it. An unreported subject reads `{"accepted":0,"pending":0,"contested":0,"latest_accepted_id":null,"latest_deny_id":null,"deny":false}`.
Invariants the contract keeps, and a reader must check (a summary that breaks one is unreadable, not a pass):

1. `contested` is at most `accepted`.
2. `latest_accepted_id` is null exactly when `accepted` is 0.
3. `latest_deny_id` is null exactly when `deny` is false.
4. `deny` true implies `accepted` above 0.
5. The object has exactly the six keys; the counts are non negative integers and `deny` is a boolean.

The view's current JSON shape is the six fields listed above; any new fields would require changing the contract method.

## The consumer rule

How a summary changes a payment verdict is fixed in one place, `docs/LADDER.md`: deny beats ask, ask beats none, and an unreadable summary is its own outcome that never passes. A consumer that maps a summary to an outcome by any other rule does not conform to this version. The Python reader is implemented in `intel/reports.py`, `gate/rules.py` applies its effect when `REPORTS_ACCOUNT_ID` is set, and `tests/test_reports_rules.py` covers the ladder.

## Worked example

The body vector `fixtures/records/body-vector.json` has `kind` `scam`, `outcome` `loss`, `subject` `vendor.testnet`, `submitter` `reporter.testnet`, `evidence_sha256` the letter `a` 64 times and `evidence_url` `https://evidence.example/naïve` (with U+00EF). Its body bytes, with the hash shortened here:

```
{"evidence_sha256":"aaaaaaaa...","evidence_url":"https://evidence.example/naïve","kind":"scam","outcome":"loss","subject":"vendor.testnet","submitter":"reporter.testnet"}
```

In UTF-8, `ï` is encoded as the two bytes `c3 af`. The sha256 of the bytes is `e1b323c2...`, the vector's `expected_sha256`.

## Vectors

| Path | What it pins | Reproduced by |
|---|---|---|
| `fixtures/records/body-vector.json` | the body bytes and `body_sha256` for one report, with a non ASCII URL; the Rust unit test `canonical_body_hash_matches_shared_vector` reads the same file | `gate.evidence.evidence_hash` (the canonical JSON function; the repository has no separate Python function for the report body) |
| `intel/record_enums.json` | the enumeration values (not under `fixtures/`) | the unit test `rust_enum_values_match_the_canonical_json` |

Python reproduces this vector with `gate.evidence.evidence_hash`; Rust's `canonical_body_hash_matches_shared_vector` test in `contracts/reports/src/lib.rs` serializes the same input with `CanonicalBody`, checks the bytes against the expected six-field JSON bytes, and verifies their hash against the vector.

No fixture pins the events or the summary. They are pinned by the contract's unit tests in `contracts/reports/src/lib.rs`: `emits_all_five_nep_297_events_with_report_identity` parses the report and dispute events from logs and checks their report identity, including `moderator` and `reason_sha256` against the calls for `report_moderated` and `dispute_resolved`, with a resolver different from the accepter; `owner_can_set_moderators` checks `moderators_updated`; `initializes_empty_counters_and_views`, `accepted_report_updates_summary_and_publisher_stats`, `dispute_contests_accepted_report_and_updates_summary`, `moderator_can_overturn_dispute`, `summary_deny_stays_true_when_one_of_two_deny_reports_is_overturned` and `overturning_middle_deny_report_relinks_deny_chain_without_changing_latest` check the summary; `report_and_dispute_storage_measurement_is_pinned`, `report_requires_the_pinned_deposit` and `report_rejects_a_deposit_above_the_exact_amount` check the deposit; `report_submitter_cannot_moderate_own_report`, `report_subject_cannot_moderate_own_report`, `accepting_moderator_cannot_resolve_with_multiple_moderators`, `missing_accepting_moderator_uses_report_moderator_fallback_without_upheld_disputes`, `legacy_report_fallback_is_migrated_before_moderator_changes`, `legacy_upheld_report_without_accepting_moderator_refuses_every_resolver`, `report_submitter_can_resolve_when_the_moderator_list_has_one_member`, `dispute_submitter_can_resolve_when_the_moderator_list_has_one_member`, `report_subject_cannot_resolve_own_dispute_with_one_moderator`, `report_subject_cannot_resolve_own_dispute_with_multiple_moderators`, `report_submitter_cannot_resolve_own_dispute`, `dispute_submitter_cannot_resolve_own_dispute`, `accepting_moderator_uses_registered_ab_storage_key`, `duplicate_moderators_are_refused_by_new`, `duplicate_moderators_are_refused_by_set_moderators`, `upheld_disputes_never_cap_further_disputes` and `upheld_counter_saturates_instead_of_panicking` cover moderator and resolver rules; `version_views_return_the_crate_and_state_versions` and `report_deposit_view_returns_fixed_amount` cover the views.

## Source of truth

- `contracts/reports/src/lib.rs:216-223` the body; `:323-358` `submit_report`; `:808-817` `subject_summary`; `:846-851` the moderator membership check; `:853-858` the deposit check; `:982-989`, `:990-999` the evidence checks; `:425-434`, `:662-710`, `:839-845`, `:846-851` and `:888-938` the moderator and resolver checks; `:267-269`, `:275-277` the version views; `contracts/reports/src/events.rs` the event envelope.
- `docs/LADDER.md` the consumer rule.
