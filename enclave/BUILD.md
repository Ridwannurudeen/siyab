# Enclave image

Build from the repository root. The image is Linux amd64 only. The base image and three binary wheel inputs are pinned by digest. The measured build settings are written to `/app/enclave/build.json` through `enclave.service.BuildConfig`. `SIYAB_REGISTRY` is required. `SIYAB_NETWORK` defaults to `mainnet`. `SIYAB_DELTA_URL` defaults to no delta. These are build arguments, not runtime configuration.

The image copies explicit source files and the baked label floor. It includes neither the relay nor command line payment entry points. Existing verdict imports still require `gate.agent` and `gate.signer`; their key file loading functions are not service entry points. The service uses its own memory generated signer. Shell executables are removed, and the entry point runs Python directly as uid and gid 65532 with Python environment, user site packages and bytecode writes disabled.

The service generates a private relay bearer token at each boot and prints it once to its local output. Give that token only to the relay through a private channel and protect container logs. Every HTTP route except `/health` requires it, including the cached attestation and public key. Neither signing keys nor authentication values are runtime inputs. The token is transient service state and is not a build argument, environment variable or mounted file.

The Docker build context has the repository root layout. To exclude ignored development files and unrelated data from transport, use an explicit tar context:

```sh
tar -cf - enclave/Dockerfile enclave/requirements.lock enclave/*.py gate/*.py intel/*.py intel/labels.jsonl intel/issuers.json fixtures/reduced-recordings.json |
  docker build --platform linux/amd64 --build-arg SIYAB_REGISTRY=attest.siyab.near --build-arg SOURCE_DATE_EPOCH=1795304440 -f enclave/Dockerfile -t siyab-enclave:local -
```

The registry above is a format example, not a deployment assertion. Select the reviewed registry before building. Changing the registry, network, delta URL, image contents or manifest changes measured inputs and requires review. A testnet build needs both `--build-arg SIYAB_NETWORK=testnet` and a `.testnet` registry.

## Independent build comparison

Use the same source tree, platform, build arguments, Dockerfile frontend and BuildKit version for both builds. `1795304440` is the timestamp returned by `git show -s --format=%ct c4899e190`. Pin it explicitly rather than using the current time. BuildKit 0.13 or later supports the exporter timestamp rewrite below. Disable provenance and SBOM attestations for this comparison because their separate metadata can introduce varying inputs. This recipe does not publish an image.

```sh
mkdir -p .codex/enclave-repro
python - <<'PY'
import io
import tarfile
from pathlib import Path

names = {"enclave/Dockerfile"}
for line in Path("enclave/Dockerfile").read_text().splitlines():
    if line.startswith("COPY "):
        names.update(line.split()[1:-1])
with tarfile.open(".codex/enclave-repro/context.tar", "w", format=tarfile.USTAR_FORMAT) as archive:
    for name in sorted(names):
        data = Path(name).read_bytes()
        entry = tarfile.TarInfo(name)
        entry.size = len(data)
        entry.mode = 0o644
        entry.mtime = 1795304440
        archive.addfile(entry, io.BytesIO(data))
PY
for run in one two; do
  docker buildx build --no-cache --platform linux/amd64 --provenance=false --sbom=false \
    --build-arg SIYAB_REGISTRY=attest.siyab.near --build-arg SOURCE_DATE_EPOCH=1795304440 \
    --output "type=oci,dest=.codex/enclave-repro/$run.tar,rewrite-timestamp=true" \
    -f enclave/Dockerfile - < .codex/enclave-repro/context.tar
done
python - <<'PY'
import json
import tarfile
from pathlib import Path

digests = []
for name in ("one", "two"):
    with tarfile.open(Path(".codex/enclave-repro") / (name + ".tar")) as archive:
        index = json.load(archive.extractfile("index.json"))
    digests.append(index["manifests"][0]["digest"])
print(*digests, sep="\n")
assert digests[0] == digests[1], "independent image manifests differ"
PY
```

Do not substitute a cached build for this comparison. Equal measurement ids from identical measurement inputs establish formula determinism. They do not establish image reproducibility. The lane's actual local build and test results are recorded in `docs/lanes/attest-enclave.md`.

