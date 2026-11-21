# Holdout receiver set

A labelled set of NEAR mainnet accounts for scoring the rules. One object per line in `receivers.jsonl`:

```json
{"account": "example.near", "label": "bad", "source_url": "https://example.com/report", "note": "What the source says."}
```

`label` is `bad` or `good`. Every row has a public `source_url` and a `note` in our own words. No account is in `fixtures/receivers/top.json`, the 990 judged top receivers. Every good account was checked with `view_account` before it was written; a few bad accounts no longer exist and are kept because the report still names them.

Current size: 128 rows, 28 bad and 100 good.

## The bad rows

- 14 accounts named in the rekt.news Rhea Finance report, including the collector, the master account, five worker accounts, four token contracts and three multichain accounts.
- 1 account named in the rekt.news Skyward Finance report.
- 5 accounts the Tether issuer blacklist marks as banned on NEAR, read through `get_blacklist_status`.
- 2 accounts from NEAR forum reports that the label store already asserts: the fake NEAR Wallet collection account and the Kangaroo Coin Flip account.
- 1 account from the May 2022 Telegram impersonation report, also in the label store.
- 5 accounts from NEAR forum reports that the label store does not assert: `dolbabob.near`, two intermediate accounts from the May 2022 chain, `9ebef819...`, and `5c33c621...`. These are reported, not adjudicated, and the notes say so.

23 of the 28 bad rows are already incident or denylist subjects in `intel/labels.jsonl`, because the same public reports are the source for both. Recall on those rows is therefore expected to be complete and is not evidence of detection quality. The five forum rows and the issuer rows are the part that measures something new. `python eval/score.py --exclude-labelled` prints the score with and without the label store subjects.

### Why not 100 bad rows

The public record does not carry 100 bad NEAR mainnet accounts with links we can defend. Measured on 2026-11-21:

- OFAC publishes no NEAR account (the SDN extract has 0 under a NEAR code).
- Neither Tether nor Circle exposes a list of blacklisted accounts; the contracts answer one account at a time, and the USDT and USDC contracts are global contracts whose state cannot be enumerated.
- Chainabuse's NEAR category has about 10 reports, and its API needs a key.
- The rekt.news articles about NEAR incidents name 14 accounts between them.
- The NEAR forum's scam threads name about a dozen accounts, and several are the reporting user's own wallet.

We will not invent rows, and we will not label an account bad on a guess. To reach 100, the fastest honest route is an export of the subject accounts from the reports contract or another internal list, with the report as the source. The scorer does not change when rows are added.

## The good rows

- 40 validators in the protocol validator set at the recorded epoch start block, with their stake in the note.
- 10 exchange hot or cold wallets: Binance, Binance cold wallets 1 to 3, Coinbase, Kraken, OKX, OKEx, Bybit, Gate.io, Gate, KuCoin and the Tether treasury.
- 50 Sputnik DAOs created by the DAO factory, read with `get_dao_list`.

Each account was checked with `view_account` before it was written, so a missing account is not mislabelled as good. Good rows are not in the allowlist reference set, so they do not quiet the lookalike check.

## Scoring

```bash
python eval/score.py --verdicts path/to/verdicts
python eval/score.py --verdicts path/to/verdicts --exclude-labelled
```

Each verdict file is one account. The account is read from `subject`, `account` or `receiver_id`, or from the file name. The outcome is read from `verdict` when it is a string, else from `outcome`, and `UNKNOWN` counts as `ASK`. ASK is counted on its own and left out of precision and recall. Accounts with no verdict file are listed and left out of the matrix.
