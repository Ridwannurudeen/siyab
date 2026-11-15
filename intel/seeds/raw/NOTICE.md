# Raw source snapshots

These 29 files are source responses retained byte for byte. When a label or incident source records `content_sha256`, `intel/seed.py` computes it from the exact response bytes. A reader can recompute SHA-256 from any snapshot; full digests appear in label rows and incident records where applicable. The manifest at `intel/seeds/README.md` lists all 29 snapshots with abbreviated digests, including sources with no label rows.

| File | Publisher | Source URL | Fetched (UTC) | Why this copy is kept |
|---|---|---|---|---|
| `1click-tokens-20261029T092002Z.json` | 1Click token catalog (NEAR Intents) | <https://1click.chaindefuser.com/v0/tokens> | 2026-10-29 09:20:02Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `near-intents-treasury-addresses-20261029T092003Z.md` | NEAR Intents | <https://docs.near-intents.org/security-compliance/treasury-addresses.md> | 2026-10-29 09:20:03Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `rekt-rhea-20261029T112635Z.html` | rekt.news | <https://rekt.news/rhea-finance-rekt> | 2026-10-29 11:26:35Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `intents-quote-doc-20261029T114539Z.md` | NEAR Intents documentation | <https://docs.near-intents.org/api-reference/oneclick/request-a-swap-quote.md> | 2026-10-29 11:45:39Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `fireblocks-sweat-hot-20261029T152642Z.html` | Fireblocks | <https://www.fireblocks.com/blog/near-zero-day-sweat-hot-token-exploit> | 2026-10-29 15:26:42Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `defillama-hacks-20261029T152644Z.json` | DeFiLlama hacks API | <https://api.llama.fi/hacks> | 2026-10-29 15:26:44Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `rekt-skyward-20261115T093012Z.html` | rekt.news | <https://rekt.news/skyward-rekt> | 2026-11-15 09:30:12Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `l2-aurora-bounties-20261115T140031Z.json` | Aurora forum contributors | <https://forum.aurora.dev/t/1257.json> | 2026-11-15 14:00:31Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-chainabuse-home-20261115T135613Z.html` | Chainabuse | <https://www.chainabuse.com/> | 2026-11-15 13:56:13Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-forum-11112-20261115T140054Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/11112.json> | 2026-11-15 14:00:54Z | Attribution replay and the quoted collection or exploit report. |
| `l2-forum-17380-20261115T140054Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/17380.json> | 2026-11-15 14:00:54Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-forum-17616-20261115T140843Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/17616.json> | 2026-11-15 14:08:43Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-forum-17750-20261115T140841Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/17750.json> | 2026-11-15 14:08:41Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-forum-18659-20261115T140052Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/18659.json> | 2026-11-15 14:00:52Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-forum-19675-20261115T140842Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/19675.json> | 2026-11-15 14:08:42Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-forum-21725-20261115T140053Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/21725.json> | 2026-11-15 14:00:53Z | Attribution replay and the quoted collection or exploit report. |
| `l2-forum-25445-20261115T140336Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/25445.json> | 2026-11-15 14:03:36Z | Attribution replay and the quoted collection or exploit report. |
| `l2-forum-2733-20261115T140053Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/2733.json> | 2026-11-15 14:00:53Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-forum-35032-20261115T140841Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/35032.json> | 2026-11-15 14:08:41Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-forum-38862-20261115T140052Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/38862.json> | 2026-11-15 14:00:52Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-forum-42360-20261115T140842Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/42360.json> | 2026-11-15 14:08:42Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-near-discord-20261115T135729Z.html` | NEAR Foundation | <https://www.near.org/blog/post-mortem-near-discord-security-incident> | 2026-11-15 13:57:29Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-near-wallet-update-20261115T135728Z.html` | NEAR Foundation | <https://www.near.org/blog/near-web-wallet-security-update> | 2026-11-15 13:57:28Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-nearblocks-openapi-20261115T140447Z.json` | NearBlocks | <https://api.nearblocks.io/openapi.json> | 2026-11-15 14:04:47Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-quill-rhea-20261115T135752Z.html` | QuillAudits | <https://www.quillaudits.com/blog/hack-analysis/rhea-finance-hack> | 2026-11-15 13:57:52Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-ref-week22-20261115T140028Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/5823.json> | 2026-11-15 14:00:28Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-rekt-intents-20261115T135906Z.html` | rekt.news | <https://rekt.news/near-intents-rekt> | 2026-11-15 13:59:06Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-telegram-final-post-20261115T141436Z.json` | NEAR governance forum contributors | <https://gov.near.org/t/21725/21.json> | 2026-11-15 14:14:36Z | Source review and exclusions; this snapshot adds no label row. |
| `l2-uk-sanctions-list-page-20261115T135612Z.html` | UK Foreign, Commonwealth and Development Office | <https://www.gov.uk/government/publications/the-uk-sanctions-list> | 2026-11-15 13:56:12Z | Source review and exclusions; this snapshot adds no label row. |

These publisher copies are excluded from the repository's Apache-2.0 license and remain the publishers' material.

A publisher may request removal by opening an issue. On request, the raw copy will be removed and its entry replaced by the source URL, SHA-256 hash, and an archive link.

## OFAC Specially Designated Nationals list

No raw copy of this source is kept: the file is about 128 MB. `python -m intel sanctions-fetch` reads it and commits only its digital currency addresses, under `fixtures/sanctions/`, with the file's SHA-256, its fetch time and the list's date of issue, so a holder of a copy with that SHA-256 can derive the same addresses again (`docs/SANCTIONS.md`).

| Publisher | Source URL | Terms |
|---|---|---|
| Office of Foreign Assets Control (OFAC), US Department of the Treasury | <https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML> | A work of the United States government, not subject to copyright in the United States (17 U.S.C. 105). Free to download, with no account. |

OFAC names this host for its list files in its technical notice of 16 May 2024: <https://ofac.treasury.gov/sdn-list-data-formats-data-schemas/ofac-technical-actions-in-reverse-chronological-order/20240516_44>.

The addresses are used as published, each with OFAC's code and the entry's uid, name and programs; the only addition is a comparison key, which lower cases EVM, bech32 and bech32m addresses and otherwise preserves the address as published. No claim goes beyond the list: nothing here says who controls an address, and an address the list does not name is not judged by it. A match will mean DENY with the SDN entry and the list's date of issue. The list issued 6 Nov 2026 has no NEAR account (none of its 1064 distinct addresses).
