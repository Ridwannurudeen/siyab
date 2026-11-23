# NEAR AI lane

21 Nov 2026. Local changes on `feat/nearai-attest`, based on `0e113268c`. Nothing was committed, submitted, deployed or sent to a chain. No paid completion or private key was used.

## Result and current limits

`gate/nearai.py` is the attested transport for the optional NEAR AI Ask provider, injected model recordings and the evaluation recorder and judge. It uses the published `nearai-inference-sdk` 0.3.0 and `dcap-qvl` 0.7.1. The optional live dependencies require Python 3.12. Hosted Ask keeps the existing direct Anthropic path as its default. The ordinary offline path still imports with the standard library. Missing dependencies, a missing policy, failed evidence or a missing model signature cannot release a NEAR AI answer.

The captures do not establish an operational verified model fleet. The original replay in `fixtures/nearai/quote-results.json` failed local verification with dcap-qvl 0.7.1 for DeepSeek candidate 1 and all four GLM candidates, plus their five manager quotes, with `Verification failed: Signature is invalid for qe_report in quote`. Independent Intel DCAP verification accepts all 14 original CPU quotes. The original replay reused a machine-specific PCK certificate chain by FMSPC. Removing that optional saved chain makes dcap-qvl verify each quote against its own embedded chain; all 14 then pass. These were local replay failures, not established provider failures. The cross-check section below records both verifiers and the negative controls.

Both recorded fleets return multiple candidates sharing one signing key. The client rejects that ambiguity before a paid request, following the web documentation's unique preflight match rule. The released SDK accepts a group of matching reports, which is a weaker selection rule. Switch over only after a complete fleet has every candidate verified under an independently reviewed policy, including GPU, deployment and manager evidence, and its retained signers permit an unambiguous response match. A single passing candidate or all CPU quotes passing does not satisfy that condition. [Model preflight and unique match](https://docs.near.ai/cloud/verification/cloud-api/model-attestations)

The positive offline test deliberately selects DeepSeek candidate 0, for which a signed GPU verdict is recorded. It verifies its CPU quote, nonce, signer, configuration, event log, manager actions and NVIDIA signed verdict under a test policy derived from that recording. This is a verifier regression, not production enrollment. The complete report remains rejected. No production measurement policy is supplied or learned automatically.

Running the verdict signer inside a TEE is a design below. This lane does not claim that the existing verdict service or its key has moved into one.

## Verified report API

The endpoint is `GET https://cloud-api.near.ai/v1/attestation/report`. Report retrieval is non-billable and accepts no credential for listed models. Organization-only models require an appropriate API key. A client generates 32 random bytes and supplies their 64-character hexadecimal encoding in `nonce`. The API's authorizations table still says required, but its prose and the successful keyless captures establish the public report behavior. [Report API](https://docs.near.ai/api-reference/attestation/get-attestation-report)

