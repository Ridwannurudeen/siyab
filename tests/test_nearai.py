import asyncio
import base64
import copy
import hashlib
import json
import os
import signal
import socket
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from gate import nearai


ROOT = Path(__file__).resolve().parent.parent


class NearAIClosedTests(unittest.TestCase):
    def test_close_failure_clears_the_runtime_and_propagates(self):
        runtime = SimpleNamespace(close=Mock(side_effect=RuntimeError("unit-sentinel-value")))
        with patch.object(nearai, "_runtime", runtime):
            with self.assertRaisesRegex(RuntimeError, "^unit-sentinel-value$"):
                nearai._close_runtime()
            self.assertIsNone(nearai._runtime)
        runtime.close.assert_called_once_with()

    def test_attestation_errors_assume_a_completion_may_have_started(self):
        self.assertTrue(nearai.AttestationError("attestation_failed").completion_started)

    def test_unexpected_preflight_failure_is_known_to_precede_a_completion(self):
        with patch.object(nearai.Client, "preflight", side_effect=RuntimeError("unit-sentinel-value")):
            with self.assertRaisesRegex(nearai.AttestationError, "^attestation_failed$") as error:
                nearai.Client("unit-value").chat({"model": nearai.DEFAULT_MODEL}, timeout=1)
        self.assertFalse(error.exception.completion_started)

    def test_paid_calls_are_disabled_before_loading_the_sdk(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(nearai, "_sdk") as sdk:
            with self.assertRaisesRegex(nearai.AttestationError, "live_disabled"):
                nearai.Client("unit-value").chat({"model": nearai.DEFAULT_MODEL}, timeout=1)
        sdk.assert_not_called()

    def test_missing_policy_cannot_become_verified(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(nearai, "_sdk") as sdk:
            with self.assertRaisesRegex(nearai.AttestationError, "policy_unavailable"):
                nearai.Client().attest("ab" * 32)
        sdk.assert_not_called()

    def test_nonce_requires_exactly_32_hexadecimal_bytes(self):
        for nonce in (None, "", "ab" * 31, "ab" * 33, "zz" * 32, " ab" * 32):
            with self.subTest(nonce=nonce), self.assertRaisesRegex(nearai.AttestationError, "nonce_invalid"):
                nearai.Client().attest(nonce)

    def test_timeout_accepts_the_judge_deadline_and_refuses_unbounded_values(self):
        nearai._timeout(120)
        for value in (0, -1, True, float("inf"), float("nan"), 120.001):
            with self.subTest(value=value), self.assertRaisesRegex(nearai.AttestationError, "timeout_invalid"):
                nearai._timeout(value)

    def test_recorded_reports_keep_the_challenge_and_every_model_candidate(self):
        for name, count in (("deepseek", 2), ("glm", 4)):
            body = (ROOT / f"fixtures/nearai/attestation-{name}.json").read_bytes()
            capture = json.loads((ROOT / f"fixtures/nearai/attestation-{name}.capture.json").read_text())
            report = nearai.decode_report(body)
            self.assertEqual(len(report["model_attestations"]), count)
            self.assertEqual(report["gateway_attestation"]["request_nonce"], capture["nonce"])
            self.assertTrue(all(item["request_nonce"] == capture["nonce"]
                                for item in report["model_attestations"]))

    def test_recorded_deepseek_and_glm_signers_are_ambiguous(self):
        for name, count in (("deepseek", 2), ("glm", 4)):
            report = json.loads((ROOT / f"fixtures/nearai/attestation-{name}.json").read_text())
            models = [SimpleNamespace(signer=SimpleNamespace(signing_algo=row["signing_algo"],
                signing_address=row["signing_address"])) for row in report["model_attestations"]]
            self.assertEqual(len(models), count)
            with self.subTest(capture=name), self.assertRaisesRegex(
                    nearai.AttestationError, "^ambiguous_model_signer$"):
                nearai._check_signers(models)

    def test_duplicate_json_and_nonfinite_numbers_are_refused(self):
        for body in (b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'\xff', b'{'):
            with self.subTest(body=body), self.assertRaises(nearai.AttestationError):
                nearai.decode_report(body)

    def test_recording_hashes_and_catalog_rates_match_the_saved_files(self):
        for name in ("deepseek", "glm"):
            capture = json.loads((ROOT / f"fixtures/nearai/attestation-{name}.capture.json").read_text())
            body = (ROOT / f"fixtures/nearai/attestation-{name}.json").read_bytes()
            self.assertEqual(hashlib.sha256(body).hexdigest(), capture["sha256"])
        catalog = json.loads((ROOT / "fixtures/nearai/models.json").read_text())
        for row in catalog["data"]:
            self.assertEqual(nearai.PRICES[row["id"]], (round(row["pricing"]["input"] * 1000000),
                                                      round(row["pricing"]["output"] * 1000000)))
        manifest = json.loads((ROOT / "fixtures/nearai/manifest.json").read_text())
        self.assertTrue({"crosscheck-results.json", "crosscheck-verifier.json"}.issubset(
            row["file"] for row in manifest["files"]))
        for row in manifest["files"]:
            self.assertEqual(hashlib.sha256((ROOT / "fixtures/nearai" / row["file"]).read_bytes()).hexdigest(),
                             row["sha256"])

    def test_answer_adapter_preserves_usage_and_finish_semantics(self):
        for finish, expected in (("stop", "end_turn"), ("length", "max_tokens"),
                                 ("content_filter", "refusal"), ("tool_calls", "tool_calls")):
            body = {"choices": [{"message": {"content": "selection"}, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                              "completion_tokens_details": {"reasoning_tokens": 15},
                              "prompt_tokens_details": {"cached_tokens": 80}}}
            with self.subTest(finish=finish), patch.object(nearai.Client, "chat", return_value=body):
                output = nearai.Client("unit-value").create({}, timeout=1)
            self.assertEqual(output, {"stop_reason": expected, "text": "selection", "usage": {
                "input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0}})

    def test_invalid_usage_cannot_become_zero_billed_usage(self):
        body = {"choices": [{"message": {"content": "selection"}, "finish_reason": "stop"}]}
        with patch.object(nearai.Client, "chat", return_value=body):
            output = nearai.Client("unit-value").create({}, timeout=1)
        self.assertIsNone(output["usage"]["input_tokens"])
        self.assertIsNone(output["usage"]["output_tokens"])

    def test_null_response_message_is_a_provider_error(self):
        body = {"choices": [{"message": None, "finish_reason": "stop"}]}
        with patch.object(nearai.Client, "chat", return_value=body):
            with self.assertRaises(nearai.ProviderBadRequest):
                nearai.Client("unit-value").create({}, timeout=1)

    def test_policy_rejects_missing_empty_and_malformed_measurements(self):
        boot = {name: "ab" * 48 for name in nearai.BOOT_FIELDS}
        deployment = {"app_compose_sha256": "ab" * 32, "os_image_hash": "cd" * 32,
                      "compose_manager_actions_sha256": None}
        role = {"boot_measurements": [boot], "deployments": [deployment]}
        policy = {"version": 1, "gateway": role, "models": {nearai.DEFAULT_MODEL: role}}
        variants = [{}, {**policy, "version": True}, {**policy, "models": {}},
                    {**policy, "gateway": {**role, "deployments": []}},
                    {**policy, "gateway": {**role, "boot_measurements": [{**boot, "mr_td": "*"}]}}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            with patch.dict(os.environ, {"NEARAI_ATTESTATION_POLICY_FILE": str(path)}):
                for value in variants:
                    path.write_text(json.dumps(value))
                    with self.subTest(value=value), self.assertRaises(nearai.AttestationError):
                        nearai.load_policy(nearai.DEFAULT_MODEL)
                path.write_text(json.dumps(policy))
                self.assertEqual(nearai.load_policy(nearai.DEFAULT_MODEL), (role, role))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        nearai._close_runtime()
        broken = patch.object(nearai, "_broken_runtimes", [])
        broken.start()
        self.addCleanup(broken.stop)
        self.addCleanup(nearai._close_runtime)

    def test_loop_creation_and_close_belong_to_the_runtime_thread(self):
        created, closed = [], []
        new_event_loop = asyncio.new_event_loop

        def create():
            created.append(threading.get_ident())
            loop = new_event_loop()
            close = loop.close

            def close_on_thread():
                closed.append(threading.get_ident())
                close()

            loop.close = close_on_thread
            return loop

        with patch.object(nearai.asyncio, "new_event_loop", side_effect=create):
            runtime = nearai._get_runtime()
            runtime.run(asyncio.sleep(0), timeout=2)
            nearai._close_runtime()
        self.assertFalse(runtime.thread.is_alive())
        self.assertTrue(runtime.loop.is_closed())
        self.assertEqual(created, [runtime.thread.ident])
        self.assertEqual(closed, created)

    def test_broken_runtime_does_not_replace_the_process_wakeup_descriptor(self):
        with patch.object(signal, "set_wakeup_fd", wraps=signal.set_wakeup_fd) as wakeup:
            runtime, release, _ = self.blocked_runtime()
            release.set()
            runtime.thread.join(timeout=2)
            self.assertFalse(runtime.thread.is_alive())
            self.assertTrue(runtime.loop.is_closed())
        wakeup.assert_not_called()

    def test_loop_creation_failure_reaches_the_caller(self):
        with patch.object(nearai.asyncio, "new_event_loop", side_effect=OSError("loop unavailable")):
            with self.assertRaisesRegex(OSError, "^loop unavailable$"):
                nearai._get_runtime()
        self.assertIsNone(nearai._runtime)

    def blocked_runtime(self):
        runtime = nearai._get_runtime()
        entered, release = threading.Event(), threading.Event()
        client = SimpleNamespace(aclose=AsyncMock())
        runtime.clients["unit-value"] = client

        async def blocked():
            entered.set()
            release.wait()

        def cleanup():
            release.set()
            runtime.thread.join(timeout=2)
            self.assertFalse(runtime.thread.is_alive())

        self.addCleanup(cleanup)
        with ThreadPoolExecutor(max_workers=1) as workers:
            call = workers.submit(runtime.run, blocked(), timeout=0.1)
            self.assertTrue(entered.wait(2))
            with self.assertRaises(TimeoutError):
                call.result(timeout=2)
        self.assertTrue(runtime.broken)
        self.assertTrue(runtime.thread.is_alive())
        return runtime, release, client

    def test_broken_runtime_stops_and_closes_clients_after_unblocking(self):
        runtime, release, client = self.blocked_runtime()
        release.set()
        runtime.thread.join(timeout=2)
        self.assertFalse(runtime.thread.is_alive())
        self.assertTrue(runtime.loop.is_closed())
        client.aclose.assert_awaited_once_with()
        self.assertFalse(runtime.clients)

    def test_two_live_broken_runtimes_refuse_calls_until_one_thread_exits(self):
        first, release, client = self.blocked_runtime()
        second, _, _ = self.blocked_runtime()
        with patch.object(nearai, "_Runtime", wraps=nearai._Runtime) as construct:
            for _ in range(3):
                with self.assertRaises(nearai.ProviderTimeout):
                    nearai._get_runtime()
            construct.assert_not_called()
        release.set()
        first.thread.join(timeout=2)
        self.assertFalse(first.thread.is_alive())
        client.aclose.assert_awaited_once_with()
        replacement = nearai._get_runtime()
        self.assertIsNot(replacement, second)
        self.assertEqual(nearai._broken_runtimes, [second])
        self.assertIs(nearai._get_runtime(), replacement)

    def test_shutdown_skips_the_broken_loop_without_waiting_for_clients(self):
        runtime, release, client = self.blocked_runtime()
        with patch.object(runtime, "run", side_effect=AssertionError("close was queued on a broken loop")) as run:
            nearai._close_runtime()
        run.assert_not_called()
        self.assertIsNone(nearai._runtime)
        self.assertTrue(runtime.thread.is_alive())
        client.aclose.assert_not_awaited()
        release.set()
        runtime.thread.join(timeout=2)
        self.assertFalse(runtime.thread.is_alive())
        client.aclose.assert_awaited_once_with()


class SDKRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.sdk = nearai._sdk()
        except nearai.SDKUnavailable:
            raise unittest.SkipTest("optional pinned NEAR AI verification dependencies are unavailable")

    def setUp(self):
        self.addCleanup(nearai._close_runtime)
        from dcap_qvl import Quote, QuoteCollateralV3
        from nearai_inference_sdk.utils.fetch import FetchResponse

        self.raw = json.loads((ROOT / "fixtures/nearai/attestation-deepseek.json").read_text())
        gateway_report = json.loads((ROOT / "fixtures/nearai/attestation-deepseek-gateway.json").read_text())
        self.raw["gateway_attestation"] = gateway_report["gateway_attestation"]
        capture = json.loads((ROOT / "fixtures/nearai/attestation-deepseek.capture.json").read_text())
        self.nonce = capture["nonce"]
        self.model = self.raw["model_attestations"][0]["model_name"]
        self.peer = capture["peer_spki"]
        token_body = json.loads((ROOT / "fixtures/nearai/nras-deepseek-0.json").read_bytes())
        token = token_body[0][1]
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        self.now = payload["iat"] + 1

        class RecordedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(payload["iat"] + 1, tz or UTC)

        async def collateral(url, quote_bytes):
            self.assertEqual(url, "https://api.trustedservices.intel.com")
            key = Quote.parse(quote_bytes).fmspc().lower()
            saved = json.loads((ROOT / f"fixtures/nearai/intel-collateral-{key}.json").read_text())
            del saved["pck_certificate_chain"]
            return QuoteCollateralV3.from_json(json.dumps(saved))

        self.gpu_response = (ROOT / "fixtures/nearai/nras-deepseek-0.json").read_bytes()

        async def gpu_fetch(url, **kwargs):
            if url == "https://nras.attestation.nvidia.com/v3/attest/gpu":
                self.assertEqual(kwargs["data"], self.raw["model_attestations"][0]["nvidia_payload"])
                return FetchResponse(status=200, body=self.gpu_response)
            self.assertEqual(url, "https://nras.attestation.nvidia.com/.well-known/jwks.json")
            return FetchResponse(status=200, body=(ROOT / "fixtures/nearai/nvidia-jwks.json").read_bytes())

        connect = socket.socket.connect

        def offline_connect(connection, address):
            if address[0] not in ("127.0.0.1", "::1"):
                raise AssertionError("offline verification used network")
            return connect(connection, address)

        for target, kwargs in (
                ("socket.socket.connect", {"new": offline_connect}),
                ("nearai_inference_sdk.utils.intel.get_collateral", {"side_effect": collateral}),
                ("nearai_inference_sdk.utils.intel._unix_time", {"return_value": self.now}),
                ("nearai_inference_sdk.utils.nvidia.fetch", {"side_effect": gpu_fetch}),
                ("jwt.api_jwt.datetime", {"new": RecordedDatetime})):
            mock = patch(target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)
        self.roles = (self.role(self.raw["gateway_attestation"]), self.role(self.raw["model_attestations"][0]))

    def role(self, raw):
        from dcap_qvl import Quote
        quote = Quote.parse(bytes.fromhex(raw["intel_quote"])).report
        info = raw["info"]["tcb_info"]
        info = json.loads(info) if isinstance(info, str) else info
        manager = raw.get("compose_manager_attestation")
        action_hash = None if manager is None else hashlib.sha256(json.dumps(manager["actions"],
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return {"boot_measurements": [{key: getattr(quote, key).hex() for key in nearai.BOOT_FIELDS}],
                "deployments": [{"app_compose_sha256": hashlib.sha256(info["app_compose"].encode()).hexdigest(),
                                 "os_image_hash": raw["info"]["os_image_hash"],
                                 "compose_manager_actions_sha256": action_hash}]}

    def verify(self, report=None, nonce=None, peer=None, roles=None):
        roles = self.roles if roles is None else roles
        gateway = nearai._options(self.sdk, roles[0])
        model = nearai._options(self.sdk, roles[1], model=True)
        report = copy.deepcopy(self.raw) if report is None else report
        return asyncio.run(nearai.verify_report(report, nonce or self.nonce, peer or self.peer,
                                                self.model, gateway, model, self.sdk))

    def single_valid_candidate(self):
        report = copy.deepcopy(self.raw)
        report["model_attestations"] = report["model_attestations"][:1]
        return report

    def test_one_recorded_candidate_has_valid_cpu_gpu_nonce_and_manager_evidence(self):
        verified = self.verify(self.single_valid_candidate())
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["models"][0]["gpu_evidence"], "verified")
        self.assertEqual(verified["models"][0]["deployment_policy"], "verified")

    def test_attest_passes_the_callers_nonce_and_returns_only_verified_evidence(self):
        with patch.object(nearai, "load_policy", return_value=self.roles), patch.object(nearai, "_fetch_report",
                new_callable=AsyncMock, return_value=(json.dumps(self.single_valid_candidate()).encode(),
                                                     self.peer)) as fetch:
            result = nearai.Client().attest(self.nonce, self.model)
        fetch.assert_awaited_once_with(self.nonce, self.model)
        self.assertTrue(result["verified"])
        self.assertEqual(result["nonce"], self.nonce)

    def test_attest_rejects_shared_signers_even_when_every_quote_verifies(self):
        report = self.single_valid_candidate()
        report["model_attestations"].append(copy.deepcopy(report["model_attestations"][0]))
        with self.assertRaisesRegex(nearai.AttestationError, "ambiguous_model_signer"):
            self.verify(report)

    def test_second_chat_reuses_the_sdk_attestation_cache_across_wrappers(self):
        attestation = SimpleNamespace(models=[SimpleNamespace(signer=SimpleNamespace(
            signing_algo="ed25519", signing_address="ab" * 32))])
        session = SimpleNamespace(attestation_result=attestation)
        instances = []
        sdk_client = self.sdk.InferenceClient

        def factory(*args, **kwargs):
            client = sdk_client(*args, **kwargs)
            client.chat = SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(
                return_value=SimpleNamespace(id="chat_1", model_dump=lambda **kwargs: {"id": "chat_1"}))))
            client.verify_response = AsyncMock(return_value=SimpleNamespace(
                signature_kind="provider_tee", attestations=(object(),)))
            instances.append(client)
            return client

        with patch.dict(os.environ, {"SIYAB_NEARAI_LIVE": "1"}), patch.object(nearai, "load_policy",
                return_value=self.roles), patch.object(self.sdk, "InferenceClient", side_effect=factory), \
                patch.object(sdk_client, "_create_session", new_callable=AsyncMock, return_value=session) as verify:
            self.assertEqual(nearai.Client("cache-value").chat({"model": self.model}, timeout=10), {"id": "chat_1"})
            self.assertEqual(nearai.Client("cache-value").chat({"model": self.model}, timeout=10), {"id": "chat_1"})
            with ThreadPoolExecutor(max_workers=2) as workers:
                calls = [workers.submit(nearai.Client("cache-value").chat, {"model": self.model}, 10)
                         for _ in range(2)]
                self.assertEqual([call.result() for call in calls], [{"id": "chat_1"}] * 2)
        self.assertEqual(len(instances), 1)
        verify.assert_awaited_once_with(self.model)

    def test_changed_policy_does_not_reuse_the_previous_verified_session(self):
        first, _ = self.client()
        second, _ = self.client()
        changed = copy.deepcopy(self.roles)
        changed[1]["boot_measurements"][0]["mr_td"] = "aa" * 48
        with patch.dict(os.environ, {"SIYAB_NEARAI_LIVE": "1"}), patch.object(nearai, "load_policy",
                side_effect=[self.roles, changed]), patch.object(self.sdk, "InferenceClient",
                side_effect=[first, second]) as construct:
            nearai.Client("unit-value").chat({"model": self.model}, timeout=1)
            nearai.Client("unit-value").chat({"model": self.model}, timeout=1)
        self.assertEqual(construct.call_count, 2)
        first.verify.assert_awaited_once_with(self.model)
        second.verify.assert_awaited_once_with(self.model)

    def test_tcb_advisories_debug_and_configuration_tail_are_rejected(self):
        from dcap_qvl import Quote
        quote = Quote.parse(bytes.fromhex(self.raw["gateway_attestation"]["intel_quote"])).report
        values = {"tcb_status": "UpToDate", "advisory_ids": (), "debug_enabled": False,
                  "report_data": quote.report_data, "mr_config_id": quote.mr_config_id, "rt_mr3": quote.rt_mr3}
        for edit in ({"tcb_status": "OutOfDate"}, {"advisory_ids": ("advisory",)}, {"debug_enabled": True},
                     {"mr_config_id": quote.mr_config_id[:-1] + b"\x01"}):
            verifier = AsyncMock(return_value=self.sdk.TdxQuoteVerificationResult(**(values | edit)))
            with patch.object(self.sdk, "create_tdx_quote_verifier", return_value=verifier):
                options = nearai._options(self.sdk, self.roles[0])
            with self.subTest(edit=edit), self.assertRaises(nearai.AttestationError):
                asyncio.run(options.verifiers.tdx_quote(self.raw["gateway_attestation"]["intel_quote"]))

    def test_machine_chain_mismatch_is_rejected_and_embedded_quote_chain_verifies(self):
        from dcap_qvl import Quote, QuoteCollateralV3, verify

        raw = self.raw["model_attestations"][1]
        quote_bytes = bytes.fromhex(raw["intel_quote"])
        fmspc = Quote.parse(quote_bytes).fmspc().lower()
        saved = (ROOT / f"fixtures/nearai/intel-collateral-{fmspc}.json").read_text()
        with self.assertRaisesRegex(ValueError, "Signature is invalid for qe_report"):
            verify(quote_bytes, QuoteCollateralV3.from_json(saved), self.now)
        checked = asyncio.run(self.sdk.create_tdx_quote_verifier()(raw["intel_quote"]))
        self.assertEqual(checked.tcb_status, "UpToDate")
        self.assertEqual(checked.advisory_ids, ())
        with self.assertRaisesRegex(nearai.AttestationError, "^attestation_failed$"):
            self.verify()

    def test_recorded_cpu_crosscheck_matches_all_quotes_and_negative_controls(self):
        import dcap_qvl as dcap
        from scripts.nearai_crosscheck import dcap_result, leaf_digest

        root = ROOT / "fixtures/nearai"
        expected = json.loads((root / "crosscheck-results.json").read_text())
        metadata = json.loads((root / "crosscheck-verifier.json").read_text())
        original = (root / "quote-results.json").read_bytes()
        self.assertEqual(expected["original_results_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(expected["verification_time_unix"], json.loads(original)["checked_at_unix"])
        self.assertEqual(expected["dcap_qvl"], dcap.__version__)
        self.assertEqual(expected["intel_qvl"], metadata["intel_qvl"])
        now = expected["verification_time_unix"]
        rows, sources, controls = [], {}, []
        for capture in ("deepseek", "glm"):
            path = root / f"attestation-{capture}.json"
            sources[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            report = json.loads(path.read_text())
            quotes = [(None, "gateway", report["gateway_attestation"]["intel_quote"])]
            for index, model in enumerate(report["model_attestations"]):
                quotes.extend(((index, "model", model["intel_quote"]),
                               (index, "manager", model["compose_manager_attestation"]["quote"])))
            for candidate, kind, encoded in quotes:
                quote = bytes.fromhex(encoded)
                parsed = dcap.Quote.parse(quote)
                fmspc = parsed.fmspc().lower()
                path = root / f"intel-collateral-{fmspc}.json"
                sources[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
                saved = json.loads(path.read_text())
                corrected = dict(saved)
                del corrected["pck_certificate_chain"]
                embedded_leaf = leaf_digest(parsed.cert_chain_pem_bytes().decode("ascii"))
                saved_leaf = leaf_digest(saved["pck_certificate_chain"])
                rows.append({"capture": capture, "candidate": candidate, "kind": kind,
                    "quote_sha256": hashlib.sha256(quote).hexdigest(), "fmspc": fmspc,
                    "embedded_pck_leaf_sha256": embedded_leaf, "saved_pck_leaf_sha256": saved_leaf,
                    "saved_chain_matches_quote": embedded_leaf == saved_leaf,
                    "dcap_original_collateral": dcap_result(dcap, quote, saved, now),
                    "dcap_quote_embedded_chain": dcap_result(dcap, quote, corrected, now)})
                if capture == "deepseek" and candidate == 0 and kind == "model":
                    control_quote, control_collateral = quote, corrected
        self.assertEqual(len(rows), 14)
        self.assertEqual(sources, expected["source_sha256"])
        self.assertEqual(rows, [{key: value for key, value in row.items() if key != "intel_qvl"}
                                for row in expected["results"]])
        self.assertEqual(control_quote[:2], bytes((4, 0)))
        self.assertEqual(control_quote[764:766], bytes((6, 0)))
        for name, offset, check_time in (("td_report_bit_changed", 60, now),
                                        ("qe_report_signature_bit_changed", 1154, now),
                                        ("expired_collateral", None, 2145916800)):
            altered = bytearray(control_quote)
            if offset is not None:
                altered[offset] ^= 1
            controls.append({"case": name, "verification_time_unix": check_time,
                "dcap_quote_embedded_chain": dcap_result(dcap, bytes(altered), control_collateral, check_time)})
        self.assertEqual(controls, [{key: value for key, value in row.items() if key != "intel_qvl"}
                                    for row in expected["negative_controls"]])

    def test_nonce_cannot_be_changed_even_if_echoes_are_rewritten(self):
        report = self.single_valid_candidate()
        for raw in [report["gateway_attestation"]] + report["model_attestations"]:
            raw["request_nonce"] = "ac" * 32
        with self.assertRaises(nearai.AttestationError):
            self.verify(report, nonce="ac" * 32)

    def test_tls_identity_and_required_echoed_quote_data_are_enforced(self):
        for field, value in (("tls_cert_fingerprint", None), ("report_data", None)):
            report = self.single_valid_candidate()
            report["gateway_attestation"][field] = value
            with self.subTest(field=field), self.assertRaises(nearai.AttestationError):
                self.verify(report)
        with self.assertRaises(nearai.AttestationError):
            self.verify(self.single_valid_candidate(), peer="aa" * 32)

    def test_missing_gpu_evidence_is_refused(self):
        report = self.single_valid_candidate()
        del report["model_attestations"][0]["nvidia_payload"]
        with self.assertRaises(nearai.AttestationError):
            self.verify(report)

    def test_changed_signer_compose_actions_and_boot_policy_are_refused(self):
        for field in ("signer", "compose", "actions", "boot"):
            report, roles = self.single_valid_candidate(), copy.deepcopy(self.roles)
            raw = report["model_attestations"][0]
            if field == "signer":
                raw["signing_address"] = "aa" * 32
            elif field == "compose":
                info = raw["info"]["tcb_info"]
                info = json.loads(info) if isinstance(info, str) else info
                info["app_compose"] += " "
                raw["info"]["tcb_info"] = info
            elif field == "actions":
                raw["compose_manager_attestation"]["actions"][0]["timestamp"] += "x"
            else:
                roles[1]["boot_measurements"][0]["mr_td"] = "aa" * 48
            with self.subTest(field=field), self.assertRaises(nearai.AttestationError):
                self.verify(report, roles=roles)

    def test_unsigned_nvidia_success_and_corrupted_jwt_are_refused(self):
        corrupted = json.loads(self.gpu_response)
        corrupted[0][1] += "x"
        for response in (b'{"valid":true}', json.dumps(corrupted).encode()):
            self.gpu_response = response
            with self.subTest(response=response[:24]), self.assertRaises(nearai.AttestationError):
                self.verify(self.single_valid_candidate())

    def client(self, identities=None, kind="provider_tee"):
        client = SimpleNamespace(verify=AsyncMock(return_value=SimpleNamespace(models=identities or [
            SimpleNamespace(signer=SimpleNamespace(signing_algo="ed25519", signing_address="ab" * 32))])),
            chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(
                id="chat_1", model_dump=lambda **kwargs: {"id": "chat_1"})))),
            verify_response=AsyncMock(return_value=SimpleNamespace(signature_kind=kind, attestations=(object(),))))
        client.aclose = AsyncMock()
        return client, client

    def chat(self, context):
        nearai._close_runtime()
        with patch.dict(os.environ, {"SIYAB_NEARAI_LIVE": "1"}), patch.object(nearai, "load_policy",
                return_value=self.roles), patch.object(self.sdk, "InferenceClient", return_value=context):
            return nearai.Client("unit-value").chat({"model": self.model}, timeout=1)

    def test_reply_is_released_only_after_provider_receipt_verification(self):
        client, context = self.client()
        self.assertEqual(self.chat(context), {"id": "chat_1"})
        client.verify.assert_awaited_once_with(self.model)
        client.verify_response.assert_awaited_once_with("chat_1")

    def test_gateway_receipt_and_missing_receipt_never_release_content(self):
        client, context = self.client(kind="gateway")
        with self.assertRaises(nearai.AttestationError) as error:
            self.chat(context)
        self.assertTrue(error.exception.completion_started)
        client, context = self.client()
        client.verify_response.side_effect = self.sdk.VerificationError(
            self.sdk.VerificationFailure(code="signature.invalid"))
        with self.assertRaises(nearai.AttestationError):
            self.chat(context)

    def test_equivalent_shared_signers_are_rejected_before_any_paid_request(self):
        for address in ("ab" * 32, "AB" * 32, "0x" + "ab" * 32):
            identities = [SimpleNamespace(signer=SimpleNamespace(signing_algo="ed25519", signing_address=value))
                          for value in ("ab" * 32, address)]
            client, context = self.client(identities)
            with self.subTest(address=address), self.assertRaises(nearai.AttestationError) as error:
                self.chat(context)
            self.assertFalse(error.exception.completion_started)
            client.chat.completions.create.assert_not_awaited()

    def test_unexpected_client_setup_failure_cannot_start_a_completion(self):
        client, context = self.client()
        with patch.object(nearai._Runtime, "client", side_effect=RuntimeError("unit-sentinel-value")):
            with self.assertRaisesRegex(nearai.AttestationError, "^attestation_failed$") as error:
                self.chat(context)
        self.assertFalse(error.exception.completion_started)
        client.verify.assert_not_awaited()
        client.chat.completions.create.assert_not_awaited()

    def test_unexpected_verifier_and_paid_failures_keep_the_completion_state(self):
        for stage, started in (("verify", False), ("create", True), ("receipt", True)):
            client, context = self.client()
            target = {"verify": client.verify, "create": client.chat.completions.create,
                      "receipt": client.verify_response}[stage]
            target.side_effect = RuntimeError("unit-sentinel-value")
            with self.subTest(stage=stage):
                with self.assertRaisesRegex(nearai.AttestationError, "^attestation_failed$") as error:
                    self.chat(context)
                self.assertEqual(error.exception.completion_started, started)
                self.assertEqual(client.chat.completions.create.await_count, int(started))

    def test_malformed_preflight_signer_is_rejected_before_a_completion(self):
        client, context = self.client()
        client.verify.return_value.models[0].signer.signing_address = "nonhex"
        with self.assertRaisesRegex(nearai.AttestationError, "^attestation_failed$") as error:
            self.chat(context)
        self.assertFalse(error.exception.completion_started)
        client.chat.completions.create.assert_not_awaited()

    def test_outer_timeout_bounds_a_blocked_loop_and_prevents_a_late_completion(self):
        client, context = self.client()
        entered, release = threading.Event(), threading.Event()

        async def blocked(model):
            entered.set()
            release.wait(20)
            return client.verify.return_value

        client.verify.side_effect = blocked
        runtime = nearai._get_runtime()
        with patch.dict(os.environ, {"SIYAB_NEARAI_LIVE": "1"}), patch.object(nearai, "load_policy",
                return_value=self.roles), patch.object(self.sdk, "InferenceClient", return_value=context), \
                patch.object(runtime, "run", wraps=runtime.run) as run:
            with ThreadPoolExecutor(max_workers=1) as workers:
                call = workers.submit(nearai.Client("unit-value").chat, {"model": self.model}, 1)
                try:
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(nearai.ProviderTimeout) as error:
                        call.result(timeout=8)
                    self.assertFalse(error.exception.completion_started)
                    self.assertEqual(run.call_args.kwargs, {"timeout": 6})
                finally:
                    release.set()
        runtime.thread.join(timeout=2)
        self.assertFalse(runtime.thread.is_alive())
        client.aclose.assert_awaited_once_with()
        client.chat.completions.create.assert_not_awaited()

    def test_queued_call_cannot_start_after_the_outer_timeout(self):
        client, context = self.client()
        entered, release = threading.Event(), threading.Event()
        runtime = nearai._get_runtime()

        def blocked():
            entered.set()
            release.wait(20)

        runtime.loop.call_soon_threadsafe(blocked)
        try:
            self.assertTrue(entered.wait(2))
            with patch.dict(os.environ, {"SIYAB_NEARAI_LIVE": "1"}), patch.object(nearai, "load_policy",
                    return_value=self.roles), patch.object(self.sdk, "InferenceClient", return_value=context):
                with self.assertRaises(nearai.ProviderTimeout) as error:
                    nearai.Client("unit-value").chat({"model": self.model}, timeout=0.001)
                self.assertFalse(error.exception.completion_started)
                release.set()
                runtime.thread.join(timeout=2)
                self.assertFalse(runtime.thread.is_alive())
        finally:
            release.set()
        client.verify.assert_not_awaited()
        client.chat.completions.create.assert_not_awaited()

    def test_next_chat_recovers_while_the_timed_out_verifier_stays_blocked(self):
        first, _ = self.client()
        second, _ = self.client()
        entered, release = threading.Event(), threading.Event()

        async def blocked(model):
            entered.set()
            release.wait()
            return first.verify.return_value

        first.verify.side_effect = blocked
        runtime = nearai._get_runtime()
        try:
            with patch.dict(os.environ, {"SIYAB_NEARAI_LIVE": "1"}), patch.object(nearai, "load_policy",
                    return_value=self.roles), patch.object(self.sdk, "InferenceClient",
                    side_effect=[first, second]) as construct:
                with ThreadPoolExecutor(max_workers=1) as workers:
                    call = workers.submit(nearai.Client("unit-value").chat, {"model": self.model}, 1)
                    try:
                        self.assertTrue(entered.wait(2))
                        with self.assertRaises(nearai.ProviderTimeout) as error:
                            call.result(timeout=8)
                        self.assertFalse(error.exception.completion_started)
                        for _ in range(2):
                            self.assertEqual(nearai.Client("unit-value").chat({"model": self.model}, timeout=1),
                                             {"id": "chat_1"})
                        self.assertIsNot(nearai._get_runtime(), runtime)
                        self.assertTrue(runtime.thread.is_alive())
                        self.assertFalse(release.is_set())
                        self.assertEqual(construct.call_count, 2)
                        self.assertEqual(second.chat.completions.create.await_count, 2)
                        self.assertEqual(second.verify_response.await_count, 2)
                    finally:
                        release.set()
        finally:
            release.set()
            runtime.thread.join(timeout=2)
            self.assertFalse(runtime.thread.is_alive())
            first.aclose.assert_awaited_once_with()
        first.chat.completions.create.assert_not_awaited()

    def test_preflight_timeout_cannot_send_a_completion(self):
        client, context = self.client()

        async def delayed(model):
            await asyncio.sleep(0.02)

        client.verify.side_effect = delayed
        runtime = nearai._get_runtime()
        with patch.dict(os.environ, {"SIYAB_NEARAI_LIVE": "1"}), patch.object(nearai, "load_policy",
                return_value=self.roles), patch.object(self.sdk, "InferenceClient", return_value=context):
            with self.assertRaises(nearai.ProviderTimeout) as error:
                nearai.Client("unit-value").chat({"model": self.model}, timeout=0.001)
        self.assertFalse(error.exception.completion_started)
        self.assertIs(nearai._get_runtime(), runtime)
        nearai._get_runtime().run(asyncio.sleep(0.03))
        client.chat.completions.create.assert_not_awaited()

    def test_sdk_fetch_is_keyless_bounded_and_refuses_redirects(self):
        from nearai_inference_sdk.utils.fetch import FetchResponse
        with patch("nearai_inference_sdk.utils.fetch.fetch", new_callable=AsyncMock,
                return_value=FetchResponse(status=200, body=b'{"model_attestations":[]}',
                                          peer_spki_fingerprint=self.peer)) as fetch:
            body, peer = asyncio.run(nearai._fetch_report(self.nonce, self.model))
            self.assertEqual(json.loads(body), {"model_attestations": []})
            self.assertEqual(peer, self.peer)
        self.assertEqual(fetch.await_count, 2)
        self.assertIn("include_tls_fingerprint=true", fetch.call_args_list[0].args[0])
        url = fetch.call_args_list[1].args[0]
        self.assertIn("provider=near", url)
        self.assertIn("include_tls_fingerprint=false", url)
        self.assertIn("signing_algo=ed25519", url)
        self.assertIn(self.nonce, url)
        self.assertNotIn("Authorization", fetch.call_args.kwargs["headers"])
        self.assertFalse(fetch.call_args.kwargs["allow_redirects"])
        self.assertEqual(fetch.call_args.kwargs["max_body_bytes"], nearai.MAX_REPORT_BYTES)
