import base64
import contextlib
import copy
import hashlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from gate import base58, chainsig, ed25519, rpc
from gate.chainsig import addresses
from gate.chainsig import __main__ as cli
from gate.chainsig.keccak import keccak256
from gate.chainsig.views import fetch_root, read_derivation
from tests.data import chainsig_record

GENERATOR = "secp256k1:" + base58.encode(
    bytes.fromhex("79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
                  "483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8"))
ED_GENERATOR = base58.encode_key(ed25519.encodepoint(ed25519.B))
REFERENCE = Path(__file__).parent / "data" / "chainsig_reference.json"


class ReferenceVectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(REFERENCE.read_text(encoding="utf-8"))
        cls.roots = {row["id"]: row for row in cls.data["root_observations"]}
        cls.children = {}
        for vector in cls.data["vectors"]:
            for curve in ("secp256k1", "ed25519"):
                root = cls.roots[vector["root_id"]]["public_keys"][str(vector[curve]["domain_id"])]
                cls.children[vector["name"], curve] = chainsig.derive_public_key(
                    root, vector["predecessor"], vector["path"])

    def test_tweaks_match_reference_sha3(self):
        for vector in self.data["vectors"]:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(chainsig.tweak(vector["predecessor"], vector["path"]).hex(), vector["tweak_hex"])

    def test_secp_children_match_reference_keys(self):
        for vector in self.data["vectors"]:
            with self.subTest(vector=vector["name"]):
                child = self.children[vector["name"], "secp256k1"]
                expected = vector["secp256k1"]
                self.assertEqual(child, expected["public_key_naj"])
                self.assertEqual(chainsig.secp256k1_bytes(child).hex(), expected["public_key_sec1_hex"])
                self.assertEqual(chainsig.secp256k1_bytes(child, compressed=True).hex(),
                                 expected["public_key_compressed_hex"])

    def test_ed_children_match_reference_keys(self):
        for vector in self.data["vectors"]:
            with self.subTest(vector=vector["name"]):
                child = self.children[vector["name"], "ed25519"]
                self.assertEqual(child, vector["ed25519"]["public_key_naj"])
                self.assertEqual(chainsig.ed25519_bytes(child).hex(), vector["ed25519"]["public_key_hex"])

    def test_ethereum_addresses_match_reference_adapter(self):
        for vector in self.data["vectors"]:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(addresses.ethereum_address(self.children[vector["name"], "secp256k1"]),
                                 vector["secp256k1"]["ethereum"])

    def test_bitcoin_encodings_match_reference_for_every_network(self):
        for vector in self.data["vectors"]:
            child = self.children[vector["name"], "secp256k1"]
            for network, encodings in vector["secp256k1"]["bitcoin"].items():
                for kind, expected in encodings.items():
                    with self.subTest(vector=vector["name"], network=network, kind=kind):
                        self.assertEqual(addresses.bitcoin_address(child, network=network, kind=kind), expected)

    def test_solana_addresses_match_reference_adapter(self):
        for vector in self.data["vectors"]:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(addresses.solana_address(self.children[vector["name"], "ed25519"]),
                                 vector["ed25519"]["solana"])

    def test_keccak_matches_independent_vectors_at_sponge_boundaries(self):
        for vector in self.data["keccak_vectors"]:
            with self.subTest(length=vector["length"]):
                message = bytes.fromhex(vector["input_hex"])
                self.assertEqual(len(message), vector["length"])
                self.assertEqual(keccak256(message).hex(), vector["digest_hex"])

    def test_children_match_recorded_signer_views(self):
        for observation in self.data["contract_derived_observations"]:
            for domain, public_key in observation["public_keys"].items():
                with self.subTest(vector=observation["vector"], domain=domain):
                    curve = public_key.split(":", 1)[0]
                    self.assertEqual(self.children[observation["vector"], curve], public_key)


