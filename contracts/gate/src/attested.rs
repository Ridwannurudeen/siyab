use crate::{
    events::emit,
    types::{
        ApprovedMeasurement, AttestationRegistryConfig, AttestedKeyView, AttestedMode, Measurement,
        PlatformChange, PlatformPolicy,
    },
    verdict::hex_decode,
    Gate, GateExt, ATTESTED_DENIED_KEYS_PREFIX, ATTESTED_KEYS_PREFIX, ATTESTED_MEASUREMENTS_PREFIX,
    ATTESTED_MODE_PREFIX, ATTESTED_PLATFORMS_PREFIX,
};
use near_sdk::{
    borsh::{self, BorshDeserialize, BorshSerialize},
    env, near,
    serde_json::{self, json, Value},
    AccountId, Gas, GasWeight, NearToken, Promise, PromiseOrValue,
};

const MIN_MEASUREMENT_TIMELOCK_MS: u64 = 86_400_000;
const MAX_MEASUREMENT_TIMELOCK_MS: u64 = 2_592_000_000;
const MAX_CACHED_KEYS: usize = 4;
const MAX_REGISTRY_RESULT_BYTES: usize = 16_384;
const GAS_REGISTRY_VIEW: Gas = Gas::from_tgas(10);
const GAS_CONFIG_CALLBACK: Gas = Gas::from_tgas(30);
const GAS_KEY_CALLBACK: Gas = Gas::from_tgas(10);

fn read<T: BorshDeserialize>(key: &[u8]) -> Option<T> {
    env::storage_read(key).map(|bytes| {
        T::try_from_slice(&bytes).unwrap_or_else(|_| env::panic_str("invalid attested state"))
    })
}

fn write<T: BorshSerialize>(key: &[u8], value: &T) {
    env::storage_write(
        key,
        &borsh::to_vec(value).expect("attested state serialization"),
    );
}

pub(crate) fn mode() -> Option<AttestedMode> {
    read(ATTESTED_MODE_PREFIX)
}

fn measurements() -> Vec<ApprovedMeasurement> {
    read(ATTESTED_MEASUREMENTS_PREFIX).unwrap_or_default()
}

fn cached_keys() -> Vec<AttestedKeyView> {
    read(ATTESTED_KEYS_PREFIX).unwrap_or_default()
}

fn denied_keys() -> Vec<String> {
    read(ATTESTED_DENIED_KEYS_PREFIX).unwrap_or_default()
}

fn platforms() -> PlatformPolicy {
    read(ATTESTED_PLATFORMS_PREFIX).expect("attested platforms")
}

fn effective_platforms(policy: &PlatformPolicy, now_ms: u64) -> &[String] {
    match &policy.pending {
        Some(change) if change.active_from_ms <= now_ms => &change.platforms,
        _ => &policy.platforms,
    }
}

fn valid_platforms(values: &[String]) -> bool {
    (1..=8).contains(&values.len())
        && values.iter().all(|value| hex_decode(value, 16).is_ok())
        && values.windows(2).all(|pair| pair[0] < pair[1])
}

fn approved(record: &AttestedKeyView, now_ms: u64) -> bool {
    record.registered_ms <= now_ms
        && now_ms < record.expires_ms
        && measurements().iter().any(|measurement| {
            measurement.measurement_id == record.measurement_id
                && measurement.active_from_ms <= now_ms
        })
        && effective_platforms(&platforms(), now_ms).contains(&record.ppid)
        && !denied_keys().contains(&record.public_key)
}

pub(crate) fn usable_keys(now_ms: u64) -> Vec<[u8; 32]> {
    cached_keys()
        .iter()
        .take(MAX_CACHED_KEYS)
        .filter(|record| approved(record, now_ms))
        .map(|record| {
            hex_decode(&record.public_key, 32)
                .expect("cached attested public key")
                .try_into()
                .expect("cached attested key width")
        })
        .collect()
}

fn measurement_id(measurement: &Measurement) -> Result<String, &'static str> {
    let mut preimage = b"siyab-measurement-v1".to_vec();
    for value in [
        &measurement.mrtd,
        &measurement.rtmr0,
        &measurement.rtmr1,
        &measurement.rtmr2,
        &measurement.key_provider_digest,
    ] {
        preimage.extend(hex_decode(value, 48)?);
    }
    preimage.extend(hex_decode(&measurement.compose_hash, 32)?);
    Ok(crate::lower_hex(&env::sha256(&preimage)))
}

fn valid_record(record: &AttestedKeyView, public_key: &str, max_lease_ms: u64) -> bool {
    if record.public_key != public_key
        || hex_decode(&record.public_key, 32).is_err()
        || hex_decode(&record.measurement_id, 32).is_err()
        || hex_decode(&record.ppid, 16).is_err()
        || hex_decode(&record.aux, 32).is_err()
        || record.tcb_status != "UpToDate"
        || measurement_id(&record.measurement).as_ref() != Ok(&record.measurement_id)
    {
        return false;
    }
    let Some(lease_end_ms) = record.issued_at_ms.checked_add(max_lease_ms) else {
        return false;
    };
    max_lease_ms > 0
        && record.issued_at_ms >= record.registered_ms.saturating_sub(7_200_000)
        && record.issued_at_ms <= record.registered_ms
        && record.registered_ms < record.expires_ms
        && record.expires_ms == lease_end_ms.min(record.collateral_expires_ms)
}

