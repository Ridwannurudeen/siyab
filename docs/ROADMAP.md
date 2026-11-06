# Missing features, in the order they should land

Two groups: what can land before the cut line on 12 November, and what waits until after the submission. Every entry states what it is, why it matters, what done means and what it depends on; the four before the cut line also state how each is tested. No entry may be started before the one above it is merged, because each of the first four changes the numbers a later one reports.

## Before the cut line

### F1. Breadth: the issuer ban sweep

Why: the label store grows by hand today, and a hand grown store cannot look like infrastructure. Every NEP-141 token that exposes a blacklist view already publishes bans on chain, with a block and an answer, which is provenance we do not have to invent.

What: iterate the token catalog, call each blacklist view, and write one assertion per banned account carrying the token id, the quoted answer and the view that produced it. Provenance class `onchain`, whose source shape is the one `docs/LABEL-RECORD.md` gives it: the RPC, the method, the arguments and the block height the read was made at. Recorded fixtures for every call, so the sweep replays offline.

Done when: the new assertions validate, the counts block regenerates, a replay test rejudges the recorded set to the committed files, and no assertion lacks a source.

Depends: RPC only. Size: one day. Risk: a token whose view has a different shape needs its own collector, so cap the sweep at the tokens with a known interface and record the rest as unknown.

### F2. Exchange deposit addresses

Why: a deposit address is often young and created by a small deposit, and that is the pair the tier 2 rules flag: a dust creation deposit counts as a signal only together with a young account or a lookalike (`gate/rules.py`), so a young, dust funded address trips both. The rules do not read funder count, so this class adds the reason those addresses exist rather than repeating a match already made. Without it we will produce false positives on ordinary exchange users, and a false positive costs a customer.

What: a seed list of hot wallets, each with its public source; walk their outgoing transfers; mark an account funded only by one of them as a likely deposit address, with confidence and inputs. Provenance class `inferred`. It never adds a subject to the refusal set, and it may only raise a detail line on a verdict.

Done when: a page shows each hypothesis with its inputs and confidence, a test proves an inferred row cannot deny, and the receiver set is rejudged with the class available.

Depends: FastNEAR transfers and a seed list with a citation per wallet. Size: two days. Risk: the seed list is the whole feature; a wrong wallet poisons the class, so each entry needs a source and a hand check.

### F3. A graph view

Why: one drawing of a cluster or a flow says more than a table of hops, and an edge's provenance can be read beside it instead of two files apart.

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

Done when: every source the store and the rules read has a line quoting the clause it rests on, and each question a paid contract would turn on has a written answer rather than a to do.

Depends: the full list of sources behind the seeds and the rules, and time with a lawyer. Size: two days plus their reply.

### F6. Entity attribution beyond deposits

Bridges, routers, staking pools and exchange hot wallets identified from public disclosure. Never persons, never a claim about who controls an account. Each class carries its source and its confidence, and the `inferred` class keeps its current rule: it may inform, it may never refuse.

Done when: each new class has a published source, a row in the label store with that source and a confidence, and a test that an `inferred` row of the class cannot deny.

Depends: one public disclosure page per class, checked by hand. Size: half a day per class.

### F7. A cross chain destination view

A column on the Intents leg naming the destination chain and recipient, taken from the dry quote. This is not bridge tracing and must never be described as tracing. Bridge level work needs licensed data and months.

Done when: every Intents judgement carries those two columns, a test pins a recorded dry quote to them, and no copy calls the column tracing.

Depends: the dry quote endpoint and the recorded quotes already in `fixtures/intents/`. Size: one day.

### F8. A public API and an SDK

Keys, quotas, versioning, a deprecation policy and an on call rota. The read surface is a week; the product surface is a quarter, and it is the difference between a demo and something an exchange integrates.

Done when: a key can be issued and revoked, a quota is enforced and reported to its holder, a version is deprecated on a published timetable, and each week of the rota names someone.

Depends: F4's documented read surface, and an error contract a client can code against. Size: a week for the read surface, a quarter for the product surface.

### F9. The measured cohort

Pre-registered precision on accounts the store never held, with the rule committed before the run. This is the feature that makes every other claim credible, and it is worth more than any of F3 to F8.

Done when: the rule, the selection and the metric are committed before the run, the run's inputs are readable after it, and the result gives the precision with its interval and names every miss.

Depends: a cohort drawn from accounts the store never held, and a reviewer who signs the rule before the numbers exist. Size: three days plus their review.

## Out of scope, now and later

Person level attribution. A token. Scraping a vendor. Bridge level tracing. Any claim of a TEE.

## How to take one of these

One branch per feature. Write the test before the collector. Add the claims row in the same commit as the behaviour. A feature that changes the counts block updates `docs/CLAIMS.md` and the README copy in the same pull request, because a number in two places is a number that will disagree.
