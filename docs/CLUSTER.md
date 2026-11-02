# Account relationship edges

`python -m intel.cluster <seed> ... [--record] [--name rhea]` reads only seeds with a current NEAR incident label. With no positional seeds, it uses the eligible NEAR seeds listed by `intel/incidents/rhea.json`. `--subject <account>` accepts one caller-named account, prints the result, and writes no file. The default mode replays `fixtures/cluster/`; `--record` uses the allowed read endpoints and records their responses there.

The result in `intel/clusters/rhea.json` contains the seed list, `edges`, `facts`, `precision`, a collector-only `depositors` count, the latest fixture time, and the evidence standard. An edge contains its rule, endpoints, provenance, source, confidence, and input fact ids. Chain edges carry method, arguments, and block evidence for both endpoints. Inferred edges carry the input fact ids and a confidence value of 0.5. A fact names its seed subject, reported value, provenance, source, block, and whether the funder was excluded.

## Heuristics and limits

### Shared FullAccess key

For each seed, the command reads `view_access_key_list`, keeps only `FullAccess` keys, and asks FastNEAR for the accounts listed for each key. It intersects that response with the seeds, then checks each candidate with `view_access_key`. The output contains an `onchain` edge only when both reads confirm the same FullAccess key. Function-call keys do not create edges. This records a key present on two accounts at the reported blocks. It does not identify who uses the key.

Rhea precision: `shares_full_access_key: 0 of 21 pairs among the 7 seeds that hold a full access key`.
The denominator is every pair within the seeds that hold at least one FullAccess key, whether or not FastNEAR lists the same key for both.

### Sub account

For a named account with a nested account id, the command reads `view_account` for the seed and its immediate parent. Top-level registrars are excluded. The edge records both account reads and their block heights.

NEAR's account documentation says: “Only the parent can create a sub-account such as `app.bob.near`; after creation, the parent has no control over the sub-account.” [NEAR account ID documentation](https://docs.near.org/protocol/accounts-contracts/account-id).

This edge records the account hierarchy and the two account reads. It does not establish who operates either account.

Rhea precision: `sub_account_of: 3 of 3 seeds with a parent`.

### Creation funder

The command reads each seed's key creation record from NearBlocks, then reads the matching transaction response for its signer and block. A fact records the NearBlocks endpoint and transaction hash as its source. Matching, non-excluded funders create `inferred` edges with the two fact ids as inputs and confidence 0.5. These edges are hypotheses from indexed transaction data; they do not establish that the seed accounts have the same operator or purpose.

The exclusion set covers account factories, wallet creators, and relayers. An excluded funder remains a fact and creates no edge. The Rhea run observed `team.herewallet.near` as the signer in the collector's first-key transaction; it was already listed in `FUNDER_EXCLUSIONS`. No new exclusion was added. The Rhea run found ten other seed accounts with the collector as their first-key transaction signer.

Rhea precision: `created_by_funder: 10 of 14 seeds with a creation record signed by one seed account (NearBlocks facts), 1 seed created through an excluded wallet creator, 3 seeds without a creation record; 45 inferred pairs at confidence 0.5`.

### Collector deposit count

The command requests inbound transfers for the collector from the incident date, follows at most 20 pages, and counts distinct `other_account_id` values. The output names the collector and gives the total and implicit-account count. Depositor identifiers are not written to the cluster result.

Rhea result since 2026-04-16: 140 distinct depositors, 130 implicit accounts. The transfer window was read to its end in 8 of 20 pages; the last page has no `resume_token`. The count does not establish a shared purpose among depositors.

## Evidence and privacy

Every edge is limited to the seed set, with a parent account appearing only as the target of its seed's sub-account edge. The `--subject` form writes no result file. The deposit pattern stores counts and the collector id, never depositor ids. Function-call keys do not count. A failed or missing read produces no edge for that heuristic.

The Rhea incident file lists 16 NEAR accounts. Two entries, `intents.near` and `zec.omft.near`, have no current incident assertion in the label store, so the required refusal rule excludes them from the seed set. The committed run covers the other 14 current incident-labelled NEAR accounts. The separate ETH account is outside this NEAR analysis.
