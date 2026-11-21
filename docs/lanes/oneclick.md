# 1Click settlement lane

21 Nov 2026. Branch `feat/oneclick-settle`, based on `0e113268c`. Changes are left in the working tree for independent review. No commit, push, deployment, transaction, paid call, credential use or tag change was performed.

## What runs

`python -m gate intents-settle` obtains a judged dry quote, requests a non-dry quote with a real allocated deposit address, checks status and funding, prints the transfer and stops with ASK. This preview consumes a provider allocation even though it cannot fund it. Only its explicit `--deposit` flag permits signing, broadcasting and deposit notification. The settlement parser disables option abbreviation, so `--dep` cannot enable spending. `--live` is rejected on this command. The existing read-only `intents --live` is unchanged. `python -m gate intents-resume` follows the original deposit without creating another quote or transaction. `python -m gate intents-release --sender ACCOUNT --report PATH` verifies one reserved report and removes only its registry row when no deposit from that run can land. Release never reads a key, creates a quote, signs, broadcasts or notifies the provider.

The implemented funding route is `EXACT_INPUT`, `ORIGIN_CHAIN`, `nep141:wrap.near`, with origin-chain refunds to the sender and delivery to an EVM address. The hard deposit cap is `250000000000000000000000` base units, or 0.25 wNEAR. The transaction contains one `wrap.near.ft_transfer`, 50 Tgas and one yoctoNEAR. If the allocated receiver is unregistered, the same transaction first calls `storage_deposit` with `registration_only: true`, 30 Tgas and the viewed storage minimum capped at 0.00125 NEAR. Both calls target `wrap.near`. Native NEAR must already have been wrapped. The CLI does not wrap funds, register the sender, top up a deposit or retry a transaction.

This is a local checked wallet path. It does not route deposits through the existing testnet Gate and does not claim mainnet contract enforcement. Its recipient judgement and final reconciliation are unsigned local results. The existing `intents` command and its legacy deposit binder remain read only. That binder remains closed because its original checks do not authenticate or reconcile a real quote's economics. The complete status handling equivalent lives in `gate.oneclick.STATUS_ACTIONS`.

## Checks before funding

1. Validate the canonical positive amount, cap, owner output floor, mainnet sender and EVM recipient before network access. `--deposit` requires a key file and a new report outside the repository. Under an exclusive account registry lock, check every registered open run and existing files in the output directory before reserving the report or reading the key. Block a new deposit when any report is at `prepared`, `broadcast`, `deposited`, `submitted` or `polling`, even if its filename has no `.json` suffix or resembles an atomic temporary file. A report with an unrecognized later phase and a deposit hash, signed reference or signed payload also blocks. An owner may explicitly name each blocker with repeatable `--override-report REPORT`. A registered path remains a blocker and can be named even if its report is missing or in another directory. Final or unrelated unregistered paths cannot serve as overrides. Record the exact named paths in the new report. Keep the output directory dedicated to JSON reports; unreadable or malformed files stop the run. Only the explicitly supplied key file is excluded from scanning. Commit the new registry reservation before constructing `NaClSigner`, reading its Ed25519 key or constructing any network client. A missing PyNaCl package, unreadable file, bad key or conflicting account id stops before any quote. Fixture replay cannot fund a live call.
2. Read the provider token catalog. Bind both asset ids, blockchains, contracts and decimals. Verify mainnet on the origin RPC, the expected EVM chain id and support for finalized block reads.
3. Run the existing recipient checks through `intents.judge_quote`. An opt-in `quote_verifier` verifies the raw dry answer before its quote can pass. Every required recipient check must ALLOW.
4. Verify the non-dry quote signature against the manager public key from the pinned official SDK. Bind its request to the intact judged request, including sender refund account, recipient, assets, exact input, slippage and named app fees. Only the recorded service fee may be added. Missing or changed fees stop the run.
5. Require exact quoted input and minimum input. Allow at most a 25 basis point downward tick from the dry quote's minimum, rounding the tolerated floor upward to base units. Independently require `int(minAmountOut) - int(withdrawFee) >= int(owner_floor)`. This conservative predeposit bound protects the owner's receipt under either a gross or net reading of the quote output. The non-dry quote still enforces its own judged slippage ceiling. Require explicit refund and withdrawal fees no higher than the dry quote's fees. Missing fees remain unsupported. A present but malformed withdrawal fee, including null, is refused. The owner's floor is never reduced.
6. Require a NEAR deposit account, no deposit memo, no alternate chain addresses and no custom recipient message. Require at least 120 seconds plus five seconds of clock skew before the request refund deadline, address expiry and any supplied cold time. Quote age accepts five seconds of clock skew at each edge of the five minute bound, so its permitted local age is minus five through 305 seconds. The dry judgement uses the same opt-in tolerance; read-only callers retain zero tolerance by default. Status `updatedAt` may be up to five seconds ahead of the local clock or behind quote creation. The address expiry may follow the refund deadline. A cold time is a conservative local funding bound, not an assertion that the address has expired.
7. Fetch status for that exact deposit address. Verify its nested quote signature, selected signed payload and normalized economic request against the allocated quote. Correlation ids are tracing ids and are not quote identity. Known null optional fields and omitted defaults are normalized from recorded status responses and the SDK vector. Unknown fields or changed economics stop.
8. Require PENDING_DEPOSIT, no transaction hashes and no reported prior funds. Require sender registration on `wrap.near` and sufficient wNEAR. Read receiver registration. A null balance requires valid storage bounds with equal minimum and maximum, and a positive minimum no greater than 0.00125 NEAR. Malformed bounds stop UNKNOWN. Require at least 0.01 native NEAR plus any registration deposit. The chain remains authoritative about gas and storage affordability.
9. With `--deposit`, require the signer's public key to have ordinary on-chain FullAccess permission for the explicit sender. Recheck funding, quote lifetime and status immediately before broadcasting. A receiver registered by another party in the meantime needs no new storage payment; if it becomes registered after the last read, `storage_deposit` refunds its redundant deposit to the sender. Persist the recomputed signed `storage_deposit` amount and print it in the final CLI summary, even when it differs from the preview.

The SDK signature covers selected fields. In particular it excludes app fees, correlation ids and several defaults. It does not sign `swapDetails`. Independent request and fee checks remain necessary. Public signature bytes are recorded as JSON byte arrays so they cannot be confused with private-key strings by repository hygiene.

## Deposit, recovery and settlement

Before any broadcast, a new external report is reserved exclusively. Each update writes a temporary file in the same directory, flushes it to disk and atomically replaces the report. On POSIX, it then opens and fsyncs the parent directory before returning. A write failure before replacement preserves the preceding valid report. A directory sync failure after replacement is reported and prevents the broadcast that would follow that save. Windows cannot perform this directory fsync through the Python CRT file descriptor path used here. The file is flushed, but a power cut can roll back its directory entry. Report phase alone therefore never proves that a signed transaction was not broadcast. The prepared transaction hash, exact storage deposit, signed reference block hash and height, public key and nonce are persisted before `send_tx`. Reusing a report path for a new settlement is refused before network access.

The fixed per-signer registry is `~/.siyab-oneclick/<sender>.sqlite3`, where `~` is `Path.home()`. On this Windows account that is `C:/Users/gudma/.siyab-oneclick/<sender>.sqlite3`. Run owner commands from a normal shell with the usual home and no HOME or USERPROFILE overrides. A different home selects a different registry and cannot see earlier reservations. The directory uses mode 0700 and the database mode 0600 on POSIX. Windows uses the user's home directory permissions. SQLite `BEGIN EXCLUSIVE` protects checking and reserving the open run, with DELETE journaling, synchronous EXTRA and a five second lock timeout. POSIX also fsyncs the home directory and registry directory. The lock is released only after the reservation is committed, before reading the key or making a network call. Concurrent runs and a different `--out` directory therefore see the same reservation. A lock or registry failure refuses the run before key access or network access.

Reservations hold absolute report paths, never key material. A missing, malformed or rolled back report does not remove its reservation. Neither age nor process death removes one. Each new reservation starts with durable `signing_attempted: 0` and `reserved_at_ms`, the local Unix time in milliseconds captured under the registry lock. Immediately before entering the signing path, a separate exclusive registry transaction changes the marker from zero to one and requires exactly one updated row. Signing refuses if that exact reservation has disappeared or its marker is no longer zero. This prevents an active run from entering signing after another process releases or overrides its unsigned row. A retained signed or unknown marker cannot authorize another entry into signing. Existing rows migrate with null values for missing fields. A null signing marker means unknown, rather than a claim that no signing happened. A null reservation time always requires interactive confirmation before an override.

A normal exit before any signing attempt releases only its own reservation whose durable marker is still zero, even if the final report save fails. It preserves a signed or unknown row when the signing transition refuses. Once signing has been attempted, automatic release requires this invocation to prove settlement, original receipt failure or expiry, then save the final report successfully. A failed final save keeps that signed reservation. A loaded terminal phase cannot release a reservation after a failed proof read. If signing is interrupted before its hash reaches the report, the marker remains 1 and a report rolled back to `ready` cannot claim unsigned release.

