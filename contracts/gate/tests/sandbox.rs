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

    let verifier_seed: [u8; 32] = Sha256::digest(b"gate test verifier v1").into();
    sign_request(
        gate_id,
        &mut request,
        &SigningKey::from_bytes(&verifier_seed),
    );
    request
}

fn sign_request(
    gate_id: &near_workspaces::types::AccountId,
    request: &mut Value,
    verifier: &SigningKey,
) {
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
        request["issued_ms"].as_u64().unwrap(),
        request["expires_ms"].as_u64().unwrap(),
        request["verdict_id"].as_str().unwrap(),
        request["evidence_sha256"].as_str().unwrap(),
        request["key_id"].as_str().unwrap(),
        request["literal"].as_str().unwrap(),
    );
    request["signature_hex"] = json!(hex_encode(
        &verifier
            .sign(&Sha256::digest(message.as_bytes()))
            .to_bytes()
    ));
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

    let previous_version: String = gate
        .view(gate.id(), "version")
        .await
        .unwrap()
        .json()
        .unwrap();
    assert!(matches!(previous_version.as_str(), "0.2.1" | "0.2.2"));

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
    assert_eq!(version, env!("CARGO_PKG_VERSION"));
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
    let mut expired_head_expires = vec![
        happy["expires_ms"].as_u64().unwrap(),
        empty_memo["expires_ms"].as_u64().unwrap(),
    ];
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
    let last_head_expires_ms = *expired_head_expires.iter().max().unwrap();
    let mut now_ms = worker.view_block().await.unwrap().timestamp() / 1_000_000;
    for _ in 0..20 {
        if now_ms >= last_head_expires_ms {
            break;
        }
        worker.fast_forward(400).await.unwrap();
        now_ms = worker.view_block().await.unwrap().timestamp() / 1_000_000;
    }
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
    let rollback_version: String = gate
        .view(gate.id(), "version")
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(rollback_version, previous_version);
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
    // Return to the candidate, the deployed end state, so every check below runs on the candidate.
    gate.deploy(&gate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let redeployed_policy: Value = gate
        .view(gate.id(), "get_policy")
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(redeployed_policy, rollback_policy);

    let before_refusals: Value = gate
        .view(gate.id(), "get_agent")
        .args_json(json!({"public_key": public_key.to_string()}))
        .await
        .unwrap()
        .json()
        .unwrap();
    let mut native_memo = vector("02-near-happy-sandbox");
    native_memo["memo"] = json!("invoice 1");
    let mut native_empty_memo = vector("02-near-happy-sandbox");
    native_empty_memo["memo"] = json!("");
    let mut unknown_token = vector("01-ft-happy-sandbox");
    unknown_token["token_id"] = json!("unknown-token.test.near");
    for (request, expected) in [
        (native_memo, "REFUSED:bad_request"),
        (native_empty_memo, "REFUSED:bad_request"),
        (unknown_token, "REFUSED:unknown_token"),
    ] {
        let request = fresh_request(&worker, gate.id(), request, 300_000).await;
        let refused = agent
            .call(gate.id(), "request_transfer")
            .args_json(transfer_args(&request))
            .gas(Gas::from_tgas(100))
            .transact()
            .await
            .unwrap();
        let error = refused.clone().into_result().unwrap_err();
        assert!(format!("{error:?}").contains(expected));
        assert!(!refused
            .logs()
            .iter()
            .any(|line| line.contains("transfer_scheduled")));
        let used: bool = gate
            .view(gate.id(), "is_verdict_used")
            .args_json(json!({"verdict_id": request["verdict_id"]}))
            .await
            .unwrap()
            .json()
            .unwrap();
        assert!(!used);
        let after_refusal: Value = gate
            .view(gate.id(), "get_agent")
            .args_json(json!({"public_key": public_key.to_string()}))
            .await
            .unwrap()
            .json()
            .unwrap();
        assert_eq!(after_refusal, before_refusals);
    }

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

struct Gate022Sandbox {
    worker: near_workspaces::Worker<near_workspaces::network::Sandbox>,
    owner: Account,
    gate: Account,
    vendor: Account,
    agent: Account,
    agent_public_key: PublicKey,
    previous_wasm: Vec<u8>,
    candidate_wasm: Vec<u8>,
}

impl Gate022Sandbox {
    async fn new() -> Self {
        let worker = near_workspaces::sandbox().await.unwrap();
        let root = worker.root_account().unwrap();
        let mut accounts = Vec::new();
        for name in ["owner", "gate", "vendor"] {
            accounts.push(
                root.create_subaccount(name)
                    .initial_balance(NearToken::from_near(20))
                    .transact()
                    .await
                    .unwrap()
                    .into_result()
                    .unwrap(),
            );
        }
        let vendor = accounts.pop().unwrap();
        let gate = accounts.pop().unwrap();
        let owner = accounts.pop().unwrap();
        let repository = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let previous_wasm = ["res/previous/gate.wasm", "res/gate.wasm"]
            .into_iter()
            .find_map(|relative_path| {
                let wasm = fs::read(repository.join(relative_path)).unwrap();
                let digest = Sha256::digest(&wasm);
                if base58_encode(&digest) != "5EmM2UQRYcvaNvJX6jfcgtNS7Mm8ZPjZhwK1J8bMXrcu" {
                    return None;
                }
                assert_eq!(
                    fs::read_to_string(repository.join(format!("{relative_path}.sha256")))
                        .unwrap()
                        .trim(),
                    format!("{}  {relative_path}", hex_encode(&digest))
                );
                Some(wasm)
            })
            .expect("hash-pinned Gate 0.2.2 artifact in res or res/previous");
        let candidate_wasm = match std::env::var_os("GATE_CANDIDATE_WASM") {
            Some(path) => fs::read(path).unwrap(),
            None => near_workspaces::compile_project(env!("CARGO_MANIFEST_DIR"))
                .await
                .unwrap(),
        };
        gate.deploy(&previous_wasm)
            .await
            .unwrap()
            .into_result()
            .unwrap();
        let verifier =
            fs::read_to_string(repository.join("fixtures/keys/test-verifier.pub")).unwrap();
        gate.call(gate.id(), "new")
            .args_json(json!({
                "owner_id": owner.id(),
                "verifier_key": verifier.lines().next().unwrap(),
                "max_ttl_ms": 300_000
            }))
            .transact()
            .await
            .unwrap()
            .into_result()
            .unwrap();
        let version: String = gate
            .view(gate.id(), "version")
            .await
            .unwrap()
            .json()
            .unwrap();
        assert_eq!(version, "0.2.2");
        let (agent_public_key, agent_secret_key) = agent_keys();
        owner
            .call(gate.id(), "register_agent")
            .args_json(json!({
                "public_key": agent_public_key.to_string(),
                "label": "attested sandbox agent",
                "caps": [{
                    "asset": "Near",
                    "per_call": "1000000000000000000000000",
                    "window_cap": "10000000000000000000000000",
                    "window_ms": 86_400_000,
                    "window_start_ms": 0,
                    "spent": "0"
                }],
                "unknown_caps": [],
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
                "agent_pk": agent_public_key.to_string(),
                "accounts": [vendor.id()]
            }))
            .deposit(NearToken::from_yoctonear(1))
            .transact()
            .await
            .unwrap()
            .into_result()
            .unwrap();
        let agent = Account::from_secret_key(gate.id().clone(), agent_secret_key, &worker);
        Self {
            worker,
            owner,
            gate,
            vendor,
            agent,
            agent_public_key,
            previous_wasm,
            candidate_wasm,
        }
    }

    async fn policy_and_agent(&self) -> (Value, Value) {
        (
            self.gate
                .view(self.gate.id(), "get_policy")
                .await
                .unwrap()
                .json()
                .unwrap(),
            self.gate
                .view(self.gate.id(), "get_agent")
                .args_json(json!({"public_key": self.agent_public_key.to_string()}))
                .await
                .unwrap()
                .json()
                .unwrap(),
        )
    }

    async fn request(&self, signer: Option<&SigningKey>) -> Value {
        let mut request = vector("02-near-happy-sandbox");
        request["amount"] = json!("1");
        let mut request = fresh_request(&self.worker, self.gate.id(), request, 300_000).await;
        if let Some(signer) = signer {
            sign_request(self.gate.id(), &mut request, signer);
        }
        request
    }

    async fn assert_completed(&self, request: &Value, attested_key: Option<&str>) {
        let before = self
            .vendor
            .view_account()
            .await
            .unwrap()
            .balance
            .as_yoctonear();
        let result = self
            .agent
            .call(self.gate.id(), "request_transfer")
            .args_json(transfer_args(request))
            .gas(Gas::from_tgas(100))
            .transact()
            .await
            .unwrap();
        result.clone().into_result().unwrap();
        assert!(result
            .logs()
            .iter()
            .any(|line| line.contains("transfer_completed")));
        if let Some(public_key) = attested_key {
            assert!(result.logs().iter().any(|line| {
                line.contains("transfer_scheduled")
                    && line.contains(&format!("\"verifier_key\":\"{public_key}\""))
            }));
        }
        assert_eq!(
            self.vendor
                .view_account()
                .await
                .unwrap()
                .balance
                .as_yoctonear(),
            before + request["amount"].as_str().unwrap().parse::<u128>().unwrap()
        );
        let used: bool = self
            .gate
            .view(self.gate.id(), "is_verdict_used")
            .args_json(json!({"verdict_id": request["verdict_id"]}))
            .await
            .unwrap()
            .json()
            .unwrap();
        assert!(used);
    }

    async fn assert_refused(&self, request: &Value, reason: &str) {
        let before = self.policy_and_agent().await;
        let balance = self.vendor.view_account().await.unwrap().balance;
        let result = self
            .agent
            .call(self.gate.id(), "request_transfer")
            .args_json(transfer_args(request))
            .gas(Gas::from_tgas(100))
            .transact()
            .await
            .unwrap();
        let error = result.clone().into_result().unwrap_err();
        assert!(format!("{error:?}").contains(reason), "{error:?}");
        assert!(!result
            .logs()
            .iter()
            .any(|line| line.contains("transfer_scheduled")));
        assert_eq!(self.policy_and_agent().await, before);
        assert_eq!(self.vendor.view_account().await.unwrap().balance, balance);
        let used: bool = self
            .gate
            .view(self.gate.id(), "is_verdict_used")
            .args_json(json!({"verdict_id": request["verdict_id"]}))
            .await
            .unwrap()
            .json()
            .unwrap();
        assert!(!used);
    }

    async fn attestation(&self) -> Value {
        self.gate
            .view(self.gate.id(), "get_attestation")
            .await
            .unwrap()
            .json()
            .unwrap()
    }

    async fn deploy_mock(&self) -> Account {
        let registry = self
            .worker
            .root_account()
            .unwrap()
            .create_subaccount("registry")
            .initial_balance(NearToken::from_near(20))
            .transact()
            .await
            .unwrap()
            .into_result()
            .unwrap();
        let mock_path = format!("{}/../test-attest-mock", env!("CARGO_MANIFEST_DIR"));
        let wasm = near_workspaces::compile_project(&mock_path).await.unwrap();
        registry.deploy(&wasm).await.unwrap().into_result().unwrap();
        let config = attest_vectors()["registry_config"][0]["new_arguments"].clone();
        registry
            .call(registry.id(), "new")
            .args_json(config.clone())
            .transact()
            .await
            .unwrap()
            .into_result()
            .unwrap();
        let actual: Value = registry
            .view(registry.id(), "get_config")
            .args_json(json!({}))
            .await
            .unwrap()
            .json()
            .unwrap();
        assert_eq!(actual, config);
        registry
    }

    async fn enable(&self, registry: &Account) {
        self.owner
            .call(self.gate.id(), "enable_attested_mode")
            .args_json(json!({
                "registry_id": registry.id(),
                "measurement_id": attest_vectors()["measurements"][1]["measurement_id"],
                "measurement_timelock_ms": 86_400_000,
                "platforms": ["11".repeat(16)]
            }))
            .deposit(NearToken::from_yoctonear(1))
            .transact()
            .await
            .unwrap()
            .into_result()
            .unwrap();
    }

    async fn record(&self, key: &SigningKey, measurement: usize) -> Value {
        let now_ms = self.worker.view_block().await.unwrap().timestamp() / 1_000_000;
        let vectors = attest_vectors();
        let mut record = vectors["attested_keys"][0]["view"].clone();
        record["public_key"] = json!(hex_encode(&key.verifying_key().to_bytes()));
        record["measurement"] = vectors["measurements"][measurement]["input"].clone();
        record["measurement_id"] = vectors["measurements"][measurement]["measurement_id"].clone();
        record["issued_at_ms"] = json!(now_ms);
        record["registered_ms"] = json!(now_ms);
        record["expires_ms"] = json!(now_ms + 86_400_000);
        record["collateral_expires_ms"] = json!(now_ms + 172_800_000);
        record
    }

    async fn sync(&self, registry: &Account, key: &SigningKey, record: Option<Value>) -> bool {
        let public_key = hex_encode(&key.verifying_key().to_bytes());
        registry
            .call(registry.id(), "set_key")
            .args_json(json!({"public_key": public_key, "record": record}))
            .transact()
            .await
            .unwrap()
            .into_result()
            .unwrap();
        registry
            .call(self.gate.id(), "sync_verifier")
            .args_json(json!({"public_key": public_key}))
            .gas(Gas::from_tgas(100))
            .transact()
            .await
            .unwrap()
            .into_result()
            .unwrap()
            .json()
            .unwrap()
    }
}

fn attest_vectors() -> Value {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../fixtures/attest/vectors.json");
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn enclave_signer(name: &str) -> SigningKey {
    let seed: [u8; 32] = Sha256::digest(format!("gate sandbox enclave {name}").as_bytes()).into();
    SigningKey::from_bytes(&seed)
}

#[tokio::test]
async fn sandbox_gate_022_legacy_round_trip() {
    let sandbox = Gate022Sandbox::new().await;
    let first = sandbox.request(None).await;
    sandbox.assert_completed(&first, None).await;
    let original = sandbox.policy_and_agent().await;
    sandbox
        .gate
        .deploy(&sandbox.candidate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    assert_eq!(sandbox.policy_and_agent().await, original);
    assert_eq!(sandbox.attestation().await["enabled"], false);
    let version: String = sandbox
        .gate
        .view(sandbox.gate.id(), "version")
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(version, "0.3.0");
    let used: bool = sandbox
        .gate
        .view(sandbox.gate.id(), "is_verdict_used")
        .args_json(json!({"verdict_id": first["verdict_id"]}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert!(used);
    sandbox
        .assert_completed(&sandbox.request(None).await, None)
        .await;
    let upgraded = sandbox.policy_and_agent().await;
    sandbox
        .gate
        .deploy(&sandbox.previous_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    assert_eq!(sandbox.policy_and_agent().await, upgraded);
    sandbox
        .assert_completed(&sandbox.request(None).await, None)
        .await;
    sandbox
        .gate
        .deploy(&sandbox.candidate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    assert_eq!(sandbox.attestation().await["enabled"], false);
}

#[tokio::test]
async fn sandbox_gate_022_attested_round_trip_burns_legacy_verifiers() {
    let sandbox = Gate022Sandbox::new().await;
    sandbox
        .assert_completed(&sandbox.request(None).await, None)
        .await;
    let verifier_seed: [u8; 32] = Sha256::digest(b"gate test verifier v1").into();
    let current = PublicKey::try_from_parts(
        KeyType::ED25519,
        &SigningKey::from_bytes(&verifier_seed)
            .verifying_key()
            .to_bytes(),
    )
    .unwrap();
    let now_ms = sandbox.worker.view_block().await.unwrap().timestamp() / 1_000_000;
    sandbox
        .owner
        .call(sandbox.gate.id(), "set_verifier_keys")
        .args_json(json!({
            "current": current.to_string(),
            "previous": sandbox.agent_public_key.to_string(),
            "previous_valid_until_ms": now_ms + 86_400_000
        }))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let original = sandbox.policy_and_agent().await;
    sandbox
        .gate
        .deploy(&sandbox.candidate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    assert_eq!(sandbox.policy_and_agent().await, original);
    let registry = sandbox.deploy_mock().await;
    sandbox.enable(&registry).await;
    let key = enclave_signer("round-trip");
    let public_key = hex_encode(&key.verifying_key().to_bytes());
    let record = sandbox.record(&key, 1).await;
    assert!(sandbox.sync(&registry, &key, Some(record.clone())).await);
    assert_eq!(sandbox.attestation().await["keys"][0], record);
    sandbox
        .assert_completed(&sandbox.request(Some(&key)).await, Some(&public_key))
        .await;
    let attestation = sandbox.attestation().await;
    let upgraded = sandbox.policy_and_agent().await;
    let burned: Value = sandbox
        .gate
        .view(sandbox.gate.id(), "get_verifier_keys")
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(
        burned["current"],
        hex_encode(&Sha256::digest(
            b"siyab gate: attested mode, no operator verifier key"
        ))
    );
    assert_eq!(burned["previous"], Value::Null);
    let restored = sandbox
        .owner
        .call(sandbox.gate.id(), "set_verifier_keys")
        .args_json(json!({
            "current": sandbox.agent_public_key.to_string(),
            "previous": null,
            "previous_valid_until_ms": 0
        }))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap_err();
    assert!(format!("{restored:?}").contains("REFUSED:attested_mode"));
    sandbox
        .assert_refused(&sandbox.request(None).await, "REFUSED:bad_signature")
        .await;
    sandbox
        .gate
        .deploy(&sandbox.previous_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    assert_eq!(sandbox.policy_and_agent().await, upgraded);
    let rollback_keys: Value = sandbox
        .gate
        .view(sandbox.gate.id(), "get_verifier_keys")
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(rollback_keys, burned);
    sandbox
        .assert_refused(&sandbox.request(None).await, "REFUSED:bad_signature")
        .await;
    sandbox
        .assert_refused(&sandbox.request(Some(&key)).await, "REFUSED:bad_signature")
        .await;
    let mut previous_request = sandbox.request(None).await;
    previous_request["key_id"] = json!("prev");
    let previous_seed: [u8; 32] = Sha256::digest(b"gate test agent v1").into();
    sign_request(
        sandbox.gate.id(),
        &mut previous_request,
        &SigningKey::from_bytes(&previous_seed),
    );
    sandbox
        .assert_refused(&previous_request, "REFUSED:bad_key_id")
        .await;
    sandbox
        .gate
        .deploy(&sandbox.candidate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    assert_eq!(sandbox.attestation().await, attestation);
    assert_eq!(sandbox.policy_and_agent().await, upgraded);
    sandbox
        .assert_completed(&sandbox.request(Some(&key)).await, Some(&public_key))
        .await;
}

#[tokio::test]
async fn sandbox_attested_sync_refuses_ineligible_records_and_current_policy() {
    let sandbox = Gate022Sandbox::new().await;
    sandbox
        .gate
        .deploy(&sandbox.candidate_wasm)
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let registry = sandbox.deploy_mock().await;
    sandbox.enable(&registry).await;
    for control in [
        "missing",
        "expired",
        "wrong_platform",
        "unapproved_measurement",
        "pending_measurement",
        "mismatched_public_key",
        "extra_field",
        "missing_field",
        "wrong_tcb_status",
        "lease_extended",
    ] {
        let key = enclave_signer(control);
        let mut record = sandbox.record(&key, 1).await;
        match control {
            "expired" => {
                let now_ms = record["registered_ms"].as_u64().unwrap();
                record["issued_at_ms"] = json!(now_ms - 1000);
                record["registered_ms"] = json!(now_ms - 500);
                record["expires_ms"] = json!(now_ms - 1);
                record["collateral_expires_ms"] = json!(now_ms - 1);
            }
            "wrong_platform" => record["ppid"] = json!("22".repeat(16)),
            "unapproved_measurement" | "pending_measurement" => {
                record = sandbox.record(&key, 0).await;
                if control == "pending_measurement" {
                    sandbox
                        .owner
                        .call(sandbox.gate.id(), "propose_measurement")
                        .args_json(json!({
                            "measurement_id": record["measurement_id"],
                            "label": "pending sandbox measurement"
                        }))
                        .deposit(NearToken::from_yoctonear(1))
                        .transact()
                        .await
                        .unwrap()
                        .into_result()
                        .unwrap();
                    let view = sandbox.attestation().await;
                    let proposed = view["measurements"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|measurement| {
                            measurement["measurement_id"] == record["measurement_id"]
                        })
                        .unwrap();
                    assert!(
                        proposed["active_from_ms"].as_u64().unwrap()
                            >= record["registered_ms"].as_u64().unwrap() + 86_400_000
                    );
                }
            }
            "mismatched_public_key" => record["public_key"] = json!("ff".repeat(32)),
            "extra_field" => record["untrusted_extra"] = json!(true),
            "missing_field" => {
                record.as_object_mut().unwrap().remove("aux");
            }
            "wrong_tcb_status" => record["tcb_status"] = json!("OutOfDate"),
            "lease_extended" => {
                record["expires_ms"] = json!(record["expires_ms"].as_u64().unwrap() + 1);
            }
            "missing" => {}
            _ => unreachable!(),
        }
        let record = if control == "missing" {
            None
        } else {
            Some(record)
        };
        assert!(!sandbox.sync(&registry, &key, record).await, "{control}");
        assert_eq!(sandbox.attestation().await["keys"], json!([]), "{control}");
        sandbox
            .assert_refused(
                &sandbox.request(Some(&key)).await,
                "REFUSED:no_attested_verifier",
            )
            .await;
    }

    let denied_key = enclave_signer("locally-denied");
    let denied_public_key = hex_encode(&denied_key.verifying_key().to_bytes());
    assert!(
        sandbox
            .sync(
                &registry,
                &denied_key,
                Some(sandbox.record(&denied_key, 1).await)
            )
            .await
    );
    sandbox
        .assert_completed(
            &sandbox.request(Some(&denied_key)).await,
            Some(&denied_public_key),
        )
        .await;
    sandbox
        .owner
        .call(sandbox.gate.id(), "deny_verifier_key")
        .args_json(json!({"public_key": denied_public_key}))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    assert_eq!(sandbox.attestation().await["keys"], json!([]));
    assert!(
        !sandbox
            .sync(
                &registry,
                &denied_key,
                Some(sandbox.record(&denied_key, 1).await)
            )
            .await
    );
    sandbox
        .assert_refused(
            &sandbox.request(Some(&denied_key)).await,
            "REFUSED:no_attested_verifier",
        )
        .await;

    let expired_key = enclave_signer("cached-expiry");
    let mut expiring = sandbox.record(&expired_key, 1).await;
    let expires_ms = expiring["registered_ms"].as_u64().unwrap() + 60_000;
    expiring["expires_ms"] = json!(expires_ms);
    expiring["collateral_expires_ms"] = json!(expires_ms);
    assert!(sandbox.sync(&registry, &expired_key, Some(expiring)).await);
    sandbox
        .assert_completed(
            &sandbox.request(Some(&expired_key)).await,
            Some(&hex_encode(&expired_key.verifying_key().to_bytes())),
        )
        .await;
    let mut now_ms = sandbox.worker.view_block().await.unwrap().timestamp() / 1_000_000;
    for _ in 0..20 {
        if now_ms >= expires_ms {
            break;
        }
        sandbox.worker.fast_forward(400).await.unwrap();
        now_ms = sandbox.worker.view_block().await.unwrap().timestamp() / 1_000_000;
    }
    assert!(now_ms >= expires_ms);
    sandbox
        .assert_refused(
            &sandbox.request(Some(&expired_key)).await,
            "REFUSED:no_attested_verifier",
        )
        .await;

    let revoked_key = enclave_signer("locally-revoked-measurement");
    assert!(
        sandbox
            .sync(
                &registry,
                &revoked_key,
                Some(sandbox.record(&revoked_key, 1).await)
            )
            .await
    );
    sandbox
        .owner
        .call(sandbox.gate.id(), "revoke_measurement")
        .args_json(json!({"measurement_id": attest_vectors()["measurements"][1]["measurement_id"]}))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    assert_eq!(sandbox.attestation().await["keys"], json!([]));
    assert!(
        !sandbox
            .sync(
                &registry,
                &revoked_key,
                Some(sandbox.record(&revoked_key, 1).await)
            )
            .await
    );
    sandbox
        .assert_refused(
            &sandbox.request(Some(&revoked_key)).await,
            "REFUSED:no_attested_verifier",
        )
        .await;
}
