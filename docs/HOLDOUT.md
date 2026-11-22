# Receiver holdout, 22 November 2026

The recorded run refused 9 of 34 accounts labelled bad, or 26%. The 9 refused were 5 Tether blacklisted, 2 HAPI and 2 publicly reported accounts, all already in the label store. It refused 0 of the 25 bad accounts absent from that store. Among 218 presumed good accounts, it refused 0 and sent 6 to ASK, or 2.8%. Precision was 9 of 9 and rests on only 9 refusals.

These are the historical scores in [score.json](../fixtures/holdout/2026-11-22/score.json) and [score-excluding-labelled.json](../fixtures/holdout/2026-11-22/score-excluding-labelled.json). They describe this selected sample, not all NEAR accounts. A good label means presumed good for this measurement, not verified clean. No transfer was executed by the judgement run.

## Draw and provenance

The [draw record](../fixtures/holdout/2026-11-22/RULE.md) describes 252 distinct accounts, comprising 34 bad and 218 presumed good. The original target was 300 good accounts. The draw stopped at 218 good at block 225602615 because of public RPC and neardata throttling. This stop reason is the draw operator's recorded observation, not a provider rate limit verified here.

The bad side contains 26 HAPI registry assertions, 5 Tether blacklist observations and 3 reported incident accounts. The HAPI selection retained final categories Scam and Theft, comprising 19 Scam and 7 Theft, from a dump of the since deleted proxy.hapiprotocol.near registry. One final category None was omitted. Six HAPI accounts also appeared in the contributor draft and were deduplicated. The draft contributed 5 issuer rows, one Skyward Finance report row and two NEAR forum report rows. Its 100 good rows and 14 Rhea Finance rows were excluded. Nine retained bad accounts were marked as already present in the label store.

The draw record says the five Tether accounts were checked with get_blacklist_status on usdt.tether-token.near and returned the JSON string Banned at blocks 225642546 through 225642551. These observations and the source classifications are retained provenance. The original HAPI writes and all incident reports were not independently revalidated for this publication. Each row retains its source URL and note in [receivers.jsonl](../fixtures/holdout/2026-11-22/receivers.jsonl).

For the good side, the fixed rule walked forward from mainnet height 225600000. In shard order and receipt execution order, it considered the receiver of each successfully executed receipt containing a positive native Transfer. Only the first occurrence of each receiver counted. A final view_account read had to succeed with a positive amount and the all ones code_hash. The id had to match the named account pattern `^[a-z0-9_-]+[.]near$`. Subaccounts and implicit accounts were excluded. The named account fallback applied to every accepted row; none was verified to be 365 days old.

The good draw excluded subjects from the prior receiver set, the top receiver set including its Intents cohort, the label store and the bad rows. Bad rows were also compared with the prior receiver and top sets; none was dropped by that comparison. The walk used neardata receipt executions instead of RPC block and chunk calls. Heights 225600000 through 225602615 were walked, with 225601519 omitted after a failed fetch. Later fetched blocks were not used. This sampling rule selects active, funded, named accounts without deployed local code and does not represent arbitrary receivers.

## Recorded judgement

Rules version 3 is attributed to commit fb7765763. Runner code was based on 7505f2cac with the then uncommitted judge-accounts command. The rule source is identical between those commits. The command judged a proposed native transfer of 1 NEAR to each account, with no memo, an empty allowlist and empty caps. It neither signed a verdict nor sent a payment.

The exact [account input](../fixtures/holdout/2026-11-22/accounts-shuffled.txt) contains only account ids and is sorted by SHA256 of the UTF-8 account id, independently of labels. The 252 ids match the labelled file exactly. This order matters because new NearBlocks age reads were limited to the first 80 accounts. Future runs must use the same hash ordering.

The [run directory](../fixtures/holdout/2026-11-22/run/) retains the account list and label store hash in set.json, the clock and last block proof in judged-block.json, the read fixtures in rpc and one judgement per account in judgements. The judged clock is 1795340103342 milliseconds since the Unix epoch. Its archival proof identifies block 225686896 and the following block 225686897. Individual current account reads can name later blocks; this was a record run with bounded pinned rejudgement for unresolved ages, not a claim that every read came from one block.

