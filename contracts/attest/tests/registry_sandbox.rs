use near_workspaces::{
    network::Sandbox,
    result::{ExecutionFinalResult, ValueOrReceiptId},
    types::{Gas, NearToken},
    Contract, Worker,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256, Sha384};
use std::{fs, path::PathBuf};
use tee_verifier_interface::{
    Collateral, CollateralDates, QuoteBytes, Report, TDReport10, TDReport15, TcbStatus,
    TcbStatusWithAdvisory, VerificationResultWithCollateralDates, VerifiedReport, VerifierError,
};

const DEPOSIT: u128 = 10_000_000_000_000_000_000_000;
const KEY: [u8; 32] = [7; 32];
const LEASE: u64 = 86_400_000;

fn wasm(name: &str) -> Vec<u8> {
    let target = std::env::var_os("CARGO_TARGET_DIR").expect("CARGO_TARGET_DIR must name the external directory containing built registry and TEST ONLY mock wasm");
    let relative = if name == "attest" {
        "near/attest.wasm".into()
    } else {
        format!("wasm32-unknown-unknown/release/{name}.wasm")
    };
    fs::read(PathBuf::from(target).join(relative)).unwrap()
}

fn vendor(name: &str) -> Vec<u8> {
    fs::read(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/vendor")
            .join(name),
    )
    .unwrap()
}

fn collateral() -> Collateral {
    borsh::from_slice::<(QuoteBytes, Collateral)>(&vendor("upstream/verify_quote_args.borsh"))
        .unwrap()
        .1
}

async fn setup(real: bool) -> (Worker<Sandbox>, Contract, Contract) {
    let worker = near_workspaces::sandbox().await.unwrap();
    let verifier = worker
        .dev_deploy(&if real {
            vendor("tee-verifier-TEST-ONLY-mpc.wasm")
        } else {
            wasm("attest_test_verifier")
        })
        .await
        .unwrap();
    let registry = worker.dev_deploy(&wasm("attest")).await.unwrap();
    let config = json!({"verifier_account_id": verifier.id(), "signer_account_id": "v1.signer", "ed25519_domain_id": 7, "max_lease_ms": LEASE, "vote_recipients": ["outlayer.near"]});
    registry
        .call("new")
        .args_json(config.clone())
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    assert_eq!(
        registry
            .view("get_config")
            .args_json(json!({}))
            .await
            .unwrap()
            .json::<Value>()
            .unwrap(),
        config
    );
    (worker, registry, verifier)
}

async fn now(worker: &Worker<Sandbox>) -> u64 {
    worker.view_block().await.unwrap().timestamp() / 1_000_000
}

fn events(compose: &str) -> (Value, [u8; 48]) {
    let mut register = [0u8; 48];
    let mut events = Vec::new();
    for (name, payload) in [
        ("compose-hash", Sha256::digest(compose.as_bytes()).to_vec()),
        ("key-provider", b"TEST ONLY provider".to_vec()),
    ] {
        let mut digest = Sha384::new();
        digest.update(134217729u32.to_le_bytes());
        digest.update(b":");
        digest.update(name.as_bytes());
        digest.update(b":");
        digest.update(&payload);
        let digest = digest.finalize();
        let mut extend = Sha384::new();
        extend.update(register);
        extend.update(digest);
        register = extend.finalize().into();
        events.push(json!({"imr": 3, "event_type": 134217729, "digest": hex::encode(digest), "event": name, "event_payload": hex::encode(payload)}));
    }
    (json!(events), register)
}

