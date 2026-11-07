import contextlib
import hashlib
import io
import re
import unittest
from pathlib import Path

from gate import __main__ as gate_main

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "res"
CRATES = ("gate", "registry", "reports")
COMMIT = re.compile(r"[0-9a-f]{40}")
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
        "state_version",
        "subject_summary",
        "submit_report",
        "totals",
        "version",
        "withdraw",
    ],
}


def artifact_state(record, wasm_present):
    """The state a build record and the presence of its wasm describe: "built" for a 40 hex source
    commit with the wasm in res/, "unbuilt" for a TBD source commit with no wasm. Any other
    combination raises AssertionError."""
    lines = [line for line in record.splitlines() if line.startswith("Source commit: ")]
    if len(lines) != 1:
        raise AssertionError(f"expected one Source commit line, found {len(lines)}")
    deployed = [line for line in record.splitlines() if line.startswith("Deployed: ")]
    if len(deployed) != 1:
        raise AssertionError("expected one Deployed line")
    commit = lines[0].removeprefix("Source commit: ")
    if deployed[0] != "Deployed: not deployed" and not COMMIT.fullmatch(commit):
        raise AssertionError("a deployed or pending crate must record its source commit")
    if COMMIT.fullmatch(commit):
        if not wasm_present:
            raise AssertionError(f"source commit {commit} is recorded but the wasm is absent")
        return "built"
    if commit.startswith("TBD"):
        if wasm_present:
            raise AssertionError("the source commit is TBD but the wasm is present")
        return "unbuilt"
    raise AssertionError(f"the source commit is neither 40 hex nor TBD: {commit!r}")


class WasmArtifactsTest(unittest.TestCase):
    def test_each_crate_is_built_or_unbuilt_with_matching_files(self):
        for crate in CRATES:
            with self.subTest(crate=crate):
                wasm_path = RES / f"{crate}.wasm"
                record = (RES / f"BUILD-{crate}.md").read_text(encoding="utf-8")
                state = artifact_state(record, wasm_path.exists())
                if state == "built":
                    self.assertTrue((RES / f"{crate}.wasm.sha256").exists())
                else:
                    self.assertFalse((RES / f"{crate}.wasm.sha256").exists())

    def test_mismatched_record_and_wasm_states_are_refused(self):
        commit = "Source commit: " + "a" * 40 + "\n\nDeployed: pending\n"
        tbd = "Source commit: TBD by the orchestrator at deploy\n\nDeployed: not deployed\n"
        self.assertEqual(artifact_state(commit, True), "built")
        self.assertEqual(artifact_state(tbd, False), "unbuilt")
        with self.assertRaisesRegex(AssertionError, "wasm is absent"):
            artifact_state(commit, False)
        with self.assertRaisesRegex(AssertionError, "TBD but the wasm is present"):
            artifact_state(tbd, True)
        with self.assertRaisesRegex(AssertionError, "one Source commit line"):
            artifact_state(commit + commit, True)
        with self.assertRaisesRegex(AssertionError, "one Deployed line"):
            artifact_state("Source commit: " + "a" * 40 + "\n", True)
        with self.assertRaisesRegex(AssertionError, "must record its source commit"):
            artifact_state("Source commit: eb54c8e\nDeployed: pending\n", True)
        with self.assertRaisesRegex(AssertionError, "must record its source commit"):
            artifact_state("Source commit: TBD\n\nDeployed: 2 Nov, code_hash MATCH\n", False)
        with self.assertRaisesRegex(AssertionError, "neither 40 hex nor TBD"):
            artifact_state("Source commit: eb54c8e\nDeployed: not deployed\n", True)

    def test_sha256_files_match_the_wasm_bytes(self):
        for filename in CONTRACT_EXPORTS:
            wasm_path = RES / filename
            if not wasm_path.exists():
                continue
            expected = f"{hashlib.sha256(wasm_path.read_bytes()).hexdigest()}  res/{filename}\n"
            self.assertEqual((RES / f"{filename}.sha256").read_text(encoding="ascii"), expected)

    def test_previous_gate_wasm_matches_the_pinned_artifact(self):
        gate_bytes = (RES / "gate.wasm").read_bytes()
        previous_path = RES / "previous" / "gate.wasm"
        previous_bytes = previous_path.read_bytes()
        self.assertEqual(previous_bytes, gate_bytes)
        previous_sha256 = hashlib.sha256(previous_bytes).hexdigest()
        self.assertEqual(
            (RES / "gate.wasm.sha256").read_text(encoding="ascii"),
            f"{previous_sha256}  res/gate.wasm\n",
        )
        expected = f"{previous_sha256}  res/previous/gate.wasm\n"
        self.assertEqual(
            (RES / "previous" / "gate.wasm.sha256").read_text(encoding="ascii"),
            expected,
        )

    def test_exported_functions_match_each_contract_method_table(self):
        for filename, expected in CONTRACT_EXPORTS.items():
            if not (RES / filename).exists():
                continue
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = gate_main.main(["wasm-exports", str(RES / filename)])
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue().splitlines(), expected)


if __name__ == "__main__":
    unittest.main()
