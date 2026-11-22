# Attestation enclave

22 November 2026. T2 on `feat/attest-enclave` at `213ed0503`, rebased onto main `6dc9bc89e`. The original T0 base `c4899e190` still supplies the fixed build epoch. The frozen interface is `docs/spec/attest-v1.md`. The design's older registration shape is not used. Changes stay uncommitted for the orchestrator's independent review.

The current build record was regenerated on the NEP-413 branch at `8393b2c3a` with its uncommitted predeploy review fixes after the budget input changed. Current build artifacts and validation are recorded in `docs/lanes/nep413-enclave-build.md`. The earlier NEP-413 proof and T2 validation remain historical and are preserved separately.

## Implemented

`enclave/service.py` exposes `POST /verdict`, `GET /attestation` and `GET /health` on port 8080. Both key bearing routes require the private relay's per boot bearer token. Public health contains no public key, signer or quote. Authentication remains required after registration. The verdict request fields are `kind`, `token`, `receiver`, `amount`, `memo`, `gate` and `agent_pk_hex`. A `policy` member is accepted and ignored. Other unknown fields are refused. The service reads `get_agent` and `get_policy` through `gate.agent.read_policy_views` for every verdict, validates those results and calls `gate.verdict.request` with `sign_actions=("sign",)`. Paused or disabled policy, unreadable checks and policy that requires a human cannot produce a signature.

The image contains the network and registry settings. All RPC, archival and NearBlocks clients explicitly use mode `off` and fixed endpoints from the source. Environment endpoint, fixture and policy overrides do not select service behavior. A mainnet image refuses a testnet Gate, including the root account `testnet`. The mainnet issuer checks and existing rules run unchanged. Rules version 1 is the current `gate.verdict.request` behavior at this base; this lane adds no alternative rule engine, model call or remote injection scanner.

The result preserves the original seven field Verdict. It adds the exact evidence bytes as a UTF-8 JSON string in `evidence`, plus `signer` with exactly `public_key_hex`, `measurement_id` and `registry`. The string encodes back to the bytes whose SHA256 the Verdict signs. Internal evidence paths are removed. Temporary evidence is deleted after each request; the deployment compose places its temporary directory in guest memory. The floor and current label hashes are also returned outside the Verdict.

`enclave/keys.py` creates a fresh PyNaCl key using `SigningKey.generate()` with no constructor arguments. There is no key import or export endpoint, file load, persistence or generic signing endpoint. `key_id` is `cur`; only 32 byte digests are accepted by its internal `sign` method. Pickle serialization is refused. Python and PyNaCl necessarily hold the signing object in process memory; this is not a memory zeroization claim.

At boot and every attestation refresh the service reads a final NEAR block's nanosecond timestamp, floors it to milliseconds, constructs the frozen report binding and obtains a raw quote. It compares the quote's actual report data, every supplied TCB register and the complete RTMR3 replay. The initial measurement becomes this process's identity. A changed measurement permanently disables that process, even if a later reply returns to the old measurement; restarting creates a new key. A failed renewal preserves the previous unexpired cache. An expired or absent cache, a backwards chain clock or a quote at least 24 hours old refuses signing. A monotonic 24 hour local deadline also prevents a stalled chain read from keeping the cache available forever. Registry lease and collateral eligibility remain the consumer's responsibility and may expire earlier.

`GET /attestation` returns only a deep copy of that cache and never quotes or reads the network. The background worker refreshes every hour and retries an unavailable renewal after 60 seconds. An unchanged chain issue time preserves the previous monotonic deadline and cannot revive an expired cache. Network reads hold neither the state lock nor the verdict signing lock. Each verdict has separate client response metadata and block cache. Shared NearBlocks spacing and budget reservations are atomic through an enclave local client subclass; only reservation is locked. Label additions merge against the latest store under the state lock, preventing lost additions. The final signing step checks readiness and that its label snapshot is still current. A new threat or measurement fault during judgement refuses the signature. An older request cannot invalidate a newer successful renewal.

The hourly quote cadence is separate from onchain lease renewal. The frozen spec requires the issue time to be no more than two hours before the final successful registry callback. Failed refreshes can leave a locally usable signing cache whose quote is too old for registration. The relay must check final chain time and leave margin for collateral fetching, submission and callback execution; the cadence alone is not a freshness guarantee. Relay renewals follow the actual accepted lease and collateral expiry, which can be shorter than the example 24 hour lease.

Each HTTP connection has one absolute ten second deadline for reading its request line, headers and body, including clients that keep sending small chunks. The eight connection limit is retained. RPC reads have the separate existing 45 second request budget. Response errors contain neither upstream details nor key material.