fn candidate(registry: &Contract, issued: u64) -> (Value, VerificationResultWithCollateralDates) {
    let compose = "{\"services\": {\"siyab\": {}}}\n";
    let (events, rt_mr3) = events(compose);
    let td = TDReport10 {
        tee_tcb_svn: [0; 16],
        mr_seam: [0; 48],
        mr_signer_seam: [0; 48],
        seam_attributes: [0; 8],
        td_attributes: 0x10000000u64.to_le_bytes(),
        xfam: [0; 8],
        mr_td: [1; 48],
        mr_config_id: [0; 48],
        mr_owner: [0; 48],
        mr_owner_config: [0; 48],
        rt_mr0: [2; 48],
        rt_mr1: [3; 48],
        rt_mr2: [4; 48],
        rt_mr3,
        report_data: attest::report_data(registry.id().as_str(), &KEY, issued, &[0; 32]),
    };
    let status = TcbStatusWithAdvisory {
        status: TcbStatus::UpToDate,
        advisory_ids: vec![],
    };
    let result = VerificationResultWithCollateralDates::Verified {
        report: VerifiedReport {
            status: "UpToDate".into(),
            advisory_ids: vec!["TEST-ADVISORY".into()],
            report: Report::TD10(td),
            ppid: vec![9; 16],
            qe_status: status.clone(),
            platform_status: status,
        },
        collateral_dates: CollateralDates {
            earliest_issue_date: 0,
            latest_issue_date: 0,
            earliest_expiration_date: issued / 1000 + 172800,
            qe_iden_earliest_issue_date: 0,
            qe_iden_latest_issue_date: 0,
            qe_iden_earliest_expiration_date: issued / 1000 + 172800,
        },
    };
    (
        json!({"public_key": hex::encode(KEY), "issued_at_ms": issued, "aux": "00".repeat(32), "verification_args": "", "app_compose": compose, "event_log": events}),
        result,
    )
}

fn set_output(args: &mut Value, output: Vec<u8>, mode: u8, delay: u8) {
    args["verification_args"] = json!(hex::encode(
        borsh::to_vec(&(
            QuoteBytes(borsh::to_vec(&(mode, delay, output)).unwrap()),
            collateral()
        ))
        .unwrap()
    ));
}

fn encode(args: &mut Value, result: &VerificationResultWithCollateralDates) {
    set_output(args, borsh::to_vec(result).unwrap(), 0, 0);
}

async fn register(registry: &Contract, args: Value) -> ExecutionFinalResult {
    registry
        .call("register_key")
        .args_json(args)
        .deposit(NearToken::from_yoctonear(DEPOSIT))
        .gas(Gas::from_tgas(300))
        .transact()
        .await
        .unwrap()
}

async fn stored(registry: &Contract) -> Value {
    registry
        .view("get_key")
        .args_json(json!({"public_key": hex::encode(KEY)}))
        .await
        .unwrap()
        .json()
        .unwrap()
}

#[tokio::test]
async fn registry_mock_success_refund_and_gas_snapshot() {
    let (worker, registry, verifier) = setup(false).await;
    let issued = now(&worker).await - 1000;
    let (mut args, result) = candidate(&registry, issued);
    encode(&mut args, &result);
    let outcome = register(&registry, args).await;
    let view: Value = outcome.clone().json().unwrap();
    println!(
        "ATTEST_T1_GAS {}",
        json!({"case": "mock-registration", "gas": outcome.total_gas_burnt.as_gas(), "receipts": outcome.receipt_outcomes().iter().map(|r| json!({"executor": r.executor_id, "gas": r.gas_burnt.as_gas()})).collect::<Vec<_>>()})
    );
    assert_eq!(view["issued_at_ms"], issued);
    assert_eq!(view["expires_ms"], issued + LEASE);
    assert!(view["registered_ms"].as_u64().unwrap() >= issued);
    assert_eq!(view["advisory_ids"], json!(["TEST-ADVISORY"]));
    assert_eq!(stored(&registry).await, view);
    let balance = registry
        .view_account()
        .await
        .unwrap()
        .balance
        .as_yoctonear();
    let outcome = verifier.as_account().call(registry.id(), "register_key")
        .args_json(json!({"public_key": "INVALID", "issued_at_ms": issued, "aux": "00".repeat(32), "verification_args": "", "app_compose": "", "event_log": []}))
        .deposit(NearToken::from_yoctonear(DEPOSIT)).gas(Gas::from_tgas(300)).transact().await.unwrap();
    assert!(outcome.clone().json::<Value>().unwrap().is_null());
    let after = registry
        .view_account()
        .await
        .unwrap()
        .balance
        .as_yoctonear();
    assert!(after.abs_diff(balance) < DEPOSIT / 2);
    println!(
        "ATTEST_T1_GAS {}",
        json!({"case": "ingress-refusal", "gas": outcome.total_gas_burnt.as_gas()})
    );
}