fn cache_record(record: AttestedKeyView, now_ms: u64) -> bool {
    let mut keys = cached_keys();
    if let Some(index) = keys
        .iter()
        .position(|key| key.public_key == record.public_key)
    {
        let previous = &keys[index];
        if previous == &record {
            return true;
        }
        if record.issued_at_ms <= previous.issued_at_ms
            || record.registered_ms < previous.registered_ms
            || record.measurement_id != previous.measurement_id
            || record.ppid != previous.ppid
        {
            return false;
        }
        if record.aux == previous.aux {
            keys[index] = record;
        } else {
            keys.remove(index);
            keys.push(record);
        }
    } else {
        if keys.len() == MAX_CACHED_KEYS {
            let Some(index) = keys.iter().position(|key| key.expires_ms <= now_ms) else {
                return false;
            };
            keys.remove(index);
        }
        keys.push(record);
    }
    write(ATTESTED_KEYS_PREFIX, &keys);
    true
}

#[near]
impl Gate {
    #[payable]
    pub fn enable_attested_mode(
        &mut self,
        registry_id: AccountId,
        measurement_id: String,
        measurement_timelock_ms: u64,
        platforms: Option<Vec<String>>,
    ) {
        self.assert_owner();
        near_sdk::require!(mode().is_none(), "REFUSED:attested_mode");
        near_sdk::require!(
            hex_decode(&measurement_id, 32).is_ok(),
            "REFUSED:bad_request"
        );
        near_sdk::require!(
            (MIN_MEASUREMENT_TIMELOCK_MS..=MAX_MEASUREMENT_TIMELOCK_MS)
                .contains(&measurement_timelock_ms),
            "REFUSED:bad_request"
        );
        let platforms = platforms.unwrap_or_else(|| env::panic_str("REFUSED:bad_request"));
        near_sdk::require!(valid_platforms(&platforms), "REFUSED:bad_request");
        write(
            ATTESTED_MODE_PREFIX,
            &AttestedMode {
                registry_id: registry_id.clone(),
                measurement_timelock_ms,
            },
        );
        write(
            ATTESTED_MEASUREMENTS_PREFIX,
            &vec![ApprovedMeasurement {
                measurement_id: measurement_id.clone(),
                label: String::new(),
                active_from_ms: env::block_timestamp_ms(),
            }],
        );
        write(
            ATTESTED_PLATFORMS_PREFIX,
            &PlatformPolicy {
                platforms,
                pending: None,
            },
        );
        self.verifier_current =
            env::sha256_array(b"siyab gate: attested mode, no operator verifier key");
        self.verifier_previous = None;
        emit(
            "attested_mode_enabled",
            json!({"registry_id": registry_id, "measurement_id": measurement_id,
            "measurement_timelock_ms": measurement_timelock_ms}),
        );
    }

    #[payable]
    pub fn propose_measurement(&mut self, measurement_id: String, label: String) {
        self.assert_owner();
        let mode = mode().unwrap_or_else(|| env::panic_str("REFUSED:legacy_mode"));
        near_sdk::require!(
            hex_decode(&measurement_id, 32).is_ok(),
            "REFUSED:bad_request"
        );
        let mut approved = measurements();
        near_sdk::require!(
            !approved
                .iter()
                .any(|value| value.measurement_id == measurement_id),
            "REFUSED:measurement_exists"
        );
        let active_from_ms = env::block_timestamp_ms()
            .checked_add(mode.measurement_timelock_ms)
            .unwrap_or_else(|| env::panic_str("REFUSED:bad_request"));
        approved.push(ApprovedMeasurement {
            measurement_id: measurement_id.clone(),
            label: label.clone(),
            active_from_ms,
        });
        write(ATTESTED_MEASUREMENTS_PREFIX, &approved);
        emit(
            "measurement_proposed",
            json!({"measurement_id": measurement_id, "label": label, "active_from_ms": active_from_ms}),
        );
    }

    #[payable]
    pub fn revoke_measurement(&mut self, measurement_id: String) {
        self.assert_owner();
        near_sdk::require!(mode().is_some(), "REFUSED:legacy_mode");
        near_sdk::require!(
            hex_decode(&measurement_id, 32).is_ok(),
            "REFUSED:bad_request"
        );
        let mut approved = measurements();
        approved.retain(|value| value.measurement_id != measurement_id);
        write(ATTESTED_MEASUREMENTS_PREFIX, &approved);
        let mut keys = cached_keys();
        keys.retain(|key| key.measurement_id != measurement_id);
        write(ATTESTED_KEYS_PREFIX, &keys);
        emit(
            "measurement_revoked",
            json!({"measurement_id": measurement_id}),
        );
    }

