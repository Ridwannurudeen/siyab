# Demo video and post

The video runs 140 s, 16:9 at 1080p, with captions burned in and the narration spoken. Each terminal run is full screen with the explorer tab behind it. The commands below run from the repository root; the testnet ones need `GATE_ACCOUNT_ID=kestrelridge-main.testnet`, `AGENT_KEY_FILE` and `VERIFIER_KEY_FILE` set (`env.example`), and the accounts are those of `docs/TESTNET.md`. A command that prints a transaction prints its `explorer:` line; that URL is the explorer tab for the shot. Narration states facts only; the one sentence of pitch voice is marked in shot 7 and appears nowhere else in the repository.

## The opening line

The opening frame is the line the rules print for the lookalike, from `python -m gate demo --offline` (testnet responses recorded on 1 and 2 Nov, replayed):

```
DENIED. kestrelridge-sh0p.testnet is a lookalike of kestrelridge-shop.testnet. Created less than 7 d ago. Not on the owner's allowlist. Not registered on the token.
```

Account age stayed in the rules (`docs/SPIKES.md` S8): NearBlocks testnet has indexed nothing since 27 Sep, so the age comes from the oldest access key nonce and the creation deposit is not read. The opening line therefore names no deposit. A live run on the recording day dates `kestrelridge-sh0p.testnet` at more than 7 days, so the opening frame uses the recorded replay, captioned as such.

## Shots

### 1. 0 to 5 s: the refusal

- Command: `python -m gate demo --offline`, the lookalike's section in frame.
- Expected first line: `ShieldBot for NEAR offline demo v1: testnet responses recorded on 1 and 2 Nov 2026, replayed from`
- On screen: the DENIED line above, with the headline burned in under it.
- Caption: "ShieldBot for NEAR: security intelligence for agent money, enforced on chain." and, small, "replay of the 1 and 2 Nov testnet recording".
- Narration: "This agent holds no key that can move money. The guard refused its payment."
- Explorer: the refused receipt `REFUSED:no_verdict`, https://testnet.nearblocks.io/txns/6kfxEFCR2uuoiM6cpj8CmGAfmRjKTYNtFq4igkv48qK7

### 2. 5 to 20 s: the problem and the agent key

- Command: `near account list-keys kestrelridge-main.testnet network-config testnet now`
- Expected first line: not recorded in the repository. The same keys as `python -m gate scan kestrelridge-main.testnet` reads them: `access keys: 2`, `full access keys: 1`, `agent keys: 1 scoped to kestrelridge-main.testnet, 0 with method_names other than exactly ["request_transfer"]`.
- Caption: "The agent's only key: a function call key scoped to request_transfer."
- Narration: "On NEAR a function call key cannot attach the 1 yoctoNEAR a token transfer needs, so agents are handed full keys; here the funds sit in the guard, and the agent can only ask it to pay."
- Explorer: none; the keys are read over RPC as the command prints them.

### 3. 20 to 35 s: the allowed payment

- Command: `python -m gate pay --kind ft --token token.kestrelridge-main.testnet --receiver kestrelridge-shop.testnet --amount 10000000 --memo "invoice 3" --out <dir>`
- Expected first line: `pass    receiver_exists: kestrelridge-shop.testnet exists`
- On screen: the checks (receiver exists, no label, token in the catalog, metadata, storage registered, memo clean), `verdict: ALLOW`, the evidence hash, the latency in ms, then the transaction.
- Caption: "ALLOW: every check ran and none found a signal. Signed for this exact transfer." and, next to the latency, "allowlisted payee, tier 1 checks".
- Narration: "The service checks the receiver, the token, the amount and the memo, signs an ALLOW bound to this exact transfer, and the guard pays."
- Explorer: the transaction the command prints, with the `EVENT_JSON` log carrying the verdict id and the evidence hash, the `ft_transfer` receipt and the vendor's balance. The recorded payment of this shape is invoice 2, https://testnet.nearblocks.io/txns/7agsRsZgeRm2vhK1LXug2qRQ2qudbAMjsLncuxGpvdwn

### 4. 35 to 50 s: the attacks

