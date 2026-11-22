mod registry;
pub mod replay;
mod types;

pub use registry::AttestRegistry;
pub use types::{
    AttestedKeyView, MeasurementView, RegisterContext, RegistryConfig, ReplayEvent, VoteIntent,
};

use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

pub(crate) const KEYS_PREFIX: &[u8] = b"ak:";

pub type VoteReservations = BTreeMap<([u8; 32], String, String, [u8; 32]), String>;

fn valid_account_id(value: &str) -> bool {
    (2..=64).contains(&value.len())
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'-' | b'_')
        })
        && value
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphanumeric)
        && value
            .as_bytes()
            .last()
            .is_some_and(u8::is_ascii_alphanumeric)
        && value
            .as_bytes()
            .windows(2)
            .all(|pair| pair[0].is_ascii_alphanumeric() || pair[1].is_ascii_alphanumeric())
}

fn lower_hex<const N: usize>(value: &str) -> Result<[u8; N], &'static str> {
    if value.len() != N * 2
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("invalid lowercase hex");
    }
    let mut result = [0; N];
    for (index, byte) in result.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&value[index * 2..index * 2 + 2], 16)
            .map_err(|_| "invalid lowercase hex")?;
    }
    Ok(result)
}

pub fn validate_config(config: &RegistryConfig) -> Result<(), &'static str> {
    if !valid_account_id(&config.verifier_account_id)
        || !valid_account_id(&config.signer_account_id)
    {
        return Err("invalid config account id");
    }
    if config.max_lease_ms == 0 {
        return Err("max_lease_ms must be positive");
    }
    if !(1..=8).contains(&config.vote_recipients.len())
        || !config
            .vote_recipients
            .iter()
            .all(|value| valid_account_id(value))
        || !config
            .vote_recipients
            .windows(2)
            .all(|pair| pair[0] < pair[1])
    {
        return Err("vote recipients must be bounded sorted unique account ids");
    }
    Ok(())
}

pub fn validate_quote_td_attributes(quote: &[u8]) -> Result<(), &'static str> {
    if quote.len() < 48 || quote[4..8] != 0x81_u32.to_le_bytes() {
        return Err("expected TDX quote header");
    }
    let offset = match u16::from_le_bytes(quote[..2].try_into().unwrap()) {
        4 => 168,
        5 if quote.len() >= 54 && quote[48..54] == [2, 0, 0x48, 2, 0, 0] => 174,
        _ => return Err("expected version 4 or version 5 TD10 quote"),
    };
    let attributes = quote
        .get(offset..offset + 8)
        .ok_or("truncated TD attributes")?;
    validate_td_attributes(attributes.try_into().unwrap())
}

pub fn registration_callback(
    keys: &mut BTreeMap<String, AttestedKeyView>,
    candidate: AttestedKeyView,
    max_lease_ms: u64,
) -> Result<(), &'static str> {
    validate_key_view(&candidate, max_lease_ms)?;
    if let Some(previous) = keys.get(&candidate.public_key) {
        if candidate.issued_at_ms <= previous.issued_at_ms {
            return Err("issue time must be strictly greater");
        }
        if candidate.measurement_id != previous.measurement_id
            || candidate.ppid != previous.ppid
            || candidate.aux != previous.aux
        {
            return Err("registered key identity cannot change");
        }
    }
    keys.insert(candidate.public_key.clone(), candidate);
    Ok(())
}

pub fn verify_vote_signature(
    intent: &VoteIntent,
    signature: &[u8; 64],
    registry_account_id: &str,
) -> Result<(), &'static str> {
    if !valid_account_id(&intent.registry_account_id)
        || !valid_account_id(&intent.recipient)
        || !(1..=8).contains(&intent.platforms.len())
        || !intent.platforms.windows(2).all(|pair| pair[0] < pair[1])
        || !matches!(intent.verb.as_str(), "approve" | "reject")
        || intent.approval_id.len() != 36
        || !intent.approval_id.bytes().enumerate().all(|(index, byte)| {
            if [8, 13, 18, 23].contains(&index) {
                byte == b'-'
            } else {
                byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
            }
        })
        || !(1..=256).contains(&intent.wallet_pubkey.len())
        || !intent
            .wallet_pubkey
            .bytes()
            .all(|byte| (b'!'..=b'~').contains(&byte))
    {
        return Err("invalid vote structure");
    }
    let mut reconstructed = intent.clone();
    reconstructed.registry_account_id = registry_account_id.into();
    let digest = vote_intent_digest(&reconstructed);
    let key = ed25519_dalek::VerifyingKey::from_bytes(&intent.public_key)
        .map_err(|_| "invalid vote signature")?;
    key.verify_strict(&digest, &ed25519_dalek::Signature::from_bytes(signature))
        .map_err(|_| "invalid vote signature")?;
    if intent.registry_account_id != registry_account_id {
        return Err("wrong registry");
    }
    Ok(())
}

