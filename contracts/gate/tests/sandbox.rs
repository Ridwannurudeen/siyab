use ed25519_dalek::{Signer, SigningKey};
use near_sdk::serde_json::{json, Value};
use near_workspaces::{
    types::{Gas, KeyType, NearToken, PublicKey, SecretKey},
    Account,
};
use sha2::{Digest, Sha256};
use std::{
    fs,
    str::FromStr,
    sync::atomic::{AtomicU64, Ordering},
};

static FRESH_REQUEST_COUNTER: AtomicU64 = AtomicU64::new(0);

fn base58_encode(bytes: &[u8]) -> String {
    const ALPHABET: &[u8] = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
    let zeros = bytes.iter().take_while(|byte| **byte == 0).count();
    let mut digits = vec![0u8];
    for byte in bytes {
        let mut carry = u32::from(*byte);
        for digit in &mut digits {
            carry += u32::from(*digit) << 8;
            *digit = (carry % 58) as u8;
            carry /= 58;
        }
        while carry > 0 {
            digits.push((carry % 58) as u8);
            carry /= 58;
        }
    }
    let mut output = vec![b'1'; zeros];
    output.extend(digits.iter().rev().map(|digit| ALPHABET[*digit as usize]));
    String::from_utf8(output).unwrap()
}

fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn agent_keys() -> (PublicKey, SecretKey) {
    let seed: [u8; 32] = Sha256::digest(b"gate test agent v1").into();
    let signing_key = SigningKey::from_bytes(&seed);
    let public_bytes = signing_key.verifying_key().to_bytes();
    let public_key = PublicKey::try_from_parts(KeyType::ED25519, &public_bytes).unwrap();
    let mut secret_bytes = Vec::with_capacity(64);
    secret_bytes.extend_from_slice(&seed);
    secret_bytes.extend_from_slice(&public_bytes);
    let secret_key =
        SecretKey::from_str(&format!("ed25519:{}", base58_encode(&secret_bytes))).unwrap();
    assert_eq!(secret_key.public_key(), public_key);
    (public_key, secret_key)
}

