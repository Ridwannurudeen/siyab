use base64::{engine::general_purpose::STANDARD, Engine};
use near_workspaces::{result::ExecutionFinalResult, types::Gas, Contract};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};
use tee_verifier_interface::{
    Collateral, QuoteBytes, Report, VerificationResultWithCollateralDates,
};

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn vendor(name: &str) -> Vec<u8> {
    fs::read(root().join("tests/vendor").join(name)).unwrap()
}

fn fixture(name: &str) -> Value {
    serde_json::from_slice(&fs::read(root().join("../../fixtures/attest").join(name)).unwrap())
        .unwrap()
}

fn nearai_cases() -> Vec<(String, QuoteBytes, Collateral)> {
    let source = root().join("../../fixtures/nearai");
    let crosscheck: Value =
        serde_json::from_slice(&fs::read(source.join("crosscheck-results.json")).unwrap()).unwrap();
    assert_eq!(
        crosscheck["verification_time_unix"],
        fixture("clock.json")["nearai_recorded_at_unix"]
    );
    for (file, expected) in crosscheck["source_sha256"].as_object().unwrap() {
        assert_eq!(
            hex::encode(Sha256::digest(fs::read(source.join(file)).unwrap())),
            expected.as_str().unwrap()
        );
    }
    let mut rows = crosscheck["results"].as_array().unwrap().clone();
    assert_eq!(rows.len(), 14);
    for extra in fixture("additional-quotes.json")["quotes"]
        .as_array()
        .unwrap()
    {
        let raw = fs::read(source.join(extra["file"].as_str().unwrap())).unwrap();
        assert_eq!(hex::encode(Sha256::digest(raw)), extra["source_sha256"]);
        rows.push(
            json!({"capture": "", "file": extra["file"], "name": extra["name"], "kind": "gateway",
            "candidate": null, "quote_sha256": extra["quote_sha256"], "fmspc": extra["fmspc"]}),
        );
    }
    let mut cases = Vec::new();
    for row in &rows {
        let capture = row["capture"].as_str().unwrap();
        let file = row["file"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| format!("attestation-{capture}.json"));
        let report: Value = serde_json::from_slice(&fs::read(source.join(file)).unwrap()).unwrap();
        let kind = row["kind"].as_str().unwrap();
        let encoded = if let Some(index) = row["candidate"].as_u64() {
            let candidate = &report["model_attestations"][index as usize];
            if kind == "model" {
                &candidate["intel_quote"]
            } else {
                &candidate["compose_manager_attestation"]["quote"]
            }
        } else {
            &report["gateway_attestation"]["intel_quote"]
        };
        let quote = hex::decode(encoded.as_str().unwrap()).unwrap();
        assert_eq!(hex::encode(Sha256::digest(&quote)), row["quote_sha256"]);
        let fmspc = row["fmspc"].as_str().unwrap();
        let raw: Value = serde_json::from_slice(
            &fs::read(source.join(format!("intel-collateral-{fmspc}.json"))).unwrap(),
        )
        .unwrap();
        let bytes = |field: &str| hex::decode(raw[field].as_str().unwrap()).unwrap();
        let text = |field: &str| raw[field].as_str().unwrap().to_owned();
        let collateral = Collateral {
            pck_crl_issuer_chain: text("pck_crl_issuer_chain"),
            root_ca_crl: bytes("root_ca_crl"),
            pck_crl: bytes("pck_crl"),
            tcb_info_issuer_chain: text("tcb_info_issuer_chain"),
            tcb_info: text("tcb_info"),
            tcb_info_signature: bytes("tcb_info_signature"),
            qe_identity_issuer_chain: text("qe_identity_issuer_chain"),
            qe_identity: text("qe_identity"),
            qe_identity_signature: bytes("qe_identity_signature"),
            pck_certificate_chain: None,
        };
        cases.push((
            row["name"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| format!("{capture}-{kind}-{}", row["candidate"])),
            QuoteBytes(quote),
            collateral,
        ));
    }
    assert_eq!(cases.len(), 16);
    cases
}

async fn verify(
    contract: &Contract,
    name: &str,
    args: Vec<u8>,
) -> VerificationResultWithCollateralDates {
    let args_sha256 = hex::encode(Sha256::digest(&args));
    let outcome = contract
        .call("verify_quote_with_collateral_dates")
        .args(args)
        .gas(Gas::from_tgas(200))
        .transact()
        .await
        .unwrap();
    record(name, contract, &outcome, &args_sha256);
    let value: VerificationResultWithCollateralDates = outcome.borsh().unwrap();
    match &value {
        VerificationResultWithCollateralDates::Verified {
            report,
            collateral_dates,
        } => {
            println!(
                "ATTEST_T0 {}",
                json!({"name": name, "result": "Verified", "status": report.status,
                "ppid": hex::encode(&report.ppid), "earliest_expiration_date": collateral_dates.earliest_expiration_date,
                "latest_issue_date": collateral_dates.latest_issue_date})
            );
            assert_eq!(report.status, "UpToDate");
            let Report::TD10(td) = &report.report else {
                panic!("expected exactly TD10")
            };
            attest::validate_td_attributes(td.td_attributes).unwrap();
            assert_eq!(report.ppid.len(), 16);
            let clock = fixture("clock.json");
            let expiry = if name == "upstream-good" {
                &clock["upstream_earliest_expiration_unix"]
            } else {
                &clock["nearai_earliest_expiration_unix"]
            };
            assert_eq!(
                collateral_dates.earliest_expiration_date,
                expiry.as_u64().unwrap()
            );
        }
        VerificationResultWithCollateralDates::Rejected(error) => {
            println!(
                "ATTEST_T0 {}",
                json!({"name": name, "result": "Rejected", "error": error.to_string()})
            );
        }
    }
    value
}