TLS ends outside this service. Its auxiliary binding is exactly 32 zero bytes. This makes no TLS endpoint or query privacy assertion. Health reports local quote readiness, not DCAP acceptance, registry registration, complete provider coverage or permission to pay. There is no `POST /approval` in T2; that is T4a's separate work.

## Labels

`enclave/labels.py` retains the baked `intel/labels.jsonl` floor. The only accepted delta shape is `{"add":[<complete label assertion objects>]}`. Each added row must pass the existing label validator, be a current `denylist` or `incident` assertion and never supersede or mutate an existing row. Inferred rows still require a current cited incident on the same subject and chain. A repeated identical addition is idempotent. Removals, replacement operations, nonthreat categories and supersessions are refused atomically.

An optional HTTPS delta URL is selected at image build time. It is unset by default. If configured, every verdict uses the enclave's dedicated HTTPS fetch. The body is capped at 1048576 bytes with or without Content-Length, including chunked responses. All redirects are refused, including another path on the same HTTPS host. Invalid or repeated lengths, truncation, non-200 responses and transport failures refuse the request. The existing RPC deadline adapter bounds header and body reads to the earlier of ten seconds and the verdict deadline. Strict JSON parsing still refuses duplicate members and nonfinite numbers. No RPC source file was changed.

Accepted additions accumulate for the process lifetime, so a later empty delta cannot remove them. Only the baked floor is guaranteed across restarts. A delta publisher must retain its cumulative additions; this lane does not claim a rollback resistant external label log. Promote any required durable floor change into a reviewed new image. Evidence still has its existing label-store hash; adding a dedicated attestation block inside evidence belongs to a later lane because `gate/verdict.py` is frozen here.

## Guest API and measurement

`enclave/dstack.py` uses the legacy dstack v0 HTTP API over `/var/run/dstack.sock`. The verified raw endpoints are `POST /Info` with `{}` and `POST /GetQuote` with `{"report_data":"<128 lowercase hex characters>"}`; the same legacy methods are also available under `/v0/`. The upstream API document describes v0 as frozen since v0.5.11. `GetQuote`'s `RawQuoteArgs` does not take `hash_algorithm`. That option belongs to the legacy `Tappd.TdxQuote` API. Current v1 uses `POST /v1/Attest` and MessagePack V1 evidence. This adapter does not implement v1; v1 Info does not supply the old `tcb_info` object.

Info returns `tcb_info` as a JSON string. Its decoded object contains MRTD, RTMR0 through RTMR3, `compose_hash`, the original `app_compose` string and an `event_log` array. GetQuote returns lowerhex `quote`, lowerhex `report_data` and a separately JSON-encoded event-log string. At dstack revision c87303818753e7abb3233bd1f5a4b611cb981154 the GetQuote serializer strips payloads for events whose type is not 134217729, except IMR0/type10 with exact payload `ACPI DATA`. Info retains raw payloads. The service compares only ordered RTMR3 events, after independently validating Info's full schema, digests, register equality and RTMR3 replay against the quote. A quoted RTMR3 digest may be empty for older deployments; any nonempty digest must equal the independently checked Info digest. Event types, names, payloads, order and count must agree. Replies, headers and request lifetime are bounded. Duplicate JSON members, invalid HTTP framing, redirects, truncation and missing socket support fail closed. The socket tests use real local Unix sockets under WSL.

`enclave/measure.py` implements the exact report binding, six field measurement hash, TD10 version 4 and version 5 offsets, all production TD attribute checks and event replay. Every event has the frozen five field schema, including events outside RTMR3. Selected RTMR3 events require the fixed type and independently recomputed SHA384 digest. Unique compose and key provider events, exact compose bytes and all quoted registers are checked. `scripts/enclave_measure.py` prints the measurement id for a six field measurement JSON document. Repeating the same input produces the same id.

These checks are structural and do not authenticate a quote signature, collateral or freshness. The registry and an independent offline verifier must do that. The real GLM gateway capture identifies its deployment as dstack-0.5.12. Its 30 event logs differ in all 30 rows: 20 boot payloads are stripped and ten quoted RTMR3 digests are empty. The service regression retains both logs verbatim and replaces only the captured quote's report data with the fresh test binding. That replacement invalidates the retained signature, so reaching ready proves structural compatibility only. Its replay measurement remains unchanged. The ordinary fake now reproduces the pinned serializer, including the ACPI exception. Real retained upstream and NEAR AI captures also exercise register comparison and event replay without changing their bytes. Current dstack source describes newer event preimages and GCP vTPM evidence. An incompatible actual log is refused; this TDX-only path does not prove a complete GCP attestation. A real Phala CVM dump is still required before choosing a host or approving a measurement.

## Private bootstrap and rotation

