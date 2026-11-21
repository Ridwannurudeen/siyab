# Rules version 3

21 Nov 2026, fix round 3. Branch `feat/rules-v3`, based on `e2d15123d`. The existing branch was kept in place. Nothing was committed, pushed or deployed.

## Rule and selection

`gate.rules.RULES_VERSIONS` is `(1, 2, 3)`. Select version 3 by passing `rules_version=3` to `gate.rules.judge`, `gate.verdict.request` or `gate.account_check.check_account`, with their existing arguments. The offline replay command below also accepts a version. Version 1 remains the default everywhere. No payment CLI, MCP tool, HTTP route or deployment was switched.

Version 3 runs exactly the version 1 checks. It does not run the `gate/freshness.py` checks of version 2, and it retains the version 1 metadata check for global contracts. Only the final decision changes: if the complete set of signal check ids is exactly `{"account_age"}`, return UNKNOWN instead of DENY. The account_age check stays signal, with the same detail and source.

Every other signal still denies. Account age together with dust_funded, lookalike, code_without_metadata or any tier 1 signal still denies. An account at the age cutoff follows the existing pass rule. ALLOW is unchanged. UNKNOWN is never signed by the verdict service, and the Gate requires a signed ALLOW.

The additional UNKNOWN reason at the default threshold is:

> RECEIVER is younger than 7 days; no other signal was found among the checks that ran. The owner should confirm the receiver before paying.

RECEIVER is replaced with the receiver id. An explicit `min_age_days` changes the number in this reason. Existing reasons for unknown checks follow it unchanged. For example, an unknown creation deposit also adds:

> dust_funded could not run: the creation transaction is not known.

Version 3 UNKNOWN reasons also keep "Not on the owner's allowlist." when tier 2 ran. Version 1 and 2 reasons are unchanged. In particular, an unavailable dust_funded or metadata check stays unknown; the confirmation reason makes no claim about a check that could not run.

## Offline impact

All output went into temporary directories. Network entry points were refused. A row is not replayed when a required original recording or input is unavailable. A successful fallback to a recorded response counts as replayed; an unsuccessful probe alone does not make the final proof incomplete. Stored verdicts were never used to guess missing reads.

Changes below count only fully replayed inputs. Rows can overlap between sets, so the total is not a count of distinct accounts. The broader inventory is from the prior round. This round reproduces both complete receiver sets with the new command and leaves the other recordings in place.

| Committed set | Total | Replayed | Not replayed | DENY to UNKNOWN |
|---|---:|---:|---:|---:|
| Ordinary receivers | 84 | 84 | 0 | 0 |
| Top receivers | 990 | 990 | 0 | 137 |
| Rhea rules_today | 16 | 6 | 10 | 0 |
| SWEAT/HOT rules_today | 1 | 0 | 1 | 0 |
| Historical Rhea | 7 | 7 | 0 | 1 |
| Recorded transfer evidence | 12 | 1 | 11 | 0 |
| Total | 1110 | 1088 | 22 | 138 |

Ordinary receivers remain 70 ALLOW and 14 DENY. Top receivers change from 850 ALLOW, 138 DENY and 2 UNKNOWN to 850 ALLOW, 1 DENY and 139 UNKNOWN. Every changed top receiver has account_age as its only signal and retains an unknown dust_funded reason. `whxrc1mu0gob.near` also retains an unknown code_without_metadata reason.

The historical change is the collector `31ac7a27...e724a540`, located by its full account id in [accounts[0] of the historical Rhea run](../../fixtures/replay/rhea/run.json). Its only signal is account_age. Under version 3 its verdict is UNKNOWN with the confirmation and unknown dust reasons. The corrected shared reducer maps that verdict to ASK. Default replay still uses version 1 and the saved run was not rewritten.

### Reproducible receiver replay

From the repository root, use a different new destination for each run:

```
python -m intel replay-receivers --set ordinary --rules-version 1 --out ../siyab-rules-v3-replay-20261121/ordinary-v1
python -m intel replay-receivers --set ordinary --rules-version 3 --out ../siyab-rules-v3-replay-20261121/ordinary-v3
python -m intel replay-receivers --set top --rules-version 1 --out ../siyab-rules-v3-replay-20261121/top-v1
python -m intel replay-receivers --set top --rules-version 3 --out ../siyab-rules-v3-replay-20261121/top-v3
```

The selector is `ordinary` or `top`; its input is respectively `fixtures/receivers/set.json` or `fixtures/receivers/top.json`. The command runs the existing receiver replay at the set's recorded judged_at_ms. Top replay keeps its recorded judged block and pinned age fallback. It passes the selected version through both the first judgement and any pinned retry.

Every RPC and NearBlocks client is explicitly in replay mode, even if GATE_FIXTURES_MODE=off. No recording option, provider key, reports account, live read, signature or payment is used. Missing responses retain the rule's unknown checks. The command refuses an existing output directory and any resolved directory inside the repository. It writes `judgements/<subject>.json` and `summary.json` only into the chosen new directory. An error returns nonzero without a completed summary.

The four commands print these summaries, also saved as summary.json:

```json
{"changed_verdicts": 0, "judged": 84, "rules_version": 1, "set": "ordinary", "transitions": {}, "verdicts": {"ALLOW": 70, "DENY": 14, "UNKNOWN": 0}}
{"changed_verdicts": 0, "judged": 84, "rules_version": 3, "set": "ordinary", "transitions": {}, "verdicts": {"ALLOW": 70, "DENY": 14, "UNKNOWN": 0}}
{"changed_verdicts": 0, "judged": 990, "rules_version": 1, "set": "top", "transitions": {}, "verdicts": {"ALLOW": 850, "DENY": 138, "UNKNOWN": 2}}
{"changed_verdicts": 137, "judged": 990, "rules_version": 3, "set": "top", "transitions": {"DENY to UNKNOWN": 137}, "verdicts": {"ALLOW": 850, "DENY": 1, "UNKNOWN": 139}}
```

Transitions compare verdicts with the committed version 1 judgements. They do not count reason changes. Top version 3 changes 139 judgement files: 137 verdict changes plus two UNKNOWN reason-only changes, `aisthetic.near` and `snirk83.near`. Those two rows gain the version 3 allowlist reason without changing verdict. All 137 changed verdicts retain exactly the original checks and account_age signal. Version 1 replay produces every one of the 84 ordinary and 990 top judgement files byte for byte. The four verification directories from the first round remain at `../siyab-rules-v3-replay-20261121/`. Automatic approval review rejected their cleanup command with "blocked by policy" and supplied no further reason. No retry or alternative deletion was attempted.

The ten Rhea incident rows that lack required recordings are zero based `rules_today` indices 1, 7, 8, 9, 10, 11, 12, 13, 14 and 15 in `intel/incidents/rhea.json`. Missing responses cover age keys, four storage registrations and two token metadata reads. SWEAT/HOT lacks its receiver age key recording. Their deterministic fail closed reconstructions still match all 17 saved rows, but incomplete inputs are excluded from the impact count.