class RecordedSignerViewsTest(unittest.TestCase):
    def test_recorded_views_replay_without_network_access(self):
        output = io.StringIO()
        with mock.patch.object(rpc, "_fetch", side_effect=AssertionError("offline replay contacted the network")):
            with contextlib.redirect_stdout(output):
                self.assertEqual(chainsig_record.main([]), 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "state": 2, "public_key": 8, "derived_public_key": 26, "version": 2, "config": 2})

    def test_every_recorded_view_retains_raw_bytes_and_block_context(self):
        data = json.loads(REFERENCE.read_text(encoding="utf-8"))
        roots = {root["rpc"]: root for root in data["root_observations"]}
        recordings = list(chainsig_record.RECORDINGS.glob("*.json"))
        self.assertEqual(len(recordings), 42)
        views = 0
        for path in recordings:
            row = json.loads(path.read_text(encoding="utf-8"))
            request, response = row["request"], row["response"]["result"]
            root = roots[row["url"]]
            with self.subTest(recording=path.name):
                self.assertEqual(path.stem, rpc.fixture_key(row["url"], request["method"], request["params"]))
                self.assertTrue(row["fetched_at"])
                self.assertEqual(request["params"]["block_id"], root["block_height"])
                if request["method"] == "block":
                    self.assertEqual(response["header"]["height"], root["block_height"])
                    self.assertEqual(response["header"]["hash"], root["block_hash"])
                else:
                    self.assertEqual(request["method"], "query")
                    self.assertEqual(request["params"]["request_type"], "call_function")
                    self.assertEqual(request["params"]["account_id"], root["contract"])
                    self.assertEqual(response["block_height"], root["block_height"])
                    self.assertEqual(response["block_hash"], root["block_hash"])
                    self.assertIsInstance(response["result"], list)
                    json.loads(bytes(response["result"]))
                    views += 1
        self.assertEqual(views, 40)

    def test_record_command_requires_explicit_live_and_new_directory(self):
        for args in (["--live"], ["--live", "--out", str(chainsig_record.RECORDINGS)]):
            with self.subTest(args=args), mock.patch.object(rpc, "Client") as factory:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    chainsig_record.main(args)
                self.assertEqual(error.exception.code, 2)
                factory.assert_not_called()