#[tokio::test]
async fn registry_mock_refuses_verified_report_and_clock_mutations() {
    let (worker, registry, _) = setup(false).await;
    let issued = now(&worker).await - 1000;
    for case in [
        "debug",
        "binding",
        "td15",
        "status",
        "ppid",
        "stale",
        "future",
        "expired",
        "malformed",
        "failed",
        "rejected",
        "replay",
        "compose",
    ] {
        let time = match case {
            "stale" => issued - 7_200_001,
            "future" => issued + 86_400_000,
            _ => issued,
        };
        let (mut args, mut result) = candidate(&registry, time);
        if let VerificationResultWithCollateralDates::Verified {
            report,
            collateral_dates,
        } = &mut result
        {
            match case {
                "debug" => {
                    if let Report::TD10(td) = &mut report.report {
                        td.td_attributes[0] |= 1;
                    }
                }
                "binding" => {
                    if let Report::TD10(td) = &mut report.report {
                        td.report_data = attest::report_data("wrong.testnet", &KEY, time, &[0; 32]);
                    }
                }
                "td15" => {
                    if let Report::TD10(td) = &report.report {
                        report.report = Report::TD15(TDReport15 {
                            base: td.clone(),
                            tee_tcb_svn2: [0; 16],
                            mr_service_td: [0; 48],
                        });
                    }
                }
                "status" => report.status = "OutOfDate".into(),
                "ppid" => report.ppid.pop().map(|_| ()).unwrap(),
                "expired" => collateral_dates.earliest_expiration_date = issued / 1000 - 1,
                "replay" => args["event_log"][0]["digest"] = json!("00".repeat(48)),
                "compose" => args["app_compose"] = json!("changed"),
                _ => (),
            }
        }
        if case == "rejected" {
            result = VerificationResultWithCollateralDates::Rejected(
                VerifierError::DcapVerification("TEST ONLY rejection".into()),
            );
        }
        encode(&mut args, &result);
        if case == "malformed" {
            set_output(&mut args, vec![255], 0, 0);
        }
        if case == "failed" {
            set_output(&mut args, vec![], 1, 0);
        }
        let outcome = register(&registry, args).await;
        assert!(
            outcome.clone().json::<Value>().unwrap().is_null(),
            "{case}: {outcome:?}"
        );
        assert!(stored(&registry).await.is_null(), "{case}");
    }
}

#[tokio::test]
async fn registry_ingress_budgets_envelopes_and_private_callback() {
    let (worker, registry, verifier) = setup(false).await;
    assert!(registry
        .view("get_config")
        .args_json(json!({"extra":0}))
        .await
        .is_err());
    assert!(registry
        .view("get_key")
        .args_json(json!({"public_key":hex::encode(KEY),"extra":0}))
        .await
        .is_err());
    assert!(registry
        .view("get_key")
        .args_json(json!({"public_key":"AA".repeat(32)}))
        .await
        .is_err());
    let (mut good, result) = candidate(&registry, now(&worker).await - 1000);
    encode(&mut good, &result);
    for (deposit, gas) in [(DEPOSIT - 1, 300), (DEPOSIT + 1, 300), (DEPOSIT, 299)] {
        let outcome = registry
            .call("register_key")
            .args_json(good.clone())
            .deposit(NearToken::from_yoctonear(deposit))
            .gas(Gas::from_tgas(gas))
            .transact()
            .await
            .unwrap();
        assert!(outcome.is_failure());
        assert!(!outcome
            .logs()
            .iter()
            .any(|log| log.contains("ATTEST_TEST_ONLY_VERIFIER")));
    }
    for case in [
        "extra",
        "uppercase",
        "trailing",
        "compose-bound",
        "events-bound",
        "event-extra",
        "event-array",
    ] {
        let mut args = good.clone();
        match case {
            "extra" => args["extra"] = json!(true),
            "uppercase" => args["aux"] = json!("AA".repeat(32)),
            "trailing" => {
                args["verification_args"] =
                    json!(format!("{}00", args["verification_args"].as_str().unwrap()))
            }
            "compose-bound" => args["app_compose"] = json!("x".repeat(65537)),
            "events-bound" => args["event_log"] = json!(vec![args["event_log"][0].clone(); 65]),
            "event-extra" => args["event_log"][0]["extra"] = json!(0),
            "event-array" => {
                let event = &args["event_log"][0];
                args["event_log"][0] = json!([
                    event["imr"],
                    event["event_type"],
                    event["digest"],
                    event["event"],
                    event["event_payload"]
                ]);
            }
            _ => unreachable!(),
        }
        let outcome = register(&registry, args).await;
        assert!(
            outcome.is_failure() || outcome.clone().json::<Value>().unwrap().is_null(),
            "{case}"
        );
        assert!(
            !outcome
                .logs()
                .iter()
                .any(|log| log.contains("ATTEST_TEST_ONLY_VERIFIER")),
            "{case}"
        );
    }
    let context = attest::RegisterContext {
        public_key: KEY,
        issued_at_ms: good["issued_at_ms"].as_u64().unwrap(),
        aux: [0; 32],
        app_compose: good["app_compose"].as_str().unwrap().into(),
        event_log: serde_json::from_value(good["event_log"].clone()).unwrap(),
        relayer: verifier.id().clone(),
        deposit: DEPOSIT,
    };
    let spoof = verifier
        .as_account()
        .call(registry.id(), "on_quote_verified")
        .args(borsh::to_vec(&context).unwrap())
        .gas(Gas::from_tgas(60))
        .transact()
        .await
        .unwrap();
    assert!(spoof.is_failure());
    let failure = format!("{:?}", spoof.into_result().unwrap_err());
    assert!(
        failure.contains("Method on_quote_verified is private"),
        "{failure}"
    );
    assert!(stored(&registry).await.is_null());
}

