import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from enclave.measure import (
    MEASUREMENT_FIELDS, measurement_from_tcb, measurement_id, parse_quote, report_data,
    validate_quote_td_attributes, validate_td_attributes,
)

ROOT = Path(__file__).resolve().parent.parent
VECTORS = json.loads((ROOT / "fixtures/attest/vectors.json").read_text(encoding="utf-8"))


def sample(version=4):
    args = VECTORS["registration_args"][0]["arguments"]
    events = copy.deepcopy(args["event_log"])
    register = bytes(48)
    for event in events:
        register = hashlib.sha384(register + bytes.fromhex(event["digest"])).digest()
    header = bytearray(48)
    header[:2] = version.to_bytes(2, "little")
    header[4:8] = b"\x81\0\0\0"
    body = bytearray(584)
    body[120:128] = (1 << 28).to_bytes(8, "little")
    body[136:184] = bytes(range(48))
    for index in range(3):
        body[328 + index * 48:376 + index * 48] = bytes([index + 1]) * 48
    body[472:520] = register
    body[520:584] = report_data("registry.near", args["public_key"], args["issued_at_ms"], args["aux"])
    quote = bytes(header) + (b"\x02\0\x48\x02\0\0" if version == 5 else b"") + bytes(body)
    parsed = parse_quote(quote)
    tcb = {key: parsed[key] for key in ("mrtd", "rtmr0", "rtmr1", "rtmr2", "rtmr3")}
    tcb.update(app_compose=args["app_compose"], compose_hash=events[0]["event_payload"], event_log=events)
    return quote, tcb


