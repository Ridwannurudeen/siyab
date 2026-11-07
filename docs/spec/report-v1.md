# Community report

Version: 1 (events `shieldbot-near` `1.0.0`)

## Purpose

The reports contract (`contracts/reports`) stores community reports about NEAR accounts, moderation of those reports, and disputes against accepted ones. Each report's body is hashed when it is submitted, so a reader can check that the stored report is the one submitted. The `subject_summary` view condenses a subject's reports into six fields, and the verdict service reads only that view, through one rule, the ladder (`docs/LADDER.md`). `docs/REPORTS.md` lists the methods and their tests; this page fixes the shapes.

`subject` means the account agents should not pay; never a victim.

## Enumerations

In JSON every enumeration value is a lowercase string (`contracts/reports/src/lib.rs:17`, `:38`, `:48`; the canonical list is `intel/record_enums.json`, compared with the contract by the unit test `rust_enum_values_match_the_canonical_json`).

| Name | Values |
|---|---|
| kind | `scam`, `theft`, `phishing`, `exploit`, `key_compromise`, `social` |
| outcome | `loss`, `attempt`, `near_miss`, `unknown` |
| state | `pending`, `accepted`, `rejected`, `withdrawn`, `contested`, `overturned` |

A deny kind is any kind except `social` (`contracts/reports/src/lib.rs:27`). The outcome is recorded and never weighed.

## The report body and `body_sha256`

`submit_report(subject, kind, outcome, evidence_sha256, evidence_url)` (`contracts/reports/src/lib.rs:273-280`) checks its arguments, then hashes a body of exactly six string fields (`contracts/reports/src/lib.rs:195-204`, `:299-308`):

| Key | Content | Constraint at submission |
|---|---|---|
| `evidence_sha256` | the sha256 of the reporter's evidence | 64 lowercase hex, else `REFUSED:bad_hash` (`:848-856`) |
| `evidence_url` | where the evidence is published | starts with `https://` and is at most 512 bytes, else `REFUSED:bad_evidence_url` (`:840-846`) |
| `kind` | a kind value | one of the kind values (refused by argument parsing otherwise) |
| `outcome` | an outcome value | one of the outcome values |
| `subject` | the reported account | a valid NEAR account id, else `REFUSED:bad_subject` (`:282-284`) |
| `submitter` | the account that called `submit_report` | the caller; not an argument |

The body is serialized as compact JSON with the keys in exactly the order of this table, which is code point order, and no key is ever null or absent. The contract writes it with `serde_json::to_vec` over a struct whose fields are declared in that order (`:307`); the bytes follow the canonical JSON conventions in `README.md`: non-ASCII text is unescaped UTF-8, and quotes, backslashes, and control characters are escaped. `body_sha256` is the sha256 of those bytes as 64 lowercase hex (`:308`). The URL is the only field that can hold characters that need escaping.

The source contract version 0.1.2 requires the attached deposit to equal `REPORT_DEPOSIT`, fixed at 30000000000000000000000 yoctoNEAR (0.03 NEAR); any other amount is refused as `REFUSED:bad_deposit` (`:9`, `:715-720`). This source amount changes deployed Reports 0.1.1's 23136000000000000000000 yoctoNEAR (0.023136 NEAR; release behavior is recorded in `docs/REPORTS.md:3`). The fixed amount is readable through `report_deposit() -> U128`. The deposit is checked before any argument. A submitter may hold at most 5 pending reports, else `REFUSED:pending_limit` (`:11`, `:293`). Deposits are never refunded: they stay in the contract account in every state and pay for the permanent rows. For source 0.1.2, `report_and_dispute_storage_measurement_is_pinned` keeps the worst-case measurements pinned at 2,046 bytes for one maximum-size report through acceptance and 734 bytes for one maximum-size dispute, then asserts `measured * byte_cost * 120 / 100 <= REPORT_DEPOSIT <= measured * byte_cost * 2`, with `measured` as the larger byte count, and pins `REPORT_DEPOSIT` at the fixed source amount. This amount was chosen to cover at least 1.2 times the measured worst-case storage, so it does not move with each storage change. Report ids start at 0 and increase by 1 (`:295-298`).

## Stored records

`get_report(id)` returns null or a report with these keys in this order (`contracts/reports/src/lib.rs:83-98`): `id` (number), `body_sha256` (string), `submitter`, `subject` (account ids), `kind`, `outcome`, `evidence_sha256`, `evidence_url`, `submitted_ms` (number, block time in milliseconds), `state`, `moderator` (account id or null), `reason_sha256` (64 lowercase hex or null), `moderated_ms` (number or null), `dispute_ids` (array of numbers). `moderator`, `reason_sha256` and `moderated_ms` are set by `moderate` and again by `resolve_dispute`.

`get_dispute(id)` returns null or `{"id", "report_id", "submitter", "evidence_sha256", "evidence_url", "submitted_ms", "open"}` in that order, `open` a boolean (`contracts/reports/src/lib.rs:134-142`). A dispute needs the same exact deposit and the same evidence checks as a report (`:482-489`); dispute ids are a separate counter starting at 0.