#[tokio::test]
async fn registry_renewal_preserves_newest_record_in_both_completion_orders() {
    let (worker, registry, _) = setup(false).await;
    let issued = now(&worker).await - 5000;
    for (time, accepted) in [
        (issued, true),
        (issued + 1, true),
        (issued, false),
        (issued + 1, false),
        (issued + 2, true),
    ] {
        let before = stored(&registry).await;
        let (mut args, result) = candidate(&registry, time);
        encode(&mut args, &result);
        let returned = register(&registry, args).await.json::<Value>().unwrap();
        if accepted {
            assert_eq!(returned["issued_at_ms"], time);
        } else {
            assert!(returned.is_null());
            assert_eq!(stored(&registry).await, before);
        }
    }
    let before = stored(&registry).await;
    for case in ["machine", "measurement", "aux"] {
        let (mut args, mut result) = candidate(&registry, issued + 3);
        if let VerificationResultWithCollateralDates::Verified { report, .. } = &mut result {
            match case {
                "machine" => report.ppid[0] ^= 1,
                "measurement" => {
                    if let Report::TD10(td) = &mut report.report {
                        td.mr_td[0] ^= 1;
                    }
                }
                "aux" => {
                    args["aux"] = json!("01".repeat(32));
                    if let Report::TD10(td) = &mut report.report {
                        td.report_data =
                            attest::report_data(registry.id().as_str(), &KEY, issued + 3, &[1; 32]);
                    }
                }
                _ => unreachable!(),
            }
        }
        encode(&mut args, &result);
        assert!(register(&registry, args)
            .await
            .json::<Value>()
            .unwrap()
            .is_null());
        assert_eq!(stored(&registry).await, before);
    }
}

#[tokio::test]
async fn genuine_vendored_verifier_cannot_register_unbound_quote() {
    let (worker, registry, verifier) = setup(true).await;
    let raw = vendor("upstream/verify_quote_args.borsh");
    let tcb: Value = serde_json::from_slice(&vendor("upstream/tcb_info.json")).unwrap();
    let issued = now(&worker).await - 1000;
    let (mut args, _) = candidate(&registry, issued);
    args["verification_args"] = json!(hex::encode(raw));
    args["app_compose"] = tcb["app_compose"].clone();
    args["event_log"] = tcb["event_log"].clone();
    let outcome = register(&registry, args).await;
    assert!(outcome.failures().is_empty(), "{outcome:?}");
    let receipts: Vec<_> = outcome
        .receipt_outcomes()
        .iter()
        .filter(|receipt| receipt.executor_id == *verifier.id())
        .collect();
    assert_eq!(receipts.len(), 1, "{outcome:?}");
    let ValueOrReceiptId::Value(checked) = receipts[0].clone().into_result().unwrap() else {
        panic!("genuine verifier must return its Borsh result to the registry callback")
    };
    assert!(checked.raw_bytes().unwrap().len() <= 16_384);
    let VerificationResultWithCollateralDates::Verified { report, .. } = checked.borsh().unwrap()
    else {
        panic!("genuine verifier must accept the quote before the registry binding check")
    };
    let Report::TD10(td) = report.report else {
        panic!("genuine verifier must return exactly TD10 before the registry binding check")
    };
    assert_eq!(report.status, "UpToDate");
    attest::validate_td_attributes(td.td_attributes).unwrap();
    assert_ne!(
        td.report_data,
        attest::report_data(registry.id().as_str(), &KEY, issued, &[0; 32]),
        "report data mismatch must be the first failed verified report check"
    );
    attest::replay::replay_rtmr3(
        tcb["app_compose"].as_str().unwrap(),
        &serde_json::from_value::<Vec<attest::ReplayEvent>>(tcb["event_log"].clone()).unwrap(),
        &td.rt_mr3,
    )
    .unwrap();
    assert_eq!(report.ppid.len(), 16);
    let callback_values: Vec<_> = outcome
        .receipt_outcomes()
        .iter()
        .filter(|receipt| receipt.executor_id == *registry.id())
        .filter_map(|receipt| match receipt.clone().into_result().unwrap() {
            ValueOrReceiptId::Value(value) => Some(value.raw_bytes().unwrap()),
            ValueOrReceiptId::ReceiptId(_) => None,
        })
        .filter(|bytes| !bytes.is_empty())
        .collect();
    assert_eq!(callback_values, vec![b"null".to_vec()]);
    assert!(outcome.clone().json::<Value>().unwrap().is_null());
    assert!(stored(&registry).await.is_null());
    println!(
        "ATTEST_T1_GAS {}",
        json!({"case": "genuine-verifier-binding-refusal", "gas": outcome.total_gas_burnt.as_gas()})
    );
}

