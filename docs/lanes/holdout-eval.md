# Holdout scorer

The scorer now includes every bad input row in recall. ASK and absent verdicts count as misses and remain separate in the detailed table. The binary DENY confusion counts include all input rows. Verdicts outside the input have a count and complete sorted account list; missing verdicts have a count and complete account list.

## Files

* eval/score.py: canonical account validation, evidence and service-response account extraction, consistent decision columns, recall, confusion counts, outside accounts, strict label filtering and text and JSON provenance.
* tests/test_eval_score.py: synthetic loader, metric and CLI regressions, including the real evidence fixture shape with a synthetic receiver.
* eval/holdout/README.md: private input format, supported verdict documents, formulas and commands. It contains no receiver records.
* docs/lanes/holdout-eval.md: this handoff and verified sources.
* .gitignore: eval/holdout/*.jsonl keeps private input files out of ordinary git add eval/.

The contributor's eval/__init__.py is unchanged. No private receiver data was inspected, searched for, fetched or created. No other lane's files were edited. No commit, push, remote change, tag change, deployment, transaction or paid call was made.

## Decisions

Evidence uses subject.receiver, not the object itself or the evidence hash filename. A full service response uses request.receiver_id and outcome. Simple verdict files keep their original string and filename forms. All receiver sources now use gate.canon.account_id(value, field), including filename fallbacks. Invalid holdout accounts produce physical line numbers, invalid verdict accounts name the file and field, and either error stops scoring without a partial report. Mixed case is rejected without lowercasing, so Alice.near cannot silently hide a false positive for alice.near. Valid duplicate accounts still report the first physical line.

Columns measure the declared decision consistently across document shapes. ALLOW maps to ALLOW, DENY to DENY, ASK to ASK and UNKNOWN to ASK. A service response with outcome ALLOW and action ask now uses ALLOW, just like its evidence document. The service action does not change the decision being measured. These columns do not authorize payment or establish that a signature exists.

The exclusion filter uses the existing intel.labels.load validator and only current NEAR incident or denylist assertions. Superseded rows do not exclude a receiver. Missing files, empty or blank stores, malformed JSON, invalid assertion schemas and dangling replacement ids fail with no score on stdout. A nonempty valid store can have no eligible assertions and exclude zero rows. Excluded rows cannot reappear as outside accounts in the second report.

The new ignore regression queries Git with synthetic path arguments only. It confirms that the default receivers.jsonl path and another JSONL input are ignored while the format document and scorer remain visible. It never reads or creates a private input file. Path arguments avoid Windows text-mode stdin newline translation.

Invalid verdict files and duplicate receiver verdicts now stop the run instead of yielding a partial score or choosing the last file. An empty verdict directory produces a complete missing-verdict report. Holdout row diagnostics include physical line numbers and the original line for duplicates. A bad row requires a source URL; a good row requires an explanatory note. URLs are checked for form only and are never fetched by the scorer.

The exact holdout bytes are read once, used for validation and hashed for holdout_sha256. rules_commit records a locally resolved commit. The default is HEAD, and --rules-commit selects the commit that produced an existing verdict directory. This is a declaration of provenance, not proof that arbitrary files were produced by that commit. No rules code is executed by scoring.

## Validation

The existing 43 scorer tests passed before this fix round. Nine new tests and an updated decision test reproduced the review findings before implementation. The commands were run with TEMP and TMP set to C:\t\sc3:

```powershell
$env:TEMP = 'C:\t\sc3'
$env:TMP = 'C:\t\sc3'
python -m unittest tests.test_eval_score
python scripts/check_hygiene.py --all
python scripts/check_citations.py
```

The scorer module has 52 passing tests, 0 failures and 0 skips. New coverage checks invalid account types, case, whitespace, length, separators and characters; all five explicit verdict account locations and the filename fallback; mixed-case CLI refusal with physical line numbers; empty and blank label-store refusal at both loader and CLI; and Git ignore behavior. The explicit rules-commit test now passes the local HEAD commit id and requires no origin/main ref.

One existing test covers all 64 assignments of DENY, ALLOW, ASK or no verdict across three synthetic bad receivers and verifies that replacing a DENY cannot improve recall. The CLI tests verify JSON and text output, exact CRLF input hashes, explicit and default commits, missing rows, outside accounts and exclusion failures. Only tests/test_eval_score.py imports eval.score. The full suite is left to the orchestrator under the lane instructions.

The full-tree hygiene check passed with exit code 0 and 0 findings. The citation check passed with exit code 0 and 0 findings.

## Verified facts and sources

Primary repository sources:

* gate/evidence.py and docs/spec/evidence-v1.md define the object-valued subject with receiver and the top-level string verdict. fixtures/registry/sample-allow-evidence.json supplies the regression's real document shape.
* gate/verdict.py defines request.receiver_id, outcome, an optional signed verdict object and action ask for a policy-constrained ALLOW.
* gate/canon.py defines account_id(value, field), returning the exact account string or raising CanonError, a ValueError subclass. It enforces the repository's account syntax and 2 to 64 character bounds.
* gate/__main__.py writes standalone permission or refusal records without receiver fields. These must be scored through evidence or the complete response.
* intel/labels.py and docs/spec/label-assertion-v1.md define complete-store validation and current assertions with null superseded_by.

The installed commands report Python 3.12.10 and Git 2.54.0.windows.1. Primary external sources for this fix round:

* Git ignore patterns use slashes for path segments and an asterisk matches characters within one segment. The installed command confirms eval/holdout/*.jsonl matches both private paths: https://git-scm.com/docs/gitignore
* git check-ignore reports matching paths and --no-index checks the ignore rules regardless of index membership. The regression checks path strings without creating files or changing the index: https://git-scm.com/docs/git-check-ignore
* git rev-parse --verify with the commit suffix resolves a revision to a commit. --end-of-options prevents interpreting an input revision as a command option: https://git-scm.com/docs/git-rev-parse

## Owner action and limits

After independent review, the orchestrator can commit these files. For an actual score, the owner supplies the private input and the already generated verdict directory and runs the commands in eval/holdout/README.md with --rules-commit set to the producing commit. No live flag, key, transaction or paid model request is needed. The private input was intentionally unavailable to this lane, so no result against it is claimed.
