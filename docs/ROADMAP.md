# Missing features, in the order they should land

Two groups: what can land before the cut line on 12 November, and what waits until after the submission. Every entry states what it is, why it matters, what done means, how it is tested, and what it depends on. No entry may be started before the one above it is merged, because each of the first four changes the numbers a later one reports.

## Before the cut line

### F1. Breadth: the issuer ban sweep

Why: the label store grows by hand today, and a hand grown store cannot look like infrastructure. Every NEP-141 token that exposes a blacklist view already publishes bans on chain, with a block and an answer, which is provenance we do not have to invent.

What: iterate the token catalog, call each blacklist view, and write one assertion per banned account carrying the token id, the block height, the quoted answer and the view that produced it. Provenance class `official_source`. Recorded fixtures for every call, so the sweep replays offline.

Done when: the new assertions validate, the counts block regenerates, a replay test rejudges the recorded set to the committed files, and no assertion lacks a source.

Depends: RPC only. Size: one day. Risk: a token whose view has a different shape needs its own collector, so cap the sweep at the tokens with a known interface and record the rest as unknown.

### F2. Exchange deposit addresses

Why: a deposit address is young, dust funded and has one funder, which is the exact profile the tier 2 rules flag. Without this class we will produce false positives on ordinary exchange users, and a false positive costs a customer.

What: a seed list of hot wallets, each with its public source; walk their outgoing transfers; mark an account funded only by one of them as a likely deposit address, with confidence and inputs. Provenance class `inferred`. It never adds a subject to the refusal set, and it may only raise a detail line on a verdict.

Done when: a page shows each hypothesis with its inputs and confidence, a test proves an inferred row cannot deny, and the receiver set is rejudged with the class available.

Depends: FastNEAR transfers and a seed list with a citation per wallet. Size: two days. Risk: the seed list is the whole feature; a wrong wallet poisons the class, so each entry needs a source and a hand check.

### F3. A graph view

Why: this is judged partly on design, and one drawing of a cluster or a flow says more than a table of hops.

What: nodes and edges rendered from the committed cluster and flow files, no script, no external asset. The same data the JSON already holds, with the provenance of each edge shown beside it.

Done when: a page renders a counted number of nodes and edges, a test asserts the counts, and the committed bytes equal a fresh render.

Depends: nothing. Size: one day.

### F4. A documented read API

Why: an intelligence layer that cannot be queried looks like a report. The read endpoints exist; the contract for them does not.

What: one page listing every read endpoint, its fields, its limits, and a worked example per endpoint, each example generated from committed fixtures.

Done when: the page exists, a test fails if an example stops matching its fixture, and the limits are the ones the code enforces rather than a wish.

Depends: the existing read server. Size: one day.

## After the submission

### F5. Data licence inventory

Legal work, not code. One line per source: what it is used for, what its terms allow, and whether a commercial product may rely on it. The explorer free tier is personal use with a daily cap, which blocks a paid contract on its own. Start the conversations before anyone signs a customer.

### F6. Entity attribution beyond deposits

Bridges, routers, staking pools and exchange hot wallets identified from public disclosure. Never persons, never a claim about who controls an account. Each class carries its source and its confidence, and the `inferred` class keeps its current rule: it may inform, it may never refuse.

### F7. A cross chain destination view

A column on the Intents leg naming the destination chain and recipient, taken from the dry quote. This is not bridge tracing and must never be described as tracing. Bridge level work needs licensed data and months.

### F8. A public API and an SDK

Keys, quotas, versioning, a deprecation policy and an on call rota. The read surface is a week; the product surface is a quarter, and it is the difference between a demo and something an exchange integrates.

### F9. The measured cohort

Pre-registered precision on accounts the store never held, with the rule committed before the run. This is the feature that makes every other claim credible, and it is worth more than any of F3 to F8.

## Out of scope, now and later

Person level attribution. A token. Scraping a vendor. Bridge level tracing. Any claim of a TEE.

## How to take one of these

One branch per feature. Write the test before the collector. Add the claims row in the same commit as the behaviour. A feature that changes the counts block updates `docs/CLAIMS.md` and the README copy in the same pull request, because a number in two places is a number that will disagree.
