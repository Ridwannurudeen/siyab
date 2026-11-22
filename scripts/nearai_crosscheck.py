"""Recheck the recorded CPU quotes offline with dcap-qvl and Intel's QVL.

Usage:
  python scripts/nearai_crosscheck.py --intel-qvl /path/to/libsgx_dcap_quoteverify.so.1

Requires dcap-qvl 0.7.1 and the Linux x86_64 Intel library pinned in
fixtures/nearai/crosscheck-verifier.json. No report, collateral or model call is made.
"""

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import re
import ssl
from pathlib import Path

FIELDS = ("pck_crl_issuer_chain", "root_ca_crl", "pck_crl", "tcb_info_issuer_chain",
          "tcb_info", "qe_identity_issuer_chain", "qe_identity")
ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "nearai"


class Collateral(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("tee_type", ctypes.c_uint32)] + [
        (name, kind) for field in FIELDS for name, kind in (
            (field, ctypes.c_char_p), (field + "_size", ctypes.c_uint32))]


def intel_result(library, quote, raw, now):
    # Version 3.0 collateral uses hexadecimal DER CRLs and terminated JSON and PEM strings.
    values = dict(raw)
    values["tcb_info"] = ('{"tcbInfo":' + raw["tcb_info"] + ',"signature":"'
                          + raw["tcb_info_signature"] + '"}')
    values["qe_identity"] = ('{"enclaveIdentity":' + raw["qe_identity"] + ',"signature":"'
                             + raw["qe_identity_signature"] + '"}')
    collateral = Collateral(version=3, tee_type=0x81)
    buffers = {field: values[field].encode("utf-8") for field in FIELDS}
    for field, value in buffers.items():
        setattr(collateral, field, value)
        setattr(collateral, field + "_size", len(value) + 1)
    expired = ctypes.c_uint32(0xffffffff)
    result = ctypes.c_uint32(0xffffffff)
    code = library.tdx_qv_verify_quote(quote, len(quote), ctypes.byref(collateral), now,
        ctypes.byref(expired), ctypes.byref(result), None, 0, None)
    return {"api_code": code, "quote_result": result.value,
            "collateral_expiration_status": expired.value,
            "accepted": code == 0 and result.value == 0 and expired.value == 0}


def dcap_result(dcap, quote, raw, now):
    try:
        result = dcap.verify(quote, dcap.QuoteCollateralV3.from_json(json.dumps(raw)), now)
        return {"tcb_status": result.status, "advisory_ids": result.advisory_ids}
    except ValueError as error:
        return {"error": str(error)}


