use near_sdk::serde_json::{json, Value};
use near_sdk::Gas;
use near_workspaces::types::NearToken;

fn hash(letter: char) -> String {
    letter.to_string().repeat(64)
}

#[tokio::test]
async fn registry_records_over_rpc_and_rotates_recorder() {
    let worker = near_workspaces::sandbox().await.unwrap();
    let root = worker.root_account().unwrap();
    assert_eq!(root.id().as_str(), "test.near");

    let owner = root
        .create_subaccount("owner")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let gate = root
        .create_subaccount("gate")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let recorder = gate
        .create_subaccount("recorder")
        .initial_balance(NearToken::from_near(2))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let registry = gate
        .create_subaccount("registry")
        .initial_balance(NearToken::from_near(2))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let wasm = near_workspaces::compile_project(env!("CARGO_MANIFEST_DIR"))
        .await
        .unwrap();
    registry.deploy(&wasm).await.unwrap().into_result().unwrap();
    registry
        .call(registry.id(), "new")
        .args_json(json!({"owner_id": owner.id(), "recorder_id": recorder.id()}))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let records = json!({
        "records": [
            {"subject": "one.test.near", "verdict": "Allow", "evidence_hash": hash('a'), "observed_block": 10, "verdict_id": "one"},
            {"subject": "two.test.near", "verdict": "Deny", "evidence_hash": hash('b'), "observed_block": 11, "verdict_id": "two"},
            {"subject": "three.test.near", "verdict": "Unknown", "evidence_hash": hash('c'), "observed_block": 12, "verdict_id": "three"}
        ]
    });
    recorder
        .call(registry.id(), "record_batch")
        .args_json(records)
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let total: u64 = registry
        .view(registry.id(), "total_records")
        .args_json(json!({}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(total, 3);

    let latest: Value = registry
        .view(registry.id(), "latest")
        .args_json(json!({"subject": "one.test.near"}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(latest["verdict"], "Allow");
    assert_eq!(latest["evidence_hash"], hash('a'));
    assert!(latest["observed_block"].as_u64().is_some());
    assert!(latest["recorded_ms"].as_u64().is_some());
    assert_eq!(latest["verdict_id"], "one");

    let allowed: Value = registry
        .view(registry.id(), "is_allowed")
        .args_json(json!({"subject": "one.test.near", "max_age_ms": 300000}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(allowed, json!([true, "allow"]));

    let count = 29;
    let records = (0..count)
        .map(|index| {
            json!({
                "subject": format!("{}{:02}.test.near", "a".repeat(52), index),
                "verdict": "Allow",
                "evidence_hash": hash('a'),
                "observed_block": index as u64,
                "verdict_id": format!("{}-{index:02}", "v".repeat(60))
            })
        })
        .collect::<Vec<_>>();
    let result = recorder
        .call(registry.id(), "record_batch")
        .args_json(json!({"records": records}))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let log_bytes = result.logs().iter().map(|line| line.len()).sum::<usize>();
    assert_eq!(result.logs().len(), count);
    assert!(log_bytes <= 16_384);
    println!("measured registry batch: records={count}, log_bytes={log_bytes}");

    let refusal = owner
        .call(registry.id(), "record")
        .args_json(json!({
            "subject": "owner.test.near",
            "verdict": "Allow",
            "evidence_hash": hash('a'),
            "observed_block": 13,
            "verdict_id": "owner"
        }))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap_err();
    let refusal_text = refusal.to_string();
    let panic_text = refusal_text
        .strip_prefix("Action #0: ExecutionError(\"")
        .and_then(|text| text.strip_suffix("\")"))
        .unwrap();
    assert_eq!(panic_text, "Smart contract panicked: REFUSED:not_recorder");

    owner
        .call(registry.id(), "set_recorder")
        .args_json(json!({"recorder_id": owner.id()}))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let old_recorder_refusal = recorder
        .call(registry.id(), "record")
        .args_json(json!({
            "subject": "old.test.near",
            "verdict": "Unknown",
            "evidence_hash": hash('a'),
            "observed_block": 14,
            "verdict_id": "old"
        }))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap_err();
    assert!(format!("{old_recorder_refusal:?}").contains("REFUSED:not_recorder"));

    owner
        .call(registry.id(), "record_label")
        .args_json(json!({
            "subject": "one.test.near",
            "chain": "near",
            "label": "catalog_entry",
            "category": "infrastructure",
            "provenance": "official_source",
            "assertion_hash": hash('d')
        }))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let labels: u64 = registry
        .view(registry.id(), "total_labels")
        .args_json(json!({}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(labels, 1);
}