pub fn submit_vote_reference(
    intent: &VoteIntent,
    signature: &[u8; 64],
    config: &RegistryConfig,
    registry_account_id: &str,
    keys: &BTreeMap<String, AttestedKeyView>,
    reservations: &mut VoteReservations,
    now_ms: u64,
) -> Result<(), &'static str> {
    verify_vote_signature(intent, signature, registry_account_id)?;
    validate_config(config)?;
    if !config.vote_recipients.contains(&intent.recipient) {
        return Err("vote recipient is not allowed");
    }
    let key = keys
        .values()
        .find(|key| lower_hex::<32>(&key.public_key) == Ok(intent.public_key))
        .ok_or("unknown key")?;
    validate_key_view(key, config.max_lease_ms)?;
    if lower_hex::<32>(&key.measurement_id)? != intent.measurement_id
        || !intent.platforms.contains(&lower_hex::<16>(&key.ppid)?)
    {
        return Err("key identity outside vote scope");
    }
    validate_vote_expiry(now_ms, key.registered_ms, key.expires_ms, intent.expires_ms)?;
    let vote_id = (
        scope_id(&intent.measurement_id, &intent.platforms),
        intent.approval_id.clone(),
        intent.wallet_pubkey.clone(),
        intent.request_hash,
    );
    if reservations
        .get(&vote_id)
        .is_some_and(|verb| *verb != intent.verb)
    {
        return Err("opposite verb already reserved");
    }
    reservations.insert(vote_id, intent.verb.clone());
    Ok(())
}

pub fn validate_td_attributes(td_attributes: [u8; 8]) -> Result<(), &'static str> {
    let attributes = u64::from_le_bytes(td_attributes);
    if attributes & 1 != 0 {
        return Err("TD DEBUG is enabled");
    }
    if attributes & 0x70 != 0 {
        return Err("TD profiling is enabled");
    }
    if attributes & 0x3fffffff0780ff8e != 0 {
        return Err("TD reserved bits are set");
    }
    if attributes & (1 << 29) != 0 {
        return Err("TD migration is enabled");
    }
    if attributes & (1 << 17) != 0 {
        return Err("TD SERVTD_EXT is enabled");
    }
    if attributes & (1 << 28) == 0 {
        return Err("TD SEPT_VE_DISABLE is not enabled");
    }
    Ok(())
}

pub fn validate_key_times(view: &AttestedKeyView, max_lease_ms: u64) -> Result<(), &'static str> {
    let lease_end_ms = view
        .issued_at_ms
        .checked_add(max_lease_ms)
        .ok_or("u64 lease overflow")?;
    if view.expires_ms != lease_end_ms.min(view.collateral_expires_ms) {
        return Err("lease must start at issue time");
    }
    if !(view.registered_ms < view.expires_ms && view.expires_ms <= view.collateral_expires_ms) {
        return Err("invalid lease ordering");
    }
    if !(view.registered_ms.saturating_sub(7_200_000)..=view.registered_ms)
        .contains(&view.issued_at_ms)
    {
        return Err("issue time outside inclusive two hour window");
    }
    Ok(())
}

pub fn validate_key_view(view: &AttestedKeyView, max_lease_ms: u64) -> Result<(), &'static str> {
    lower_hex::<32>(&view.public_key)?;
    lower_hex::<16>(&view.ppid)?;
    lower_hex::<32>(&view.aux)?;
    let measurement = &view.measurement;
    let expected = measurement_id(
        &lower_hex::<48>(&measurement.mrtd)?,
        &lower_hex::<48>(&measurement.rtmr0)?,
        &lower_hex::<48>(&measurement.rtmr1)?,
        &lower_hex::<48>(&measurement.rtmr2)?,
        &lower_hex::<48>(&measurement.key_provider_digest)?,
        &lower_hex::<32>(&measurement.compose_hash)?,
    );
    if lower_hex::<32>(&view.measurement_id)? != expected {
        return Err("measurement_id mismatch");
    }
    if view.tcb_status != "UpToDate" {
        return Err("expected UpToDate");
    }
    validate_key_times(view, max_lease_ms)
}

pub fn registration_ms(block_timestamp_ns: u64) -> u64 {
    block_timestamp_ns / 1_000_000
}

pub fn validate_vote_expiry(
    now_ms: u64,
    registered_ms: u64,
    key_expires_ms: u64,
    intent_expires_ms: u64,
) -> Result<(), &'static str> {
    if !(registered_ms <= now_ms
        && now_ms < intent_expires_ms
        && intent_expires_ms <= key_expires_ms)
    {
        return Err("vote or key outside live interval");
    }
    Ok(())
}