class EnclaveMeasureTests(unittest.TestCase):
    def test_report_vectors(self):
        for vector in VECTORS["report_data"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(report_data(**vector["input"]).hex(), vector["report_data_hex"])

    def test_measurement_vectors_and_repeat(self):
        for vector in VECTORS["measurements"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(measurement_id(vector["input"]), vector["measurement_id"])
                self.assertEqual(measurement_id(dict(reversed(list(vector["input"].items())))), vector["measurement_id"])

    def test_all_66_td_attribute_vectors(self):
        self.assertEqual(len(VECTORS["td_attributes"]), 66)
        for vector in VECTORS["td_attributes"]:
            with self.subTest(name=vector["name"]):
                if vector["accepted"]:
                    validate_td_attributes(vector["td_attributes"])
                else:
                    with self.assertRaises(ValueError):
                        validate_td_attributes(vector["td_attributes"])

    def test_all_quote_prefix_vectors(self):
        for vector in VECTORS["quote_td_attributes"]:
            with self.subTest(name=vector["name"]):
                quote = bytes.fromhex(vector["quote_prefix_hex"])
                if vector["accepted"]:
                    validate_quote_td_attributes(quote)
                else:
                    with self.assertRaises(ValueError):
                        validate_quote_td_attributes(quote)
                with self.assertRaises(ValueError):
                    parse_quote(quote)

    def test_report_refuses_bad_inputs(self):
        original = VECTORS["report_data"][0]["input"]
        for field, values in (("registry_account_id", ("a", "UPPER.near", "a..near", "a.near\0")),
                              ("issued_at_ms", (True, -1, 2**64, 1.0, "1")),
                              ("public_key", ("AA" * 32, "ab" * 31, None)),
                              ("aux", ("ab" * 33, "a " * 32, None))):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    report_data(**{**original, field: value})

    def test_measurement_refuses_noncanonical_fields(self):
        original = VECTORS["measurements"][0]["input"]
        for field in MEASUREMENT_FIELDS:
            for value in ("AA" * 48, "ab" * 47, None):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    measurement_id({**original, field: value})
            with self.assertRaises(ValueError):
                measurement_id({key: value for key, value in original.items() if key != field})
        with self.assertRaises(ValueError):
            measurement_id({**original, "rtmr3": "ab" * 48})

    def test_both_td10_wrappers(self):
        for version in (4, 5):
            quote, tcb = sample(version)
            parsed = parse_quote(quote)
            self.assertEqual(parsed["mrtd"], bytes(range(48)).hex())
            self.assertEqual(len(bytes.fromhex(parsed["report_data"])), 64)
            measurement = measurement_from_tcb(quote, tcb)
            self.assertEqual(set(measurement), set(MEASUREMENT_FIELDS))
            self.assertEqual(measurement["key_provider_digest"], tcb["event_log"][1]["digest"])

    def test_retained_upstream_quote_extraction(self):
        path = ROOT / "contracts/attest/tests/vendor/upstream/quote.json"
        quote = bytes(json.loads(path.read_text(encoding="utf-8")))
        parsed = parse_quote(quote)
        self.assertEqual(parsed["mrtd"], quote[184:232].hex())
        self.assertEqual(parsed["rtmr3"], quote[520:568].hex())
        self.assertEqual(parsed["report_data"], quote[568:632].hex())

    def test_retained_dstack_tcb_replay(self):
        upstream = ROOT / "contracts/attest/tests/vendor/upstream"
        quote = bytes(json.loads((upstream / "quote.json").read_text(encoding="utf-8")))
        tcb = json.loads((upstream / "tcb_info.json").read_text(encoding="utf-8"))
        self.assertEqual(measurement_from_tcb(quote, tcb)["compose_hash"], tcb["compose_hash"])
        capture = json.loads((ROOT / "fixtures/nearai/attestation-glm-gateway.json").read_text(encoding="utf-8"))
        gateway = capture["gateway_attestation"]
        tcb = gateway["info"]["tcb_info"]
        result = measurement_from_tcb(bytes.fromhex(gateway["intel_quote"]), tcb)
        self.assertEqual(measurement_id(result), "d494685f4456a7f63e3f4d193b6d0387fc8ed79fd3636e4b9ee853adb7f3b010")

    def test_body_attribute_checks_cannot_be_bypassed(self):
        for version in (4, 5):
            quote, tcb = sample(version)
            offset = 48 if version == 4 else 54
            for vector in VECTORS["td_attributes"]:
                changed = quote[:offset + 120] + bytes.fromhex(vector["td_attributes"]) + quote[offset + 128:]
                with self.subTest(version=version, name=vector["name"]):
                    if vector["accepted"]:
                        measurement_from_tcb(changed, tcb)
                    else:
                        with self.assertRaises(ValueError):
                            measurement_from_tcb(changed, tcb)

    def test_quote_refuses_malformed_wrappers(self):
        quote, _ = sample(5)
        for bad in (quote[:47], quote[:173], quote[:-1], quote.hex(),
                    b"\x06\0" + quote[2:], quote[:4] + bytes(4) + quote[8:],
                    quote[:48] + b"\x03\0" + quote[50:], quote[:50] + bytes(4) + quote[54:]):
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                parse_quote(bad)

    def test_tcb_registers_must_match_quote(self):
        quote, tcb = sample()
        for field in ("mrtd", "rtmr0", "rtmr1", "rtmr2", "rtmr3"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                measurement_from_tcb(quote, {**tcb, field: "ab" * 48})

    def test_replay_refuses_mutation_order_and_deletion(self):
        quote, tcb = sample()
        variants = [list(reversed(tcb["event_log"])), tcb["event_log"][:1],
                    [*tcb["event_log"], tcb["event_log"][1]],
                    [{**tcb["event_log"][0], "digest": "ab" * 48}, tcb["event_log"][1]],
                    [{**tcb["event_log"][0], "event_type": True}, tcb["event_log"][1]],
                    [{**tcb["event_log"][0], "event_payload": "GG"}, tcb["event_log"][1]],
                    [{**tcb["event_log"][0], "imr": True}, tcb["event_log"][1]]]
        for events in variants:
            with self.subTest(events=events), self.assertRaises(ValueError):
                measurement_from_tcb(quote, {**tcb, "event_log": events})

    def test_compose_uses_exact_bytes(self):
        quote, tcb = sample()
        for changed in ({**tcb, "app_compose": tcb["app_compose"].rstrip()},
                        {**tcb, "compose_hash": "00" * 32}):
            with self.assertRaises(ValueError):
                measurement_from_tcb(quote, changed)

    def test_replay_refuses_false_compose_and_absent_or_duplicate_key_provider(self):
        quote, tcb = sample()
        false_compose = {**tcb["event_log"][0], "event_payload": "00" * 32}
        false_compose["digest"] = hashlib.sha384((134217729).to_bytes(4, "little")
                                                + b":compose-hash:" + bytes(32)).hexdigest()
        for events in ([false_compose, tcb["event_log"][1]], [tcb["event_log"][0]],
                       [*tcb["event_log"], tcb["event_log"][0]]):
            with self.assertRaises(ValueError):
                measurement_from_tcb(quote, {**tcb, "event_log": events})

    def test_uncheckable_tcb_is_refused(self):
        quote, tcb = sample()
        for changed in (None, {}, {**tcb, "app_compose": None}, {**tcb, "event_log": {}},
                        {**tcb, "event_log": [None]}, {**tcb, "event_log": [{"imr": 3}]}):
            with self.subTest(tcb=changed), self.assertRaises(ValueError):
                measurement_from_tcb(quote, changed)

    def test_other_register_event_does_not_enter_replay(self):
        quote, tcb = sample()
        events = [{**tcb["event_log"][1], "imr": 2}, *tcb["event_log"]]
        self.assertEqual(measurement_from_tcb(quote, {**tcb, "event_log": events}), measurement_from_tcb(quote, tcb))

    def test_other_register_events_still_require_canonical_fields(self):
        quote, tcb = sample()
        original = {**tcb["event_log"][0], "imr": 2}
        for field, value in (("event_type", True), ("event_type", 2**32), ("event_type", -1),
                             ("digest", "AA" * 48), ("event", 3), ("event_payload", "0"),
                             ("event_payload", "AA")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                measurement_from_tcb(quote, {**tcb, "event_log": [{**original, field: value}, *tcb["event_log"]]})

    def test_cli_repeats_and_refuses_extra_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "measurement.json"
            vector = VECTORS["measurements"][1]
            path.write_text(json.dumps(vector["input"]), encoding="utf-8")
            command = [sys.executable, str(ROOT / "scripts/enclave_measure.py"), str(path)]
            first = subprocess.run(command, capture_output=True, text=True, check=True)
            second = subprocess.run(command, capture_output=True, text=True, check=True)
            self.assertEqual(first.stdout.strip(), vector["measurement_id"])
            self.assertEqual(first.stdout, second.stdout)
            path.write_text(json.dumps({**vector["input"], "extra": 1}), encoding="utf-8")
            refused = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(refused.stdout, "")

    def test_cli_refuses_duplicate_measurement_members(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "measurement.json"
            original = json.dumps(VECTORS["measurements"][1]["input"])
            path.write_text(original[:-1] + ', "mrtd": "' + "ab" * 48 + '"}', encoding="utf-8")
            result = subprocess.run([sys.executable, str(ROOT / "scripts/enclave_measure.py"), str(path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("duplicate measurement member", result.stderr)


if __name__ == "__main__":
    unittest.main()
