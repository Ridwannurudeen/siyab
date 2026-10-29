# Seed snapshots

Each file under `raw/` is the body a source returned, stored byte for byte and named with its fetch time in UTC. The label rows cite each snapshot by its full sha256, which is shortened here to its first and last four hex characters. `python -m intel seed <source>` rewrites this file, and `tests/test_labels.py` checks it against the snapshots and the rows.

| Snapshot | Source | sha256 | Fetched (UTC) | Rows |
|---|---|---|---|---|
| `raw/1click-tokens-20261029T092002Z.json` | https://1click.chaindefuser.com/v0/tokens | 3bce...f8c6 | 2026-10-29T09:20:02Z | 312 |
| `raw/near-intents-treasury-addresses-20261029T092003Z.md` | https://docs.near-intents.org/security-compliance/treasury-addresses.md | 701e...3843 | 2026-10-29T09:20:03Z | 34 |
