# Attestation registry build

Source commit: TBD

Deployed: not deployed

22 November 2026. This record covers a reproducible working tree snapshot above base c4899e190ec8acea202f38793d04b384112a48b3. It does not attribute the new contract to that base commit. `res/attest.source.sha256` pins all six build input files. No commit, public deployment or transaction was made during that build. The source merged to main at 64c55270a82fe468dee7b8a1001e7c50a17c3a4b on 22 Nov 2026. All six inputs match that tree. The reproducible release build is not yet promoted to `res/`.

Two isolated Docker builds, with independent empty external target directories and networking disabled, produced identical optimized wasm bytes. `cmp` returned zero. Both builds compiled every dependency afresh. Both outputs used the same source mount path `/workspace` and target mount path `/target`. Use fresh target directories when repeating the recipe so cached artifacts cannot conceal a changed build input.

| Item | Value |
|---|---|
| Image | `sourcescan/cargo-near:0.21.1-rust-1.96.0` |
| Image digest | `sha256:ccb22bb4e677ed022d8b9d1aa1b32f0af52dd0471ef1c32e48dedbf68ee6ee17` |
| Container Rust | `rustc 1.96.0 (ac68faa20 2026-05-25)` |
| Container cargo-near | `0.21.1` |
| Host validation | Ubuntu WSL, Rust 1.99.1, cargo-near 0.22.0 |
| SDK | `near-sdk 5.29.1`, exact manifest pin |
| Verifier interface | near/mpc tag 3.18.0, locked e691bdb98d68dcf74653da58d043453a6318765f |
| Profile | release, one codegen unit, size optimization, LTO, abort, overflow checks |
| Post processing | cargo-near's wasm-opt size pass, bulk memory and nontrapping float conversion enabled |
| ABI | Omitted explicitly with `--no-abi`; JSON and Borsh wire interfaces are in the frozen spec |
| Wasm | 172539 bytes |
| SHA256 | `9e0a7f3e9a15d0b832fdaf16249b68c268cd4d6671b6d3e6a687b4773b46bfdf` |
| NEAR code hash | `BdvhbRrwz8y5dnMB5EBjpy5Gz8XXrMSqmrbWPikj8dzE` |

The fix round 1 outputs are under `/home/gudman/target/siyab-attest-t1-fix1-repro-clean-c/near/attest.wasm` and `/home/gudman/target/siyab-attest-t1-fix1-repro-clean-d/near/attest.wasm`. The sandbox registry tests consume their identical copy at `/home/gudman/target/siyab-attest-t1/near/attest.wasm`, not a raw Cargo wasm. A raw Cargo build without `--cfg near` does not select the SDK environment's on-chain panic implementation. Use cargo-near for this artifact.

Fix round 1 extracted the existing renewal comparison into the production predicate tested by every frozen reregistration vector. The optimized artifact grew by two bytes. SDK features and lockfile are unchanged. The SDK has no feature switch that removes its three gas key and four global contract imports; their exact names and the required owner testnet call before any access key removal are in `docs/lanes/attest-registry.md`. The six function exports remain `contract_source_metadata`, `get_config`, `get_key`, `new`, `on_quote_verified` and `register_key`. No test view, diagnostic log or additional release export was added.

The working tree cannot honestly be reproduced by checking out its base commit. cargo-near's top level `reproducible-wasm` command builds committed source and discards uncommitted changes. It was therefore not used to claim this snapshot. Following the repository's uncommitted build convention, `res/attest.wasm` and its checksum are absent while Source commit is TBD. This record and the source fingerprints retain the tested result without claiming a release artifact.

## Repeat the snapshot build

Run in Ubuntu WSL from the repository root. The exact image must be available, and Cargo registry and git dependency caches must be populated from the lockfile. A missing cache fails with networking disabled. No credentials are mounted. `CARGO_TARGET_DIR` stays outside the repository.

```sh
sha256sum -c res/attest.source.sha256
attest_root="$PWD"
attest_image="sourcescan/cargo-near@sha256:ccb22bb4e677ed022d8b9d1aa1b32f0af52dd0471ef1c32e48dedbf68ee6ee17"
for build in c d; do
    attest_target="$HOME/target/siyab-attest-t1-fix1-repro-clean-$build"
    if test -e "$attest_target"; then
        printf 'Use a new empty external target path: %s\n' "$attest_target" >&2
        exit 1
    fi
    mkdir -p "$attest_target"
    docker run --rm --network=none \
        -v "$attest_root/contracts/attest:/workspace:ro" \
        -v "$HOME/.cargo/registry:/cargo/registry:ro" \
        -v "$HOME/.cargo/git:/cargo/git:ro" \
        -v "$attest_target:/target" \
        -e CARGO_HOME=/cargo -e CARGO_TARGET_DIR=/target \
        -e CARGO_NET_OFFLINE=true -e CARGO_BUILD_JOBS=2 \
        -w /workspace "$attest_image" \
        cargo near build non-reproducible-wasm --locked --no-abi --out-dir /target/near
done
cmp "$HOME/target/siyab-attest-t1-fix1-repro-clean-c/near/attest.wasm" \
    "$HOME/target/siyab-attest-t1-fix1-repro-clean-d/near/attest.wasm"
sha256sum "$HOME/target/siyab-attest-t1-fix1-repro-clean-c/near/attest.wasm" \
    "$HOME/target/siyab-attest-t1-fix1-repro-clean-d/near/attest.wasm"
```

After the orchestrator commits the reviewed implementation, it must set Source commit to that commit, run the normal committed-source build below, retain its output as `res/attest.wasm` with its matching checksum, and replace this snapshot digest if the committed build metadata changes the bytes. Do not substitute the base commit or assert the release bytes will be identical before comparing them.

Use a checkout whose Git metadata is readable in the build environment. WSL Git could not follow this Windows worktree's absolute Windows gitdir pointer. The snapshot build above mounts source directly and does not inspect Git. No Git metadata was changed to work around that limitation.

```sh
unset CARGO_TARGET_DIR
(cd contracts/attest && cargo near build reproducible-wasm)
```

The orchestrator must also add `attest` to the existing build-record and CI crate inventories, and fold the prefix rows from `docs/lanes/attest-registry.md` into the prefix table before deployment. This lane does not edit those shared files.

Sources: https://github.com/near/cargo-near/blob/main/README.md, https://github.com/near/cargo-near/blob/main/cargo-near/src/commands/new/new-project-template/Cargo.template.toml and https://github.com/near/near-sdk-rs/tree/near-sdk-v5.29.1. Installed CLI help and the actual pinned container commands above were checked. The two immutable MPC interface source files and their local equivalents were compared as recorded in the lane report.

The target-dependent SDK panic implementation was checked against the installed near-sdk-env 0.1.8 source and byte-identical immutable primary sources: https://raw.githubusercontent.com/near/near-sdk-rs/e19629c8e6381f67a77301379e850ad7b057dc32/near-sdk-env/src/lib.rs and https://raw.githubusercontent.com/near/near-sdk-rs/e19629c8e6381f67a77301379e850ad7b057dc32/near-sdk-env/src/macros.rs.