fn record(name: &str, contract: &Contract, outcome: &ExecutionFinalResult, args_sha256: &str) {
    let receipts: Vec<_> = outcome.receipt_outcomes().iter().map(|receipt| json!({
        "executor_id": receipt.executor_id.as_str(), "gas": receipt.gas_burnt.as_gas(), "logs": receipt.logs,
    })).collect();
    println!(
        "ATTEST_T0 {}",
        json!({"name": name, "args_sha256": args_sha256,
        "contract": contract.id().as_str(), "total_gas": outcome.total_gas_burnt.as_gas(), "receipts": receipts})
    );
}

#[test]
fn upstream_borsh_fixture_and_artifact_digests() {
    let args = vendor("upstream/verify_quote_args.borsh");
    let (quote, collateral): (QuoteBytes, Collateral) = borsh::from_slice(&args).unwrap();
    let source_quote: Vec<u8> = serde_json::from_slice(&vendor("upstream/quote.json")).unwrap();
    assert_eq!(quote.0, source_quote);
    assert_eq!(borsh::to_vec(&(quote, collateral)).unwrap(), args);
    for file in serde_json::from_slice::<Value>(&vendor("upstream/manifest.json")).unwrap()["files"]
        .as_array()
        .unwrap()
    {
        assert_eq!(
            hex::encode(Sha256::digest(vendor(&format!(
                "upstream/{}",
                file["file"].as_str().unwrap()
            )))),
            file["sha256"]
        );
    }
    let deployments = fixture("deployment.json");
    let code = &deployments["records"][0];
    assert_eq!(
        hex::encode(Sha256::digest(vendor("tee-verifier-mainnet.wasm"))),
        code["wasm_sha256"]
    );
    let records = deployments["records"].as_array().unwrap();
    assert_eq!(
        records[1]["response"]["result"]["code_hash"],
        records[3]["response"]["result"]["code_hash"]
    );
    assert_eq!(records[2]["response"]["result"]["keys"], json!([]));
    assert_eq!(records[4]["response"]["result"]["keys"], json!([]));
    let builds: Value = serde_json::from_slice(&vendor("builds.json")).unwrap();
    assert_eq!(builds["block_clock"], false);
    assert_eq!(builds["tag"], "3.18.0");
    for artifact in builds["artifacts"].as_array().unwrap() {
        assert_eq!(artifact["test_only"], true);
        let wasm = vendor(artifact["file"].as_str().unwrap());
        assert_eq!(wasm.len() as u64, artifact["bytes"].as_u64().unwrap());
        assert_eq!(hex::encode(Sha256::digest(wasm)), artifact["sha256"]);
    }
    let (_, quote, collateral) = nearai_cases().remove(0);
    assert_eq!(
        borsh::to_vec(&(quote, collateral)).unwrap(),
        fs::read(root().join("../../fixtures/attest/deepseek-gateway-20261121.borsh")).unwrap()
    );
}

#[tokio::test]
async fn pinned_upstream_accepts_good_quote_and_rejects_flipped_byte() {
    let worker = near_workspaces::sandbox().await.unwrap();
    let verifier = worker
        .dev_deploy(&vendor("tee-verifier-TEST-ONLY-mpc.wasm"))
        .await
        .unwrap();
    let args = vendor("upstream/verify_quote_args.borsh");
    assert!(matches!(
        verify(&verifier, "upstream-good", args.clone()).await,
        VerificationResultWithCollateralDates::Verified { .. }
    ));
    let (mut quote, collateral): (QuoteBytes, Collateral) = borsh::from_slice(&args).unwrap();
    quote.0[60] ^= 1;
    assert!(matches!(
        verify(
            &verifier,
            "upstream-flipped",
            borsh::to_vec(&(quote, collateral)).unwrap()
        )
        .await,
        VerificationResultWithCollateralDates::Rejected(error)
            if error.to_string() == "dcap verification failed: ISV enclave report signature is invalid"
    ));
}