fn vector(name: &str) -> Value {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../fixtures/canon")
        .join(format!("{name}.json"));
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn transfer_args(vector: &Value) -> Value {
    json!({
        "request": {
            "kind": vector["kind"],
            "token_id": vector["token_id"],
            "receiver_id": vector["receiver_id"],
            "amount": vector["amount"],
            "memo": vector["memo"]
        },
        "verdict": {
            "literal": vector["literal"],
            "issued_ms": vector["issued_ms"],
            "expires_ms": vector["expires_ms"],
            "verdict_id": vector["verdict_id"],
            "evidence_hash": vector["evidence_sha256"],
            "key_id": vector["key_id"],
            "signature": vector["signature_hex"]
        }
    })
}

async fn fresh_request(
    worker: &near_workspaces::Worker<near_workspaces::network::Sandbox>,
    gate_id: &near_workspaces::types::AccountId,
    mut request: Value,
    ttl_ms: u64,
) -> Value {
    let issued_ms = worker.view_block().await.unwrap().timestamp() / 1_000_000;
    let expires_ms = issued_ms + ttl_ms;
    request["issued_ms"] = json!(issued_ms);
    request["expires_ms"] = json!(expires_ms);
    let verdict_id = request["verdict_id"].as_str().unwrap().to_owned();
    let request_number = FRESH_REQUEST_COUNTER.fetch_add(1, Ordering::Relaxed);
    request["verdict_id"] = json!(format!("{}{:016x}", &verdict_id[..16], request_number));

    let token_id = request["token_id"].as_str().unwrap_or("-");
    let memo = request["memo"]
        .as_str()
        .map(|memo| hex_encode(&Sha256::digest(memo.as_bytes())))
        .unwrap_or_else(|| "-".to_owned());
    let message = format!(
        "GATEv1|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}",
        gate_id,
        request["agent_pk_hex"].as_str().unwrap(),
        request["kind"].as_str().unwrap(),
        token_id,
        request["receiver_id"].as_str().unwrap(),
        request["amount"].as_str().unwrap(),
        memo,
        issued_ms,
        expires_ms,
        request["verdict_id"].as_str().unwrap(),
        request["evidence_sha256"].as_str().unwrap(),
        request["key_id"].as_str().unwrap(),
        request["literal"].as_str().unwrap(),
    );
    let verifier_seed: [u8; 32] = Sha256::digest(b"gate test verifier v1").into();
    let verifier = SigningKey::from_bytes(&verifier_seed);
    request["signature_hex"] = json!(hex_encode(
        &verifier
            .sign(&Sha256::digest(message.as_bytes()))
            .to_bytes()
    ));
    request
}

async fn balance(account: &Account, token: &near_workspaces::types::AccountId) -> u128 {
    account
        .view(token, "ft_balance_of")
        .args_json(json!({"account_id": account.id()}))
        .await
        .unwrap()
        .json::<near_sdk::json_types::U128>()
        .unwrap()
        .0
}

#[tokio::test]
async fn sandbox_transfer_policy_and_protocol_boundary() {
    let worker = near_workspaces::sandbox().await.unwrap();
    let root = worker.root_account().unwrap();
    assert_eq!(root.id().as_str(), "test.near");
    let owner = root
        .create_subaccount("owner")
        .initial_balance(NearToken::from_near(20))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let gate = root
        .create_subaccount("gate")
        .initial_balance(NearToken::from_near(20))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let vendor = root
        .create_subaccount("vendor")
        .initial_balance(NearToken::from_near(5))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let stranger = root
        .create_subaccount("stranger")
        .initial_balance(NearToken::from_near(5))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let fresh = root
        .create_subaccount("fresh")
        .initial_balance(NearToken::from_near(5))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let token = gate
        .create_subaccount("token")
        .initial_balance(NearToken::from_near(5))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let previous_gate_path =
        std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../res/previous/gate.wasm");
    let old_gate_wasm = fs::read(&previous_gate_path).unwrap();
    let old_gate_sha256 = format!(
        "{}  res/previous/gate.wasm",
        hex_encode(&Sha256::digest(&old_gate_wasm))
    );
    assert_eq!(
        fs::read_to_string(
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../res/previous/gate.wasm.sha256")
        )
        .unwrap()
        .trim(),
        old_gate_sha256
    );
    let gate_wasm = match std::env::var_os("GATE_CANDIDATE_WASM") {
        Some(path) => fs::read(path).unwrap(),
        None => near_workspaces::compile_project(env!("CARGO_MANIFEST_DIR"))
            .await
            .unwrap(),
    };
    let token_path = format!("{}/../test-token", env!("CARGO_MANIFEST_DIR"));
    let token_wasm = near_workspaces::compile_project(&token_path).await.unwrap();
    token
        .deploy(&token_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    token
        .call(token.id(), "new")
        .args_json(json!({
            "owner_id": owner.id(),
            "total_supply": "1000000000",
            "metadata": {
                "spec": "ft-1.0.0",
                "name": "ShieldBot test token",
                "symbol": "SBT",
                "icon": null,
                "reference": null,
                "reference_hash": null,
                "decimals": 6
            }
        }))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    for account_id in [gate.id(), vendor.id(), stranger.id()] {
        owner
            .call(token.id(), "storage_deposit")
            .args_json(json!({"account_id": account_id, "registration_only": false}))
            .deposit(NearToken::from_near(1))
            .transact()
            .await
            .unwrap()
            .into_result()
            .unwrap();
    }
    owner
        .call(token.id(), "ft_transfer")
        .args_json(json!({"receiver_id": gate.id(), "amount": "50000000", "memo": null}))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    gate.deploy(&old_gate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let verifier_line = fs::read_to_string(
        std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../fixtures/keys/test-verifier.pub"),
    )
    .unwrap();
    let verifier_key = verifier_line.lines().next().unwrap();
    gate.call(gate.id(), "new")
        .args_json(json!({
            "owner_id": owner.id(),
            "verifier_key": verifier_key,
            "max_ttl_ms": 300000
        }))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let (public_key, secret_key) = agent_keys();
    let token_id = token.id().to_string();
    let cap = json!({
        "asset": {"Ft": token_id},
        "per_call": "10000000",
        "window_cap": "50000000",
        "window_ms": 86400000,
        "window_start_ms": 4102444800000u64,
        "spent": "0"
    });
    let near_cap = json!({
        "asset": "Near",
        "per_call": "10000000000000000000000000",
        "window_cap": "50000000000000000000000000",
        "window_ms": 86400000,
        "window_start_ms": 4102444800000u64,
        "spent": "0"
    });
    let unknown_cap = json!({
        "asset": {"Ft": token.id()},
        "per_call": "1",
        "window_cap": "1",
        "window_ms": 86400000,
        "window_start_ms": 4102444800000u64,
        "spent": "0"
    });
    owner
        .call(gate.id(), "register_agent")
        .args_json(json!({
            "public_key": public_key.to_string(),
            "label": "test agent",
            "caps": [cap, near_cap],
            "unknown_caps": [unknown_cap],
            "allowance_yocto": "1000000000000000000000000"
        }))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    owner
        .call(gate.id(), "set_allowlist")
        .args_json(json!({
            "agent_pk": public_key.to_string(),
            "accounts": [vendor.id()]
        }))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let keys = gate.view_access_keys().await.unwrap();
    let key_record = format!("{keys:?}");
    assert!(key_record.contains(&public_key.to_string()));
    assert!(key_record.contains("method_names: [\"request_transfer\"]"));
    assert!(key_record.contains(&format!("receiver_id: {:?}", gate.id().as_str())));
    assert!(key_record.contains("allowance: Some"));

    let agent = Account::from_secret_key(gate.id().clone(), secret_key, &worker);
    let happy = fresh_request(&worker, gate.id(), vector("01-ft-happy-sandbox"), 300_000).await;
    let before = balance(&vendor, token.id()).await;
    let happy_result = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&happy))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap();
    let happy_gas = happy_result.total_gas_burnt.as_gas();
    assert!(happy_result
        .logs()
        .iter()
        .any(|line| line.contains("transfer_scheduled")));
    assert!(happy_result
        .logs()
        .iter()
        .any(|line| line.contains("transfer_completed")));
    assert_eq!(balance(&vendor, token.id()).await, before + 1_000_000);
    let used: bool = gate
        .view(gate.id(), "is_verdict_used")
        .args_json(json!({"verdict_id": happy["verdict_id"]}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert!(used);
    println!("happy FT total gas burnt: {happy_gas}");
    for outcome in happy_result.outcomes() {
        println!("happy FT outcome gas burnt: {}", outcome.gas_burnt.as_gas());
    }

    let original_policy: Value = gate
        .view(gate.id(), "get_policy")
        .await
        .unwrap()
        .json()
        .unwrap();
    let original_agent: Value = gate
        .view(gate.id(), "get_agent")
        .args_json(json!({"public_key": public_key.to_string()}))
        .await
        .unwrap()
        .json()
        .unwrap();

    // Replace the code over existing state without calling new.
    gate.deploy(&gate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let upgraded_policy: Value = gate
        .view(gate.id(), "get_policy")
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(upgraded_policy, original_policy);
    assert_eq!(upgraded_policy["max_ttl_ms"], 300000);
    assert_eq!(upgraded_policy["paused"], false);
    let version: String = gate
        .view(gate.id(), "version")
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(version, "0.2.0");
    let upgraded_agent: Value = gate
        .view(gate.id(), "get_agent")
        .args_json(json!({"public_key": public_key.to_string()}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(upgraded_agent, original_agent);
    assert_eq!(upgraded_agent["label"], "test agent");
    assert_eq!(upgraded_agent["enabled"], true);
    let empty_memo =
        fresh_request(&worker, gate.id(), vector("03-memo-empty-sandbox"), 10_000).await;
    let upgrade_transfer = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&empty_memo))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap();
    upgrade_transfer.clone().into_result().unwrap();
    assert!(upgrade_transfer
        .logs()
        .iter()
        .any(|line| line.contains("transfer_completed")));
    let happy_used: bool = gate
        .view(gate.id(), "is_verdict_used")
        .args_json(json!({"verdict_id": happy["verdict_id"]}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert!(happy_used);
    let upgrade_used: bool = gate
        .view(gate.id(), "is_verdict_used")
        .args_json(json!({"verdict_id": empty_memo["verdict_id"]}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert!(upgrade_used);
    let used_verdicts: Vec<(String, u64)> = gate
        .view(gate.id(), "used_verdicts")
        .args_json(json!({"from": 0, "limit": 50}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert!(used_verdicts
        .iter()
        .any(|(verdict_id, _)| verdict_id == happy["verdict_id"].as_str().unwrap()));
    assert!(used_verdicts
        .iter()
        .any(|(verdict_id, _)| verdict_id == empty_memo["verdict_id"].as_str().unwrap()));

    let replay_error = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&happy))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap_err();
    let refusal_text = replay_error.to_string();
    let panic_text = refusal_text
        .strip_prefix("Action #0: ExecutionError(\"")
        .and_then(|text| text.strip_suffix("\")"))
        .unwrap();
    println!("sandbox refusal text: {panic_text}");
    assert_eq!(panic_text, "Smart contract panicked: REFUSED:verdict_used");

    let mut expired_head_ids = Vec::with_capacity(4);
    let mut expired_head_expires = vec![empty_memo["expires_ms"].as_u64().unwrap()];
    for index in 0..4 {
        let mut expired = vector("03-memo-empty-sandbox");
        expired["amount"] = json!("1");
        expired["verdict_id"] = json!(hex_encode(
            &Sha256::digest(format!("expired-head-{index}").as_bytes())[..16]
        ));
        let expired = fresh_request(&worker, gate.id(), expired, 10_000).await;
        let result = agent
            .call(gate.id(), "request_transfer")
            .args_json(transfer_args(&expired))
            .gas(Gas::from_tgas(100))
            .transact()
            .await
            .unwrap();
        result.clone().into_result().unwrap();
        assert!(result
            .logs()
            .iter()
            .any(|line| line.contains("transfer_completed")));
        expired_head_ids.push(expired["verdict_id"].as_str().unwrap().to_owned());
        expired_head_expires.push(expired["expires_ms"].as_u64().unwrap());
    }
    worker.fast_forward(400).await.unwrap();
    let now_ms = worker.view_block().await.unwrap().timestamp() / 1_000_000;
    assert!(expired_head_expires
        .iter()
        .all(|expires_ms| *expires_ms <= now_ms));
    let mut gas_probe_vector = vector("03-memo-empty-sandbox");
    gas_probe_vector["amount"] = json!("1");
    gas_probe_vector["verdict_id"] = json!(hex_encode(
        &Sha256::digest(b"request-transfer-prune4-probe")[..16]
    ));
    let gas_probe = fresh_request(&worker, gate.id(), gas_probe_vector, 300_000).await;
    let gas_probe_result = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&gas_probe))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap();
    gas_probe_result.clone().into_result().unwrap();
    assert!(gas_probe_result
        .logs()
        .iter()
        .any(|line| line.contains("transfer_completed")));
    let request_prune4_gas = gas_probe_result.total_gas_burnt.as_gas();
    assert!(request_prune4_gas < Gas::from_tgas(100).as_gas());
    println!("GAS request_transfer_prune4={request_prune4_gas}");
    let mut pruned_ids = vec![empty_memo["verdict_id"].as_str().unwrap().to_owned()];
    pruned_ids.extend(expired_head_ids.iter().take(2).cloned());
    for verdict_id in &pruned_ids {
        let used: bool = gate
            .view(gate.id(), "is_verdict_used")
            .args_json(json!({"verdict_id": verdict_id}))
            .await
            .unwrap()
            .json()
            .unwrap();
        assert!(!used);
    }

    let candidate_policy_after_transfer: Value = gate
        .view(gate.id(), "get_policy")
        .await
        .unwrap()
        .json()
        .unwrap();
    let candidate_agent_after_transfer: Value = gate
        .view(gate.id(), "get_agent")
        .args_json(json!({"public_key": public_key.to_string()}))
        .await
        .unwrap()
        .json()
        .unwrap();
    gate.deploy(&old_gate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let rollback_policy: Value = gate
        .view(gate.id(), "get_policy")
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(rollback_policy, candidate_policy_after_transfer);
    let rollback_agent: Value = gate
        .view(gate.id(), "get_agent")
        .args_json(json!({"public_key": public_key.to_string()}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(rollback_agent, candidate_agent_after_transfer);
    let rollback_transfer = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(
            &fresh_request(&worker, gate.id(), vector("02-near-happy-sandbox"), 300_000).await,
        ))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap();
    rollback_transfer.clone().into_result().unwrap();
    assert!(rollback_transfer
        .logs()
        .iter()
        .any(|line| line.contains("transfer_completed")));

    let no_verdict = agent
        .call(gate.id(), "request_transfer")
        .args_json(json!({"request": transfer_args(&happy)["request"], "verdict": null}))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap_err();
    assert!(format!("{no_verdict:?}").contains("REFUSED:no_verdict"));

    let callback_error = agent
        .call(gate.id(), "on_transfer")
        .args_json(json!({"request_id": 0}))
        .transact()
        .await
        .unwrap_err();
    println!("callback protocol rejection: {callback_error:?}");
    assert!(format!("{callback_error:?}").contains("MethodNameMismatch"));
    let token_error = agent
        .call(token.id(), "ft_transfer")
        .args_json(json!({"receiver_id": vendor.id(), "amount": "1", "memo": null}))
        .transact()
        .await
        .unwrap_err();
    println!("token protocol rejection: {token_error:?}");
    assert!(format!("{token_error:?}").contains("ReceiverMismatch"));
    let deposit_error = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(
            &fresh_request(&worker, gate.id(), happy.clone(), 300_000).await,
        ))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap_err();
    println!("deposit protocol rejection: {deposit_error:?}");
    assert!(format!("{deposit_error:?}").contains("DepositWithFunctionCall"));

    let unknown = fresh_request(
        &worker,
        gate.id(),
        vector("12-unknown-payee-sandbox"),
        300_000,
    )
    .await;
    let unknown_result = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&unknown))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap();
    unknown_result.clone().into_result().unwrap();
    assert!(unknown_result
        .logs()
        .iter()
        .any(|line| line.contains("transfer_completed")));
    let unknown_agent_view: Value = gate
        .view(gate.id(), "get_agent")
        .args_json(json!({"public_key": public_key.to_string()}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(unknown_agent_view["unknown_caps"][0]["spent"], "1");
    let over_cap = fresh_request(
        &worker,
        gate.id(),
        vector("13-unknown-payee-over-cap-sandbox"),
        300_000,
    )
    .await;
    let over_cap_error = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&over_cap))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap_err();
    assert!(format!("{over_cap_error:?}").contains("REFUSED:unknown_payee_cap"));

    owner
        .call(gate.id(), "set_allowlist")
        .args_json(json!({
            "agent_pk": public_key.to_string(),
            "accounts": [vendor.id(), fresh.id()]
        }))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let mut fresh_vector = vector("01-ft-happy-sandbox");
    fresh_vector["receiver_id"] = json!(fresh.id());
    fresh_vector["verdict_id"] = json!(hex_encode(
        &Sha256::digest(b"fresh unregistered receiver")[..16]
    ));
    let fresh_transfer = fresh_request(&worker, gate.id(), fresh_vector, 300_000).await;
    let fresh_before: Value = gate
        .view(gate.id(), "get_agent")
        .args_json(json!({"public_key": public_key.to_string()}))
        .await
        .unwrap()
        .json()
        .unwrap();
    let fresh_result = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&fresh_transfer))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap();
    fresh_result.clone().into_result().unwrap();
    assert!(fresh_result
        .logs()
        .iter()
        .any(|line| line.contains("transfer_failed") && line.contains("\"restored\":true")));
    let fresh_after: Value = gate
        .view(gate.id(), "get_agent")
        .args_json(json!({"public_key": public_key.to_string()}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(
        fresh_after["caps"][0]["spent"],
        fresh_before["caps"][0]["spent"]
    );

    let near = fresh_request(&worker, gate.id(), vector("02-near-happy-sandbox"), 300_000).await;
    let vendor_balance = vendor.view_account().await.unwrap().balance;
    let near_result = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&near))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap();
    near_result.clone().into_result().unwrap();
    let near_gas = near_result.total_gas_burnt.as_gas();
    assert!(vendor.view_account().await.unwrap().balance > vendor_balance);
    println!("happy NEAR total gas burnt: {near_gas}");
    for outcome in near_result.outcomes() {
        println!(
            "happy NEAR outcome gas burnt: {}",
            outcome.gas_burnt.as_gas()
        );
    }

    let mut prune_batch_ids = Vec::with_capacity(50);
    let mut prune_batch_expires = Vec::with_capacity(50);
    for index in 0..50 {
        let mut batch_vector = vector("01-ft-happy-sandbox");
        batch_vector["amount"] = json!("1");
        batch_vector["verdict_id"] = json!(hex_encode(
            &Sha256::digest(format!("prune-batch-{index}").as_bytes())[..16]
        ));
        let batch_request = fresh_request(&worker, gate.id(), batch_vector, 300_000).await;
        let batch_result = agent
            .call(gate.id(), "request_transfer")
            .args_json(transfer_args(&batch_request))
            .gas(Gas::from_tgas(100))
            .transact()
            .await
            .unwrap();
        batch_result.clone().into_result().unwrap();
        assert!(batch_result
            .logs()
            .iter()
            .any(|line| line.contains("transfer_completed")));
        prune_batch_ids.push(batch_request["verdict_id"].as_str().unwrap().to_owned());
        prune_batch_expires.push(batch_request["expires_ms"].as_u64().unwrap());
    }
    let last_expires_ms = *prune_batch_expires.iter().max().unwrap();
    let mut now_ms = worker.view_block().await.unwrap().timestamp() / 1_000_000;
    for _ in 0..20 {
        if now_ms > last_expires_ms {
            break;
        }
        worker.fast_forward(400).await.unwrap();
        now_ms = worker.view_block().await.unwrap().timestamp() / 1_000_000;
    }
    assert!(prune_batch_expires
        .iter()
        .all(|expires_ms| *expires_ms <= now_ms));
    let prune_used_result = owner
        .call(gate.id(), "prune_used")
        .args_json(json!({"ids": prune_batch_ids}))
        .deposit(NearToken::from_yoctonear(1))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap();
    let prune_used_gas = prune_used_result.total_gas_burnt.as_gas();
    assert!(prune_used_gas < Gas::from_tgas(100).as_gas());
    println!("GAS prune_used_50={prune_used_gas}");
    assert_eq!(prune_used_result.json::<u32>().unwrap(), 50);

    owner
        .call(gate.id(), "set_paused")
        .args_json(json!({"paused": true}))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let paused_error = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(
            &fresh_request(&worker, gate.id(), happy.clone(), 300_000).await,
        ))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap_err();
    assert!(format!("{paused_error:?}").contains("REFUSED:paused"));
    let _ = stranger;
}
