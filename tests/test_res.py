import contextlib
import hashlib
import io
import unittest
from pathlib import Path

from gate import __main__ as gate_main

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "res"
CONTRACT_EXPORTS = {
    "gate.wasm": [
        "__contract_abi",
        "contract_source_metadata",
        "get_agent",
        "get_pending",
        "get_policy",
        "get_verifier_keys",
        "is_verdict_used",
        "new",
        "on_transfer",
        "register_agent",
        "request_transfer",
        "revoke_agent",
        "set_agent_enabled",
        "set_allowlist",
        "set_caps",
        "set_denylist",
        "set_max_ttl",
        "set_paused",
        "set_verifier_keys",
        "version",
        "withdraw",
    ],
    "registry.wasm": [
        "__contract_abi",
        "contract_source_metadata",
        "get_label",
        "get_recorder",
        "is_allowed",
        "latest",
        "new",
        "record",
        "record_batch",
        "record_label",
        "set_recorder",
        "total_labels",
        "total_records",
        "total_subjects",
        "version",
    ],
    "reports.wasm": [
        "__contract_abi",
        "contract_source_metadata",
        "dispute",
        "get_dispute",
        "get_report",
        "moderate",
        "new",
        "publisher_stats",
        "reports_by_subject",
        "reports_by_submitter",
        "resolve_dispute",
        "set_moderators",
        "subject_summary",
        "submit_report",
        "totals",
        "withdraw",
    ],
}


class WasmArtifactsTest(unittest.TestCase):
    def test_sha256_files_match_the_wasm_bytes(self):
        for filename in CONTRACT_EXPORTS:
            wasm_path = RES / filename
            if filename == "reports.wasm" and not wasm_path.exists():
                continue
            expected = f"{hashlib.sha256(wasm_path.read_bytes()).hexdigest()}  res/{filename}\n"
            self.assertEqual((RES / f"{filename}.sha256").read_text(encoding="ascii"), expected)

    def test_previous_gate_wasm_matches_the_pinned_artifact(self):
        gate_bytes = (RES / "gate.wasm").read_bytes()
        previous_path = RES / "previous" / "gate.wasm"
        previous_bytes = previous_path.read_bytes()
        self.assertEqual(previous_bytes, gate_bytes)
        expected = (
            f"{hashlib.sha256(previous_bytes).hexdigest()}  "
            "res/previous/gate.wasm\n"
        )
        self.assertEqual(
            (RES / "previous" / "gate.wasm.sha256").read_text(encoding="ascii"),
            expected,
        )

    def test_exported_functions_match_each_contract_method_table(self):
        for filename, expected in CONTRACT_EXPORTS.items():
            if filename == "reports.wasm" and not (RES / filename).exists():
                continue
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = gate_main.main(["wasm-exports", str(RES / filename)])
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue().splitlines(), expected)


if __name__ == "__main__":
    unittest.main()
