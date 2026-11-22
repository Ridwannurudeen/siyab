# NEP-413 enclave build record

22 November 2026. Branch `feat/nep413-workspace` at `8393b2c3a` with the uncommitted predeploy review fixes. The current two build comparison includes the final sign-in budget source, SHA256 prefix `4901a9e2a7d8`. The earlier proof and its test results are preserved below as history.

## Current predeploy build

Two fresh builders, `siyab-nep413-predeploy-one-20261122` and `siyab-nep413-predeploy-two-20261122`, completed sequential uncached builds in 51.266 and 35.781 seconds. Their container ids and cache volumes were distinct. Both used Docker 29.1.3 through WSL Ubuntu 24.04 root, the official checksum verified Buildx v0.39.1 binary and the same digest pinned BuildKit v0.35.1 image as the earlier proof. Each had a two CPU quota, worker parallelism two and restart policy `no`. A new blank private Docker configuration used `unix:///var/run/docker.sock`. Existing configurations and builders were untouched.

The unchanged Dockerfile and lockfile received the same fixed epoch `1795304440`, Linux amd64 platform, mainnet network, registry `attest.siyab.near` and no label delta. Provenance and SBOM were disabled, with exporter timestamp rewriting enabled. All 26 inputs were hashed from their actual bytes before freezing the USTAR context. The frozen contents were verified, then all source hashes were checked again after each build and after runtime validation. Only `intel/budget.py` differed from the earlier proof, from prefix `576141cc3230` to `4901a9e2a7d8`. This build regeneration edited no runtime or test source.

Both archives had identical OCI index bytes, full image manifest and config descriptors, and all 12 layer descriptors. Every one of the 14 blobs per archive matched its digest and byte size, with no unreferenced blob. The record was generated from those verified bytes. The existing compose CLI then produced identical outputs twice, exactly 826 UTF-8 bytes with LF endings.

| Current artifact | SHA256 prefix |
|---|---|
| Image manifest | `b3bcc3d69ed5` |
| Image config | `12afbd083d4c` |
| OCI index bytes | `effc9a81d3d6` |
| Frozen source context | `3572415242e4` |
| App compose bytes | `44776e2cd93d` |

Full public digests and all input hashes are in `enclave/build-record.json`. The image reference uses `registry.example/siyab`, so this remains a local review compose. No image was published and no deployment measurement was inferred.

Current artifacts remain under ignored `.codex/nep413-predeploy-enclave-repro`: both OCI archives, frozen context, input hashes, exact command arrays and logs, builder inspections, descriptor and blob verification, compose bytes, the verified Buildx binary and fresh official checksums. Copies of the previous record and three build documents preserve the predeploy starting state. The older `.codex/nep413-enclave-repro` proof was retained intact. Both new private builders and their cache volumes were removed and their absence checked. The current local test image is `siyab-nep413-predeploy-enc:final-20261122`; the earlier local image remains available separately.

From the repository root in WSL, the build invocations were equivalent to:

```sh
for run in one two; do
  docker --config .codex/nep413-predeploy-enclave-repro/docker-config \
    --host unix:///var/run/docker.sock buildx build \
    --builder "siyab-nep413-predeploy-$run-20261122" --no-cache \
    --platform linux/amd64 --provenance=false --sbom=false \
    --build-arg SIYAB_REGISTRY=attest.siyab.near \
    --build-arg SOURCE_DATE_EPOCH=1795304440 \
    --output "type=oci,dest=.codex/nep413-predeploy-enclave-repro/$run.tar,rewrite-timestamp=true" \
    --progress plain -f enclave/Dockerfile - < .codex/nep413-predeploy-enclave-repro/context.tar
done
```

The retained command arrays have the exact absolute paths used. The private builder creation options and frozen context recipe are in `enclave/BUILD.md`.

## Current predeploy validation

All six `tests.test_enclave_build` methods passed inside the current image in 0.805 seconds, with zero skips, failures or errors. The test container had no external network, a read only source mount and ephemeral `/tmp` for TEMP, TMP and TMPDIR. Its command is in `enclave/BUILD.md` and the ignored `image-tests-command.json`.