class DerivationBoundsTest(unittest.TestCase):
    def test_tweak_is_exact_sha3_of_utf8(self):
        for path in ("", "ethereum-1", "a,b", "a\0b", "\u03b1/\U0001f512"):
            with self.subTest(path=path):
                message = (chainsig.TWEAK_PREFIX + "alice.near," + path).encode("utf-8")
                self.assertEqual(chainsig.tweak("alice.near", path), hashlib.sha3_256(message).digest())
                self.assertNotEqual(chainsig.tweak("alice.near", path), keccak256(message))

    def test_invalid_predecessor_cannot_inject_separator(self):
        for account in ("alice.near,b", "UPPER.near", "a", "a" * 65, "", None, 5):
            with self.subTest(account=account), self.assertRaises(ValueError):
                chainsig.derive_public_key(GENERATOR, account, "path")

    def test_invalid_path_fails(self):
        for path in (None, 5, b"path", "\ud800"):
            with self.subTest(path=repr(path)), self.assertRaises(ValueError):
                chainsig.derive_public_key(GENERATOR, "alice.near", path)

    def test_unsupported_or_malformed_root_fails(self):
        for root in (None, "", "bls12381:111", "secp256k1:0", "secp256k1:",
                     "secp256k1:" + "1" * 63, "secp256k1:" + "1" * 65,
                     "secp256k1:" + "z" * 91, "ed25519:" + "1" * 31,
                     "ed25519:" + "1" * 33):
            with self.subTest(root=root), self.assertRaises(ValueError):
                chainsig.derive_public_key(root, "alice.near", "path")

    def test_secp_point_coordinates_and_curve_are_validated(self):
        for x, y in ((0, 0), (chainsig.SECP_P, 2), (2, chainsig.SECP_P)):
            root = "secp256k1:" + base58.encode(x.to_bytes(32, "big") + y.to_bytes(32, "big"))
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                chainsig.derive_public_key(root, "alice.near", "path")

    def test_ed_identity_torsion_and_noncanonical_points_fail(self):
        encodings = [bytes(32), (1).to_bytes(32, "little"),
                     (ed25519.q - 1).to_bytes(32, "little"), ed25519.q.to_bytes(32, "little"),
                     (1 | 1 << 255).to_bytes(32, "little")]
        for raw in encodings:
            root = base58.encode_key(raw)
            with self.subTest(raw=raw.hex()), self.assertRaises(ValueError):
                chainsig.derive_public_key(root, "alice.near", "path")

    def test_secp_tweak_is_big_endian_and_not_reduced(self):
        with mock.patch.object(chainsig, "tweak", return_value=(1).to_bytes(32, "big")):
            child = chainsig.derive_public_key(GENERATOR, "alice.near", "path")
        self.assertEqual(chainsig.secp256k1_bytes(child, compressed=True).hex(),
                         "02c6047f9441ed7d6d3045406e95c07cd85"
                         "c778e4b8cef3ca7abac09b95c709ee5")
        for scalar in (chainsig.SECP_N, 2**256 - 1):
            with self.subTest(scalar=scalar), mock.patch.object(chainsig, "tweak",
                                                              return_value=scalar.to_bytes(32, "big")):
                with self.assertRaises(ValueError):
                    chainsig.derive_public_key(GENERATOR, "alice.near", "path")

    def test_zero_tweak_returns_root(self):
        with mock.patch.object(chainsig, "tweak", return_value=bytes(32)):
            for root in (GENERATOR, ED_GENERATOR):
                with self.subTest(root=root):
                    self.assertEqual(chainsig.derive_public_key(root, "alice.near", "path"), root)

    def test_ed_tweak_is_little_endian_and_reduced(self):
        for scalar in (1, ed25519.l + 1):
            with self.subTest(scalar=scalar), mock.patch.object(chainsig, "tweak",
                                                              return_value=scalar.to_bytes(32, "little")):
                child = chainsig.derive_public_key(ED_GENERATOR, "alice.near", "path")
                self.assertEqual(child, base58.encode_key(ed25519.encodepoint(
                    ed25519.edwards(ed25519.B, ed25519.B))))

    def test_derived_infinity_or_identity_fails(self):
        x, y = chainsig.SECP_G
        negative_secp = "secp256k1:" + base58.encode(x.to_bytes(32, "big")
                                                    + (chainsig.SECP_P - y).to_bytes(32, "big"))
        negative_ed = base58.encode_key(ed25519.encodepoint([(-ed25519.B[0]) % ed25519.q,
                                                            ed25519.B[1]]))
        for root, order in ((negative_secp, "big"), (negative_ed, "little")):
            with self.subTest(root=root), mock.patch.object(chainsig, "tweak",
                                                            return_value=(1).to_bytes(32, order)):
                with self.assertRaises(ValueError):
                    chainsig.derive_public_key(root, "alice.near", "path")


