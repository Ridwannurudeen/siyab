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

`submit_report(subject, kind, outcome, evidence_sha256, evidence_url)` (`contracts/reports/src/lib.rs:268`) checks its arguments, then hashes a body of exactly six string fields (`contracts/reports/src/lib.rs:196`, `:294`):

| Key | Content | Constraint at submission |
|---|---|---|
| `evidence_sha256` | the sha256 of the reporter's evidence | 64 lowercase hex, else `REFUSED:bad_hash` (`:817`) |
| `evidence_url` | where the evidence is published | starts with `https://` and is at most 512 bytes, else `REFUSED:bad_evidence_url` (`:811`) |
| `kind` | a kind value | one of the kind values (refused by argument parsing otherwise) |
| `outcome` | an outcome value | one of the outcome values |
| `subject` | the reported account | a valid NEAR account id, else `REFUSED:bad_subject` (`:279`) |
| `submitter` | the account that called `submit_report` | the caller; not an argument |

The body is serialized as compact JSON with the keys in exactly the order of this table, which is code point order, and no key is ever null or absent. The contract writes it with `serde_json::to_vec` over a struct whose fields are declared in that order (`:302`); the bytes are the canonical JSON of `README.md`, shared conventions, applied to this six key object: non ASCII characters unescaped in UTF-8, `"`, `\` and control characters escaped as listed there. `body_sha256` is the sha256 of those bytes as 64 lowercase hex (`:303`). The URL is the only field that can hold characters that need escaping.

The other submission rules: the attached deposit must be exactly `REPORT_DEPOSIT`, 23136000000000000000000 yoctoNEAR (0.023136 NEAR); any other amount, more or less, is refused as `REFUSED:bad_deposit` (`:8`, `:684`). The deposit is checked before any argument. A submitter may hold at most 5 pending reports, else `REFUSED:pending_limit` (`:9`, `:288`). Deposits are never refunded: they stay in the contract account in every state and pay for the permanent rows. The amount is the larger of the measured storage of one maximum size report through acceptance (1,928 bytes) and one maximum size dispute (734 bytes), times the storage byte cost, with a 20 percent margin; the unit test `report_and_dispute_storage_measurement_is_pinned` pins both measurements and the amount. Report ids start at 0 and increase by 1 (`:290`).

## Stored records

`get_report(id)` returns null or a report with these keys in this order (`contracts/reports/src/lib.rs:82`): `id` (number), `body_sha256` (string), `submitter`, `subject` (account ids), `kind`, `outcome`, `evidence_sha256`, `evidence_url`, `submitted_ms` (number, block time in milliseconds), `state`, `moderator` (account id or null), `reason_sha256` (64 lowercase hex or null), `moderated_ms` (number or null), `dispute_ids` (array of numbers). `moderator`, `reason_sha256` and `moderated_ms` are set by `moderate` and again by `resolve_dispute`.

`get_dispute(id)` returns null or `{"id", "report_id", "submitter", "evidence_sha256", "evidence_url", "submitted_ms", "open"}` in that order, `open` a boolean (`contracts/reports/src/lib.rs:133`). A dispute needs the same exact deposit and the same evidence checks as a report (`:480`, `:481`); dispute ids are a separate counter starting at 0.

`publisher_stats(account)` returns `{"submitted", "accepted", "rejected", "withdrawn", "overturned"}` (`:180`); `totals()` returns `{"reports", "disputes"}` (`:189`).

`version()` returns the crate version string, `0.1.0` in `contracts/reports/Cargo.toml` (`:247`), and `state_version()` returns the stored root state version as a number, 1 since `new` (`:232`, `:251`); the unit test `version_views_return_the_crate_and_state_versions` checks both. Neither is a format version: the formats on this page are versioned by this page and by the events' `1.0.0`.

### State changes

The only transitions (`contracts/reports/src/lib.rs:370`, `:437`, `:474`, `:542`):

| From | To | By | Refusal otherwise |
|---|---|---|---|
| `pending` | `accepted` or `rejected` | `moderate(id, accept, reason_sha256)`, a moderator other than the report's submitter, exactly 1 yoctoNEAR | `REFUSED:not_moderator`, `REFUSED:bad_hash`, `REFUSED:self_moderation` (`:376`), `REFUSED:not_pending` |
| `pending` | `withdrawn` | `withdraw(id)`, the submitter, exactly 1 yoctoNEAR | `REFUSED:not_submitter`, `REFUSED:not_pending` |
| `accepted` | `contested` | `dispute(report_id, evidence_sha256, evidence_url)`, anyone, exactly the deposit | `REFUSED:bad_deposit`, `REFUSED:dispute_open`, `REFUSED:not_accepted`, `REFUSED:dispute_limit` (`:492`: a report takes at most 5 upheld disputes over its lifetime, that is disputes resolved against the disputer; open and overturned disputes do not count) |
| `contested` | `accepted` or `overturned` | `resolve_dispute(dispute_id, overturn, reason_sha256)`, a moderator, exactly 1 yoctoNEAR; while the moderator list has more than one member, not the moderator recorded on the report | `REFUSED:not_moderator`, `REFUSED:bad_hash`, `REFUSED:dispute_closed`, `REFUSED:not_contested`, `REFUSED:same_moderator` (`:554`) |

A missing id is refused as `REFUSED:report_missing` or `REFUSED:dispute_missing`. Rows are never deleted. An upheld dispute returns the report to `accepted` and adds 1 to its count of upheld disputes, which `get_report` does not show (`:74`, `:580`).

### Moderators

The owner sets the moderator list with `new(owner_id, moderators)` and `set_moderators(moderators)`, which takes exactly 1 yoctoNEAR and refuses any caller but the owner as `REFUSED:not_owner` (`:256`, `:670`). Both refuse a list that names the same account twice as `REFUSED:duplicate_moderator` (`:228`, `:259`, `:776`), so the one moderator exception of `resolve_dispute` counts distinct accounts. A contract can enforce distinct accounts, not independent people (`docs/DECISIONS.md`, entry 6).

## Events

Every event is one log line: `EVENT_JSON:` followed by compact JSON with the keys in the order `standard`, `version`, `event`, `data`; `standard` is `shieldbot-near`, `version` is `1.0.0`, `data` a one element array (`contracts/reports/src/events.rs`). Keys are written in the order listed here, not sorted: the contract's `serde_json` keeps insertion order (its `Cargo.lock` pulls `indexmap` into `serde_json`). `state` is a state value.

| Event | `data[0]` keys, in order | Emitted at |
|---|---|---|
| `moderators_updated` | `moderators` (array of account ids) | `:261` |
| `report_submitted` | `report_id`, `subject`, `state`, `body_sha256` | `:357` |
| `report_moderated` | `report_id`, `subject`, `state`, `body_sha256`, `moderator`, `reason_sha256` | `:423` |
| `report_withdrawn` | `report_id`, `subject`, `state`, `body_sha256` | `:462` |
| `report_disputed` | `report_id`, `dispute_id`, `subject`, `state`, `body_sha256` | `:528` |
| `dispute_resolved` | `report_id`, `dispute_id`, `subject`, `state`, `body_sha256`, `moderator`, `reason_sha256` | `:596` |

## The `subject_summary` view

`subject_summary(subject)` (`contracts/reports/src/lib.rs:645`, shape at `:169`) returns exactly these six keys in this order:

| Key | JSON type | Meaning |
|---|---|---|
| `accepted` | number (u64) | reports on the subject in state `accepted` or `contested` |
| `pending` | number (u64) | reports in state `pending` |
| `contested` | number (u64) | reports in state `contested` |
| `latest_accepted_id` | number or null | the id of the most recently accepted report still `accepted` or `contested`, of any kind |
| `latest_deny_id` | number or null | the id of the most recently accepted report of a deny kind still `accepted` or `contested` |
| `deny` | boolean | true while at least one report of a deny kind is `accepted` or `contested` |

"Most recently accepted" is the order of acceptance, kept by linked lists over the reports (`append_accepted`, `append_deny`, `remove_accepted` and `remove_deny`, `contracts/reports/src/lib.rs:719`, `:730`, `:741`, `:758`); overturning a report unlinks it, and when it was the latest the previous remaining one becomes the latest. A dispute leaves a report counted in `accepted` and in the lists; only an overturn removes it. An unreported subject reads `{"accepted":0,"pending":0,"contested":0,"latest_accepted_id":null,"latest_deny_id":null,"deny":false}`.

Invariants the contract keeps, and a reader must check (a summary that breaks one is unreadable, not a pass):

1. `contested` is at most `accepted`.
2. `latest_accepted_id` is null exactly when `accepted` is 0.
3. `latest_deny_id` is null exactly when `deny` is false.
4. `deny` true implies `accepted` above 0.
5. The object has exactly the six keys; the counts are non negative integers and `deny` is a boolean.

The view never changes shape: a richer view is a new method with a new name (`docs/DECISIONS.md`, entry 1).

## The consumer rule

How a summary changes a payment verdict is fixed in one place, `docs/LADDER.md`: deny beats ask, ask beats none, and an unreadable summary is its own outcome that never passes. A consumer that maps a summary to an outcome by any other rule does not conform to this version. The Python reader that page names (`intel/reports.py`) is not on main at the commit this page was written against; the ladder table in `docs/LADDER.md` is the rule until it lands.

## Worked example

The body vector `fixtures/records/body-vector.json` has `kind` `scam`, `outcome` `loss`, `subject` `vendor.testnet`, `submitter` `reporter.testnet`, `evidence_sha256` the letter `a` 64 times and `evidence_url` `https://evidence.example/naïve` (with U+00EF). Its body bytes, with the hash shortened here:

```
{"evidence_sha256":"aaaaaaaa...","evidence_url":"https://evidence.example/naïve","kind":"scam","outcome":"loss","subject":"vendor.testnet","submitter":"reporter.testnet"}
```

The `ï` is the two UTF-8 bytes `c3 af`, not `ï`. The sha256 of the bytes is `e1b323c2...`, the vector's `expected_sha256`.

## Vectors

| Path | What it pins | Reproduced by |
|---|---|---|
| `fixtures/records/body-vector.json` | the body bytes and `body_sha256` for one report, with a non ASCII URL; the Rust unit test `canonical_body_hash_matches_shared_vector` reads the same file | `gate.evidence.evidence_hash` (the canonical JSON function; the repository has no separate Python function for the report body) |
| `intel/record_enums.json` | the enumeration values (not under `fixtures/`) | the unit test `rust_enum_values_match_the_canonical_json` |

Python reproduces this vector with `gate.evidence.evidence_hash`; Rust's `canonical_body_hash_matches_shared_vector` test in `contracts/reports/src/lib.rs` serializes the same input with `CanonicalBody`, checks the bytes against the expected six-field JSON bytes, and verifies their hash against the vector.

No fixture pins the events or the summary. They are pinned by the contract's unit tests in `contracts/reports/src/lib.rs`: `emits_all_five_nep_297_events_with_report_identity` (the five report and dispute events: `report_id`, `subject`, `state`, `body_sha256` and `dispute_id`; it does not assert `moderator` or `reason_sha256`) and `owner_can_set_moderators` (`moderators_updated`), `initializes_empty_counters_and_views`, `accepted_report_updates_summary_and_publisher_stats`, `dispute_contests_accepted_report_and_updates_summary`, `moderator_can_overturn_dispute`, `summary_deny_stays_true_when_one_of_two_deny_reports_is_overturned` and `overturning_middle_deny_report_relinks_deny_chain_without_changing_latest` (summary), `report_and_dispute_storage_measurement_is_pinned`, `report_requires_the_pinned_deposit` and `report_rejects_a_deposit_above_the_exact_amount` (the deposit), `report_submitter_cannot_moderate_own_report`, `accepting_moderator_cannot_resolve_with_multiple_moderators`, `same_moderator_can_resolve_when_the_moderator_list_has_one_member`, `duplicate_moderators_are_refused_by_new`, `duplicate_moderators_are_refused_by_set_moderators`, `five_overturned_disputes_do_not_block_a_sixth_dispute` and `sixth_upheld_dispute_is_refused` (governance), `version_views_return_the_crate_and_state_versions` (the version views).

## Source of truth

- `contracts/reports/src/lib.rs:196` the body; `:268` `submit_report`; `:645` `subject_summary`; `:684` the deposit check; `:811` and `:817` the evidence checks; `:376`, `:492`, `:554` and `:776` the governance refusals; `:247` and `:251` the version views; `contracts/reports/src/events.rs` the event envelope.
- `docs/LADDER.md` the consumer rule.
