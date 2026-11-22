"""Attested verifier views and signed verdict checks. No signer or transaction path.

The registry and Gate views are trusted inputs, not DCAP evidence. Offline verification
checks a supplied snapshot; --live reads one final block through the selected RPC.
Neither mode independently verifies a quote or proves the provider's state answers.
"""

import argparse
import hashlib
import json
from pathlib import Path

from gate import canon, ed25519, evidence, rpc
from gate.verify import rebuild_message

MEASUREMENT_FIELDS = ("mrtd", "rtmr0", "rtmr1", "rtmr2", "key_provider_digest", "compose_hash")
KEY_FIELDS = {"public_key", "measurement_id", "measurement", "ppid", "aux", "tcb_status", "advisory_ids",
              "issued_at_ms", "registered_ms", "expires_ms", "collateral_expires_ms"}
CONFIG_FIELDS = {"verifier_account_id", "signer_account_id", "ed25519_domain_id", "max_lease_ms", "vote_recipients"}
ATTESTATION_FIELDS = {"enabled", "registry_id", "measurement_timelock_ms", "measurements", "platforms",
                      "pending_platforms", "denied_keys", "keys"}
VERDICT_FIELDS = {"literal", "issued_ms", "expires_ms", "verdict_id", "evidence_hash", "key_id", "signature"}
SNAPSHOT_FIELDS = {"attestation", "registry_config", "registry_keys", "max_ttl_ms"}


class AttestError(ValueError):
    """An absent, malformed, expired or ineligible attestation or receipt."""


def _account(value, name):
    try:
        return canon.account_id(value, name)
    except canon.CanonError as error:
        raise AttestError(str(error)) from error


