"""NEAR AI Cloud calls released only after local evidence and receipt verification."""

import argparse
import atexit
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import re
import secrets
import sys
import threading
from concurrent.futures import Future
from urllib.parse import urlencode

API_BASE_URL = "https://cloud-api.near.ai/v1"
DEFAULT_MODEL = "z-ai/glm-5.3-flash"
PRICES = {DEFAULT_MODEL: (80_000, 500_000),
          "Qwen/Qwen3.8-27B": (40_000, 1_350_000),
          "deepseek-ai/DeepSeek-V4.1-Flash-1017": (300_000, 1_200_000)}
SDK_VERSION = "0.3.0"
DCAP_VERSION = "0.7.1"
MAX_REPORT_BYTES = 2_097_152
MAX_POLICY_BYTES = 262_144
MAX_BROKEN_RUNTIMES = 2
BOOT_FIELDS = ("mr_td", "rt_mr0", "rt_mr1", "rt_mr2")
DEPLOYMENT_FIELDS = ("app_compose_sha256", "os_image_hash", "compose_manager_actions_sha256")


class AttestationError(Exception):
    completion_started = True


class SDKUnavailable(Exception):
    pass


class ProviderRateLimited(Exception):
    pass


class ProviderStatus(Exception):
    def __init__(self, status):
        super().__init__()
        self.status = status


class ProviderTimeout(Exception):
    pass


class ProviderConnection(Exception):
    pass


class ProviderBadRequest(Exception):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("invalid_number")


def decode_report(body):
    try:
        if len(body) > MAX_REPORT_BYTES:
            raise ValueError()
        result = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise AttestationError("report_invalid") from None


def _nonce(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AttestationError("nonce_invalid")


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 120:
        raise AttestationError("timeout_invalid")


def _sdk():
    try:
        if (sys.version_info < (3, 12)
                or importlib.metadata.version("nearai-inference-sdk") != SDK_VERSION
                or importlib.metadata.version("dcap-qvl") != DCAP_VERSION):
            raise SDKUnavailable()
        import nearai_inference_sdk
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise SDKUnavailable() from None
    return nearai_inference_sdk


def load_policy(model):
    name = os.environ.get("NEARAI_ATTESTATION_POLICY_FILE")
    if not name:
        raise AttestationError("policy_unavailable")
    try:
        with open(name, "rb") as handle:
            body = handle.read(MAX_POLICY_BYTES + 1)
        if len(body) > MAX_POLICY_BYTES:
            raise ValueError()
        policy = decode_report(body)
        if (set(policy) != {"version", "gateway", "models"} or type(policy["version"]) is not int
                or policy["version"] != 1 or not isinstance(policy["models"], dict)):
            raise ValueError()
        selected = (policy["gateway"], policy["models"][model])
        for role in selected:
            if not isinstance(role, dict) or set(role) != {"boot_measurements", "deployments"}:
                raise ValueError()
            for field, fields, size in (("boot_measurements", BOOT_FIELDS, 96),
                                        ("deployments", DEPLOYMENT_FIELDS, 64)):
                rows = role[field]
                if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
                    raise ValueError()
                for row in rows:
                    if not isinstance(row, dict) or set(row) != set(fields):
                        raise ValueError()
                    for key, value in row.items():
                        if key == "compose_manager_actions_sha256" and value is None:
                            continue
                        if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{size}}}", value) is None:
                            raise ValueError()
        return selected
    except (OSError, KeyError, TypeError, ValueError, AttestationError):
        raise AttestationError("policy_invalid") from None


