# Raw source snapshots

These six files are source responses retained byte for byte. When a label or incident source records `content_sha256`, `intel/seed.py` computes it from the exact response bytes. A reader can recompute SHA-256 from any snapshot; full digests appear in label rows and incident records where applicable. The manifest at `intel/seeds/README.md` lists all six snapshots with abbreviated digests, including sources with no label rows.

| File | Publisher | Source URL | Fetched (UTC) | Why this copy is kept |
|---|---|---|---|---|
| `1click-tokens-20261029T092002Z.json` | 1Click token catalog (NEAR Intents) | <https://1click.chaindefuser.com/v0/tokens> | 2026-10-29 09:20:02Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `near-intents-treasury-addresses-20261029T092003Z.md` | NEAR Intents | <https://docs.near-intents.org/security-compliance/treasury-addresses.md> | 2026-10-29 09:20:03Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `rekt-rhea-20261029T112635Z.html` | rekt.news | <https://rekt.news/rhea-finance-rekt> | 2026-10-29 11:26:35Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `intents-quote-doc-20261029T114539Z.md` | NEAR Intents documentation | <https://docs.near-intents.org/api-reference/oneclick/request-a-swap-quote.md> | 2026-10-29 11:45:39Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `fireblocks-sweat-hot-20261029T152642Z.html` | Fireblocks | <https://www.fireblocks.com/blog/near-zero-day-sweat-hot-token-exploit> | 2026-10-29 15:26:42Z | The exact bytes make the recorded SHA-256 independently reproducible. |
| `defillama-hacks-20261029T152644Z.json` | DeFiLlama hacks API | <https://api.llama.fi/hacks> | 2026-10-29 15:26:44Z | The exact bytes make the recorded SHA-256 independently reproducible. |

These publisher copies are excluded from the repository's Apache-2.0 license and remain the publishers' material.

A publisher may request removal by opening an issue. On request, the raw copy will be removed and its entry replaced by the source URL, SHA-256 hash, and an archive link.

## OFAC Specially Designated Nationals list

No raw copy of this source is kept: the file is about 128 MB. `python -m intel sanctions-fetch` reads it and commits only its digital currency addresses, under `fixtures/sanctions/`, with the file's SHA-256, its fetch time and the list's date of issue, so a holder of a copy with that SHA-256 can derive the same addresses again (`docs/SANCTIONS.md`).

| Publisher | Source URL | Terms |
|---|---|---|
| Office of Foreign Assets Control (OFAC), US Department of the Treasury | <https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML> | A work of the United States government, not subject to copyright in the United States (17 U.S.C. 105). Free to download, with no account. |

OFAC names this host for its list files in its technical notice of 16 May 2024: <https://ofac.treasury.gov/sdn-list-data-formats-data-schemas/ofac-technical-actions-in-reverse-chronological-order/20240516_44>.

The addresses are used as published, each with OFAC's code and the entry's uid, name and programs; the only addition is a comparison key, the address lower cased when it is an EVM address. No claim goes beyond the list: nothing here says who controls an address, and an address the list does not name is not judged by it. A match will mean DENY with the SDN entry and the list's date of issue. The list issued 6 Nov 2026 has no NEAR account (none of its 1064 distinct addresses).
