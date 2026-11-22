# Siyab private holdout, rules v3 (main fb7765763) - RULE

Built 2026-11-22. Read-only; nothing committed, no transactions sent, nothing written outside this folder.

## Result: 252 rows = 34 bad + 218 good (target was 300 good; NOT reached)
The good draw stopped at 218 rows. Last block of the draw: **225602615** (the block that yielded the 218th good row). Reason: public RPC / neardata throttling (HTTP 429 above roughly one request per 2 s, JSON-RPC batching unsupported), about 13 blocks per minute. The coordinator ordered the walk stopped and the file finalized. Blocks 225600000..225602615 (2616 heights, 1 skipped: 225601519, fetch failed) were walked for these rows; the walker had fetched further (to 225603556) but those later rows are not used.

## Bad side (34 rows)
- 26 rows: HAPI registry (incident=hapi, adjudication=registry). Source: shieldbot-handoff/siyab-2026-11/holdout-private-hapi-2026-11-21.json key "final" (proxy.hapiprotocol.near, deleted). Kept accounts whose final category is Scam or Theft (19 Scam + 7 Theft = 26; 1 "None" skipped). source_url https://nearblocks.io/txns/<tx_hash>. 6 of these also appear in the contributor draft; the draft's source and note are appended to the note.
- 8 rows from the contributor draft siyab-pr9/eval/holdout/receivers.jsonl (128 rows: 100 good dropped; 28 bad, of which 14 Rhea Finance rows dropped, 14 kept, 6 of those duplicate HAPI rows):
  - 5 Tether blacklist rows (adjudication=issuer). Each re-confirmed 2026-11-22 by RPC view call get_blacklist_status {"account_id": ...} on usdt.tether-token.near (rpc.mainnet.fastnear.com); all 5 returned "Banned" (blocks 225642546-225642551, recorded in each note). source_url https://nearblocks.io/address/usdt.tether-token.near.
  - 1 rekt.news Skyward Finance row and 2 gov.near.org forum-only rows (adjudication=reported; source_url and note kept from the draft).
- in_label_store=true on 9 bad rows (subjects of siyab-eval-scorer/intel/labels.jsonl; kept and flagged).
- Not confirmed on chain by me: HAPI rows rest on the deleted-registry dump; the tx hashes were not re-fetched. Forum/rekt rows are as reported in the draft, not independently re-read.

## Good side (218 rows, label "good", presumed good, NOT verified clean)
These are unlabelled receivers; they are presumed good only to measure the false-DENY rate. Some may be bad.
Fixed rule (written before drawing): start at mainnet block height 225600000 and walk forward one height at a time (finalized block JSON from https://mainnet.neardata.xyz/v0/block/<h>; chosen over RPC block+chunk calls because chunk calls were throttled); per block take, in shard order then receipt-execution order, every successfully executed receipt whose actions include a native Transfer with deposit > 0, and its receiver_id; first occurrence only; accept if view_account (final, public mainnet RPC rpc.mainnet.fastnear.com / rpc.mainnet.near.org) succeeds, code_hash is all 1s (no deployed code), amount > 0, the id matches ^[a-z0-9_-]+[.]near$ (named .near account, no sub-accounts, no 64-hex implicit accounts; account age is not determinable from RPC, so the named-account fallback applies to every row, none was verified 365 days old) and the id is not in set.json, top.json, labels.jsonl or the bad rows. Stop at 300 (stopped early at 218, see above).
Deviation from brief: walk used neardata blocks (receipt executions), not RPC block/chunk calls.
source_url https://nearblocks.io/address/<account>; note "unlabelled receiver drawn by fixed rule: <rule>, block <height>".
Counts at stop (candidate = distinct receiver first seen up to the 218th good row): about 1,900 candidates; 56 excluded (in set/top/labels/bad), about 1,620 not a named .near id, 3 with deployed code, 218 accepted.

## Exclusions
siyab-eval-scorer/fixtures/receivers/set.json (subjects), top.json (subjects and intents_cohort receivers), intel/labels.jsonl (all subjects) for the good side; bad rows additionally checked against set/top (0 dropped).

## Sources
holdout-private-hapi-2026-11-21.json; siyab-pr9/eval/holdout/receivers.jsonl; siyab-eval-scorer/fixtures/receivers/{set,top}.json; siyab-eval-scorer/intel/labels.jsonl; usdt.tether-token.near view calls; mainnet.neardata.xyz; public mainnet RPC view_account.