For the current NEP-413 predeploy comparison, the same build invocation selected distinct private builders with `--builder siyab-nep413-predeploy-one-20261122` and `--builder siyab-nep413-predeploy-two-20261122`, respectively. Each was created with the `docker-container` driver, the BuildKit image digest recorded in `enclave/build-record.json`, `cpu-quota=200000`, `cpu-period=100000` and `restart-policy=no`. Its private `buildkitd.toml` set `[worker.oci]` and `max-parallelism = 2`. The Docker CLI used the blank configuration at `--config .codex/nep413-predeploy-enclave-repro/docker-config` and endpoint `unix:///var/run/docker.sock`. The frozen context, OCI archives and build logs are in `.codex/nep413-predeploy-enclave-repro`. The verified Buildx binary is retained in that configuration's `cli-plugins` directory. Do not change or reuse another lane's builder.

## Measured manifest

After independent review and any separately approved image publication, generate the manifest from its reviewed repository digest:

```sh
python scripts/enclave_compose.py --image registry.example/siyab@sha256:YOUR_REVIEWED_64_CHARACTER_LOWERCASE_DIGEST > app-compose.json
```

The example digest marker is deliberately rejected by the generator. Supply the actual digest. The generator emits UTF-8 with LF endings. Keep those exact bytes. `docker_compose_file` is the embedded YAML string, not a pathname. The measurement hashes the original `app_compose` bytes, including whitespace.

The generated manifest has one service, an empty `allowed_envs` list, no KMS or local key provider, no gateway, no sidecar, no shell command and no application host data volume. It mounts only the guest socket. The filesystem is read only; `/tmp` is an ephemeral 16 MiB tmpfs for request evidence. All capabilities are dropped, privilege escalation is disabled, and core dumps are disabled. The socket's read only mount does not restrict guest API methods. The measured service calls only the legacy dstack v0 `Info` and `GetQuote` methods. The current v1 `Attest` surface is not implemented by this adapter. The pinned API sources and compatibility bounds are recorded in `docs/lanes/attest-enclave.md`.

At the pinned dstack source revision, both provider flags being false selects the `none` provider. Guest setup creates temporary application keys and unconditionally emits the `key-provider` event with JSON name `none` and an empty id. The application does not use these guest keys. The default event log version is V1. On TDX, runtime events extend RTMR3 with event type 134217729 and the frozen spec's little endian SHA384 formula. The default V1 JSON omits the optional version and preimage fields, preserving the five event fields. These are source verified facts; a real guest dump remains required to establish the deployed OS behavior.

Port 8080 is direct HTTP. TLS does not terminate inside this image; the attestation auxiliary value is zero and makes no TLS endpoint binding claim. Do not claim the manifest establishes an authenticated public TLS endpoint. Confirm the uid 65532 socket access on a hello-world CVM before adopting this manifest. Do not grant broad capabilities, add a shell, change the service uid or add an unmeasured configuration volume to repair an access failure.

## Owner validation on Phala

First use an owner approved hello-world CVM to dump `Info` and `GetQuote`, confirm the guest API version, socket permissions and Intel TDX platform, and verify the quote offline with dcap-qvl before authorizing Siyab spending. No real CVM, registry transaction, signer domain, image publication or paid model call is part of these build commands. Keep every real step under the owner's separate control. The legacy v0 `GetQuote` used here returns TDX only; GCP verification also needs its vTPM evidence, so this path cannot establish the full GCP platform claim.

## Local proof on 22 November 2026

Two NEP-413 predeploy uncached builds completed sequentially in separate BuildKit containers with separate cache volumes on `feat/nep413-workspace` at `8393b2c3a` with the uncommitted review fixes. The current production source and Dockerfile were unchanged during both builds. Both consumed the same frozen 26 file USTAR context, fixed build arguments, Buildx v0.39.1 and the digest pinned amd64 BuildKit v0.35.1 image. The retained project local Buildx binary was copied into a separate private configuration and rechecked against a fresh copy of its official release SHA256. A new blank project local Docker configuration selected the local Unix Docker daemon through WSL root. Neither the user's Docker configuration nor any existing builder was changed. Each builder had a two CPU quota and worker parallelism two. The build commands took 51.266 and 35.781 seconds.

Both OCI index byte hashes, image manifests, configs and all 12 layer descriptors matched. All 14 blobs in each archive were independently hashed, matched their descriptors and sizes, and accounted for by the index and manifest. Both produced manifest prefix `b3bcc3d69ed5` and config prefix `12afbd083d4c`. All 26 source hashes were checked before freezing the context, after each build and after runtime validation. The only changed source pin was `intel/budget.py`, from prefix `576141cc3230` to `4901a9e2a7d8`. The build regeneration edited no runtime source. The context hash prefix is `3572415242e4` and the index hash prefix is `effc9a81d3d6`. The full public digests, input file hashes, build settings and tool versions are in `enclave/build-record.json`. The record proves these inputs built identically twice in this local environment. It does not prove another compiler or future source revision produces the same image. The build test checks that its recorded input hashes still match the source tree.