#[tokio::test]
#[ignore = "requires a built replay probe; see docs/lanes/attest-t0.md for build and explicit test commands"]
async fn replay_callback_measurement() {
    let target_dir = std::env::var_os("CARGO_TARGET_DIR").expect(
        "set CARGO_TARGET_DIR and build the probe as documented in docs/lanes/attest-t0.md",
    );
    let probe_wasm =
        PathBuf::from(target_dir).join("wasm32-unknown-unknown/release/attest_replay_probe.wasm");
    let probe_bytes = fs::read(&probe_wasm).unwrap_or_else(|error| {
        panic!(
            "cannot read probe wasm at {}: {error}; build it as documented in docs/lanes/attest-t0.md",
            probe_wasm.display()
        )
    });
    let worker = near_workspaces::sandbox().await.unwrap();
    let verifier = worker
        .dev_deploy(&vendor("tee-verifier-TEST-ONLY-mpc.wasm"))
        .await
        .unwrap();
    let args = vendor("upstream/verify_quote_args.borsh");
    let probe = worker.dev_deploy(&probe_bytes).await.unwrap();
    let tcb: Value = serde_json::from_slice(&vendor("upstream/tcb_info.json")).unwrap();
    let mut events = tcb["event_log"].clone();
    for (label, expected) in [("replay-good", true), ("replay-corrupt", false)] {
        let outcome = probe.call("verify").args_json(json!({"verifier_id": verifier.id(), "input": STANDARD.encode(&args), "events": events}))
            .gas(Gas::from_tgas(300)).transact().await.unwrap();
        record(label, &probe, &outcome, &hex::encode(Sha256::digest(&args)));
        if expected {
            assert!(outcome.receipt_outcomes().iter().any(|receipt| receipt
                .logs
                .iter()
                .any(|log| log == "ATTEST_REPLAY_CALLBACK")));
        }
        assert_eq!(outcome.json::<bool>().unwrap(), expected);
        let event = events
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|event| event["imr"] == 3)
            .unwrap();
        event["digest"] = json!("00".repeat(48));
    }
}

#[tokio::test]
async fn pinned_nearai_verifies_all_recorded_dstack_quotes_and_rejects_mutations() {
    let worker = near_workspaces::sandbox().await.unwrap();
    let verifier = worker
        .dev_deploy(&vendor("tee-verifier-TEST-ONLY-nearai.wasm"))
        .await
        .unwrap();
    for (name, quote, collateral) in nearai_cases() {
        assert!(matches!(
            verify(
                &verifier,
                &name,
                borsh::to_vec(&(quote, collateral)).unwrap()
            )
            .await,
            VerificationResultWithCollateralDates::Verified { .. }
        ));
    }
    let (_, mut quote, collateral) = nearai_cases().remove(1);
    quote.0[60] ^= 1;
    assert!(matches!(
        verify(
            &verifier,
            "nearai-flipped",
            borsh::to_vec(&(quote, collateral)).unwrap()
        )
        .await,
        VerificationResultWithCollateralDates::Rejected(error)
            if error.to_string() == "dcap verification failed: ISV enclave report signature is invalid"
    ));
    let (_, quote, mut collateral) = nearai_cases().remove(3);
    let saved: Value = serde_json::from_slice(
        &fs::read(root().join("../../fixtures/nearai/intel-collateral-90c06f000000.json")).unwrap(),
    )
    .unwrap();
    collateral.pck_certificate_chain =
        Some(saved["pck_certificate_chain"].as_str().unwrap().into());
    let rejected = verify(
        &verifier,
        "nearai-wrong-pck-chain",
        borsh::to_vec(&(quote, collateral)).unwrap(),
    )
    .await;
    let VerificationResultWithCollateralDates::Rejected(error) = rejected else {
        panic!("another machine's PCK chain must be rejected")
    };
    assert!(error
        .to_string()
        .contains("Signature is invalid for qe_report"));
}

#[tokio::test]
#[ignore = "dated mainnet proof expires 2026-12-21 at 14:22:06 UTC; run explicitly before expiry"]
async fn dated_mainnet_wasm_accepts_current_dstack_quote_until_collateral_expiry() {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let expiration = fixture("clock.json")["nearai_earliest_expiration_unix"]
        .as_u64()
        .unwrap();
    assert!(now < expiration, "dated mainnet verifier proof expired at Unix {expiration}; use TEST-ONLY pinned replay coverage");
    let worker = near_workspaces::sandbox().await.unwrap();
    let verifier = worker
        .dev_deploy(&vendor("tee-verifier-mainnet.wasm"))
        .await
        .unwrap();
    for (name, quote, collateral) in nearai_cases() {
        assert!(matches!(
            verify(
                &verifier,
                &format!("dated-mainnet-{name}"),
                borsh::to_vec(&(quote, collateral)).unwrap()
            )
            .await,
            VerificationResultWithCollateralDates::Verified { .. }
        ));
    }
    let (_, mut flipped, collateral) = nearai_cases().remove(0);
    flipped.0[60] ^= 1;
    assert!(matches!(
        verify(
            &verifier,
            "dated-mainnet-flipped",
            borsh::to_vec(&(flipped, collateral)).unwrap()
        )
        .await,
        VerificationResultWithCollateralDates::Rejected(error)
            if error.to_string() == "dcap verification failed: ISV enclave report signature is invalid"
    ));
}