The [frozen label store](../fixtures/holdout/2026-11-22/labels.jsonl) is a byte copy of intel/labels.jsonl from commit 7505f2cac. Its digest in [labels.sha256](../fixtures/holdout/2026-11-22/labels.sha256) matches the label_store_sha256 recorded in set.json. Replay and label exclusion use this copy rather than the live store, which can receive later assertions.

The labelled file SHA256 is recorded in both score files and was independently checked against its bytes. The input set, hash ordering, 252 judgement count and table below were independently checked from the retained files. The historical draw was not repeated.

## Historical result

UNKNOWN is scored as ASK. Refused means DENY. ASK is kept separate and does not count as a refusal.

| Rows | Count | DENY | ALLOW | ASK | NOT_JUDGED |
|---|---:|---:|---:|---:|---:|
| Bad, all | 34 | 9 | 25 | 0 | 0 |
| Bad, already in label store | 9 | 9 | 0 | 0 | 0 |
| Bad, absent from label store | 25 | 0 | 25 | 0 | 0 |
| Presumed good | 218 | 0 | 212 | 6 | 0 |

The historical recall is 9 of 34, or 26.5% before rounding to a whole percentage. The observed good refusal rate is 0 of 218. The six good ASK answers request confirmation of young receivers. The exclusion report removes the nine labelled rows and leaves 243 rows with no DENY. Its precision value of zero is the scorer's convention when there are no refusals, not evidence of incorrect refusals.

The five Tether blacklist accounts are DENY in the retained native NEAR judgements. The receiver_exists check reported all five as nonexistent on mainnet, so they would be refused even without their labels. Each also has label_denylist and issuer_blacklist signals. The issuer check is applied when the caller supplies issuer entries, including native transfers. The observed detection gap is the 25 bad accounts absent from the label store, all of which received ALLOW. Any rule change addressing that gap needs a later version and a fresh holdout.

Replay through the command reproduces all 252 recorded judgement files and both score files byte for byte. Five DENY judgements have unknown age. Four lack recorded age responses; the account beginning f712 has a recorded NearBlocks keys response containing no creation time. Their established signals already require DENY. Those age reads cannot change the refusal and do not make it NOT_JUDGED. A missing required read that leaves the rules at UNKNOWN is not scoreable; a recorded unavailable response can still produce a scoreable ASK. The replay has no NOT_JUDGED accounts and matches the table above.

The exclusion comparison uses the 27 NEAR subjects with unsuperseded incident or denylist assertions in the frozen label store. It removes nine holdout accounts. The 27 is not the total number of labelled subjects.

## Reproduction

Use a full clone, not a shallow clone. Local Git history must contain commit fb7765763 to verify the recorded rule source and resolve the historical score metadata. Run the checked reproduction from the repository root:

```
python -m unittest tests.test_holdout -v
```

The retained judgements can also be scored directly. Always name the frozen label file when reproducing the exclusion report:

```
python eval/score.py --verdicts fixtures/holdout/2026-11-22/run/judgements --holdout fixtures/holdout/2026-11-22/receivers.jsonl --rules-commit fb7765763 --labels fixtures/holdout/2026-11-22/labels.jsonl --json
python eval/score.py --verdicts fixtures/holdout/2026-11-22/run/judgements --holdout fixtures/holdout/2026-11-22/receivers.jsonl --rules-commit fb7765763 --labels fixtures/holdout/2026-11-22/labels.jsonl --exclude-labelled --json
```

## Reuse

This holdout is spent once its accounts enter the label store. Its labels and results are now public. Any later score used to judge a rules change needs a fresh holdout drawn without selecting on that change's answers. Replaying these files checks reproducibility only.

## External sources checked

The following primary sources were read for the interface facts used above. They do not independently establish the historical incident labels or the completeness of the draw.

* [NEAR account and contract RPC documentation](https://docs.near.org/api/rpc/contracts) specifies query with request_type view_account, finality or block_id and account_id, and shows amount and code_hash fields. It also specifies read only call_function queries with method_name and base64 arguments, returning serialized result bytes.
* [FastNEAR neardata server](https://github.com/fastnear/neardata-server) identifies the mainnet service at https://mainnet.neardata.xyz.
* [FastNEAR neardata OpenAPI](https://raw.githubusercontent.com/fastnear/neardata-server/main/openapi/openapi.yaml) documents block envelopes, shard payloads and receipt execution data. The draw's exact selection and omissions are documented by the local draw record.