pub fn report_data(
    registry_account_id: &str,
    public_key: &[u8; 32],
    issued_at_ms: u64,
    aux: &[u8; 32],
) -> [u8; 64] {
    let mut hash = Sha256::new();
    hash.update(b"siyab-attest-v1\0");
    hash.update(registry_account_id.as_bytes());
    hash.update([0]);
    hash.update(public_key);
    hash.update(issued_at_ms.to_be_bytes());
    let mut result = [0; 64];
    result[..32].copy_from_slice(&hash.finalize());
    result[32..].copy_from_slice(aux);
    result
}

pub fn measurement_id(
    mrtd: &[u8; 48],
    rtmr0: &[u8; 48],
    rtmr1: &[u8; 48],
    rtmr2: &[u8; 48],
    key_provider_digest: &[u8; 48],
    compose_hash: &[u8; 32],
) -> [u8; 32] {
    let mut hash = Sha256::new();
    hash.update(b"siyab-measurement-v1");
    for value in [mrtd, rtmr0, rtmr1, rtmr2, key_provider_digest] {
        hash.update(value);
    }
    hash.update(compose_hash);
    hash.finalize().into()
}

pub fn scope_id(measurement_id: &[u8; 32], platforms: &[[u8; 16]]) -> [u8; 32] {
    let mut hash = Sha256::new();
    hash.update(b"siyab-voice-v1");
    hash.update(measurement_id);
    for platform in platforms {
        hash.update(platform);
    }
    hash.finalize().into()
}

