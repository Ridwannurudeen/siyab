import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from gate import __main__ as gate_main
from gate import base58, wasm

ROOT = Path(__file__).resolve().parent.parent
TINY = ROOT / "fixtures" / "wasm" / "tiny.wasm"
GATE_WASM = ROOT / "res" / "gate.wasm"
# The tiny module, by section: header; type () -> (); two functions of type 0; one memory of one page;
# exports "ping" (function 0), "add" (function 1) and "memory"; two empty bodies.
TINY_HEX = (
    "0061736d01000000",
    "010401600000",
    "0303020000",
    "0503010001",
    "071703",
    "0470696e670000",
    "036164640001",
    "066d656d6f72790200",
    "0a070202000b02000b",
)
TINY_SHA256 = "c3193338e9ce7718da1a80b4bb9fb9e742600999849b6b1c588c01762be42121"
TINY_CODE_HASH = "E8aniLC11sBUcSbK84KLUESifSd1JvPZ2Ptdf5nTkqqA"


def run(*argv):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = gate_main.main(list(argv))
    return code, output.getvalue().splitlines(), errors.getvalue()


class TinyModuleTest(unittest.TestCase):
    def test_the_fixture_is_the_hand_assembled_module(self):
        self.assertEqual(TINY.read_bytes(), bytes.fromhex("".join(TINY_HEX)))

    def test_codehash_prints_the_hex_and_the_base58_of_the_digest(self):
        self.assertEqual(run("codehash", str(TINY)), (0, [f"sha256: {TINY_SHA256}", f"code_hash: {TINY_CODE_HASH}"], ""))
        self.assertEqual(base58.decode(TINY_CODE_HASH), bytes.fromhex(TINY_SHA256))
        self.assertEqual(hashlib.sha256(TINY.read_bytes()).hexdigest(), TINY_SHA256)

    def test_wasm_exports_prints_the_function_names_sorted_and_skips_the_memory(self):
        self.assertEqual(run("wasm-exports", str(TINY)), (0, ["add", "ping"], ""))

    def test_a_module_that_does_not_parse_exits_1(self):
        data = TINY.read_bytes()
        cases = {
            "magic": b"\0asn" + data[4:],
            "version": data[:4] + b"\x02\0\0\0" + data[8:],
            "truncated": data[:-3],
            "long number": data[:8] + b"\x01\xff\xff\xff\xff\xff\x01",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, broken in cases.items():
                path = Path(tmp) / "broken.wasm"
                path.write_bytes(broken)
                with self.subTest(case=name):
                    with self.assertRaises(ValueError):
                        wasm.exports(broken)
                    code, lines, errors = run("wasm-exports", str(path))
                    self.assertEqual((code, lines), (1, []))
                    self.assertTrue(errors.startswith(f"cannot read {path}: "))
            code, _, errors = run("codehash", str(Path(tmp) / "missing.wasm"))
            self.assertEqual(code, 1)
            self.assertIn("cannot read", errors)

    def test_an_export_entry_past_its_section_is_refused(self):
        data = bytes.fromhex("0061736d01000000" + "0705010470696e67")
        with self.assertRaises(ValueError):
            wasm.exports(data)


class GateWasmTest(unittest.TestCase):
    def test_the_built_gate_exports_its_methods(self):
        if not GATE_WASM.exists():
            reason = "res/gate.wasm is not committed yet; the built wasm lands in res/ later"
            print(f"skipped: {reason}")
            self.skipTest(reason)
        code, lines, _ = run("wasm-exports", str(GATE_WASM))
        self.assertEqual(code, 0)
        self.assertIn("request_transfer", lines)
        self.assertIn("get_verifier_keys", lines)
        self.assertEqual(lines, sorted(lines))
        code, lines, _ = run("codehash", str(GATE_WASM))
        self.assertEqual(lines[0], f"sha256: {hashlib.sha256(GATE_WASM.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    unittest.main()
