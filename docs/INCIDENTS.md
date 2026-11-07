# Incidents: pages, records and labels

Three things in this repository describe an incident, and they are not the same file. This page says what each one is, how they relate, and which of them exist on main today.

| | What it is | Where | On main |
|---|---|---|---|
| Label | one sourced assertion about one subject, such as an `incident` row | `intel/labels.jsonl`, format in `docs/LABEL-RECORD.md` | yes |
| Incident page | a page about one public incident, built from its sources and the label store | `intel/incidents/<id>.json`, rendered to `incidents/<id>.html` | yes, three pages |
| Incident record | the off chain account of one event, citing on chain reports | format v1 in `docs/RECORD.md` | no: the format only |

## Labels decide, pages explain

A verdict reads labels, never pages. The verdict service treats a current `incident` or `denylist` assertion on the receiver or the token as a signal and copies the assertion id into the evidence (`docs/LABEL-RECORD.md`, Categories). An incident page adds nothing to that: it is the human readable account of the sources the labels came from, with the ids of the labels it relies on.

Each account an incident page names carries `assertion_ids`, the ids of its rows in `intel/labels.jsonl`. ShieldBot never names an account as a hostile party or a victim in its own words. A source's own words about an account may appear only as a quote attributed to that source, as required by `docs/LABEL-RECORD.md` under `Source strings are data`.

## What exists on main today

The three pages under `intel/incidents/`:

| File | Incident | Accounts named | Built by |
|---|---|---|---|
| `intel/incidents/rhea.json` | Rhea Lend margin trading route exploit, 2026-04-16 | 17, from source `rekt-rhea` | `python -m intel seed rhea` |
| `intel/incidents/sweat-hot.json` | SWEAT token `ft_resolve_transfer` exploit and the HOT token patch, 2026-04-29 | 1, the HOT token contract the source names | `python -m intel seed sweat-hot` |
| `intel/incidents/intents-2026-10-01.json` | NEAR Intents exploit, 2026-10-01 | none: the cited source names no account | `python -m intel seed intents-2026-10-01` |

The seed commands are in `intel/seed.py`; in the default replay mode they read the committed snapshots and recorded reads and open no network. Each page holds `id`, `title`, `date`, `chains`, `sources`, `accounts`, `onchain_facts`, `rules_today`, `not_public` and `standard`, plus keys ending in `_as_reported` for figures quoted from a source, and on some pages `onchain_state`, `route_subjects` and `rules_today_at_ms`; `SCHEMA` and `ITEM_KEYS` in `tests/test_incidents.py` pin the keys every page must hold. `rules_today` holds the verdict the rules give each named account today, and `not_public` says what the sources do not show and the page therefore does not claim.

`incident_page` in `intel/render.py` renders each page to `incidents/<id>.html`, and `intel/api.py` serves the JSON at `/incidents/<id>` and the page at `/incidents/<id>.html`. The account pages link back to every incident page that names the account. `python scripts/counts.py` counts the pages for the README (`Incidents: I pages.`).

## How records will relate

An incident record (`docs/RECORD.md`) is the format for an event that arrives as a community report on the reports contract, or that the publisher records with on chain reports behind it. It differs from an incident page in three ways: it is content addressed and never edited, it cites on chain reports by network, contract, report id and body hash, and it carries the readable text behind each moderation reason hash.

A record and a page are linked through the label store: a record's `assertion_ids` and a page's `assertion_ids` name the same kind of row. Neither replaces the other or decides a verdict. The verdict service reads labels in `gate/rules.py`; for community reports, it reads `subject_summary` and applies `intel/reports.py`'s `effect` only when `REPORTS_ACCOUNT_ID` is set. The `pending` field is a count only and has no verdict effect. When the variable is unset, no community report check runs. See `docs/REPORTING.md` for the report effects.

## What does not exist on main

- No incident record in the v1 format, no record store, no validator and no renderer for one.
- No incident page cites a community report or a record; the three pages rest on public sources and chain reads only.
- No on chain report to cite. The reports contract is deployed on testnet at `reports.kestrelridge-main.testnet` (`res/BUILD-reports.md`, Deployed line; `docs/TESTNET.md`), and the read back after its 0.1.2 redeploy shows 0 reports and 0 disputes (`docs/TESTNET.md`): no report has been filed.
- No page of measured service times. The published times are in `docs/REPORTING.md` and `docs/service_times.json`; until a report is filed there is nothing to measure.
- No takedown or contact channel off chain. Corrections follow `docs/REPORTING.md`, Corrections.

## Corrections to a page

An incident page is generated, not hand edited: its seed command writes it, and the label rows it cites, from the committed source snapshot and the recorded chain reads. A wrong account or a wrong role is corrected by a reviewed change to the seed or a new snapshot; the label rows that change are replaced by new rows and the old rows' `superseded_by` (`docs/LABEL-RECORD.md`), never edited, and the page is written again. The git history keeps every earlier version of the page.