At process startup the server generates a separate 32 byte random relay credential and prints it once as private stdout JSON under `relay_bearer_token`. It is not the enclave signing seed. The token accepts exactly one `Authorization: Bearer <token>` header, compared in constant time. Missing, forged or repeated authorization receives 401 before any key bearing service work. The token is neither accepted as a runtime input nor returned by HTTP. The measured compose disables public logs. Keep its startup output and bootstrap capture within the private relay session. The direct HTTP port has no attested TLS binding; carry relay access over a private local connection or authenticated encrypted forwarding.

The relay uses the token for both attestation and verdict requests throughout the boot. There is no public pre-registration key discovery route and no registry mutation or readiness claim inferred from authentication. Only a confirmed registry record establishes registration. Do not publish bootstrap captures or prepared registration arguments before that confirmation. The relay and registration transport can still disclose a key. If a disclosed key is squatted under another measurement, restart the enclave to generate a new signing key and token, capture its new attestation privately, register the new key after separate approval, read back the accepted record, and resynchronize every Gate that uses it. Renewal cannot overwrite the squatted record's identity, including after its expiry. No registry reset or global deletion is assumed.

## Relay

`enclave/relay.py` runs on the untrusted host. It accepts a captured service attestation and a collateral JSON file, validates report binding and replay, and constructs exactly `public_key`, `issued_at_ms`, `aux`, `verification_args`, `app_compose` and `event_log` for `register_key`. The tuple is the complete Borsh `(QuoteBytes, Collateral)` in upstream field order, encoded as lowerhex. Both optional PCK chain encodings reproduce the frozen vectors. Truncated and trailing tuple bytes are refused by the decoder.

The default is offline command preparation. An explicit `--fetch-collateral` instead uses dcap-qvl 0.7.0 with this quote's embedded chain and Intel's public collateral endpoint. It creates the upstream awaitable inside a running event loop and bounds the complete fetch to 60 seconds. There is no other machine's cached PCK chain substitution.

`--send` plus `--key-file`, explicit relayer and explicit network are all required to execute near-cli. The key path must be outside the source tree; this Python process does not read it. The CLI receives an argv list without a shell. Its stdout and stderr are not printed. The required registration budget is exactly 0.01 NEAR and 300 Tgas. A zero CLI exit code is not proof that a final registry callback accepted a key: the owner must inspect the result and read back `get_key`.

Installed near-cli was unavailable in both Windows and WSL. The grammar was checked against official near-cli-rs 0.30.1 source. Actual near-cli signing and sending were not exercised. No registration transaction, public verifier call, owner key file access or collateral fetch was performed by this lane.

## Build and compose

The recipe and exact local build record are in `enclave/BUILD.md`. `enclave/Dockerfile` pins an amd64 base manifest and hash locked wheels. `scripts/enclave_compose.py` generates the actual app-compose JSON around one service and an immutable image digest, with an empty `allowed_envs` list. There is no sidecar, SSH server or runtime shell. The service has a read-only root, dropped capabilities, no-new-privileges, a disabled core dump and memory backed temporary evidence. Only this container mounts the guest socket. No chain storage is added by T2, so this lane has no prefix table rows for `docs/UPGRADES.md`.

Two independent uncached NEP-413 predeploy builds in separate BuildKit instances produced identical OCI index bytes, manifests, configs and all 12 layers. The pinned tools were Docker 29.1.3, Buildx 0.39.1 and BuildKit 0.35.1, with `SOURCE_DATE_EPOCH=1795304440`, disabled provenance and SBOM, and exporter timestamp rewriting. The current manifest prefix is `b3bcc3d69ed5` and config prefix is `12afbd083d4c`. The only changed image input was `intel/budget.py`, now prefix `4901a9e2a7d8`. Full public hashes, all 26 input file hashes and build settings are retained in `enclave/build-record.json`. All 14 blobs per archive matched their descriptors and sizes. The 25 copied input files were also hashed inside the final image and matched the frozen source context. This proves the recorded inputs built identically in this environment; it does not claim equivalence across arbitrary tools or future revisions.

The exact current OCI image passed all six build tests with no skips and an offline runtime smoke. Current validation is recorded in `docs/lanes/nep413-enclave-build.md`; Fix round 3 below records the earlier T2 run. Private builders and their cache volumes were removed after verification. The unique local image `siyab-nep413-predeploy-enc:final-20261122`, ignored archives under `.codex/nep413-predeploy-enclave-repro` and verified project local Buildx remain available for review and reload. The earlier NEP-413 proof at `.codex/nep413-enclave-repro` and its local image `siyab-nep413-enc:final-20261122` remain intact. Their broader validation is historical. The user's Docker configuration and other builders were untouched.

