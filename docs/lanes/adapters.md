# Check adapters v2

22 Nov 2026, fix round 1. Work stays on `feat/check-adapters-v2` at `b4eb8668c`, whose merge base with the existing local `origin/main` is `6d028041c`. The worktree was already on this branch and base when this round started; no history or remote was changed. No commit, push, deployment, transaction or paid model call was made. The submission tag was not changed.

## Decision handling

Both adapters require an explicit allow decision to continue without confirmation. Every other decision needs a person's literal true response, except deny, which always blocks. Confirmation receives the receiver account. A missing outcome, unreadable response, failed request or incomplete evidence cannot produce allow.

A top level DENY blocks. When the evidence hash matches, either an evidence verdict of DENY or any signal check also blocks before subject and outcome comparisons. A changed top level outcome therefore cannot turn that denial into a confirmable ask. Displayed reasons come only from hash matching evidence or fixed adapter text. Top level reasons are never a fallback. The hash checks consistency and does not authenticate the service or authorise a Gate payment.

The Python adapter validates the full account id and bounds the decimal amount before integer conversion. Amounts follow the hosted route's canonical positive u128 contract. Payment evidence must explicitly include token and a null memo_sha256; a native token must be explicitly null. Decision parsing runs inside the request error boundary, including malformed Unicode evidence. Excessive evidence nesting is unreadable evidence; it cannot downgrade a top level DENY into a confirmable ask.

LangChain's guard_payment with amount=None always asks, even after an account ALLOW. An account DENY still blocks before confirmation. This adapter describes one receiver, token and amount; it has no FunctionCall, memo or msg input. The send callback must perform only that transfer. Additional payees or contract calls require their own guarded checks. The tool text displays evidence reasons; it validates checks but does not print them.

## Wallet transaction handling

The supported action format is Wallet Selector's tagged Transfer and FunctionCall format. Transfer has only deposit in its params; its recipient is the transaction's receiverId. For FunctionCall, receiverId is the called contract and can differ from the payment recipient. Every transaction must supply a valid receiverId, even where a wallet could otherwise supply a default. Unsupported action types, invalid transfer fields and invalid deposit fields raise SIYAB_PAYEE before a signing call. Unreadable function arguments require ASK after known checks. A valid action elsewhere in the same transaction or batch does not excuse an invalid action.

The wrapper checks each transaction receiver with the account route. Every positive Transfer deposit and FunctionCall attached deposit also needs a verdict for that exact yoctoNEAR amount and token=near. There is no account-only amount exception, including for one yoctoNEAR. A zero deposit has no positive native payment to check. Account checks cannot inherit unrelated amount or token options.

For ft_transfer and ft_transfer_call, the token contract is the transaction receiverId and the token payee is args.receiver_id. The wrapper sends that token, payee and amount to the verdict route. Every FunctionCall also scans nested objects and arrays for receiver_id, account_id, beneficiary_id, recipient, recipient_id, to, new_owner_id and owner_id, camelCase and plural variants, and receiver, beneficiary, destination, payee, target and owner aliases. Owner fields are conservatively treated as possible targets. Every named account needs an explicit ALLOW or confirmed ASK. A DENY always blocks, including for mt_transfer, ft_withdraw, nft_transfer and nft_transfer_call. Only the two FT methods have a supported transfer shape. The allowlist of other methods known to have no recipients is empty, so every other method requires confirmation after its known payees have been checked.

Every account a non-empty ft_transfer_call msg names in those recipient fields, or a msg that is itself an account id, is checked, and a non-empty msg then always requires ASK confirmation, since unknown fields can name onward recipients. The parser also traverses JSON objects and arrays encoded inside string fields. Opaque non-empty msg, payload, args, data, message or instructions fields require ASK. Unreadable or accountless messages and malformed recipient fields require ASK. An empty msg string adds no onward check; a missing or non-string msg requires confirmation. A supplied msg on another FunctionCall gets the same inspection. Reused object references do not hide a later recipient field. The hosted verdict route has no memo or msg query parameter; local parsing adds account checks, not a contract simulation or a memo verdict.

The tagged format is the Action union in core 8.10.2 and the InternalAction union in core 10.1.4. Core 10.1.4 also exports Action from the NEAR JavaScript transaction package. Other action representations are not inferred or silently skipped.

Arguments accept a plain object, JSON text, UTF-8 JSON bytes or base64 JSON text or bytes. Uint8Array text is tried as JSON before base64. Arrays, ArrayBuffers and malformed UTF-8 require ASK. Shared byte backing is rejected because the transaction cannot be isolated from later changes. Sparse transaction batches are rejected too. The wrapper snapshots transaction fields before asynchronous checks. Ordinary wallet methods are bound to their original wallet; getAccounts, signOut and isSignedIn are exercised. Other methods containing sign or delegate, and verifyOwner, are blocked. The two send methods use the guarded path.

Wallet Selector 8.10.2 signIn takes contractId and optional methodNames. The guarded signIn snapshots the parameters, checks contractId through the account route, refuses DENY and requires literal true confirmation for ASK. Optional wallet specific fields remain in the snapshot. It cannot pass through unchecked as an ordinary lifecycle method.