The additional offline demo has three NEAR inputs: one replayed, two not replayed, zero changes among replayed inputs. The lookalike lacks archival block recording `40c13d35*.json`, and the aged payee lacks `2a506f44*.json`. The fourth demo input uses the independent EVM rules and does not call `gate.rules.judge`.

Both model recording folders have 30 trials and zero payment calls, so they provide zero judgement inputs. Twelve evidence files with no checks are canonical vectors, not recorded receiver judgements. Registry samples and mocked receipts are verification fixtures. The saved MCP payment judgement duplicates evidence `4b99f7ed*.json` and is not counted twice.

### Every changed top receiver

The index identifies `subjects[index]` in [the committed top ranking](../../fixtures/receivers/top.json). All 137 rows below were matched against the actual replay change list.

| Receiver | subjects index | Signals |
|---|---:|---|
| `01902257.near` | 303 | `account_age` |
| `0xarise.near` | 470 | `account_age` |
| `0xbiar.near` | 93 | `account_age` |
| `1216429489.near` | 284 | `account_age` |
| `22331a.near` | 41 | `account_age` |
| `2269011371.near` | 472 | `account_age` |
| `3135112344.near` | 266 | `account_age` |
| `6079956527.near` | 79 | `account_age` |
| `62803580.near` | 309 | `account_age` |
| `64521467.near` | 310 | `account_age` |
| `666889.near` | 53 | `account_age` |
| `7528421923.near` | 285 | `account_age` |
| `7747641265.tg` | 918 | `account_age` |
| `77fd6e4332aab6ff.trade.unrlzd.near` | 402 | `account_age` |
| `7943205968.near` | 318 | `account_age` |
| `82097030.near` | 286 | `account_age` |
| `9145681774.tg` | 177 | `account_age` |
| `agent.bakeoff.near` | 201 | `account_age` |
| `agentverse-survival.near` | 484 | `account_age` |
| `ainear.nearlyops.near` | 741 | `account_age` |
| `akst44.near` | 486 | `account_age` |
| `alexchad.v2-launchpad.justhoot.near` | 62 | `account_age` |
| `andmaybejustmaybe.near` | 256 | `account_age` |
| `asmwsmm-hot.tg` | 490 | `account_age` |
| `bakeoff.near` | 407 | `account_age` |
| `believeinmiracle.near` | 494 | `account_age` |
| `betman.nearlytrade.near` | 34 | `account_age` |
| `bucks05.near` | 501 | `account_age` |
| `buy-and-burn.meta-pool-dao.near` | 250 | `account_age` |
| `cyberpunkdeer.near` | 413 | `account_age` |
| `dawidb.near` | 206 | `account_age` |
| `deepkingdom2311.near` | 131 | `account_age` |
| `demo.bountee.near` | 513 | `account_age` |
| `dharmapal.near` | 517 | `account_age` |
| `dvzat.sputnik-dao.near` | 240 | `account_age` |
| `eliasvault.near` | 523 | `account_age` |
| `elvirad.near` | 417 | `account_age` |
| `emberwick.near` | 268 | `account_age` |
| `emmanuelhaanks.near` | 418 | `account_age` |
| `encrypteth.nearlytrade.near` | 33 | `account_age` |
| `ethcfr-hot.tg` | 207 | `account_age` |
| `fairblog5693.near` | 161 | `account_age` |
| `farlynx3777.near` | 243 | `account_age` |
| `farstreet4572.near` | 234 | `account_age` |
| `froog.near` | 288 | `account_age` |
| `fullfiller1.near` | 532 | `account_age` |
| `funnysite1561.near` | 57 | `account_age` |
| `greenmuseum9245.near` | 235 | `account_age` |
| `grt8sns.near` | 534 | `account_age` |
| `hamad101.near` | 715 | `account_age` |
| `harry190-hot5.tg` | 424 | `account_age` |
| `harry190-hot6.tg` | 425 | `account_age` |
| `holyshit.nearlytrade.near` | 118 | `account_age` |
| `hugezest930.near` | 237 | `account_age` |
| `imeme-2.nearlytrade.near` | 549 | `account_age` |
| `imeme-3.nearlytrade.near` | 550 | `account_age` |
| `imeme.nearlytrade.near` | 54 | `account_age` |
| `kent-user1.near` | 560 | `account_age` |
| `keysop.nearlyops.near` | 675 | `account_age` |
| `kite-demo.sputnik-dao.near` | 83 | `account_age` |
| `leftflour7482.near` | 687 | `account_age` |
| `littlegif5805.near` | 566 | `account_age` |
| `lmp1996-hot1.tg` | 567 | `account_age` |
| `luckyharbor2137.near` | 186 | `account_age` |
| `maincpu.near` | 437 | `account_age` |
| `majorjuice6994.near` | 125 | `account_age` |
| `maksimissajev.near` | 573 | `account_age` |
| `mallace.v2-launchpad.justhoot.near` | 574 | `account_age` |
| `manyegg4146.near` | 360 | `account_age` |
| `markek01.tg` | 109 | `account_age` |
| `marynew.near` | 577 | `account_age` |
| `moderndish1947.near` | 216 | `account_age` |
| `musas101-hot.tg` | 29 | `account_age` |
| `nbc.v2-launchpad.justhoot.near` | 587 | `account_age` |
| `nearaddict.near` | 46 | `account_age` |
| `nearbearcoin.near` | 156 | `account_age` |
| `nearcarta.near` | 295 | `account_age` |
| `nearfullfil.near` | 149 | `account_age` |
| `nearhunt.near` | 589 | `account_age` |
| `nearon-stg.near` | 264 | `account_age` |
| `nemesisnom.near` | 591 | `account_age` |
| `noa.v2-launchpad.justhoot.near` | 27 | `account_age` |
| `noamarketdev.near` | 217 | `account_age` |
| `nooneapp.near` | 370 | `account_age` |
| `normalherb1811.near` | 233 | `account_age` |
| `orangemonkey.near` | 446 | `account_age` |
| `orangemonkey77.near` | 147 | `account_age` |
| `oyster.v2-launchpad.justhoot.near` | 47 | `account_age` |
| `pearlfather.near` | 801 | `account_age` |
| `pearlmilkt.v2-launchpad.justhoot.near` | 600 | `account_age` |
| `pearlquant.near` | 672 | `account_age` |
| `pripol.sputnik-dao.near` | 297 | `account_age` |
| `proven-board.near` | 453 | `account_age` |
| `proven-ops.near` | 110 | `account_age` |
| `proven-relay.near` | 255 | `account_age` |
| `r2bib.tg` | 454 | `account_age` |
| `richgogogo.near` | 458 | `account_age` |
| `robinhoot-2.v2-launchpad.justhoot.near` | 21 | `account_age` |
| `rstowner09.tg` | 620 | `account_age` |
| `sadcap6639.near` | 282 | `account_age` |
| `santinodarie.near` | 272 | `account_age` |
| `sharpicon3186.near` | 55 | `account_age` |
| `skateboardheem.near` | 157 | `account_age` |
| `soliddress8221.near` | 134 | `account_age` |
| `soonibis5403.near` | 633 | `account_age` |
| `sparefile9404.near` | 241 | `account_age` |
| `sparequokka2874.near` | 634 | `account_age` |
| `stw21b26f81abd.near` | 38 | `account_age` |
| `stw2d96e22c69e.near` | 66 | `account_age` |
| `sv19916.tg` | 385 | `account_age` |
| `thinos1181.near` | 98 | `account_age` |
| `townhall.nearcarta.near` | 777 | `account_age` |
| `trader.slmm.near` | 144 | `account_age` |
| `u-cexdex.near` | 129 | `account_age` |
| `und.v2-launchpad.justhoot.near` | 26 | `account_age` |
| `unhuntable.near` | 646 | `account_age` |
| `uponwave.near` | 648 | `account_age` |
| `vault-1-r.sputnik-dao.near` | 778 | `account_age` |
| `vertumion-hot2.tg` | 655 | `account_age` |
| `vertumion-hot3.tg` | 656 | `account_age` |
| `viraz.near` | 657 | `account_age` |
| `w3xsandbox.sputnik-dao.near` | 247 | `account_age` |
| `w4probeacct.near` | 128 | `account_age` |
| `w4spon.sputnik-dao.near` | 466 | `account_age` |
| `w4spon2.sputnik-dao.near` | 261 | `account_age` |
| `w5vgdvsv.sputnik-dao.near` | 236 | `account_age` |
| `w5xsmuhy.sputnik-dao.near` | 265 | `account_age` |
| `wazirseerat123.near` | 660 | `account_age` |
| `weirdglove364.near` | 56 | `account_age` |
| `wgmit.near` | 661 | `account_age` |
| `whiteram300.near` | 663 | `account_age` |
| `whxrc1mu0gob.near` | 141 | `account_age` |
| `wongteckzwee.near` | 665 | `account_age` |
| `worker-v2.arras-escrow.near` | 666 | `account_age` |
| `xcdgp.sputnik-dao.near` | 468 | `account_age` |
| `xgyzg.sputnik-dao.near` | 699 | `account_age` |
| `xtrmapl.near` | 115 | `account_age` |