`publisher_stats(account)` returns `{"submitted", "accepted", "rejected", "withdrawn", "overturned"}` (`:181-187`); `totals()` returns `{"reports", "disputes"}` (`:189-193`).

The source crate version `0.1.2` is listed in `contracts/reports/Cargo.toml`; testnet runs deployed Reports version `0.1.1` per `res/BUILD-reports.md:7`. `version()` returns the source crate version (`contracts/reports/src/lib.rs:248-250`), while the deployed contract reports `0.1.1`. `state_version()` returns the stored root state version, initially 1 (`:233`, `:256-258`); `version_views_return_the_crate_and_state_versions` checks the source crate and state versions. Source 0.1.2 and deployed 0.1.1 both have no lifetime cap on disputes (`docs/REPORTS.md:3`). Neither version view is a format version: the formats on this page are versioned by this page and by the events' `1.0.0`.

### State changes in source 0.1.2

The source transitions are (`contracts/reports/src/lib.rs:375`, `:445`, `:482`, `:546`):

| From | To | By | Refusal otherwise |
|---|---|---|---|
| `pending` | `accepted` or `rejected` | `moderate(id, accept, reason_sha256)`, a moderator other than the report submitter or subject, exactly 1 yoctoNEAR | `REFUSED:not_moderator` (`:708-713`), `REFUSED:bad_hash` (`:848-856`), `REFUSED:self_moderation`, `REFUSED:not_pending` (`:381-383`) |
| `pending` | `withdrawn` | `withdraw(id)`, the submitter, exactly 1 yoctoNEAR | `REFUSED:not_submitter`, `REFUSED:not_pending` |
| `accepted` | `contested` | `dispute(report_id, evidence_sha256, evidence_url)`, anyone, exactly the source deposit | `REFUSED:bad_deposit` (`:488-489`), `REFUSED:dispute_open` (`:495`), `REFUSED:not_accepted` (`:496-499`); there is no lifetime dispute cap |
| `contested` | `accepted` or `overturned` | `resolve_dispute(dispute_id, overturn, reason_sha256)`, a moderator other than the report subject, exactly 1 yoctoNEAR; with more than one moderator, not the original accepter stored under prefix `ab` (or `report.moderator` if that entry is absent and `upheld_disputes == 0`), the report submitter, or the dispute submitter | `REFUSED:not_moderator` (`:708-713`), `REFUSED:bad_hash` (`:848-856`), `REFUSED:dispute_closed` (`:552`), `REFUSED:not_contested` (`:555-558`), `REFUSED:self_moderation` (`:554`, `:576-580`), `REFUSED:same_moderator` (`:570-575`, `:581-584`) |

A missing id is refused as `REFUSED:report_missing` or `REFUSED:dispute_missing`. Rows are never deleted. An upheld dispute returns the report to `accepted` and increments its upheld-dispute count, saturating at 255; `get_report` does not show that count (`:75`, `:614`).

`moderate` refuses a moderator who is either the report submitter or report subject (`:381-382`). `resolve_dispute` always refuses the report subject as moderator (`:554`). With multiple moderators, it checks the original accepter stored under prefix `ab`; it falls back to `report.moderator` only when the entry is absent and `upheld_disputes == 0`, then writes that fallback to `ab` before changing `report.moderator` (`:559-569`, `:586-591`), and refuses the report submitter and dispute submitter (`:576-580`). A report can still lack an `ab` entry after an upheld dispute only when it already had at least one upheld dispute under 0.1.1 itself, before redeployment; with multiple moderators, such a legacy report fails closed with `REFUSED:same_moderator`. Recovery is through the owner's `set_moderators` method with a one-moderator list (`docs/REPORTS.md:36`). The testnet Reports contract holds zero reports. Testnet's deployed 0.1.1 instead refuses a moderator only for moderating their own report and checks the current `report.moderator` for multi-moderator resolution; it has no report-subject or submitter refusals on resolution (`docs/REPORTS.md:3`).

### Moderators

The owner sets the moderator list with `new(owner_id, moderators)` and `set_moderators(moderators)`; `set_moderators` takes exactly 1 yoctoNEAR and refuses any caller but the owner as `REFUSED:not_owner` (`:228-246`, `:261-270`, `:701-706`). Both refuse a list that names the same account twice as `REFUSED:duplicate_moderator` (`:229`, `:264`, `:807-812`), so the one-moderator exception of `resolve_dispute` counts distinct accounts. A contract can enforce distinct accounts, not independent people (`docs/DECISIONS.md`, entry 6).

## Events

Every event is one log line: `EVENT_JSON:` followed by compact JSON with the keys in the order `standard`, `version`, `event`, `data`; `standard` is `shieldbot-near`, `version` is `1.0.0`, `data` a one element array (`contracts/reports/src/events.rs`). Keys are written in the order listed here, not sorted: the contract's `serde_json` keeps insertion order (`contracts/reports/Cargo.lock` pulls `indexmap` into `serde_json`). `state` is a state value.