## Native amount verification

Repository contracts were checked in docs/API.md, intel/live.py and gate/verdict.py. LiveAPI._verdict maps token=near to kind near, carries the exact amount into the request and evidence, and emits token=null and memo_sha256=null. A read-only [hosted native verdict](https://siyab.xyz/live/verdict?receiver=intents.near&amount=2&token=near) returned HTTP 200 on 22 Nov 2026 at 06:16:58 UTC. Its outcome was ALLOW, signing was never and its evidence subject named intents.near, amount 2, kind near, token null and memo_sha256 null. This verifies native amount support without a transaction or signing call.

## Recorded ASK

`integrations/langchain/fixtures/verdict-ask.json` is the response to [the hosted payment verdict](https://siyab.xyz/live/verdict?receiver=aisthetic.near&amount=1&token=near), recorded on 22 Nov 2026. Its evidence fetch times run from 02:34:09Z to 02:34:10Z. It judges aisthetic.near receiving one yoctoNEAR. The outcome is ASK and the evidence verdict is UNKNOWN because contract_source_metadata returned a compilation preparation deserialization error. No check is a signal. The response bytes and evidence string are retained, with a final newline added to the file. Its SHA-256 was verified when recording.

## Live CORS result

Read-only curl GET requests sent `Origin: https://app.mynearwallet.com`. All four returned HTTP 200, exactly one `Access-Control-Allow-Origin: *`, `Access-Control-Expose-Headers: Retry-After` and `Cache-Control: no-store`:

| URL | Response Date on 22 Nov 2026, UTC |
| --- | --- |
| [Account receiver](https://siyab.xyz/live/account-check/intents.near) | 02:33:16 |
| [Native payment verdict](https://siyab.xyz/live/verdict?receiver=hello.near&amount=1&token=near) | 02:33:18 |
| [Token contract account](https://siyab.xyz/live/account-check/usdt.tether-token.near) | 02:34:46 |
| [Token payment verdict](https://siyab.xyz/live/verdict?receiver=intents.near&amount=1000000&token=usdt.tether-token.near) | 02:34:48 |

These headers permit the adapter's anonymous cross-origin GETs with Accept: application/json. The adapter does not request credentials or add nonstandard request headers. No preflight is needed for that request shape. This checks the observed HTTP responses, not a production browser wallet session, future service availability or unobserved proxy error responses. No CORS configuration change is needed for these observed calls.

Repeat a probe without reading a key or sending a transaction:

```sh
curl -i -H 'Origin: https://app.mynearwallet.com' 'https://siyab.xyz/live/account-check/intents.near'
curl -i -H 'Origin: https://app.mynearwallet.com' 'https://siyab.xyz/live/verdict?receiver=intents.near&amount=1000000&token=usdt.tether-token.near'
```

## Primary sources checked

- [Published Wallet Selector 8.10.2 transaction declarations](https://unpkg.com/@near-wallet-selector/core@8.10.2/src/lib/wallet/transactions.types.d.ts) and [the matching transactions.types.ts source](https://raw.githubusercontent.com/near/wallet-selector/v8.10.2/packages/core/src/lib/wallet/transactions.types.ts): Transfer params contain deposit, FunctionCall params contain methodName and args, and receiverId belongs to the transaction.
- [Published core 8.10.2 package metadata](https://unpkg.com/@near-wallet-selector/core@8.10.2/package.json) confirms the version. [Its wallet declarations](https://unpkg.com/@near-wallet-selector/core@8.10.2/src/lib/wallet/wallet.types.d.ts) define SignInParams with required contractId and optional methodNames; wallet variants extend those parameters.
- [Published Wallet Selector 10.1.4 transaction declarations](https://unpkg.com/@near-wallet-selector/core@10.1.4/src/lib/wallet/transactions.types.d.ts): the tagged InternalAction union, object or Uint8Array arguments, and the separate exported Action alias.
- [Published Wallet Selector 10.1.4 wallet declarations](https://unpkg.com/@near-wallet-selector/core@10.1.4/src/lib/wallet/wallet.types.d.ts): lifecycle methods, both send methods, verifyOwner, signMessage and createSignedTransaction.
- [NEP-141](https://raw.githubusercontent.com/near/NEPs/master/neps/nep-0141.md): both token transfer methods carry receiver_id and amount; ft_transfer_call additionally carries msg.
- [NEP-171](https://raw.githubusercontent.com/near/NEPs/master/neps/nep-0171.md): nft_transfer and nft_transfer_call name receiver_id; the call variant also carries msg.
- [NEAR account id validation](https://raw.githubusercontent.com/near/near-account-id/main/src/validation.rs): 2 to 64 ASCII characters, lowercase letters and digits, separators between components, and no adjacent separators.
- [Node 18.20.8 globals](https://nodejs.org/download/release/v18.20.8/docs/api/globals.html#crypto) and [Node 19.0.0 globals](https://nodejs.org/download/release/v19.0.0/docs/api/globals.html#crypto): global Web Crypto needs the experimental flag on Node 18 and is available without that flag from Node 19 when the binary includes crypto support. This is an API availability boundary, not a recommendation to run an unsupported Node release.
- [Node file URL conversion](https://nodejs.org/download/release/v19.0.0/docs/api/url.html#urlpathtofileurlpath): pathToFileURL returns a file URL from an absolute path and encodes URL control characters. The runtime harness uses its href for a portable ESM import.
- [Fetch CORS and credentials](https://fetch.spec.whatwg.org/#http-access-control-allow-origin) and [request header rules](https://fetch.spec.whatwg.org/#http-requests): wildcard origin permission applies to requests without credentials, and Accept: application/json is permitted without a preflight.

Repository response contracts were checked in `gate/verdict.py`, `intel/live.py` and `docs/API.md`. The live GET responses above independently confirm the hosted routes, fields and CORS result. The CORS probes and recorded ASK are retained from the earlier pass; this fix round added the native amount probe above.

## Files changed

- `integrations/langchain/adapter.py`
- `integrations/langchain/README.md`
- `integrations/langchain/fixtures/verdict-ask.json`
- `integrations/wallet-selector/adapter.mjs`
- `integrations/wallet-selector/README.md`
- `tests/test_langchain_adapter.py`
- `tests/test_wallet_selector_adapter.py`
- `tests/wallet_selector_runtime.cjs`
- `docs/lanes/adapters.md`

Eight files were edited in this fix round. The existing untracked verdict-ask.json fixture was retained without changes. The complete lane working tree therefore still contains the nine files above.

## Validation

Only the requested adapter modules and checks were run. Searching the tests found no other test module that imports either adapter.

| Check | Windows | Linux through Ubuntu 24.04 WSL |
| --- | --- | --- |
| LangChain adapter module | 33 passed, 0 skipped | 32 passed, 1 skipped |
| Wallet Selector adapter module | 3 passed, 0 skipped | 3 passed, 0 skipped |
| Combined Python invocation | 36 passed, 0 skipped | 35 passed, 1 skipped |
| JavaScript runtime harness | 30 named scenarios passed, 20 asserted stub sends | 30 named scenarios passed, 20 asserted stub sends |

Windows used Python 3.12.10, Node 24.22.3 and installed langchain-core 1.4.9. Linux used Python 3.12.3 and Node 24.16.0. The one Linux skip is the optional as_tool wrapper because langchain-core is not installed there. The wrapper passed on Windows. The JavaScript scenarios run inside one Python wallet test, so those counts must not be added as separate Python cases. Missing Node fails by default, including in CI; only SIYAB_SKIP_NODE_TESTS=1 explicitly permits a skip when Node is absent. Two Python tests verify this behavior.

```powershell
$env:TEMP='C:\t\ad3'
$env:TMP='C:\t\ad3'
python -m unittest tests.test_langchain_adapter tests.test_wallet_selector_adapter -v
wsl -d Ubuntu-24.04 --cd /mnt/c/Users/gudma/OneDrive/Desktop/GITHUB-FILES/siyab-pr10 --exec python3 -m unittest tests.test_langchain_adapter tests.test_wallet_selector_adapter -v
node tests/wallet_selector_runtime.cjs
python scripts/check_hygiene.py --all
python scripts/check_citations.py
git diff --check
```

The hygiene and citation checks exited 0 with no findings, with TEMP and TMP set to C:\t\ad3. The whitespace check exited 0. All nine recorded adapter fixture evidence hashes were recomputed and matched. Tests exercise stub send counters, denial without signing, a genuine recorded ASK that sends once only after confirmation, every supported argument representation, malformed and unsupported actions within otherwise valid batches, the reported drainer and NFT paths, message recipients and opaque instructions, native amount binding, guarded signIn, and missing amount and memo bindings. No test reads a key or calls a live wallet.

This round's eight initial wallet regression scenarios failed before their fixes. The LangChain regressions also reproduced the missing memo and amount=None bypasses before their fixes. Further regressions reproduced recipient alias, nested JSON instruction, account option leakage, reused object and missing native token binding cases before their fixes. Independent read-only review checked the final runtime paths and found no remaining concrete issue.

## Owner handoff and limits

The orchestrator should review the nine-file working tree and run the full suite after integrating lanes. The full suite was intentionally not run here under the lane's resource rule. No remote action is required to reproduce these results, and the observed CORS responses require no server change.

A production wallet session and real transaction execution were not tested because this lane prohibits signing and transactions. These are reference adapters, without maintainer adoption or an integration claim. Native payments now use exact amount verdicts. Named recipients in supported argument and message encodings are checked, while unknown methods and opaque instructions require confirmation. Arbitrary contract semantics, other encodings and token memo text are not simulated. LangChain cannot inspect additional transfers hidden inside a caller's send callback. A confirmed ASK is a human override, never an ALLOW or a signed Gate authorisation. No owner transaction or paid call is needed for this handoff; production wallet testing requires a separate authorised session.