#[tokio::test]
async fn registry_concurrent_older_quote_cannot_overwrite_newer_callback() {
    for older_delayed in [true, false] {
        let (worker, registry, _) = setup(false).await;
        let issued = now(&worker).await - 5000;
        let (mut older, old_result) = candidate(&registry, issued);
        let (mut newer, new_result) = candidate(&registry, issued + 1);
        set_output(
            &mut older,
            borsh::to_vec(&old_result).unwrap(),
            0,
            if older_delayed { 16 } else { 0 },
        );
        set_output(
            &mut newer,
            borsh::to_vec(&new_result).unwrap(),
            0,
            if older_delayed { 0 } else { 16 },
        );
        let delayed = if older_delayed {
            older.clone()
        } else {
            newer.clone()
        };
        let pending = registry
            .call("register_key")
            .args_json(delayed)
            .deposit(NearToken::from_yoctonear(DEPOSIT))
            .gas(Gas::from_tgas(300))
            .transact_async()
            .await
            .unwrap();
        let immediate = register(&registry, if older_delayed { newer } else { older }).await;
        assert!(!immediate.json::<Value>().unwrap().is_null());
        let completed = pending.await.unwrap().json::<Value>().unwrap();
        if older_delayed {
            assert!(completed.is_null());
        } else {
            assert_eq!(completed["issued_at_ms"], issued + 1);
        }
        assert_eq!(stored(&registry).await["issued_at_ms"], issued + 1);
    }
}

