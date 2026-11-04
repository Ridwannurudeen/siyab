# Demo video and post

The video runs 140 seconds at 1080p, with captions and narration. The hosted pages are opened by path only. The EVM leg is a dry quote and a DENIED preflight. Nothing is signed or deposited there.

The commands run from the repository root. The `pay` and `attack` commands of shots 3 and 4 send on testnet: they need `GATE_FIXTURES_MODE=off`, `GATE_ACCOUNT_ID=kestrelridge-main.testnet`, `AGENT_KEY_FILE` and `VERIFIER_KEY_FILE` (`env.example`) with PyNaCl and py-near installed, the accounts are those of `docs/TESTNET.md`, and they are recorded on testnet, not run offline. Without `GATE_ACCOUNT_ID` and `AGENT_KEY_FILE` each prints `not sent: set GATE_ACCOUNT_ID and AGENT_KEY_FILE` and sends nothing. `scripts/set_allowlist.sh` in shot 4 is the owner's testnet transaction. Every other command runs offline.

## Shot list

### 1. 0 to 5 seconds: the refusal

Run from the repository root:

```console
python -m gate demo --offline
```

The command starts with `ShieldBot for NEAR offline demo v1: testnet responses recorded on 1 and 2 Nov 2026, replayed from`. Show the recorded lookalike result:

```
DENIED. kestrelridge-sh0p.testnet is a lookalike of kestrelridge-shop.testnet. Created less than 7 d ago. Not on the owner's allowlist. Not registered on the token.
```

Caption: "ShieldBot for NEAR: security intelligence with a source on every row, enforced on chain." Add "recorded replay" under the opening line.

### 2. 5 to 15 seconds: the agent key

Show the recorded account scan and the key scoped to `request_transfer`. The scan command is replayed from local fixtures:

```console
python -m gate scan kestrelridge-main.testnet
```

Its first line is `account: kestrelridge-main.testnet on https://test.rpc.fastnear.com, read at block 275865306`. The output reports two access keys, one full access key, and one function call key scoped to `request_transfer`.

Caption: "The agent can request a payment. The guard holds the funds."

### 3. 15 to 30 seconds: the allowed payment and its record

The payment, on testnet:

```console
python -m gate pay --kind ft --token token.kestrelridge-main.testnet --receiver kestrelridge-shop.testnet --amount 10000000 --memo "invoice 3" --out <dir>
```

Its first line is `pass    receiver_exists: kestrelridge-shop.testnet exists`. Show the checks, `verdict: ALLOW`, the evidence hash and the latency in ms, then the transaction and its `explorer:` line. Caption: "ALLOW: every check ran and none found a signal." and, beside the latency, "allowlisted payee, tier 1 checks". Recorded payments of this shape (`docs/TESTNET.md`): invoice 2 on 1 Nov, ALLOW in 1794 ms, https://testnet.nearblocks.io/txns/7agsRsZgeRm2vhK1LXug2qRQ2qudbAMjsLncuxGpvdwn, and invoice 3 on 2 Nov, ALLOW in 1754 ms, `DU55HxkN...zXNwGh`.

Then the record: open `/verify.html` and enter its three placeholders: the registry account `registry.kestrelridge-main.testnet`, the subject `kestrelridge-shop.testnet` and the evidence hash of the vendor's current `latest` record, `4b99f7ed...fc84bf29`, held in full in `fixtures/registry/testnet-latest-kestrelridge-shop.json`. That record is the ALLOW of invoice 4 (https://testnet.nearblocks.io/txns/AADLzYbqhEm6D83osYfQQ5SMED86chiS77XGsA8d7rTJ), recorded on 3 Nov by `record_batch` `HmxrLXBe...j5Ugyo`, and the page prints it with "equal to the recorded evidence hash". A payment recorded on the recording day replaces the record again; then paste the `evidence:` line that `pay` printed. The page reads the record in the browser from `https://test.rpc.fastnear.com`, so the shot needs that endpoint reachable.

This offline fixture command verifies a committed sample record:

```console
python -m gate verify fixtures/registry/sample-allow.json fixtures/registry/sample-allow-evidence.json
```

The output ends with `verified`.

### 4. 30 to 45 seconds: the attacks

Each attack, on testnet, prints `attack <name>: a simulation the Gate or the protocol must refuse` as its first line. The no-verdict attack submits a payment to the lookalike without a verdict:

```console
python -m gate attack no-verdict --kind ft --token token.kestrelridge-main.testnet --receiver kestrelridge-sh0p.testnet --amount 10000000 --memo "attack no-verdict" --out <dir>
```