    #[payable]
    pub fn set_platforms(&mut self, platforms: Option<Vec<String>>) {
        self.assert_owner();
        let mode = mode().unwrap_or_else(|| env::panic_str("REFUSED:legacy_mode"));
        let requested = platforms.unwrap_or_else(|| env::panic_str("REFUSED:bad_request"));
        near_sdk::require!(valid_platforms(&requested), "REFUSED:bad_request");
        let now_ms = env::block_timestamp_ms();
        let policy = self::platforms();
        let current = effective_platforms(&policy, now_ms);
        let narrowing = requested.iter().all(|value| current.contains(value));
        let policy = if narrowing {
            PlatformPolicy {
                platforms: requested,
                pending: None,
            }
        } else {
            PlatformPolicy {
                platforms: current
                    .iter()
                    .filter(|value| requested.contains(value))
                    .cloned()
                    .collect(),
                pending: Some(PlatformChange {
                    platforms: requested,
                    active_from_ms: now_ms
                        .checked_add(mode.measurement_timelock_ms)
                        .unwrap_or_else(|| env::panic_str("REFUSED:bad_request")),
                }),
            }
        };
        let mut keys = cached_keys();
        keys.retain(|key| effective_platforms(&policy, now_ms).contains(&key.ppid));
        write(ATTESTED_KEYS_PREFIX, &keys);
        write(ATTESTED_PLATFORMS_PREFIX, &policy);
        emit(
            "platforms_updated",
            json!({"platforms": policy.platforms, "pending_platforms": policy.pending}),
        );
    }

    #[payable]
    pub fn deny_verifier_key(&mut self, public_key: String) {
        self.assert_owner();
        near_sdk::require!(mode().is_some(), "REFUSED:legacy_mode");
        near_sdk::require!(hex_decode(&public_key, 32).is_ok(), "REFUSED:bad_request");
        let mut denied = denied_keys();
        if !denied.contains(&public_key) {
            denied.push(public_key.clone());
            write(ATTESTED_DENIED_KEYS_PREFIX, &denied);
        }
        let mut keys = cached_keys();
        keys.retain(|key| key.public_key != public_key);
        write(ATTESTED_KEYS_PREFIX, &keys);
        emit("verifier_key_denied", json!({"public_key": public_key}));
    }

    pub fn sync_verifier(&mut self, public_key: String) -> Promise {
        let mode = mode().unwrap_or_else(|| env::panic_str("REFUSED:legacy_mode"));
        near_sdk::require!(hex_decode(&public_key, 32).is_ok(), "REFUSED:bad_request");
        Promise::new(mode.registry_id)
            .function_call_weight(
                "get_config",
                b"{}".to_vec(),
                NearToken::from_yoctonear(0),
                GAS_REGISTRY_VIEW,
                GasWeight(0),
            )
            .then(
                Self::ext(env::current_account_id())
                    .with_static_gas(GAS_CONFIG_CALLBACK)
                    .with_unused_gas_weight(0)
                    .on_attestation_config(public_key),
            )
    }

    #[private]
    pub fn on_attestation_config(&mut self, public_key: String) -> PromiseOrValue<bool> {
        let Ok(bytes) = env::promise_result_checked(0, MAX_REGISTRY_RESULT_BYTES) else {
            return PromiseOrValue::Value(false);
        };
        let Ok(config) = serde_json::from_slice::<AttestationRegistryConfig>(&bytes) else {
            return PromiseOrValue::Value(false);
        };
        if config.max_lease_ms == 0
            || !(1..=8).contains(&config.vote_recipients.len())
            || !config
                .vote_recipients
                .windows(2)
                .all(|pair| pair[0] < pair[1])
        {
            return PromiseOrValue::Value(false);
        }
        let Some(mode) = mode() else {
            return PromiseOrValue::Value(false);
        };
        PromiseOrValue::Promise(
            Promise::new(mode.registry_id)
                .function_call_weight(
                    "get_key",
                    json!({"public_key": public_key}).to_string().into_bytes(),
                    NearToken::from_yoctonear(0),
                    GAS_REGISTRY_VIEW,
                    GasWeight(0),
                )
                .then(
                    Self::ext(env::current_account_id())
                        .with_static_gas(GAS_KEY_CALLBACK)
                        .with_unused_gas_weight(0)
                        .on_verifier_synced(public_key, config.max_lease_ms),
                ),
        )
    }

    #[private]
    pub fn on_verifier_synced(&mut self, public_key: String, max_lease_ms: u64) -> bool {
        let Ok(bytes) = env::promise_result_checked(0, MAX_REGISTRY_RESULT_BYTES) else {
            return false;
        };
        let Ok(Some(record)) = serde_json::from_slice::<Option<AttestedKeyView>>(&bytes) else {
            return false;
        };
        let now_ms = env::block_timestamp_ms();
        if mode().is_none()
            || !valid_record(&record, &public_key, max_lease_ms)
            || !approved(&record, now_ms)
        {
            return false;
        }
        cache_record(record, now_ms)
    }