#[tokio::test]
async fn registry_maximum_bounded_evidence_gas_snapshot() {
    let (worker, registry, _) = setup(false).await;
    let (mut args, mut result) = candidate(&registry, now(&worker).await - 1000);
    let compose = "x".repeat(65536);
    let (mut event_log, _) = events(&compose);
    let rows = event_log.as_array_mut().unwrap();
    for _ in 2..64 {
        rows.push(json!({"imr":3, "event_type":134217729, "digest":"00".repeat(48), "event":"filler", "event_payload":"00".repeat(150)}));
    }
    let size: usize = rows
        .iter()
        .map(|e| {
            e["digest"].as_str().unwrap().len()
                + e["event"].as_str().unwrap().len()
                + e["event_payload"].as_str().unwrap().len()
        })
        .sum();
    let extra = 32768 - size;
    assert_eq!(extra % 2, 0);
    let last = rows.last_mut().unwrap();
    last["event_payload"] = json!(format!(
        "{}{}",
        last["event_payload"].as_str().unwrap(),
        "00".repeat(extra / 2)
    ));
    let mut register = [0; 48];
    for row in rows {
        let mut hash = Sha384::new();
        hash.update(134217729u32.to_le_bytes());
        hash.update(b":");
        hash.update(row["event"].as_str().unwrap().as_bytes());
        hash.update(b":");
        hash.update(hex::decode(row["event_payload"].as_str().unwrap()).unwrap());
        let digest = hash.finalize();
        row["digest"] = json!(hex::encode(digest));
        let mut hash = Sha384::new();
        hash.update(register);
        hash.update(digest);
        register = hash.finalize().into();
    }
    if let VerificationResultWithCollateralDates::Verified { report, .. } = &mut result {
        if let Report::TD10(td) = &mut report.report {
            td.rt_mr3 = register;
        }
    }
    args["event_log"] = event_log;
    args["app_compose"] = json!(compose);
    let issued = args["issued_at_ms"].as_u64().unwrap();
    for (index, case) in [
        "maximum-bounded-evidence",
        "maximum-raw-whitespace",
        "maximum-raw-unicode-escapes",
    ]
    .into_iter()
    .enumerate()
    {
        let issued = issued + index as u64;
        args["issued_at_ms"] = json!(issued);
        if let VerificationResultWithCollateralDates::Verified { report, .. } = &mut result {
            if let Report::TD10(td) = &mut report.report {
                td.report_data =
                    attest::report_data(registry.id().as_str(), &KEY, issued, &[0; 32]);
            }
        }
        let quote =
            QuoteBytes(borsh::to_vec(&(0u8, 0u8, borsh::to_vec(&result).unwrap())).unwrap());
        let mut collateral = collateral();
        let length = borsh::to_vec(&(quote.clone(), collateral.clone()))
            .unwrap()
            .len();
        collateral
            .root_ca_crl
            .resize(collateral.root_ca_crl.len() + 65536 - length, 0);
        let raw = borsh::to_vec(&(quote, collateral)).unwrap();
        assert_eq!(raw.len(), 65536);
        args["verification_args"] = json!(hex::encode(raw));
        let compact = serde_json::to_vec(&args).unwrap();
        let mut wire = compact.clone();
        if case == "maximum-raw-unicode-escapes" {
            let mut extra = 262144 - compact.len();
            let mut in_string = false;
            let mut escaped = false;
            wire.clear();
            for byte in compact {
                if escaped {
                    wire.push(byte);
                    escaped = false;
                } else if in_string && byte == b'\\' {
                    wire.push(byte);
                    escaped = true;
                } else if byte == b'"' {
                    wire.push(byte);
                    in_string = !in_string;
                } else if in_string && byte.is_ascii_hexdigit() && extra >= 5 {
                    wire.extend_from_slice(format!("\\u{byte:04x}").as_bytes());
                    extra -= 5;
                } else {
                    wire.push(byte);
                }
            }
            assert!(extra < 5);
        }
        if index > 0 {
            wire.resize(262144, b' ');
            assert_eq!(wire.len(), 262144);
        }
        assert_eq!(serde_json::from_slice::<Value>(&wire).unwrap(), args);
        if case == "maximum-raw-whitespace" {
            let mut oversized = wire.clone();
            oversized.push(b' ');
            let refused = registry
                .call("register_key")
                .args(oversized)
                .deposit(NearToken::from_yoctonear(DEPOSIT))
                .gas(Gas::from_tgas(300))
                .transact()
                .await
                .unwrap();
            assert!(refused.clone().json::<Value>().unwrap().is_null());
            assert!(!refused
                .logs()
                .iter()
                .any(|log| log.contains("ATTEST_TEST_ONLY_VERIFIER")));
        }
        let bytes = wire.len();
        let outcome = registry
            .call("register_key")
            .args(wire)
            .deposit(NearToken::from_yoctonear(DEPOSIT))
            .gas(Gas::from_tgas(300))
            .transact()
            .await
            .unwrap();
        let view = outcome.clone().json::<Value>().unwrap();
        assert_eq!(view["issued_at_ms"], issued, "{case}: {outcome:?}");
        let receipts = outcome.receipt_outcomes();
        assert!(
            receipts[0].gas_burnt.as_gas() < 39_000_000_000_000,
            "{case}: ingress gas"
        );
        assert!(
            receipts
                .iter()
                .skip(1)
                .filter(|receipt| receipt.executor_id == *registry.id())
                .all(|receipt| receipt.gas_burnt.as_gas() < 30_000_000_000_000),
            "{case}: callback gas"
        );
        println!(
            "ATTEST_T1_GAS {}",
            json!({"case":case, "input_bytes":bytes, "gas":outcome.total_gas_burnt.as_gas(), "receipts":outcome.receipt_outcomes().iter().map(|r| json!({"executor":r.executor_id, "gas":r.gas_burnt.as_gas()})).collect::<Vec<_>>()})
        );
    }
}
