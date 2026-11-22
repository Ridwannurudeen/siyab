# Receiver holdout format

The input is a private UTF-8 JSONL file with one object per nonblank line. No receiver records are included here. The default path is receivers.jsonl in this directory; an external file can be selected with --holdout. The repository ignores eval/holdout/*.jsonl so ordinary git add eval/ omits private input files.

| Field | Requirement |
|---|---|
| account | A valid NEAR account id accepted by gate.canon.account_id, unique in the file. Mixed case and surrounding whitespace are rejected without lowercasing. |
| label | Exactly bad or good. Other values are rejected. |
| source_url | Required for a bad row: an HTTP or HTTPS URL with a host and no whitespace. Optional for a good row. |
| note | Required for a good row: a nonblank explanation. Optional for a bad row. |

An empty file, invalid JSON, invalid UTF-8, duplicate accounts or invalid required fields stop scoring. Row errors include physical line numbers, including blank lines. A duplicate error also names the first line. Source URLs are checked for form, never fetched; the scorer does not establish whether a label is true.

## Verdict input

Use a directory containing one JSON document per receiver. Subdirectories are included. Supported documents are:

* Evidence documents with an object-valued subject, subject.receiver and a string verdict.
* Verdict-service responses with request.receiver_id and outcome.
* Simple documents with a string subject, account or receiver_id, and a string verdict or outcome. When all account fields are absent, the filename without .json identifies the receiver.

Outcomes are normalized to uppercase. ALLOW, DENY and ASK always use their respective columns, and UNKNOWN becomes ASK. The declared decision controls the column in every document shape, so an ALLOW service response with action ask counts as ALLOW just like its evidence document. The service action does not change the decision being measured or authorize a payment.

Every receiver source, including the filename, uses gate.canon.account_id without lowercasing. A malformed explicit account field does not fall back to a filename. Invalid documents, unsupported outcomes and duplicate receiver verdicts stop scoring with exit code 2 and no score on stdout. An empty verdict directory is valid and leaves every input row unjudged.

A standalone signed permission or refusal record with literal and evidence_hash does not contain a receiver or the supported outcome fields. Supply its evidence document or the complete service response. Do not mix these records, summary files or multiple answers for the same receiver into the verdict directory. The scorer does not check signatures or authorize payments.

## Scoring

The positive decision is DENY and the positive reference class is bad.

| Reference | DENY | ALLOW, ASK or no verdict |
|---|---|---|
| bad | True positive | False negative |
| good | False positive | True negative |

Precision is true positives divided by all DENY decisions on input rows. Recall is true positives divided by every bad input row. Bad ASK and unjudged rows are misses for recall. Changing DENY to ALLOW, ASK or no verdict cannot improve recall on an unchanged input. Precision can change independently when the false-positive count changes. A zero denominator gives 0.0.

The detailed table keeps DENY, ALLOW, ASK and NOT_JUDGED separate for each reference class. A statistical true negative that is ASK or unjudged is not an ALLOW. All unjudged accounts are listed. Verdict accounts outside the input are counted and listed in sorted order, and do not enter either metric.

With --exclude-labelled, a second report removes receivers with a current NEAR incident or denylist assertion in the selected label store. The full store is validated with intel.labels.load and must contain at least one assertion. Only assertions whose superseded_by is null are current. Missing, empty, blank or malformed stores stop the run; they never become an empty exclusion set. A valid store with assertions but no current matching categories can exclude zero receivers. Excluded input receivers are removed from that report's verdicts too, so they are not mistaken for outside accounts.

## Running

From the repository root, with private inputs supplied by their owner:

```text
python -m eval.score --holdout <private-jsonl> --verdicts <verdict-directory> --rules-commit <producing-commit>
python -m eval.score --holdout <private-jsonl> --verdicts <verdict-directory> --rules-commit <producing-commit> --exclude-labelled --json
```

Both text and JSON output record holdout_sha256 over the exact bytes parsed and rules_commit as a full local Git commit id. --rules-commit defaults to the scoring checkout's HEAD. When scoring previously generated verdicts, supply the commit that produced them. The scorer resolves that declaration locally; it cannot prove which code produced arbitrary input files. An invalid or unavailable Git revision stops scoring.

JSON includes matrix, confusion, precision, recall, counts, not_judged_accounts and outside_holdout_accounts. The optional excluding_label_store object contains subjects, excluded_rows and its report. The top-level input hash and commit apply to both reports. Counts, separate missing and ASK columns, and the denominator should be retained when comparing results.

The scorer performs local file and Git reads only. It does not generate receiver rows, sign, pay or request a model.