    pub fn get_attestation(&self) -> Value {
        let Some(mode) = mode() else {
            return json!({"enabled": false, "registry_id": null, "measurement_timelock_ms": null,
                "measurements": [], "platforms": [], "pending_platforms": null, "denied_keys": [], "keys": []});
        };
        let now_ms = env::block_timestamp_ms();
        let policy = platforms();
        let pending = policy
            .pending
            .as_ref()
            .filter(|change| change.active_from_ms > now_ms);
        json!({"enabled": true, "registry_id": mode.registry_id,
            "measurement_timelock_ms": mode.measurement_timelock_ms, "measurements": measurements(),
            "platforms": effective_platforms(&policy, now_ms), "pending_platforms": pending,
            "denied_keys": denied_keys(), "keys": cached_keys()})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::{
        mock::MockAction,
        test_utils::{get_created_receipts, VMContextBuilder},
        testing_env, CurveType, PromiseResult, PublicKey,
    };

    fn vectors() -> Value {
        serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../fixtures/attest/vectors.json"
        )))
        .unwrap()
    }

    fn record() -> AttestedKeyView {
        serde_json::from_value(vectors()["attested_keys"][0]["view"].clone()).unwrap()
    }

    fn context(now_ms: u64, owner: bool, result: Option<PromiseResult>) {
        let builder = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id(
                if owner {
                    "owner.testnet"
                } else {
                    "gate.testnet"
                }
                .parse()
                .unwrap(),
            )
            .attached_deposit(NearToken::from_yoctonear(u128::from(owner)))
            .block_timestamp(now_ms * 1_000_000)
            .prepaid_gas(Gas::from_tgas(300))
            .build();
        testing_env!(
            builder,
            near_sdk::test_vm_config(),
            near_sdk::RuntimeFeesConfig::test(),
            Default::default(),
            result.into_iter().collect()
        );
    }

    fn gate() -> Gate {
        context(record().registered_ms, true, None);
        Gate::new(
            "owner.testnet".parse().unwrap(),
            PublicKey::from_parts(CurveType::ED25519, vec![1; 32]).unwrap(),
            300_000,
        )
    }

    fn enabled() -> Gate {
        let mut gate = gate();
        let record = record();
        gate.enable_attested_mode(
            "attest.testnet".parse().unwrap(),
            record.measurement_id,
            MIN_MEASUREMENT_TIMELOCK_MS,
            Some(vec![record.ppid]),
        );
        gate
    }

    fn sync(gate: &mut Gate, record: &AttestedKeyView, now_ms: u64) -> bool {
        context(
            now_ms,
            false,
            Some(PromiseResult::Successful(
                serde_json::to_vec(record).unwrap(),
            )),
        );
        gate.on_verifier_synced(record.public_key.clone(), 86_400_000)
    }

    #[test]
    fn frozen_measurements_and_key_view_match() {
        context(0, false, None);
        for vector in vectors()["measurements"].as_array().unwrap() {
            let measurement = serde_json::from_value(vector["input"].clone()).unwrap();
            assert_eq!(
                measurement_id(&measurement).unwrap(),
                vector["measurement_id"]
            );
        }
        let record = record();
        assert!(valid_record(&record, &record.public_key, 86_400_000));
    }

    #[test]
    fn frozen_issue_window_and_overflow_vectors_match() {
        context(0, false, None);
        for vector in vectors()["key_issue_times"].as_array().unwrap() {
            let mut value = serde_json::to_value(record()).unwrap();
            for (key, field) in vector["times"].as_object().unwrap() {
                value[key] = field.clone();
            }
            let candidate: AttestedKeyView = serde_json::from_value(value).unwrap();
            assert_eq!(
                valid_record(
                    &candidate,
                    &candidate.public_key,
                    vector["max_lease_ms"].as_u64().unwrap()
                ),
                vector["accepted"].as_bool().unwrap(),
                "{}",
                vector["name"]
            );
        }
    }

    #[test]
    fn frozen_config_and_canonical_get_key_arguments_match() {
        let values = vectors();
        let config: AttestationRegistryConfig =
            serde_json::from_value(values["registry_config"][0]["get_config_result"].clone())
                .unwrap();
        assert_eq!(
            serde_json::to_value(config).unwrap(),
            values["registry_config"][0]["get_config_result"]
        );
        let mut gate = enabled();
        let _ = gate.sync_verifier(record().public_key);
        let receipts = get_created_receipts();
        assert_eq!(receipts[0].receiver_id.as_str(), "attest.testnet");
        assert_eq!(receipts[1].receiver_id.as_str(), "gate.testnet");
        context(
            record().registered_ms,
            false,
            Some(PromiseResult::Successful(
                serde_json::to_vec(&values["registry_config"][0]["get_config_result"]).unwrap(),
            )),
        );
        drop(gate.on_attestation_config(record().public_key));
        let receipts = get_created_receipts();
        let MockAction::FunctionCallWeight {
            method_name,
            args,
            attached_deposit,
            prepaid_gas,
            gas_weight,
            ..
        } = &receipts[0].actions[0]
        else {
            panic!("registry view call")
        };
        assert_eq!(method_name, b"get_key");
        assert_eq!(
            crate::lower_hex(args),
            values["get_key_args"][0]["arguments_json_hex"]
        );
        assert_eq!(*attached_deposit, NearToken::from_yoctonear(0));
        assert_eq!(*prepaid_gas, GAS_REGISTRY_VIEW);
        assert_eq!(gas_weight.0, 0);
    }

    #[test]
    fn enabling_burns_both_legacy_slots_without_changing_root_layout() {
        let mut gate = gate();
        gate.verifier_previous = Some(([2; 32], u64::MAX));
        let before = borsh::to_vec(&gate).unwrap();
        let record = record();
        gate.enable_attested_mode(
            "attest.testnet".parse().unwrap(),
            record.measurement_id,
            MIN_MEASUREMENT_TIMELOCK_MS,
            Some(vec![record.ppid]),
        );
        assert_eq!(
            gate.verifier_current,
            env::sha256_array(b"siyab gate: attested mode, no operator verifier key")
        );
        assert!(gate.verifier_previous.is_none());
        assert_eq!(borsh::to_vec(&gate).unwrap().len(), before.len() - 40);
        assert!(gate.get_attestation()["enabled"].as_bool().unwrap());
    }

    #[test]
    fn legacy_view_is_explicitly_disabled() {
        let gate = gate();
        assert_eq!(
            gate.get_attestation(),
            json!({"enabled": false, "registry_id": null,
            "measurement_timelock_ms": null, "measurements": [], "platforms": [],
            "pending_platforms": null, "denied_keys": [], "keys": []})
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:attested_mode")]
    fn enabling_is_one_way() {
        let mut gate = enabled();
        gate.enable_attested_mode(
            "other.testnet".parse().unwrap(),
            "00".repeat(32),
            MIN_MEASUREMENT_TIMELOCK_MS,
            Some(vec!["11".repeat(16)]),
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:attested_mode")]
    fn legacy_key_setter_is_disabled() {
        let mut gate = enabled();
        gate.set_verifier_keys(
            PublicKey::from_parts(CurveType::ED25519, vec![3; 32]).unwrap(),
            None,
            0,
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_request")]
    fn timelock_shorter_than_twenty_four_hours_is_refused() {
        let mut gate = gate();
        gate.enable_attested_mode(
            "attest.testnet".parse().unwrap(),
            "00".repeat(32),
            MIN_MEASUREMENT_TIMELOCK_MS - 1,
            Some(vec!["11".repeat(16)]),
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_request")]
    fn timelock_longer_than_thirty_days_is_refused() {
        let mut gate = gate();
        gate.enable_attested_mode(
            "attest.testnet".parse().unwrap(),
            record().measurement_id,
            2_592_000_001,
            Some(vec![record().ppid]),
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_request")]
    fn maximum_u64_timelock_is_refused_before_enabling() {
        let mut gate = gate();
        gate.enable_attested_mode(
            "attest.testnet".parse().unwrap(),
            record().measurement_id,
            u64::MAX,
            Some(vec![record().ppid]),
        );
    }

    #[test]
    fn thirty_day_timelock_can_schedule_measurements_and_platforms() {
        let mut gate = gate();
        let record = record();
        let timelock_ms = 2_592_000_000;
        gate.enable_attested_mode(
            "attest.testnet".parse().unwrap(),
            record.measurement_id,
            timelock_ms,
            Some(vec![record.ppid.clone()]),
        );
        gate.propose_measurement("ff".repeat(32), "reviewed build".to_owned());
        gate.set_platforms(Some(vec![record.ppid, "22".repeat(16)]));
        let view = gate.get_attestation();
        assert_eq!(view["measurement_timelock_ms"], timelock_ms);
        assert_eq!(
            view["measurements"][1]["active_from_ms"],
            record.registered_ms + timelock_ms
        );
        assert_eq!(
            view["pending_platforms"]["active_from_ms"],
            record.registered_ms + timelock_ms
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_request")]
    fn platform_pinning_cannot_be_disabled_on_enable() {
        let mut gate = gate();
        gate.enable_attested_mode(
            "attest.testnet".parse().unwrap(),
            "00".repeat(32),
            MIN_MEASUREMENT_TIMELOCK_MS,
            None,
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_request")]
    fn platform_pinning_cannot_be_disabled_later() {
        enabled().set_platforms(None);
    }

    #[test]
    fn platform_inputs_require_one_to_eight_sorted_unique_exact_ppids() {
        for values in [
            vec![],
            vec!["11".repeat(15)],
            vec!["AA".repeat(16)],
            vec!["11".repeat(16), "11".repeat(16)],
            vec!["22".repeat(16), "11".repeat(16)],
            (0..9)
                .map(|value| format!("{value:02x}").repeat(16))
                .collect(),
        ] {
            assert!(!valid_platforms(&values));
        }
        assert!(valid_platforms(&["11".repeat(16)]));
        assert!(valid_platforms(
            &(0..8)
                .map(|value| format!("{value:02x}").repeat(16))
                .collect::<Vec<_>>()
        ));
    }

    #[test]
    fn initial_measurement_is_active_but_a_new_measurement_waits() {
        let mut gate = enabled();
        let now_ms = record().registered_ms;
        assert!(sync(&mut gate, &record(), now_ms));
        context(now_ms, true, None);
        let mut candidate = record();
        candidate.measurement.compose_hash = "01".repeat(32);
        candidate.measurement_id = measurement_id(&candidate.measurement).unwrap();
        candidate.public_key = "22".repeat(32);
        gate.propose_measurement(
            candidate.measurement_id.clone(),
            "reviewed build".to_owned(),
        );
        assert!(!sync(&mut gate, &candidate, now_ms));
        let active = now_ms + MIN_MEASUREMENT_TIMELOCK_MS;
        candidate.issued_at_ms = active - 1;
        candidate.registered_ms = active - 1;
        candidate.expires_ms = candidate.issued_at_ms + 86_400_000;
        assert!(!sync(&mut gate, &candidate, active - 1));
        assert!(sync(&mut gate, &candidate, active));
    }

    #[test]
    fn measurement_revocation_drops_cached_and_in_flight_keys() {
        let mut gate = enabled();
        let record = record();
        assert!(sync(&mut gate, &record, record.registered_ms));
        context(record.registered_ms, true, None);
        gate.revoke_measurement(record.measurement_id.clone());
        assert!(cached_keys().is_empty());
        assert!(!sync(&mut gate, &record, record.registered_ms));
    }

    #[test]
    fn key_denial_drops_cached_and_in_flight_keys() {
        let mut gate = enabled();
        let record = record();
        assert!(sync(&mut gate, &record, record.registered_ms));
        context(record.registered_ms, true, None);
        gate.deny_verifier_key(record.public_key.clone());
        gate.deny_verifier_key(record.public_key.clone());
        assert_eq!(denied_keys(), vec![record.public_key.clone()]);
        assert!(cached_keys().is_empty());
        assert!(!sync(&mut gate, &record, record.registered_ms));
    }

    #[test]
    fn platform_widening_waits_and_mixed_change_removes_immediately() {
        let mut gate = enabled();
        let record = record();
        let now_ms = record.registered_ms;
        assert!(sync(&mut gate, &record, now_ms));
        context(now_ms, true, None);
        let new_ppid = "22".repeat(16);
        gate.set_platforms(Some(vec![new_ppid.clone()]));
        assert_eq!(gate.get_attestation()["platforms"], json!([]));
        assert!(cached_keys().is_empty());
        assert!(!sync(&mut gate, &record, now_ms));
        let active = now_ms + MIN_MEASUREMENT_TIMELOCK_MS;
        let mut candidate = record.clone();
        candidate.public_key = "22".repeat(32);
        candidate.ppid = new_ppid.clone();
        candidate.issued_at_ms = active - 1;
        candidate.registered_ms = active - 1;
        candidate.expires_ms = candidate.issued_at_ms + 86_400_000;
        assert!(!sync(&mut gate, &candidate, active - 1));
        assert!(sync(&mut gate, &candidate, active));
        assert_eq!(gate.get_attestation()["platforms"], json!([new_ppid]));
        assert!(gate.get_attestation()["pending_platforms"].is_null());
    }

    #[test]
    fn narrowing_is_immediate_and_cancels_pending_widening() {
        let mut gate = enabled();
        let record = record();
        gate.set_platforms(Some(vec![record.ppid.clone(), "22".repeat(16)]));
        assert!(!gate.get_attestation()["pending_platforms"].is_null());
        gate.set_platforms(Some(vec![record.ppid.clone()]));
        assert!(gate.get_attestation()["pending_platforms"].is_null());
        context(
            record.registered_ms + MIN_MEASUREMENT_TIMELOCK_MS,
            false,
            None,
        );
        assert_eq!(gate.get_attestation()["platforms"], json!([record.ppid]));
    }

    #[test]
    fn expiry_and_future_registration_are_refused_at_the_exact_edges() {
        let mut gate = enabled();
        let record = record();
        assert!(!sync(&mut gate, &record, record.registered_ms - 1));
        assert!(sync(&mut gate, &record, record.registered_ms));
        assert!(!usable_keys(record.expires_ms - 1).is_empty());
        assert!(usable_keys(record.expires_ms).is_empty());
        assert!(!sync(&mut gate, &record, record.expires_ms));
    }

    #[test]
    fn only_canonical_complete_key_views_are_accepted() {
        let mut gate = enabled();
        let original = serde_json::to_value(record()).unwrap();
        for (field, replacement) in [
            ("public_key", json!("33".repeat(32))),
            ("measurement_id", json!("00".repeat(32))),
            ("ppid", json!("22".repeat(16))),
            ("tcb_status", json!("OutOfDate")),
            ("issued_at_ms", json!(record().registered_ms + 1)),
            ("registered_ms", json!(record().registered_ms.to_string())),
            ("expires_ms", json!(true)),
            ("aux", json!("AA".repeat(32))),
            ("advisory_ids", json!([1])),
            ("extra", json!(0)),
        ] {
            let mut value = original.clone();
            value[field] = replacement;
            context(
                record().registered_ms,
                false,
                Some(PromiseResult::Successful(
                    serde_json::to_vec(&value).unwrap(),
                )),
            );
            assert!(
                !gate.on_verifier_synced(record().public_key, 86_400_000),
                "{field}"
            );
        }
        for field in original.as_object().unwrap().keys() {
            let mut value = original.clone();
            value.as_object_mut().unwrap().remove(field);
            context(
                record().registered_ms,
                false,
                Some(PromiseResult::Successful(
                    serde_json::to_vec(&value).unwrap(),
                )),
            );
            assert!(!gate.on_verifier_synced(record().public_key, 86_400_000));
        }
        assert!(cached_keys().is_empty());
    }

    #[test]
    fn failed_absent_malformed_and_oversized_results_never_cache() {
        let mut gate = enabled();
        for result in [
            PromiseResult::Failed,
            PromiseResult::Successful(b"null".to_vec()),
            PromiseResult::Successful(b"{}".to_vec()),
            PromiseResult::Successful(vec![b' '; MAX_REGISTRY_RESULT_BYTES + 1]),
        ] {
            context(record().registered_ms, false, Some(result));
            assert!(!gate.on_verifier_synced(record().public_key, 86_400_000));
        }
        assert!(cached_keys().is_empty());
    }

    #[test]
    fn invalid_config_never_dispatches_a_key_request() {
        let mut gate = enabled();
        let original = vectors()["registry_config"][0]["get_config_result"].clone();
        for (field, replacement) in [
            ("max_lease_ms", json!(0)),
            ("max_lease_ms", json!("86400000")),
            ("verifier_account_id", json!("Bad.near")),
            ("ed25519_domain_id", json!(false)),
            ("vote_recipients", json!([])),
            ("vote_recipients", json!(["z.near", "a.near"])),
            ("vote_recipients", json!(["a.near", "a.near"])),
            ("extra", json!(0)),
        ] {
            let mut value = original.clone();
            value[field] = replacement;
            context(
                record().registered_ms,
                false,
                Some(PromiseResult::Successful(
                    serde_json::to_vec(&value).unwrap(),
                )),
            );
            assert!(matches!(
                gate.on_attestation_config(record().public_key),
                PromiseOrValue::Value(false)
            ));
            assert!(get_created_receipts().is_empty());
        }
    }

    #[test]
    fn identical_sync_and_renewal_update_in_place_without_eviction() {
        let mut gate = enabled();
        let original = record();
        for index in 0..4 {
            let mut record = original.clone();
            record.public_key = format!("{index:02x}").repeat(32);
            assert!(sync(&mut gate, &record, record.registered_ms));
        }
        let before = cached_keys();
        let mut renewed = before[1].clone();
        assert!(sync(&mut gate, &renewed, renewed.registered_ms));
        assert_eq!(cached_keys(), before);
        renewed.issued_at_ms += 1;
        renewed.expires_ms += 1;
        assert!(sync(&mut gate, &renewed, renewed.registered_ms));
        let after = cached_keys();
        assert_eq!(after.len(), 4);
        assert_eq!(after[1], renewed);
        assert_eq!(after[0], before[0]);
        assert_eq!(after[2..], before[2..]);
        assert!(!sync(&mut gate, &before[1], before[1].registered_ms));
        assert_eq!(cached_keys(), after);
    }

    #[test]
    fn changed_aux_is_admitted_as_new_without_displacing_other_cached_keys() {
        let mut gate = enabled();
        let original = record();
        for index in 0..4 {
            let mut record = original.clone();
            record.public_key = format!("{index:02x}").repeat(32);
            assert!(sync(&mut gate, &record, record.registered_ms));
        }
        let before = cached_keys();
        let values = vectors();
        let vector = values["reregistration"]
            .as_array()
            .unwrap()
            .iter()
            .find(|vector| vector["name"] == "aux-mutation")
            .unwrap();
        let mut value = serde_json::to_value(&before[1]).unwrap();
        for (field, replacement) in vector["candidate_changes"].as_object().unwrap() {
            value[field] = replacement.clone();
        }
        let mut candidate: AttestedKeyView = serde_json::from_value(value).unwrap();
        assert!(sync(&mut gate, &candidate, candidate.registered_ms));
        assert_eq!(
            cached_keys(),
            vec![
                before[0].clone(),
                before[2].clone(),
                before[3].clone(),
                candidate.clone()
            ]
        );
        let admitted = cached_keys();
        assert!(sync(&mut gate, &candidate, candidate.registered_ms));
        assert_eq!(cached_keys(), admitted);
        assert!(!sync(&mut gate, &before[1], candidate.registered_ms));
        assert_eq!(cached_keys(), admitted);
        candidate.issued_at_ms += 1;
        candidate.expires_ms += 1;
        assert!(sync(&mut gate, &candidate, candidate.registered_ms));
        assert_eq!(cached_keys()[3], candidate);
        assert_eq!(cached_keys()[..3], admitted[..3]);
    }

    #[test]
    fn stale_aux_changes_cannot_invalidate_or_replace_the_cached_key() {
        let mut gate = enabled();
        let original = record();
        assert!(sync(&mut gate, &original, original.registered_ms));
        let mut candidate = original.clone();
        candidate.aux = "ab".repeat(32);
        for age in 0..2 {
            candidate.issued_at_ms = original.issued_at_ms - age;
            candidate.expires_ms = original.expires_ms - age;
            assert!(!sync(&mut gate, &candidate, original.registered_ms));
            assert_eq!(cached_keys(), vec![original.clone()]);
        }
        candidate.issued_at_ms = original.issued_at_ms + 1;
        candidate.expires_ms = original.expires_ms + 1;
        candidate.registered_ms -= 1;
        assert!(!sync(&mut gate, &candidate, original.registered_ms));
        assert_eq!(cached_keys(), vec![original]);
    }

    #[test]
    fn full_cache_refuses_new_keys_without_displacing_valid_signers() {
        let mut gate = enabled();
        let original = record();
        for index in 0..4 {
            let mut record = original.clone();
            record.public_key = format!("{index:02x}").repeat(32);
            record.registered_ms += index;
            assert!(sync(&mut gate, &record, record.registered_ms));
        }
        let before = cached_keys();
        let now_ms = original.registered_ms + 4;
        let signers = usable_keys(now_ms);
        for index in 4..6 {
            let mut record = original.clone();
            record.public_key = format!("{index:02x}").repeat(32);
            assert!(!sync(&mut gate, &record, now_ms));
            assert_eq!(cached_keys(), before);
            assert_eq!(usable_keys(now_ms), signers);
        }
    }

    #[test]
    fn cache_renewal_refuses_identity_changes_and_out_of_order_callbacks() {
        let mut gate = enabled();
        let original = record();
        assert!(sync(&mut gate, &original, original.registered_ms));
        for vector in vectors()["reregistration"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|vector| vector["name"] != "aux-mutation")
        {
            let mut value = serde_json::to_value(&original).unwrap();
            for (field, replacement) in vector["candidate_changes"].as_object().unwrap() {
                value[field] = replacement.clone();
            }
            let candidate: AttestedKeyView = serde_json::from_value(value).unwrap();
            if vector["name"] == "equal-quote" {
                assert!(sync(&mut gate, &candidate, original.registered_ms));
            } else if vector["accepted"] == false {
                assert!(!sync(&mut gate, &candidate, original.registered_ms));
                assert_eq!(cached_keys(), vec![original.clone()]);
            }
        }
        let mut newer = original.clone();
        newer.issued_at_ms += 2;
        newer.expires_ms += 2;
        assert!(sync(&mut gate, &newer, original.registered_ms));
        let mut older = original.clone();
        older.issued_at_ms += 1;
        older.expires_ms += 1;
        assert!(!sync(&mut gate, &older, original.registered_ms));
        assert_eq!(cached_keys(), vec![newer]);
    }

    #[test]
    fn new_key_replaces_only_an_expired_cached_key() {
        let mut gate = enabled();
        let original = record();
        for index in 0..4 {
            let mut record = original.clone();
            record.public_key = format!("{index:02x}").repeat(32);
            record.registered_ms += index;
            if index == 2 {
                record.collateral_expires_ms = original.registered_ms + 10;
                record.expires_ms = record.collateral_expires_ms;
            }
            assert!(sync(&mut gate, &record, record.registered_ms));
        }
        let mut record = original.clone();
        record.public_key = "04".repeat(32);
        assert!(sync(&mut gate, &record, original.registered_ms + 10));
        assert!(cached_keys()
            .iter()
            .all(|key| key.public_key != "02".repeat(32)));
        record.public_key = "05".repeat(32);
        let before = cached_keys();
        assert!(!sync(&mut gate, &record, original.registered_ms + 12));
        assert_eq!(cached_keys(), before);
        assert!(cached_keys()
            .iter()
            .any(|key| key.public_key == "00".repeat(32)));
        assert_eq!(cached_keys().len(), 4);
    }
}