Explicit `intents-release` holds the account registry lock while checking one row and its report. It accepts a bound sender, version 1, a `started` or `ready` report without a deposit hash, reference or signed payload, and the durable zero signing marker. That local combination proves this run did not enter signing. A missing or unreadable report, null legacy marker, attempted signing without a recoverable hash, or signed payload without a hash is refused with its specific cause. Restore the original signed report to resume or prove release, or check the chain before deliberately overriding the unresolved row. Report phase alone is never sufficient. For a signed report, release reruns the existing bound original receipt Failure or expiry proof. A successful deposit, pending transaction, unsupported key, incomplete receipt or unavailable proof refuses release. It does not poll a destination or treat a successful deposit as unused. Save the verified proof before deleting the exact row; all other rows remain. Successful release returns ASK and `registry_released: true`, with exit code 0. Refusal exits 2 and preserves the row.

An override records the owner's bypass. Inside the same exclusive registry transaction that checks blockers and reserves the new report, it deletes every explicitly overridden row whose signing marker is still zero. The older invocation then cannot update its missing row and refuses before signing, just as after unsigned release. This revocation remains effective if the new run later refuses or fails. Signed or unknown rows remain reserved, require the existing interactive prompt and cannot authorize a new signing attempt through the zero to one transition. If the older invocation already committed its marker to one, it may already be signing or broadcasting; an override cannot recall that attempt or prevent its settlement. The required prompt covers that possible duplicate payment. The override prints the Windows rollback warning, runs a bounded sender to `wrap.near` transaction lookup and prints the lookup URL and full JSON answer before reading the key. The answer or exact lookup error is retained in `override_lookup`. The lookup explicitly supplies both signer and receiver because NearBlocks defaults an omitted receiver to the account when a signer filter is present. Every returned row must bind both accounts, a valid transaction hash and a canonical 19 digit nanosecond timestamp. Other timestamp units, malformed rows, provider error fields, HTTP errors and timeouts are lookup failures.

Any overridden row whose durable `signing_attempted` is not zero, including an unknown marker or an unregistered report, always requires the owner to type exactly `OVERRIDE ACCOUNT` at an interactive prompt. This applies even when NearBlocks lists no activity or only old activity, because a signed deposit may not have been indexed. Independently, any transaction at or after `reserved_at_ms - 5000`, any failed lookup, or any unknown reservation time requires confirmation. The five second margin reuses the quote clock tolerance and covers a local clock ahead of chain time by that allowance. Equality at the adjusted cutoff requires confirmation. Only a durable zero signing marker, known reservation time and a successful lookup containing no activity at or after that adjusted cutoff can avoid the prompt. The ten row sample never proves non-inclusion.

The report retains the reasons in `override_confirmation_required` and records `override_confirmed: true` only after a matching answer. The CLI saves that decision before loading the key. Non-interactive stdin, a wrong answer or end of input stops UNKNOWN before key loading. No flag skips this prompt. Run overrides directly in interactive PowerShell or cmd. Git Bash mintty presents non-interactive stdin to the native Windows Python path used here, so required confirmation always refuses there. Piping an answer cannot approve an override.

Live resume also registers the report before network access, so following a legacy report brings it into the registry. Legacy reports not yet registered can only be discovered in the selected output directory or by explicitly resuming them. The registry coordinates this user's home on this machine; it cannot discover runs under another home or on another machine. Preserve the registry with the reports.

At phase `prepared`, a signed deposit may have executed after a later `broadcast` save was lost. Resume reads its original hash and requires exactly the same final failure or expiry proof as an uncertain broadcast. Without that proof it returns UNKNOWN and "do not redeposit yet". A prepared report with a signed reference or signed payload remains a blocker even if its hash is missing. Before attempting `send_tx`, persist phase `broadcast` and its instruction to resume and never redeposit. An unresolved broadcast may have executed even if the process timed out. Follow that original report. Only a proven `deposit_failed` or `deposit_expired` outcome permits an automatic new run after an unconfirmed deposit; both are ASK, never an ALLOW of a settlement. The command never starts that fresh run itself.

The single `send_tx` uses `wait_until: FINAL`. The response must name the prepared hash, sender, `wrap.near`, exact `ft_transfer` arguments, gas, attached deposit and successful finalized execution. If registration was prepared, it must precede the transfer and match the exact recorded deposit, receiver, boolean registration flag and gas. Unexpected actions stop UNKNOWN. NEAR executes these same-contract actions in one receipt and reverts their state changes if any action fails; gas can still be charged. The optional provider notification is `POST /v0/deposit/submit` with `depositAddress`, that `txHash` and `nearSenderAccount`. Notification errors stop UNKNOWN without sending again.

A matching FINAL transaction with Failure becomes `deposit_failed` only when its original `wrap.near` action receipt also has that exact Failure, or transaction conversion failed before emitting any receipt. Select the original receipt through `transaction_outcome.outcome.status.SuccessReceiptId` and verify its id and executor. A failed returned child promise after a successful root receipt cannot prove rollback and remains UNKNOWN. The report retains the transaction and receipt outcomes in `deposit_result`, and the reason cites the hash and actual Failure. No wNEAR was deposited in the accepted failure cases. Registration state is rolled back with the batch; gas may have been charged. Native attachments are refunded by the protocol, with exceptional refund failure outside this local claim. Resume can verify the same failed outcome and permit a fresh run with a new report.

For an uncertain `prepared` or `broadcast`, resume first reads `tx` with `wait_until: NONE`. A known transaction is then read again with `wait_until: FINAL` before checking the full matching transaction and outcome. The official NEAR RPC documentation defines NONE as the current status without waiting and FINAL as finalized execution with all receipts, including refunds, finalized: https://docs.near.org/api/rpc/transactions#the-wait_until-field. A landed discovery response without an outcome therefore no longer leaves the report stuck in broadcast. A timeout, provider error, missing result or incomplete final outcome still remains UNKNOWN. Only a correctly bound `UNKNOWN_TRANSACTION` error from discovery can enter the expiry check. Read a finalized block and `EXPERIMENTAL_protocol_config` pinned to its hash. Use the returned `transaction_validity_period` in blocks, requiring `final_height > signed_reference_height + transaction_validity_period`; equality is still within the window. Public mainnet reads in the earlier round returned protocol version 88 and period 86400. That observation is not hardcoded. Check the reference block hash and height against both hash and canonical height reads from the archival endpoint, so non-archival garbage collection cannot prevent this check. Record that endpoint in `deposit_expiry.reference_rpc`, then read the ordinary FullAccess key at the same finalized head. An unchanged nonce of `signed_nonce - 1` retains the existing expiry proof.

When the final key nonce is greater than or equal to the signed nonce, the old signed transaction cannot newly pass nonce validation. To check that the signed transaction did not already execute, require the archival provider https://archival-rpc.mainnet.fastnear.com to return the same final block hash and height, then issue `tx` with the original `tx_hash`, `sender_account_id` and `wait_until: NONE`. Its correctly bound `UNKNOWN_TRANSACTION` error must also be corroborated by NearBlocks. First read `GET /v3/blocks/latest?limit=1` and require its indexed block height to be at least the signed reference height plus the dynamically read validity period plus ten blocks. The decimal string height must be canonical and fit u64, and the block hash must encode 32 bytes. An empty list, null, malformed row, provider errors, lagging height or unavailable read refuses release before asking for hash absence. The ten block margin puts the observed index beyond the entire validity window rather than accepting its edge. It is a local allowance, not a provider lag guarantee. Then require `GET /v3/txns/{hash}` to return HTTP success with exactly `{"data":null}`. Validate the original transaction hash as Base58 encoding of 32 bytes before any provider request; NearBlocks itself returns null even for malformed text. Only all these checks together permit `deposit_expired`. Fastnear's ordinary and archival endpoints count as one operator. A NearBlocks transaction object, unrelated hash, mixed error envelope, malformed response or unavailable source refuses release with its specific cause. The proof records the Fastnear URL, parameters and full error envelope in `deposit_expiry.archival_lookup`, the latest block URL, parameters, response and required height in `deposit_expiry.independent_indexed_block`, and the hash URL and response in `deposit_expiry.independent_lookup`.

If the archive finds that hash, read FINAL and use the matching original transaction checks; successful execution never becomes expiry. These are trusted provider observations, not a cryptographic proof of absence. NearBlocks exposes its latest indexed block height through `/v3/blocks/latest`; the hash lookup alone is a database miss without a completeness attestation. The latest endpoint is documented as cached for five seconds. A cached or lagging height cannot bypass the required height and can delay release. The height check does not prove that every historical transaction is present. Its published source searches its recent window and then the remaining configured historical range. The deployed source revision is not independently attested. The current node's missing transaction alone is insufficient when the nonce has been consumed because older outcomes may have been pruned. A decreased, malformed or unsupported key still refuses. Old reports without the signed reference cannot prove expiry and stay UNKNOWN.

The shared `rpc.Client.call` can carry `wait_until`, but its validator does not include `UNKNOWN_TRANSACTION` in `_ERROR_PAIRS`. A direct check in round 4 returned `MalformedResponse: the RPC object has undeclared fields` for the documented error envelope. It cannot expose the required unknown-hash proof through that shared path yet. This lane leaves the excluded `gate/rpc.py` untouched: `_transaction_read` uses the existing `_exchange` transport and fixture path, validates that one complete error envelope and its requested hash, and delegates every other response to `rpc.response_gate`. Mainnet and archival discovery share this one validator. Mixed result and error bodies are refused. The exact requested shared change is to add `("HANDLER_ERROR", "UNKNOWN_TRANSACTION")` with required `requested_transaction_hash`, bind it to the requested `tx_hash`, then expose the validated error through `Client.call`. That would allow replacing the local envelope branch.

