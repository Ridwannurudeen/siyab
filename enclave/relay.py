"""Untrusted host relay. Preparing a registration does not verify DCAP or send it."""

import argparse
import asyncio
import importlib.metadata
import json
import re
import subprocess
from pathlib import Path

from gate.canon import account_id
from enclave import measure

MAX_BYTES = 2_097_152
COLLATERAL_FIELDS = ("pck_crl_issuer_chain", "root_ca_crl", "pck_crl", "tcb_info_issuer_chain",
                     "tcb_info", "tcb_info_signature", "qe_identity_issuer_chain", "qe_identity",
                     "qe_identity_signature", "pck_certificate_chain")
BYTE_FIELDS = {"root_ca_crl", "pck_crl", "tcb_info_signature", "qe_identity_signature"}
ROOT = Path(__file__).resolve().parent.parent


def _hex(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{2})*", value):
        raise ValueError("expected lowercase hexadecimal bytes")
    if len(value) > MAX_BYTES * 2:
        raise ValueError("encoded input too large")
    return bytes.fromhex(value)


def verification_args(quote, collateral):
    """Exact upstream Borsh (QuoteBytes, Collateral), including the Option tag."""
    if not isinstance(quote, bytes) or len(quote) > MAX_BYTES:
        raise ValueError("invalid quote bytes")
    if not isinstance(collateral, dict) or set(collateral) != set(COLLATERAL_FIELDS):
        raise ValueError("expected exact collateral fields")
    values = [quote]
    for field in COLLATERAL_FIELDS[:-1]:
        value = collateral[field]
        if field in BYTE_FIELDS:
            if not isinstance(value, list) or any(type(x) is not int or not 0 <= x <= 255 for x in value):
                raise ValueError("expected collateral byte array")
            value = bytes(value)
        else:
            if not isinstance(value, str):
                raise ValueError("expected collateral string")
            value = value.encode("utf-8")
        values.append(value)
    chain = collateral["pck_certificate_chain"]
    if chain is not None and not isinstance(chain, str):
        raise ValueError("expected optional PCK chain string")
    encoded = b"".join(len(value).to_bytes(4, "little") + value for value in values)
    encoded += b"\0" if chain is None else b"\1" + len(chain.encode("utf-8")).to_bytes(4, "little") + chain.encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ValueError("verification tuple too large")
    return encoded.hex()


def decode_verification_args(encoded):
    data = _hex(encoded)
    offset = 0

    def vector():
        nonlocal offset
        if offset + 4 > len(data):
            raise ValueError("truncated Borsh length")
        length = int.from_bytes(data[offset:offset + 4], "little")
        offset += 4
        if offset + length > len(data):
            raise ValueError("truncated Borsh value")
        value = data[offset:offset + length]
        offset += length
        return value

    quote = vector()
    collateral = {}
    for field in COLLATERAL_FIELDS[:-1]:
        value = vector()
        collateral[field] = list(value) if field in BYTE_FIELDS else value.decode("utf-8")
    if offset >= len(data) or data[offset] not in (0, 1):
        raise ValueError("invalid Borsh Option")
    present = data[offset]
    offset += 1
    collateral["pck_certificate_chain"] = vector().decode("utf-8") if present else None
    if offset != len(data):
        raise ValueError("trailing Borsh bytes")
    return quote, collateral


def register_arguments(attestation, collateral):
    if not isinstance(attestation, dict) or set(attestation) != {
            "registry", "public_key_hex", "issued_at_ms", "aux", "quote", "tcb_info"}:
        raise ValueError("expected exact attestation fields")
    quote = _hex(attestation["quote"])
    expected = measure.report_data(attestation["registry"], attestation["public_key_hex"],
                                   attestation["issued_at_ms"], attestation["aux"])
    if measure.parse_quote(quote)["report_data"] != expected.hex():
        raise ValueError("quote report data differs from key binding")
    measure.measurement_from_tcb(quote, attestation["tcb_info"])
    tcb = attestation["tcb_info"]
    return {"public_key": attestation["public_key_hex"], "issued_at_ms": attestation["issued_at_ms"],
            "aux": attestation["aux"], "verification_args": verification_args(quote, collateral),
            "app_compose": tcb["app_compose"], "event_log": tcb["event_log"]}


def fetch_collateral(quote):
    """Fetch using this quote's certificate chain, never another machine's chain."""
    if importlib.metadata.version("dcap-qvl") != "0.7.0":
        raise ValueError("collateral fetching requires dcap-qvl 0.7.0")
    from dcap_qvl import get_collateral

    async def fetch():
        return await asyncio.wait_for(get_collateral("https://api.trustedservices.intel.com", quote), 60)

    try:
        fetched = asyncio.run(fetch())
        collateral = json.loads(fetched.to_json())
        collateral.setdefault("pck_certificate_chain", None)
        verification_args(quote, collateral)
        return collateral
    except (ValueError, RuntimeError, TimeoutError):
        raise ValueError("collateral fetch failed") from None


def command(registry, arguments, relayer, network, key_file=None):
    account_id(registry, "registry")
    account_id(relayer, "relayer")
    if network not in ("mainnet", "testnet"):
        raise ValueError("expected explicit mainnet or testnet")
    for value in (registry, relayer):
        if (value == "testnet" or value.endswith(".testnet")) != (network == "testnet"):
            raise ValueError("account does not match selected network")
    if key_file is not None:
        path = Path(key_file).resolve()
        if path.is_relative_to(ROOT):
            raise ValueError("key file must be outside the repository")
    return ["near", "contract", "call-function", "as-transaction", registry, "register_key", "json-args",
            json.dumps(arguments, separators=(",", ":"), ensure_ascii=False), "prepaid-gas", "300 Tgas",
            "attached-deposit", "0.01 NEAR", "sign-as", relayer, "network-config", network,
            "sign-with-access-key-file", str(key_file) if key_file is not None else "<external-key-file>", "send"]


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_json(path):
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("JSON input too large")
    return json.loads(data, object_pairs_hook=_unique)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attestation", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--collateral", type=Path)
    source.add_argument("--fetch-collateral", action="store_true")
    parser.add_argument("--relayer", required=True)
    parser.add_argument("--network", choices=("mainnet", "testnet"), required=True)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    if args.send and args.key_file is None:
        parser.error("--send requires an explicit external --key-file")
    try:
        attestation = _read_json(args.attestation)
        collateral = (fetch_collateral(_hex(attestation["quote"])) if args.fetch_collateral
                      else _read_json(args.collateral))
        arguments = register_arguments(attestation, collateral)
        planned = command(attestation["registry"], arguments, args.relayer, args.network, args.key_file)
        print(json.dumps({"argv": planned, "submitted": False}, ensure_ascii=False))
        if args.send:
            done = subprocess.run(planned, capture_output=True, text=True, timeout=180, check=False)
            print(json.dumps({"submitted": True, "returncode": done.returncode}))
            return 0 if done.returncode == 0 else 1
    except (OSError, ValueError, KeyError, TypeError, importlib.metadata.PackageNotFoundError,
            subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
