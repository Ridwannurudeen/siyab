import contextlib
import asyncio
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from enclave import measure, relay

ROOT = Path(__file__).resolve().parent.parent
VECTORS = json.loads((ROOT / "fixtures/attest/vectors.json").read_text(encoding="utf-8"))


def attestation():
    row = VECTORS["registration_args"][0]["arguments"]
    tcb = {field: "00" * 48 for field in ("mrtd", "rtmr0", "rtmr1", "rtmr2")}
    register = bytes(48)
    for event in row["event_log"]:
        register = hashlib.sha384(register + bytes.fromhex(event["digest"])).digest()
    tcb.update(rtmr3=register.hex(), app_compose=row["app_compose"],
               compose_hash=hashlib.sha256(row["app_compose"].encode()).hexdigest(),
               event_log=copy.deepcopy(row["event_log"]))
    result = {"registry": "registry.testnet", "public_key_hex": row["public_key"],
              "issued_at_ms": row["issued_at_ms"], "aux": row["aux"], "tcb_info": tcb}
    quote = bytearray(48 + 584)
    quote[:2] = (4).to_bytes(2, "little")
    quote[4:8] = (129).to_bytes(4, "little")
    quote[168:176] = (0x10000000).to_bytes(8, "little")
    quote[48 + 472:48 + 520] = register
    quote[48 + 520:] = measure.report_data(result["registry"], result["public_key_hex"],
                                          result["issued_at_ms"], result["aux"])
    result["quote"] = quote.hex()
    return result


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.collateral = copy.deepcopy(VECTORS["registration_args"][0]["verification_input"]["collateral"])

    def test_exact_registration_tuple_vectors(self):
        for row in VECTORS["registration_args"]:
            with self.subTest(row=row["name"]):
                source = row["verification_input"]
                encoded = relay.verification_args(bytes.fromhex(source["quote_hex"]), source["collateral"])
                self.assertEqual(encoded, row["arguments"]["verification_args"])
                self.assertEqual(relay.decode_verification_args(encoded),
                                 (bytes.fromhex(source["quote_hex"]), source["collateral"]))

    def test_tuple_refuses_malformed_and_trailing_bytes(self):
        encoded = VECTORS["registration_args"][0]["arguments"]["verification_args"]
        for candidate in (encoded + "00", encoded[:-2], encoded[:-2] + "02", "ffffffff", "FF", "0x00", "0"):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                relay.decode_verification_args(candidate)

    def test_collateral_requires_exact_fields_and_byte_values(self):
        for field, value in (("root_ca_crl", [True]), ("pck_crl", [-1]),
                             ("qe_identity_signature", "0000"), ("pck_certificate_chain", 4)):
            bad = dict(self.collateral, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                relay.verification_args(b"x", bad)
        with self.assertRaises(ValueError):
            relay.verification_args(b"x", dict(self.collateral, extra="x"))
        with self.assertRaises(ValueError):
            relay.verification_args(b"x" * (relay.MAX_BYTES + 1), self.collateral)

    def test_registration_exact_shape_and_binding(self):
        record = attestation()
        arguments = relay.register_arguments(record, self.collateral)
        self.assertEqual(set(arguments), {"public_key", "issued_at_ms", "aux", "verification_args",
                                         "app_compose", "event_log"})
        self.assertEqual(arguments["app_compose"], record["tcb_info"]["app_compose"])
        for field, value in (("registry", "other.testnet"), ("public_key_hex", "11" * 32),
                             ("issued_at_ms", record["issued_at_ms"] + 1), ("aux", "11" * 32)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                relay.register_arguments(dict(record, **{field: value}), self.collateral)

    def test_registration_rejects_quote_tcb_and_replay_mutations(self):
        for mutation in ("register", "digest", "missing", "debug", "truncated", "extra"):
            record = attestation()
            if mutation == "register":
                record["tcb_info"]["rtmr0"] = "11" * 48
            elif mutation == "digest":
                record["tcb_info"]["event_log"][0]["digest"] = "11" * 48
            elif mutation == "missing":
                record["tcb_info"]["event_log"] = []
            elif mutation == "debug":
                quote = bytearray.fromhex(record["quote"])
                quote[168] = 1
                record["quote"] = quote.hex()
            elif mutation == "truncated":
                record["quote"] = record["quote"][:100]
            else:
                record["extra"] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                relay.register_arguments(record, self.collateral)

    def test_command_exact_budget_and_network(self):
        argv = relay.command("registry.testnet", {}, "owner.testnet", "testnet")
        self.assertEqual(argv[argv.index("prepaid-gas") + 1], "300 Tgas")
        self.assertEqual(argv[argv.index("attached-deposit") + 1], "0.01 NEAR")
        self.assertEqual(argv[-1], "send")
        for registry, relayer, network in (("registry.testnet", "owner.near", "testnet"),
                                           ("registry.testnet", "owner.testnet", "mainnet"),
                                           ("registry.near", "owner.near", "arbitrary")):
            with self.assertRaises(ValueError):
                relay.command(registry, {}, relayer, network)
        with self.assertRaises(ValueError):
            relay.command("registry.testnet", {}, "owner.testnet", "testnet", ROOT / "key.json")

    def test_root_testnet_account_matches_only_testnet(self):
        self.assertEqual(relay.command("testnet", {}, "testnet", "testnet")[4], "testnet")
        for registry, relayer in (("testnet", "owner.near"), ("registry.near", "testnet")):
            with self.assertRaises(ValueError):
                relay.command(registry, {}, relayer, "mainnet")

    def test_default_only_prints_no_fetch_key_read_or_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "attestation.json"
            collateral = root / "collateral.json"
            report.write_text(json.dumps(attestation()), encoding="utf-8")
            collateral.write_text(json.dumps(self.collateral), encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(relay.subprocess, "run") as run, mock.patch.object(relay, "fetch_collateral") as fetch:
                with contextlib.redirect_stdout(output):
                    result = relay.main(["--attestation", str(report), "--collateral", str(collateral),
                                         "--relayer", "owner.testnet", "--network", "testnet",
                                         "--key-file", str(root / "nonexistent-key.json")])
                self.assertEqual(result, 0)
                run.assert_not_called()
                fetch.assert_not_called()
            self.assertFalse(json.loads(output.getvalue())["submitted"])

    def test_send_without_key_refused_before_input_read(self):
        with mock.patch.object(relay, "_read_json") as read, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                relay.main(["--attestation", "absent.json", "--collateral", "absent.json",
                            "--relayer", "owner.testnet", "--network", "testnet", "--send"])
            read.assert_not_called()

    def test_explicit_send_uses_argv_and_never_prints_cli_output(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "unread-key.json"
            argv = ["--attestation", "attestation.json", "--collateral", "collateral.json",
                    "--relayer", "owner.testnet", "--network", "testnet", "--send", "--key-file", str(key_file)]
            with mock.patch.object(relay, "_read_json", side_effect=[attestation(), self.collateral]):
                with mock.patch.object(relay.subprocess, "run", return_value=mock.Mock(returncode=0,
                                       stdout="not public", stderr="not public")) as run:
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(relay.main(argv), 0)
                    passed = run.call_args.args[0]
                    self.assertIsInstance(passed, list)
                    self.assertNotIn("shell", run.call_args.kwargs)
                    self.assertEqual(passed[-2:], [str(key_file), "send"])
                    self.assertNotIn("not public", output.getvalue())
                    self.assertTrue(json.loads(output.getvalue().splitlines()[-1])["submitted"])

    def test_json_duplicate_and_size_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for body in ('{"a":1,"a":2}', " " * (relay.MAX_BYTES + 1)):
                path.write_text(body)
                with self.assertRaises(ValueError):
                    relay._read_json(path)

    def test_fetch_pinned_version_and_current_quote_mapping(self):
        quote = b"this quote"

        def fetch(url, supplied):
            self.assertEqual(url, "https://api.trustedservices.intel.com")
            self.assertIs(supplied, quote)
            future = asyncio.get_running_loop().create_future()
            future.set_result(mock.Mock(to_json=lambda: json.dumps(self.collateral)))
            return future

        with mock.patch.object(relay.importlib.metadata, "version", return_value="0.7.0"):
            with mock.patch.dict("sys.modules", {"dcap_qvl": mock.Mock(get_collateral=fetch)}):
                self.assertEqual(relay.fetch_collateral(quote), self.collateral)
        with mock.patch.object(relay.importlib.metadata, "version", return_value="0.7.1"):
            with self.assertRaises(ValueError):
                relay.fetch_collateral(quote)

    def test_fetch_total_timeout_cancels_and_sanitizes_errors(self):
        original_wait_for = asyncio.wait_for
        pending = []

        def fetch(url, quote):
            future = asyncio.get_running_loop().create_future()
            pending.append(future)
            return future

        async def short_wait(awaitable, timeout):
            self.assertEqual(timeout, 60)
            return await original_wait_for(awaitable, 0.001)

        with mock.patch.object(relay.importlib.metadata, "version", return_value="0.7.0"):
            with mock.patch.dict("sys.modules", {"dcap_qvl": mock.Mock(get_collateral=fetch)}):
                with mock.patch.object(relay.asyncio, "wait_for", side_effect=short_wait):
                    with self.assertRaisesRegex(ValueError, "^collateral fetch failed$"):
                        relay.fetch_collateral(b"quote")
            self.assertTrue(pending[0].cancelled())
            with mock.patch.dict("sys.modules", {"dcap_qvl": mock.Mock(get_collateral=mock.Mock(
                    side_effect=ValueError("upstream sensitive details")))}):
                with self.assertRaisesRegex(ValueError, "^collateral fetch failed$"):
                    relay.fetch_collateral(b"quote")


if __name__ == "__main__":
    unittest.main()