| Event | `data[0]` keys, in order | Emitted at |
|---|---|---|
| `moderators_updated` | `moderators` (array of account ids) | `:267` |
| `report_submitted` | `report_id`, `subject`, `state`, `body_sha256` | `:363-370` |
| `report_moderated` | `report_id`, `subject`, `state`, `body_sha256`, `moderator`, `reason_sha256` | `:432-441` |
| `report_withdrawn` | `report_id`, `subject`, `state`, `body_sha256` | `:471-479` |
| `report_disputed` | `report_id`, `dispute_id`, `subject`, `state`, `body_sha256` | `:533-540` |
| `dispute_resolved` | `report_id`, `dispute_id`, `subject`, `state`, `body_sha256`, `moderator`, `reason_sha256` | `:628-637` |

## The `subject_summary` view

`subject_summary(subject)` (`contracts/reports/src/lib.rs:676`, shape at `:170-177`) returns exactly these six keys in this order:

| Key | JSON type | Meaning |
|---|---|---|
| `accepted` | number (u64) | reports on the subject in state `accepted` or `contested` |
| `pending` | number (u64) | the count of reports in state `pending`; pending is a count only and has no verdict effect |
| `contested` | number (u64) | reports in state `contested` |
| `latest_accepted_id` | number or null | the id of the most recently accepted report still `accepted` or `contested`, of any kind |
| `latest_deny_id` | number or null | the id of the most recently accepted report of a deny kind still `accepted` or `contested` |
| `deny` | boolean | true while at least one report of a deny kind is `accepted` or `contested` |

"Most recently accepted" is the order of acceptance, kept by linked lists over the reports (`append_accepted`, `append_deny`, `remove_accepted` and `remove_deny`, `contracts/reports/src/lib.rs:750`, `:761`, `:772`, `:789`); overturning a report unlinks it, and when it was the latest the previous remaining one becomes the latest. A dispute leaves a report counted in `accepted` and in the lists; only an overturn removes it. An unreported subject reads `{"accepted":0,"pending":0,"contested":0,"latest_accepted_id":null,"latest_deny_id":null,"deny":false}`.
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

No fixture pins the events or the summary. They are pinned by the contract's unit tests in `contracts/reports/src/lib.rs`: `emits_all_five_nep_297_events_with_report_identity` parses the report and dispute events from logs and checks their report identity, including `moderator` and `reason_sha256` against the calls for `report_moderated` and `dispute_resolved`, with a resolver different from the accepter; `owner_can_set_moderators` checks `moderators_updated`; `initializes_empty_counters_and_views`, `accepted_report_updates_summary_and_publisher_stats`, `dispute_contests_accepted_report_and_updates_summary`, `moderator_can_overturn_dispute`, `summary_deny_stays_true_when_one_of_two_deny_reports_is_overturned` and `overturning_middle_deny_report_relinks_deny_chain_without_changing_latest` check the summary; `report_and_dispute_storage_measurement_is_pinned`, `report_requires_the_pinned_deposit` and `report_rejects_a_deposit_above_the_exact_amount` check the deposit; `report_submitter_cannot_moderate_own_report`, `report_subject_cannot_moderate_own_report`, `accepting_moderator_cannot_resolve_with_multiple_moderators`, `missing_accepting_moderator_uses_report_moderator_fallback_without_upheld_disputes`, `legacy_report_fallback_is_migrated_before_moderator_changes`, `legacy_upheld_report_without_accepting_moderator_refuses_every_resolver`, `report_submitter_can_resolve_when_the_moderator_list_has_one_member`, `dispute_submitter_can_resolve_when_the_moderator_list_has_one_member`, `report_subject_cannot_resolve_own_dispute_with_one_moderator`, `report_subject_cannot_resolve_own_dispute_with_multiple_moderators`, `report_submitter_cannot_resolve_own_dispute`, `dispute_submitter_cannot_resolve_own_dispute`, `accepting_moderator_uses_registered_ab_storage_key`, `duplicate_moderators_are_refused_by_new`, `duplicate_moderators_are_refused_by_set_moderators`, `upheld_disputes_never_cap_further_disputes` and `upheld_counter_saturates_instead_of_panicking` cover governance; `version_views_return_the_crate_and_state_versions` and `report_deposit_view_returns_fixed_amount` cover the views.

## Source of truth

- `contracts/reports/src/lib.rs:195-204` the body; `:273-280` `submit_report`; `:676-686` `subject_summary`; `:708-713` the moderator membership check; `:715-720` the deposit check; `:840-857` the evidence checks; `:375-383`, `:546-591`, `:701-713` and `:807-812` the governance checks; `:248-258` the version views; `contracts/reports/src/events.rs` the event envelope.
- `docs/LADDER.md` the consumer rule.
