# Testnet accounts and deployment

Network: NEAR testnet. Accounts created 29 Oct 2026 through the faucet service (10 NEAR each). The Gate account was topped up by two transfers from the owner (6 NEAR) and the vendor (5 NEAR).

## Accounts

| Role | Account | Balance after setup (NEAR) |
|---|---|---|
| Owner | `kestrelridge-owner.testnet` | 4.0 |
| Gate | `kestrelridge-main.testnet` | 13.973 |
| Vendor | `kestrelridge-shop.testnet` | 5.0 |
| Attacker | `kestrelridge-mallory.testnet` | 8.992 |
| Aged payee | `lindenvale.testnet` (created 29 Oct from the attacker account, 1 NEAR) | 1.0 |
| Token | `token.kestrelridge-main.testnet` | 3.001 |
| Registry | `registry.kestrelridge-main.testnet` | 2.999 |
| Recorder | `recorder.kestrelridge-main.testnet` | 1.0 |

## Contracts

Built with `cargo near build non-reproducible-wasm` from a clean export of `main` at `89db0c6`, deployed by `scripts/setup_testnet.sh` on 29 Oct. Each `code_hash` read back from `view_account` equals base58 of sha256 of the local wasm.

| Contract | Account | code_hash | Deploy transaction |
|---|---|---|---|
| Gate | `kestrelridge-main.testnet` | `7NBhj8kM18mWLrpGbA3yupr4pefezfyowdLPy6Q2DUW8` | `8VPBkPi2...c8EcE6` |
| Registry | `registry.kestrelridge-main.testnet` | `64jBn2DD6PjypZKsWf7WhfvvxTaKWRCaug1izAjcmrLU` | `4bhQViAx...ifcJU3` |
| Test token (DMT, 6 decimals, supply 1000000 DMT to the Gate) | `token.kestrelridge-main.testnet` | `5uXqAX8C8fTupiJYwZQz1fC5voSnQsaD9xhxQ4R28rZw` | `A1To88Pg...s9dMs7` |

Read back after setup: Gate and registry `version` `0.1.0`; Gate policy `max_ttl_ms` 300000, not paused, empty denylist; one verifier key, no previous key; the registry recorder is `recorder.kestrelridge-main.testnet`; token storage registered for the Gate, the vendor and the aged payee (0.00129 NEAR each). The Gate holds one full access key and no agent key yet.