Polling is bounded to 1 through 120 status reads, with five seconds between ordinary status reads and 30 seconds while a SUCCESS awaits an indexed or finalized EVM receipt. The real RPC client's precise no-result error on a null receipt maps to pending; other malformed receipt errors stop UNKNOWN. Each HTTP/RPC read uses the existing 30 second transport timeout. A timeout or uncertain broadcast preserves the original hash and returns UNKNOWN. Resume reads `tx` with the original hash and sender, requires matching finalized execution and then polls the same deposit address. Expired quotes can still be followed after funding; quote expiry never hides a later refund or proves transaction expiry. Resume has no signing, broadcast, quote creation or notification path.

| Documented status | Provider meaning | Siyab action |
| --- | --- | --- |
| PENDING_DEPOSIT | Waiting for a deposit | Fund only a fresh empty address after all checks. After a broadcast, poll the same deposit. |
| KNOWN_DEPOSIT_TX | Deposit transaction is known | Poll the original deposit. Never fund again. |
| INCOMPLETE_DEPOSIT | Deposit is below the required amount | Stop UNKNOWN. No automatic top up. |
| PROCESSING | Swap execution is in progress | Poll the original deposit. Never fund again. |
| SUCCESS | Provider reports destination delivery | Reconcile actual amounts and a finalized destination receipt. |
| REFUNDED | Provider reports refund handling completed | Stop UNKNOWN. Do not claim the sender received a refund from this status alone. |
| FAILED | Provider reports an error | Stop UNKNOWN. Do not infer that no funds moved. |

Any other status or malformed response stops UNKNOWN. Terminal refusal states in the submit response also stop immediately.

SUCCESS must include the original origin-chain deposit hash. The actual input must equal the deposit. Actual output must meet `minAmountOut` and therefore the protected owner floor. There is no upper bound at the quoted `amountOut`: better fills are accepted only when the finalized on-chain amount matches that exact actual output. The existing historical NEAR-origin recording settled 133142 against a quote of 132894. Its regression reuses those recorded economics with a synthetic supported EVM route and receipt; the historical INTENTS delivery itself is not a supported EVM settlement. Unexpected deposited amounts, refund data or increased fees stop UNKNOWN.

The destination RPC must report the intended chain, a successful receipt and its canonical block at or below the finalized head. Its block timestamp must be at least the quote creation time rounded up to seconds, so an old transfer cannot prove this settlement. For ERC20 output, reconcile net Transfer credits from the exact token contract to the judged recipient, including any debits in the same receipt. Reject malformed, removed or duplicate logs and duplicate transaction hashes. For native output, require a direct transaction with the exact recipient, value and empty input. The reconciled amount must equal the provider's actual output. Explorer URLs are recorded as provider data and are never followed.

## Owner run

Use a normal PowerShell or cmd session with its usual HOME and USERPROFILE values, without the isolated test environment below. The registry belongs to that home. Overrides require interactive stdin, so Git Bash mintty and piped input cannot provide the required approval on the native Python path used here. This read-only diagnostic must show `True` and the usual home before an owner override:

```powershell
python -c "import sys; from pathlib import Path; print(sys.stdin.isatty()); print(Path.home())"
```

The owner must supply an existing funded mainnet account, its external Ed25519 FullAccess key file, a destination address they control, and a new external report path. Install PyNaCl into the interpreter used for the command before the spending run:

```powershell
python -m pip install pynacl
```

The credential file is a JSON object with `public_key` and `private_key`, each encoded with an `ed25519:` prefix and Base58 payload. The private payload contains the 32 byte seed followed by the 32 byte public key. The existing `secret_key` spelling is also accepted; two fields that disagree are refused. The reader verifies both public key copies and the seed-derived key. No key value belongs in the repository or the report.

near-api-js filesystem credentials also contain `account_id`, which must equal `--sender` when present. CLI-RS generated Ed25519 credentials can omit that field and include `implicit_account_id` instead. That implicit account is not treated as the named sender. The explicit `--sender` plus the on-chain FullAccess lookup binds the loaded public key to the sending account. Extra CLI metadata is not copied to the report. The Windows interpreter used for this lane has no PyNaCl installed, so real signing was not exercised. Public quote signature verification uses the repository's existing Ed25519 verifier and needs no additional package.

For a small run, hold at least 0.1 wNEAR on the registered sender and at least 0.01125 native NEAR for the local gas reserve plus maximum receiver registration. The destination in the example is Base USDC, verified in the recorded live token catalog with six decimals. The example owner floor is 250000 base units, or 0.25 USDC. It is a refusal threshold, not a promise of a current price. Replace the account, address and file path values below before running. Create the external report directory first.

```powershell
$sender = 'YOUR_MAINNET_ACCOUNT.near'
$recipient = 'YOUR_EVM_ADDRESS'
$destination = 'nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near'
$runDir = 'C:/Users/gudma/OneDrive/Desktop/GITHUB-FILES/siyab-oneclick-runs'
New-Item -ItemType Directory -Force -Path $runDir

python -m gate intents-settle --sender $sender --destination $destination --amount 100000000000000000000000 --recipient $recipient --min-amount-out 250000

python -m gate intents-settle --sender $sender --destination $destination --amount 100000000000000000000000 --recipient $recipient --min-amount-out 250000 --deposit --key-file 'C:/PATH/OUTSIDE/REPOSITORY/account.json' --out "$runDir/settlement-001.json" --polls 120

python -m gate intents-resume "$runDir/settlement-001.json" --polls 120
```

The second settlement command is the only command above that can move funds. It fetches and checks a new quote rather than reusing the preview. If any check fails, resolve the reported cause. Only proven `deposit_failed` or proven `deposit_expired` recovery permits a fresh run after an uncertain signed deposit. A `prepared` phase does not prove non-inclusion. For unresolved signed runs, follow the original report. To deliberately start another run while one remains unresolved, the owner must add `--override-report "$runDir/settlement-001.json"` to the new `intents-settle --deposit` command and use a new `--out` filename. This also works when the new output directory differs, or the old registered report is missing. Repeat the flag for every blocker. This records an explicit bypass; it does not prove that the old deposit failed or prevent it from settling. Base finality depends on L1 finality and batch inclusion and may need several resume runs, especially with a small `--polls` value or provider delays. Repeating `intents-resume` is the continuation path and never spends. At 30 second spacing, 60 receipt-wait polls span about 29.5 minutes and 120 span about 59.5 minutes, plus network time; these are polling bounds, not a finality guarantee.

For a stale reserved row, first try the command below. It checks proof and releases only that row. It cannot fund anything. It refuses an already successful deposit and any signed run whose failure or expiry cannot be proved. A new report is required for any later settlement.

```powershell
python -m gate intents-release --sender $sender --report "$runDir/settlement-001.json"
```

A Windows power cut can roll a report back to `ready` after broadcast, including loss of its transaction hash. The owner must check the chain before any `--override-report`, even when the old report says nothing was signed. Use this exact read-only sender lookup first:

```powershell
Invoke-RestMethod -Method Get -Headers @{'User-Agent'='siyab'} -Uri "https://api.nearblocks.io/v3/accounts/$sender/txns?signer=$sender&receiver=wrap.near&limit=10" | ConvertTo-Json -Depth 100
```

This is the same NearBlocks lookup run and printed automatically by `--override-report`. It reads at most the first ten matching transactions and is not a complete account history or proof of non-inclusion. Review earlier pages and the account's own transaction records when the relevant time is outside this page. An empty answer does not authorize release. Public round 5 reads returned HTTP 200 with the repository's `siyab` User-Agent. The earlier Python urllib default-agent HTTP 403 was an agent artefact, not the current response through the repository transport. All lane NearBlocks reads use that transport and agent. A failed lookup still requires the typed interactive confirmation described above before any key access. Ordinary runs without an override do not make this account lookup.

If the report retains its signed hash, also run this exact read-only archival status check, or use `intents-resume`:

```powershell
$record = Get-Content -LiteralPath "$runDir/settlement-001.json" -Raw | ConvertFrom-Json
$lookup = @{jsonrpc='2.0'; id='gate'; method='tx'; params=@{tx_hash=$record.deposit_hash; sender_account_id=$sender; wait_until='NONE'}} | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Post -Uri 'https://archival-rpc.mainnet.fastnear.com' -ContentType 'application/json' -Body $lookup | ConvertTo-Json -Depth 100
```

This POST only queries status. It has no signed payload and cannot broadcast. UNKNOWN_TRANSACTION by itself does not establish expiry. Let `intents-release` or `intents-resume` check the complete final-block, protocol-period, canonical-reference and key-nonce proof. A missing original report or hash with an unknown or consumed signing marker cannot establish unsigned release. Restore the signed report when possible. An explicit override preserves that unresolved row and still risks a second payment if the old transaction executed or can execute.

The first owner-funded EVM run must retain the allocated quote, terminal status and finalized receipt in the external report. Compare the net on-chain Transfer credit with `swapDetails.amountOut` and with `swapDetails.amountOut - withdrawFee`, retaining both quoted and actual fee fields. Equality with actual `amountOut` supports the SDK's documented net reading; equality only after subtracting the actual withdrawal fee supports a gross reading. With a zero fee the readings cannot be distinguished. Quoted and actual outputs can differ because of slippage or a better fill, so quoted `amountOut` alone cannot settle this question. This lane has no funded EVM receipt proving either reading. Reconciliation continues to require an exact match with the reported actual `amountOut`; a gross-only or otherwise inconsistent result stays UNKNOWN for review, without another deposit. The predeposit floor already reserves the quoted fee under either reading.