pub fn vote_intent_digest(intent: &VoteIntent) -> [u8; 32] {
    let mut hash = Sha256::new();
    hash.update(b"siyab-vote-v1|");
    hash.update(borsh::to_vec(intent).unwrap());
    hash.finalize().into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn fixed<const N: usize>(value: &Value) -> [u8; N] {
        hex::decode(value.as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap()
    }

    fn vectors() -> Value {
        serde_json::from_str(include_str!("../../../fixtures/attest/vectors.json")).unwrap()
    }

    fn measurement(input: &Value) -> [u8; 32] {
        measurement_id(
            &fixed(&input["mrtd"]),
            &fixed(&input["rtmr0"]),
            &fixed(&input["rtmr1"]),
            &fixed(&input["rtmr2"]),
            &fixed(&input["key_provider_digest"]),
            &fixed(&input["compose_hash"]),
        )
    }

    fn intent(input: &Value) -> VoteIntent {
        use base64::{engine::general_purpose::STANDARD, Engine};

        VoteIntent {
            registry_account_id: input["registry_account_id"].as_str().unwrap().into(),
            public_key: fixed(&input["public_key"]),
            measurement_id: fixed(&input["measurement_id"]),
            platforms: input["platforms"]
                .as_array()
                .unwrap()
                .iter()
                .map(fixed)
                .collect(),
            verb: input["verb"].as_str().unwrap().into(),
            approval_id: input["approval_id"].as_str().unwrap().into(),
            wallet_pubkey: input["wallet_pubkey"].as_str().unwrap().into(),
            request_hash: fixed(&input["request_hash"]),
            recipient: input["recipient"].as_str().unwrap().into(),
            nonce: STANDARD
                .decode(input["nonce"].as_str().unwrap())
                .unwrap()
                .try_into()
                .unwrap(),
            evidence_hash: fixed(&input["evidence_hash"]),
            expires_ms: input["expires_ms"].as_u64().unwrap(),
        }
    }

    #[test]
    fn registration_callback_vectors() {
        let data = vectors();
        let original = &data["attested_keys"][0]["view"];
        let lease = data["attested_keys"][0]["max_lease_ms"].as_u64().unwrap();
        for vector in data["reregistration"].as_array().unwrap() {
            let previous: AttestedKeyView = serde_json::from_value(original.clone()).unwrap();
            let mut candidate = original.clone();
            for (key, value) in vector["candidate_changes"].as_object().unwrap() {
                candidate[key] = value.clone();
            }
            let candidate: AttestedKeyView = serde_json::from_value(candidate).unwrap();
            validate_key_view(&candidate, lease).unwrap();
            let mut keys = BTreeMap::from([(previous.public_key.clone(), previous.clone())]);
            let accepted = vector["accepted"].as_bool().unwrap();
            assert_eq!(
                registration_callback(&mut keys, candidate.clone(), lease).is_ok(),
                accepted,
                "{}",
                vector["name"]
            );
            assert_eq!(
                keys[&previous.public_key],
                if accepted { candidate } else { previous }
            );
        }
    }

    #[test]
    fn registration_callbacks_compare_latest_state_in_both_orders() {
        let data = vectors();
        let original: AttestedKeyView =
            serde_json::from_value(data["attested_keys"][0]["view"].clone()).unwrap();
        let mut older = original.clone();
        older.issued_at_ms += 1;
        older.expires_ms += 1;
        let mut newer = older.clone();
        newer.issued_at_ms += 1;
        newer.expires_ms += 1;
        for order in [
            [older.clone(), newer.clone()],
            [newer.clone(), older.clone()],
        ] {
            let mut keys = BTreeMap::from([(original.public_key.clone(), original.clone())]);
            registration_callback(&mut keys, order[0].clone(), 86_400_000).unwrap();
            assert_eq!(
                registration_callback(&mut keys, order[1].clone(), 86_400_000).is_ok(),
                order[1].issued_at_ms > order[0].issued_at_ms
            );
            assert_eq!(keys[&original.public_key], newer);
        }
        let mut keys = BTreeMap::new();
        registration_callback(&mut keys, original.clone(), 86_400_000).unwrap();
        assert_eq!(keys[&original.public_key], original);
    }

    #[test]
    fn registry_config_vectors_and_bounds() {
        let data = vectors();
        let vector = &data["registry_config"][0];
        let config: RegistryConfig =
            serde_json::from_value(vector["new_arguments"].clone()).unwrap();
        validate_config(&config).unwrap();
        assert_eq!(
            serde_json::to_value(&config).unwrap(),
            vector["get_config_result"]
        );
        assert_eq!(vector["get_config_arguments"], serde_json::json!({}));
        let mut invalid = config.clone();
        invalid.max_lease_ms = 0;
        assert!(validate_config(&invalid).is_err());
        for recipients in [
            vec![],
            vec!["outlayer.near"; 2],
            vec!["z.near", "a.near"],
            vec!["a..near"],
            vec!["recipient.near"; 9],
        ] {
            let mut invalid = config.clone();
            invalid.vote_recipients = recipients.into_iter().map(String::from).collect();
            assert!(validate_config(&invalid).is_err());
        }
        for account in ["a", "A.near", "a..near", "a-_near", "a.near."] {
            let mut invalid = config.clone();
            invalid.verifier_account_id = account.into();
            assert!(validate_config(&invalid).is_err());
        }
        let mut unknown = vector["new_arguments"].clone();
        unknown["owner"] = "owner.near".into();
        assert!(serde_json::from_value::<RegistryConfig>(unknown).is_err());
    }

    #[test]
    fn quote_attributes_vectors_and_truncation() {
        for vector in vectors()["quote_td_attributes"].as_array().unwrap() {
            let quote = hex::decode(vector["quote_prefix_hex"].as_str().unwrap()).unwrap();
            assert_eq!(
                validate_quote_td_attributes(&quote).is_ok(),
                vector["accepted"].as_bool().unwrap(),
                "{}",
                vector["name"]
            );
            for length in [0, 1, 47, quote.len() - 1] {
                assert!(validate_quote_td_attributes(&quote[..length]).is_err());
            }
        }
    }

    #[test]
    fn vote_policy_vectors_and_permanent_reservations() {
        let data = vectors();
        let mut config: RegistryConfig =
            serde_json::from_value(data["registry_config"][0]["new_arguments"].clone()).unwrap();
        config.vote_recipients.push("outlayer.testnet".into());
        for vector in data["vote_policy"].as_array().unwrap() {
            let intent = intent(&vector["intent"]);
            let mut key: AttestedKeyView =
                serde_json::from_value(data["attested_keys"][0]["view"].clone()).unwrap();
            key.public_key = hex::encode(intent.public_key);
            let keys = BTreeMap::from([(key.public_key.clone(), key)]);
            let scope = scope_id(&intent.measurement_id, &intent.platforms);
            assert_eq!(hex::encode(scope), vector["scope_id"]);
            let vote_id = (
                scope,
                intent.approval_id.clone(),
                intent.wallet_pubkey.clone(),
                intent.request_hash,
            );
            let mut reservations = VoteReservations::new();
            for verb in vector["previous_verbs"].as_array().unwrap() {
                reservations.insert(vote_id.clone(), verb.as_str().unwrap().into());
            }
            let before = reservations.clone();
            let accepted = vector["accepted"].as_bool().unwrap();
            let result = submit_vote_reference(
                &intent,
                &fixed(&vector["signature"]),
                &config,
                &intent.registry_account_id,
                &keys,
                &mut reservations,
                vector["now_ms"].as_u64().unwrap(),
            );
            assert_eq!(result.is_ok(), accepted, "{}: {:?}", vector["name"], result);
            if accepted {
                assert_eq!(reservations[&vote_id], intent.verb);
                let mut other = intent.clone();
                other.verb = if intent.verb == "approve" {
                    "reject"
                } else {
                    "approve"
                }
                .into();
                other.recipient = "outlayer.testnet".into();
                other.nonce[0] ^= 1;
                other.evidence_hash[0] ^= 1;
                other.expires_ms += 1;
                let seed: [u8; 32] = Sha256::digest(b"gate test agent v1").into();
                use ed25519_dalek::Signer;
                let signature =
                    ed25519_dalek::SigningKey::from_bytes(&seed).sign(&vote_intent_digest(&other));
                assert_eq!(
                    submit_vote_reference(
                        &other,
                        &signature.to_bytes(),
                        &config,
                        &other.registry_account_id,
                        &keys,
                        &mut reservations,
                        vector["now_ms"].as_u64().unwrap()
                    ),
                    Err("opposite verb already reserved")
                );
                assert_eq!(reservations[&vote_id], intent.verb);
            } else {
                assert_eq!(reservations, before);
                assert!(result
                    .unwrap_err()
                    .contains(vector["error"].as_str().unwrap()));
            }
        }
    }

    #[test]
    fn signature_precedes_policy_and_mutation() {
        let data = vectors();
        let vector = &data["vote_policy"][0];
        let original = intent(&vector["intent"]);
        let signature = fixed(&vector["signature"]);
        verify_vote_signature(&original, &signature, &original.registry_account_id).unwrap();
        let mut changes = Vec::new();
        let mut changed = original.clone();
        changed.verb = "reject".into();
        changes.push(changed);
        let mut changed = original.clone();
        changed.public_key = [0xff; 32];
        changes.push(changed);
        let mut changed = original.clone();
        changed.registry_account_id = "other.near".into();
        changes.push(changed);
        let mut changed = original.clone();
        changed.recipient = "other.near".into();
        changes.push(changed);
        let mut changed = original.clone();
        changed.measurement_id[0] ^= 1;
        changes.push(changed);
        let mut changed = original.clone();
        changed.platforms = vec![[0x11; 16]];
        changes.push(changed);
        let mut changed = original.clone();
        changed.approval_id = "ffffffff-ffff-ffff-ffff-ffffffffffff".into();
        changes.push(changed);
        let mut changed = original.clone();
        changed.wallet_pubkey = "other-key".into();
        changes.push(changed);
        let mut changed = original.clone();
        changed.request_hash[0] ^= 1;
        changes.push(changed);
        let mut changed = original.clone();
        changed.nonce[0] ^= 1;
        changes.push(changed);
        let mut changed = original.clone();
        changed.evidence_hash[0] ^= 1;
        changes.push(changed);
        let mut changed = original.clone();
        changed.expires_ms += 1;
        changes.push(changed);
        let mut invalid_config: RegistryConfig =
            serde_json::from_value(data["registry_config"][0]["new_arguments"].clone()).unwrap();
        invalid_config.max_lease_ms = 0;
        for changed in changes {
            let mut reservations = VoteReservations::new();
            let expected = if changed.registry_account_id == original.registry_account_id {
                "invalid vote signature"
            } else {
                "wrong registry"
            };
            assert_eq!(
                submit_vote_reference(
                    &changed,
                    &signature,
                    &invalid_config,
                    &original.registry_account_id,
                    &BTreeMap::new(),
                    &mut reservations,
                    0
                ),
                Err(expected)
            );
            assert!(reservations.is_empty());
        }
        assert_eq!(
            verify_vote_signature(&original, &[0; 64], &original.registry_account_id),
            Err("invalid vote signature")
        );
        let mut small_order = original;
        small_order.public_key = [0; 32];
        small_order.public_key[0] = 1;
        let mut forged = [0; 64];
        forged[0] = 1;
        assert_eq!(
            verify_vote_signature(&small_order, &forged, &small_order.registry_account_id),
            Err("invalid vote signature")
        );

        let mut other_registry = small_order;
        other_registry.public_key = fixed(&vector["intent"]["public_key"]);
        other_registry.registry_account_id = "other.near".into();
        let seed: [u8; 32] = Sha256::digest(b"gate test agent v1").into();
        use ed25519_dalek::Signer;
        let signature =
            ed25519_dalek::SigningKey::from_bytes(&seed).sign(&vote_intent_digest(&other_registry));
        let mut reservations = VoteReservations::new();
        assert_eq!(
            submit_vote_reference(
                &other_registry,
                &signature.to_bytes(),
                &invalid_config,
                vector["intent"]["registry_account_id"].as_str().unwrap(),
                &BTreeMap::new(),
                &mut reservations,
                0
            ),
            Err("invalid vote signature")
        );
        assert!(reservations.is_empty());
    }

    #[test]
    fn vote_reservation_scope_vectors() {
        let data = vectors();
        let config: RegistryConfig =
            serde_json::from_value(data["registry_config"][0]["new_arguments"].clone()).unwrap();
        for vector in data["vote_reservations"].as_array().unwrap() {
            let mut reservations = VoteReservations::new();
            let mut scopes = Vec::new();
            let steps = vector["steps"].as_array().unwrap();
            for step in steps {
                let intent = intent(&step["intent"]);
                let mut key = data["attested_keys"][0]["view"].clone();
                for (field, value) in step["key_changes"].as_object().unwrap() {
                    key[field] = value.clone();
                }
                let key: AttestedKeyView = serde_json::from_value(key).unwrap();
                validate_key_view(&key, config.max_lease_ms).unwrap();
                let signature = fixed(&step["signature"]);
                verify_vote_signature(&intent, &signature, &intent.registry_account_id).unwrap();
                let scope = scope_id(&intent.measurement_id, &intent.platforms);
                assert_eq!(hex::encode(scope), step["scope_id"]);
                scopes.push(scope);
                let before = reservations.clone();
                let result = submit_vote_reference(
                    &intent,
                    &signature,
                    &config,
                    &intent.registry_account_id,
                    &BTreeMap::from([(key.public_key.clone(), key)]),
                    &mut reservations,
                    vector["now_ms"].as_u64().unwrap(),
                );
                assert_eq!(
                    result.is_ok(),
                    step["accepted"].as_bool().unwrap(),
                    "{}: {:?}",
                    vector["name"],
                    result
                );
                if result.is_ok() {
                    assert_eq!(
                        reservations.len(),
                        scopes
                            .iter()
                            .collect::<std::collections::BTreeSet<_>>()
                            .len()
                    );
                } else {
                    assert_eq!(result, Err("opposite verb already reserved"));
                    assert_eq!(reservations, before);
                }
            }
            assert_eq!(
                scopes[0] == scopes[1],
                vector["same_scope"].as_bool().unwrap()
            );
            assert_ne!(
                steps[0]["intent"]["public_key"],
                steps[1]["intent"]["public_key"]
            );
            for field in ["approval_id", "wallet_pubkey", "request_hash"] {
                assert_eq!(steps[0]["intent"][field], steps[1]["intent"][field]);
            }
            if !vector["same_scope"].as_bool().unwrap() {
                assert_eq!(steps[0]["intent"]["verb"], "approve");
                assert_eq!(steps[1]["intent"]["verb"], "reject");
            }
        }
    }

    #[test]
    fn wire_result_vectors() {
        for vector in vectors()["call_wire"].as_array().unwrap() {
            assert_eq!(vector["deposit_yocto"], "10000000000000000000000");
            assert_eq!(vector["gas_tgas"], 300);
            assert_eq!(vector["callee_gas_tgas"], 200);
            assert_eq!(vector["callback_gas_tgas"], 60);
            let result = &vector["result"];
            if result.is_null() {
                continue;
            }
            if vector["method"] == "register_key" {
                let view: AttestedKeyView = serde_json::from_value(result.clone()).unwrap();
                validate_key_view(&view, 86_400_000).unwrap();
            } else {
                assert_eq!(result.as_object().unwrap().len(), 3);
                let path = result["vote_path"].as_str().unwrap();
                assert_eq!(&path[..5], "vote/");
                lower_hex::<32>(&path[5..]).unwrap();
                let _: [u8; 32] = fixed(&result["nep413_digest"]);
                let _: [u8; 64] = fixed(&result["signature"]);
            }
        }
    }

    #[test]
    fn report_data_vectors() {
        for vector in vectors()["report_data"].as_array().unwrap() {
            let input = &vector["input"];
            let output = report_data(
                input["registry_account_id"].as_str().unwrap(),
                &fixed(&input["public_key"]),
                input["issued_at_ms"].as_u64().unwrap(),
                &fixed(&input["aux"]),
            );
            let preimage = hex::decode(vector["preimage_hex"].as_str().unwrap()).unwrap();
            assert_eq!(&output[..32], &Sha256::digest(preimage)[..]);
            assert_eq!(hex::encode(output), vector["report_data_hex"]);
        }
    }

    #[test]
    fn measurement_vectors() {
        for vector in vectors()["measurements"].as_array().unwrap() {
            let output = measurement(&vector["input"]);
            let preimage = hex::decode(vector["preimage_hex"].as_str().unwrap()).unwrap();
            assert_eq!(output.as_slice(), &Sha256::digest(preimage)[..]);
            assert_eq!(hex::encode(output), vector["measurement_id"]);
        }
    }

    #[test]
    fn scope_vectors() {
        for vector in vectors()["scopes"].as_array().unwrap() {
            let input = &vector["input"];
            let platforms: Vec<[u8; 16]> = input["platforms"]
                .as_array()
                .unwrap()
                .iter()
                .map(fixed)
                .collect();
            assert!((1..=8).contains(&platforms.len()));
            assert!(platforms.windows(2).all(|pair| pair[0] < pair[1]));
            let output = scope_id(&fixed(&input["measurement_id"]), &platforms);
            assert_eq!(hex::encode(output), vector["scope_id"]);
            assert_eq!(format!("vote/{}", hex::encode(output)), vector["vote_path"]);
        }
    }

    #[test]
    fn vote_vectors() {
        use base64::{engine::general_purpose::STANDARD, Engine};

        for vector in vectors()["votes"].as_array().unwrap() {
            let input = &vector["input"];
            let nonce = STANDARD.decode(input["nonce"].as_str().unwrap()).unwrap();
            let intent = VoteIntent {
                registry_account_id: input["registry_account_id"].as_str().unwrap().into(),
                public_key: fixed(&input["public_key"]),
                measurement_id: fixed(&input["measurement_id"]),
                platforms: input["platforms"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(fixed)
                    .collect(),
                verb: input["verb"].as_str().unwrap().into(),
                approval_id: input["approval_id"].as_str().unwrap().into(),
                wallet_pubkey: input["wallet_pubkey"].as_str().unwrap().into(),
                request_hash: fixed(&input["request_hash"]),
                recipient: input["recipient"].as_str().unwrap().into(),
                nonce: nonce.try_into().unwrap(),
                evidence_hash: fixed(&input["evidence_hash"]),
                expires_ms: input["expires_ms"].as_u64().unwrap(),
            };
            let mut preimage = b"siyab-vote-v1|".to_vec();
            preimage.extend(borsh::to_vec(&intent).unwrap());
            assert_eq!(hex::encode(preimage), vector["intent_preimage_hex"]);
            assert_eq!(
                hex::encode(vote_intent_digest(&intent)),
                vector["intent_digest"]
            );
            let message = format!(
                "{}:{}:{}:{}",
                intent.verb,
                intent.approval_id,
                intent.wallet_pubkey,
                hex::encode(intent.request_hash)
            );
            assert_eq!(message, vector["nep413_message"]);
            let payload = (
                2_u32.pow(31) + 413,
                message,
                intent.nonce,
                intent.recipient,
                Option::<String>::None,
            );
            let nep413 = borsh::to_vec(&payload).unwrap();
            assert_eq!(hex::encode(&nep413), vector["nep413_preimage_hex"]);
            assert_eq!(hex::encode(Sha256::digest(nep413)), vector["nep413_digest"]);
        }
    }

    #[test]
    fn attested_key_view_vectors() {
        for vector in vectors()["attested_keys"].as_array().unwrap() {
            let view: AttestedKeyView = serde_json::from_value(vector["view"].clone()).unwrap();
            assert_eq!(serde_json::to_value(&view).unwrap(), vector["view"]);
            assert_eq!(
                view.measurement_id,
                hex::encode(measurement(&vector["view"]["measurement"]))
            );
            assert_eq!(view.tcb_status, "UpToDate");
            validate_key_times(&view, vector["max_lease_ms"].as_u64().unwrap()).unwrap();
        }
    }

    #[test]
    fn td_attributes_vectors() {
        for vector in vectors()["td_attributes"].as_array().unwrap() {
            assert_eq!(
                validate_td_attributes(fixed(&vector["td_attributes"])).is_ok(),
                vector["accepted"].as_bool().unwrap(),
                "{}",
                vector["name"]
            );
        }
    }

    #[test]
    fn key_issue_time_vectors() {
        let data = vectors();
        for vector in data["key_issue_times"].as_array().unwrap() {
            let mut view = data["attested_keys"][0]["view"].clone();
            for (key, value) in vector["times"].as_object().unwrap() {
                view[key] = value.clone();
            }
            let view: AttestedKeyView = serde_json::from_value(view).unwrap();
            assert_eq!(
                validate_key_times(&view, vector["max_lease_ms"].as_u64().unwrap()).is_ok(),
                vector["accepted"].as_bool().unwrap(),
                "{}",
                vector["name"]
            );
        }
    }

    #[test]
    fn get_key_argument_vectors() {
        for vector in vectors()["get_key_args"].as_array().unwrap() {
            assert_eq!(vector["method"], "get_key");
            let key: [u8; 32] = fixed(&vector["public_key"]);
            let arguments = format!("{{\"public_key\":\"{}\"}}", hex::encode(key));
            assert_eq!(
                hex::encode(arguments.as_bytes()),
                vector["arguments_json_hex"]
            );
        }
    }

    #[test]
    fn registration_argument_vectors() {
        use tee_verifier_interface::{Collateral, QuoteBytes};

        for vector in vectors()["registration_args"].as_array().unwrap() {
            assert_eq!(vector["method"], "register_key");
            let arguments = &vector["arguments"];
            assert_eq!(arguments.as_object().unwrap().len(), 6);
            let _: [u8; 32] = fixed(&arguments["public_key"]);
            let _: [u8; 32] = fixed(&arguments["aux"]);
            arguments["issued_at_ms"].as_u64().unwrap();
            let input = &vector["verification_input"];
            let quote = QuoteBytes(hex::decode(input["quote_hex"].as_str().unwrap()).unwrap());
            let collateral: Collateral =
                serde_json::from_value(input["collateral"].clone()).unwrap();
            let encoded = borsh::to_vec(&(quote.clone(), collateral.clone())).unwrap();
            assert_eq!(hex::encode(&encoded), arguments["verification_args"]);
            assert_eq!(
                borsh::from_slice::<(QuoteBytes, Collateral)>(&encoded).unwrap(),
                (quote, collateral)
            );
            let mut trailing = encoded;
            trailing.push(0);
            assert!(borsh::from_slice::<(QuoteBytes, Collateral)>(&trailing).is_err());
            let compose = arguments["app_compose"].as_str().unwrap();
            assert_eq!(
                hex::encode(Sha256::digest(compose.as_bytes())),
                arguments["event_log"][0]["event_payload"]
            );
            for event in arguments["event_log"].as_array().unwrap() {
                assert_eq!(event.as_object().unwrap().len(), 5);
                assert_eq!(event["imr"], 3);
                let event_type: u32 = event["event_type"].as_u64().unwrap().try_into().unwrap();
                let mut hash = sha2::Sha384::new();
                hash.update(event_type.to_le_bytes());
                hash.update(b":");
                hash.update(event["event"].as_str().unwrap().as_bytes());
                hash.update(b":");
                hash.update(hex::decode(event["event_payload"].as_str().unwrap()).unwrap());
                assert_eq!(hex::encode(hash.finalize()), event["digest"]);
            }
        }
    }

    #[test]
    fn vote_submission_and_signing_request_vectors() {
        let data = vectors();
        for vector in data["vote_submission_args"].as_array().unwrap() {
            assert_eq!(vector["method"], "submit_vote");
            let arguments = &vector["arguments"];
            assert_eq!(arguments.as_object().unwrap().len(), 2);
            let vote = data["votes"]
                .as_array()
                .unwrap()
                .iter()
                .find(|vote| vote["name"] == vector["name"])
                .unwrap();
            assert_eq!(arguments["intent"], vote["input"]);
            assert_eq!(
                fixed::<64>(&arguments["signature"]),
                std::array::from_fn(|index| index as u8)
            );
            let signing = data["signing_requests"]
                .as_array()
                .unwrap()
                .iter()
                .find(|signing| signing["name"] == vector["name"])
                .unwrap();
            let intent = &arguments["intent"];
            let platforms: Vec<[u8; 16]> = intent["platforms"]
                .as_array()
                .unwrap()
                .iter()
                .map(fixed)
                .collect();
            let path = format!(
                "vote/{}",
                hex::encode(scope_id(&fixed(&intent["measurement_id"]), &platforms))
            );
            assert_eq!(signing["method"], "sign");
            assert_eq!(signing["predecessor"], intent["registry_account_id"]);
            assert_eq!(signing["path_utf8_hex"], hex::encode(path.as_bytes()));
            let payload = hex::encode(Sha256::digest(
                hex::decode(vote["nep413_preimage_hex"].as_str().unwrap()).unwrap(),
            ));
            assert_eq!(
                signing["arguments"],
                serde_json::json!({"request": {
                    "path": path,
                    "payload_v2": {"Eddsa": payload},
                    "domain_id": signing["ed25519_domain_id"].as_u64().unwrap(),
                }})
            );
        }
    }

    #[test]
    fn registration_clock_vectors() {
        for vector in vectors()["registration_clock"].as_array().unwrap() {
            assert_eq!(
                registration_ms(vector["block_timestamp_ns"].as_u64().unwrap()),
                vector["registered_ms"].as_u64().unwrap(),
                "{}",
                vector["name"]
            );
        }
    }

    #[test]
    fn vote_expiry_vectors() {
        for vector in vectors()["vote_expiry"].as_array().unwrap() {
            let input = &vector["input"];
            assert_eq!(
                validate_vote_expiry(
                    input["now_ms"].as_u64().unwrap(),
                    input["registered_ms"].as_u64().unwrap(),
                    input["key_expires_ms"].as_u64().unwrap(),
                    input["intent_expires_ms"].as_u64().unwrap(),
                )
                .is_ok(),
                vector["accepted"].as_bool().unwrap(),
                "{}",
                vector["name"]
            );
        }
    }
}