The compose generator ran twice with `registry.example/siyab` and that manifest digest. Both outputs were the same 826 UTF-8 bytes with LF endings, retained as `.codex/nep413-predeploy-enclave-repro/app-compose.json`. Their SHA256 prefix is `44776e2cd93d`; the full image reference and digest are recorded as `app_compose_image` and `app_compose_sha256` in `enclave/build-record.json`. This is a local review manifest using an example repository, not a published image or approved deployment measurement. Selecting another repository changes the compose bytes. A full measurement id still requires the actual guest registers and key provider digest, so none is invented from the image alone.

The exact OCI image loaded into the local Docker daemon as `siyab-nep413-predeploy-enc:final-20261122`. An offline runtime smoke checked its uid and gid, baked configuration, dependency imports, missing shell and SSH executables, missing relay and refusal of a testnet registry in a mainnet build. It also verified the final one hour refresh setting, the reviewed budget limits and all 25 copied input file hashes inside the image. All six build tests passed in 0.805 seconds with no skips. The image validation used a read only source mount, disabled external networking and ephemeral `/tmp`. The requested host validation uses TEMP and TMP at `C:\t\okx` and is recorded in `docs/lanes/nep413.md`. Current and earlier build counts and commands are separated in `docs/lanes/nep413-enclave-build.md`. The earlier broader compatibility results remain in `docs/lanes/attest-enclave.md`. The whole source mount is for tests that import repository test helpers. The generated CVM manifest has no such mount.

```sh
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges --ulimit core=0 \
  --mount "type=bind,src=$PWD,dst=/work,readonly" --workdir /work \
  --env TEMP=/tmp --env TMP=/tmp --env TMPDIR=/tmp \
  --entrypoint python siyab-nep413-predeploy-enc:final-20261122 -E -s -B \
  -m unittest tests.test_enclave_build -v
```

To reload the recorded image, use `docker load --input .codex/nep413-predeploy-enclave-repro/one.tar` and then tag its returned image id as `siyab-nep413-predeploy-enc:final-20261122`. The returned id matches the manifest digest in `enclave/build-record.json`. No build test or runtime smoke started the real guest service or contacted a public provider.

Both private predeploy builders and their cache volumes were removed after verification. The unique local test image remains for the orchestrator's Linux lane and socket tests. The reviewed OCI archives, frozen context, logs and verified project local Buildx binary remain under ignored `.codex` paths for independent inspection and reload. The earlier NEP-413 proof remains intact in `.codex/nep413-enclave-repro` with manifest prefix `cfca8fbbc606` and local image `siyab-nep413-enc:final-20261122`. Its validation belongs to that earlier image. Prior archives, shared base images and existing Docker resources were preserved.

## Primary sources

The base amd64 manifest was read from the official Docker registry, using its public pull token flow. The digest was checked against the pinned Dockerfile value. Wheel URLs and hashes were checked against each official PyPI release JSON.

https://registry-1.docker.io/v2/library/python/manifests/3.12.10-slim-bookworm

https://pypi.org/pypi/PyNaCl/1.6.2/json

https://pypi.org/pypi/cffi/2.0.0/json

https://pypi.org/pypi/pycparser/3.0/json

https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/crates/dstack-cli-core/src/compose.rs

https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/docs/security/cvm-boundaries.md

https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/guest-agent/rpc/proto/agent_rpc.proto

https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/sdk/curl/api.md

https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/docs/guest-api-v1.md

https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/dstack-util/src/system_setup.rs

https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/dstack-types/src/lib.rs

https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/dstack-attest/src/lib.rs

https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/cc-eventlog/src/runtime_events.rs

https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/cc-eventlog/src/tdx.rs

https://github.com/docker/buildx/releases/tag/v0.39.1

https://github.com/docker/buildx/releases/download/v0.39.1/checksums.txt

https://github.com/docker/buildx/blob/v0.39.1/docs/reference/buildx_build.md

https://docs.docker.com/build/builders/drivers/docker-container/

https://docs.docker.com/build/exporters/oci-docker/

https://registry-1.docker.io/v2/moby/buildkit/manifests/v0.35.1

https://github.com/moby/buildkit/blob/v0.35.1/docs/build-repro.md

https://docs.docker.com/build/ci/github-actions/reproducible-builds/