## Recorded evidence and limits

`fixtures/oneclick/api-contract.json` extracts the live OpenAPI v0.1.10 paths, schemas, required properties, enums and security schemes, now including `GET /v0/tokens` and `TokenResponse`. The source digest was unchanged when rechecked in this round. The actual catalog recording contains 232 assets. It binds `wrap.near` at 24 decimals and Base USDC at six decimals. It records the source digest and extraction method. The two dry recordings are actual read-only calls. One retains its public signature bytes and verifies against the published production key. Three independent official SDK staging vectors exercise dry, non-dry and normalized status signature verification.

The unknown-address 404, public SUCCESS and public REFUNDED responses are actual reads. The first two historical terminal recordings concern BTC to Solana routes. A further recorded SUCCESS is a historical NEAR-origin swap. None is evidence of a Siyab settlement. Funded flow and destination receipt tests use explicitly synthetic responses. An earlier round requested one read-only non-dry quote and recorded its verified public signature in `allocated-quote-20261121.json`. Rounds 3 and 4 added no recordings. A public NONE read of the historical direct transfer returned its full FINAL outcome. The round 4 positive regression models a known discovery without an outcome, followed by a faithfully built matching 50 Tgas `wrap.near.ft_transfer` in the recorded FINAL envelope and outcome structure. Its sender, nonce, key, hash, arguments and modeled signature bytes match the synthetic transaction built by the deposit encoder. It passes discovery through the actual RPC client and the complete deposit checks. The unmodified historical transaction separately retains its 300 Tgas refusal. A supported synthetic deposit also resumes through FINAL to proven settlement. No funds were sent, no real transaction was signed and no deposit notification was made.

| Verification | Recorded finding | Consequence |
| --- | --- | --- |
| Fresh receiver storage | `storage_balance_of` returned null on the freshly allocated address at 16:34:53 UTC. The historical NEAR deposit address was registered. | Funding must plan capped receiver registration. Missing registration is not an ALLOW assumption. |
| Registration cost | `storage_balance_bounds` returned minimum and maximum of `1250000000000000000000` yoctoNEAR. | The chosen maximum is 0.00125 NEAR, with a local 30 Tgas budget. Changed or malformed bounds stop UNKNOWN. |
| NEAR-origin status | `status-near-success-20261121.json` identifies `9CLCjpPN2tY6UE7HNd5e5eABLedzVrvXTvLUGJDwXgEq` in `originChainTxHashes`, absent from `nearTxHashes`. This historical swap delivered to INTENTS. | Match the original deposit in the origin list. The internal NEAR transaction list is not substituted. |
| Destination output semantics | The pinned official SDK's SwapDetails and Quote models describe `amountOut` as including the withdrawal fee. No funded EVM receipt in this lane establishes that reading in execution. | Reserve the fee in the owner floor before funding. The first live run must compare the exact Transfer amount, actual `amountOut` and withdrawal fee as described above. |
| Transaction expiry | A public mainnet config read returned protocol version 88 and `transaction_validity_period: 86400`. The matching nearcore source rejects a transaction only when the previous block height is strictly greater than its reference height plus the period. | Read the period dynamically at the final head. Require UNKNOWN_TRANSACTION and an unchanged ordinary key nonce, or a consumed nonce plus Fastnear archival UNKNOWN_TRANSACTION, NearBlocks indexed height at least reference plus period plus ten blocks, and independent hash absence. Read canonical reference blocks from the archive. |
| Historical direct transfer | `tx` in record mode returned FINAL and `SuccessValue` for `DzQkkA8RPM6crTH4iyhSYViwj71Ca7x3dXC52SMnwKYg`, a real `wrap.near.ft_transfer`. | Replay tests use the real envelope and reject its 300 Tgas action under the local 50 Tgas cap. This recording does not demonstrate a funded Siyab run. |

The catalog, three storage queries and two historical `tx` responses use the normal request-hash fixture filenames under `fixtures/oneclick/`. Each records its exact endpoint, method, parameters and read time. The second historical `tx` matches the provider's NEAR-origin deposit hash and shows the delegated DAO transaction that caused the transfer. Both were read with `rpc.Client.call` in record mode from the archival endpoint. Public transaction signatures, including nested delegated signatures, are stored losslessly as byte arrays to meet repository hygiene. All other response fields are unchanged. Replay uses the original method and parameters with networking refused.

Observed drift: the dry endpoint returned HTTP 201 while the OpenAPI documents 200. Status quote responses may omit the nested correlation id, normalize optional request fields and contain null fields declared as strings. `swapDetails.refundFee` appears in live answers but is absent from the schema. The actual observed dry `appFees` was empty; that does not establish zero platform fees.

The live path deliberately stops on missing quote fee fields, unsupported signature number serialization, unsupported assets, memos or custom messages. Native withdrawals through internal EVM calls cannot be proved by the direct-transfer reader. RPCs without finalized reads stop before funding. An unprovable destination receipt returns UNKNOWN. REFUNDED never claims an independently verified refund, because the provider does not identify its observed recipient or label a refund hash. Refund receipt reconciliation would need a separate chain-specific implementation.

## D5 partner access and terms

The auth guide points to the partner dashboard for credentials and documents `X-API-Key` plus partner JWT Bearer authentication. The unauthenticated read-only calls succeeded. This CLI currently uses that unauthenticated route and does not load a partner key. No partner account or production agreement was created or verified.

The current official quickstart describes an additional 0.25 percent unauthenticated fee; older committed documentation mentioned 0.2 percent. Do not use either prose value in payment arithmetic. The code binds token amounts, explicit fees and the owner's minimum output.

The API terms dated 7 Oct 2026 say API use accepts the terms. Section 4.1 restricts standalone API proxies, wrappers and gateways, and section 4.2 requires end user terms obligations. D5 remains an owner question: confirm partner eligibility and whether a hosted checked-payment product needs a written commercial arrangement or additional end user terms. This lane adds a local command, not a hosted settlement endpoint, and makes no claim that a future hosted product is cleared. No terms were accepted through a portal or form during the lane.

## Validation through round 3

* `python -m unittest tests.test_oneclick -q`: 99 tests passed on Windows with Python 3.12.10 and SQLite 3.49.1. Round 3 adds 16 tests covering directory fsync and its failure, Windows directory handling, signed prepared recovery, known discovery followed by FINAL, the existing landed transaction recording, registry blockers across directories and missing or rolled back reports, named overrides, unsigned and proven terminal release, separate signer accounts, failed revalidation of loaded terminal phases, failed final report saves, signing errors before a hash is saved, simultaneous runs, private home registry modes and a separate process refused by the actual SQLite lock. Existing expiry tests also check prepared reports on both sides of the strict validity boundary. Signer transport and funded responses are mocked; tests do not submit transactions. Test homes are isolated and do not create a real user registry.
* `wsl -d Ubuntu-24.04 --cd /mnt/c/Users/gudma/OneDrive/Desktop/GITHUB-FILES/siyab-oneclick-settle --exec python3 -m unittest tests.test_oneclick -q`: the same 99 tests passed under Python 3.12.3, exercising the POSIX fsync path on the host-mounted repository and native temporary directories.
* `python -m unittest tests.test_agent tests.test_api_doc tests.test_demo tests.test_hot_preflight tests.test_http tests.test_incident_replay tests.test_incidents tests.test_injected_model tests.test_intents tests.test_mcp tests.test_registry_args tests.test_res tests.test_rpc tests.test_scan tests.test_verdict tests.test_verify tests.test_verify_receipt tests.test_wasm tests.test_verify_guide -q`: 624 tests ran, 617 passed, six skipped and one failed. `tests.test_incident_replay.ReplayCase.test_partial_refresh_failure_preserves_every_published_byte` found the pre-existing `%TEMP%/.incident-record-g03jqtp_` folder in its shared parent-directory assertion at line 318. That folder's last write time preceded this run. It belongs outside this worktree and was not removed. This run covers every direct Intents and CLI import and the guide's indirect CLI subprocess checks. The expressly excluded `tests/test_registry_mainnet.py` was not run. Neither the full suite nor its nested smoke test was run, as the orchestrator owns the full suite.
* `python -m unittest tests.test_incident_replay -q` alone: 59 tests ran, 58 passed and the same shared TEMP assertion failed. Running that same module alone with an isolated temporary parent passed all 59 tests. No source or shared temporary folder was changed for the rerun. Across the 723 distinct selected tests, 717 have passing results and six were skipped, with the shared TEMP failure recorded above. The separate WSL settlement run adds 99 passing executions of the same settlement cases.
* The round 2 baseline ran 83 tests successfully before any round 3 edits. Nine selected round 3 regression methods failed before implementation, reproducing the incorrect prepared recovery instruction, skipped prepared blocker, absent home registry and concurrency control, absent directory sync and missing final outcome read. The final read-only code review found no remaining crash, concurrency or recovery issue in these changes.
* `python scripts/check_hygiene.py --all`: passed.
* `ruff check gate/oneclick.py gate/__main__.py tests/test_oneclick.py`: passed.
* `git diff --check` and both new commands' `--help`: passed.

The isolated replay run used this command:

```powershell
@'
import os
import sys
import tempfile
import unittest
from unittest import mock

with tempfile.TemporaryDirectory(prefix='siyab-oneclick-replay-') as directory:
    with mock.patch('tempfile.tempdir', directory), mock.patch.dict(os.environ, {'TEMP': directory, 'TMP': directory, 'TMPDIR': directory}):
        suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_incident_replay')
        result = unittest.TextTestRunner(verbosity=0).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
'@ | python -
```

