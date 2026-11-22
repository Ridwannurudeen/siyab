import base64
import binascii
import copy
import hashlib
import json
import re
import unittest
from pathlib import Path

from gate import ed25519

ROOT = Path(__file__).resolve().parent.parent
MEASUREMENT_FIELDS = ("mrtd", "rtmr0", "rtmr1", "rtmr2", "key_provider_digest", "compose_hash")
VOTE_FIELDS = ("registry_account_id", "public_key", "measurement_id", "platforms", "verb", "approval_id",
               "wallet_pubkey", "request_hash", "recipient", "nonce", "evidence_hash", "expires_ms")
VIEW_FIELDS = ("public_key", "measurement_id", "measurement", "ppid", "aux", "tcb_status", "advisory_ids",
               "issued_at_ms", "registered_ms", "expires_ms", "collateral_expires_ms")
ACCOUNT_ID = re.compile(r"(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+", re.ASCII)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def fixed_hex(value, size):
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{size * 2}}}", value):
        raise ValueError(f"expected {size} bytes of lowercase hex")
    return bytes.fromhex(value)


def uint64(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("expected u64 integer")
    return value


def account(value):
    if not isinstance(value, str) or not 2 <= len(value) <= 64 or not ACCOUNT_ID.fullmatch(value):
        raise ValueError("expected NEAR account id")
    return value.encode("ascii")


def string_bytes(value):
    data = value.encode("utf-8")
    return len(data).to_bytes(4, "little") + data


def nonce_bytes(value):
    if not isinstance(value, str):
        raise ValueError("expected canonical base64 nonce")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("expected canonical base64 nonce") from error
    if len(data) != 32 or base64.b64encode(data).decode("ascii") != value:
        raise ValueError("expected canonical base64 nonce")
    return data


def platform_bytes(platforms):
    if not isinstance(platforms, list) or not 1 <= len(platforms) <= 8:
        raise ValueError("expected 1 to 8 platforms")
    values = [fixed_hex(value, 16) for value in platforms]
    if any(left >= right for left, right in zip(values, values[1:])):
        raise ValueError("platforms must be sorted and unique")
    return b"".join(values)


def report_data_preimage(registry_account_id, public_key, issued_at_ms, aux):
    fixed_hex(aux, 32)
    return (b"siyab-attest-v1\0" + account(registry_account_id) + b"\0" + fixed_hex(public_key, 32)
            + uint64(issued_at_ms).to_bytes(8, "big"))


def report_data(registry_account_id, public_key, issued_at_ms, aux):
    preimage = report_data_preimage(registry_account_id, public_key, issued_at_ms, aux)
    return hashlib.sha256(preimage).digest() + fixed_hex(aux, 32)


def measurement_preimage(measurement):
    if not isinstance(measurement, dict) or set(measurement) != set(MEASUREMENT_FIELDS):
        raise ValueError("expected exactly the six measurement fields")
    return b"siyab-measurement-v1" + b"".join(
        fixed_hex(measurement[field], 32 if field == "compose_hash" else 48)
        for field in MEASUREMENT_FIELDS
    )


def measurement_id(measurement):
    return hashlib.sha256(measurement_preimage(measurement)).hexdigest()


def scope_id(measurement_id, platforms):
    preimage = b"siyab-voice-v1" + fixed_hex(measurement_id, 32) + platform_bytes(platforms)
    return hashlib.sha256(preimage).hexdigest()


def vote_message(vote):
    if vote["verb"] not in ("approve", "reject"):
        raise ValueError("expected approve or reject")
    if not isinstance(vote["approval_id"], str) or not UUID.fullmatch(vote["approval_id"]):
        raise ValueError("expected lowercase canonical UUID")
    wallet = vote["wallet_pubkey"]
    if not isinstance(wallet, str) or not 1 <= len(wallet) <= 256 or not all("!" <= c <= "~" for c in wallet):
        raise ValueError("expected 1 to 256 printable nonspace ASCII wallet bytes")
    fixed_hex(vote["request_hash"], 32)
    return f"{vote['verb']}:{vote['approval_id']}:{wallet}:{vote['request_hash']}"


def vote_intent_preimage(vote):
    if not isinstance(vote, dict) or set(vote) != set(VOTE_FIELDS):
        raise ValueError("expected exactly the twelve vote intent fields")
    vote_message(vote)
    account(vote["registry_account_id"])
    account(vote["recipient"])
    platforms = platform_bytes(vote["platforms"])
    return (b"siyab-vote-v1|" + string_bytes(vote["registry_account_id"])
            + fixed_hex(vote["public_key"], 32) + fixed_hex(vote["measurement_id"], 32)
            + len(vote["platforms"]).to_bytes(4, "little") + platforms
            + string_bytes(vote["verb"]) + string_bytes(vote["approval_id"])
            + string_bytes(vote["wallet_pubkey"]) + fixed_hex(vote["request_hash"], 32)
            + string_bytes(vote["recipient"]) + nonce_bytes(vote["nonce"])
            + fixed_hex(vote["evidence_hash"], 32) + uint64(vote["expires_ms"]).to_bytes(8, "little"))


def nep413_preimage(vote):
    account(vote["recipient"])
    return ((2**31 + 413).to_bytes(4, "little") + string_bytes(vote_message(vote))
            + nonce_bytes(vote["nonce"]) + string_bytes(vote["recipient"]) + b"\0")


def validate_td_attributes(td_attributes):
    attributes = int.from_bytes(fixed_hex(td_attributes, 8), "little")
    if attributes & 1:
        raise ValueError("TD DEBUG is enabled")
    if attributes & 0x70:
        raise ValueError("TD profiling is enabled")
    if attributes & 0x3fffffff0780ff8e:
        raise ValueError("TD reserved bits are set")
    if attributes & (1 << 29):
        raise ValueError("TD migration is enabled")
    if attributes & (1 << 17):
        raise ValueError("TD SERVTD_EXT is enabled")
    if not attributes & (1 << 28):
        raise ValueError("TD SEPT_VE_DISABLE is not enabled")


def validate_quote_td_attributes(quote):
    if len(quote) < 48 or int.from_bytes(quote[4:8], "little") != 0x81:
        raise ValueError("expected TDX quote header")
    version = int.from_bytes(quote[:2], "little")
    if version == 4:
        offset = 168
    elif version == 5 and len(quote) >= 54 and quote[48:54] == b"\x02\0\x48\x02\0\0":
        offset = 174
    else:
        raise ValueError("expected version 4 or version 5 TD10 quote")
    if len(quote) < offset + 8:
        raise ValueError("truncated TD attributes")
    validate_td_attributes(quote[offset:offset + 8].hex())


def validate_key_view(view, max_lease_ms):
    if not isinstance(view, dict) or set(view) != set(VIEW_FIELDS):
        raise ValueError("expected exactly the eleven attested key fields")
    for field in ("public_key", "measurement_id", "aux"):
        fixed_hex(view[field], 32)
    fixed_hex(view["ppid"], 16)
    if view["measurement_id"] != measurement_id(view["measurement"]):
        raise ValueError("measurement_id mismatch")
    if view["tcb_status"] != "UpToDate":
        raise ValueError("expected UpToDate")
    advisories = view["advisory_ids"]
    if not isinstance(advisories, list) or not all(isinstance(value, str) for value in advisories):
        raise ValueError("expected advisory string array")
    for field in ("issued_at_ms", "registered_ms", "expires_ms", "collateral_expires_ms"):
        uint64(view[field])
    lease_end_ms = uint64(view["issued_at_ms"] + uint64(max_lease_ms))
    if view["expires_ms"] != min(lease_end_ms, view["collateral_expires_ms"]):
        raise ValueError("lease must start at issue time")
    if not view["registered_ms"] < view["expires_ms"] <= view["collateral_expires_ms"]:
        raise ValueError("invalid lease ordering")
    if not max(0, view["registered_ms"] - 7200000) <= view["issued_at_ms"] <= view["registered_ms"]:
        raise ValueError("issue time outside inclusive two hour window")
    return view


def registration_ms(block_timestamp_ns):
    return uint64(block_timestamp_ns) // 1000000


def registration_callback(keys, candidate, max_lease_ms):
    validate_key_view(candidate, max_lease_ms)
    previous = keys.get(candidate["public_key"])
    if previous is not None:
        if candidate["issued_at_ms"] <= previous["issued_at_ms"]:
            raise ValueError("issue time must be strictly greater")
        if any(candidate[field] != previous[field] for field in ("measurement_id", "ppid", "aux")):
            raise ValueError("registered key identity cannot change")
    keys[candidate["public_key"]] = copy.deepcopy(candidate)


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {"verifier_account_id", "signer_account_id",
                                                     "ed25519_domain_id", "max_lease_ms", "vote_recipients"}:
        raise ValueError("expected exactly the five immutable config fields")
    account(config["verifier_account_id"])
    account(config["signer_account_id"])
    uint64(config["ed25519_domain_id"])
    if uint64(config["max_lease_ms"]) == 0:
        raise ValueError("max_lease_ms must be positive")
    recipients = config["vote_recipients"]
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 8:
        raise ValueError("expected 1 to 8 vote recipients")
    values = [account(recipient) for recipient in recipients]
    if any(left >= right for left, right in zip(values, values[1:])):
        raise ValueError("vote recipients must be sorted and unique")
    return config


def verify_vote_signature(intent, signature, registry_account_id):
    vote_intent_preimage(intent)
    digest = hashlib.sha256(vote_intent_preimage({**intent, "registry_account_id": registry_account_id})).digest()
    public_key = fixed_hex(intent["public_key"], 32)
    signature = fixed_hex(signature, 64)
    try:
        small_order = any(ed25519.scalarmult(ed25519.decodepoint(point), 8) == [0, 1]
                          for point in (public_key, signature[:32]))
    except ValueError as error:
        raise ValueError("invalid vote signature") from error
    if small_order or not ed25519.verify(public_key, digest, signature):
        raise ValueError("invalid vote signature")
    if intent["registry_account_id"] != registry_account_id:
        raise ValueError("wrong registry")


def submit_vote_reference(intent, signature, config, registry_account_id, keys, reservations, now_ms):
    verify_vote_signature(intent, signature, registry_account_id)
    validate_config(config)
    if intent["recipient"] not in config["vote_recipients"]:
        raise ValueError("vote recipient is not allowed")
    key = keys.get(intent["public_key"])
    if key is None:
        raise ValueError("unknown key")
    validate_key_view(key, config["max_lease_ms"])
    if intent["measurement_id"] != key["measurement_id"] or key["ppid"] not in intent["platforms"]:
        raise ValueError("key identity outside vote scope")
    validate_vote_expiry(now_ms, key["registered_ms"], key["expires_ms"], intent["expires_ms"])
    vote_id = (scope_id(intent["measurement_id"], intent["platforms"]), intent["approval_id"],
               intent["wallet_pubkey"], fixed_hex(intent["request_hash"], 32))
    previous = reservations.get(vote_id)
    if previous is not None and previous != intent["verb"]:
        raise ValueError("opposite verb already reserved")
    reservations[vote_id] = intent["verb"]
    return vote_id


def validate_vote_expiry(now_ms, registered_ms, key_expires_ms, intent_expires_ms):
    for value in (now_ms, registered_ms, key_expires_ms, intent_expires_ms):
        uint64(value)
    if not registered_ms <= now_ms < intent_expires_ms <= key_expires_ms:
        raise ValueError("vote or key outside live interval")


class AttestVectorsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vectors = json.loads((ROOT / "fixtures" / "attest" / "vectors.json").read_text(encoding="utf-8"))

    def test_fixture_format_and_coverage(self):
        self.assertEqual((self.vectors["format"], self.vectors["version"]), ("siyab-attest-vectors", 1))
        for group, count in (("report_data", 3), ("measurements", 2), ("scopes", 3), ("votes", 3), ("attested_keys", 1),
                             ("td_attributes", 66), ("key_issue_times", 9), ("get_key_args", 1),
                             ("registration_args", 2), ("vote_submission_args", 3), ("signing_requests", 3),
                             ("registration_clock", 7), ("vote_expiry", 7)):
            self.assertEqual(len(self.vectors[group]), count)
            self.assertEqual(len({vector["name"] for vector in self.vectors[group]}), count)

        for group, count in (("registry_config", 1), ("reregistration", 6), ("quote_td_attributes", 6),
                             ("vote_policy", 5), ("call_wire", 4), ("vote_reservations", 5)):
            self.assertEqual(len(self.vectors[group]), count)
            self.assertEqual(len({vector["name"] for vector in self.vectors[group]}), count)

    def test_registration_callback_vectors(self):
        vector = self.vectors["attested_keys"][0]
        original = vector["view"]
        for case in self.vectors["reregistration"]:
            with self.subTest(name=case["name"]):
                keys = {original["public_key"]: copy.deepcopy(original)}
                candidate = {**original, **case["candidate_changes"]}
                validate_key_view(candidate, vector["max_lease_ms"])
                if case["accepted"]:
                    registration_callback(keys, candidate, vector["max_lease_ms"])
                    self.assertEqual(keys[original["public_key"]], candidate)
                else:
                    with self.assertRaises(ValueError):
                        registration_callback(keys, candidate, vector["max_lease_ms"])
                    self.assertEqual(keys[original["public_key"]], original)

    def test_registration_callbacks_compare_latest_state_in_both_orders(self):
        vector = self.vectors["attested_keys"][0]
        original = vector["view"]
        candidates = [{**original, "issued_at_ms": original["issued_at_ms"] + index,
                       "expires_ms": original["expires_ms"] + index} for index in (1, 2)]
        for order in (candidates, candidates[::-1]):
            with self.subTest(order=[value["issued_at_ms"] for value in order]):
                keys = {original["public_key"]: copy.deepcopy(original)}
                registration_callback(keys, order[0], vector["max_lease_ms"])
                if order[1]["issued_at_ms"] > order[0]["issued_at_ms"]:
                    registration_callback(keys, order[1], vector["max_lease_ms"])
                else:
                    with self.assertRaisesRegex(ValueError, "strictly greater"):
                        registration_callback(keys, order[1], vector["max_lease_ms"])
                self.assertEqual(keys[original["public_key"]], candidates[1])

    def test_registry_config_vectors_and_bounds(self):
        for vector in self.vectors["registry_config"]:
            self.assertEqual(validate_config(vector["new_arguments"]), vector["get_config_result"])
            self.assertEqual(vector["get_config_arguments"], {})
        original = self.vectors["registry_config"][0]["new_arguments"]
        for field, value in (("max_lease_ms", 0), ("max_lease_ms", True), ("ed25519_domain_id", -1),
                             ("ed25519_domain_id", 2**64), ("verifier_account_id", "a..near"),
                             ("signer_account_id", "A.near"), ("vote_recipients", []),
                             ("vote_recipients", ["outlayer.near"] * 2),
                             ("vote_recipients", ["z.near", "a.near"]),
                             ("vote_recipients", [f"r{index}.near" for index in range(9)]),
                             ("vote_recipients", ["a..near"]), ("owner", "owner.near")):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    validate_config({**original, field: value})

    def test_quote_attributes_vectors_and_truncation(self):
        for vector in self.vectors["quote_td_attributes"]:
            quote = bytes.fromhex(vector["quote_prefix_hex"])
            with self.subTest(name=vector["name"]):
                if vector["accepted"]:
                    validate_quote_td_attributes(quote)
                else:
                    with self.assertRaises(ValueError):
                        validate_quote_td_attributes(quote)
                for length in (0, 1, 47, len(quote) - 1):
                    with self.assertRaises(ValueError):
                        validate_quote_td_attributes(quote[:length])

    def test_vote_policy_vectors_and_permanent_reservations(self):
        config = {**self.vectors["registry_config"][0]["new_arguments"],
                  "vote_recipients": ["outlayer.near", "outlayer.testnet"]}
        original = self.vectors["attested_keys"][0]["view"]
        for vector in self.vectors["vote_policy"]:
            with self.subTest(name=vector["name"]):
                intent = vector["intent"]
                key = {**original, "public_key": intent["public_key"]}
                scope = scope_id(intent["measurement_id"], intent["platforms"])
                self.assertEqual(scope, vector["scope_id"])
                vote_id = (scope, intent["approval_id"], intent["wallet_pubkey"],
                           fixed_hex(intent["request_hash"], 32))
                reservations = {vote_id: verb for verb in vector["previous_verbs"]}
                before = copy.deepcopy(reservations)
                args = (intent, vector["signature"], config, intent["registry_account_id"],
                        {key["public_key"]: key}, reservations, vector["now_ms"])
                if vector["accepted"]:
                    submit_vote_reference(*args)
                    self.assertEqual(reservations[vote_id], intent["verb"])
                    # Callback failure or expiry does not clear an exposed signature's reservation.
                    other = {**intent, "verb": "reject" if intent["verb"] == "approve" else "approve",
                             "recipient": "outlayer.testnet", "nonce": base64.b64encode(b"b" * 32).decode("ascii"),
                             "evidence_hash": "ab" * 32, "expires_ms": intent["expires_ms"] + 1}
                    signature = ed25519.sign(ed25519.TEST_AGENT_SEED,
                                             hashlib.sha256(vote_intent_preimage(other)).digest()).hex()
                    with self.assertRaisesRegex(ValueError, "opposite verb"):
                        submit_vote_reference(other, signature, *args[2:])
                    self.assertEqual(reservations[vote_id], intent["verb"])
                else:
                    with self.assertRaisesRegex(ValueError, vector["error"]):
                        submit_vote_reference(*args)
                    self.assertEqual(reservations, before)

    def test_vote_reservation_scope_vectors(self):
        config = self.vectors["registry_config"][0]["new_arguments"]
        original = self.vectors["attested_keys"][0]["view"]
        for vector in self.vectors["vote_reservations"]:
            with self.subTest(name=vector["name"]):
                reservations = {}
                scopes = []
                for step in vector["steps"]:
                    intent = step["intent"]
                    key = {**original, **step["key_changes"]}
                    validate_key_view(key, config["max_lease_ms"])
                    verify_vote_signature(intent, step["signature"], intent["registry_account_id"])
                    scope = scope_id(intent["measurement_id"], intent["platforms"])
                    self.assertEqual(scope, step["scope_id"])
                    scopes.append(scope)
                    before = copy.deepcopy(reservations)
                    args = (intent, step["signature"], config, intent["registry_account_id"],
                            {key["public_key"]: key}, reservations, vector["now_ms"])
                    if step["accepted"]:
                        submit_vote_reference(*args)
                        self.assertEqual(len(reservations), len(set(scopes)))
                    else:
                        with self.assertRaisesRegex(ValueError, "opposite verb"):
                            submit_vote_reference(*args)
                        self.assertEqual(reservations, before)
                self.assertEqual(scopes[0] == scopes[1], vector["same_scope"])
                first, second = (step["intent"] for step in vector["steps"])
                self.assertNotEqual(first["public_key"], second["public_key"])
                for field in ("approval_id", "wallet_pubkey", "request_hash"):
                    self.assertEqual(first[field], second[field])
                if not vector["same_scope"]:
                    self.assertEqual((first["verb"], second["verb"]), ("approve", "reject"))

    def test_wire_result_vectors(self):
        for vector in self.vectors["call_wire"]:
            self.assertEqual(vector["deposit_yocto"], "10000000000000000000000")
            self.assertEqual((vector["gas_tgas"], vector["callee_gas_tgas"], vector["callback_gas_tgas"]),
                             (300, 200, 60))
            result = vector["result"]
            if result is None:
                continue
            if vector["method"] == "register_key":
                validate_key_view(result, 86400000)
            else:
                self.assertEqual(set(result), {"vote_path", "nep413_digest", "signature"})
                self.assertEqual(result["vote_path"][:5], "vote/")
                fixed_hex(result["vote_path"][5:], 32)
                fixed_hex(result["nep413_digest"], 32)
                fixed_hex(result["signature"], 64)

    def test_registration_callback_refuses_older_live_quote(self):
        vector = self.vectors["attested_keys"][0]
        older = copy.deepcopy(vector["view"])
        newer = {**older, "issued_at_ms": older["issued_at_ms"] + 1,
                 "expires_ms": older["expires_ms"] + 1}
        keys = {}
        registration_callback(keys, newer, vector["max_lease_ms"])
        with self.assertRaisesRegex(ValueError, "strictly greater"):
            registration_callback(keys, older, vector["max_lease_ms"])
        self.assertEqual(keys, {newer["public_key"]: newer})

    def test_quote_v5_attributes_offset(self):
        quote = bytearray(182)
        quote[:2] = (5).to_bytes(2, "little")
        quote[4:8] = (0x81).to_bytes(4, "little")
        quote[48:50] = (2).to_bytes(2, "little")
        quote[50:54] = (584).to_bytes(4, "little")
        quote[174:182] = (1 << 28).to_bytes(8, "little")
        validate_quote_td_attributes(bytes(quote))
        quote[174] |= 1
        with self.assertRaisesRegex(ValueError, "DEBUG"):
            validate_quote_td_attributes(bytes(quote))

    def test_vote_signature_precedes_policy_and_mutation(self):
        intent = {**self.vectors["votes"][0]["input"],
                  "public_key": ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex()}
        signature = ed25519.sign(ed25519.TEST_AGENT_SEED,
                                 hashlib.sha256(vote_intent_preimage(intent)).digest()).hex()
        reservations = {}
        with self.assertRaisesRegex(ValueError, "invalid vote signature"):
            submit_vote_reference({**intent, "verb": "reject"}, signature, {}, intent["registry_account_id"], {},
                                  reservations, 0)
        self.assertEqual(reservations, {})

    def test_vote_authentication_binds_all_fields_before_policy(self):
        vector = self.vectors["vote_policy"][0]
        original = vector["intent"]
        replacements = {"registry_account_id": "different.near", "public_key": "ff" * 32,
                        "measurement_id": "ac" * 32, "platforms": ["11" * 16], "verb": "reject",
                        "approval_id": "00000000-0000-0000-0000-000000000002", "wallet_pubkey": "other-key",
                        "request_hash": "ae" * 32, "recipient": "different.near",
                        "nonce": base64.b64encode(b"b" * 32).decode("ascii"), "evidence_hash": "af" * 32,
                        "expires_ms": original["expires_ms"] + 1}
        for field, value in replacements.items():
            with self.subTest(field=field):
                reservations = {}
                changed = {**original, field: value}
                expected = "wrong registry" if field == "registry_account_id" else "invalid vote signature"
                with self.assertRaisesRegex(ValueError, expected):
                    submit_vote_reference(changed, vector["signature"], {}, original["registry_account_id"],
                                          {}, reservations, 0)
                self.assertEqual(reservations, {})
        with self.assertRaisesRegex(ValueError, "invalid vote signature"):
            verify_vote_signature(original, "00" * 64, original["registry_account_id"])
        identity = "01" + "00" * 31
        with self.assertRaisesRegex(ValueError, "invalid vote signature"):
            verify_vote_signature({**original, "public_key": identity}, identity + "00" * 32,
                                  original["registry_account_id"])

    def test_vote_signed_for_another_registry_refuses_before_policy(self):
        vector = self.vectors["vote_policy"][0]
        intent = {**vector["intent"], "registry_account_id": "other.near"}
        signature = ed25519.sign(ed25519.TEST_AGENT_SEED,
                                 hashlib.sha256(vote_intent_preimage(intent)).digest()).hex()
        reservations = {}
        with self.assertRaisesRegex(ValueError, "invalid vote signature"):
            submit_vote_reference(intent, signature, {}, vector["intent"]["registry_account_id"],
                                  {}, reservations, 0)
        self.assertEqual(reservations, {})

    def test_report_data_vectors(self):
        for vector in self.vectors["report_data"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(report_data_preimage(**vector["input"]).hex(), vector["preimage_hex"])
                self.assertEqual(report_data(**vector["input"]).hex(), vector["report_data_hex"])

    def test_measurement_vectors(self):
        for vector in self.vectors["measurements"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(measurement_preimage(vector["input"]).hex(), vector["preimage_hex"])
                self.assertEqual(measurement_id(vector["input"]), vector["measurement_id"])

    def test_scope_vectors(self):
        for vector in self.vectors["scopes"]:
            with self.subTest(name=vector["name"]):
                value = scope_id(**vector["input"])
                self.assertEqual(value, vector["scope_id"])
                self.assertEqual("vote/" + value, vector["vote_path"])

    def test_vote_vectors(self):
        for vector in self.vectors["votes"]:
            with self.subTest(name=vector["name"]):
                vote = vector["input"]
                intent = vote_intent_preimage(vote)
                nep413 = nep413_preimage(vote)
                self.assertEqual(intent.hex(), vector["intent_preimage_hex"])
                self.assertEqual(hashlib.sha256(intent).hexdigest(), vector["intent_digest"])
                self.assertEqual(vote_message(vote), vector["nep413_message"])
                self.assertEqual(nep413.hex(), vector["nep413_preimage_hex"])
                self.assertEqual(hashlib.sha256(nep413).hexdigest(), vector["nep413_digest"])

    def test_attested_key_view_vectors(self):
        for vector in self.vectors["attested_keys"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(validate_key_view(vector["view"], vector["max_lease_ms"]), vector["view"])

    def test_td_attributes_vectors(self):
        for vector in self.vectors["td_attributes"]:
            with self.subTest(name=vector["name"]):
                if vector["accepted"]:
                    validate_td_attributes(vector["td_attributes"])
                else:
                    with self.assertRaises(ValueError):
                        validate_td_attributes(vector["td_attributes"])

    def test_attested_key_issue_time_vectors(self):
        original = self.vectors["attested_keys"][0]["view"]
        for vector in self.vectors["key_issue_times"]:
            with self.subTest(name=vector["name"]):
                view = {**original, **vector["times"]}
                if vector["accepted"]:
                    self.assertEqual(validate_key_view(view, vector["max_lease_ms"]), view)
                else:
                    with self.assertRaises(ValueError):
                        validate_key_view(view, vector["max_lease_ms"])

    def test_get_key_argument_vectors(self):
        for vector in self.vectors["get_key_args"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(vector["method"], "get_key")
                public_key = fixed_hex(vector["public_key"], 32)
                arguments = json.dumps({"public_key": public_key.hex()}, separators=(",", ":")).encode("utf-8")
                self.assertEqual(arguments.hex(), vector["arguments_json_hex"])

    def test_registration_argument_vectors(self):
        for vector in self.vectors["registration_args"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(vector["method"], "register_key")
                arguments = vector["arguments"]
                self.assertEqual(set(arguments), {"public_key", "issued_at_ms", "aux", "verification_args",
                                                  "app_compose", "event_log"})
                fixed_hex(arguments["public_key"], 32)
                fixed_hex(arguments["aux"], 32)
                uint64(arguments["issued_at_ms"])
                quote = bytes.fromhex(vector["verification_input"]["quote_hex"])
                encoded = len(quote).to_bytes(4, "little") + quote
                collateral = vector["verification_input"]["collateral"]
                for field in ("pck_crl_issuer_chain", "root_ca_crl", "pck_crl", "tcb_info_issuer_chain",
                              "tcb_info", "tcb_info_signature", "qe_identity_issuer_chain", "qe_identity",
                              "qe_identity_signature"):
                    value = collateral[field]
                    if isinstance(value, str):
                        encoded += string_bytes(value)
                    else:
                        encoded += len(value).to_bytes(4, "little") + bytes(value)
                chain = collateral["pck_certificate_chain"]
                encoded += b"\0" if chain is None else b"\1" + string_bytes(chain)
                self.assertEqual(encoded.hex(), arguments["verification_args"])
                compose_hash = hashlib.sha256(arguments["app_compose"].encode("utf-8")).hexdigest()
                self.assertEqual(arguments["event_log"][0]["event_payload"], compose_hash)
                for event in arguments["event_log"]:
                    self.assertEqual(set(event), {"imr", "event_type", "digest", "event", "event_payload"})
                    digest = hashlib.sha384(event["event_type"].to_bytes(4, "little") + b":"
                                            + event["event"].encode("utf-8") + b":"
                                            + bytes.fromhex(event["event_payload"])).digest()
                    self.assertEqual(digest, fixed_hex(event["digest"], 48))

    def test_vote_submission_argument_vectors(self):
        votes = {vector["name"]: vector["input"] for vector in self.vectors["votes"]}
        for vector in self.vectors["vote_submission_args"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(vector["method"], "submit_vote")
                arguments = vector["arguments"]
                self.assertEqual(set(arguments), {"intent", "signature"})
                self.assertEqual(arguments["intent"], votes[vector["name"]])
                vote_intent_preimage(arguments["intent"])
                self.assertEqual(fixed_hex(arguments["signature"], 64), bytes(range(64)))

    def test_signing_request_vectors(self):
        votes = {vector["name"]: vector["input"] for vector in self.vectors["votes"]}
        for vector in self.vectors["signing_requests"]:
            with self.subTest(name=vector["name"]):
                vote = votes[vector["name"]]
                path = "vote/" + scope_id(vote["measurement_id"], vote["platforms"])
                digest = hashlib.sha256(nep413_preimage(vote)).hexdigest()
                self.assertEqual(vector["method"], "sign")
                self.assertEqual(vector["predecessor"], vote["registry_account_id"])
                self.assertEqual(vector["path_utf8_hex"], path.encode("ascii").hex())
                self.assertEqual(vector["arguments"], {"request": {"path": path, "payload_v2": {"Eddsa": digest},
                                                                 "domain_id": vector["ed25519_domain_id"]}})

    def test_registration_clock_vectors(self):
        for vector in self.vectors["registration_clock"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(registration_ms(vector["block_timestamp_ns"]), vector["registered_ms"])
        for value in (-1, 2**64, True, "1", 1.0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    registration_ms(value)

    def test_vote_expiry_vectors(self):
        for vector in self.vectors["vote_expiry"]:
            with self.subTest(name=vector["name"]):
                if vector["accepted"]:
                    validate_vote_expiry(**vector["input"])
                else:
                    with self.assertRaises(ValueError):
                        validate_vote_expiry(**vector["input"])
        original = self.vectors["vote_expiry"][0]["input"]
        for field in original:
            for value in (-1, 2**64, True, "1", 1.0):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        validate_vote_expiry(**{**original, field: value})

    def test_td_attributes_reject_debug_quote(self):
        source = ROOT / "contracts" / "attest" / "tests" / "vendor" / "upstream" / "quote.json"
        quote = bytearray(json.loads(source.read_text(encoding="utf-8")))
        self.assertEqual(int.from_bytes(quote[:2], "little"), 4)
        self.assertEqual(int.from_bytes(quote[4:8], "little"), 0x81)
        offset = 48 + 16 + 48 + 48 + 8
        validate_td_attributes(quote[offset:offset + 8].hex())
        quote[offset] |= 1
        with self.assertRaisesRegex(ValueError, "DEBUG is enabled"):
            validate_td_attributes(quote[offset:offset + 8].hex())

    def test_td_attributes_reject_profiling_and_migration(self):
        for bit, reason in ((4, "profiling"), (5, "profiling"), (6, "profiling"), (29, "migration")):
            with self.subTest(bit=bit):
                with self.assertRaisesRegex(ValueError, reason):
                    validate_td_attributes((1 << bit).to_bytes(8, "little").hex())

    def test_td_attributes_have_exact_width(self):
        for value in ("00" * 7, "00" * 9, "AB" * 8, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_td_attributes(value)

    def test_attested_key_lease_cannot_be_extended_by_registration(self):
        vector = self.vectors["attested_keys"][0]
        original = vector["view"]
        max_lease_ms = 86400000
        self.assertEqual(original["expires_ms"], original["issued_at_ms"] + max_lease_ms)
        delayed = {**original, "registered_ms": original["registered_ms"] + 1000}
        self.assertEqual(validate_key_view(delayed, max_lease_ms), delayed)
        for expires_ms in (original["expires_ms"] + 1000, delayed["registered_ms"] + max_lease_ms):
            with self.subTest(expires_ms=expires_ms):
                with self.assertRaisesRegex(ValueError, "issue time"):
                    validate_key_view({**delayed, "expires_ms": expires_ms}, max_lease_ms)

    def test_attested_key_lease_is_capped_and_refuses_expired_relay(self):
        original = self.vectors["attested_keys"][0]["view"]
        expiry = original["registered_ms"] + 1
        capped = {**original, "expires_ms": expiry, "collateral_expires_ms": expiry}
        self.assertEqual(validate_key_view(capped, 86400000), capped)
        with self.assertRaisesRegex(ValueError, "lease ordering"):
            validate_key_view({**capped, "registered_ms": expiry}, 86400000)
        with self.assertRaisesRegex(ValueError, "lease ordering"):
            validate_key_view({**original, "registered_ms": original["expires_ms"]}, 86400000)

    def test_attested_key_lease_refuses_u64_overflow(self):
        original = self.vectors["attested_keys"][0]["view"]
        with self.assertRaisesRegex(ValueError, "u64"):
            validate_key_view({**original, "issued_at_ms": 2**64 - 1}, 1)
        for max_lease_ms in (-1, 2**64, True, "1"):
            with self.subTest(max_lease_ms=max_lease_ms):
                with self.assertRaisesRegex(ValueError, "u64"):
                    validate_key_view(original, max_lease_ms)

    def test_report_data_binds_every_input(self):
        original = self.vectors["report_data"][0]["input"]
        baseline = report_data(**original)
        for field, value in (("registry_account_id", "different.near"), ("public_key", "ab" * 32),
                             ("issued_at_ms", original["issued_at_ms"] + 1), ("aux", "ab" * 32)):
            with self.subTest(field=field):
                changed = {**original, field: value}
                self.assertNotEqual(report_data(**changed), baseline)
        preimage = report_data_preimage(**original)
        wrong_order = (b"siyab-attest-v1\0" + account(original["registry_account_id"]) + b"\0"
                       + original["issued_at_ms"].to_bytes(8, "big") + bytes.fromhex(original["public_key"]))
        self.assertNotEqual(hashlib.sha256(wrong_order).digest(), baseline[:32])
        self.assertEqual(preimage[-8:], original["issued_at_ms"].to_bytes(8, "big"))

    def test_report_data_has_strict_widths(self):
        original = self.vectors["report_data"][0]["input"]
        for field in ("public_key", "aux"):
            for value in ("ab" * 31, "ab" * 33, "AB" * 32, "ag" * 32, None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        report_data(**{**original, field: value})

    def test_timestamps_are_u64_integers(self):
        original = self.vectors["report_data"][0]["input"]
        for value in (-1, 2**64, True, 1.0, "1", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    report_data(**{**original, "issued_at_ms": value})

    def test_registry_and_recipient_are_account_ids(self):
        original = self.vectors["report_data"][0]["input"]
        for value in ("a", "A.near", "a..near", "a.near\0other", "a" * 65):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    report_data(**{**original, "registry_account_id": value})

    def test_measurement_field_order_is_fixed(self):
        original = self.vectors["measurements"][1]["input"]
        reordered = dict(reversed(list(original.items())))
        self.assertEqual(measurement_id(reordered), measurement_id(original))
        changed = {**original, "mrtd": original["rtmr0"], "rtmr0": original["mrtd"]}
        self.assertNotEqual(measurement_id(changed), measurement_id(original))

    def test_measurements_have_exact_fields_and_widths(self):
        original = self.vectors["measurements"][1]["input"]
        for field in MEASUREMENT_FIELDS:
            size = 32 if field == "compose_hash" else 48
            for value in ("ab" * (size - 1), "ab" * (size + 1), "AB" * size):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        measurement_id({**original, field: value})
        with self.assertRaises(ValueError):
            measurement_id({**original, "rtmr3": "ab" * 48})
        with self.assertRaises(ValueError):
            measurement_id({field: value for field, value in original.items() if field != "mrtd"})

    def test_platforms_are_bounded_sorted_and_unique(self):
        measurement = self.vectors["measurements"][1]["measurement_id"]
        valid = self.vectors["scopes"][1]["input"]["platforms"]
        for values in ([], valid[::-1], [valid[0], valid[0]], ["ab" * 15], ["ab" * 17],
                       ["AB" * 16], [f"{index:032x}" for index in range(9)], tuple(valid)):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    scope_id(measurement, values)

    def test_scope_binds_measurement_and_platform_set(self):
        vector = self.vectors["scopes"][1]["input"]
        baseline = scope_id(**vector)
        self.assertNotEqual(scope_id("ab" * 32, vector["platforms"]), baseline)
        self.assertNotEqual(scope_id(vector["measurement_id"], vector["platforms"][:1]), baseline)

    def test_vote_intent_binds_every_field(self):
        original = self.vectors["votes"][0]["input"]
        replacements = {
            "registry_account_id": "different.near", "public_key": "ab" * 32,
            "measurement_id": "ac" * 32, "platforms": ["ad" * 16], "verb": "reject",
            "approval_id": "00000000-0000-0000-0000-000000000002", "wallet_pubkey": "different-key",
            "request_hash": "ae" * 32, "recipient": "different.near",
            "nonce": base64.b64encode(b"b" * 32).decode("ascii"), "evidence_hash": "af" * 32,
            "expires_ms": original["expires_ms"] + 1,
        }
        baseline = hashlib.sha256(vote_intent_preimage(original)).digest()
        for field, value in replacements.items():
            with self.subTest(field=field):
                self.assertNotEqual(hashlib.sha256(vote_intent_preimage({**original, field: value})).digest(), baseline)

    def test_vote_intent_field_order_is_fixed(self):
        original = self.vectors["votes"][0]["input"]
        self.assertEqual(vote_intent_preimage(dict(reversed(list(original.items())))), vote_intent_preimage(original))
        changed = {**original, "request_hash": original["evidence_hash"], "evidence_hash": original["request_hash"]}
        self.assertNotEqual(vote_intent_preimage(changed), vote_intent_preimage(original))

    def test_vote_intent_rejects_missing_or_extra_fields(self):
        original = self.vectors["votes"][0]["input"]
        for field in VOTE_FIELDS:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    vote_intent_preimage({key: value for key, value in original.items() if key != field})
        with self.assertRaises(ValueError):
            vote_intent_preimage({**original, "callback_url": "https://example.com"})

    def test_vote_intent_rejects_noncanonical_fields(self):
        original = self.vectors["votes"][0]["input"]
        invalid = {
            "verb": ("ASK", "abstain", "APPROVE", "approve:reject"),
            "approval_id": ("1", "A0000000-0000-0000-0000-000000000001", "1:2"),
            "wallet_pubkey": ("", "x" * 257, "key\n", "key with space", "\u00e9"),
            "nonce": (base64.b64encode(b"a" * 31).decode("ascii"), base64.b64encode(b"a" * 33).decode("ascii"),
                      original["nonce"].rstrip("="), original["nonce"] + "\n", "?" * 44),
            "recipient": ("A.near", "a..near", "a"), "expires_ms": (True, -1, 2**64, "1"),
        }
        for field in ("public_key", "measurement_id", "request_hash", "evidence_hash"):
            invalid[field] = ("ab" * 31, "ab" * 33, "AB" * 32)
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        vote_intent_preimage({**original, field: value})

    def test_nonce_rejects_nonzero_padding_bits(self):
        nonce = base64.b64encode(bytes(32)).decode("ascii")
        noncanonical = nonce[:-2] + "B="
        self.assertEqual(base64.b64decode(noncanonical), bytes(32))
        with self.assertRaises(ValueError):
            nonce_bytes(noncanonical)

    def test_nep413_has_tag_fixed_nonce_and_no_callback(self):
        vote = self.vectors["votes"][0]["input"]
        preimage = nep413_preimage(vote)
        self.assertEqual(preimage[:4], b"\x9d\x01\x00\x80")
        self.assertEqual(preimage[-1:], b"\0")
        self.assertNotEqual(hashlib.sha256(preimage).digest(), hashlib.sha256(vote_intent_preimage(vote)).digest())
        for field, value in (("recipient", "different.near"), ("nonce", base64.b64encode(b"c" * 32).decode("ascii"))):
            with self.subTest(field=field):
                self.assertNotEqual(nep413_preimage({**vote, field: value}), preimage)

    def test_attested_key_view_rejects_bad_encodings_and_status(self):
        vector = self.vectors["attested_keys"][0]
        original = vector["view"]
        invalid = {"public_key": "ab" * 31, "ppid": "ab" * 32, "aux": "AB" * 32,
                   "measurement_id": "ab" * 32, "tcb_status": "OutOfDate", "advisory_ids": [1],
                   "issued_at_ms": True, "registered_ms": original["expires_ms"],
                   "expires_ms": original["collateral_expires_ms"] + 1}
        for field, value in invalid.items():
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_key_view({**original, field: value}, vector["max_lease_ms"])
        changed = copy.deepcopy(original)
        changed["measurement"]["key_provider_digest"] = "ab" * 32
        with self.assertRaises(ValueError):
            validate_key_view(changed, vector["max_lease_ms"])


if __name__ == "__main__":
    unittest.main()