The compose generator ran twice with `registry.example/siyab` and that manifest digest. Both outputs were the same 826 UTF-8 bytes with LF endings, retained as `.codex/nep413-predeploy-enclave-repro/app-compose.json`. Their SHA256 prefix is `44776e2cd93d`; the full image reference and digest are in `enclave/build-record.json`. No image was published. Changing the repository changes those compose bytes. A full deployment measurement still needs actual guest registers and the key provider digest, so this local build does not supply or approve one.

At the checked dstack revision, disabling both key provider flags selects provider `none` and still emits a unique `key-provider` event into RTMR3. Its V1 event format is the default and matches the frozen five fields and replay formula. Temporary guest application keys are separate from the service's fresh signing key and are not used by it. These are boot source facts, not observations of a Phala machine. The exact primary URLs and owner probe remain in the build record document.

Do not approve a mutable image tag or infer that an image digest alone is a full TDX measurement. The compose bytes, key provider, guest OS registers and platform policy are also part of the verified identity. The image used after T4a must be rebuilt and measured again.

## Owner sequence

Start a hello-world Phala CVM first (Recommended). This is an owner action with a hosting bill, not something this lane executed. Disable debug and SSH access. Confirm the nonroot service can use the guest socket without adding a sidecar or widening environment overrides. From the hello-world guest, retain public API captures outside this repository:

```sh
curl --fail --silent --show-error --unix-socket /var/run/dstack.sock \
  -H 'Content-Type: application/json' --data '{}' \
  http://localhost/Info > /tmp/siyab-info.json
python3 -c 'import json; print(json.dumps({"report_data": bytes(64).hex()}))' | \
  curl --fail --silent --show-error --unix-socket /var/run/dstack.sock \
    -H 'Content-Type: application/json' --data @- \
    http://localhost/GetQuote > /tmp/siyab-quote.json
```

These zero report-data bytes are a hello-world format probe, not an enclave key registration. Download the captures and obtain collateral for that exact quote. In an external Python environment with dcap-qvl 0.7.0, the following first command only fetches public collateral. The second command verifies from retained files without a network request. Run both before any onchain spend, using current trusted verifier time rather than a recording's pinned time.

```sh
python - /external/siyab-quote.json /external/collateral.json <<'PY'
import json, sys
from pathlib import Path
from enclave.relay import fetch_collateral
quote = bytes.fromhex(json.loads(Path(sys.argv[1]).read_bytes())["quote"])
Path(sys.argv[2]).write_text(json.dumps(fetch_collateral(quote)), encoding="utf-8")
PY
python - /external/siyab-quote.json /external/collateral.json <<'PY'
import json, sys, time
from pathlib import Path
import dcap_qvl
from enclave.measure import parse_quote
quote = bytes.fromhex(json.loads(Path(sys.argv[1]).read_bytes())["quote"])
collateral = dcap_qvl.QuoteCollateralV3.from_json(Path(sys.argv[2]).read_text())
result = dcap_qvl.verify(quote, collateral, int(time.time()))
assert result.status == "UpToDate", result.status
parse_quote(quote)
print({"status": result.status, "advisory_ids": result.advisory_ids})
PY
```

Retain the advisory ids. Their presence alone is not a refusal in the frozen spec. The hello-world check proves hardware quote compatibility only. Inspect the Info registers, event format, production attributes, key provider and exact compose before approving Siyab's image. Stop if the fleet cannot meet the spec. No result from this lane authorizes funding or a transaction.

After that probe and an approved image build, retain the service's one startup stdout JSON record as `/external/private-boot.json` within the private relay session. Use the local forwarded port to fetch the cached attestation. The following capture reads a relay credential, not a signing key, and sends no transaction:

```sh
umask 077
BOOT_TOKEN=$(python3 -c 'import json; print(json.load(open("/external/private-boot.json"))["relay_bearer_token"])')
curl --fail --silent --show-error -H "Authorization: Bearer $BOOT_TOKEN" \
  http://127.0.0.1:8080/attestation > /external/attestation.json
unset BOOT_TOKEN
```

Use the same private authorization for the host's verdict forwarding. Repeat the offline verification with the attestation's quote and collateral, and compare the complete replay-derived measurement against the reviewed image policy. To prepare a registration without sending:

```sh
python -m enclave.relay --attestation /external/attestation.json \
  --collateral /external/collateral.json --relayer RELAYER.testnet --network testnet
```

`RELAYER.testnet` is notation: substitute the actual lowercase funded relayer account. After separate owner approval, the only send form is:

```sh
python -m enclave.relay --attestation /external/attestation.json \
  --collateral /external/collateral.json --relayer RELAYER.testnet --network testnet \
  --send --key-file /external/relayer.json
```