### Recorded transfer evidence

Prefixes identify files under `fixtures/evidence/`. The historical collector refers to the exact account located above.

| Prefix | Receiver | Replay result or missing input |
|---|---|---|
| `4b99f7ed` | `kestrelridge-shop.testnet` | Original receiver, metadata and storage response timestamps unavailable |
| `5371ec35` | Historical collector | Exact original context replayed; DENY in both versions |
| `5cd25837` | `kestrelridge-shop.testnet` | Original timestamps unavailable and report response missing |
| `7e77187d` | `kestrelridge-shop.testnet` | Original receiver, metadata and storage response timestamps unavailable |
| `9471fea5` | `examples.testnet` | Receiver, storage, age and creation recordings missing; metadata timestamp and memo unavailable |
| `ac655898` | Historical collector | Receiver, issuer and age recordings missing |
| `b7805115` | `maryamnurudeen.near` | Receiver and issuer recordings missing |
| `c5d02152` | `kestrelridge-sh0p.testnet` | Original receiver, token, age and storage response timestamps unavailable |
| `cfca1a63` | `kestrelridge-shop.testnet` | Original receiver, metadata and storage response timestamps unavailable |
| `deb96dd6` | `kestrelridge-shop.testnet` | Original timestamps unavailable; report response and memo missing |
| `ea8462ba` | `kestrelridge-shop.testnet` | Original timestamps unavailable; report response and memo missing |
| `ff5a89a2` | `lindenvale.testnet` | Original age recording missing and other original timestamps unavailable |

For `5371ec35`, the original label store was recovered read only from local commit `93785db2:intel/labels.jsonl` and checked against the recorded store hash. The request and policy came from `gate.injected`, with time `1793868830241`. Both versions reproduced verdict, checks, reasons, labels and observed, and the canonical evidence bytes using its recorded metadata. The temporary label file was removed. No signing was needed.

### Replay contexts and preservation proof

Ordinary and top judgements use `intel.__main__.judge_receivers`, the committed store and recorded clients. Ordinary time is `1793731688201`; top time is `1794197345742`. Top replay uses `pin_unknown_age=True` and the saved `fixtures/receivers/top/judged-block.json`. The new command supplies rules_version to judge_receivers and its pinned retry directly.

Incident rows used their saved requests, empty allowlist and caps, the committed store, mainnet replay RPC and NearBlocks clients, mainnet issuers, and no archival client, reports account or scanner. Rhea time is `1793274062000`; SWEAT/HOT time is `1793287749000`.

Historical replay used `gate.incident_replay._FixtureClient` over `fixtures/replay/rhea/rpc`, the saved fixture manifest, and `_evaluate` with target nanoseconds `1776297600000000000`. The baseline passed the recorded run; version 3 passed `recorded=None` so the intentional verdict difference was not rejected. The evaluator resolves the historical block, pins reads, uses an empty label store and excludes NearBlocks.

The unchanged default reconstruction proved:

* All 84 ordinary files and all 990 top files byte identical.
* All 17 incident rules_today dictionaries identical.
* The entire historical Rhea run byte identical.
* The reconstructed historical transfer evidence above byte identical.
* The unchanged `scripts.smoke.receivers(temp)` step 9 passed separately.

Before and after fingerprints also matched for all 9,825 fixture files, README and all 912 bytes printed by `python scripts/counts.py`. No committed recording or count was rewritten.

## Consumer review

These are the complete judgement consumer paths reviewed through fix round 3. The named functions identify their current call sites.