Round 3 changes only `gate/oneclick.py`, `gate/__main__.py`, `tests/test_oneclick.py` and this document. The CLI change only expands the named override help to include the account registry. No recording was changed. The cumulative lane also has the earlier `gate/intents.py`, `gate/signer.py` and `tests/test_verdict.py` edits, plus the API contract extract and recordings under `fixtures/oneclick/`. The existing `tests/test_intents.py` is unchanged. No lane-excluded file was edited.

The named data files are `api-contract.json`, `dry-quote-20261121.json`, `signed-dry-quote-20261121.json`, `sdk-signature-vectors.json`, `status-404-20261121.json`, `status-success-20261121.json`, `status-refunded-20261121.json`, `status-near-success-20261121.json`, `allocated-quote-20261121.json` and `erc20-transfer.json`, plus the six request-hash recordings described above.

## Round 4 validation

The final source passed 118 settlement tests and 624 importer cases, with 618 importer passes and six skips. Across 742 distinct selected cases, 736 passed, six skipped and none failed. All subprocesses had TEMP, TMP, TMPDIR, HOME and USERPROFILE pointed at a fresh temporary directory. Every directory belonged to this run and was removed after its subprocess exited. No shared temporary folder was removed.

* `python -m unittest tests.test_oneclick -q`: 118 passed under Python 3.12.10 and SQLite 3.49.1. Round 4 adds 19 test methods and strengthens existing prepared, terminal, resume and landed deposit cases. Coverage includes one-row release, unsigned and legacy markers, final Failure, strict expiry, consumed nonce with archival absence, executed archival hashes, pending or unavailable proofs, failed report writes, rolled-back ready reports, an active unsigned release preventing later signing, override warnings and lookup failures, registry migration and stale release markers on resume. The final run took 7.923 seconds.
* The 19 importer modules listed in the round 3 command above ran again against the final source: 624 cases, 618 passed, six skipped, no failures or errors, in 180.996 seconds. AST inspection identified all 18 direct modules importing `gate.intents` or `gate.__main__`, in addition to `tests.test_oneclick`; `tests.test_verify_guide` adds its indirect CLI subprocess checks. The four py-near skips were in `tests.test_agent`, and the two missing PyNaCl skips were in `tests.test_mcp` and `tests.test_verdict`. Those dependencies were not installed during this round. The excluded `tests/test_registry_mainnet.py` and full suite were not run.
* Before implementation, 99 baseline settlement tests passed. Four selected new regression methods reproduced the missing release API and schema, consumed nonce refusal and skipped unsigned cleanup. A later two-method failing regression caught a stale `registry_released` report flag masking a failed resume; resume now drops that flag and CLI release success is scoped to the release command. Both checks pass in the final module run.
* The first combined run had 742 cases, six skips and one cleanup error in the new migration test. Its SQLite connection context committed without closing the handle, so Windows refused temporary directory removal. The test now closes that connection explicitly. A second combined run passed all 742 cases with six skips, then the final standalone settlement and importer runs above covered the remaining flag and exact encoded-argument assertions.
* `python scripts/check_hygiene.py --all`, `ruff check gate/oneclick.py gate/__main__.py tests/test_oneclick.py`, `git diff --check` and all three settlement command help paths passed. Only mocked signers and synthetic funded responses were used in tests. No real transaction or paid call was made.

The final importer harness was:

```powershell
@'
import os
import subprocess
import sys
import tempfile

modules = [
    'tests.test_agent', 'tests.test_api_doc', 'tests.test_demo', 'tests.test_hot_preflight',
    'tests.test_http', 'tests.test_incident_replay', 'tests.test_incidents',
    'tests.test_injected_model', 'tests.test_intents', 'tests.test_mcp',
    'tests.test_registry_args', 'tests.test_res', 'tests.test_rpc', 'tests.test_scan',
    'tests.test_verdict', 'tests.test_verify', 'tests.test_verify_receipt',
    'tests.test_wasm', 'tests.test_verify_guide',
]
program = "import json,sys,unittest; suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]); result=unittest.TextTestRunner(verbosity=0).run(suite); print(json.dumps({'ran':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':[(str(case),reason) for case,reason in result.skipped]})); sys.exit(0 if result.wasSuccessful() else 1)"
with tempfile.TemporaryDirectory(prefix='siyab-oneclick-r4-importers-final-') as directory:
    env = dict(os.environ, TEMP=directory, TMP=directory, TMPDIR=directory, HOME=directory, USERPROFILE=directory)
    result = subprocess.run([sys.executable, '-c', program, *modules], env=env,
                            capture_output=True, encoding='utf-8', errors='backslashreplace')
    print(result.stdout[-5000:])
    print(result.stderr[-1500:] if result.returncode == 0 else result.stderr)
sys.exit(result.returncode)
'@ | python -
```

The final settlement run used the same five isolated environment variables and `subprocess.run([sys.executable, '-m', 'unittest', 'tests.test_oneclick', '-q'], env=env)` in its own fresh directory.

## Round 4 source checks

* Re-read https://docs.near.org/api/rpc/transactions. `tx` accepts the original hash, sender and `wait_until`. NONE returns the current status and permits UNKNOWN_TRANSACTION; FINAL obtains the finalized outcome. Release and resume never pass a signed payload to this method.
* Re-read the pinned primary source at https://raw.githubusercontent.com/near/nearcore/0ad2bdb2630e85bbacecab2a3b77072c78b46bc6/runtime/runtime/src/verifier.rs, lines 254 through 280. Monotonic nonce validation rejects `tx_nonce <= current_nonce`. This establishes the consumed nonce condition, without claiming that the nonce alone proves non-execution.
* Re-read https://raw.githubusercontent.com/near/nearcore/0ad2bdb2630e85bbacecab2a3b77072c78b46bc6/chain/chain/src/store/utils.rs, lines 54 through 75. Transaction expiry uses strict greater than the reference height plus the validity period. The equality boundary remains refused.
* https://docs.near.org/api/rpc/providers identifies https://archival-rpc.mainnet.fastnear.com as a public mainnet archival endpoint. A direct round 4 read through the actual `_transaction_read` sent `tx` with `{"tx_hash":"11111111111111111111111111111111","sender_account_id":"wrap.near","wait_until":"NONE"}`. It returned JSON-RPC 2.0, matching id `gate`, HANDLER_ERROR, code minus 32000, cause UNKNOWN_TRANSACTION and that same `requested_transaction_hash`. No transaction was submitted and no recording was added.
* The official NearBlocks OpenAPI snapshot in `intel/seeds/raw/l2-nearblocks-openapi-20261115T140447Z.json` lists `GET /v3/accounts/{account}/txns` with `signer`, `receiver` and `limit` parameters, with a documented limit range of 1 through 100. Source: https://api.nearblocks.io/api-docs. The round 4 raw urllib default-agent refusal is superseded by successful round 5 HTTP 200 reads with `User-Agent: siyab`, using the repository transport. Both account ids are now explicit filters. The recorded response pins `data`, `signer_account_id`, `receiver_account_id`, `transaction_hash` and the nanosecond string `block_timestamp`. An unavailable or malformed page requires explicit interactive confirmation before key loading.

Round 4 modifies only `gate/oneclick.py`, `gate/__main__.py`, `tests/test_oneclick.py` and this document. No recording or lane-excluded file changed. The full suite, explicitly excluded mainnet registry module, funded owner run and partner onboarding remain outside this round. The owner must perform the funded run only after independent review and use the recovery commands above if an older reservation blocks it. The existing D5 and funded EVM output semantics questions remain open.

## Round 5 source checks and handoff

Round 5 changes only `.gitattributes`, `gate/oneclick.py`, `tests/test_oneclick.py`, this document and three new request-hash fixtures under `fixtures/oneclick/`. Earlier lane edits remain in the working tree. No excluded file, remote or tag was changed. No real credential, signature, transaction, deposit, paid call, commit, push or deployment was used.

The new recordings were obtained through `rpc.Client` with the repository's `siyab` User-Agent and existing NearBlocks request spacing. No key or alternate user agent was supplied. They retain the request, endpoint, read time and complete parsed response. The fixture filenames start with the prefixes below. Their full byte hashes are pinned in `tests/test_oneclick.py`. `.gitattributes` marks `fixtures/oneclick/**` as `-text`, and `git check-attr text` confirms that Git will preserve all three files' bytes.

| Fixture prefix | Read time on 21 Nov, UTC | Verified result |
| --- | --- | --- |
| `84c64267` | 20:34:35 | https://api.nearblocks.io/v3/txns/11111111111111111111111111111111 returned HTTP 200 with exactly `{"data":null}`. The supplied hash encodes 32 zero bytes. |
| `06919c0a` | 20:34:45 | https://api.nearblocks.io/v3/txns/DUzJv1DVj3tyEgewoKK8ihHaq72SrJ38xMYeMeGoyFNh returned HTTP 200 with a transaction object binding that hash, signer and receiver `wrap.near`, and historical height 40858545. |
| `c00bd682` | 20:34:55 | https://api.nearblocks.io/v3/accounts/wrap.near/txns?signer=wrap.near&receiver=wrap.near&limit=10 returned HTTP 200 with four matching historical rows. Timestamps are canonical 19 digit nanosecond strings. |