class AddressEncodingTest(unittest.TestCase):
    def test_secp_sec1_encodings(self):
        self.assertEqual(chainsig.secp256k1_bytes(GENERATOR), b"\x04" + base58.decode(GENERATOR.split(":")[1]))
        self.assertEqual(chainsig.secp256k1_bytes(GENERATOR, compressed=True).hex(),
                         "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")

    def test_generator_ethereum_address(self):
        self.assertEqual(addresses.ethereum_address(GENERATOR), "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf")

    def test_bip173_generator_address(self):
        self.assertEqual(addresses.bitcoin_address(GENERATOR, network="mainnet"),
                         "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
        self.assertEqual(addresses.bitcoin_address(GENERATOR, network="testnet"),
                         "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx")

    def test_legacy_and_wrapped_generator_addresses(self):
        self.assertEqual(addresses.bitcoin_address(GENERATOR, network="mainnet", kind="p2pkh"),
                         "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH")
        self.assertEqual(addresses.bitcoin_address(GENERATOR, network="mainnet", kind="p2sh_p2wpkh"),
                         "3JvL6Ymt8MVWiCNHC7oWU6nLeHNJKLZGLN")

    def test_missing_ripemd160_names_openssl_provider_requirement(self):
        with mock.patch.object(addresses.hashlib, "new", side_effect=ValueError("unsupported hash type ripemd160")):
            with self.assertRaisesRegex(ValueError, "RIPEMD-160.*OpenSSL provider"):
                addresses.bitcoin_address(GENERATOR, network="mainnet")

    def test_unknown_bitcoin_network_and_kind_fail(self):
        for network in ("", "signet", "MAINNET", None):
            with self.subTest(network=network), self.assertRaises(ValueError):
                addresses.bitcoin_address(GENERATOR, network=network)
        for kind in ("p2tr", "p2wsh", "p2sh", "", None):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                addresses.bitcoin_address(GENERATOR, network="mainnet", kind=kind)

    def test_address_curve_mismatch_fails(self):
        for encoder, key in ((addresses.ethereum_address, ED_GENERATOR),
                             (addresses.solana_address, GENERATOR)):
            with self.subTest(encoder=encoder.__name__), self.assertRaises(ValueError):
                encoder(key)
        with self.assertRaises(ValueError):
            addresses.bitcoin_address(ED_GENERATOR, network="mainnet")

    def test_solana_is_the_ed25519_public_key_base58(self):
        self.assertEqual(addresses.solana_address(ED_GENERATOR), ED_GENERATOR.split(":")[1])

    def test_keccak_differs_from_sha3(self):
        self.assertEqual(keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertNotEqual(keccak256(b""), hashlib.sha3_256(b"").digest())
        with self.assertRaises(ValueError):
            keccak256("message")


class ViewClient:
    def __init__(self, domain_id=0, protocol="CaitSith", root=GENERATOR):
        self.domain_id = domain_id
        self.state = {"Running": {"domains": {"domains": [
            {"id": domain_id, "protocol": protocol, "purpose": "Sign"}]}}}
        self.root = root
        self.derived = None
        self.requests = []
        self.heights = [123, 123, 123]
        self.last_block_height = None
        self.last_source = None

    def call_function(self, contract, method, args, *, block_id=None):
        self.requests.append((contract, method, args, block_id))
        self.last_block_height = self.heights[len(self.requests) - 1]
        self.last_source = {"provider": "test", "fetched_at": "2026-11-21T00:00:00Z"}
        if method == "state":
            return copy.deepcopy(self.state)
        if method == "public_key":
            return self.root
        if method == "derived_public_key":
            return self.derived
        raise AssertionError(f"unexpected view method: {method}")


class SignerViewTest(unittest.TestCase):
    def test_real_rpc_client_decodes_contract_json_and_pins_requests(self):
        data = json.loads(REFERENCE.read_text(encoding="utf-8"))
        root = data["root_observations"][0]
        vector = next(row for row in data["vectors"] if row["name"] == "mainnet-unicode")
        values = [{"Running": {"domains": root["domains"]}}, root["public_keys"]["0"],
                  vector["secp256k1"]["public_key_naj"]]
        responses = [{"jsonrpc": "2.0", "id": "gate", "result": {
            "block_height": root["block_height"], "block_hash": root["block_hash"], "logs": [],
            "result": list(json.dumps(value, ensure_ascii=False).encode("utf-8"))}} for value in values]
        client = rpc.Client("https://rpc.example", mode="off")
        client._block_hashes[root["block_height"]] = root["block_hash"]
        with mock.patch.object(rpc.budget, "reserve", return_value=False):
            with mock.patch.object(rpc, "_fetch", side_effect=responses) as fetch:
                result = read_derivation(client, root["contract"], 0, vector["predecessor"], vector["path"])
        self.assertEqual(result["public_key"], vector["secp256k1"]["public_key_naj"])
        requests = [call.args[1] for call in fetch.call_args_list]
        self.assertEqual([request["method"] for request in requests], ["query"] * 3)
        self.assertEqual([request["params"]["method_name"] for request in requests],
                         ["state", "public_key", "derived_public_key"])
        self.assertEqual(requests[0]["params"]["finality"], "final")
        for request in requests[1:]:
            self.assertEqual(request["params"]["block_id"], root["block_height"])
            self.assertNotIn("finality", request["params"])
        self.assertEqual(json.loads(base64.b64decode(requests[2]["params"]["args_base64"])),
                         {"path": vector["path"], "predecessor": vector["predecessor"], "domain_id": 0})

    def test_read_root_and_derivation_at_one_block(self):
        client = ViewClient()
        client.derived = chainsig.derive_public_key(GENERATOR, "gate.testnet", "ethereum-1")
        result = read_derivation(client, "v1.signer-prod.testnet", 0, "gate.testnet", "ethereum-1")
        self.assertEqual(client.requests, [
            ("v1.signer-prod.testnet", "state", {}, None),
            ("v1.signer-prod.testnet", "public_key", {"domain_id": 0}, 123),
            ("v1.signer-prod.testnet", "derived_public_key",
             {"path": "ethereum-1", "predecessor": "gate.testnet", "domain_id": 0}, 123)])
        self.assertTrue(result["contract_cross_check"])
        self.assertEqual(result["public_key"], client.derived)
        self.assertEqual(result["root"]["block_height"], 123)
        self.assertEqual([row["method"] for row in result["root"]["sources"]],
                         ["state", "public_key", "derived_public_key"])

    def test_domain_id_selects_registry_entry_not_curve(self):
        for domain_id, protocol, root, curve in ((4, "CaitSith", GENERATOR, "secp256k1"),
                                                (5, "Frost", ED_GENERATOR, "ed25519")):
            with self.subTest(domain_id=domain_id):
                result = fetch_root(ViewClient(domain_id, protocol, root), "v1.signer", domain_id)
                self.assertEqual(result["domain_id"], domain_id)
                self.assertEqual(result["curve"], curve)
                self.assertEqual(result["public_key"], root)

    def test_domain_id_must_be_explicit_u64(self):
        for domain_id in (None, True, "0", -1, 2**64):
            client = ViewClient()
            with self.subTest(domain_id=domain_id), self.assertRaises(ValueError):
                fetch_root(client, "v1.signer", domain_id)
            self.assertEqual(client.requests, [])

    def test_missing_nonrunning_or_malformed_registry_fails(self):
        states = (None, [], {}, {"Resharing": {}}, {"Running": None}, {"Running": {}},
                  {"Running": {"domains": {}}}, {"Running": {"domains": {"domains": []}}},
                  {"Running": {"domains": {"domains": [None]}}},
                  {"Running": {"domains": {"domains": [{"id": True}]}}})
        for state in states:
            client = ViewClient()
            client.state = state
            with self.subTest(state=state), self.assertRaises(ValueError):
                fetch_root(client, "v1.signer", 0)
            self.assertEqual(len(client.requests), 1)

    def test_unknown_protocol_purpose_and_duplicate_ids_fail(self):
        for update in ({"purpose": "ForeignTx"}, {"purpose": "CKD"}, {"purpose": None},
                       {"protocol": "ConfidentialKeyDerivation"}, {"protocol": "unknown"},
                       {"protocol": None}, {"protocol": []}):
            client = ViewClient()
            client.state["Running"]["domains"]["domains"][0].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                fetch_root(client, "v1.signer", 0)
            self.assertEqual(len(client.requests), 1)
        client = ViewClient()
        client.state["Running"]["domains"]["domains"] *= 2
        with self.assertRaises(ValueError):
            fetch_root(client, "v1.signer", 0)

    def test_root_must_match_domain_curve(self):
        for protocol, root in (("CaitSith", ED_GENERATOR), ("Frost", GENERATOR), ("CaitSith", None)):
            with self.subTest(protocol=protocol, root=root), self.assertRaises(ValueError):
                fetch_root(ViewClient(protocol=protocol, root=root), "v1.signer", 0)

    def test_invalid_block_metadata_fails(self):
        for heights in ([None, 123, 123], [True, 123, 123], [-1, 123, 123],
                        [123, 124, 123], [123, None, 123], [123, 123, 124]):
            client = ViewClient()
            client.heights = heights
            client.derived = chainsig.derive_public_key(GENERATOR, "gate.near", "x")
            with self.subTest(heights=heights), self.assertRaises(ValueError):
                read_derivation(client, "v1.signer", 0, "gate.near", "x")

    def test_derived_mismatch_or_missing_key_fails(self):
        for reported in (None, "secp256k1:invalid", GENERATOR):
            client = ViewClient()
            client.derived = reported
            with self.subTest(reported=reported), self.assertRaises(ValueError):
                read_derivation(client, "v1.signer", 0, "gate.near", "x")

    def test_provider_error_propagates_without_root_fallback(self):
        client = ViewClient()
        client.call_function = mock.Mock(side_effect=OSError("offline"))
        with self.assertRaisesRegex(OSError, "offline"):
            fetch_root(client, "v1.signer", 0)
        self.assertEqual(client.call_function.call_count, 1)

    def test_invalid_inputs_fail_before_network_access(self):
        for predecessor, path in (("bad,account", "x"), ("gate.near", None), ("gate.near", "\ud800")):
            client = ViewClient()
            with self.subTest(predecessor=predecessor, path=repr(path)), self.assertRaises(ValueError):
                read_derivation(client, "v1.signer", 0, predecessor, path)
            self.assertEqual(client.requests, [])


class CommandTest(unittest.TestCase):
    def test_invalid_domain_id_is_a_parser_error_before_network_access(self):
        for domain_id in ("-1", str(2**64)):
            stderr = io.StringIO()
            with self.subTest(domain_id=domain_id), mock.patch.object(cli.rpc, "Client") as factory:
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                    cli.main(["--live", "--domain-id", domain_id, "--predecessor", "gate.near", "--path", "x"])
                self.assertEqual(error.exception.code, 2)
                self.assertIn("--domain-id must be a u64 integer", stderr.getvalue())
                factory.assert_not_called()

    def test_offline_command_refuses_network(self):
        for network in ("mainnet", "testnet"):
            stderr = io.StringIO()
            with self.subTest(network=network), mock.patch.object(cli.rpc, "Client") as factory:
                with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        cli.main(["--root", GENERATOR, "--network", network,
                                  "--predecessor", "gate.near", "--path", "x"])
                self.assertEqual(error.exception.code, 2)
                self.assertIn("--network requires --live", stderr.getvalue())
                factory.assert_not_called()

    def test_offline_command_never_creates_client(self):
        for root, chain in ((GENERATOR, "ethereum"), (ED_GENERATOR, "solana")):
            output = io.StringIO()
            with self.subTest(chain=chain), mock.patch.object(cli.rpc, "Client") as factory:
                with contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(["--root", root, "--predecessor", "gate.near", "--path", ""]), 0)
                factory.assert_not_called()
                result = json.loads(output.getvalue())
                self.assertFalse(result["contract_cross_check"])
                self.assertIn(chain, result["addresses"])

    def test_live_command_uses_only_pinned_views(self):
        client = ViewClient(5, "Frost", ED_GENERATOR)
        client.derived = chainsig.derive_public_key(ED_GENERATOR, "gate.testnet", "solana-1")
        output = io.StringIO()
        with mock.patch.object(cli.rpc, "Client", return_value=client) as factory:
            with contextlib.redirect_stdout(output):
                cli.main(["--live", "--domain-id", "5", "--predecessor", "gate.testnet", "--path", "solana-1"])
        factory.assert_called_once_with(cli.rpc.TESTNET_RPC_URL, mode="off")
        result = json.loads(output.getvalue())
        self.assertEqual(result["root"]["domain_id"], 5)
        self.assertTrue(result["contract_cross_check"])
        self.assertEqual([request[1] for request in client.requests], ["state", "public_key", "derived_public_key"])

    def test_live_requires_domain_and_offline_refuses_unchecked_domain(self):
        for args in (["--live"], ["--root", GENERATOR, "--domain-id", "0"],
                     ["--live", "--root", GENERATOR], []):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cli.main(args + ["--predecessor", "gate.near", "--path", "x"])
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