The client makes two reads with its retained challenge. The gateway read requests `signing_algo=ed25519` and `include_tls_fingerprint=true`. The model read requests the canonical `model`, `provider=near`, `signing_algo=ed25519`, `include_tls_fingerprint=false`, and `x-no-aliasing: true`. The second setting matters: requesting TLS binding also changes model report data on the observed API. Model verification uses the separate signer and nonce flow. Neither a gateway report nor a model catalog flag substitutes for model evidence. [Gateway flow](https://docs.near.ai/cloud/verification/cloud-api/gateway-attestation), [model flow](https://docs.near.ai/cloud/verification/cloud-api/model-attestations)

The returned material includes:

| Field | Meaning and check |
| --- | --- |
| `gateway_attestation.intel_quote` | Gateway Intel TDX quote, verified locally before trusting its fields. |
| `model_attestations[].intel_quote` | Each model candidate's separate TDX quote. Every candidate must verify. |
| `request_nonce` | Echo compared to the retained client challenge and to the verified quote. |
| `signing_address`, `signing_algo` | Signing identity bound into quote report data. This client accepts Ed25519 only. |
| `signing_public_key` | Model encryption key checked against the quote-authenticated signer by the SDK. |
| `event_log` | Replayed and compared to the quote's RTMR3. Runtime OS and configuration hashes come from this authenticated log. |
| `info.tcb_info.app_compose` | Raw measured configuration string. `tcb_info` may itself be JSON text. Its original string bytes are hashed without parsing and rewriting the configuration. |
| `nvidia_payload` | GPU evidence. The captures contain `nonce`, `arch` and an eight-entry `evidence_list`; entries have architecture, certificate, evidence and nonce fields. |
| `compose_manager_attestation` | Separate CPU quote authenticating a nonce and the canonical hash of deployment actions. It must be verified, rather than omitted while checking the launcher. |

The standard configuration binding is a 48-byte MRCONFIGID containing byte `01`, SHA-256 of the raw configuration, and 15 zero bytes. The wrapper enforces the exact length and tail, in addition to the SDK's configuration comparison. It also pins MRTD and RTMR0 through RTMR2 by parsing exactly the quote bytes which the local verifier already authenticated. [SDK verification source](https://github.com/nearai/inference-sdk/tree/main/py/src/nearai_inference_sdk/core), [quote properties](https://github.com/Phala-Network/dcap-qvl/blob/v0.7.1/python-bindings/python/dcap_qvl/_dcap_qvl.pyi)

Model weights are not established by the model name. A measured configuration may contain only a Compose Manager launcher. Authenticated actions are records of intended operations, not proof of current container state or successful model-weight execution. Image digests need independent provenance review before policy enrollment. An HTTP 200 from an image registry or provenance search does not verify a build signature. This lane pins approved configuration and action hashes; it does not claim automatic source-build or weight verification. [Image provenance](https://docs.near.ai/cloud/verification/cloud-api/image-provenance), [manager verification](https://github.com/nearai/inference-sdk/blob/main/py/src/nearai_inference_sdk/core/compose_manager_attestation.py)

## Verification and trust

Intel verification runs locally through `dcap_qvl.verify(quote_bytes, collateral, unix_time)`. The SDK retrieves collateral from Intel PCS at `https://api.trustedservices.intel.com`. The pinned verifier uses the embedded Intel production root, rather than a root supplied by the inference server. Cryptographic verification, validity times, revocation collateral and application measurement policy are distinct requirements. The lane accepts only `UpToDate`, no advisories and no debug bit. Unavailable collateral fails closed. [Intel adapter](https://github.com/nearai/inference-sdk/blob/main/py/src/nearai_inference_sdk/utils/intel.py), [production root selection](https://github.com/Phala-Network/dcap-qvl/blob/v0.7.1/src/verify.rs)

GPU verification uses `POST https://nras.attestation.nvidia.com/v3/attest/gpu`. The SDK submits evidence and locally verifies NVIDIA's overall ES384 JWT against `https://nras.attestation.nvidia.com/.well-known/jwks.json`. It checks the NVIDIA issuer, signature, expiry, not-before, issued-at, signed challenge and boolean overall verdict. GPU evidence is required in the lane's policy. A returned unsigned success field is rejected. This depends on NVIDIA NRAS and the signing keys obtained through NVIDIA HTTPS. The SDK adapter does not independently consume detached per-device claims. [NVIDIA adapter](https://github.com/nearai/inference-sdk/blob/main/py/src/nearai_inference_sdk/utils/nvidia.py), [NRAS integration](https://docs.nvidia.com/attestation/poc-to-production/latest/integration-options/remote_verifier.html)

The GPU evidence is tied to the CPU enclave only through their common client nonce. The CPU quote does not bind a GPU identity or a CPU to GPU session. Matching the nonce establishes freshness under those separate evidence checks; it does not establish that the quoted CPU used those GPUs or that a particular set of weights executed on them. [Model evidence verification](https://github.com/nearai/inference-sdk/blob/main/py/src/nearai_inference_sdk/core/attestation_model.py)

A fully local GPU appraisal is a separate integration, not provided by this lane. NVIDIA's current replacement for its deprecated Python tooling is NVAT, with local verifier support and RIM and OCSP dependencies. Its newer CoRIM path is marked experimental. It must not be substituted with a local boolean or an unchecked remote verdict. [NVAT](https://docs.nvidia.com/attestation/nv-attestation-sdk-cpp/latest/overview.html), [migration](https://docs.nvidia.com/attestation/attestation-client-tools-sdk/latest/migration_guide.html)

The gateway report's TLS fingerprint is SHA-256 of the certificate SubjectPublicKeyInfo DER. The free report reader captures the actual peer on the report connection, verifies its quote binding and compares the hashes. The SDK verifies gateway evidence before sending credentials and pins subsequent HTTP connections to that observed SPKI. This is subsequent identity pinning, not a claim that inference reused the initial socket. Model evidence has its own signing key and does not prove a direct client-to-model TLS connection. [TLS contract](https://docs.near.ai/cloud/verification/cloud-api/tls), [SDK pinning](https://github.com/nearai/inference-sdk/blob/main/py/src/nearai_inference_sdk/core/pinned_tls.py)

## Completion binding

The shared client enables SDK E2EE, verifies gateway and model preflight evidence, then requests a non-streamed chat completion. It calls `verify_response(id)` before returning any text or tool call. The SDK retains the exact request and response wire bytes. Only a `provider_tee` receipt with exactly one matching retained verified model report is accepted. Hexadecimal signer spellings are normalized before the preflight uniqueness check. Gateway-only receipts, unsupported algorithms and missing receipts fail closed. No request retry is introduced in the hosted Ask path. [Python SDK](https://docs.near.ai/cloud/inference-sdk/python)

Signatures are retrieved from `GET /v1/signature/{id}?signing_algo=ed25519`, using the API key. A model receipt signs UTF-8 `MODEL_ID:request_hash:response_hash`; a gateway receipt signs only the two hashes. Each hash covers the exact uncompressed wire body. ECDSA receipts use EIP-191; Ed25519 receipts use the reported public key directly. The explicit algorithm avoids different defaults between endpoints. A later report cannot replace the retained preflight. A shared model key cannot identify one serving instance. [Signature contract](https://docs.near.ai/cloud/verification/cloud-api/response-signatures)

The implementation is tied to the inspected published wheel, rather than assuming GitHub main has the same API. The wheel is Python 3.12 or later and exposes asynchronous `InferenceClient`, verification policies and response verification. [Published 0.3.0 metadata](https://pypi.org/pypi/nearai-inference-sdk/0.3.0/json), [dcap 0.7.1 metadata](https://pypi.org/pypi/dcap-qvl/0.7.1/json)

The official SDK includes the Anthropic Python package as a dependency for its optional Messages surface. The optional attested provider uses Chat through NEAR AI. The default hosted Ask provider uses the existing direct Anthropic adapter.

## Ask and recording behavior

Ask defaults to Anthropic and `claude-haiku-5-5`, using `ANTHROPIC_API_KEY_FILE`. Set `SIYAB_ASK_PROVIDER=nearai` to select the attested path and its default `z-ai/glm-5.3-flash`, using `NEARAI_API_KEY_FILE`. `SIYAB_ASK_MODEL` must belong to the selected provider; an unknown provider or mismatched model disables Ask as misconfigured. NEAR AI never silently falls back to Anthropic. If NEAR AI is selected and attestation fails, the existing unavailable answer carries `attestation_failed` and cannot populate the answer cache. Anthropic keeps its original cache key bytes. Other providers include their identity in the key.

A valid cached NEAR AI answer is served before the credential read, live flag, SDK or policy preflight. Disabling live requests or losing a policy does not invalidate existing answers. The provider cache key keeps them separate from Anthropic, and the existing evidence and selection checks still precede a cached answer. `test_cached_answer_precedes_key_read_and_preflight` in `tests/test_ask.py` pins this order.

The public evidence projection, fixed sentence templates, unknown notices, authentication, quotas, two-call concurrency limit, ten-second model deadline, thirty-second total deadline, output cap and selection validation remain in place. The live flag, reviewed policy and exact SDK versions are checked before `ledger.reserve`. A disabled NEAR AI live flag maps to the existing `disabled` answer. Failures known to occur before a completion starts release spend, token and call reservations, including a deadline consumed by the reservation itself. Paid or uncertain failures retain their reservation. Error objects distinguish whether a completion started; no provider exception text or credential is returned.

A healthy daemon event loop serves the process. It retains one asynchronous SDK client per credential and approved policy configuration, so fresh synchronous Ask wrappers share the SDK's 60-second attestation cache. A policy change gets a separate client. The regression uses the real SDK cache and proves that a second call, followed by concurrent calls, starts no new attestation. A timed-out preflight cannot proceed to a completion. The synchronous caller waits at most the requested timeout plus five seconds, since DCAP verification can block the loop. An absolute monotonic deadline is set before queueing; checks at entry and after preflight prevent a stalled or queued verifier from resuming into a late paid request. Unexpected failures map to `attestation_failed` with the observed completion state. `AttestationError` assumes a completion may have started unless the call wrapper knows otherwise. Process shutdown closes the current clients with a bounded wait and clears the global runtime reference even if closing raises; the error propagates. Prior configurations remain retained in a healthy runtime until shutdown; initialize the runtime in each worker after a process fork. The free `attest()` command enforces the same shared-signer refusal as `chat()`. [SDK client lifecycle and cache](https://github.com/nearai/inference-sdk/blob/main/py/src/nearai_inference_sdk/core/inference_client.py), [bounded caller wait](https://docs.python.org/3.12/library/concurrent.futures.html#concurrent.futures.Future.result), [absolute event loop deadline](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.timeout_at)

When the outer wait expires with an unfinished future, that runtime is marked broken and a stop callback is scheduled on its loop from the caller's thread. Its cached attestation is no longer used by new calls. A caller that took the runtime before it was marked broken may finish on it; that attestation was verified. Healthy runtimes keep the same cache and policy separation, and completed cooperative timeouts do not replace them. A recovered loop cancels remaining tasks, closes its clients within a five-second cooperative deadline and closes the loop before its thread exits. New calls refuse with the existing timeout error while two broken runtimes still have live threads, and Ask releases every quota reservation for that refusal. Two permits one replacement after an isolated blockage while bounding persistent blocked verifier threads. Exited threads are removed from the count on the next runtime lookup. Python cannot force a permanently blocked thread to stop, so at most two such broken runtimes can remain until process exit. Shutdown skips their client close and does not wait on them. Existing absolute deadlines still prevent an expired preflight from sending a late completion. [Thread lifetime](https://docs.python.org/3.12/library/threading.html#thread-objects), [event loop stopping and callback scheduling](https://docs.python.org/3.12/library/asyncio-eventloop.html), [coroutine cancellation](https://docs.python.org/3.12/library/asyncio-task.html)

The price table is the captured 21 Nov catalog: GLM input/output USD 0.08/0.50, Qwen 3.8 27B USD 0.04/1.35, and the DeepSeek snapshot USD 0.30/1.20 per million tokens. All input, including cached input, is reserved and charged at the full input ceiling. The output count includes reasoning tokens. Recheck provider prices before enabling paid use. [Public catalog](https://cloud-api.near.ai/v1/models), [reasoning and billing fields](https://docs.near.ai/cloud/reasoning-models)

Requests use the named strict JSON Schema envelope in `response_format`, plus `reasoning_effort`. Support for that envelope was checked in the inference proxy source. Its enforcement was not exercised with a paid deployed completion. GLM's low and high efforts have minimal reasoning, while medium has full reasoning; the old effort labels are preserved without claiming equivalent behavior to Anthropic. [Schema forwarding and repair](https://github.com/nearai/inference-proxy/blob/main/src/response_format.rs)

`python -m gate injected-model` keeps main's Anthropic replay default. Use `python -m gate injected-model --provider anthropic` for the historical Anthropic trials and `python -m gate injected-model --provider nearai` for NEAR AI trials. Anthropic cannot record new trials on this branch. The NEAR AI trial cap remains thirty and USD 0.50, so the existing thirty recordings already prevent another trial without a separately reviewed recording destination. Old recordings carry no newly invented attestation claim.

The evaluation recorder and judge use sequential verified NEAR AI calls. The judge defaults to `deepseek-ai/DeepSeek-V4.1-Flash-1017`, independently of the recorder's GLM default. `judge --model` selects another priced NEAR AI model. Judging refuses before reading a credential or reserving spend if the judge is the answering model for any selected row. This is not self-grading. The judge has a 120-second deadline; hosted Ask keeps its ten-second deadline. These calls remain disabled unless the existing paid-use flags and policy pass, and no paid evaluation was run here.

The evaluator saves a reservation before each call and resumes by request digest. Writes are atomic, and an exclusive `.judge.lock` prevents simultaneous invocations sharing a ledger. After a crash, the owner must confirm no judge remains before removing that lock. A changed judge model changes the request digest and refuses incompatible saved results. Old Anthropic batch-state files are not converted into verified NEAR receipts. `_bound` now takes only its request and delegates to the shared reservation calculation.

## Policy and owner commands

Install the complete optional dependency tree in a dedicated Python 3.12 or later environment. `requirements-nearai.lock` pins every transitive requirement and its distribution hashes, including platform markers:

```
python -m pip install --require-hashes --only-binary=:all: -r requirements-nearai.lock
python -m pip check
```

The 75-requirement lock was generated with uv 0.11.16 from `requirements-nearai.txt` and installed successfully with hash enforcement in a fresh Python 3.12.10 environment on Windows. `pip check` reported no broken requirements. Hashes are required for every dependency, not only the two direct pins. [pip hash mode](https://pip.pypa.io/en/stable/topics/secure-installs/), [uv locking](https://docs.astral.sh/uv/pip/compile/)

To regenerate intentionally after reviewing dependency changes:

```
uv pip compile requirements-nearai.txt --generate-hashes --universal --python-version 3.12 --default-index https://pypi.org/simple --output-file requirements-nearai.lock
```

`NEARAI_ATTESTATION_POLICY_FILE` names a reviewed JSON document. Its root has exactly `version` equal to integer 1, `gateway`, and `models`. `models` maps each permitted canonical model ID to a role policy. Both gateway and model role policies have nonempty `boot_measurements` and `deployments` arrays, with at most 32 entries each.

Each boot entry has exactly `mr_td`, `rt_mr0`, `rt_mr1` and `rt_mr2`, each 96 lowercase hexadecimal characters. Each deployment entry has exactly `app_compose_sha256`, `os_image_hash` and `compose_manager_actions_sha256`. The first two are 64 lowercase hexadecimal characters. The last is either such a digest or null when no manager exists. Its digest uses UTF-8 JSON of the verified actions, sorted keys, comma and colon separators, and no ASCII escaping. Boot and deployment arrays are independent approved sets; all approved combinations are permitted. Authorize them only after release, OS, image and action review. Copying values from a provider response alone is not approval.

With a reviewed policy set, this free command generates a new nonce and prints a verified result or a closed failure:

```
python -m gate.nearai --model deepseek-ai/DeepSeek-V4.1-Flash-1017
```

No key is needed for this public attestation command. Paid use additionally needs `NEARAI_API_KEY_FILE`, naming the owner's external NEAR AI Cloud API key file, and `SIYAB_NEARAI_LIVE=1`. Hosted Ask also requires `SIYAB_ASK_PROVIDER=nearai`, its existing `SIYAB_ASK=1` and authenticated workspace configuration. Evaluation still requires its existing `--yes` and `--max-usd`; recording trials still requires `--record N`. These opt-ins do not bypass measurement or receipt verification. Do not switch hosted Ask while shared-signer ambiguity or any candidate's required verification remains unresolved.

No production policy, API credential, TEE instance or verdict key was provisioned. The owner must review and enroll measurements and obtain a fleet whose every candidate verifies with an unambiguous retained signer match, then separately authorize any paid acceptance run or deployment. The five candidate failures in the original report failed local verification with dcap-qvl 0.7.1 because of the replay chain mismatch; they are not an outstanding demonstrated provider defect.

## Switch-over checklist

Keep Anthropic as the hosted default until all of these checks pass:

1. Review the production measurement policy independently and verify every candidate in each selected fleet, including GPU and manager evidence. Require unambiguous retained signer matches. The recorded shared signer lists still fail this gate.
2. Test the hashed lock in a fresh Python 3.12 or later environment on the Linux production host. Run the following from the reviewed repository checkout, using a new external environment path. Record the interpreter, platform, installation output and `pip check` result. The successful Windows installation does not prove Linux wheel availability.

```
python3.12 -m venv "$HOME/.venvs/siyab-nearai-lock-check"
"$HOME/.venvs/siyab-nearai-lock-check/bin/python" -m pip install --require-hashes --only-binary=:all: -r requirements-nearai.lock
"$HOME/.venvs/siyab-nearai-lock-check/bin/python" -m pip check
"$HOME/.venvs/siyab-nearai-lock-check/bin/python" -m unittest tests.test_nearai -v
```

3. Measure cold attestation on that host against Ask's ten-second model deadline. With the reviewed `NEARAI_ATTESTATION_POLICY_FILE` set externally, run the following in a fresh process for each attempt and each selected model. This sends only public attestation and collateral reads and no completion or credential. Record the elapsed time and verification outcome, including failures. A fast rejection does not demonstrate a usable cold path. Repeat from cold SDK state rather than using its 60-second attestation cache. Leave room within ten seconds for the completion and receipt verification; the outer timeout plus five seconds is a caller backstop, not a larger Ask model budget.

```
"$HOME/.venvs/siyab-nearai-lock-check/bin/python" - <<'PY'
import json
import secrets
import time
from gate.nearai import Client, DEFAULT_MODEL
from intel.ask import MODEL_DEADLINE_S

started = time.perf_counter()
try:
    print(json.dumps(Client().attest(secrets.token_hex(32), DEFAULT_MODEL, timeout=MODEL_DEADLINE_S)))
finally:
    print(json.dumps({"cold_attestation_seconds": time.perf_counter() - started,
                      "ask_deadline_seconds": MODEL_DEADLINE_S}))
PY
```

4. Verify the cached-answer behavior before rollout: existing valid NEAR AI answers can still be served before live preflight. An unavailable credential, disabled live flag or changed policy does not retroactively reattest or remove them. A newly generated answer must still pass the full verification path.
5. Only after these gates, obtain separate approval for paid acceptance calls and deployment. Set the external credential and policy paths and the explicit live flags described above. No Linux production installation or cold production latency measurement was performed in this lane; neither a reviewed production policy nor an accepted complete fleet was supplied, and host changes are outside this local fix round.

## Independent CPU quote cross-check

`scripts/nearai_crosscheck.py` rechecks the exact 14 CPU quote byte strings used by the original `quote-results.json`, entirely offline. It uses retained collateral and the original verification time, 1795274641, equal to 21 Nov 2026 at 15:24:01 UTC. `fixtures/nearai/crosscheck-results.json` records every quote digest, input file digest, original outcome, corrected dcap-qvl outcome and Intel outcome. The original recordings and original results are unchanged.

The independent verifier is Intel DCAP QVL release 1.27.1, package `libsgx-dcap-quote-verify` version `1.27.101.1-noble1`. It runs `tdx_qv_verify_quote` as a local software verifier with the Intel production root, without an SGX QvE receipt. Intel's API result, quote result and collateral expiration status are all zero for every original quote. The binary and packages are pinned by hash in `crosscheck-verifier.json`. Intel's repository key was fetched over official HTTPS; `gpgv` accepted its signed InRelease, whose Packages digest matched, and the downloaded package digests matched Packages. This trust starts at that official HTTPS key retrieval. [Intel release](https://github.com/intel/confidential-computing.tee.dcap/releases/tag/DCAP_1.27.1), [verification API](https://github.com/intel/confidential-computing.tee.dcap/blob/DCAP_1.27.1/QuoteVerification/dcap_quoteverify/inc/sgx_dcap_quoteverify.h), [Intel verifier implementation](https://github.com/intel/confidential-computing.tee.dcap/blob/DCAP_1.27.1/ae/QvE/qve/qve.cpp)

| Capture | Candidate | CPU quote | Original dcap 0.7.1 | Corrected dcap 0.7.1 | Intel QVL |
| --- | --- | --- | --- | --- | --- |
| DeepSeek | none | gateway | UpToDate | UpToDate | accepted |
| DeepSeek | 0 | model | UpToDate | UpToDate | accepted |
| DeepSeek | 0 | manager | UpToDate | UpToDate | accepted |
| DeepSeek | 1 | model | qe_report signature error | UpToDate | accepted |
| DeepSeek | 1 | manager | qe_report signature error | UpToDate | accepted |
| GLM | none | gateway | UpToDate | UpToDate | accepted |
| GLM | 0 | model | qe_report signature error | UpToDate | accepted |
| GLM | 0 | manager | qe_report signature error | UpToDate | accepted |
| GLM | 1 | model | qe_report signature error | UpToDate | accepted |
| GLM | 1 | manager | qe_report signature error | UpToDate | accepted |
| GLM | 2 | model | qe_report signature error | UpToDate | accepted |
| GLM | 2 | manager | qe_report signature error | UpToDate | accepted |
| GLM | 3 | model | qe_report signature error | UpToDate | accepted |
| GLM | 3 | manager | qe_report signature error | UpToDate | accepted |

Every corrected dcap result has no advisories. The five failing model candidates also had five failing manager quotes. For all ten failing rows, the retained collateral's leaf certificate differs from the quote's own leaf certificate; the other four match. dcap-qvl 0.7.1 uses `collateral.pck_certificate_chain` when supplied. The original local replay indexed collateral only by FMSPC and carried that machine-specific override across machines. The correction removes only that optional field. It changes no quote bytes, shared TCB collateral, verification time, root or verifier version. The verifier then validates the quote's embedded PCK chain against the same Intel root. [Chain selection](https://github.com/Phala-Network/dcap-qvl/blob/v0.7.1/src/verify.rs), [quote-specific collateral fetch](https://github.com/Phala-Network/dcap-qvl/blob/v0.7.1/src/collateral.rs), [Python binding](https://github.com/Phala-Network/dcap-qvl/blob/v0.7.1/src/python.rs)

The installed SDK fetches collateral with the complete quote bytes, and dcap-qvl's fetcher attaches the chain extracted from that quote. No live SDK chain-reuse defect was established. The test replay helper now preserves the quote-specific chain behavior. A regression first reproduces the original error with the wrong saved chain, then verifies the identical quote using its embedded chain. [SDK collateral adapter](https://github.com/nearai/inference-sdk/blob/main/py/src/nearai_inference_sdk/utils/intel.py)

Both verifiers reject all three negative controls. Flipping a TD report bit or a QE report signature bit produces cryptographic rejection. At Unix time 2145916800, dcap reports expired CRL collateral; Intel returns quote result zero but collateral expiration status one, which the script correctly rejects. A successful API call alone never passes the check. These 14 comparisons and three negative controls passed again from the saved artifacts during this fix round.

The command from the repository root on a Linux x86_64 host is:

```
python scripts/nearai_crosscheck.py --intel-qvl "$SIYAB_INTEL_QVL_LIBRARY"
```

Install the locked dcap dependency and extract the pinned Intel package. Set the shell variable `SIYAB_INTEL_QVL_LIBRARY` to that extracted library's path. The script checks the exact library hash before loading it. The package provenance URLs are [runtime package](https://download.01.org/intel-sgx/sgx_repo/ubuntu/pool/main/libs/libsgx-dcap-quote-verify/libsgx-dcap-quote-verify_1.27.101.1-noble1_amd64.deb), [development package](https://download.01.org/intel-sgx/sgx_repo/ubuntu/pool/main/devel/libsgx-dcap-quote-verify-dev/libsgx-dcap-quote-verify-dev_1.27.101.1-noble1_amd64.deb), [SGX headers](https://download.01.org/intel-sgx/sgx_repo/ubuntu/pool/main/devel/libsgx-headers/libsgx-headers_2.30.101.1-noble1_amd64.deb), [Packages](https://download.01.org/intel-sgx/sgx_repo/ubuntu/dists/noble/main/binary-amd64/Packages), [signed InRelease](https://download.01.org/intel-sgx/sgx_repo/ubuntu/dists/noble/InRelease), and [repository key](https://download.01.org/intel-sgx/sgx_repo/ubuntu/intel-sgx-deb.key). The exact downloaded digests and SDK wheel metadata are in `crosscheck-verifier.json`.

This section establishes CPU quote validity at the recorded time. It does not establish current freshness, a passing full fleet, GPU validity for unrecorded candidates or policy enrollment.

## TEE verdict-service design

This is the recommended next deployment design, not an implemented service endpoint. Keep the deterministic `gate.verdict.request` checks and the canonical `GATEv1` transfer signature. Keep model explanations outside signing authority. The existing `gate.http` endpoint remains unsigned.

Build a reviewed, immutable CPU TDX CVM image containing the verdict service, rule and label revisions, provider policy and an in-memory Ed25519 signer. Generate a new verdict key inside that CVM from its OS entropy. Do not import the existing host key. Never expose a general signing endpoint or mount the guest-agent socket into unrelated containers. A fresh per-deployment key avoids assuming a persistent KMS recovery policy that has not been reviewed. Restart and release procedures must enroll a new key before signing resumes.

Expose a challenge-based service attestation route. A proposed versioned report-data encoding uses SHA-256 of a canonical binding document in its first 32 bytes and the client's nonce in its last 32 bytes. The document binds the verdict public key, TLS SPKI, Gate account, network, service release, policy digest and key epoch. Verify the quote, boot and runtime measurements, full document digest, nonce and live TLS identity before accepting the public key. The audited measured service must attest only its internally generated key and loaded policy. The same machine's ability to obtain a quote alone does not prove private-key custody.

dstack's current guest API supports `Attest` over caller-supplied report data and explicit algorithm-separated key derivation through `GetKey`, inside the guest Unix socket. That supports a future persistent-key alternative, but it requires reviewing the actual deployed guest version, KMS root and release policy. This lane neither uses that API nor assumes the NEAR AI fleet exposes it to Siyab. [Guest API specification](https://github.com/Dstack-TEE/dstack/blob/next/docs/guest-api-v1.md)

For enrollment, the owner verifies the new CVM and key binding off chain, then uses the existing owner-only `set_verifier_keys(current, previous, previous_valid_until_ms)` after separate transaction approval. A client also fetches fresh service evidence before trusting a verdict and checks that the public key agrees with its approved enrollment. The existing Gate checks the enrolled Ed25519 key, exact transfer, expiry and replay protection. It does not parse TDX or GPU evidence and has no current-key attestation lease. Automatic on-chain attestation expiry would need a separate contract change. Its existing signature alone must not be described as a fresh TEE check.

Rotate on release changes and suspected compromise. Verify and enroll the new key first; permit an old key only for an explicitly bounded overlap no longer than outstanding verdict lifetimes. An old signer must emit `key_id=prev` during that overlap, since old `cur` signatures do not match the new current key. On compromise, pause and remove the old key without an overlap. Revoke old deployments as well as retiring their keys. No rotation transaction was executed here. Repository basis: `gate/signer.py`, `gate/canon.py`, `gate/verdict.py`, `contracts/gate/src/verdict.rs` and `contracts/gate/src/policy.rs`.

## Recordings and validation

`fixtures/nearai/` contains separate gateway and model reports for DeepSeek and GLM, their fresh challenges and observed TLS peers, Intel collateral, one NVIDIA signed verdict and NVIDIA JWKS, the three-model catalog snapshot and quote outcomes. The manifest lists the sources and content digests. JSON Unicode escaping preserves every decoded configuration and evidence string. The attestation report HTTP envelope is not a completion receipt. Tests use the capture time for collateral and JWT validity, never the current clock to make old evidence appear fresh.

Review round 1 focused runs, retained as historical validation:

| Modules | Run | Passed | Skipped |
| --- | --- | --- | --- |
| `test_nearai`, `test_ask`, `test_ask_ledger`, `test_ask_validation`, `test_ask_eval`, `test_ask_api`, `test_ask_panel`, `test_injected_model`, with the hash-locked SDK dependencies | 266 | 265 | 1 |
| `test_agent`, `test_api_doc`, `test_demo`, `test_http`, `test_incidents`, `test_incident_replay`, `test_intents`, `test_registry_args`, `test_res`, `test_scan`, `test_verdict`, `test_verify`, `test_verify_receipt`, `test_wasm` | 473 | 468 | 5 |
| Total | 739 | 733 | 6 |

`python scripts/check_hygiene.py --all` and `git diff --check` passed. The CPU cross-check script also passed Ruff and an independent replay of all 14 quotes and three negative controls.

The first group used `python -m unittest` with its listed `tests.<module>` arguments and `-v`, in the fresh environment installed from `requirements-nearai.lock` with `--require-hashes --only-binary=:all:`. Within that group, NEAR AI ran 27, Ask 106, ledger 9, validation 15, evaluation 50 with one skip, API 31, panel 1 and injected model 27. The second group used the base interpreter and `-q`. An AST import inventory confirmed that these groups cover every test module directly importing the changed Python modules. Python 3.11 grammar parsing passed for all five application modules; execution under Python 3.11 was not performed. Independent read-only review found no blocking defect in the provider, quota, runtime and judge changes.

The SDK-enabled group skips only `test_ask_eval.AskEvalTests.test_launch_records_pass_replay`, because launch records have not been selected. The second group has five optional-dependency skips. Without Python 3.12 and the exact SDK and DCAP pins, all 17 methods in `test_nearai.SDKRecordingTests` are skipped at `setUpClass`: complete CPU and GPU replay, caller nonce through `attest`, shared signers through `attest`, cached and concurrent chats, changed policy, TCB and configuration rejection, wrong PCK chain reproduction, changed nonce, TLS identity, missing GPU evidence, changed signer and deployment, corrupt NVIDIA JWT, response receipt gating, missing or gateway receipts, equivalent shared signers, preflight timeout and the bounded keyless fetch. The base environment run of `python -m unittest tests.test_nearai -v` executed ten tests successfully and reported that one class skip. The Ask and committed injected-model replay tests do not require the optional SDK.

Review round 2 module results, with the orchestrator's later citation correction included:

| Module | Run | Passed | Skipped | Failed |
| --- | --- | --- | --- | --- |
| `test_nearai` | 36 | 36 | 0 | 0 |
| `test_ask` | 108 | 108 | 0 | 0 |
| `test_ask_eval` | 50 | 49 | 1 | 0 |
| `test_ask_ledger` | 9 | 9 | 0 | 0 |
| `test_injected_model` | 27 | 27 | 0 | 0 |
| `test_citations` | 11 | 11 | 0 | 0 |
| Requested modules | 241 | 240 | 1 | 0 |
| `test_api_doc`, `test_ask_api`, `test_ask_validation` | 73 | 73 | 0 | 0 |
| All selected modules | 314 | 313 | 1 | 0 |

The NEAR AI, Ask, evaluator, ledger and injected-model modules used the hash-locked Windows Python 3.12.10 environment. The three additional modules directly import the changed code and used the base Python 3.12.10 interpreter. The recorded cross-check regression replays all 14 quotes and three negative controls with dcap-qvl, compares both original and embedded-chain outcomes to `crosscheck-results.json`, and checks every source and quote digest. Both cross-check artifacts are now listed in the fixture manifest. Direct tests of both recorded signer lists pin the shared-signer rejection. The complete original DeepSeek verifier test now requires the exact `attestation_failed` reason; its other candidate lacks the separately recorded signed NVIDIA verdict.

The round 2 final NEAR AI run executed 36 tests in 20.004 seconds with no skips or failures. The base interpreter separately passed all 13 standard-library NEAR AI tests, with the optional SDK class skipped at setup. Independent review passed the two cross-check and cache-pin tests and both timeout regressions. These repeated checks are not added to the module totals above.

Round 2 `python scripts/check_hygiene.py --all` exited zero, and `git diff --check` passed. Its original citation scan failed only on the obsolete README line 84 range. The orchestrator corrected that range and JUDGES line 93 to `gate/injected_model.py:25-32,215-217,325-388`; all 11 citation tests then passed. The former citation failure is resolved.

The Anthropic cache-key regression pins a literal computed by the original cache function from both `0e113268c` and the current local `origin/main`, `fe6948df8`; both give the same value. The new failure tests first reproduced the raw preflight, setup, verifier and malformed-signer errors and the original false completion-state default, then passed after the fix. A blocked-loop regression exposed a second race in queued work; the deadline is now captured before queueing and checked at entry and before a completion. Both timeout regressions passed independent review. The real SDK cache test used a one-second allowance that failed even before the fix; measured client construction alone took 1.521 seconds on this machine. That test now uses ten seconds and still proves one attestation across sequential and concurrent calls. It is not a latency acceptance test.

The full suite remains the orchestrator's responsibility. In round 2, worktree environment removal was attempted only after verifying its resolved path stays inside this worktree and is not a linked directory. Automatic approval review rejected the removal with `blocked by policy`; `.venv/nearai-lock` remains in place. No alternative deletion method was attempted.

Round 2 changed `gate/nearai.py`, `intel/ask.py`, `tests/test_nearai.py`, `tests/test_ask.py`, `fixtures/nearai/manifest.json`, `README.md`, `JUDGES.md`, `AGENTS.md` and this lane report. Other working-tree changes belong to the previous round and were preserved.

`RecordedTrialsTest.test_committed_nearai_trials_and_replay_are_pinned_without_live_dependencies` uses the committed `fixtures/demo/model-runs-nearai` directly. It pins all 30 file bytes and the replay output, checks request digests and the recorded spend of 30981 micro-dollars, and forbids a key read or client creation. It does not patch `NEARAI_RUNS_DIR`. The existing Anthropic replay output remains unchanged.

The orchestrator must run the full suite. Round 1 did not edit README, JUDGES or AGENTS. Round 2 applies the permitted documentation replacements described below. LOG, API, threat model, RPC, rules, cluster and seed files remain untouched. No commit, push, deployment, remote change, transaction, credential read or paid completion was performed. The submission tag was not touched.

## Files in the lane

Application and scripts: `gate/nearai.py`, `gate/injected_model.py`, `gate/__main__.py`, `intel/ask.py`, `scripts/ask_eval.py` and `scripts/nearai_crosscheck.py`.

Configuration and documentation: `env.example`, `requirements-nearai.txt`, `requirements-nearai.lock`, `docs/lanes/nearai.md` and the permitted README, JUDGES and AGENTS lines.

Tests: `tests/test_nearai.py`, `tests/test_ask.py`, `tests/test_ask_ledger.py`, `tests/test_ask_eval.py` and `tests/test_injected_model.py`.

New evidence from this fix round: `fixtures/nearai/crosscheck-results.json` and `fixtures/nearai/crosscheck-verifier.json`. The original lane's unchanged recordings remain new files for the orchestrator to include: `attestation-deepseek-gateway.capture.json`, `attestation-deepseek-gateway.json`, `attestation-deepseek.capture.json`, `attestation-deepseek.json`, `attestation-glm-gateway.capture.json`, `attestation-glm-gateway.json`, `attestation-glm.capture.json`, `attestation-glm.json`, `intel-collateral-70a06d070000.json`, `intel-collateral-90c06f000000.json`, `manifest.json`, `models.json`, `nras-deepseek-0.json`, `nvidia-jwks.json` and `quote-results.json`, all under `fixtures/nearai/`.

## Round 2 documentation and owner actions

The Anthropic trial paragraph at `README.md:84` uses `python -m gate injected-model --provider anthropic` and cites `gate/injected_model.py:19-24,215-217,325-388`. Round 4 also uses that explicit provider command at `JUDGES.md:91`, correct on main and after merge. `README.md:86` already uses the explicit `--provider nearai` command and remains unchanged. The round 2 replacement at `AGENTS.md:88` documents provider selection and the external Anthropic credential, NEAR AI credential, live flag and reviewed policy. Other AGENTS installation and model lines are outside the permitted replacement and remain untouched.

The orchestrator has corrected the NEAR AI citations at `README.md:86` and `JUDGES.md:93` to `gate/injected_model.py:25-32,215-217,325-388`. No citation correction remains pending. Round 3 leaves README and JUDGES line 93 untouched. JUDGES is outside the scanner's default root-document inventory.

The owner does not need to switch providers now. After independent review, enrollment, complete-fleet verification and the switch-over checks above, the owner can set the external policy and API key file paths, `SIYAB_ASK_PROVIDER=nearai`, `SIYAB_NEARAI_LIVE=1`, `SIYAB_ASK=1`, and a matching `SIYAB_ASK_MODEL` on the existing hosted service. A separately authorized paid evaluation can then use:

```
python scripts/ask_eval.py record --model z-ai/glm-5.3-flash --runs 1 --max-usd 0.50 --yes
python scripts/ask_eval.py judge <recorded-jsonl-path> --model deepseek-ai/DeepSeek-V4.1-Flash-1017 --max-usd 0.50 --yes
```

Both selected fleets must pass verification first. The recorder processes the corpus until its cap or completion, not a single prompt. No paid execution, production measurement approval, deployment or TEE signer provisioning was attempted here because this lane forbids those actions.

## Round 3 runtime recovery and validation

An unfinished future at the outer deadline now marks its runtime broken. The next call replaces its event loop and SDK clients without waiting for the stuck verifier. Healthy clients retain their 60-second attestation cache, and ordinary cooperative timeouts preserve the runtime. The stuck daemon thread and its resources may leak until process exit. The existing absolute deadlines still prevent an expired verifier from starting a late completion.

`_close_runtime` clears its global reference in a `finally` block and propagates close errors. A response whose message is null now raises the existing `ProviderBadRequest`, which Ask maps to `provider_error`. The three new regressions first reproduced all three defects: three run, one failure and two errors. They pass after the fixes. The recovery test completes two new chats while the first verifier remains blocked, then releases it for cleanup. The existing real SDK cache and queued-deadline tests also pass.

Final round 3 runs used the hash-locked Windows Python 3.12.10 environment:

| Module | Run | Passed | Skipped | Failed |
| --- | --- | --- | --- | --- |
| `tests.test_nearai` | 39 | 39 | 0 | 0 |
| `tests.test_ask` | 108 | 108 | 0 | 0 |
| `tests.test_injected_model` | 27 | 27 | 0 | 0 |
| `tests.test_citations` | 11 | 11 | 0 | 0 |
| Total | 185 | 185 | 0 | 0 |

Exact validation commands from the repository root:

```
.\.venv\nearai-lock\Scripts\python.exe -m unittest tests.test_nearai -v
.\.venv\nearai-lock\Scripts\python.exe -m unittest tests.test_ask -v
.\.venv\nearai-lock\Scripts\python.exe -m unittest tests.test_injected_model -v
.\.venv\nearai-lock\Scripts\python.exe -m unittest tests.test_citations -v
python scripts/check_hygiene.py --all
git diff --check
```

The final full-tree hygiene check and `git diff --check` both exited zero.

The primary sources checked for this runtime change establish that `Future.result(timeout)` bounds the caller's wait and also propagates a completed task's own exception, `done()` distinguishes an unfinished future, cross-thread coroutine submission returns that future, and Python cannot forcibly stop a thread. Installed SDK 0.3.0 source and the cache regression confirm session retention within an SDK client. [Future wait and state](https://docs.python.org/3.12/library/concurrent.futures.html#concurrent.futures.Future.result), [coroutine submission](https://docs.python.org/3.12/library/asyncio-task.html), [thread lifetime](https://docs.python.org/3.12/library/threading.html#thread-objects), [SDK session cache](https://github.com/nearai/inference-sdk/blob/main/py/src/nearai_inference_sdk/core/inference_client.py)

This round changes only `gate/nearai.py`, `tests/test_nearai.py`, `JUDGES.md` line 91 and this report. The orchestrator's README line 84 and JUDGES line 93 citation fixes remain intact. The owner must independently review the changes and run the full suite before committing. No extra live action is needed for these fixes. Production installation, enrollment, cold latency acceptance, paid calls and deployment remain subject to the earlier owner gates and were not performed. No commit, push, remote change, transaction, private key access or submission tag mutation was performed.

## Round 4 bounded recovery and validation

The CLI defaults to Anthropic again, matching the inspected `origin/main` command. Both documented provider commands are explicit. `JUDGES.md:91` now uses `--provider anthropic`; `README.md:84-86` already used the correct explicit providers and was preserved. A new regression pins the bare CLI's actual replay output bytes, in addition to checking dispatch for the default and both explicit providers. The two selected CLI regressions failed before the fix.

A broken runtime now receives one cross-thread stop request. Its thread owns final cleanup and loop closure if the verifier later unblocks. Cancellation and client close share a five-second cooperative deadline. The caller does not wait for that cleanup. A reentrant runtime lock serializes submission, broken marking and the list of broken runtimes, including shutdown's nested submission. Further submissions to an already broken runtime close the unused coroutine and raise the existing timeout error. Healthy shutdown still propagates close failures; already broken shutdown skips client close entirely.

The circuit breaker rejects new chat calls while two broken runtime threads remain alive, using `ProviderTimeout` with `completion_started=False`. Two allows recovery after one stalled verifier without permitting an unbounded sequence of leaked loops. Dead threads leave the list at the next lookup, so recovery resumes after a broken runtime exits. Ask's existing failure path releases spend, public spend, user tokens, team tokens and call reservations. The new Ask regression exercises the real adapter and ledger and verifies zero in all five counters, no replacement SDK client and no cached answer.

The three runtime regressions first failed before the fix, with one additional cleanup error on the old shutdown path. They now prove eventual thread exit, client and loop closure, refusal at two live broken threads, recovery and count reduction after one exits, and shutdown without queueing client close on a broken loop. Existing blocked and queued deadline tests now observe the automatic thread exit. The two tests that need to enter a blocking verifier use a one-second inner budget; a one-millisecond budget could expire before dispatch under load. The queued-deadline test keeps its original one-millisecond budget. The real SDK cache and cooperative-timeout preservation checks still pass.

Final requested module results in `.venv/nearai-lock`, Python 3.12.10, SDK 0.3.0 and dcap-qvl 0.7.1:

| Module | Run | Passed | Skipped | Failed |
| --- | --- | --- | --- | --- |
| `tests.test_nearai` | 42 | 42 | 0 | 0 |
| `tests.test_ask` | 109 | 109 | 0 | 0 |
| `tests.test_injected_model` | 28 | 28 | 0 | 0 |
| `tests.test_citations` | 11 | 11 | 0 | 0 |
| `tests.test_ask_eval` | 50 | 49 | 1 | 0 |
| `tests.test_ask_ledger` | 9 | 9 | 0 | 0 |
| Requested total | 249 | 248 | 1 | 0 |

Commands from the repository root:

```
.\.venv\nearai-lock\Scripts\python.exe -m unittest tests.test_nearai -q
.\.venv\nearai-lock\Scripts\python.exe -m unittest tests.test_ask -v
.\.venv\nearai-lock\Scripts\python.exe -m unittest tests.test_injected_model tests.test_citations -v
.\.venv\nearai-lock\Scripts\python.exe -m unittest tests.test_ask_eval tests.test_ask_ledger -q
```

The evaluator skip remains the existing unselected launch-record replay. Independent read-only review passed nine selected lifecycle, deadline, cache and quota tests, plus an eight-caller probe showing a single broken entry, a single client close and count reduction after thread exit. Repeated runs are not added to the totals.

The fourteen direct CLI importer modules also ran: 473 tests, 468 passed, four skipped and one failed. `tests.test_incident_replay.ReplayCase.test_partial_refresh_failure_preserves_every_published_byte` failed its shared system temp directory assertion at `tests/test_incident_replay.py:318`. That assertion scans the parent of its own temporary directory for every incident staging directory. Read-only inspection found an older staging directory created before this run; a diagnostic rerun reproduced the failure without changing any code. The entire 59-test incident-replay module then passed with its own temporary root. The unrelated directory was left untouched. The final scoped results therefore establish 717 distinct passing tests and five skips across 722 selected tests, with the incident-replay module requiring that temp isolation. No full suite was run.

The importer command and isolated rerun were:

```
.\.venv\nearai-lock\Scripts\python.exe -m unittest tests.test_agent tests.test_api_doc tests.test_demo tests.test_http tests.test_incidents tests.test_incident_replay tests.test_intents tests.test_registry_args tests.test_res tests.test_scan tests.test_verdict tests.test_verify tests.test_wasm tests.test_verify_receipt -q
```

```
import tempfile
import unittest
from unittest.mock import patch

with tempfile.TemporaryDirectory(prefix="siyab-nearai-round4-") as directory:
    with patch.object(tempfile, "tempdir", directory):
        suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_incident_replay")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(not result.wasSuccessful())
```

The Python snippet was piped into `.venv/nearai-lock/Scripts/python.exe -`. The final citation-only rerun passed all eleven tests after the report edits. `git diff --check` passed.

The official Python sources checked this round establish that cross-thread coroutine submission returns a future, cross-thread loop callbacks must use the corresponding scheduling method, stopping a running loop finishes its current callback batch, and thread liveness lasts until its target returns. The installed Python 3.12.10 implementations were also inspected. These support the cleanup ordering and live-thread count; the two-runtime limit is the local policy choice justified above. [Coroutine submission and cancellation](https://docs.python.org/3.12/library/asyncio-task.html), [loop stopping, scheduling and closure](https://docs.python.org/3.12/library/asyncio-eventloop.html), [thread liveness and joins](https://docs.python.org/3.12/library/threading.html#thread-objects)

### Hygiene exception

The plain stdlib identifiers `run_coroutine_threadsafe` and `call_soon_threadsafe` contain the word the authored-word check rejects. They appear plainly in code, not built from fragments. `scripts/check_hygiene.py` now removes exactly those two whole identifiers (`STDLIB_NAMES`, case sensitive, word bounded) before the word check, and `tests/test_hygiene.py` pins that the word is still rejected on its own, in any other identifier and elsewhere on the same line. `python scripts/check_hygiene.py --all` exits zero.

Recording: the Anthropic run already holds MAX_TRIALS (30) trials, so `--record` with the default provider had nothing left to record on main; this branch makes it say so and exit 1, and `--provider nearai` records NEAR AI trials under their own cap.


Round 4 changes only `gate/__main__.py`, `gate/nearai.py`, `tests/test_nearai.py`, `tests/test_ask.py`, `tests/test_injected_model.py`, `JUDGES.md` line 91 and this report. Prior lane changes remain uncommitted. The prohibited files and submission tag remain untouched.