A separate offline smoke imported the actual baked runtime without a source mount. It checked uid and gid 65532, the mainnet build configuration, testnet registry refusal, the one hour refresh setting, absent shell and SSH executables, absent relay and command line payment entry point, the reviewed budget limits and every one of the 25 copied input file hashes. Runtime versions were Python 3.12.10, PyNaCl 1.6.2 and cffi 2.0.0. The installed pycparser release is 3.0; its module reports `3.00`. Neither check started the guest service or contacted a public provider.

The orchestrator runs the requested host module matrix with TEMP and TMP at `C:\t\okx`, hygiene and citations checks. Its exact counts are recorded in `docs/lanes/nep413.md`. The earlier 231 method image result below belongs to the previous image and was not repeated for this budget input. No owner command, transaction, key access, paid call, image publication or deployment is needed for this regeneration. Independent review, the complete suite and any commit remain with the orchestrator. Real Phala quote and socket validation still require the separately authorized owner sequence in `docs/lanes/attest-enclave.md`.

The current build flags, timestamp behavior, private cache behavior and pinned tool verification use the primary sources listed at the end of this file. The only build warning was the existing constant Linux amd64 platform in the unchanged Dockerfile.

## Earlier regeneration before the predeploy fixes

The rest of this report records the prior regeneration on the same date. Its source budget pin, artifact digests, image and validation are historical. That regeneration repaired the stale `intel/budget.py` pin after the earlier NEP-413 lookup budget change. Its initial `tests.test_enclave_build` run reproduced one failure and five passes. No production or test source was changed in that build task.

### Earlier reproducible build

The earlier independent comparison followed `enclave/BUILD.md`. Its frozen USTAR context contains the Dockerfile and its 25 explicit COPY inputs. All 26 source hashes were recomputed from the source tree at that time, verified against the context and checked again after both builds. Only the budget source pin changed, to prefix `576141cc3230`.

Docker 29.1.3 runs in WSL Ubuntu 24.04. The pinned Buildx v0.39.1 binary was copied from the retained project build tools into this worktree and matched a freshly downloaded official release checksum. The pinned BuildKit v0.35.1 image was available locally, and both bootstrapped builder inspections reported that exact version and image. Windows has no Docker CLI on its PATH; WSL root can access the local Unix Docker socket. The default WSL Ubuntu 24.04 user cannot access that socket.

Both builds used the unchanged Dockerfile, lockfile and build settings: Linux amd64, `SOURCE_DATE_EPOCH=1795304440`, registry `attest.siyab.near`, network mainnet, no label delta, no cache, no provenance, no SBOM and exporter timestamp rewriting. Separate private builders `siyab-nep413-enc-one-20261122` and `siyab-nep413-enc-two-20261122` used separate cache volumes. Each had a two CPU quota, worker parallelism two and no restart policy. The builds ran sequentially. A blank private Docker configuration and explicit `unix:///var/run/docker.sock` endpoint kept existing configurations and builders unchanged.

Both OCI index byte hashes, manifests, configs and all 12 layer descriptors matched. Every one of the 14 blobs in each archive matched its descriptor digest and byte size. The descriptor graph accounted for every blob. Builder container ids and cache volume names were distinct. `enclave/build-record.json` was generated from those verified archives and the frozen input map, followed by two identical outputs from the existing compose CLI. No digest was substituted without producing its bytes.

| Earlier artifact | SHA256 prefix |
|---|---|
| Image manifest | `cfca8fbbc606` |
| Image config | `9fc27c79b69f` |
| OCI index bytes | `a57e0a114007` |
| Frozen source context | `35afadda1f69` |
| App compose bytes | `1c9ed32dda0e` |

The earlier full record is retained as `.codex/nep413-predeploy-enclave-repro/previous-enclave-build-record.json`. Its compose file uses `registry.example/siyab` and is exactly 826 UTF-8 bytes with LF endings. This is an example repository for local review; no image was published. A deployment measurement cannot be derived without actual guest registers and the key provider digest. Existing synthetic vector and upstream capture measurements were preserved.

Artifacts remain under ignored `.codex/nep413-enclave-repro`: `context.tar`, `input-sha256.json`, `one.tar`, `two.tar`, both build logs and command arrays, builder inspections, blob verification summary, `app-compose.json`, the verified Buildx binary, its official checksums and test logs. The loaded local image is `siyab-nep413-enc:final-20261122`. Both private builders and their cache volumes were removed after verification. Other Docker resources and prior build artifacts were preserved.

