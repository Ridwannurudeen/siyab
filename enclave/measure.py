"""Siyab v1 byte bindings and structural quote checks, without DCAP verification."""

import hashlib
import re

from gate.canon import account_id

MEASUREMENT_FIELDS = ("mrtd", "rtmr0", "rtmr1", "rtmr2", "key_provider_digest", "compose_hash")
REGISTER_FIELDS = ("mrtd", "rtmr0", "rtmr1", "rtmr2", "rtmr3")


def fixed_hex(value, size):
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{size * 2}}}", value):
        raise ValueError(f"expected {size} bytes of lowercase hex")
    return bytes.fromhex(value)


def report_data(registry_account_id, public_key, issued_at_ms, aux):
    registry = account_id(registry_account_id, "registry_account_id").encode("ascii")
    if type(issued_at_ms) is not int or not 0 <= issued_at_ms < 2**64:
        raise ValueError("expected u64 issue time")
    binding = (b"siyab-attest-v1\0" + registry + b"\0" + fixed_hex(public_key, 32)
               + issued_at_ms.to_bytes(8, "big"))
    return hashlib.sha256(binding).digest() + fixed_hex(aux, 32)


def measurement_id(measurement):
    if not isinstance(measurement, dict) or set(measurement) != set(MEASUREMENT_FIELDS):
        raise ValueError("expected exactly the six measurement fields")
    preimage = b"siyab-measurement-v1" + b"".join(
        fixed_hex(measurement[field], 32 if field == "compose_hash" else 48)
        for field in MEASUREMENT_FIELDS
    )
    return hashlib.sha256(preimage).hexdigest()


def validate_td_attributes(value):
    attributes = int.from_bytes(fixed_hex(value, 8), "little")
    if attributes & 0x3fffffff2782ffff or not attributes & 0x10000000:
        raise ValueError("TD attributes fail the production TD10 predicate")


def _report_offset(quote):
    if not isinstance(quote, bytes) or len(quote) < 48 or quote[4:8] != b"\x81\0\0\0":
        raise ValueError("expected TDX quote header")
    version = int.from_bytes(quote[:2], "little")
    if version == 4:
        return 48
    if version == 5 and len(quote) >= 54 and quote[48:54] == b"\x02\0\x48\x02\0\0":
        return 54
    raise ValueError("expected version 4 or version 5 TD10 quote")


def validate_quote_td_attributes(quote):
    offset = _report_offset(quote) + 120
    if len(quote) < offset + 8:
        raise ValueError("truncated TD attributes")
    validate_td_attributes(quote[offset:offset + 8].hex())


def parse_quote(quote):
    """Extract a complete TD10 body; signatures and collateral remain unchecked."""
    offset = _report_offset(quote)
    if len(quote) < offset + 584:
        raise ValueError("truncated TD10 report")
    body = quote[offset:offset + 584]
    validate_td_attributes(body[120:128].hex())
    result = {"mrtd": body[136:184].hex()}
    for index in range(4):
        start = 328 + index * 48
        result[f"rtmr{index}"] = body[start:start + 48].hex()
    result["td_attributes"] = body[120:128].hex()
    result["report_data"] = body[520:584].hex()
    return result


def measurement_from_tcb(quote, tcb_info):
    """Check untrusted Info fields against a quote and replay its RTMR3 events."""
    report = parse_quote(quote)
    if not isinstance(tcb_info, dict):
        raise ValueError("expected TCB info object")
    for field in REGISTER_FIELDS:
        if fixed_hex(tcb_info.get(field), 48).hex() != report[field]:
            raise ValueError(f"TCB {field} differs from quote")
    compose = tcb_info.get("app_compose")
    if not isinstance(compose, str):
        raise ValueError("expected exact app_compose string")
    compose_hash = hashlib.sha256(compose.encode("utf-8")).digest()
    if fixed_hex(tcb_info.get("compose_hash"), 32) != compose_hash:
        raise ValueError("TCB compose hash differs from exact app_compose bytes")
    events = tcb_info.get("event_log")
    if not isinstance(events, list):
        raise ValueError("expected event log array")
    register = bytes(48)
    selected = {}
    for event in events:
        if not isinstance(event, dict) or set(event) != {"imr", "event_type", "digest", "event", "event_payload"}:
            raise ValueError("expected exact event fields")
        if type(event["imr"]) is not int or not 0 <= event["imr"] < 2**32:
            raise ValueError("expected u32 event register")
        if type(event["event_type"]) is not int or not 0 <= event["event_type"] < 2**32:
            raise ValueError("expected u32 event type")
        supplied_digest = fixed_hex(event["digest"], 48)
        name, payload = event["event"], event["event_payload"]
        if not isinstance(name, str) or not isinstance(payload, str) or not re.fullmatch(r"(?:[0-9a-f]{2})*", payload):
            raise ValueError("invalid event name or lowercase hexadecimal payload")
        if event["imr"] != 3:
            continue
        if event["event_type"] != 134217729:
            raise ValueError("invalid RTMR3 event type")
        payload_bytes = bytes.fromhex(payload)
        digest = hashlib.sha384(event["event_type"].to_bytes(4, "little") + b":"
                                + name.encode("utf-8") + b":" + payload_bytes).digest()
        if supplied_digest != digest:
            raise ValueError("event digest mismatch")
        register = hashlib.sha384(register + digest).digest()
        if name in ("compose-hash", "key-provider"):
            if name in selected:
                raise ValueError(f"duplicate {name} event")
            selected[name] = (payload_bytes, digest)
    if set(selected) != {"compose-hash", "key-provider"}:
        raise ValueError("missing compose-hash or key-provider event")
    if selected["compose-hash"][0] != compose_hash:
        raise ValueError("compose-hash event differs from app_compose")
    if register.hex() != report["rtmr3"]:
        raise ValueError("RTMR3 event replay mismatch")
    return {**{field: report[field] for field in REGISTER_FIELDS[:4]},
            "key_provider_digest": selected["key-provider"][1].hex(), "compose_hash": compose_hash.hex()}
