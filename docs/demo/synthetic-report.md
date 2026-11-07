This is a synthetic report made for a recorded demonstration: its subject, `kestrelridge-mallory.testnet`, is our own demo counterparty account on NEAR testnet, nothing happened to anyone, and the run is designed to overturn the report before it ends; the run log under `fixtures/runs/` records whether it did.

# Synthetic report: kestrelridge-mallory.testnet

## What this page is

`scripts/live_beat.sh` files one report on the testnet reports contract `reports.kestrelridge-main.testnet` and points it at this page. The run shows the whole report life on chain, each step followed by a recorded read of `subject_summary`, `get_report` and a verdict for a payment to the subject that is judged and never sent:

1. The report is filed by the reporter account, kind `theft`, outcome `near_miss`. While it is pending it has no effect on a verdict.
2. The owner, the sole moderator, accepts it. The subject's summary turns `deny` true and the verdict for a payment to the subject is DENY, citing the report id.
3. The disputer account opens a dispute that points at this same page. The report is contested and still denies.
4. The owner resolves the dispute with `overturn`. The report is overturned, the subject's summary turns `deny` false, and the report no longer denies.

## The subject

`kestrelridge-mallory.testnet` is an account the team created and controls for the testnet demonstration (`docs/TESTNET.md`, demo counterparty, ours). No theft was attempted and no payment was sent to anyone; the only funds that moved are the report and dispute deposits the contract keeps as a storage fee, paid by our own accounts. No one outside the team took part. The report describes nothing that happened. It exists only to drive the reports contract through filing, acceptance, dispute and overturn in one recorded run.

## The hash rule

The report's `evidence_sha256` is the sha256 of this file's bytes as committed at the commit its `evidence_url` names, `https://raw.githubusercontent.com/Ridwannurudeen/shieldbot-near/<commit>/docs/demo/synthetic-report.md`. `scripts/live_beat.sh` computes it from the file at run time and refuses to file when the file differs from that commit. No hash of this page is written in the repository; it is read back from `get_report` and kept in the run log under `fixtures/runs/`.

The dispute points at this same page with the same hash: the reason to overturn is written here, in the first line.