The live [NearBlocks OpenAPI](https://api.nearblocks.io/openapi.json), checked again in this round, documents the hash lookup, `data: null` for a missing transaction, both account filters, and error responses. The [published transaction route](https://github.com/Nearblocks/nearblocks/blob/27be10332a241a01b55467687c56f0dd5ff7761f/apps/api/src/routes/v3/txns.ts#L142-L163) and [handler](https://github.com/Nearblocks/nearblocks/blob/27be10332a241a01b55467687c56f0dd5ff7761f/apps/api/src/services/v3/txns/index.ts#L199-L210) confirm the object or null envelope. The [hash resolver](https://github.com/Nearblocks/nearblocks/blob/27be10332a241a01b55467687c56f0dd5ff7761f/apps/api/src/libs/txnAnchor.ts) reads the NearBlocks database. Its [timestamp-bounded exact-hash query](https://github.com/Nearblocks/nearblocks/blob/27be10332a241a01b55467687c56f0dd5ff7761f/apps/api/src/sql/queries/txns/txnCte.sql) and [historical search](https://github.com/Nearblocks/nearblocks/blob/27be10332a241a01b55467687c56f0dd5ff7761f/apps/api/src/libs/response.ts#L174-L224) describe the index lookup bounds. No completeness guarantee is inferred from null. The upstream commit is a source reference, not an attestation of the deployed revision.

The [account filter implementation](https://github.com/Nearblocks/nearblocks/blob/27be10332a241a01b55467687c56f0dd5ff7761f/apps/api/src/services/v3/accounts/txns.ts#L35-L95) and [pair-filter SQL](https://github.com/Nearblocks/nearblocks/blob/27be10332a241a01b55467687c56f0dd5ff7761f/apps/api/src/sql/queries/accounts/txns/cte.sql) explain why both signer and receiver must be explicit. The ten row sample remains bounded and does not establish complete account history.

The [official NEAR provider list](https://docs.near.org/api/rpc/providers) identifies Fastnear's archival endpoint. Both signed reference block reads now use it, with a regression where the ordinary endpoint has garbage-collected that block. The [transaction RPC documentation](https://docs.near.org/api/rpc/transactions) was also rechecked. Release still uses only hash and sender reads, never a signed payload.

Independent read-only review found one timestamp-unit problem before completion: a millisecond string could look older than every reservation when interpreted as nanoseconds. A failing regression reproduced key loading. The final validation requires the recorded canonical 19 digit nanosecond shape, with other widths refused as lookup failures. A further failing regression reproduced a partial provider-error envelope reaching key loading; those envelopes now require interactive confirmation as failed lookups. The supported timestamp range is intentionally explicit and fails closed on a future format change.

The owner must wait for the orchestrator's independent review and use the existing Owner run commands above for the first funded run. An override with a risky or unavailable lookup must be run in an interactive terminal and answered with `OVERRIDE ACCOUNT`; piping that text into stdin cannot approve it. No extra flag can suppress confirmation. No funded run was attempted here. The first funded EVM receipt and D5 partner or hosted-product terms questions remain open for the reasons already stated above.

## Round 5 validation

The final requested set ran 750 tests in 227.391 seconds: 744 passed, six skipped, zero failures and zero errors. Its 126 settlement tests all passed. The other 19 modules ran 624 cases, with 618 passes and six dependency skips. The subprocess had TEMP, TMP, TMPDIR, HOME and USERPROFILE set to one fresh temporary directory before Python started. That directory was removed only after the subprocess exited. No shared temporary directory or actual home registry was touched. The excluded `tests/test_registry_mainnet.py`, full suite and nested smoke were not run.

AST inspection identified every direct test import of `gate.intents`, `gate.__main__` or `gate.oneclick`. The existing guide module adds its indirect CLI subprocess checks. These are the exact final modules and case counts:

| Module | Cases | Skips |
| --- | --- | --- |
| `tests.test_agent` | 52 | 4 |
| `tests.test_api_doc` | 27 | 0 |
| `tests.test_demo` | 9 | 0 |
| `tests.test_hot_preflight` | 1 | 0 |
| `tests.test_http` | 27 | 0 |
| `tests.test_incident_replay` | 59 | 0 |
| `tests.test_incidents` | 22 | 0 |
| `tests.test_injected_model` | 42 | 0 |
| `tests.test_intents` | 119 | 0 |
| `tests.test_mcp` | 36 | 1 |
| `tests.test_oneclick` | 126 | 0 |
| `tests.test_registry_args` | 9 | 0 |
| `tests.test_res` | 5 | 0 |
| `tests.test_rpc` | 64 | 0 |
| `tests.test_scan` | 10 | 0 |
| `tests.test_verdict` | 53 | 1 |
| `tests.test_verify` | 17 | 0 |
| `tests.test_verify_receipt` | 62 | 0 |
| `tests.test_wasm` | 6 | 0 |
| `tests.test_verify_guide` | 4 | 0 |

The four agent skips require py-near. The MCP and verdict skips require PyNaCl. Neither package was installed during this round. These are the same dependency skips as round 4.

Before implementation, all 118 baseline settlement tests passed. Three new regression methods failed against the old code, reproducing pruned reference reads, missing independent absence checks and missing reservation timestamps. The provider partial-error and millisecond timestamp regressions also failed before their fixes. The final module adds eight test methods and strengthens existing cases. Coverage includes independent sources agreeing, disagreeing or unavailable, retained reports and reservations on refusal, archival canonical reference reads after ordinary-node garbage collection, raw fixture bytes and replay, the actual transport User-Agent, every reservation cutoff, equal timestamps, legacy timestamps, failed lookups, typed acceptance, wrong answers, EOF and non-interactive refusal before key reads.

A standalone final settlement run passed all 126 cases in 8.151 seconds. An earlier combined run passed 749 cases with six skips before the final timestamp regression was added; the 750 case final run above supersedes it. Independent read-only review found no remaining blocker after that fix. Ruff, `git diff --check` and the three fixture attribute checks passed. Both final repository gates passed: `python scripts/check_hygiene.py --all` and `python scripts/check_citations.py`.

## Round 6 source checks and handoff

Round 6 changes only `gate/oneclick.py`, `tests/test_oneclick.py`, this document and one new request-hash fixture under `fixtures/oneclick/`. Earlier lane edits remain untouched. No excluded file, remote or tag was changed. No real credential, signature, transaction, deposit, paid call, commit, push or deployment was used.

The live [NearBlocks OpenAPI](https://api.nearblocks.io/openapi.json), read on 21 Nov, defines `GET /v3/blocks/latest`, an optional `limit` from 1 through 10, newest blocks first, and a five second cache. Its `blocks.blocks` response requires a `data` member containing an array or null. Block rows include decimal string `block_height`, Base58 `block_hash`, and string `block_timestamp`. The implementation requests one row and refuses null or incomplete evidence. The same read verified the documented shape against the live endpoint before implementation.

At 21:07:33 UTC, a public read of https://api.nearblocks.io/v3/blocks/latest?limit=1 through `rpc.Client` returned HTTP 200, one row and indexed height `225608394`. The complete parsed response, URL, parameters and time are recorded in the request-hash fixture with prefix `9869e995` under `fixtures/oneclick/`. Its SHA256 has prefix `a14dc825`; the full byte hash is pinned in `tests/test_oneclick.py`. The existing `fixtures/oneclick/** -text` attribute preserves those bytes. Replay and the actual transport User-Agent are tested. A separate read of https://api.nearblocks.io/v3/txns/not-a-transaction returned HTTP 200 with exactly `{"data":null}`, confirming why local hash validation must precede the provider read. Neither GET can submit a transaction.

The ten indexed block margin is a local policy buffer beyond the complete signed validity horizon. It is measured in blocks and does not convert five seconds into a guaranteed number of blocks. Latest block height plus the subsequent exact hash miss bounds the observed indexer progress; it does not attest complete historical indexing. Archive and indexer observations remain trusted inputs.

The [Python Path.home documentation](https://docs.python.org/3.12/library/pathlib.html#pathlib.Path.home) confirms home resolution through `os.path.expanduser`. [Python expanduser documentation](https://docs.python.org/3.12/library/os.path.html#os.path.expanduser) describes USERPROFILE on Windows and HOME on Unix. The [mintty compatibility notes](https://github.com/mintty/mintty/wiki/Tips#inputoutput-interaction-with-alien-programs) describe native Windows console interoperability and ConPTY variations. The operator requirement here is direct interactive PowerShell or cmd, with the normal home and no HOME or USERPROFILE overrides. The diagnostic above makes both conditions reviewable before an override. No interactive owner override was performed in this round.

The owner must wait for independent orchestrator review. No funded command is needed to verify these fixes. The earlier Owner run commands remain the separately authorized path for a first actual settlement. Signed or ambiguous overrides always require the typed `OVERRIDE ACCOUNT` confirmation even with an empty transaction page. The first funded EVM receipt and D5 partner terms questions remain open; this lane is expressly prohibited from spending, signing or making paid calls.

## Round 6 validation

The final requested set ran 755 cases in 268.213 seconds: 749 passed, six skipped, zero failures and zero errors. All 131 settlement cases passed. The other 19 modules ran 624 cases with 618 passes and six skips. A standalone settlement run also passed all 131 in 12.146 seconds. The baseline before any round 6 edits passed all 126 cases in 16.951 seconds. New failing regressions reproduced the absent signing prompt, skipped clock margin, unvalidated hash reads and missing indexer horizon before the fixes.

AST inspection again identified the 19 direct importers of `gate.intents`, `gate.__main__` or `gate.oneclick`, including settlement tests. The guide module adds indirect CLI execution. These are the exact final modules and counts:

| Module | Cases | Skips |
| --- | --- | --- |
| `tests.test_agent` | 52 | 4 |
| `tests.test_api_doc` | 27 | 0 |
| `tests.test_demo` | 9 | 0 |
| `tests.test_hot_preflight` | 1 | 0 |
| `tests.test_http` | 27 | 0 |
| `tests.test_incident_replay` | 59 | 0 |
| `tests.test_incidents` | 22 | 0 |
| `tests.test_injected_model` | 42 | 0 |
| `tests.test_intents` | 119 | 0 |
| `tests.test_mcp` | 36 | 1 |
| `tests.test_oneclick` | 131 | 0 |
| `tests.test_registry_args` | 9 | 0 |
| `tests.test_res` | 5 | 0 |
| `tests.test_rpc` | 64 | 0 |
| `tests.test_scan` | 10 | 0 |
| `tests.test_verdict` | 53 | 1 |
| `tests.test_verify` | 17 | 0 |
| `tests.test_verify_receipt` | 62 | 0 |
| `tests.test_wasm` | 6 | 0 |
| `tests.test_verify_guide` | 4 | 0 |

Before starting the test interpreter, the parent created a fresh temporary directory and set TEMP, TMP, TMPDIR, HOME and USERPROFILE to that directory in the child environment. The test loader loaded the listed modules into one unittest suite and reported their case counts and skips. The temporary directory was removed only after the subprocess exited. The normal shell environment and real home registry were not changed. Four skips need py-near, and the MCP and verdict skips need PyNaCl. Neither dependency was installed. The expressly excluded `tests/test_registry_mainnet.py`, full suite and nested smoke were not run.

The five added test methods and strengthened existing cases cover attempted and unknown signing with an empty index page before key reads, an unsigned row with old activity and no prompt, every overridden reservation at the skew cutoff minus one nanosecond, equality and plus one nanosecond, both indexed height boundaries, invalid transaction hashes before provider reads, malformed and unavailable latest block replies, and unchanged report bytes and registry rows on refused release. The new public fixture is replayed with networking refused and its exact bytes are pinned. The three NearBlocks routes retain the repository User-Agent. Independent read-only review found no remaining blocker.

Ruff, `git diff --check` and the fixture attribute check passed. No lane-excluded file changed.

## Round 7 fix and source checks

Round 7 changes only `gate/oneclick.py`, `tests/test_oneclick.py` and this document. Earlier lane edits remain in the working tree. No excluded file, recording, remote or tag was changed. No real credential, signature, transaction, deposit, paid call, commit, push or deployment was used.

The override deletes only the explicitly named zero marker rows before inserting its new reservation, under the existing `BEGIN EXCLUSIVE` transaction. The signing update requires the original report and a zero marker. An update matching no row refuses before `_signed_deposit`. Unsigned cleanup also requires a zero marker, so a refused older invocation cannot remove a retained signed or unknown reservation.

The [SQLite transaction documentation](https://www.sqlite.org/lang_transaction.html#deferred_immediate_and_exclusive_transactions) confirms that an exclusive transaction starts a write immediately and excludes other connections in DELETE journal mode. Its transaction control section confirms that the explicit transaction persists until commit or rollback and rolls back when the connection closes. The [Python 3.12 Cursor.rowcount documentation](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Cursor.rowcount) confirms the modified row count for a completed UPDATE. Python 3.12.10 with SQLite 3.49.1 was verified locally. The real registry regression verifies the missing and nonzero row refusal against that installed runtime. This round changes no external payment API and makes no live RPC request.

The owner needs only independent orchestrator review for this fix. No funded command is required to verify the race. The earlier first funded EVM receipt and D5 partner terms questions remain open and outside this round.

## Round 7 validation

`python -m unittest tests.test_oneclick -v` passed all 132 cases in 12.674 seconds. The added regression starts live run A on a worker thread and pauses its saved ready report before the signing transition. Live run B takes the explicit override from another output directory, passes the existing confirmation rules, signs through a mock and reaches a synthetic uncertain broadcast. A then resumes and refuses without signing or broadcasting. The actual SQLite registry remains in use. The five subcases exercise marker zero, one, null, negative one and text. Only zero is deleted; every other marker requires the typed prompt and stays reserved even after A refuses. B's signed reservation remains in every subcase. Before the production fix, this one test failed all five subcases because A's mock signer was called once after B had signed.

The other 19 modules named in the round 6 table ran 624 cases in 230.548 seconds: 618 passed, six skipped, zero failures and zero errors. AST inspection identified all 18 direct importers of `gate.intents` or `gate.__main__` apart from `tests.test_oneclick`; `tests.test_verify_guide` adds its indirect CLI checks. Across the 20 selected modules, 756 distinct cases ran with 750 passes and six skips. Four skips require py-near, and the MCP and verdict skips require PyNaCl. Those packages were not installed. The excluded `tests/test_registry_mainnet.py`, full suite and nested smoke were not run.

The existing clock cutoff test recreates both unsigned reservations for each independent case because an override now revokes them. Its original nanosecond and skew assertions remain in force.

Each test subprocess verified TEMP, TMP, TMPDIR, HOME and USERPROFILE against its own fresh directory before loading tests. Both `Path.home()` and `tempfile.gettempdir()` resolved there. Each directory was removed only after its subprocess exited. No actual home registry or shared temporary folder was touched. Signers, network reads, funding responses and broadcast results in the new regression are mocked. The existing offline test fixture refuses unmocked networking.

`python scripts/check_hygiene.py --all` and `python scripts/check_citations.py` passed with zero findings. `ruff check gate/oneclick.py tests/test_oneclick.py` and `git diff --check` also passed. No funded run is needed for this fix; independent orchestrator review remains the next step.

## Round 8 fix and source checks

Round 8 changes only `gate/oneclick.py`, `tests/test_oneclick.py` and this document. Earlier lane changes remain in the working tree. No excluded file, recording, remote or tag was changed. No real key, signature, transaction, deposit, paid call, commit, push or deployment was used.

The override now retains each matched registry row's stored report string and uses that exact string for deletion. Every overridden row with a zero signing marker must delete exactly one row. Any other rowcount refuses before the replacement report, key read or network access. The exclusive transaction rolls back earlier deletions when a later deletion fails. Signed and unknown rows retain the existing confirmation rules and remain reserved.

All registry paths now use one resolved absolute string, with `str.casefold()` applied on Windows and case preserved on POSIX. Blocker comparisons, inserts, signing transitions, cleanup, release and resume use that same representation. Existing stored paths normalize under the exclusive lock without changing signing markers or reservation times. A collision between legacy spellings fails at the primary key constraint, rolls back and preserves both rows for owner review. It never merges away a signed or unknown reservation.

The native Windows regression pauses A with a saved `Run.json` before signing, removes that report, then lets B override the missing path as `run.json` from another directory. B alone reaches its mock signer and synthetic uncertain broadcast. A resumes with no signature or broadcast and B's signed reservation remains. This exact mixed case subcase failed before the fix because A also signed. The five earlier marker subcases remain in the same threaded test.

Four new methods cover a real SQLite trigger suppressing the second unsigned deletion, canonical storage inspected through a raw connection before any migration can mask it, resume and release with equivalent paths, legacy normalization and preservation of both rows on a canonical collision. The failed deletion test proves the first deletion rolls back, no replacement report exists, and neither key reading nor network access occurs. Before the production changes, the four selected regression methods all failed, including the mixed case race and a key read after the ignored deletion. Existing registry assertions now inspect canonical keys; report display paths keep their supplied spelling.

Primary sources read in this round:

* [Path comparison semantics](https://docs.python.org/3.12/library/pathlib.html#general-properties): Windows path equality respects case folding. The installed Python 3.12.10 source and a local equality check confirmed the behavior.
* [Path.resolve](https://docs.python.org/3.12/library/pathlib.html#pathlib.Path.resolve): produces an absolute path, resolves symlinks and removes parent components, including non-strict handling of missing reports.
* [str.casefold](https://docs.python.org/3.12/library/stdtypes.html#str.casefold): supplies the caseless string representation used by the Windows registry keys.
* [Cursor.rowcount](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Cursor.rowcount): reports modified rows for a completed DELETE. Local SQLite 3.49.1 also confirmed that a differently cased text deletion matches zero rows.
* [SQLite transactions](https://www.sqlite.org/lang_transaction.html#deferred_immediate_and_exclusive_transactions): the existing exclusive transaction excludes other connections in DELETE journal mode; closing an uncommitted transaction rolls it back.
* [SQLite RAISE](https://www.sqlite.org/lang_createtrigger.html#the_raise_function): RAISE(IGNORE) abandons the triggering statement without rolling back earlier changes. The regression uses this to require explicit rowcount refusal and transaction rollback.

The owner needs independent orchestrator review for this fix. No funded command is needed. The earlier first funded EVM receipt and D5 questions remain separate owner work. A legacy path collision deliberately stays blocked for review rather than choosing which reservation to discard.

## Round 8 validation

The final requested set has 760 distinct cases: 754 passed, six skipped, zero failures and zero errors. `tests.test_oneclick` passed all 136 cases in 9.272 seconds. The other 19 modules named in the round 6 table ran 624 cases in 176.797 seconds, with 618 passes and six skips. AST inspection again identified all 18 direct Intents and CLI importers apart from settlement; the guide module adds indirect CLI checks. Four skips require py-near and two require PyNaCl. Neither package was installed. The excluded `tests/test_registry_mainnet.py`, full suite and nested smoke were not run.

Each test subprocess started with TEMP, TMP, TMPDIR, HOME and USERPROFILE pointing to its own fresh temporary directory. Before loading tests, it asserted those five values plus the actual `Path.home()` and `tempfile.gettempdir()`. Each directory was removed after its subprocess exited. No normal home registry or shared temporary directory was changed. All 132 baseline settlement tests passed before edits. The four selected regressions failed before the production fix, then passed after it. The final suite also includes the canonical collision regression and the strengthened raw storage assertion.

`python scripts/check_hygiene.py --all` and `python scripts/check_citations.py` passed with zero findings. `ruff check gate/oneclick.py tests/test_oneclick.py`, syntax parsing and diff whitespace checks, including the untracked lane files against the round 7 snapshot, also passed.

## Sources verified

* Live contract: https://1click.chaindefuser.com/docs/v0/openapi.yaml
* Flow and statuses: https://docs.agentsonintents.com/integration/distribution-channels/1click-api/quickstart/making-a-request
* Mainnet only: https://docs.agentsonintents.com/integration/distribution-channels/1click-api/quickstart/introduction
* NEAR funding: https://github.com/near/agent-skills/blob/main/skills/near-intents/rules/deposit-near.md
* Signature method: https://docs.near-intents.org/integration/distribution-channels/1click-api/verify-quote-signature
* Pinned verifier and public key: https://raw.githubusercontent.com/defuse-protocol/one-click-sdk-typescript/ae8b24e064785c8cba8081263e03e52fed4de089/src/quote-signature.ts
* Pinned signature vectors: https://raw.githubusercontent.com/defuse-protocol/one-click-sdk-typescript/ae8b24e064785c8cba8081263e03e52fed4de089/src/__tests__/quote-signature.test.ts
* NEAR transaction encoding: https://raw.githubusercontent.com/near/nearcore/master/core/primitives/src/transaction.rs
* NEAR action encoding: https://raw.githubusercontent.com/near/nearcore/master/core/primitives/src/action/mod.rs
* NEAR public key and signature encoding: https://raw.githubusercontent.com/near/nearcore/master/core/crypto/src/signature.rs
* Borsh encoding: https://borsh.io/
* NEAR broadcast and status: https://docs.near.org/api/rpc/transactions
* NONE and FINAL milestones: https://docs.near.org/api/rpc/transactions#the-wait_until-field
* SQLite exclusive transactions: https://www.sqlite.org/lang_transaction.html#deferred_immediate_and_exclusive_transactions
* SQLite EXTRA and DELETE durability: https://sqlite.org/pragma.html#pragma_synchronous
* Python 3.12 SQLite connection and lock timeout: https://docs.python.org/3.12/library/sqlite3.html#sqlite3.connect
* Python file fsync and Windows CRT behavior: https://docs.python.org/3.12/library/os.html#os.fsync
* Windows CRT directory open refusal: https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-wopen?view=msvc-170. The documented EACCES directory case was confirmed locally as PermissionError with errno 13.
* Parent directory fsync requirement: https://man7.org/linux/man-pages/man2/fsync.2.html
* Protocol config method and block pinning: https://docs.near.org/api/rpc/protocol
* Protocol config validity field: https://docs.near.org/api-reference/post-experimental_protocol_config
* Mainnet protocol and unknown-transaction reads: https://free.rpc.fastnear.com with `EXPERIMENTAL_protocol_config`, `{"finality":"final"}`, and `tx`, `{"tx_hash":"11111111111111111111111111111111","sender_account_id":"wrap.near","wait_until":"NONE"}`. Config returned mainnet, protocol 88 and period 86400. The transaction read returned HANDLER_ERROR, UNKNOWN_TRANSACTION and the requested hash, without a result. No transaction was submitted.
* Matching nearcore validity comparison: https://github.com/near/nearcore/blob/0ad2bdb2630e85bbacecab2a3b77072c78b46bc6/chain/chain/src/store/utils.rs#L54-L75
* Matching nearcore nonce validation: https://github.com/near/nearcore/blob/0ad2bdb2630e85bbacecab2a3b77072c78b46bc6/runtime/runtime/src/verifier.rs#L252-L280
* Accepted transaction nonce update: https://github.com/near/nearcore/blob/0ad2bdb2630e85bbacecab2a3b77072c78b46bc6/runtime/runtime/src/lib.rs#L348-L352
* Regular access-key readdition nonce: https://github.com/near/nearcore/blob/0ad2bdb2630e85bbacecab2a3b77072c78b46bc6/runtime/runtime/src/access_keys.rs#L230-L241
* Root versus returned-promise execution status: https://github.com/near/nearcore/blob/0ad2bdb2630e85bbacecab2a3b77072c78b46bc6/core/primitives/src/transaction.rs#L588-L603
* Failed receipt rollback: https://github.com/near/nearcore/blob/0ad2bdb2630e85bbacecab2a3b77072c78b46bc6/runtime/runtime/src/lib.rs#L1092-L1105
* Attached-deposit refunds: https://github.com/near/nearcore/blob/0ad2bdb2630e85bbacecab2a3b77072c78b46bc6/runtime/runtime/src/lib.rs#L1310-L1319
* Atomic actions in a receipt: https://docs.near.org/protocol/transactions/transaction-anatomy#actions
* NEP141 transfer: https://github.com/near/NEPs/blob/master/neps/nep-0141.md
* Token storage registration: https://github.com/near/NEPs/blob/master/neps/nep-0145.md
* EVM receipts, transactions, block fields and finality: https://ethereum.org/developers/docs/apis/json-rpc/
* ERC20 event: https://eips.ethereum.org/EIPS/eip-20
* Event ABI encoding: https://docs.soliditylang.org/en/latest/abi-spec.html#events
* Event topic verification: https://ethereum-rpc.publicnode.com with `web3_sha3`; exact parameters and answer are in `erc20-transfer.json`.
* Public provider SUCCESS: https://1click.chaindefuser.com/v0/status?depositAddress=bc1qylrmm04t439vnqcmfu9ry9hgpq7d7nn7zl9q0m
* Public provider REFUNDED: https://1click.chaindefuser.com/v0/status?depositAddress=bc1qh24lnnls8fpp90vttch8zwdkeetvvkdfnt3rry
* Credentials: https://docs.near-intents.org/integration/distribution-channels/1click-api/authentication.md
* Partner dashboard: https://partners.near-intents.org/
* Terms: https://docs.near-intents.org/security-compliance/terms-of-service.md
* Signing library: https://pynacl.readthedocs.io/en/latest/signing/
* Installing PyNaCl: https://pynacl.readthedocs.io/en/latest/install/
* Current near-api-js credential reader: https://raw.githubusercontent.com/near/near-api-js/999eeee097eeeff1d658f8ed0ef0d96c6e6568f7/src/signers/legacy_keystore.ts
* near-api-js credential writer and legacy alias: https://raw.githubusercontent.com/near/near-api-js/v0.45.1/src/key_stores/unencrypted_file_system_keystore.ts
* CLI-RS private key field: https://raw.githubusercontent.com/near/near-cli-rs/cfbc0a6ce8b259f452ac6a76ad5a16f83b438caf/src/transaction_signature_options/mod.rs
* CLI-RS generated credential metadata and file writer: https://raw.githubusercontent.com/near/near-cli-rs/cfbc0a6ce8b259f452ac6a76ad5a16f83b438caf/src/common/mod.rs
* Published wrap.near registration behavior: https://raw.githubusercontent.com/near/core-contracts/1b0436c96379c63aee7d2429c6d5d24019031ab8/w-near/README.md
* Published wrap.near wrapping behavior: https://raw.githubusercontent.com/near/core-contracts/1b0436c96379c63aee7d2429c6d5d24019031ab8/w-near/src/w_near.rs
* Token standard transfer registration check: https://raw.githubusercontent.com/near/near-sdk-rs/3.1.0/near-contract-standards/src/fungible_token/core_impl.rs
* Token standard registration bounds and refunds: https://raw.githubusercontent.com/near/near-sdk-rs/3.1.0/near-contract-standards/src/fungible_token/storage_impl.rs
* Same-contract batch rollback: https://docs.near.org/smart-contracts/anatomy/actions
* Actual net output and hash fields: https://raw.githubusercontent.com/defuse-protocol/one-click-sdk-typescript/ae8b24e064785c8cba8081263e03e52fed4de089/src/models/SwapDetails.ts
* Quoted net output: https://raw.githubusercontent.com/defuse-protocol/one-click-sdk-typescript/ae8b24e064785c8cba8081263e03e52fed4de089/src/models/Quote.ts
* Recorded NEAR-origin SUCCESS: https://1click.chaindefuser.com/v0/status with the exact `depositAddress` in `status-near-success-20261121.json`.
* Historical NEAR deposit discovery: https://github.com/NEAR-DevHub/trezu/issues/377
* Token catalog: https://1click.chaindefuser.com/v0/tokens
* Recorded storage reads: https://free.rpc.fastnear.com with `query`; exact parameters and answers are in the request-hash recordings.
* Recorded historical transaction reads: https://archival-rpc.mainnet.fastnear.com with `tx`; exact parameters and answers are in the request-hash recordings.
* Base finalized blocks depend on L1 finality and inclusion: https://docs.base.org/base-chain/specs/protocol/consensus/derivation
