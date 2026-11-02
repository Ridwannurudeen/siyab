# Claims to code

Every statement intended for the README has a code path, a test and a local evidence source. The `idea` column marks claims the later idea generator may use.

| Claim | File:function | Test | Evidence | idea |
|---|---|---|---|---|
| The receiver rules check available evidence and return Unknown when a required check cannot run. | `gate/rules.py:judge` | `tests/test_rules.py::OutputsTest.test_missing_fixture_is_unknown_never_a_pass` | offline | yes |
| The Gate requires a verdict and rejects a missing verdict. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::missing_verdict_is_refused` | offline | yes |
| The Gate refuses a verdict with a forged signature. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::flipped_signature_is_refused` | `fixtures/rpc/attacks/02-forged-signature.json` | yes |
| A non ALLOW verdict is refused by the Gate. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::non_allow_literal_is_refused` | offline | yes |
| A valid ALLOW schedules a bounded transfer and emits the transfer event. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::happy_path_debits_cap_and_emits_events` | offline | yes |
| The `pay` command always uses the verdict path and has no bypass. | `gate/agent.py:pay` | `tests/test_agent.py::NoBypassTest.test_the_source_has_no_bypass_word_and_pay_has_no_bypass_option` | offline | yes |
| The registry records verdicts and its latest view returns the evidence hash. | `contracts/registry/src/lib.rs:record_batch` | `contracts/registry/tests/sandbox.rs::registry_records_over_rpc_and_rotates_recorder` | offline | yes |
| The agent function call key is scoped to request_transfer. | `contracts/gate/tests/sandbox.rs:sandbox_transfer_policy_and_protocol_boundary` | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` | offline | yes |
| The agent key calling `on_transfer` is refused by the protocol (transaction rejected), not by the contract. | `contracts/gate/tests/sandbox.rs:sandbox_transfer_policy_and_protocol_boundary` | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` | `fixtures/rpc/attacks/05-callback.json` | yes |
| The agent key calling token `ft_transfer` is refused by the protocol (transaction rejected), not by the contract. | `contracts/gate/tests/sandbox.rs:sandbox_transfer_policy_and_protocol_boundary` | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` | `fixtures/rpc/attacks/06-token-direct.json` | yes |
| The agent key attaching 1 yoctoNEAR is refused by the protocol (transaction rejected), not by the contract. | `contracts/gate/tests/sandbox.rs:sandbox_transfer_policy_and_protocol_boundary` | `contracts/gate/tests/sandbox.rs::sandbox_transfer_policy_and_protocol_boundary` | `fixtures/rpc/attacks/08-one-yocto.json` | yes |
| Reusing a verdict is refused with `REFUSED:verdict_used`. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::repeated_verdict_id_is_refused` | `fixtures/rpc/attacks/03-replay.json` | yes |
| An expired ALLOW is refused with `REFUSED:expired`. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::expired_vector_is_refused` | `fixtures/rpc/attacks/03-expired.json` | yes |
| A transfer over its per call cap is refused. | `contracts/gate/src/lib.rs:request_transfer` | `contracts/gate/src/lib.rs::tests::per_call_cap_is_enforced` | `fixtures/rpc/attacks/04-over-cap.json` | yes |
| The NEAR Intents EVM leg is preflight before signing; no transaction on any chain. | `gate/intents.py:judge_quote` | `tests/test_intents.py::DemoRecipientTest.test_the_demo_recipient_is_deny_with_the_labels_and_the_fetch_time` | `fixtures/intents/`, `fixtures/goplus/` | yes |
| The demo replays the recorded NEAR and EVM outcomes offline. | `gate/demo.py:run` | `tests/test_demo.py::DemoTest.test_four_outcomes` | `fixtures/demo/` | yes |
| `scripts/smoke.py` runs the nine offline steps read only and reports PASS or FAIL per step. | `scripts/smoke.py:main` | `tests/test_smoke.py::SmokeTest.test_smoke_runs_nine_steps_read_only_and_leaves_the_tree_unchanged` | offline | no |
| The MCP server exposes the guard, EVM quote and account scan tools. | `mcp/server.py:serve` | `tests/test_mcp.py::McpServerTest.test_a_scripted_session` | offline | yes |
| The MCP server signs with the live key only when every live variable is set and refuses a partial configuration. | `mcp/server.py:_live_signing` | `tests/test_mcp.py::McpLiveSigningTest.test_every_partial_live_configuration_is_refused_and_nothing_is_signed` | offline | no |
| Consumers today are the guard, the MCP server and the demo agent; none from a third party yet. | `mcp/server.py:serve` | `tests/test_mcp.py::McpServerTest.test_a_scripted_session` | offline | no |
| The mainnet receiver outcome line is generated from recorded receiver judgements. | `scripts/counts.py:receivers_line` | `tests/test_receivers.py::ReceiverSetTest.test_a_replay_rejudges_every_subject_to_the_committed_files_and_the_counts_line` | `fixtures/receivers/` | no |
| The label store count is generated from committed assertions and provenance. | `scripts/counts.py:labels_line` | `tests/test_counts.py::CountsTest.test_the_label_and_incident_lines_equal_a_recount` | `intel/labels.jsonl` | no |
| Incident page totals are generated from committed incident files. | `scripts/counts.py:incidents_line` | `tests/test_counts.py::CountsTest.test_the_label_and_incident_lines_equal_a_recount` | `intel/incidents/` | no |
| Feed and flow totals are generated from committed state, events and traces. | `scripts/counts.py:feed_line` | `tests/test_counts.py::CountsTest.test_the_feed_and_flows_lines_equal_a_recount` | `intel/feed/`, `intel/flows/` | no |
| Verdict latency percentiles are generated from the recorded run fixtures. | `scripts/counts.py:latency_line` | `tests/test_counts.py::CountsTest.test_latency_is_not_measured_without_files_and_nearest_rank_with_them` | `fixtures/latency/` | no |
| README count lines are generated by `scripts/counts.py` from committed files. | `scripts/counts.py:lines` | `tests/test_counts.py::CountsTest.test_the_lines_follow_the_fixed_format_in_order` | offline | no |
| A TEE is not built. | `tests/test_attack_evidence.py:unbuilt_limit_rows` | `tests/test_attack_evidence.py::AttackEvidenceTest.test_unbuilt_surfaces_are_stated_as_limits` | offline | no |
| An OutLayer approver is not built. | `tests/test_attack_evidence.py:unbuilt_limit_rows` | `tests/test_attack_evidence.py::AttackEvidenceTest.test_unbuilt_surfaces_are_stated_as_limits` | offline | no |
| A browser extension for NEAR is not built. | `tests/test_attack_evidence.py:unbuilt_limit_rows` | `tests/test_attack_evidence.py::AttackEvidenceTest.test_unbuilt_surfaces_are_stated_as_limits` | offline | no |
| A Telegram bot for NEAR is not built. | `tests/test_attack_evidence.py:unbuilt_limit_rows` | `tests/test_attack_evidence.py::AttackEvidenceTest.test_unbuilt_surfaces_are_stated_as_limits` | offline | no |