The Gate refuses it with `REFUSED:no_verdict` (recorded: https://testnet.nearblocks.io/txns/6kfxEFCR2uuoiM6cpj8CmGAfmRjKTYNtFq4igkv48qK7). The replay sends one ALLOW twice:

```console
python -m gate attack replay --kind ft --token token.kestrelridge-main.testnet --receiver kestrelridge-shop.testnet --amount 10000000 --memo "attack replay" --out <dir>
```

The first send pays (recorded: https://testnet.nearblocks.io/txns/HWXiWZKcvyEh1gUPS9uBRe72PhHSNJibFVFRexd8jiRu) and the second fails `REFUSED:verdict_used` (recorded: https://testnet.nearblocks.io/txns/2FFnYUrXYLLgAHA2vsNud2mawU1xttkCQaNXg2yZG6Pn). The unknown payee attack pays an aged account that is not on the allowlist:

```console
python -m gate attack unknown-payee --kind ft --token token.kestrelridge-main.testnet --receiver examples.testnet --amount 2000000 --memo "attack unknown-payee" --out <dir>
```

The service answers ALLOW and the Gate refuses the 2 DMT, above the 1 DMT unknown payee cap, with `REFUSED:unknown_payee_cap` (recorded 2 Nov: https://testnet.nearblocks.io/txns/H6WqJYSbwBPECSVqADRHAGA1qV1a2ictujUiBqaLe416). `examples.testnet` is not a team account and is never paid, so the allowlist change on camera allows the team's own payee instead: the owner runs `bash scripts/set_allowlist.sh <agent pub> kestrelridge-shop.testnet,lindenvale.testnet`, then `python -m gate pay` with the same token, amount 2000000 and receiver `lindenvale.testnet` is expected to go through. That payment is to be recorded on 11 Nov; no transaction in the repository yet.

Caption: "A clean looking account is not enough. Payees the owner has not approved are capped."

### 5. 45 to 95 seconds: the intelligence pages

Show the local replay of the dry quote and denied preflight:

```console
python -m gate intents --origin nep141:wrap.near --destination nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near --amount 1000000000000000000000000 --recipient 0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107
```

The output begins `dry quote: 1000000000000000000000000 of nep141:wrap.near` and includes `verdict: DENY (unsigned preflight)` followed by `Nothing was signed, nothing was deposited.` Caption the cross-chain step "dry quote and denied preflight".

Open these hosted paths in order:

1. `/incidents/rhea.html`, showing the `issuer_blacklist` row.
2. `/flows/rhea-collector.html`, showing the committed flow summary, shares, and balance at a block.
3. `/clusters/rhea.html`, showing each edge with its heuristic and the precision lines.
4. `/issuers.html`, showing the issuer reads and action history.
5. `/feed.html`, showing the last recorded block, events, and the five signal names. The committed feed snapshot ends at 29 Oct, so present it as a recorded snapshot, not as a current follower view.
6. `/accounts/000001.sputnik-dao.near.html`, an account page in the committed site.
7. `/`, using its search box over the committed search index at `/search.json`.

Also show the offline incident output. Its first line is `Rhea Lend margin trading route exploit (2026-04-16)`:

```console
python -m gate incident rhea
```

Caption: "Every row names its source. Every edge names its heuristic."

### 6. 95 to 120 seconds: the checks

Show the recorded scan, the sample record verification from shot 3, the wasm code hash beside the Gate account's testnet `code_hash` (`Hfdk3jGU...R5iE`, `docs/TESTNET.md`), and `total_labels` on the registry. Print the local wasm hash with:

```console
python -m gate codehash res/gate.wasm
```

The output begins `sha256: f7a09b28` and then prints the base58 `code_hash`.

The local feed summary is available with:

```console
python -m intel feed-stats
```

Its first line is `start 2026-10-29T14:51:35Z at block 222138899`. The feed page is a committed snapshot; it does not show a live host follower.

Caption: "The registry is a public record. The evidence can be checked offline."

### 7. 120 to 140 seconds: connect and close

Show one MCP tool call using the setup in `mcp/README.md`, then run the offline smoke command:

```console
python scripts/smoke.py
```

The first line is `1. python -m gate demo --offline: PASS (ALLOW, DENY, UNKNOWN, DENY)` and the final line is `9 of 9 PASS`. Show the repository URL, the hosted page path `/`, "built at NEAR HACKATHON", and the AI Security & Defense track. Narration, the one sentence of pitch voice: "security intelligence, the way the best EVM products do it, with the evidence on chain".

## Recording order

1. Start with the recorded refusal and account scan.
2. Run the payment and open its record, then run the three attacks and the allowlist change on testnet.
3. Show the dry quote and denied preflight, then the hosted incident, flow, cluster, issuer, feed, account, and search views.
4. Close with the scan, registry verification, wasm code hash, MCP call, and offline smoke run.

## The post

The owner posts the video with this text. The first line follows the section 5.3 wording; the track and repository link follow it.

<!-- post:start -->
```
Built at NEAR HACKATHON: security intelligence for NEAR with a source on every row. Who your agent pays gets a verdict with evidence, the guard refuses the rest, on NEAR and at the bridge. Track: AI Security & Defense. <repo URL>
```
<!-- post:end -->

Measured with Python `len`: 219 characters before the URL, counting the space before it, 242 with the URL counted as 23. Both limits are UNVERIFIED: the 280 character limit and the 23 character weight of a link are not checked against the platform's own documentation, and the owner reads the composer's counter before posting.