The registry id is taken from the captured binding and must match the image's reviewed registry. Mainnet requires its own reviewed mainnet image, registry and explicit network selection. Read back the key and its lease after registration, then use T3's Gate synchronization path. Fetch the worker's later cached chain-timed quote through the same authenticated route and relay it before lease expiry. A restart generates a new key and token requiring a new private capture, registration and Gate sync. No paid model call is required by T2.

## Earlier validation

All 113 lane tests passed together under WSL Ubuntu 24.04 and Docker in the exact final image's Python 3.12.10 runtime, with the source mounted read only and external networking disabled. There were zero failures and zero skips. The modules contain 6 build tests, 12 Unix socket tests, 18 label tests, 20 measurement tests, 13 relay tests and 44 signer, service and HTTP tests. Fix round 1 adds 32 tests to the original 81. The shared relevant vectors include all three report bindings, both measurement digests, all 66 TD attribute cases, all six quote-prefix cases and both registration tuple encodings. The 47 reference tests in the compatibility run separately reproduce every frozen vector. Tests also exercise retained upstream and NEAR AI event logs. The measurement CLI is invoked twice on the same input and its bytes match; duplicate JSON members are refused.

The core compatibility command below ran 600 tests under WSL in the pinned Python 3.12.10 image with PyNaCl 1.6.2, cffi 2.0.0 and pycparser 3.0: 593 passed, 5 skipped, 1 failed and 1 errored. Four skips are absent py-near; the fifth requires Node for the private graph download boundary. That exact Node case passed separately on Windows with Node 24.22.3. The two inherited issues also reproduce there in the isolated three case command below. Their test and production source files are byte unchanged from the base. Across the 713 distinct lane and compatibility cases, 707 passed on at least one tested platform, 4 skipped and 2 retain the inherited failures. The full repository suite was not run.

The final service and label modules also passed all 62 cases on Windows. The original log mismatch, request serialization, stale label signing, failed refresh behavior and NearBlocks reservation race were reproduced before their fixes. The last timer and unchanged chain time regressions also ran red before the final changes. Independent read only source review found no remaining scoped code blocker after those changes.

```sh
python -m unittest tests.test_enclave_build tests.test_enclave_dstack \
  tests.test_enclave_labels tests.test_enclave_measure tests.test_enclave_relay \
  tests.test_enclave_service -v
python -m unittest tests.test_agent tests.test_canon tests.test_fuzz tests.test_http \
  tests.test_investigation_jobs tests.test_mainnet_run tests.test_mcp tests.test_record_latency \
  tests.test_reports_rules tests.test_spec tests.test_token_check tests.test_verdict \
  tests.test_verify tests.test_labels tests.test_evidence tests.test_ed25519 \
  tests.test_rpc tests.test_attest_vectors -v
python -m unittest \
  tests.test_investigation_jobs.InvestigationJobsTest.test_authorized_private_graph_download_preserves_exact_requested_bounds \
  tests.test_spec.PageTest.test_the_index_lists_every_page \
  tests.test_mcp.McpLiveSigningTest.test_a_live_allow_is_signed_with_the_configured_key_for_the_configured_agent -v
python scripts/check_hygiene.py --all
python scripts/check_citations.py
git diff --check
```

The first command requires Linux for all real Unix socket cases and the locked PyNaCl runtime. The exact container invocation and image verification are in `enclave/BUILD.md`. Windows commands used TEMP and TMP at `C:\t\t2f1`. The WSL containers set TEMP, TMP and TMPDIR to the short ephemeral `/tmp`. The compatibility modules do not import enclave code; the final timer and local expiry changes were covered by the repeated complete final lane run. The hygiene, citation and whitespace checks exited zero. Ruff's undefined and unused name checks also passed for all lane Python files. All 11 protected files, including the frozen specification and vectors, were hash checked unchanged. No exact-byte or binary fixture was added, so no new `.gitattributes` entry is needed.

The inherited issues need changes outside this lane:

1. `tests/test_spec.py::PageTest.test_the_index_lists_every_page` enumerates only six pages, while the base already contains `docs/spec/attest-v1.md`. The orchestrator should include that attestation page in the index check and add its link to `docs/spec/README.md`. Preserve the frozen attestation spec. If unifying it with `PAGES`, adapt the version-line check to its existing `Version: 1. Status: ...` spelling rather than changing the frozen line.
2. `tests/test_mcp.py::McpLiveSigningTest.test_a_live_allow_is_signed_with_the_configured_key_for_the_configured_agent` injects three mode `replay` clients into the live signing test. The unchanged verdict service correctly raises `SignerError` because every live client must be off. The orchestrator should replace those fixture clients with off-mode test doubles whose network boundary supplies the recorded responses. Preserve the production refusal and the no-network test guard. Simply changing real client modes would attempt network reads and does not repair the test correctly.

