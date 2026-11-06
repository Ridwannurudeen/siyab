# Seed snapshots

Each source snapshot under `raw/` is the body a source returned, stored byte for byte and named with its fetch time in UTC. The label rows cite each snapshot by its full sha256, which is shortened here to its first and last four hex characters. `python -m intel seed <source>` rewrites this file, and `tests/test_labels.py` checks it against the snapshots and the rows.

| Snapshot | Source | sha256 | Fetched (UTC) | Rows |
|---|---|---|---|---|
| `raw/1click-tokens-20261029T092002Z.json` | https://1click.chaindefuser.com/v0/tokens | 3bce...f8c6 | 2026-10-29T09:20:02Z | 316 |
| `raw/near-intents-treasury-addresses-20261029T092003Z.md` | https://docs.near-intents.org/security-compliance/treasury-addresses.md | 701e...3843 | 2026-10-29T09:20:03Z | 34 |
| `raw/rekt-rhea-20261029T112635Z.html` | https://rekt.news/rhea-finance-rekt | 1743...6c1f | 2026-10-29T11:26:35Z | 17 |
| `raw/fireblocks-sweat-hot-20261029T152642Z.html` | https://www.fireblocks.com/blog/near-zero-day-sweat-hot-token-exploit | 364b...2641 | 2026-10-29T15:26:42Z | 1 |
| `raw/defillama-hacks-20261029T152644Z.json` | https://api.llama.fi/hacks | 7069...e103 | 2026-10-29T15:26:44Z | 0 |
| `raw/intents-quote-doc-20261029T114539Z.md` | https://docs.near-intents.org/api-reference/oneclick/request-a-swap-quote.md | b108...4d9a | 2026-10-29T11:45:39Z | 0 |