| Consumer | Result and exact change |
|---|---|
| `gate/rules.py:_reasons` and `gate/rules.py:judge` | Version 3 alone reduces the full account_age only signal set to UNKNOWN. The confirmation reason refers only to checks that ran and version 3 tier 2 UNKNOWN keeps the allowlist reason. Versions 1 and 2 keep their checks, verdicts and reasons. |
| `intel/live.py:_verdict_shape` | Fixed. DENY or any signal produces DENY, except UNKNOWN with exactly account_age as its signal set, which produces ASK. Every other input retains the base mapping, including inconsistent ALLOW with signal and unrecognised verdict strings. |
| `gate/verify.py:verify_receipt` | Fixed through the shared live reducer, with no edit to this file. A canonical age confirmation receipt with outcome ASK and action ask verifies. UNKNOWN with a label_denylist signal and an ASK answer is rejected. Changed outcome, action, reasons or canonical evidence still fails. ALLOW with signal retains the base DENY mapping. |
| `gate/account_check.py:check_account` | Fixed. After appending contract and incident checks, preserve the exception only for rules_version 3, an original UNKNOWN judgement and account_age as the entire combined signal set. Reuse the confirmation reasons and append extra unknown reasons. Any extra incident or control signal still denies. |
| `gate/verdict.py:ask_reasons` and `gate/verdict.py:request` | Fixed. request accepts rules_version=1 by default and forwards it. For UNKNOWN with an age signal it prepends the judgement's confirmation reason to the existing missing check and policy questions. It never signs UNKNOWN. |
| `gate/agent.py:_print_owner_step` and `gate/agent.py:pay` | Fixed. The UNKNOWN branch prints each owner question, starting with the young receiver confirmation and followed by actual missing checks. The existing owner step and no payment behavior remain. Other UNKNOWN output is unchanged. |
| `gate/demo.py:_run` | Fixed. This UNKNOWN shape prints the propagated owner questions instead of an empty could not run cause. Other output is unchanged. |
| `mcp/server.py:handle` | Fixed. ASK with an account_age signal has the headline New account, confirm first. All other ASK headlines retain Not enough data. Replay disclosure and the structured result remain. |
| `intel/delivery_checks.py:reply` | Fixed. The Telegram headline uses the same semantic distinction, keeping reasons, evidence, sources and the unsigned notice. No Telegram message was sent. |
| `gate/incident_replay.py:_evaluate` | Fixed through the shared live reducer, with no edit to this file. Version 3 replay now records UNKNOWN with outcome ASK. Default replay remains version 1 and its committed recordings are unchanged. |
| `intel/__main__.py:judge_receivers`, `intel/__main__.py:_pinned_judgement` and `intel/__main__.py:_replay_receivers` | Added explicit version forwarding to ordinary and pinned replay, and the offline replay-receivers command. Existing replay entry points still default to version 1. |
| `intel/__main__.py:top_receivers_coverage_line` | Fixed. UNKNOWN with an account_age signal adds the young_receiver_confirmation cause alongside any actual unavailable checks, instead of unspecified when every other check passed. Version 1 and 2 coverage strings are unchanged. |
| `intel/render.py:_top_receivers_summary` | Fixed in round 3. Keep counting checks with unknown status, then add one `young_receiver_confirmation` cause for an UNKNOWN document that has an account_age signal. The signal stays signal; an unavailable account_age read does not count as confirmed young. |
| `intel/render.py:top_receivers_page` | Fixed in round 3. Render the new cause as New account, confirm first. Every other label and the overlapping counts notice stay unchanged. An age only UNKNOWN row no longer displays None recorded. |
| `scripts/scorecard.py:receiver_rows` | Fixed in round 3. Count the same young_receiver_confirmation cause alongside actual unavailable checks, without reclassifying an unavailable account_age read. |
| `intel/render.py:incident_page` | Regression added in round 3 through the existing shared reducer. An UNKNOWN incident row with an account_age signal and outcome ASK renders WARN and preserves the detail. Changing only its outcome to DENY fails the existing mismatch check. |
| `scripts/ask_eval.py:from_document` | Regression added in round 3 through the existing shared reducer, with no source edit. A real version 3 receiver replay produces ASK in both verdict and projection, with the original account_age signal and exact confirmation reason. Committed corpus files and counts stay unchanged. |

Compatible consumers were checked too. `gate/evidence.py` accepts and canonically hashes UNKNOWN with signal checks. `gate/canon.py` constructs only ALLOW messages. The verdict service signs only a literal ALLOW judgement. The registry client accepts unsigned UNKNOWN records, and registry verification does not assume UNKNOWN has no signal. The Gate refuses absent verdicts and non ALLOW literals; the registry contract does not treat Unknown as allow.

The browser check renderers accept ASK with signal checks and show WARN. Ask preserves check statuses and supplied reasons. Token, transaction and intents reducers do not consume version 3 judge results; they reduce their own independent checks. Existing application callers still use version 1.

The render aggregation, scorecard aggregation and both consumer regressions are implemented. Incident rendering and explanation projection continue to use the existing shared reducer.

The UNKNOWN branch in `intel/render.py:_top_receivers_summary` is:

```python
        if document["verdict"] == "UNKNOWN":
            unknown_causes.update(check["id"] for check in document["checks"] if check["status"] == "unknown")
            if any(check["id"] == "account_age" and check["status"] == "signal" for check in document["checks"]):
                unknown_causes["young_receiver_confirmation"] += 1
```

The causes expression in `intel/render.py:top_receivers_page` is:

```python
    causes = "".join(f"<li>{esc('New account, confirm first' if cause == 'young_receiver_confirmation' else cause)}: {count}</li>"
                      for cause, count in sorted(summary["unknown_causes"].items())) or "<li>None recorded.</li>"
```

The render regression uses one age only UNKNOWN row and one age plus unavailable dust UNKNOWN row. It requires two confirmation causes, one dust cause and no None recorded. Separate render and scorecard cases prove that an unavailable age read is not a confirmed young account. Incident and explanation regressions exercise the shared reducer as described above. Complete default and indexable renders are byte identical before and after this round, and every default file matches the committed site.

## Tests from fix round 1

The scoped importer run covered 54 complete modules and three nonrecursive smoke tests. It ran 1,875 cases in 1007.123 seconds: 1,868 passed, 6 skipped and the one source guard failure described below. That failure was fixed during the run, then the affected module was rerun alone: 27 passed in 10.897 seconds. The final coverage helper addition was verified with 36 passing tests. The current scoped inventory is 1,876 cases after that additional regression, with 1,870 passing and 6 skipped using the final successful module results. There was no second combined run of all 1,876 cases.

The six scoped skips are four py-near transport checks and two PyNaCl cross checks, whose optional packages are not installed. The two additional downstream modules test_ask_eval and test_incident_replay_page ran 54 cases: 53 passed and one skipped because launch records have not been selected. Those are reported separately and overlap other imported test helpers.

New regressions were written before the fixes. The consumer tests reproduced account DENY, missing verdict version selection, empty CLI causes, the incorrect live DENY mapping and missing young headlines. Five reason tests then failed on the unqualified claim or missing allowlist sentence. The seven replay tests first ran with six errors because the new command did not exist. The option refusal test passed. An additional replay expectation was corrected after it exposed the intentional allowlist reason change on the two preexisting UNKNOWN rows.

The new regression modules are:

| Module | Tests | Coverage |
|---|---:|---|
| tests.test_rules_v3_account_cli | 11 | Actual version 3 judgement through account checks, verdict owner questions, agent and demo; missing check ordering; no signature or payment; additional signal DENY; default and version 2 preservation |
| tests.test_rules_v3_live | 6 | Actual version 3 judgement through live shape and receipt verification; mismatched fields and evidence rejected; age plus unavailable check; historical evaluator outcome and young detail preserved; coverage summary names confirmation; versions 1 and 2 unchanged |
| tests.test_rules_v3_messages | 5 | Actual version 3 account check through MCP and Telegram formatting; young headline, reasons, replay disclosure and unsigned notice; every other ASK and versions 1 and 2 unchanged |
| tests.test_replay_receivers | 7 | Both full sets and both selected versions; byte identical version 1 files; exact transitions and unchanged checks; live environment ignored; source tree and existing destinations refused; unsupported recording and version refused |

Focused final runs already completed:

```
python -m unittest tests.test_rules_v3_account_cli tests.test_rules -q
python -m unittest tests.test_account_check tests.test_verdict tests.test_agent tests.test_demo -q
python -m unittest tests.test_rules_v3_messages tests.test_mcp tests.test_delivery_checks -q
python -m unittest tests.test_api_doc tests.test_replay_receivers -q
python -m unittest tests.test_ask_eval tests.test_incident_replay_page -q
python -m unittest tests.test_rules_v3_live tests.test_top_receivers -q
```

These ran respectively 226 tests with 226 passed, 149 with 144 passed and 5 skipped, 50 with 49 passed and 1 skipped, 34 with 34 passed, 54 with 53 passed and 1 skipped, and 36 with 36 passed. The individual live API, receipt and historical replay modules ran 21, 62 and 59 tests, all passed. The strengthened incident detail assertion was checked again in the five test live consumer module, all passed, before its sixth coverage test was added. These focused runs overlap and are not added as distinct coverage.

The coverage helper regression first failed alone with unspecified instead of young_receiver_confirmation. The final 36 test live and top receiver run checks its fix and the existing version 1 summary.

The new command initially tripped the source guard at tests.test_api_doc.CheckTableTest.test_the_rules_version_2_checks_stay_out_of_the_table_while_no_caller_runs_version_2. The module was rerun alone and reproduced the failure. Forwarding now binds the validated CLI selection to rules_version before passing it, keeping the existing guard and the default of 1. The final 34 test API and replay run above passed.

The historical replay module initially encountered another process's publication lock in the shared system temporary directory. Its isolated rerun still saw a sibling recording directory. With a process private temporary root, all 59 tests passed. No shared directory was deleted or edited. The integrated run uses a private temporary root and sets TEMP, TMP and TMPDIR for its subprocesses.

The required replay and count modules contain exactly 164 tests: test_receivers 16, test_top_receivers 30, test_top_receiver_recording 19, test_incidents 22, test_demo 9, test_injected_model 42, test_record_latency 12 and test_counts 14. The updated test_rules module contains 215 tests. These all belong to the scoped importer run rather than the full repository suite.

The complete module inventory below is the direct importer set for the changed source files, plus gate.verify and gate.incident_replay consumers and the explicitly requested replay modules. All names have the `tests.` prefix. Each module's non skipped cases passed on its final run.

| Module | Cases | Module | Cases |
|---|---:|---|---:|
| test_account_check | 39 | test_agent | 52 |
| test_alerts | 24 | test_alerts_telegram | 22 |
| test_analytics_api | 12 | test_api_doc | 27 |
| test_ask | 92 | test_ask_api | 31 |
| test_ask_validation | 15 | test_asof_errors | 84 |
| test_budget | 21 | test_counts | 14 |
| test_delivery_checks | 9 | test_demo | 9 |
| test_feed | 79 | test_fuzz | 12 |
| test_http | 27 | test_incident_replay | 59 |
| test_incidents | 22 | test_injected | 6 |
| test_injected_model | 42 | test_intents | 119 |
| test_investigation_jobs | 53 | test_labels | 53 |
| test_live_accounts | 3 | test_live_api | 21 |
| test_live_history | 14 | test_live_limits | 32 |
| test_mcp | 36 | test_nearblocks_parity | 13 |
| test_receivers | 16 | test_record_check_supersedes | 1 |
| test_record_latency | 12 | test_records | 45 |
| test_render | 84 | test_replay_receivers | 7 |
| test_reports_pending_counts | 5 | test_reports_rules | 15 |
| test_rpc | 79 | test_rules | 215 |
| test_rules_v3_account_cli | 11 | test_rules_v3_live | 6 |
| test_rules_v3_messages | 5 | test_sanctions | 20 |
| test_snapshot | 17 | test_spec | 17 |
| test_token_check | 53 | test_top_receiver_recording | 19 |
| test_top_receivers | 30 | test_trace | 29 |
| test_verdict | 49 | test_verify | 17 |
| test_verify_receipt | 62 | test_workspace_cli | 17 |

The three smoke cases were test_mcp_step_accepts_the_listed_tools_with_checks_disabled, test_a_failing_step_prints_fail_and_exits_1 and test_every_step_runs_in_replay_from_the_smoke_temporary_directory.

The combined test wrapper removed its private temporary parent before the latency command's registered atexit cleanup callbacks ran. This produced shutdown FileNotFoundError warnings for already removed temporary evidence directories, after the test result was printed. The wrapper cleanup order was the cause. scripts/record_latency.py was left untouched and its module was rerun normally: 12 passed in 2.280 seconds with no shutdown warnings. There are no corresponding repository artifacts.

The complete smoke wrapper was not run because its second step recursively runs the full suite. Its three other tests are included in the scoped importer run. The full suite remains the orchestrator's check. Independent read only review found no code issue and passed eight focused consumer, receipt and destination refusal tests.

The full repository hygiene command `python scripts/check_hygiene.py --all` passed with exit code 0. `python scripts/counts.py` still prints the recorded counts, and its generated block was compared directly with README without rewriting README. `git diff --check` passed.

The current fixture tree has all 9,825 starting files and the same aggregate fingerprint. Starting and final hashes also match for README, LOG, API, THREAT-MODEL, gate/rpc.py, intel/cluster.py, intel/seed.py, intel/render.py and scripts/ask_eval.py. Version 1 ordinary and top output files were also compared individually after the real command runs. Neither preservation check rewrites a source or fixture file.

## Files and owner actions

Changed source files:

* `gate/rules.py`
* `gate/account_check.py`
* `gate/verdict.py`
* `gate/agent.py`
* `gate/demo.py`
* `intel/live.py`
* `intel/delivery_checks.py`
* `mcp/server.py`
* `intel/__main__.py`
* `intel/render.py`
* `scripts/scorecard.py`

Changed existing tests are `tests/test_rules.py`, `tests/test_verify_receipt.py`, `tests/test_render.py`, `tests/test_scorecard.py` and `tests/test_ask_eval.py`. New tests are `tests/test_rules_v3_account_cli.py`, `tests/test_rules_v3_live.py`, `tests/test_rules_v3_messages.py` and `tests/test_replay_receivers.py`. Documentation changes are this report and citation repairs in `README.md`, `docs/API.md`, `docs/THREAT-MODEL.md`, `docs/VERDICT-RECORD.md`, `docs/COVERAGE.md`, `docs/spec/evidence-v1.md` and `docs/spec/verdict-v1.md`. The API, evidence and verdict documentation also state the version 3 exception. gate/verify.py, gate/incident_replay.py and scripts/ask_eval.py remain byte identical because the fix reaches them through their existing shared reducer.

