use ed25519_dalek::SigningKey;
use near_sdk::serde_json::{json, Value};
use near_workspaces::{
    types::{Gas, KeyType, NearToken, PublicKey, SecretKey},
    Account,
};
use sha2::{Digest, Sha256};
use std::{fs, str::FromStr};

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
    let token = gate
        .create_subaccount("token")
        .initial_balance(NearToken::from_near(5))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();

    let target = std::env::var("CARGO_TARGET_DIR").unwrap();
    let gate_wasm = fs::read(format!("{target}/near/gate.wasm")).unwrap();
    let token_wasm = fs::read(format!("{target}/near/test_token.wasm")).unwrap();
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
    for account_id in [gate.id(), vendor.id()] {
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

    gate.deploy(&gate_wasm)
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
    let happy = vector("01-ft-happy-sandbox");
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

    let replay_error = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&happy))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap_err();
    assert!(format!("{replay_error:?}").contains("REFUSED:verdict_used"));
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
        .args_json(json!({"request_id": 0, "unknown_payee": false}))
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
        .args_json(transfer_args(&happy))
        .deposit(NearToken::from_yoctonear(1))
        .transact()
        .await
        .unwrap_err();
    println!("deposit protocol rejection: {deposit_error:?}");
    assert!(format!("{deposit_error:?}").contains("DepositWithFunctionCall"));

    let empty_memo = vector("03-memo-empty-sandbox");
    agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&empty_memo))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap();
    let unknown = vector("12-unknown-payee-sandbox");
    let unknown_result = agent
        .call(gate.id(), "request_transfer")
        .args_json(transfer_args(&unknown))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap();
    assert!(unknown_result
        .logs()
        .iter()
        .any(|line| line.contains("transfer_failed")));
    let agent_view: Value = gate
        .view(gate.id(), "get_agent")
        .args_json(json!({"public_key": public_key.to_string()}))
        .await
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(agent_view["unknown_caps"][0]["spent"], "0");
    let over_cap = vector("13-unknown-payee-over-cap-sandbox");
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

    let near = vector("02-near-happy-sandbox");
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
        .args_json(transfer_args(&happy))
        .gas(Gas::from_tgas(100))
        .transact()
        .await
        .unwrap()
        .into_result()
        .unwrap_err();
    assert!(format!("{paused_error:?}").contains("REFUSED:paused"));
    let _ = stranger;
}