def _object(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise AttestError(f"{name}: expected exactly {', '.join(sorted(fields))}")


def _uint(value, name):
    if type(value) is not int or not 0 <= value <= canon.U64_MAX:
        raise AttestError(f"{name}: expected a u64 JSON integer")
    return value


def _hex(value, size, name):
    if (not isinstance(value, str) or len(value) != 2 * size
            or any(char not in "0123456789abcdef" for char in value)):
        raise AttestError(f"{name}: expected {size} bytes of lowercase hexadecimal")
    return bytes.fromhex(value)


def _platforms(value, *, empty=False):
    if not isinstance(value, list) or not (0 if empty else 1) <= len(value) <= 8:
        raise AttestError("platforms: expected a pinned array of 1 through 8 PPIDs")
    for item in value:
        _hex(item, 16, "ppid")
    if any(left >= right for left, right in zip(value, value[1:])):
        raise AttestError("platforms: expected sorted unique PPIDs")


def measurement_id(measurement):
    """The frozen v1 measurement digest, consuming bytes in the specified field order."""
    _object(measurement, MEASUREMENT_FIELDS, "measurement")
    preimage = b"siyab-measurement-v1" + b"".join(
        _hex(measurement[field], 32 if field == "compose_hash" else 48, field)
        for field in MEASUREMENT_FIELDS)
    return hashlib.sha256(preimage).hexdigest()


def validate_config(config):
    _object(config, CONFIG_FIELDS, "registry config")
    _account(config["verifier_account_id"], "verifier_account_id")
    _account(config["signer_account_id"], "signer_account_id")
    _uint(config["ed25519_domain_id"], "ed25519_domain_id")
    if _uint(config["max_lease_ms"], "max_lease_ms") == 0:
        raise AttestError("max_lease_ms: expected a positive lease")
    recipients = config["vote_recipients"]
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 8:
        raise AttestError("vote_recipients: expected 1 through 8 accounts")
    for account in recipients:
        _account(account, "vote_recipients")
    if any(left >= right for left, right in zip(recipients, recipients[1:])):
        raise AttestError("vote_recipients: expected sorted unique accounts")
    return config


def validate_key_view(view, max_lease_ms, *, public_key=None, now_ms=None):
    """Check the complete frozen key view, including its issue based lease formula."""
    _object(view, KEY_FIELDS, "attested key")
    for field in ("public_key", "measurement_id", "aux"):
        _hex(view[field], 32, field)
    _hex(view["ppid"], 16, "ppid")
    if public_key is not None:
        _hex(public_key, 32, "requested public_key")
        if view["public_key"] != public_key:
            raise AttestError("registry returned another public_key")
    if measurement_id(view["measurement"]) != view["measurement_id"]:
        raise AttestError("measurement_id does not match measurement")
    if view["tcb_status"] != "UpToDate":
        raise AttestError("tcb_status is not UpToDate")
    advisories = view["advisory_ids"]
    if not isinstance(advisories, list) or not all(isinstance(item, str) for item in advisories):
        raise AttestError("advisory_ids: expected a string array")
    for field in ("issued_at_ms", "registered_ms", "expires_ms", "collateral_expires_ms"):
        _uint(view[field], field)
    if _uint(max_lease_ms, "max_lease_ms") == 0:
        raise AttestError("max_lease_ms: expected a positive lease")
    issued, registered = view["issued_at_ms"], view["registered_ms"]
    lease_end = _uint(issued + max_lease_ms, "issued_at_ms + max_lease_ms")
    if view["expires_ms"] != min(lease_end, view["collateral_expires_ms"]):
        raise AttestError("lease expiry does not equal the issue based policy formula")
    if not max(0, registered - 7200000) <= issued <= registered:
        raise AttestError("issued_at_ms is outside the inclusive two hour window")
    if not registered < view["expires_ms"] <= view["collateral_expires_ms"]:
        raise AttestError("attested key has invalid lease ordering")
    if now_ms is not None and not registered <= _uint(now_ms, "now_ms") < view["expires_ms"]:
        raise AttestError("attested key is expired or not yet registered")
    return view


def sync_args(public_key):
    """The exact JSON arguments for Gate.sync_verifier and registry.get_key."""
    _hex(public_key, 32, "public_key")
    return {"public_key": public_key}


def registry_views(client, registry_id, public_key, *, block_id=None, now_ms=None):
    """Read immutable config before the key, optionally pinned to one final height."""
    _account(registry_id, "registry_id")
    args = sync_args(public_key)
    config = validate_config(client.call_function(registry_id, "get_config", {}, block_id=block_id))
    key = client.call_function(registry_id, "get_key", args, block_id=block_id)
    validate_key_view(key, config["max_lease_ms"], public_key=public_key, now_ms=now_ms)
    return {"config": config, "key": key}


def _attestation(view, max_lease_ms):
    _object(view, ATTESTATION_FIELDS, "Gate attestation")
    if view["enabled"] is not True:
        raise AttestError("Gate attested mode is not enabled")
    _account(view["registry_id"], "registry_id")
    if _uint(view["measurement_timelock_ms"], "measurement_timelock_ms") < 86400000:
        raise AttestError("measurement timelock is shorter than 24 hours")
    _platforms(view["platforms"], empty=True)
    pending = view["pending_platforms"]
    if pending is not None:
        _object(pending, {"platforms", "active_from_ms"}, "pending platforms")
        _platforms(pending["platforms"])
        _uint(pending["active_from_ms"], "pending active_from_ms")
    measurements = view["measurements"]
    if not isinstance(measurements, list):
        raise AttestError("measurements: expected an array")
    seen = set()
    for row in measurements:
        _object(row, {"measurement_id", "label", "active_from_ms"}, "approved measurement")
        _hex(row["measurement_id"], 32, "measurement_id")
        if not isinstance(row["label"], str) or row["measurement_id"] in seen:
            raise AttestError("approved measurements have invalid labels or duplicate ids")
        _uint(row["active_from_ms"], "active_from_ms")
        seen.add(row["measurement_id"])
    denied = view["denied_keys"]
    if not isinstance(denied, list):
        raise AttestError("denied_keys: expected an array")
    for key in denied:
        _hex(key, 32, "denied public_key")
    if len(set(denied)) != len(denied):
        raise AttestError("denied_keys contains duplicate keys")
    keys = view["keys"]
    if not isinstance(keys, list) or len(keys) > 4:
        raise AttestError("Gate cache must contain at most four keys")
    seen = set()
    for key in keys:
        validate_key_view(key, max_lease_ms)
        if key["public_key"] in seen:
            raise AttestError("Gate cache contains duplicate keys")
        seen.add(key["public_key"])
    return view


def read_views(client, gate, *, pinned_registry=None):
    """Read Gate policy, registry config and every cached key at one final block."""
    _account(gate, "gate")
    block = client.final_block()
    header = block.get("header") if isinstance(block, dict) else None
    if not isinstance(header, dict):
        raise AttestError("final block has no header")
    height = _uint(header.get("height"), "block height")
    now_ms = _uint(header.get("timestamp"), "block timestamp") // 1000000
    view = client.call_function(gate, "get_attestation", {}, block_id=height)
    _object(view, ATTESTATION_FIELDS, "Gate attestation")
    registry_id = _account(view["registry_id"], "registry_id")
    if pinned_registry is not None and registry_id != _account(pinned_registry, "pinned registry"):
        raise AttestError("Gate registry differs from the client pin")
    policy = client.call_function(gate, "get_policy", {}, block_id=height)
    _object(policy, {"max_ttl_ms", "paused", "denylist"}, "Gate policy")
    max_ttl_ms = _uint(policy["max_ttl_ms"], "max_ttl_ms")
    config = validate_config(client.call_function(registry_id, "get_config", {}, block_id=height))
    _attestation(view, config["max_lease_ms"])
    keys = []
    for cached in view["keys"]:
        key = client.call_function(registry_id, "get_key", sync_args(cached["public_key"]), block_id=height)
        if key is not None:
            validate_key_view(key, config["max_lease_ms"], public_key=cached["public_key"])
            keys.append(key)
    return {"attestation": view, "registry_config": config, "registry_keys": keys, "max_ttl_ms": max_ttl_ms,
            "now_ms": now_ms, "block_height": height}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AttestError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(value):
    raise AttestError("nonfinite JSON number")


def _decode(data):
    return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _receipt_message(receipt, data, gate, agent_pk_hex, now_ms, max_ttl_ms):
    if not isinstance(receipt, dict) or receipt.get("outcome") != "ALLOW" or receipt.get("action") != "sign":
        raise AttestError("receipt is not a signed ALLOW action")
    verdict = receipt.get("verdict")
    _object(verdict, VERDICT_FIELDS, "Verdict")
    if verdict["literal"] != "ALLOW" or verdict["key_id"] != "cur":
        raise AttestError("attested Verdict requires ALLOW and key_id cur")
    for field in ("issued_ms", "expires_ms"):
        _uint(verdict[field], field)
    _hex(verdict["verdict_id"], 16, "verdict_id")
    _hex(verdict["evidence_hash"], 32, "evidence_hash")
    _hex(verdict["signature"], 64, "signature")
    if (verdict["expires_ms"] <= now_ms or verdict["expires_ms"] < verdict["issued_ms"]
            or verdict["issued_ms"] > min(canon.U64_MAX, now_ms + 30000)
            or verdict["expires_ms"] - verdict["issued_ms"] > max_ttl_ms):
        raise AttestError("Verdict is expired, future dated or exceeds the Gate TTL")
    if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != verdict["evidence_hash"]:
        raise AttestError("evidence bytes do not match the signed hash")
    document = _decode(data)
    _object(document, {"version", "subject", "checks", "labels", "verdict", "reasons", "observed",
                       "issued_ms", "expires_ms", "verdict_id", "latency_ms"}, "evidence")
    if evidence.canonical_bytes(document) != data or document["version"] != evidence.VERSION:
        raise AttestError("evidence is not canonical version 1 JSON")
    if document["verdict"] != "ALLOW":
        raise AttestError("evidence verdict is not ALLOW")
    subject = document["subject"]
    _object(subject, {"gate", "agent_pk", "kind", "token", "receiver", "amount", "memo_sha256"}, "subject")
    if subject["gate"] != gate or subject["agent_pk"] != agent_pk_hex:
        raise AttestError("evidence does not match the requested Gate and agent")
    if not isinstance(subject["amount"], str):
        raise AttestError("evidence amount must be a canonical decimal string")
    if subject["memo_sha256"] is not None:
        _hex(subject["memo_sha256"], 32, "memo_sha256")
    for field in ("labels", "reasons"):
        if not isinstance(document[field], list) or not all(isinstance(item, str) for item in document[field]):
            raise AttestError(f"evidence {field} must be a string array")
    observed = document["observed"]
    _object(observed, {"block_height", "timestamp_ms"}, "observed")
    for field in observed:
        if observed[field] is not None:
            _uint(observed[field], field)
    if document["latency_ms"] is not None:
        _uint(document["latency_ms"], "latency_ms")
    checks = document["checks"]
    if not isinstance(checks, list):
        raise AttestError("evidence checks must be an array")
    for check in checks:
        _object(check, {"id", "status", "detail", "source"}, "evidence check")
        if (not isinstance(check["id"], str) or check["status"] not in evidence.STATUSES
                or not isinstance(check["detail"], str)):
            raise AttestError("evidence check has invalid fields")
        source = check["source"]
        _object(source, {"provider", "request_sha256", "fetched_at"}, "check source")
        if (not isinstance(source["provider"], str)
                or any(source[field] is not None and not isinstance(source[field], str)
                       for field in ("request_sha256", "fetched_at"))):
            raise AttestError("evidence check source has invalid fields")
    for field in ("issued_ms", "expires_ms", "verdict_id"):
        if type(document[field]) is not type(verdict[field]) or document[field] != verdict[field]:
            raise AttestError(f"evidence {field} differs from Verdict")
    for field in ("checks", "reasons", "evidence_hash", "latency_ms"):
        expected = verdict["evidence_hash"] if field == "evidence_hash" else document[field]
        if field in receipt and evidence.canonical_bytes(receipt[field]) != evidence.canonical_bytes(expected):
            raise AttestError(f"receipt {field} differs from evidence")
    if "evidence" in receipt and receipt["evidence"] != data.decode("utf-8"):
        raise AttestError("receipt embedded evidence differs from the supplied bytes")
    if "request" in receipt:
        request = receipt["request"]
        _object(request, {"kind", "token_id", "receiver_id", "amount", "memo"}, "request")
        canon.validate_transfer(gate=gate, agent_pk_hex=agent_pk_hex, kind=request["kind"], token_id=request["token_id"],
                                receiver_id=request["receiver_id"], amount=request["amount"], memo=request["memo"])
        if evidence.subject(gate=gate, agent_pk=agent_pk_hex, kind=request["kind"], token=request["token_id"],
                            receiver=request["receiver_id"], amount=request["amount"], memo=request["memo"]) != subject:
            raise AttestError("receipt request differs from the signed evidence subject")
    message = rebuild_message(document, verdict)
    if "message" in receipt and receipt["message"] != message.decode("utf-8"):
        raise AttestError("receipt message differs from rebuilt GATEv1")
    return message


def verify_receipt(receipt, evidence_bytes, *, gate, agent_pk_hex, attestation, registry_config,
                   registry_keys, now_ms, pinned_measurements=None, pinned_registry=None, max_ttl_ms=300000):
    """Authenticate an ALLOW receipt against supplied Gate and registry policy views.

    Raises ValueError or TypeError on refusal. A successful result proves the signature and these
    inputs agree. It does not prove a payment will pass caps, pause or replay checks.
    The caller must authenticate and refresh its view inputs, or keep the action UNKNOWN.
    """
    _account(gate, "gate")
    _hex(agent_pk_hex, 32, "agent_pk_hex")
    _uint(now_ms, "now_ms")
    _uint(max_ttl_ms, "max_ttl_ms")
    config = validate_config(registry_config)
    view = _attestation(attestation, config["max_lease_ms"])
    registry_id = view["registry_id"]
    if pinned_registry is not None and registry_id != _account(pinned_registry, "pinned registry"):
        raise AttestError("Gate registry differs from the client pin")
    if pinned_measurements is not None:
        if not isinstance(pinned_measurements, list) or not pinned_measurements:
            raise AttestError("pinned_measurements must be a nonempty array")
        for value in pinned_measurements:
            _hex(value, 32, "pinned measurement_id")
    message = _receipt_message(receipt, evidence_bytes, gate, agent_pk_hex, now_ms, max_ttl_ms)
    if not isinstance(registry_keys, list) or len(registry_keys) > 4:
        raise AttestError("registry_keys must contain at most four key views")
    records = {}
    for record in registry_keys:
        validate_key_view(record, config["max_lease_ms"])
        if record["public_key"] in records:
            raise AttestError("duplicate registry key view")
        records[record["public_key"]] = record
    active = {row["measurement_id"] for row in view["measurements"] if row["active_from_ms"] <= now_ms}
    signature = bytes.fromhex(receipt["verdict"]["signature"])
    signer = receipt.get("signer")
    if signer is not None:
        _object(signer, {"public_key_hex", "measurement_id", "registry"}, "receipt signer")
        _hex(signer["public_key_hex"], 32, "signer public_key_hex")
        _hex(signer["measurement_id"], 32, "signer measurement_id")
        if signer["registry"] != registry_id:
            raise AttestError("receipt signer registry differs from Gate registry")
    for cached in view["keys"]:
        public_key = cached["public_key"]
        if (public_key in view["denied_keys"] or cached["measurement_id"] not in active
                or cached["ppid"] not in view["platforms"]
                or not cached["registered_ms"] <= now_ms < cached["expires_ms"]
                or pinned_measurements is not None and cached["measurement_id"] not in pinned_measurements
                or signer is not None and signer["public_key_hex"] != public_key):
            continue
        record = records.get(public_key)
        if record is None or not record["registered_ms"] <= now_ms < record["expires_ms"]:
            continue
        if (any(record[field] != cached[field] for field in ("measurement_id", "measurement", "ppid", "aux"))
                or record["issued_at_ms"] < cached["issued_at_ms"]
                or record["registered_ms"] < cached["registered_ms"]
                or record["issued_at_ms"] == cached["issued_at_ms"] and record != cached):
            continue
        if signer is not None and signer["measurement_id"] != record["measurement_id"]:
            continue
        public = bytes.fromhex(public_key)
        try:
            small_order = any(ed25519.scalarmult(ed25519.decodepoint(point), 8) == [0, 1]
                              for point in (public, signature[:32]))
        except ValueError:
            continue
        if not small_order and ed25519.verify(public, canon.digest(message), signature):
            return {"verified": True, "signed_verdict": "ALLOW", "public_key": public_key,
                    "measurement_id": record["measurement_id"], "registry_id": registry_id,
                    "ppid": record["ppid"], "evidence_hash": receipt["verdict"]["evidence_hash"],
                    "dcap_verified": False}
    raise AttestError("no eligible cached attested key verifies this receipt")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check attested receipts or print view and sync arguments; never sends transactions.")
    commands = parser.add_subparsers(dest="command", required=True)
    measure = commands.add_parser("measure", help="hash an exact six field measurement object, offline")
    measure.add_argument("measurement")
    sync = commands.add_parser("sync-args", help="print exact sync_verifier JSON arguments, offline")
    sync.add_argument("public_key")
    verify = commands.add_parser("verify", help="verify signed receipt evidence against Gate and registry views")
    verify.add_argument("receipt")
    verify.add_argument("--evidence", help="exact evidence bytes; otherwise use the receipt evidence string")
    verify.add_argument("--gate", required=True)
    verify.add_argument("--agent-pk", required=True, help="raw 32 byte agent key in lowercase hexadecimal")
    source = verify.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", help="JSON containing attestation, registry_config, registry_keys and max_ttl_ms")
    source.add_argument("--live", action="store_true", help="explicitly permit read only public RPC calls")
    verify.add_argument("--rpc-url", help="required with --live; no network endpoint is inferred")
    verify.add_argument("--now-ms", type=int, help="required for a supplied offline snapshot")
    verify.add_argument("--registry", help="also pin the registry account")
    verify.add_argument("--measurement", action="append", help="also pin a measurement id; may be repeated")
    args = parser.parse_args(argv)
    try:
        if args.command == "measure":
            print(measurement_id(_decode(Path(args.measurement).read_bytes())))
            return 0
        if args.command == "sync-args":
            print(json.dumps(sync_args(args.public_key), separators=(",", ":")))
            return 0
        receipt = _decode(Path(args.receipt).read_bytes())
        if args.evidence:
            data = Path(args.evidence).read_bytes()
        elif isinstance(receipt, dict) and isinstance(receipt.get("evidence"), str):
            data = receipt["evidence"].encode("utf-8")
        else:
            raise AttestError("provide --evidence or an embedded evidence string")
        if args.live:
            if not args.rpc_url or args.now_ms is not None:
                raise AttestError("--live requires --rpc-url and refuses --now-ms")
            snapshot = read_views(rpc.Client(args.rpc_url, mode="off"), args.gate, pinned_registry=args.registry)
            now_ms = snapshot.pop("now_ms")
            height = snapshot.pop("block_height")
        else:
            if args.rpc_url or args.now_ms is None:
                raise AttestError("--snapshot requires --now-ms and refuses --rpc-url")
            snapshot = _decode(Path(args.snapshot).read_bytes())
            now_ms, height = args.now_ms, None
        _object(snapshot, SNAPSHOT_FIELDS, "snapshot")
        result = verify_receipt(receipt, data, gate=args.gate, agent_pk_hex=args.agent_pk, now_ms=now_ms,
                                pinned_registry=args.registry, pinned_measurements=args.measurement, **snapshot)
        result.update(outcome="UNKNOWN", payment_eligibility="not checked", block_height=height, now_ms=now_ms,
                      state_source="final RPC views" if args.live else "supplied snapshot",
                      trust="Gate and registry policy inputs; RPC state is not independently proven; no DCAP re-verification")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, rpc.RpcError, rpc.TransportError,
            rpc.HttpError, rpc.RateLimited, rpc.FixtureMiss) as error:
        print(json.dumps({"verified": False, "outcome": "UNKNOWN", "error": str(error)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