The orchestrator must independently review the changes, fold any remaining README and LOG notes below into the release documentation, and run the full suite before committing. The renderer, scorecard and consumer regressions are implemented, and the API paragraph is corrected. No transaction, model purchase, private key or deployment command is required for this lane. All changes remain in the working tree, all application defaults remain version 1 and no recording, remote or tag was changed. Earlier replay output directories remain available for independent review. Their cleanup remains with the owner because automatic approval review rejected the cleanup command.

## Exact documentation text for the orchestrator

README, under What it does:

> Rules version 1 remains the default. Python callers can explicitly select rules_version=3 on gate.rules.judge, gate.verdict.request and gate.account_check.check_account. Version 3 runs the version 1 checks and asks the owner to confirm a receiver younger than 7 days when account_age is its only signal. The rule verdict is UNKNOWN and the public outcome is ASK, never a signed ALLOW. Age together with any other signal still denies. Its signal, evidence, unavailable check reasons and allowlist reason remain recorded. python -m intel replay-receivers --set top --rules-version 3 --out NEW_EXTERNAL_DIRECTORY reproduces 137 DENY to UNKNOWN changes across the 990 recorded top receivers; two existing UNKNOWN files change only their reasons, for 139 changed files in total. The ordinary 84 have no change. This command writes a new directory and does not update the committed recordings. docs/lanes/rules-v3.md records the commands, consumer regressions and unchanged version 1 output proof.

LOG, append to the existing 21 Nov entry:

> Rules version 3 age confirmation now stays UNKNOWN and ASK through live shaping, receipt verification, account checks, verdict owner questions, CLI output, MCP, Telegram reply formatting and incident replay. Added an offline receiver replay command: top 990 gives 137 DENY to UNKNOWN changes plus two UNKNOWN reason-only changes, ordinary 84 gives zero. Renderer and scorecard counts distinguish young receiver confirmation from unavailable checks, and incident and explanation regressions preserve ASK. Versions 1 and 2 retain their outputs and version 1 remains the default. Committed recordings, site output and counts are unchanged. docs/lanes/rules-v3.md records exact tests and source links.

API behavior, now documented in Outcome and action:

> Application entry points use rules version 1 by default: DENY when any check is signal, else UNKNOWN when any check is unknown, else ALLOW. The Python judge, verdict.request and check_account functions accept rules_version=3 explicitly. Version 3 runs the version 1 checks and returns UNKNOWN when account_age is the only signal. The exact default reason is "RECEIVER is younger than 7 days; no other signal was found among the checks that ran. The owner should confirm the receiver before paying." An explicit min_age_days on judge changes the threshold in the reason. Existing unknown check reasons and "Not on the owner's allowlist." follow it. account_age stays signal with the same detail and source, so UNKNOWN can have a signal even when no check is unknown. Public check receipts map this verdict to outcome ASK and action ask; the verdict service keeps outcome UNKNOWN and action ask, supplies the confirmation in ask and never signs it. MCP and Telegram check replies use New account, confirm first for this ASK and retain Not enough data for every other ASK. Version 2 remains independently selectable and adds freshness checks; version 3 does not. No HTTP parameter, MCP argument or payment CLI default was switched. The separate offline replay-receivers command accepts --set ordinary or top, --rules-version 1, 2 or 3 and --out pointing to a new directory outside the repository.

## Sources

No external API was added or changed. The source shapes reused by the existing checks were verified against official documentation on 21 Nov 2026:

