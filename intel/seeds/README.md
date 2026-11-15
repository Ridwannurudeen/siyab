# Seed snapshots

Each source snapshot under `raw/` is the body a source returned, stored byte for byte and named with its fetch time in UTC. The label rows cite each snapshot by its full sha256, which is shortened here to its first and last four hex characters. `python -m intel seed <source>` rewrites this file, and `tests/test_labels.py` checks it against the snapshots and the rows.

| Snapshot | Source | sha256 | Fetched (UTC) | Rows |
|---|---|---|---|---|
| `raw/1click-tokens-20261029T092002Z.json` | https://1click.chaindefuser.com/v0/tokens | 3bce...f8c6 | 2026-10-29T09:20:02Z | 316 |
| `raw/near-intents-treasury-addresses-20261029T092003Z.md` | https://docs.near-intents.org/security-compliance/treasury-addresses.md | 701e...3843 | 2026-10-29T09:20:03Z | 34 |
| `raw/rekt-rhea-20261029T112635Z.html` | https://rekt.news/rhea-finance-rekt | 1743...6c1f | 2026-10-29T11:26:35Z | 17 |
| `raw/fireblocks-sweat-hot-20261029T152642Z.html` | https://www.fireblocks.com/blog/near-zero-day-sweat-hot-token-exploit | 364b...2641 | 2026-10-29T15:26:42Z | 1 |
| `raw/rekt-skyward-20261115T093012Z.html` | https://rekt.news/skyward-rekt | 83db...72f8 | 2026-11-15T09:30:12Z | 1 |
| `raw/l2-forum-21725-20261115T140053Z.json` | https://gov.near.org/t/21725.json | 342e...ff6d | 2026-11-15T14:00:53Z | 1 |
| `raw/l2-forum-11112-20261115T140054Z.json` | https://gov.near.org/t/11112.json | dd49...9b1e | 2026-11-15T14:00:54Z | 1 |
| `raw/l2-forum-25445-20261115T140336Z.json` | https://gov.near.org/t/25445.json | ef95...e1da | 2026-11-15T14:03:36Z | 1 |
| `raw/defillama-hacks-20261029T152644Z.json` | https://api.llama.fi/hacks | 7069...e103 | 2026-10-29T15:26:44Z | 0 |
| `raw/intents-quote-doc-20261029T114539Z.md` | https://docs.near-intents.org/api-reference/oneclick/request-a-swap-quote.md | b108...4d9a | 2026-10-29T11:45:39Z | 0 |
| `raw/l2-aurora-bounties-20261115T140031Z.json` | https://forum.aurora.dev/t/1257.json | 8cb7...a972 | 2026-11-15T14:00:31Z | 0 |
| `raw/l2-chainabuse-home-20261115T135613Z.html` | https://www.chainabuse.com/ | 1e6c...1539 | 2026-11-15T13:56:13Z | 0 |
| `raw/l2-forum-17380-20261115T140054Z.json` | https://gov.near.org/t/17380.json | 1d7a...8932 | 2026-11-15T14:00:54Z | 0 |
| `raw/l2-forum-17616-20261115T140843Z.json` | https://gov.near.org/t/17616.json | d234...1732 | 2026-11-15T14:08:43Z | 0 |
| `raw/l2-forum-17750-20261115T140841Z.json` | https://gov.near.org/t/17750.json | 9925...412e | 2026-11-15T14:08:41Z | 0 |
| `raw/l2-forum-18659-20261115T140052Z.json` | https://gov.near.org/t/18659.json | 58ac...f1d7 | 2026-11-15T14:00:52Z | 0 |
| `raw/l2-forum-19675-20261115T140842Z.json` | https://gov.near.org/t/19675.json | 4984...31c0 | 2026-11-15T14:08:42Z | 0 |
| `raw/l2-forum-2733-20261115T140053Z.json` | https://gov.near.org/t/2733.json | 1d70...1758 | 2026-11-15T14:00:53Z | 0 |
| `raw/l2-forum-35032-20261115T140841Z.json` | https://gov.near.org/t/35032.json | 2228...fad2 | 2026-11-15T14:08:41Z | 0 |
| `raw/l2-forum-38862-20261115T140052Z.json` | https://gov.near.org/t/38862.json | 64c1...5f84 | 2026-11-15T14:00:52Z | 0 |
| `raw/l2-forum-42360-20261115T140842Z.json` | https://gov.near.org/t/42360.json | caad...6a79 | 2026-11-15T14:08:42Z | 0 |
| `raw/l2-near-discord-20261115T135729Z.html` | https://www.near.org/blog/post-mortem-near-discord-security-incident | 654e...e6e0 | 2026-11-15T13:57:29Z | 0 |
| `raw/l2-near-wallet-update-20261115T135728Z.html` | https://www.near.org/blog/near-web-wallet-security-update | e291...fd34 | 2026-11-15T13:57:28Z | 0 |
| `raw/l2-nearblocks-openapi-20261115T140447Z.json` | https://api.nearblocks.io/openapi.json | b5d3...6a44 | 2026-11-15T14:04:47Z | 0 |
| `raw/l2-quill-rhea-20261115T135752Z.html` | https://www.quillaudits.com/blog/hack-analysis/rhea-finance-hack | d29f...db01 | 2026-11-15T13:57:52Z | 0 |
| `raw/l2-ref-week22-20261115T140028Z.json` | https://gov.near.org/t/5823.json | aea1...3b68 | 2026-11-15T14:00:28Z | 0 |
| `raw/l2-rekt-intents-20261115T135906Z.html` | https://rekt.news/near-intents-rekt | bc7d...727e | 2026-11-15T13:59:06Z | 0 |
| `raw/l2-telegram-final-post-20261115T141436Z.json` | https://gov.near.org/t/21725/21.json | c360...49ea | 2026-11-15T14:14:36Z | 0 |
| `raw/l2-uk-sanctions-list-page-20261115T135612Z.html` | https://www.gov.uk/government/publications/the-uk-sanctions-list | b8b1...a993 | 2026-11-15T13:56:12Z | 0 |