The offline runtime smoke passed as uid and gid 65532. It checked the baked configuration, one hour refresh setting, dependency imports, absent shell and SSH executables, absent relay, testnet registry refusal and all 25 copied input hashes inside the exact image. Runtime versions are Python 3.12.10, PyNaCl 1.6.2, cffi 2.0.0 and pycparser 3.0. The first temporary smoke command used the nonexistent field `registry_id`; reading `BuildConfig` confirmed its field is `registry`, and the corrected smoke passed without source edits.

### Earlier validation

The exact rebuilt image passed all 231 requested test methods in 272.045 seconds with no skips, failures or errors. It used a read only repository mount and disabled external networking. The Windows run discovered the same 231 methods in 261.131 seconds: 182 passed and 49 methods skipped, with no failures or errors.

| Module | Image passed | Windows passed | Windows skipped methods |
|---|---|---|---|
| `tests.test_enclave_build` | 6 | 6 | 0 |
| `tests.test_enclave_dstack` | 12 | 3 | 9 |
| `tests.test_enclave_labels` | 18 | 18 | 0 |
| `tests.test_enclave_measure` | 20 | 20 | 0 |
| `tests.test_enclave_relay` | 13 | 13 | 0 |
| `tests.test_enclave_service` | 44 | 4 | 40 |
| `tests.test_attest` | 43 | 43 | 0 |
| `tests.test_attest_vectors` | 47 | 47 | 0 |
| `tests.test_citations` | 11 | 11 | 0 |
| `tests.test_spec` | 17 | 17 | 0 |
| Total distinct methods | 231 | 182 | 49 |

The Windows raw summary is `OK (skipped=72)`: 40 missing PyNaCl skips and 32 AF_UNIX skip records across nine socket test methods. The existing subtests account for the difference between skip records and skipped methods. No package was installed into Windows. The image run exercised all those methods without skips. The initial six test build run with one reproduced failure is additional to this final 231 method matrix.

From the repository root on Windows, all requested host commands use TEMP and TMP at `C:\t\enc`:

```powershell
$env:TEMP = 'C:\t\enc'
$env:TMP = 'C:\t\enc'
python -m unittest tests.test_enclave_build tests.test_enclave_dstack tests.test_enclave_labels tests.test_enclave_measure tests.test_enclave_relay tests.test_enclave_service tests.test_attest tests.test_attest_vectors tests.test_citations tests.test_spec -v
python scripts/check_hygiene.py --all
python scripts/check_citations.py
git diff --check
```

The exact image command is retained in `enclave/BUILD.md` and the ignored test command array. Its TEMP, TMP and TMPDIR use ephemeral `/tmp` because `C:\t\enc` is a Windows path. The complete repository suite was not run.

### Earlier references and scope

The stale image, config, compose, context and index references existed only in `enclave/build-record.json`, `enclave/BUILD.md` and `docs/lanes/attest-enclave.md`. The root README and tests contain no literal pins for these values, and this tree contains no files matching `docs/ATTEST*.md`. The build test reads the recorded source hashes dynamically. The current build and artifact references in both existing documents were refreshed.

This round changes the build record, those two build documents and this report. `intel/budget.py`, every other image source input, the frozen specification and vectors remain unchanged. No owner transaction, key access, paid call, image publication or deployment is needed for this CI repair. Independent review, the complete suite and the orchestrator's commit remain with the owner. Real Phala quote and socket validation remain unexercised because no owner CVM was used; the separately controlled owner sequence remains in `docs/lanes/attest-enclave.md`.

## Primary sources checked

The freshly fetched release checksums verified the exact Buildx binary used:

https://github.com/docker/buildx/releases/download/v0.39.1/checksums.txt

The installed CLI help and pinned Buildx source verified the build flags and disabled attestations:

https://github.com/docker/buildx/blob/v0.39.1/docs/reference/buildx_build.md

The Docker container driver documentation verified the pinned image option, CPU settings, restart policy and dedicated cache volume behavior:

https://docs.docker.com/build/builders/drivers/docker-container/

The OCI exporter documentation verified the local archive and timestamp rewrite options:

https://docs.docker.com/build/exporters/oci-docker/

The pinned BuildKit reproducibility document verified the fixed epoch behavior and exporter timestamp rewriting:

https://github.com/moby/buildkit/blob/v0.35.1/docs/build-repro.md