def _options(sdk, role, model=False):
    default_quote_verifier = sdk.create_tdx_quote_verifier()

    async def verify_quote(quote):
        result = await default_quote_verifier(quote)
        if result.tcb_status != "UpToDate" or result.advisory_ids or result.debug_enabled:
            raise AttestationError("tcb_rejected")
        if len(result.mr_config_id) != 48 or result.mr_config_id[0] != 1 or any(result.mr_config_id[33:]):
            raise AttestationError("configuration_invalid")
        from dcap_qvl import Quote
        report = Quote.parse(bytes.fromhex(quote.removeprefix("0x"))).report
        boot = {name: getattr(report, name).hex() for name in BOOT_FIELDS}
        if boot not in role["boot_measurements"]:
            raise AttestationError("boot_measurement_rejected")
        return result

    def verify_deployment(deployment):
        manager = deployment.compose_manager
        actions_hash = None
        if manager is not None:
            actions_hash = hashlib.sha256(json.dumps(manager.actions, sort_keys=True, separators=(",", ":"),
                                                     ensure_ascii=False).encode("utf-8")).hexdigest()
        measured = {"app_compose_sha256": hashlib.sha256(deployment.app_compose.encode("utf-8")).hexdigest(),
                    "os_image_hash": deployment.runtime_measurements.os_image_hash,
                    "compose_manager_actions_sha256": actions_hash}
        if measured not in role["deployments"]:
            raise AttestationError("deployment_rejected")

    if model:
        return sdk.ModelVerificationOptions(
            policy=sdk.ModelAttestationPolicy(accepted_tcb_statuses=("UpToDate",), gpu_evidence="required"),
            verifiers=sdk.ModelAttestationVerifiers(tdx_quote=verify_quote, deployment=verify_deployment))
    return sdk.GatewayVerificationOptions(
        include_spki_fingerprint=True,
        policy=sdk.AttestationPolicy(accepted_tcb_statuses=("UpToDate",)),
        verifiers=sdk.AttestationVerifiers(tdx_quote=verify_quote, deployment=verify_deployment))


async def _fetch_report(nonce, model):
    import aiohttp
    from nearai_inference_sdk.utils.fetch import ResponseBodyTooLarge, fetch

    try:
        responses = []
        for query in ({"nonce": nonce, "signing_algo": "ed25519", "include_tls_fingerprint": "true"},
                      {"model": model, "provider": "near", "nonce": nonce, "signing_algo": "ed25519",
                       "include_tls_fingerprint": "false"}):
            response = await fetch(API_BASE_URL + "/attestation/report?" + urlencode(query),
                headers={"Accept": "application/json", "Accept-Encoding": "identity", "x-no-aliasing": "true"},
                _capture_peer_spki=True, allow_redirects=False, max_body_bytes=MAX_REPORT_BYTES)
            if response.status != 200:
                raise ProviderStatus(response.status)
            responses.append((decode_report(response.body), response.peer_spki_fingerprint))
        gateway, peer = responses[0]
        if not peer:
            raise AttestationError("tls_peer_missing")
        gateway["model_attestations"] = responses[1][0].get("model_attestations")
        return json.dumps(gateway, ensure_ascii=True).encode("utf-8"), peer
    except ResponseBodyTooLarge:
        raise AttestationError("report_invalid") from None
    except (aiohttp.ClientError, OSError):
        raise ProviderConnection() from None


def _raw_evidence(sdk, raw, model=False):
    info = raw["info"]["tcb_info"]
    if isinstance(info, str):
        info = decode_report(info)
    values = {"nonce": raw["request_nonce"],
              "signer": sdk.SigningIdentity(signing_algo=raw["signing_algo"],
                                             signing_address=raw["signing_address"]),
              "intel_quote": raw["intel_quote"], "event_log": raw["event_log"],
              "app_compose": info["app_compose"],
              "reported_quote_data": raw.get("report_data") if model else raw["report_data"]}
    if raw["signing_algo"] != "ed25519":
        raise AttestationError("algorithm_rejected")
    if not model:
        if not isinstance(raw["report_data"], str):
            raise AttestationError("report_invalid")
        return sdk.GatewayAttestation(**values, spki_fingerprint=raw.get("tls_cert_fingerprint"))
    manager = raw.get("compose_manager_attestation")
    if manager is not None:
        manager = sdk.ComposeManagerAttestation(actions=tuple(manager["actions"]),
            actions_hash=manager["actions_hash"], nonce=manager["nonce"], intel_quote=manager["quote"],
            event_log=manager["event_log"], reported_quote_data=manager.get("report_data"))
    return sdk.ModelAttestation(**values, nvidia_payload=raw.get("nvidia_payload"),
                               signing_public_key=raw.get("signing_public_key"),
                               compose_manager_attestation=manager)


def _verified_summary(value):
    return {"signing_address": value.signer.signing_address, "signing_algo": value.signer.signing_algo,
            "tcb_status": value.tcb_status, "deployment_policy": value.deployment_provenance}