- On screen first: a staged vendor reply that tells the agent to pay `kestrelridge-sh0p.testnet`, captioned "staged vendor reply".
- Command: `python -m gate attack no-verdict --kind ft --token token.kestrelridge-main.testnet --receiver kestrelridge-sh0p.testnet --amount 10000000 --memo "attack no-verdict" --out <dir>`
- Expected first line: `attack no-verdict: a simulation the Gate or the protocol must refuse`
- Caption: "DENY from the service. Submitted anyway: refused by the guard."
- Explorer: https://testnet.nearblocks.io/txns/6kfxEFCR2uuoiM6cpj8CmGAfmRjKTYNtFq4igkv48qK7
- Replay cut: `python -m gate attack replay ...`, expected first line `attack replay: a simulation the Gate or the protocol must refuse`; the second submission fails `REFUSED:verdict_used` (recorded: https://testnet.nearblocks.io/txns/2FFnYUrXYLLgAHA2vsNud2mawU1xttkCQaNXg2yZG6Pn). A one second caption: "A day old ALLOW: `REFUSED:expired`" (recorded: https://testnet.nearblocks.io/txns/5KG3sPSqdvUP6y6a5iR1APzcHxb8pSsVxKiffWbNvQ7H).
- Unknown payee: `python -m gate attack unknown-payee --kind ft --token token.kestrelridge-main.testnet --receiver examples.testnet --amount 2000000 --memo "attack unknown-payee" --out <dir>`, expected first line `attack unknown-payee: a simulation the Gate or the protocol must refuse`; `examples.testnet` is an aged account not on the allowlist, the service answers ALLOW and the guard refuses the 2 DMT, above the 1 DMT unknown payee cap, with `REFUSED:unknown_payee_cap` (recorded 2 Nov: https://testnet.nearblocks.io/txns/H6WqJYSbwBPECSVqADRHAGA1qV1a2ictujUiBqaLe416). `examples.testnet` is not a team account and is never paid, so the allowlist change on camera allows the team's own payee instead: the owner runs `bash scripts/set_allowlist.sh <agent pub> kestrelridge-shop.testnet,lindenvale.testnet`, and `python -m gate pay` with the same token, amount 2000000 and receiver `lindenvale.testnet` goes through; a caption names `lindenvale.testnet` as the allowlisted account.
- Narration: "A clean looking account is not enough. Payees the owner has not approved are capped."

### 5. 50 to 85 s: beat 2, the money leaves NEAR

- Command: `python -m gate intents --origin nep141:wrap.near --destination nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near --amount 1000000000000000000000000 --recipient 0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107`
- Expected first line: `dry quote: 1000000000000000000000000 of nep141:wrap.near to nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near, recipient 0x1fcdb04d0c5364fbd92c73ca8af9baa72c269107, amountOut 4398699` (the recorded quote; with `--live` the quote is new and its amountOut differs).
- On screen: the dry quote (recipient, Base, amount), `verdict: DENY (unsigned preflight)`, the reason naming the GoPlus labels `stealing_attack` with data_source "SlowMist,BlockSec" and the fetch time, then `Nothing was signed, nothing was deposited.`
- Caption: "The verdict follows the money off NEAR."
- Narration: "The same agent is told to send NEAR to a supplier on Base. The dry quote shows the recipient before anything is signed; the recipient carries a theft label, so nothing is signed and nothing is deposited."
- Then: `python -m gate incident rhea`, expected first line `Rhea Lend margin trading route exploit (2026-04-16)`: the accounts, the sources and the on chain re-read. Then the exit trace page `demo/site/flows/rhea-collector.html`, one hop per card.
- Explorer: none; the preflight sends no transaction on any chain.

### 6. 85 to 110 s: the record

- Command: `python -m gate scan kestrelridge-main.testnet`
- Expected first line: `account: kestrelridge-main.testnet on https://test.rpc.fastnear.com, read at block <height>`; on screen the on chain verifier key next to the TEST key, labelled different.
- Command: `python -m gate verify fixtures/registry/testnet-vendor.json fixtures/evidence/7e77187d*.json`, expected first line `record shape: view`, last line `verified`, next to the registry record on the explorer.
- Command: `sha256sum res/gate.wasm` next to the testnet `code_hash`; its output lands with the wasm build under `res/`.
- Caption: "The registry is the public record. Anyone can verify it without a key."
- Narration: "Every verdict is recorded on chain with its evidence hash, and the evidence file checks against it offline."
- Explorer: the registry `record_batch`, https://testnet.nearblocks.io/txns/CW5TWeRyvLngggXiBY5Y7xSZTD3BPWerFtsXczJdPRhW

### 7. 110 to 140 s: integrate and close

- On screen: one MCP tool call from an MCP client connected as `mcp/README.md` describes, and its reply; then `<repo URL>`, "built at NEAR HACKATHON" and the track, AI Security & Defense.
- Expected first line of the reply: the tool's JSON text, starting `{"outcome": ` for `check_near_payment`.
- Caption: "Who your agent pays gets a verdict with evidence. Unknown never passes."
- Narration, the one sentence of pitch voice: "security intelligence, the way the best EVM products do it, with the evidence on chain".
- Explorer: none for the call itself. Recorded on 2 Nov: with `GATE_LIVE_SIGNING=1` and the live conditions of `mcp/README.md` set, `check_near_payment` signed an ALLOW for invoice 4 that `python -m gate pay --verdict-file` paid, https://testnet.nearblocks.io/txns/AADLzYbqhEm6D83osYfQQ5SMED86chiS77XGsA8d7rTJ

## Recording order

1. At least three hours before recording: `bash scripts/make_attack_receiver.sh <a second lookalike name>`.
2. Beat 1: the staged vendor reply, the DENIED line, `attack no-verdict`, the failed receipt; `pay` to the vendor with the ALLOW, the event and the receipt; the replay cut; the unknown payee refusal on `examples.testnet`, then the owner runs `scripts/set_allowlist.sh` on camera to allow `lindenvale.testnet` and a 2 DMT payment to it goes through.
3. Beat 2: `python -m gate intents ...` with the dry quote on screen and the DENIED line; `python -m gate incident rhea`; the flow page.
4. The close: `scan`, the registry record next to `verify`, `sha256sum res/gate.wasm` next to the testnet `code_hash`, the MCP call, the repository URL.

The raw footage stays outside the repository.

## The post

The owner posts it. The first line is fixed; then the track and the repository link. The video is attached to the post, not written in it.

<!-- post:start -->
```
Built at NEAR HACKATHON: security intelligence for AI agent money on NEAR. Who your agent pays gets a verdict with evidence, the guard refuses the rest, on NEAR and at the bridge. Track: AI Security & Defense. <repo URL>
```
<!-- post:end -->

Measured with Python `len`: 210 characters before the URL, counting the space before it, 233 with the URL counted as 23. Both limits are UNVERIFIED: the 280 character limit and the 23 character weight of a link are not checked against the platform's own documentation, and the owner reads the composer's counter before posting.
