# Incidents: pages, records and labels

Three things in this repository describe an incident, and they are not the same file. This page says what each one is, how they relate, and which of them exist on main today.

| | What it is | Where | On main |
|---|---|---|---|
| Label | one sourced assertion about one subject, such as an `incident` row | `intel/labels.jsonl`, format in `docs/LABEL-RECORD.md` | yes |
| Incident page | a page about one public incident, built from its sources and the label store | `intel/incidents/<id>.json`, rendered to `incidents/<id>.html` | yes, three pages |
| Incident record | the off chain account of one event, citing on chain reports | format v1 in `docs/RECORD.md` | no: the format only |

## Labels decide, pages explain

A verdict reads labels, never pages. The verdict service treats a current `incident` or `denylist` assertion on the receiver or the token as a signal and copies the assertion id into the evidence (`docs/LABEL-RECORD.md`, Categories). An incident page adds nothing to that: it is the human readable account of the sources the labels came from, with the ids of the labels it relies on.

Each account an incident page names carries `assertion_ids`, the ids of its rows in `intel/labels.jsonl`. Siyab never names an account as a hostile party or a victim in its own words. A source's own words about an account may appear only as a quote attributed to that source, as required by `docs/LABEL-RECORD.md` under `Source strings are data`.

## What exists on main today

The three pages under `intel/incidents/`:

| File | Incident | Accounts named | Built by |
|---|---|---|---|
| `intel/incidents/rhea.json` | Rhea Lend margin trading route exploit, 2026-04-16 | 17, from source `rekt-rhea` | `python -m intel seed rhea` |
| `intel/incidents/sweat-hot.json` | SWEAT token `ft_resolve_transfer` exploit and the HOT token patch, 2026-04-29 | 1, the HOT token contract the source names | `python -m intel seed sweat-hot` |
| `intel/incidents/intents-2026-10-01.json` | NEAR Intents exploit, 2026-10-01 | none: the cited source names no account | `python -m intel seed intents-2026-10-01` |

The seed commands are in `intel/seed.py`; in the default replay mode they read the committed snapshots and recorded reads and open no network. Each page holds `id`, `title`, `date`, `chains`, `sources`, `accounts`, `onchain_facts`, `rules_today`, `not_public` and `standard`, plus keys ending in `_as_reported` for figures quoted from a source, and on some pages `onchain_state`, `route_subjects` and `rules_today_at_ms`; `SCHEMA` and `ITEM_KEYS` in `tests/test_incidents.py` pin the keys every page must hold. `rules_today` holds the verdict the rules give each named account today, and `not_public` says what the sources do not show and the page therefore does not claim.

`incident_page` in `intel/render.py` renders each page to `incidents/<id>.html`, and `intel/api.py` serves the JSON at `/incidents/<id>` and the page at `/incidents/<id>.html`. The account pages link back to every incident page that names the account. `python scripts/counts.py` counts the pages for the README (`Incidents: I pages.`).

## Replay at an incident block

After a run has been recorded, replay it offline:

```
python -m gate incident-replay rhea
```

The command reads `fixtures/replay/rhea/run.json` and the recorded RPC fixtures, recomputes the checks and guard outcomes, and refuses a mismatch. Every fixture the run reads, including height resolution, must be listed in the manifest and match its digest; the verified bytes are the bytes parsed. Replay proves that the derived results are consistent with a trusted `run.json` and its fixtures. It does not prove chain authenticity: someone who replaces the fixtures, their digests and the results together can produce a self-consistent forgery. Without a recorded run it cannot replay. The incident page adds its replay table only when that run file exists.

Recording is a separate network read for the operator:

```
python -m gate incident-replay rhea --record --at 2026-04-16T00:00:00Z
```

`NEAR_ARCHIVAL_RPC_URL` selects the archival endpoint when set. The command selects record or replay mode explicitly; `GATE_FIXTURES_MODE` does not select its mode. Recording writes `fixtures/replay/<incident>/run.json` and its RPC fixtures under `fixtures/replay/<incident>/rpc`, so one incident's recording never replaces another's. The committed Rhea run uses that layout. The default as-of time is 00:00 UTC on the incident date. Recording resolves the last final block at or before that time and records its height and timestamp; offline replay repeats that search from verified fixtures and compares the resulting height and timestamp with the recorded bounds.

The replay selects the NEAR accounts whose reported roles are master, collector or worker. Each request asks about 1 NEAR with no memo, an empty allowlist and a deliberately empty label store, including no incident labels. All chain reads are bounded by the selected block; NearBlocks is not used. This is state at one block, not a complete transaction history. A young access key does not prove a young account, and absence at an older block alone does not prove creation within seven days. Absence at that block and at a second block whose read-back time is less than seven days before the replay block does prove it, and the check signals: the recorded Rhea collector is DENY for that reason. When those reads cannot establish account age, the check remains UNKNOWN. An UNKNOWN check never counts as a pass; without a denying signal, an incomplete verdict produces ASK rather than ALLOW.

This replay shows what the current rules would have answered from the recorded on-chain state without labels. It is not a live prediction, evidence that anyone asked then, or proof of who controlled an account. The recorded answers are published as returned, including unknowns.

## How records will relate

An incident record (`docs/RECORD.md`) is the format for an event that arrives as a community report on the reports contract, or that the publisher records with on chain reports behind it. It differs from an incident page in three ways: it is content addressed and never edited, it cites on chain reports by network, contract, report id and body hash, and it carries the readable text behind each moderation reason hash.

A record and a page are linked through the label store: a record's `assertion_ids` and a page's `assertion_ids` name the same kind of row. Neither replaces the other or decides a verdict. The verdict service reads labels in `gate/rules.py`; for community reports, it reads `subject_summary` and applies `intel/reports.py`'s `effect` only when `REPORTS_ACCOUNT_ID` is set. The `pending` field is a count only and has no verdict effect. When the variable is unset, no community report check runs. See `docs/REPORTING.md` for the report effects.

## What does not exist on main

- No incident record in the v1 format, no record store, no validator and no renderer for one.
- No incident page cites a community report or a record; the three pages rest on public sources and chain reads only.
- The reports contract has one on-chain report, the synthetic report filed on 8 Nov by our own accounts against our own demo account and overturned by the same owner in that run; it is not evidence of a community incident (`docs/TESTNET.md`).
- No page of measured service times. The published times are in `docs/REPORTING.md` and `docs/service_times.json`; the synthetic report run is not a community service-time sample.
- No takedown or contact channel off chain. Corrections follow `docs/REPORTING.md`, Corrections.

## Corrections to a page

An incident page is generated, not hand edited: its seed command writes it, and the label rows it cites, from the committed source snapshot and the recorded chain reads. A wrong account or a wrong role is corrected by a reviewed change to the seed or a new snapshot; the label rows that change are replaced by new rows and the old rows' `superseded_by` (`docs/LABEL-RECORD.md`), never edited, and the page is written again. The git history keeps every earlier version of the page.
