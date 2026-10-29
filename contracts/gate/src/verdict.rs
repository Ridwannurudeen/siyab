use near_sdk::{env, AccountId};
use crate::types::{TransferRequest, Verdict};

fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn hex_decode(value: &str, expected: usize) -> Result<Vec<u8>, &'static str> {
    if value.len() != expected * 2 || !value.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)) {
        return Err("bad_request");
    }
    (0..value.len())
        .step_by(2)
        .map(|index| u8::from_str_radix(&value[index..index + 2], 16).map_err(|_| "bad_request"))
        .collect()
}

pub fn canonical_message(
    gate: &AccountId,
    agent_pk: &[u8; 32],
    request: &TransferRequest,
    verdict: &Verdict,
) -> Vec<u8> {
    let token = match (&request.kind[..], &request.token_id) {
        ("near", None) => "-".to_owned(),
        ("ft", Some(token_id)) => token_id.to_string(),
        _ => "-".to_owned(),
    };
    let memo = request.memo.as_ref().map_or_else(
        || "-".to_owned(),
        |value| hex_encode(&env::sha256(value.as_bytes())),
    );
    format!(
        "GATEv1|{gate}|{}|{}|{token}|{}|{}|{memo}|{}|{}|{}|{}|{}|{}",
        hex_encode(agent_pk),
        request.kind,
        request.receiver_id,
        request.amount.0,
        verdict.issued_ms,
        verdict.expires_ms,
        verdict.verdict_id,
        verdict.evidence_hash,
        verdict.key_id,
        verdict.literal,
    )
    .into_bytes()
}

pub fn verify(
    gate: &AccountId,
    agent_pk: &[u8; 32],
    request: &TransferRequest,
    verdict: &Verdict,
    current_key: &[u8; 32],
    previous_key: Option<&([u8; 32], u64)>,
    now_ms: u64,
    max_ttl_ms: u64,
) -> Result<(), &'static str> {
    if verdict.literal != "ALLOW" {
        return Err("not_allow");
    }
    if verdict.issued_ms > now_ms || verdict.expires_ms < now_ms || verdict.expires_ms < verdict.issued_ms {
        return Err("expired");
    }
    if verdict.expires_ms.saturating_sub(verdict.issued_ms) > max_ttl_ms {
        return Err("ttl_too_long");
    }
    let key = match verdict.key_id.as_str() {
        "cur" => current_key,
        "prev" => {
            let Some((key, until_ms)) = previous_key else {
                return Err("bad_key_id");
            };
            if now_ms > *until_ms {
                return Err("bad_key_id");
            }
            key
        }
        _ => return Err("bad_key_id"),
    };
    hex_decode(&verdict.verdict_id, 16)?;
    hex_decode(&verdict.evidence_hash, 32)?;
    let signature = hex_decode(&verdict.signature, 64)?;
    let signature: [u8; 64] = signature.try_into().map_err(|_| "bad_request")?;
    let message = canonical_message(gate, agent_pk, request, verdict);
    let digest = env::sha256_array(&message);
    if !env::ed25519_verify(&signature, &digest, key) {
        return Err("bad_signature");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::{json_types::U128, test_utils::VMContextBuilder, testing_env};
    use serde_json::Value;
    use std::{fs, path::PathBuf};

    fn decode(value: &str) -> Vec<u8> {
        (0..value.len())
            .step_by(2)
            .map(|index| u8::from_str_radix(&value[index..index + 2], 16).unwrap())
            .collect()
    }

    #[test]
    fn shared_vectors_match_message_digest_and_signature() {
        testing_env!(VMContextBuilder::new().build());
        let directory = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/canon");
        let paths: Vec<_> = fs::read_dir(&directory)
            .expect("canonical vector directory exists")
            .map(|entry| entry.expect("vector entry").path())
            .filter(|path| path.extension().is_some_and(|extension| extension == "json"))
            .collect();
        assert!(!paths.is_empty(), "canonical vector directory is empty");

        for path in paths {
            let vector: Value = serde_json::from_slice(&fs::read(path).expect("read vector"))
                .expect("parse vector");
            let gate: AccountId = vector["gate_account_id"].as_str().unwrap().parse().unwrap();
            let agent_pk: [u8; 32] = decode(vector["agent_pk_hex"].as_str().unwrap())
                .try_into()
                .unwrap();
            let request = TransferRequest {
                kind: vector["kind"].as_str().unwrap().to_owned(),
                token_id: vector["token_id"].as_str().map(|value| value.parse().unwrap()),
                receiver_id: vector["receiver_id"].as_str().unwrap().parse().unwrap(),
                amount: U128(vector["amount"].as_str().unwrap().parse().unwrap()),
                memo: vector["memo"].as_str().map(str::to_owned),
            };
            let verdict = Verdict {
                literal: vector["literal"].as_str().unwrap().to_owned(),
                issued_ms: vector["issued_ms"].as_u64().unwrap(),
                expires_ms: vector["expires_ms"].as_u64().unwrap(),
                verdict_id: vector["verdict_id"].as_str().unwrap().to_owned(),
                evidence_hash: vector["evidence_sha256"].as_str().unwrap().to_owned(),
                key_id: vector["key_id"].as_str().unwrap().to_owned(),
                signature: vector["signature_hex"].as_str().unwrap().to_owned(),
            };
            let message = canonical_message(&gate, &agent_pk, &request, &verdict);
            assert_eq!(String::from_utf8(message.clone()).unwrap(), vector["message"]);
            assert_eq!(hex_encode(&env::sha256(&message)), vector["digest_sha256"]);
            let key: [u8; 32] = decode(vector["verifier_pub_hex"].as_str().unwrap())
                .try_into()
                .unwrap();
            let signature: [u8; 64] = decode(verdict.signature.as_str())
                .try_into()
                .unwrap();
            assert_eq!(
                env::ed25519_verify(&signature, &env::sha256_array(&message), &key),
                vector["expect_valid"].as_bool().unwrap(),
                "signature result for {}",
                vector["name"].as_str().unwrap()
            );
        }
    }
}