The initial T2 work additionally exercised the dcap-qvl 0.7.0 offline verification API on the retained gateway quote with its own embedded PCK chain and recording time: `UpToDate`, no advisories, no network request. That validates the owner command's API shape; it was not repeated in fix round 1 and is not a new Phala quote or current deployment claim.

## Fix round 1 files

This round changes `enclave/service.py`, `enclave/labels.py`, `enclave/build-record.json`, `enclave/BUILD.md`, `tests/test_enclave_service.py`, `tests/test_enclave_labels.py`, `tests/test_enclave_build.py` and this report. The other files in the full T2 inventory below are retained from the previous round. The owner has no transaction or paid call to run for these offline fixes. The real CVM, current collateral validation, socket permissions and any separately approved registration remain deferred to the exact owner commands above. They could not be exercised without the owner's host and the separately authorized live path. No paid call or public transaction was attempted.

## Fix round 2

Only `tests/test_enclave_service.py` and this report change in this round. The optional PyNaCl import and `unittest.skipIf` follow `tests/test_verdict.py`. The three signer tests that construct a key and the shared service setup skip with `PyNaCl is not installed; run in the enclave image` when `nacl.signing` cannot be imported. No runtime module, image input or CI job changes. All 44 service module tests and all 113 lane tests remain present.

The constructor argument refusal test still runs without PyNaCl. Configuration validation, fake guest event serialization and the request reader deadline test now use a separate case without signer setup, so they also run without PyNaCl. The other five enclave modules remain dependency free and run subject to their existing platform requirements. The production signer still requires PyNaCl and has no fallback.

Before the fix, plain Windows Python 3.12.10 reproduced 43 errors, all from the missing `nacl` module. After the fix, the same discovery command has no errors or failures. Windows has no AF_UNIX support in this interpreter. Its nine affected socket test methods produce 32 skip records because several methods have subtests. Together with 40 PyNaCl skips, unittest prints `OK (skipped=72)` for 113 discovered methods. Exactly 64 methods pass and 49 methods skip. The existing 32 socket skip records were also present before this fix.

| Check | Result |
|---|---|
| Plain Windows Python 3.12.10 without PyNaCl | 113 discovered, 64 passed, 49 methods skipped, 0 failures, 0 errors; 72 skip records as explained above |
| Final enclave image through WSL, Python 3.12.10 and PyNaCl 1.6.2 | 113 passed, 0 skipped, 0 failures, 0 errors |
| `python scripts/check_hygiene.py --all` | Exit 0 |
| `python scripts/check_citations.py` | Exit 0 |

The Windows command uses TEMP and TMP at `C:\t\t2f2`. No PyNaCl package was installed into Windows or CI. From the repository root:

```powershell
$env:TEMP = 'C:\t\t2f2'
$env:TMP = 'C:\t\t2f2'
python -m unittest discover -s tests -p 'test_enclave*.py' -v
python scripts/check_hygiene.py --all
python scripts/check_citations.py
```

The second run used the existing final image `siyab-t2f1:final-20261122` with a read only source mount and no external network. Its installed dependencies remain PyNaCl 1.6.2, cffi 2.0.0 and pycparser 3.0. From the repository root in WSL, with access to the local Docker daemon:

```sh
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges --ulimit core=0 \
  --mount "type=bind,src=$PWD,dst=/work,readonly" --workdir /work \
  --env TEMP=/tmp --env TMP=/tmp --env TMPDIR=/tmp \
  --entrypoint python siyab-t2f1:final-20261122 -E -s -B \
  -m unittest discover -s tests -p 'test_enclave*.py' -v
```

The standard library supports conditional method and class skips before their setup runs. This was checked against the installed Python 3.12.10 source and both official CI version documents: https://docs.python.org/3.11/library/unittest.html#skipping-tests-and-expected-failures and https://docs.python.org/3.12/library/unittest.html#skipping-tests-and-expected-failures. These are the only new external API facts needed for this test change.

The frozen spec, vectors, runtime source, build record and all protected files remain unchanged. The owner has no command or paid action to run for this fix. The full repository suite remains with the orchestrator. Existing live CVM and registration work above remains deferred; this round adds no live step. No commit, push, deployment, PR, remote change, tag mutation, transaction, owner key access or paid model call occurred.

## Fix round 3

The rebase left three stale source pins in `enclave/build-record.json`: `gate/agent.py`, `gate/rules.py` and `gate/verdict.py`. The existing build test reproduced the first mismatch before regeneration. Their current SHA256 prefixes are `3959295f1082`, `8a10d4ccef53` and `fbd6120d04f4`, respectively. All 26 source hashes were regenerated from the current tree, and the independent build tooling verified every blob and descriptor from both new OCI archives before updating the record. No runtime or test source changed.

