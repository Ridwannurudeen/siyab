use near_sdk::serde_json::{json, Value};
use near_sdk::Gas;
use near_workspaces::types::NearToken;
use reports::REPORT_DEPOSIT;
use sha2::{Digest, Sha256};

// The deposit of the code in res/previous/reports.wasm (res/BUILD-reports.md); calls made while that
// code is deployed must attach it, since a deposit is exact.
const PREVIOUS_REPORT_DEPOSIT: u128 = 23_136_000_000_000_000_000_000;

fn hash(letter: char) -> String {
    letter.to_string().repeat(64)
}

#[tokio::test]
async fn sandbox_submit_accept_dispute_overturn_updates_subject_summary() {
    let worker = near_workspaces::sandbox().await.unwrap();
    let root = worker.root_account().unwrap();
    let owner = root
        .create_subaccount("owner")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let moderator = root
        .create_subaccount("moderator")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let second_moderator = root
        .create_subaccount("moderator-b")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let reporter = root
        .create_subaccount("reporter")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let disputer = root
        .create_subaccount("disputer")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let contract = root
        .create_subaccount("reports")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let wasm = near_workspaces::compile_project(env!("CARGO_MANIFEST_DIR"))
        .await
        .unwrap();
    contract.deploy(&wasm).await.unwrap().into_result().unwrap();
    contract
        .call(contract.id(), "new")
        .args_json(json!({
            "owner_id": owner.id(),
            "moderators": [moderator.id(), second_moderator.id()]
        }))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let starting_account = contract.view_account().await.unwrap();
    let report_id: u64 = reporter
        .call(contract.id(), "submit_report")
        .args_json(json!({
            "subject": "vendor.test.near",
            "kind": "scam",
            "outcome": "loss",
            "evidence_sha256": hash('a'),
            "evidence_url": "https://evidence.example/report"
        }))
        .deposit(NearToken::from_yoctonear(REPORT_DEPOSIT))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap()
        .json()
        .unwrap();
    moderator
        .call(contract.id(), "moderate")
        .args_json(json!({"id": report_id, "accept": true, "reason_sha256": hash('b')}))
        .deposit(NearToken::from_yoctonear(1))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let dispute_id: u64 = disputer
        .call(contract.id(), "dispute")
        .args_json(json!({
            "report_id": report_id,
            "evidence_sha256": hash('c'),
            "evidence_url": "https://evidence.example/dispute"
        }))
        .deposit(NearToken::from_yoctonear(REPORT_DEPOSIT))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap()
        .json()
        .unwrap();
    let refused_resolution = moderator
        .call(contract.id(), "resolve_dispute")
        .args_json(json!({
            "dispute_id": dispute_id,
            "overturn": true,
            "reason_sha256": hash('d')
        }))
        .deposit(NearToken::from_yoctonear(1))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap_err();
    assert!(refused_resolution
        .to_string()
        .contains("REFUSED:same_moderator"));
    second_moderator
        .call(contract.id(), "resolve_dispute")
        .args_json(json!({
            "dispute_id": dispute_id,
            "overturn": true,
            "reason_sha256": hash('d')
        }))
        .deposit(NearToken::from_yoctonear(1))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let summary: Value = contract
        .view(contract.id(), "subject_summary")
        .args_json(json!({"subject": "vendor.test.near"}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(summary["accepted"], 0);
    assert_eq!(summary["pending"], 0);
    assert_eq!(summary["contested"], 0);
    assert!(summary["latest_accepted_id"].is_null());
    assert!(summary["latest_deny_id"].is_null());
    assert_eq!(summary["deny"], false);

    let report: Value = contract
        .view(contract.id(), "get_report")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(report["state"], "overturned");
    assert_eq!(report["dispute_ids"][0], dispute_id);
    let dispute: Value = contract
        .view(contract.id(), "get_dispute")
        .args_json(json!({"id": dispute_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(dispute["report_id"], report_id);
    assert_eq!(dispute["open"], false);
    let ending_account = contract.view_account().await.unwrap();
    let increase = ending_account.balance.as_yoctonear() + ending_account.locked.as_yoctonear()
        - starting_account.balance.as_yoctonear()
        - starting_account.locked.as_yoctonear();
    // Contract calls add gas rewards to the balance.
    assert!(increase >= 2 * REPORT_DEPOSIT + 2);
    assert!(increase <= 2 * REPORT_DEPOSIT + 2 + 100_000_000_000_000_000_000_000);
}

#[tokio::test]
async fn sandbox_reports_layout_preserving_round_trip() {
    let worker = near_workspaces::sandbox().await.unwrap();
    let root = worker.root_account().unwrap();
    let owner = root
        .create_subaccount("owner")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let moderator = root
        .create_subaccount("moderator")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let reporter = root
        .create_subaccount("reporter")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let disputer = root
        .create_subaccount("disputer")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let contract = root
        .create_subaccount("reports")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let previous_wasm = std::fs::read(manifest_dir.join("../../res/previous/reports.wasm"))
        .unwrap();
    let candidate_wasm = match std::env::var_os("REPORTS_CANDIDATE_WASM") {
        Some(path) => std::fs::read(path).unwrap(),
        None => near_workspaces::compile_project(env!("CARGO_MANIFEST_DIR"))
            .await
            .unwrap(),
    };

    contract
        .deploy(&previous_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    contract
        .call(contract.id(), "new")
        .args_json(json!({
            "owner_id": owner.id(),
            "moderators": [moderator.id()]
        }))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let report_id: u64 = reporter
        .call(contract.id(), "submit_report")
        .args_json(json!({
            "subject": "vendor.test.near",
            "kind": "scam",
            "outcome": "loss",
            "evidence_sha256": hash('a'),
            "evidence_url": "https://evidence.example/report"
        }))
        .deposit(NearToken::from_yoctonear(PREVIOUS_REPORT_DEPOSIT))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap()
        .json()
        .unwrap();
    moderator
        .call(contract.id(), "moderate")
        .args_json(json!({
            "id": report_id,
            "accept": true,
            "reason_sha256": hash('b')
        }))
        .deposit(NearToken::from_yoctonear(1))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    for index in 0..5 {
        let dispute_id: u64 = disputer
            .call(contract.id(), "dispute")
            .args_json(json!({
                "report_id": report_id,
                "evidence_sha256": hash('c'),
                "evidence_url": "https://evidence.example/dispute"
            }))
            .deposit(NearToken::from_yoctonear(PREVIOUS_REPORT_DEPOSIT))
            .gas(Gas::from_tgas(100))
            .transact()
            .await
            .unwrap()
            .into_result()
            .unwrap()
            .json()
            .unwrap();
        assert_eq!(dispute_id, index);
        moderator
            .call(contract.id(), "resolve_dispute")
            .args_json(json!({
                "dispute_id": dispute_id,
                "overturn": false,
                "reason_sha256": hash('d')
            }))
            .deposit(NearToken::from_yoctonear(1))
            .gas(Gas::from_tgas(100))
            .transact()
            .await
            .unwrap()
            .into_result()
            .unwrap();
    }

    contract
        .deploy(&candidate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let accepted: Value = contract
        .view(contract.id(), "get_report")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(accepted["state"], "accepted");
    assert_eq!(accepted["dispute_ids"].as_array().unwrap().len(), 5);

    let sixth_dispute: u64 = disputer
        .call(contract.id(), "dispute")
        .args_json(json!({
            "report_id": report_id,
            "evidence_sha256": hash('e'),
            "evidence_url": "https://evidence.example/sixth-dispute"
        }))
        .deposit(NearToken::from_yoctonear(REPORT_DEPOSIT))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(sixth_dispute, 5);
    let contested: Value = contract
        .view(contract.id(), "get_report")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(contested["state"], "contested");

    contract
        .deploy(&previous_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let rolled_back: Value = contract
        .view(contract.id(), "get_report")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(rolled_back["state"], "contested");
    assert_eq!(rolled_back["dispute_ids"].as_array().unwrap().len(), 6);

    moderator
        .call(contract.id(), "resolve_dispute")
        .args_json(json!({
            "dispute_id": sixth_dispute,
            "overturn": false,
            "reason_sha256": hash('f')
        }))
        .deposit(NearToken::from_yoctonear(1))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let accepted_again: Value = contract
        .view(contract.id(), "get_report")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(accepted_again["state"], "accepted");
}

#[tokio::test]
async fn sandbox_reports_threshold_round_trip() {
    let worker = near_workspaces::sandbox().await.unwrap();
    let root = worker.root_account().unwrap();
    let owner = root
        .create_subaccount("owner")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let moderator = root
        .create_subaccount("moderator")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let second_moderator = root
        .create_subaccount("moderator-b")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let reporter = root
        .create_subaccount("reporter")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let contract = root
        .create_subaccount("reports")
        .initial_balance(NearToken::from_near(10))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let previous_wasm = std::fs::read(manifest_dir.join("../../res/reports.wasm")).unwrap();
    let expected_checksum =
        std::fs::read_to_string(manifest_dir.join("../../res/reports.wasm.sha256")).unwrap();
    let digest = Sha256::digest(&previous_wasm);
    let digest_hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    assert_eq!(
        expected_checksum.trim_end(),
        format!("{digest_hex}  res/reports.wasm")
    );
    let candidate_wasm = match std::env::var_os("REPORTS_CANDIDATE_WASM") {
        Some(path) => std::fs::read(path).unwrap(),
        None => near_workspaces::compile_project(env!("CARGO_MANIFEST_DIR"))
            .await
            .unwrap(),
    };

    contract
        .deploy(&previous_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    contract
        .call(contract.id(), "new")
        .args_json(json!({
            "owner_id": owner.id(),
            "moderators": [moderator.id()]
        }))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    owner
        .call(contract.id(), "set_moderators")
        .args_json(json!({"moderators": [moderator.id(), second_moderator.id()]}))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let report_id: u64 = reporter
        .call(contract.id(), "submit_report")
        .args_json(json!({
            "subject": "vendor.test.near",
            "kind": "scam",
            "outcome": "loss",
            "evidence_sha256": hash('a'),
            "evidence_url": "https://evidence.example/report"
        }))
        .deposit(NearToken::from_yoctonear(REPORT_DEPOSIT))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap()
        .json()
        .unwrap();

    contract
        .deploy(&candidate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let before_vote: Value = contract
        .view(contract.id(), "get_report")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(before_vote["state"], "pending");
    owner
        .call(contract.id(), "set_approvals_required")
        .args_json(json!({"required": 2}))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    moderator
        .call(contract.id(), "moderate")
        .args_json(json!({
            "id": report_id,
            "accept": true,
            "reason_sha256": hash('b')
        }))
        .deposit(NearToken::from_yoctonear(1))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let pending: Value = contract
        .view(contract.id(), "get_report")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(pending["state"], "pending");
    let first_votes: Value = contract
        .view(contract.id(), "report_votes")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(first_votes.as_array().unwrap().len(), 1);
    assert_eq!(first_votes[0]["moderator"], moderator.id().as_str());
    assert_eq!(first_votes[0]["side"], "accept");
    assert_eq!(first_votes[0]["reason_sha256"], hash('b'));

    contract
        .deploy(&previous_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let rolled_back: Value = contract
        .view(contract.id(), "get_report")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(rolled_back["state"], "pending");
    second_moderator
        .call(contract.id(), "moderate")
        .args_json(json!({
            "id": report_id,
            "accept": true,
            "reason_sha256": hash('c')
        }))
        .deposit(NearToken::from_yoctonear(1))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    contract
        .deploy(&candidate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let accepted: Value = contract
        .view(contract.id(), "get_report")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(accepted["state"], "accepted");
    assert_eq!(accepted["moderator"], second_moderator.id().as_str());
    let stale_votes: Value = contract
        .view(contract.id(), "report_votes")
        .args_json(json!({"id": report_id}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(stale_votes.as_array().unwrap().len(), 1);
    assert_eq!(stale_votes[0]["moderator"], moderator.id().as_str());
}