def leaf_digest(chain):
    first = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                      chain, flags=re.DOTALL).group()
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(first)).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intel-qvl", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    metadata = json.loads((ROOT / "crosscheck-verifier.json").read_text())
    library_digest = hashlib.sha256(args.intel_qvl.read_bytes()).hexdigest()
    if (library_digest != metadata["intel_qvl"]["library_sha256"]
            or ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_long) != 8):
        parser.error("the pinned Linux x86_64 Intel QVL library is required")
    if importlib.metadata.version("dcap-qvl") != "0.7.1":
        parser.error("dcap-qvl 0.7.1 is required")
    import dcap_qvl as dcap

    library = ctypes.CDLL(str(args.intel_qvl))
    library.tdx_qv_verify_quote.argtypes = [ctypes.c_char_p, ctypes.c_uint32,
        ctypes.POINTER(Collateral), ctypes.c_long, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    library.tdx_qv_verify_quote.restype = ctypes.c_uint32
    library.tee_get_fmspc_from_quote.argtypes = [ctypes.c_char_p, ctypes.c_uint32,
                                               ctypes.c_void_p, ctypes.c_uint32]
    library.tee_get_fmspc_from_quote.restype = ctypes.c_uint32
    original = json.loads((ROOT / "quote-results.json").read_text())
    now = original["checked_at_unix"]
    results, sources, control = [], {}, None
    for capture in ("deepseek", "glm"):
        path = ROOT / f"attestation-{capture}.json"
        sources[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        report = json.loads(path.read_text())
        quotes = [(None, "gateway", report["gateway_attestation"]["intel_quote"])]
        for index, model in enumerate(report["model_attestations"]):
            quotes.extend(((index, "model", model["intel_quote"]),
                           (index, "manager", model["compose_manager_attestation"]["quote"])))
        for candidate, kind, encoded in quotes:
            quote = bytes.fromhex(encoded)
            fmspc = ctypes.create_string_buffer(6)
            code = library.tee_get_fmspc_from_quote(quote, len(quote), fmspc, 6)
            if code != 0:
                raise ValueError(f"Intel could not extract FMSPC: {code:#x}")
            parsed = dcap.Quote.parse(quote)
            if fmspc.raw.hex() != parsed.fmspc().lower():
                raise ValueError("verifiers disagree on FMSPC")
            path = ROOT / f"intel-collateral-{fmspc.raw.hex()}.json"
            sources[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            raw = json.loads(path.read_text())
            corrected = dict(raw)
            del corrected["pck_certificate_chain"]
            replay = dcap_result(dcap, quote, raw, now)
            expected = next(row for row in original["results"] if row["capture"] == capture
                and row["candidate"] == candidate and row["kind"] == kind)
            if replay != {key: value for key, value in expected.items()
                          if key not in ("capture", "candidate", "kind")}:
                raise ValueError("original replay outcome changed")
            embedded = leaf_digest(parsed.cert_chain_pem_bytes().decode("ascii"))
            saved = leaf_digest(raw["pck_certificate_chain"])
            results.append({"capture": capture, "candidate": candidate, "kind": kind,
                "quote_sha256": hashlib.sha256(quote).hexdigest(), "fmspc": fmspc.raw.hex(),
                "embedded_pck_leaf_sha256": embedded, "saved_pck_leaf_sha256": saved,
                "saved_chain_matches_quote": embedded == saved,
                "dcap_original_collateral": replay,
                "dcap_quote_embedded_chain": dcap_result(dcap, quote, corrected, now),
                "intel_qvl": intel_result(library, quote, raw, now)})
            if capture == "deepseek" and candidate == 0 and kind == "model":
                control = (quote, raw, corrected)
    quote, raw, corrected = control
    if quote[:2] != bytes((4, 0)) or quote[764:766] != bytes((6, 0)):
        raise ValueError("negative controls require the recorded v4 TDX quote layout")
    controls = []
    for name, offset, check_time in (("td_report_bit_changed", 60, now),
                                    ("qe_report_signature_bit_changed", 1154, now),
                                    ("expired_collateral", None, 2145916800)):
        altered = bytearray(quote)
        if offset is not None:
            altered[offset] ^= 1
        changed = bytes(altered)
        controls.append({"case": name, "verification_time_unix": check_time,
            "dcap_quote_embedded_chain": dcap_result(dcap, changed, corrected, check_time),
            "intel_qvl": intel_result(library, changed, raw, check_time)})
    output = {"verification_time_unix": now, "time_source": "quote-results.json checked_at_unix",
        "original_results_sha256": hashlib.sha256((ROOT / "quote-results.json").read_bytes()).hexdigest(),
        "dcap_qvl": "0.7.1", "intel_qvl": metadata["intel_qvl"],
        "source_sha256": sources, "results": results, "negative_controls": controls,
        "accepted_cpu_quotes": sum(row["intel_qvl"]["accepted"] for row in results)}
    body = json.dumps(output, indent=2, ensure_ascii=True) + "\n"
    if args.out:
        args.out.write_text(body, encoding="ascii")
    else:
        print(body, end="")
    return int(any(not row["intel_qvl"]["accepted"]
                   or row["dcap_quote_embedded_chain"].get("tcb_status") != "UpToDate"
                   or row["dcap_quote_embedded_chain"].get("advisory_ids") != []
                   for row in results)
               or any(row["intel_qvl"]["accepted"]
                      or "error" not in row["dcap_quote_embedded_chain"] for row in controls))


if __name__ == "__main__":
    raise SystemExit(main())