async def verify_report(report, nonce, peer_spki, model, gateway_options, model_options, sdk):
    """Verify a captured report against an independently retained challenge and policy."""
    _nonce(nonce)
    try:
        gateway = await sdk.verify_gateway_attestation(_raw_evidence(sdk, report["gateway_attestation"]),
            sdk.GatewayClientBinding(nonce=nonce, spki_fingerprint=peer_spki),
            policy=gateway_options.policy, verifiers=gateway_options.verifiers)
        if (gateway.tls_binding.kind != "attested" or not peer_spki
                or gateway.tls_binding.spki_fingerprint != peer_spki):
            raise AttestationError("tls_binding_required")
        raw_models = report.get("model_attestations")
        if not isinstance(raw_models, list) or not raw_models:
            raise AttestationError("model_evidence_missing")
        models = []
        for raw in raw_models:
            if raw.get("model_name") != model:
                raise AttestationError("model_mismatch")
            verified = await sdk.verify_model_attestation(_raw_evidence(sdk, raw, model=True),
                sdk.ModelClientBinding(nonce=nonce), policy=model_options.policy, verifiers=model_options.verifiers)
            models.append(verified)
        _check_signers(models)
        return {"verified": True, "nonce": nonce, "model": model,
                "gateway": _verified_summary(gateway),
                "models": [{**_verified_summary(value), "gpu_evidence": value.gpu_evidence} for value in models]}
    except (sdk.VerificationError, sdk.ApiError, KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise AttestationError("attestation_failed") from None


def _check_signers(models):
    identities = [(item.signer.signing_algo, bytes.fromhex(item.signer.signing_address
                   .removeprefix("0x").removeprefix("0X"))) for item in models]
    if not identities or len(set(identities)) != len(identities):
        raise AttestationError("ambiguous_model_signer")


class _Runtime:
    def __init__(self):
        self.clients = {}
        self.broken = False
        ready = Future()
        self.thread = threading.Thread(target=self._run, args=(ready,), name="nearai", daemon=True)
        self.thread.start()
        ready.result()

    def _run(self, ready):
        try:
            self.loop = asyncio.new_event_loop()
        except BaseException as error:
            ready.set_exception(error)
            return
        ready.set_result(None)
        try:
            self.loop.run_forever()
        finally:
            try:
                if self.broken:
                    self.loop.run_until_complete(self._close_clients())
            finally:
                self.loop.close()

    def run(self, coroutine, timeout=None):
        with _runtime_lock:
            if self.broken:
                coroutine.close()
                raise ProviderTimeout()
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except BaseException as error:
            unfinished_timeout = isinstance(error, TimeoutError) and not future.done()
            future.cancel()
            if unfinished_timeout:
                with _runtime_lock:
                    if not self.broken:
                        self.broken = True
                        _broken_runtimes.append(self)
                        self.loop.call_soon_threadsafe(self.loop.stop)
            raise

    def client(self, sdk, api_key, roles):
        key = (api_key, json.dumps(roles, sort_keys=True, separators=(",", ":")))
        if key not in self.clients:
            self.clients[key] = sdk.InferenceClient(api_key, base_url=API_BASE_URL, e2ee=True,
                signing_algo="ed25519", headers={"x-no-aliasing": "true"},
                gateway_verification=_options(sdk, roles[0]),
                model_verification=_options(sdk, roles[1], model=True),
                attestation_cache_time_to_live_ms=60_000)
        return self.clients[key]

    async def _close_clients(self):
        async with asyncio.timeout(5):
            pending = asyncio.all_tasks() - {asyncio.current_task()}
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.gather(*(client.aclose() for client in self.clients.values()))
            self.clients.clear()

    def close(self):
        if self.broken:
            return
        try:
            self.run(self._close_clients(), timeout=6)
        finally:
            if not self.broken:
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.thread.join(timeout=1)


_runtime = None
_runtime_lock = threading.RLock()
_broken_runtimes = []


def _get_runtime():
    global _runtime
    with _runtime_lock:
        _broken_runtimes[:] = [runtime for runtime in _broken_runtimes if runtime.thread.is_alive()]
        if len(_broken_runtimes) >= MAX_BROKEN_RUNTIMES:
            raise ProviderTimeout()
        if _runtime is None or _runtime.broken:
            _runtime = _Runtime()
        return _runtime


def _close_runtime():
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            try:
                _runtime.close()
            finally:
                _runtime = None


atexit.register(_close_runtime)


class Client:
    def __init__(self, api_key=None):
        self.api_key = api_key

    def attest(self, nonce, model=DEFAULT_MODEL, timeout=30):
        _nonce(nonce)
        _timeout(timeout)
        roles = load_policy(model)
        sdk = _sdk()
        gateway, models = _options(sdk, roles[0]), _options(sdk, roles[1], model=True)

        async def verify():
            async with asyncio.timeout(timeout):
                body, fingerprint = await _fetch_report(nonce, model)
                return await verify_report(decode_report(body), nonce, fingerprint, model, gateway, models, sdk)

        try:
            return asyncio.run(verify())
        except TimeoutError:
            raise ProviderTimeout() from None

    def preflight(self, model):
        if os.environ.get("SIYAB_NEARAI_LIVE") != "1":
            raise AttestationError("live_disabled")
        if model not in PRICES or not self.api_key:
            raise ProviderBadRequest()
        roles = load_policy(model)
        return _sdk(), roles

    def chat(self, params, timeout=60):
        sdk = None
        completion_started = False

        async def complete():
            nonlocal completion_started
            async with asyncio.timeout_at(deadline):
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError()
                client = runtime.client(sdk, self.api_key, roles)
                preflight = await client.verify(params["model"])
                _check_signers(preflight.models)
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError()
                completion_started = True
                response = await client.chat.completions.create(**params)
                receipt = await client.verify_response(response.id)
                if receipt.signature_kind != "provider_tee" or len(receipt.attestations) != 1:
                    raise AttestationError("model_signature_required")
                return response.model_dump(mode="json")

        try:
            sdk, roles = self.preflight(params.get("model"))
            _timeout(timeout)
            if params.get("stream"):
                raise ProviderBadRequest()
            runtime = _get_runtime()
            deadline = runtime.loop.time() + timeout
            return runtime.run(complete(), timeout=timeout + 5)
        except SDKUnavailable:
            raise
        except Exception as error:
            if isinstance(error, (AttestationError, ProviderRateLimited, ProviderStatus, ProviderTimeout,
                                  ProviderConnection, ProviderBadRequest)):
                failure = error
            elif isinstance(error, TimeoutError):
                failure = ProviderTimeout()
            elif (sdk is None or isinstance(error, (sdk.VerificationError, sdk.ApiError))
                    or sdk.get_sdk_error(error) is not None):
                failure = AttestationError("attestation_failed")
            else:
                import openai
                if isinstance(error, openai.RateLimitError):
                    failure = ProviderRateLimited()
                elif isinstance(error, openai.APITimeoutError):
                    failure = ProviderTimeout()
                elif isinstance(error, openai.APIConnectionError):
                    failure = ProviderConnection()
                elif isinstance(error, openai.APIStatusError):
                    failure = ProviderStatus(error.status_code)
                elif isinstance(error, openai.APIError):
                    failure = ProviderBadRequest()
                else:
                    failure = AttestationError("attestation_failed")
            failure.completion_started = completion_started
            raise failure from None

    def create(self, params, timeout):
        response = self.chat(params, timeout)
        try:
            choices = response["choices"]
            if len(choices) != 1:
                raise ValueError()
            choice = choices[0]
            message = choice["message"]
            text = message.get("content")
            if message.get("tool_calls") or text is not None and not isinstance(text, str):
                text = None
            stop = ("refusal" if message.get("refusal") else
                    {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}
                    .get(choice["finish_reason"], choice["finish_reason"]))
            usage = response.get("usage") or {}
            return {"stop_reason": stop, "text": text,
                    "usage": {"input_tokens": usage.get("prompt_tokens"),
                              "output_tokens": usage.get("completion_tokens"),
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderBadRequest() from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify free NEAR AI attestation evidence; sends no prompt.")
    parser.add_argument("--model", choices=PRICES, default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    try:
        result = Client().attest(secrets.token_hex(32), args.model)
    except (AttestationError, SDKUnavailable, ProviderTimeout, ProviderConnection, ProviderStatus) as error:
        reason = str(error) if isinstance(error, AttestationError) else {
            SDKUnavailable: "sdk_unavailable", ProviderTimeout: "timeout", ProviderConnection: "connection",
            ProviderStatus: "provider_status"}[type(error)]
        print(json.dumps({"verified": False, "reason": reason}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