Both builds used the unchanged Dockerfile, lockfile and fixed epoch. The record has since been regenerated for the NEP-413 predeploy fixes. Its current frozen context hash prefix is `3572415242e4` and OCI index byte hash prefix is `effc9a81d3d6`. The current manifest, config, compose hash and exact retained artifact paths are in Build and compose above and `enclave/BUILD.md`; `docs/lanes/nep413-enclave-build.md` records the new comparison. The record includes the example repository as `app_compose_image` and its exact generated bytes' hash as `app_compose_sha256`. This manifest is for local review. It does not name a published repository or replace the required real CVM measurement.

The enclave image uses Python 3.12.10, PyNaCl 1.6.2, cffi 2.0.0 and pycparser 3.0. All 113 enclave tests passed with zero skips and no external network. Plain Windows Python 3.12.10 has no PyNaCl or AF_UNIX support: 113 methods were discovered, 64 passed and 49 methods skipped, with zero failures or errors. The raw summary is `OK (skipped=72)`, comprising 40 PyNaCl skips and 32 Unix socket skip records across nine methods. These are the existing optional dependency and platform skips.

| Final check | Rebuilt enclave image with PyNaCl | Windows host without PyNaCl |
|---|---|---|
| Six enclave modules | 113 passed, 0 skipped | 113 methods, 64 passed, 49 methods skipped; 72 skip records |
| `tests/test_spec.py` | 17 passed | 17 passed |
| `tests/test_citations.py` | 11 passed | 11 passed |
| Total distinct requested methods | 141 passed, 0 skipped | 141 methods, 92 passed, 49 methods skipped |
| `python scripts/check_hygiene.py --all` | Run on host | Exit 0 |
| `python scripts/check_citations.py` | Exit 0 | Exit 0 |

All final runs have zero failures and errors. The initial one test mismatch reproduced before regeneration; all six build tests then passed separately on both platforms before the complete enclave runs. The unchanged runtime smoke verified the baked configuration and all 25 copied image input hashes. The earlier spec index failure does not recur on this rebased tree. The earlier broader compatibility suite was not repeated. The frozen spec, vectors, all protected files and submission tag remain unchanged. Whitespace checks pass.

Host commands use TEMP and TMP at `C:\t\t2f3`. The image uses the short ephemeral `/tmp` for TEMP, TMP and TMPDIR. From the repository root on Windows:

```powershell
$env:TEMP = 'C:\t\t2f3'
$env:TMP = 'C:\t\t2f3'
python -m unittest discover -s tests -p 'test_enclave_*.py' -v
python -m unittest tests.test_spec tests.test_citations -v
python scripts/check_hygiene.py --all
python scripts/check_citations.py
git diff --check
```

From the repository root in WSL, as a user with access to the local Docker daemon:

```sh
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges --ulimit core=0 \
  --mount "type=bind,src=$PWD,dst=/work,readonly" --workdir /work \
  --env TEMP=/tmp --env TMP=/tmp --env TMPDIR=/tmp \
  --entrypoint python siyab-t2f3:final-20261122 -E -s -B \
  -m unittest discover -s tests -p 'test_enclave_*.py' -v
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges --ulimit core=0 \
  --mount "type=bind,src=$PWD,dst=/work,readonly" --workdir /work \
  --env TEMP=/tmp --env TMP=/tmp --env TMPDIR=/tmp \
  --entrypoint python siyab-t2f3:final-20261122 -E -s -B \
  -m unittest tests.test_spec tests.test_citations -v
```

This round changes only `enclave/build-record.json`, `enclave/BUILD.md` and this report. The owner has no paid action, transaction or command to run for this regeneration. The full suite and independent review remain with the orchestrator. Actual Phala registers, quote acceptance and socket permissions remain unverified because no owner CVM was used; the separately controlled commands in Owner sequence remain the required path before any deployment or registration. No new fixture, prefix table row or `.gitattributes` entry is needed.

The build facts used again this round were checked against these primary sources: the official Buildx v0.39.1 checksums, the Docker container driver's pinned image and dedicated cache behavior, the OCI exporter and its timestamp rewrite option, and BuildKit v0.35.1's fixed epoch behavior. The binary matched the freshly fetched official checksum, both live builder inspections reported v0.35.1, and the local Docker daemon reported 29.1.3. Source URLs:

https://github.com/docker/buildx/releases/download/v0.39.1/checksums.txt

https://docs.docker.com/build/builders/drivers/docker-container/

https://docs.docker.com/build/exporters/oci-docker/

https://github.com/moby/buildkit/blob/v0.35.1/docs/build-repro.md

## Files