* [NEAR RPC View Account](https://docs.near.org/api/rpc/contracts#view-account): view_account uses query with account_id and finality or block_id; the response includes block_height and code_hash.
* [NEAR RPC View Access Key List](https://docs.near.org/api/rpc/contracts#view-access-key-list): view_access_key_list returns access keys with nonce and permissions.

The decision rule and all impact figures are local source and recording facts, not live network claims. Their primary inputs are `gate/rules.py`, the two receiver sets, the incident documents, `fixtures/replay/rhea/run.json`, `fixtures/evidence`, and the original label file identified above.

## Fix round 2

Round 2 changed only `intel/live.py`, `gate/rules.py`, `gate/verdict.py`, `tests/test_verify_receipt.py`, `tests/test_rules_v3_messages.py` and this report. The other round 1 changes remained in place.

The shared live reducer now returns DENY when the verdict is DENY or any check signals. Its only exception is verdict UNKNOWN with exactly account_age as the complete signal set, which returns ASK. Every other input retains the base mapping. In particular, ALLOW plus signal and UNKNOWN plus label_denylist signal reduce to DENY. The receipt verifier rejects an ASK answer for the latter evidence and accepts its matching DENY answer.

The table regression covers all four requested verdicts and all four signal sets. Each combination runs with a pass and an unknown background check, giving 32 subcases. The existing receipt test again expects `("ALLOW", "signal", "DENY")`. An independent comparison of the AST extracted base and current reducers covered 1,024 inputs. All 27 differences were the intended UNKNOWN plus account_age only exception; the other 997 complete results were identical.

The version comment above RULES_VERSIONS is one line. TIER2_CHECKS is back at line 48 and DUST_YOCTO at line 54, matching the existing API citations. docs/API.md was not edited in round 2. The verdict service selects the young confirmation using the account_age signal status and the first generated reason, whose position is established by gate.rules._reasons. It no longer matches reason prose. Its new regression uses independently worded prose and signal, pass and unknown statuses.

### Validation from this round

Each matrix module ran separately as `python -B -X utf8 -m unittest MODULE -v`, with TEMP, TMP and TMPDIR pointing to its own fresh directory. The 31 direct test importers were selected from Python import statements for intel.live, gate.verify, gate.verdict and gate.rules. The matrix adds test_citations, test_replay_receivers, test_ask_eval and test_incident_replay_page. test_incident_replay ran alone afterward. test_registry_mainnet was excluded.

| Run | Cases | Passed | Skipped |
|---|---:|---:|---:|
| Matrix, 34 modules | 1171 | 1169 | 2 |
| test_smoke, all four tests | 4 | 4 | 0 |
| test_render | 84 | 84 | 0 |
| test_incident_replay alone, final system temp run | 59 | 59 | 0 |
| Final module inventory | 1318 | 1316 | 2 |

The smoke subprocess also repeated the same 1,171 matrix cases, with 1,169 passed, 2 skipped and no failures or errors. These repeated executions are not added to the final module inventory. An external sitecustomize adapter limited only the smoke subprocess's unittest discovery to the 34 matrix modules. All nine smoke steps ran, including citation checks through its nested suite, and the tree preservation assertion passed. No smoke source or test was edited. The full unscoped suite was left to the orchestrator under the lane rule.

The two skips were test_ask_eval.test_launch_records_pass_replay because launch records have not been selected, and test_verdict's PyNaCl comparison because PyNaCl is not installed. All tests in test_rules (215), test_verify_receipt (64) and test_citations (11) passed.

All names in the inventory below have the `tests.` prefix.

| Module | Cases | Skipped | Module | Cases | Skipped |
|---|---:|---:|---|---:|---:|
| test_account_check | 39 | 0 | test_analytics_api | 12 | 0 |
| test_api_doc | 27 | 0 | test_ask | 92 | 0 |
| test_ask_api | 31 | 0 | test_ask_eval | 43 | 1 |
| test_ask_validation | 15 | 0 | test_asof_errors | 84 | 0 |
| test_budget | 21 | 0 | test_citations | 11 | 0 |
| test_fuzz | 12 | 0 | test_http | 27 | 0 |
| test_incident_replay_page | 11 | 0 | test_injected | 6 | 0 |
| test_investigation_jobs | 53 | 0 | test_labels | 53 | 0 |
| test_live_accounts | 3 | 0 | test_live_api | 21 | 0 |
| test_live_history | 14 | 0 | test_live_limits | 32 | 0 |
| test_nearblocks_parity | 13 | 0 | test_record_latency | 12 | 0 |
| test_replay_receivers | 7 | 0 | test_reports_rules | 15 | 0 |
| test_rpc | 79 | 0 | test_rules | 215 | 0 |
| test_rules_v3_account_cli | 11 | 0 | test_rules_v3_live | 6 | 0 |
| test_rules_v3_messages | 6 | 0 | test_spec | 17 | 0 |
| test_token_check | 53 | 0 | test_verdict | 49 | 1 |
| test_verify | 17 | 0 | test_verify_receipt | 64 | 0 |

Before the reducer fix, the restored receipt expectation and two new receipt tests ran as three tests with 20 failing subcases. After the fix, test_verify_receipt plus test_rules_v3_live ran 70 tests, all passed. The citation regression ran 11 tests with one failure before comment compression, then 11 passed. The message regression ran six tests with one failing subcase before removing prose matching, then six passed. These focused runs overlap the matrix and are not added to its totals.

The first standalone incident replay run used a fresh directory under OneDrive. It ran 59 tests with one failure and 45 error records after a publication failure left the parent lock in place. An isolated diagnostic reproduced WinError 5 from os.replace during publication. No incident replay code or tests were changed. The unchanged module then ran alone with TEMP, TMP and TMPDIR set to a fresh system temporary directory, and all 59 tests passed. This records the observed difference without attributing the file lock to an unverified process.

`python scripts/check_hygiene.py --all` and `git diff --check` passed. The ten protected source and documentation files, all 9,825 fixture files, HEAD and the submission tag match their recorded fingerprints. No protected file, committed recording or count was rewritten.

### Replays from this round

The three commands used new directories under `../siyab-rules-r2-d2_hcq8s`:

```
python -B -m intel replay-receivers --set top --rules-version 3 --out ../siyab-rules-r2-d2_hcq8s/top-v3
python -B -m intel replay-receivers --set top --rules-version 1 --out ../siyab-rules-r2-d2_hcq8s/top-v1
python -B -m intel replay-receivers --set ordinary --rules-version 3 --out ../siyab-rules-r2-d2_hcq8s/ordinary-v3
```

| Set and version | Judged | ALLOW | DENY | UNKNOWN | Changed verdicts |
|---|---:|---:|---:|---:|---:|
| Top version 3 | 990 | 850 | 1 | 139 | 137 DENY to UNKNOWN |
| Top version 1 | 990 | 850 | 138 | 2 | 0 |
| Ordinary version 3 | 84 | 70 | 14 | 0 | 0 |

All 990 version 1 top judgement files were compared with the committed files byte for byte. All 990 version 3 top check lists match the committed version 1 lists; every one of the 137 changed verdicts has exactly account_age as its signal set. The commands used replay mode and made no network calls.

No external API behavior was added or changed. The two official RPC sources in the Sources section were rechecked in this round for their view_account fields and access key nonce and permission shapes. The rule, citation and replay conclusions are local source and recording facts.

Round 2 left independent review, the orchestrator's full suite before committing and the temporary directory cleanup below with the owner. No transaction, paid call, deployment or key was required. Round 2 did not act on the earlier consumer follow ups; round 3 completes them. All changes remain uncommitted.

Automatic approval review rejected deletion of round 2's two temporary directories as "blocked by policy", with no further reason. They remain at `../siyab-rules-r2-d2_hcq8s` and `%LOCALAPPDATA%/Temp/siyab-rules-r2-incident-99nxbbyg`. No retry or alternative deletion method was attempted. The replay outputs, test logs and external smoke discovery adapter remain available there for review; their cleanup requires owner action.

## Fix round 3

This round keeps the existing branch and all earlier changes. HEAD and `git merge-base HEAD origin/main` both identify `e2d15123d`; the implementation is uncommitted, so base and HEAD source texts match while working source texts differ. No rebase was performed.

### Citations and documentation

A throwaway audit compared cited source excerpts on the base commit, HEAD and the working tree. It included raw prose references, backticks, continued ranges and default file paths. It repaired all 69 requested moved occurrences, representing 39 distinct source range mappings, across the seven documentation files listed below. The renderer fix moved two additional ranges in COVERAGE, so the final count is 71 repaired occurrences and 41 distinct mappings. A changed request signature retained its existing range coordinates. Each moved range maps to the same excerpt or to the corresponding expanded block where version 3 changed its contents. No moved references remain elsewhere in tracked documentation. The audit scripts stayed outside the repository.

The outcome paragraphs in API, the evidence specification and VERDICT-RECORD now explain the age only signal exception. API also documents the confirmation prepended to the owner's questions. Defaults remain version 1. All absolute user directory paths were removed from this report. The top replay count distinguishes 137 changed verdicts from 139 changed judgement files, including `aisthetic.near` and `snirk83.near` as the two UNKNOWN reason-only changes.

Round 3 changes these 13 files:

* `intel/render.py`
* `scripts/scorecard.py`
* `tests/test_render.py`
* `tests/test_scorecard.py`
* `tests/test_ask_eval.py`
* `README.md`
* `docs/API.md`
* `docs/THREAT-MODEL.md`
* `docs/VERDICT-RECORD.md`
* `docs/COVERAGE.md`
* `docs/spec/evidence-v1.md`
* `docs/spec/verdict-v1.md`
* `docs/lanes/rules-v3.md`

### Consumer behavior and preservation

The renderer and scorecard count young_receiver_confirmation for UNKNOWN with an account_age signal, alongside any actual unknown checks. An unavailable age check still counts as account_age, without the young confirmation. The renderer names the new cause New account, confirm first. No check status changes.

Five new tests cover top receiver causes, unavailable age, incident WARN and outcome mismatch rejection, scorecard causes, and the explanation projection from a real version 3 replay. The renderer and scorecard regressions reproduced the missing confirmation cause before the fix. The incident and explanation paths use the existing shared reducer; scripts/ask_eval.py stays unchanged.

Complete default site renders before and after the fix each contain 5,579 files. Every file is byte identical across both renders and committed demo/site. Complete indexable renders also contain 5,579 files and are byte identical before and after the fix. The generated scorecard is unchanged across the before and after render and committed SCORECARD: 2,539 bytes. No generated site or scorecard was rewritten in the repository.

Four fresh offline replay commands reproduced the summaries above. Version 1 ordinary replay matches all 84 committed judgements byte for byte; top replay matches all 990. Version 3 ordinary replay changes no file. Top version 3 changes exactly 139 files, comprising 137 DENY to UNKNOWN verdicts and the two named reason-only changes. The latter files differ only in reasons. No recording was changed.

No external API behavior was added or changed. The two official RPC source URLs in Sources were checked again in this round: view_account uses query with account_id and finality or block_id, and returns block_height and code_hash; view_access_key_list returns keys with nonce and permission. These source facts do not turn unknown checks into an ALLOW.

### Validation from round 3

Validation completed on 22 Nov 2026. Python is 3.12.10. Each matrix module ran separately as `python -B -X utf8 -m unittest MODULE -v`, with TEMP, TMP and TMPDIR set to a fresh short directory under `C:/t/rv3/`. The import inventory comes from Python import statements for intel.live, gate.verify, gate.verdict, gate.rules, intel.render and scripts.scorecard. It adds test_citations, test_ask_eval and test_replay_receivers. test_incident_replay ran alone in a separate invocation; test_registry_mainnet was excluded.

| Final run | Cases | Passed | Skipped |
|---|---:|---:|---:|
| Importer and consumer matrix, 53 modules | 1521 | 1519 | 2 |
| test_incident_replay alone | 59 | 59 | 0 |
| test_smoke, all four tests, one invocation | 4 | 4 | 0 |
| Final module inventory, 55 modules | 1584 | 1582 | 2 |

No failures or errors occurred in these final runs. The two skips are unselected launch records in test_ask_eval and the absent optional PyNaCl package in test_verdict.

The smoke test passed all nine offline steps and its source tree preservation assertion. An external sitecustomize adapter limited only its nested unittest discovery to the 36 core and touched consumer modules marked below, containing 1,267 cases and the same two existing skips. The 17 additional renderer importers ran in the separate matrix. This avoids repeating the longer browser tests within the smoke subprocess's fixed 1,800-second timeout. No smoke source or test changed, and the full unscoped suite remains with the orchestrator. Nested repetitions are not added to the final module inventory.

All module names below have the tests prefix.

| Module | Cases | Skipped | Nested smoke |
|---|---:|---:|---|
| test_account_check | 39 | 0 | yes |
| test_analytics_api | 12 | 0 | yes |
| test_analytics_render | 16 | 0 | no |
| test_api | 32 | 0 | no |
| test_api_doc | 27 | 0 | yes |
| test_ask | 92 | 0 | yes |
| test_ask_api | 31 | 0 | yes |
| test_ask_eval | 44 | 1 | yes |
| test_ask_validation | 15 | 0 | yes |
| test_asof_errors | 84 | 0 | yes |
| test_budget | 21 | 0 | yes |
| test_citations | 11 | 0 | yes |
| test_cloud_workspace | 2 | 0 | no |
| test_component_boundaries | 8 | 0 | no |
| test_counts | 14 | 0 | no |
| test_design_system | 51 | 0 | no |
| test_entity_tables | 4 | 0 | no |
| test_feed_behavior_signal | 19 | 0 | no |
| test_feed_workspace | 7 | 0 | no |
| test_fuzz | 12 | 0 | yes |
| test_graph_loading | 4 | 0 | no |
| test_graph_viz | 8 | 0 | no |
| test_http | 27 | 0 | yes |
| test_incident_replay_page | 11 | 0 | yes |
| test_injected | 6 | 0 | yes |
| test_investigate | 2 | 0 | no |
| test_investigation_jobs | 53 | 0 | yes |
| test_labels | 53 | 0 | yes |
| test_landing | 25 | 0 | no |
| test_landing_motion | 17 | 0 | no |
| test_live_accounts | 3 | 0 | yes |
| test_live_api | 21 | 0 | yes |
| test_live_history | 14 | 0 | yes |
| test_live_limits | 32 | 0 | yes |
| test_live_site_render | 29 | 0 | no |
| test_nearblocks_parity | 13 | 0 | yes |
| test_profiles | 12 | 0 | no |
| test_record_latency | 12 | 0 | yes |
| test_registry_overview | 4 | 0 | no |
| test_render | 87 | 0 | yes |
| test_replay_receivers | 7 | 0 | yes |
| test_reports_rules | 15 | 0 | yes |
| test_rpc | 79 | 0 | yes |
| test_rules | 215 | 0 | yes |
| test_rules_v3_account_cli | 11 | 0 | yes |
| test_rules_v3_live | 6 | 0 | yes |
| test_rules_v3_messages | 6 | 0 | yes |
| test_scorecard | 8 | 0 | yes |
| test_spec | 17 | 0 | yes |
| test_token_check | 53 | 0 | yes |
| test_verdict | 49 | 1 | yes |
| test_verify | 17 | 0 | yes |
| test_verify_receipt | 64 | 0 | yes |

The separate touched-module run covered 139 cases: test_render 87 passed, test_scorecard eight passed, and test_ask_eval 44 ran with 43 passed and one existing skip. Citation-specific review ran test_citations plus test_api_doc as 38 passing cases, then test_citations as 11 passing cases. These executions overlap the final inventory and are not added to its totals.

`python scripts/check_hygiene.py --all`, `python scripts/check_citations.py` and `git diff --check` all passed with exit code 0.

Final fingerprints match all 9,825 fixture files, all 5,579 committed site files, docs/LOG.md, gate/rpc.py, intel/cluster.py, intel/seed.py and scripts/ask_eval.py. HEAD and the submission tag are unchanged. The lane report contains no absolute user directory path. Read only independent review found no issues in the round 3 source, tests, citation mappings or preservation proof.

The orchestrator still owns independent review, the full suite, any remaining README and LOG release notes, and committing the reviewed working tree. No transaction, paid call, key or deployment is required. No default was switched.

Automatic approval review rejected cleanup of generated render directories under `C:/t/rv3/consumer-fix-c178d869/` and the external citation audit under `C:/t/rv3-cites/` as "blocked by policy", with no further reason. The temporary renders, audit files and test evidence remain outside the repository for owner cleanup. No throwaway audit script entered the repository, and no blocked directory was removed by another method.