- Service: `enclave/service.py`, `enclave/keys.py`, `enclave/labels.py`.
- Guest and evidence adapters: `enclave/dstack.py`, `enclave/measure.py`, `enclave/relay.py`.
- Build assets: `enclave/Dockerfile`, `enclave/requirements.lock`, `enclave/BUILD.md`, `enclave/build-record.json`, `scripts/enclave_compose.py`.
- Measurement CLI: `scripts/enclave_measure.py`.
- Tests: `tests/test_enclave_build.py`, `tests/test_enclave_dstack.py`, `tests/test_enclave_labels.py`, `tests/test_enclave_measure.py`, `tests/test_enclave_relay.py`, `tests/test_enclave_service.py`.
- Documentation: `docs/lanes/attest-enclave.md`.

The orchestrator owns final independent review, the complete suite, documentation folding and any commit. No commit, push, deployment, PR, remote change, tag mutation, public transaction or paid model call occurred. The owner CVM and near-cli live path remain unexercised for the reasons above.

## Primary sources checked

1. Raw guest requests, response strings and socket path: https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/guest-agent/rpc/proto/agent_rpc.proto and https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/sdk/curl/api.md
2. Concrete Info and GetQuote handlers, exact compose string and TCB fields: https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/guest-agent/src/rpc_service.rs
3. App compose manifest: https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/crates/dstack-cli-core/src/compose.rs
4. TD10 layouts and production attributes: https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/quote.rs and https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/verify.rs
5. Replay digest, register extension and key provider: https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/attestation/src/attestation.rs and https://github.com/near/mpc/blob/e691bdb98d68dcf74653da58d043453a6318765f/crates/attestation-types/src/tcb_info.rs
6. Borsh quote and collateral field order: https://github.com/near/mpc/blob/3.18.0/crates/tee-verifier-interface/src/lib.rs
7. Python collateral and offline verify API, event-loop behavior and collateral HTTP timeout: https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/python-bindings/python/dcap_qvl/__init__.py, https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/python-bindings/python/dcap_qvl/_dcap_qvl.pyi, https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/python.rs and https://github.com/Phala-Network/dcap-qvl/blob/94208e2753a854191759e493ca5892be482ad453/src/collateral.rs
8. near-cli-rs transaction grammar and key-file option: https://github.com/near/near-cli-rs/blob/v0.30.1/src/commands/contract/call_function/as_transaction/mod.rs and https://github.com/near/near-cli-rs/blob/v0.30.1/src/transaction_signature_options/sign_with_access_key_file/mod.rs
9. Final block request and nanosecond timestamp: https://docs.near.org/api/rpc/block-chunk
10. PyNaCl key generation, signing and public-key encoding: https://pynacl.readthedocs.io/en/latest/signing/ and https://pypi.org/pypi/PyNaCl/1.6.2/json
11. Exact locked dependency metadata: https://pypi.org/pypi/cffi/2.0.0/json and https://pypi.org/pypi/pycparser/3.0/json
12. Base image manifest verified through the official registry: https://registry-1.docker.io/v2/library/python/manifests/3.12.10-slim-bookworm
13. Guest provider selection, unconditional provider event and V1 replay: https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/dstack-types/src/lib.rs, https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/dstack-util/src/system_setup.rs and https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/dstack-attest/src/lib.rs
14. Build tools and deterministic exporter: https://github.com/docker/buildx/releases/tag/v0.39.1, https://github.com/docker/buildx/releases/download/v0.39.1/checksums.txt, https://registry-1.docker.io/v2/moby/buildkit/manifests/v0.35.1 and https://docs.docker.com/build/exporters/oci-docker/
15. dstack quote serializer and payload stripping at the same pinned revision: https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/guest-agent/src/backend.rs, https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/dstack-attest/src/attestation.rs and https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/dstack/cc-eventlog/src/tdx.rs
16. Legacy v0, v0 aliases and v1 Attest encoding: https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/sdk/curl/api.md and https://github.com/Dstack-TEE/dstack/blob/c87303818753e7abb3233bd1f5a4b611cb981154/docs/guest-api-v1.md
17. Python 3.12 redirect refusal and bounded HTTP body reads: https://docs.python.org/3.12/library/urllib.request.html#urllib.request.HTTPRedirectHandler.redirect_request, https://docs.python.org/3.12/library/http.client.html#http.client.HTTPResponse.read and https://github.com/python/cpython/blob/v3.12.10/Lib/urllib/request.py
18. OS generated relay tokens, hexadecimal byte count and constant time comparison: https://docs.python.org/3.12/library/secrets.html
19. Raw buffered reads and per operation socket timeouts: https://docs.python.org/3.12/library/io.html#io.RawIOBase and https://docs.python.org/3.12/library/socket.html#socket.socket.settimeout
20. Private relay capture header and HTTP failure flags, also checked in installed curl 8.5.0 help: https://curl.se/docs/manpage.html
