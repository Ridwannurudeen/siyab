use crate::{
    lower_hex,
    replay::{decode_hex, replay_rtmr3, validate_evidence, MAX_VERIFICATION_BYTES},
    report_data,
    types::StoredKey,
    validate_config, validate_key_times, validate_td_attributes, AttestedKeyView, RegisterContext,
    RegistryConfig, ReplayEvent, KEYS_PREFIX,
};
use near_sdk::{
    env, near, AccountId, Gas, GasWeight, NearToken, PanicOnDefault, Promise, PromiseOrValue,
};
use tee_verifier_interface::{
    Collateral, QuoteBytes, Report, VerificationResultWithCollateralDates,
};

const DEPOSIT: u128 = 10_000_000_000_000_000_000_000;
const MAX_INPUT_BYTES: usize = 262_144;
const MAX_RESULT_BYTES: usize = 16_384;

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct AttestRegistry {
    config: RegistryConfig,
}

#[near]
impl AttestRegistry {
    #[init]
    #[private]
    #[deny_unknown_arguments]
    pub fn new(
        verifier_account_id: String,
        signer_account_id: String,
        ed25519_domain_id: u64,
        max_lease_ms: u64,
        vote_recipients: Vec<String>,
    ) -> Self {
        near_sdk::require!(json_object_input(), "REFUSED:expected_object");
        near_sdk::require!(
            env::predecessor_account_id() == env::current_account_id(),
            "REFUSED:self_only"
        );
        let config = RegistryConfig {
            verifier_account_id,
            signer_account_id,
            ed25519_domain_id,
            max_lease_ms,
            vote_recipients,
        };
        validate_config(&config).unwrap_or_else(|error| env::panic_str(error));
        Self { config }
    }

    pub fn get_config(&self) -> RegistryConfig {
        near_sdk::require!(
            env::input().is_some_and(|input| {
                matches!(near_sdk::serde_json::from_slice::<near_sdk::serde_json::Value>(&input),
                    Ok(near_sdk::serde_json::Value::Object(fields)) if fields.is_empty())
            }),
            "REFUSED:expected_empty_object"
        );
        self.config.clone()
    }

    #[deny_unknown_arguments]
    pub fn get_key(&self, public_key: String) -> Option<AttestedKeyView> {
        near_sdk::require!(json_object_input(), "REFUSED:expected_object");
        let public_key = lower_hex::<32>(&public_key).unwrap_or_else(|error| env::panic_str(error));
        env::storage_read(&key_slot(&public_key)).map(|bytes| {
            borsh::from_slice::<StoredKey>(&bytes)
                .expect("invalid stored key")
                .view(&public_key)
        })
    }

    #[payable]
    #[deny_unknown_arguments]
    pub fn register_key(
        &mut self,
        public_key: String,
        issued_at_ms: u64,
        aux: String,
        verification_args: String,
        app_compose: String,
        event_log: Vec<ReplayEvent>,
    ) -> PromiseOrValue<Option<AttestedKeyView>> {
        near_sdk::require!(
            env::attached_deposit().as_yoctonear() == DEPOSIT,
            "REFUSED:deposit"
        );
        near_sdk::require!(env::prepaid_gas() == Gas::from_tgas(300), "REFUSED:gas");
        let relayer = env::predecessor_account_id();
        let input = (|| {
            if !json_object_input() || verification_args.len() > MAX_VERIFICATION_BYTES * 2 {
                return Err("registration exceeds bounds");
            }
            let public_key = lower_hex::<32>(&public_key)?;
            let aux = lower_hex::<32>(&aux)?;
            validate_evidence(&app_compose, &event_log)?;
            let args = decode_hex(&verification_args)?;
            borsh::from_slice::<(QuoteBytes, Collateral)>(&args)
                .map_err(|_| "invalid verification tuple")?;
            Ok((public_key, aux, args))
        })();
        let Ok((public_key, aux, args)) = input else {
            refund(relayer, DEPOSIT);
            return PromiseOrValue::Value(None);
        };
        let context = RegisterContext {
            public_key,
            issued_at_ms,
            aux,
            app_compose,
            event_log,
            relayer,
            deposit: DEPOSIT,
        };
        PromiseOrValue::Promise(
            Promise::new(
                self.config
                    .verifier_account_id
                    .parse()
                    .expect("validated verifier account"),
            )
            .function_call_weight(
                "verify_quote_with_collateral_dates",
                args,
                NearToken::from_yoctonear(0),
                Gas::from_tgas(200),
                GasWeight(0),
            )
            .then(
                Promise::new(env::current_account_id()).function_call_weight(
                    "on_quote_verified",
                    borsh::to_vec(&context).unwrap(),
                    NearToken::from_yoctonear(0),
                    Gas::from_tgas(60),
                    GasWeight(0),
                ),
            ),
        )
    }

    #[private]
    pub fn on_quote_verified(
        &mut self,
        #[serializer(borsh)] context: RegisterContext,
    ) -> Option<AttestedKeyView> {
        let accepted = (|| {
            if env::promise_results_count() != 1 {
                return None;
            }
            let bytes = env::promise_result_checked(0, MAX_RESULT_BYTES).ok()?;
            let result = borsh::from_slice(&bytes).ok()?;
            let candidate = verified_key(
                &self.config,
                env::current_account_id().as_str(),
                &context,
                result,
                env::block_timestamp_ms(),
            )
            .ok()?;
            let slot = key_slot(&context.public_key);
            let previous_bytes = env::storage_read(&slot);
            if let Some(bytes) = &previous_bytes {
                let previous: StoredKey = borsh::from_slice(bytes).expect("invalid stored key");
                if !valid_renewal(&previous, &candidate) {
                    return None;
                }
            }
            let before = env::storage_usage();
            env::storage_write(&slot, &borsh::to_vec(&candidate).unwrap());
            let cost = u128::from(env::storage_usage().saturating_sub(before))
                .checked_mul(env::storage_byte_cost().as_yoctonear());
            match cost {
                Some(cost) if cost <= context.deposit => {
                    Some((candidate.view(&context.public_key), cost))
                }
                _ => {
                    match previous_bytes {
                        Some(bytes) => {
                            env::storage_write(&slot, &bytes);
                        }
                        None => {
                            env::storage_remove(&slot);
                        }
                    }
                    None
                }
            }
        })();
        let (view, cost) = match accepted {
            Some((view, cost)) => (Some(view), cost),
            None => (None, 0),
        };
        refund(context.relayer, context.deposit - cost);
        view
    }
}

fn json_object_input() -> bool {
    env::input().is_some_and(|input| {
        input.len() <= MAX_INPUT_BYTES
            && input.iter().find(|byte| !byte.is_ascii_whitespace()) == Some(&b'{')
    })
}

fn key_slot(public_key: &[u8; 32]) -> Vec<u8> {
    let mut slot = KEYS_PREFIX.to_vec();
    slot.extend_from_slice(public_key);
    slot
}

fn valid_renewal(previous: &StoredKey, candidate: &StoredKey) -> bool {
    candidate.issued_at_ms > previous.issued_at_ms
        && candidate.measurement_id() == previous.measurement_id()
        && candidate.ppid == previous.ppid
        && candidate.aux == previous.aux
}

fn refund(relayer: AccountId, amount: u128) {
    if amount > 0 {
        Promise::new(relayer)
            .transfer(NearToken::from_yoctonear(amount))
            .detach();
    }
}

fn verified_key(
    config: &RegistryConfig,
    registry_account_id: &str,
    context: &RegisterContext,
    result: VerificationResultWithCollateralDates,
    now_ms: u64,
) -> Result<StoredKey, &'static str> {
    let VerificationResultWithCollateralDates::Verified {
        report,
        collateral_dates,
    } = result
    else {
        return Err("quote rejected");
    };
    let Report::TD10(td) = report.report else {
        return Err("expected exactly TD10");
    };
    if report.status != "UpToDate" {
        return Err("expected UpToDate");
    }
    validate_td_attributes(td.td_attributes)?;
    if td.report_data
        != report_data(
            registry_account_id,
            &context.public_key,
            context.issued_at_ms,
            &context.aux,
        )
    {
        return Err("report data mismatch");
    }
    let (key_provider_digest, compose_hash) =
        replay_rtmr3(&context.app_compose, &context.event_log, &td.rt_mr3)?;
    let collateral_expires_ms = collateral_dates
        .earliest_expiration_date
        .checked_mul(1000)
        .ok_or("collateral time overflow")?;
    let candidate = StoredKey {
        mrtd: td.mr_td,
        rtmr0: td.rt_mr0,
        rtmr1: td.rt_mr1,
        rtmr2: td.rt_mr2,
        key_provider_digest,
        compose_hash,
        ppid: report
            .ppid
            .try_into()
            .map_err(|_| "expected 16 PPID bytes")?,
        aux: context.aux,
        advisory_ids: report.advisory_ids,
        issued_at_ms: context.issued_at_ms,
        registered_ms: now_ms,
        expires_ms: context
            .issued_at_ms
            .checked_add(config.max_lease_ms)
            .ok_or("u64 lease overflow")?
            .min(collateral_expires_ms),
        collateral_expires_ms,
    };
    validate_key_times(&candidate.view(&context.public_key), config.max_lease_ms)?;
    Ok(candidate)
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::{
        mock::MockAction,
        test_utils::{get_created_receipts, VMContextBuilder},
        testing_env, PromiseResult,
    };
    use serde_json::Value;
    use tee_verifier_interface::{
        CollateralDates, TDReport10, TcbStatus, TcbStatusWithAdvisory, VerifiedReport,
    };

    const NOW: u64 = 1_795_219_201_000;

    fn sample() -> (
        RegistryConfig,
        RegisterContext,
        VerificationResultWithCollateralDates,
    ) {
        let vectors: Value =
            serde_json::from_str(include_str!("../../../fixtures/attest/vectors.json")).unwrap();
        let config =
            serde_json::from_value(vectors["registry_config"][0]["new_arguments"].clone()).unwrap();
        let tcb: Value =
            serde_json::from_str(include_str!("../tests/vendor/upstream/tcb_info.json")).unwrap();
        let context = RegisterContext {
            public_key: [7; 32],
            issued_at_ms: NOW - 1000,
            aux: [0; 32],
            app_compose: tcb["app_compose"].as_str().unwrap().into(),
            event_log: serde_json::from_value(tcb["event_log"].clone()).unwrap(),
            relayer: "relay.testnet".parse().unwrap(),
            deposit: DEPOSIT,
        };
        let mut td: TDReport10 = borsh::from_slice(&[0; 584]).unwrap();
        td.td_attributes = 0x10000000_u64.to_le_bytes();
        td.report_data = report_data(
            "registry.testnet",
            &context.public_key,
            context.issued_at_ms,
            &context.aux,
        );
        td.rt_mr3 = lower_hex::<48>(tcb["rtmr3"].as_str().unwrap()).unwrap();
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
                earliest_expiration_date: NOW / 1000 + 172800,
                qe_iden_earliest_issue_date: 0,
                qe_iden_latest_issue_date: 0,
                qe_iden_earliest_expiration_date: NOW / 1000 + 172800,
            },
        };
        (config, context, result)
    }

    fn callback_env(result: PromiseResult, now: u64) {
        let mut context = VMContextBuilder::new()
            .current_account_id("registry.testnet".parse().unwrap())
            .predecessor_account_id("registry.testnet".parse().unwrap())
            .block_timestamp(now * 1_000_000 + 999_999)
            .prepaid_gas(Gas::from_tgas(60))
            .build();
        context.input = b"{}".to_vec().into();
        testing_env!(
            context,
            near_sdk::test_vm_config(),
            near_sdk::RuntimeFeesConfig::test(),
            Default::default(),
            vec![result]
        );
    }

    fn assert_refund(amount: u128) {
        let receipts = get_created_receipts();
        assert_eq!(receipts.len(), 1);
        assert_eq!(receipts[0].receiver_id.as_str(), "relay.testnet");
        assert!(
            matches!(&receipts[0].actions[..], [MockAction::Transfer { deposit, .. }] if deposit.as_yoctonear() == amount)
        );
    }

    fn stored_key(view: &AttestedKeyView) -> StoredKey {
        StoredKey {
            mrtd: lower_hex(&view.measurement.mrtd).unwrap(),
            rtmr0: lower_hex(&view.measurement.rtmr0).unwrap(),
            rtmr1: lower_hex(&view.measurement.rtmr1).unwrap(),
            rtmr2: lower_hex(&view.measurement.rtmr2).unwrap(),
            key_provider_digest: lower_hex(&view.measurement.key_provider_digest).unwrap(),
            compose_hash: lower_hex(&view.measurement.compose_hash).unwrap(),
            ppid: lower_hex(&view.ppid).unwrap(),
            aux: lower_hex(&view.aux).unwrap(),
            advisory_ids: view.advisory_ids.clone(),
            issued_at_ms: view.issued_at_ms,
            registered_ms: view.registered_ms,
            expires_ms: view.expires_ms,
            collateral_expires_ms: view.collateral_expires_ms,
        }
    }

    #[test]
    fn production_renewal_check_reproduces_every_reregistration_vector() {
        let vectors: Value =
            serde_json::from_str(include_str!("../../../fixtures/attest/vectors.json")).unwrap();
        let original = &vectors["attested_keys"][0]["view"];
        let lease = vectors["attested_keys"][0]["max_lease_ms"]
            .as_u64()
            .unwrap();
        let previous: AttestedKeyView = serde_json::from_value(original.clone()).unwrap();
        let previous = stored_key(&previous);
        for vector in vectors["reregistration"].as_array().unwrap() {
            let mut candidate = original.clone();
            for (key, value) in vector["candidate_changes"].as_object().unwrap() {
                candidate[key] = value.clone();
            }
            let candidate: AttestedKeyView = serde_json::from_value(candidate).unwrap();
            crate::validate_key_view(&candidate, lease).unwrap();
            let candidate = stored_key(&candidate);
            assert_eq!(
                valid_renewal(&previous, &candidate),
                vector["accepted"].as_bool().unwrap(),
                "{}",
                vector["name"]
            );
        }
    }

    #[test]
    fn verified_key_uses_callback_clock_issue_lease_and_collateral_cap() {
        let (config, context, result) = sample();
        let key = verified_key(&config, "registry.testnet", &context, result.clone(), NOW).unwrap();
        assert_eq!(key.registered_ms, NOW);
        assert_eq!(key.expires_ms, context.issued_at_ms + config.max_lease_ms);
        assert_eq!(key.advisory_ids, vec!["TEST-ADVISORY"]);
        let mut capped = result;
        let VerificationResultWithCollateralDates::Verified {
            collateral_dates, ..
        } = &mut capped
        else {
            unreachable!()
        };
        collateral_dates.earliest_expiration_date = NOW / 1000 + 2;
        let key = verified_key(&config, "registry.testnet", &context, capped.clone(), NOW).unwrap();
        assert_eq!(key.expires_ms, NOW + 2000);
        assert!(verified_key(&config, "registry.testnet", &context, capped, NOW + 2000).is_err());
    }

    #[test]
    fn report_data_refusal_precedes_expired_collateral() {
        let (config, context, mut result) = sample();
        let VerificationResultWithCollateralDates::Verified {
            report,
            collateral_dates,
        } = &mut result
        else {
            unreachable!()
        };
        let Report::TD10(td) = &mut report.report else {
            unreachable!()
        };
        td.report_data[0] ^= 1;
        collateral_dates.earliest_expiration_date = NOW / 1000 - 1;
        assert_eq!(
            verified_key(&config, "registry.testnet", &context, result, NOW).unwrap_err(),
            "report data mismatch"
        );
    }

    #[test]
    fn callback_success_charges_actual_growth_and_refunds_original_relayer() {
        let (config, context, result) = sample();
        callback_env(
            PromiseResult::Successful(borsh::to_vec(&result).unwrap()),
            NOW,
        );
        let mut registry = AttestRegistry { config };
        let before = env::storage_usage();
        let view = registry.on_quote_verified(context.clone()).unwrap();
        assert_eq!(view.registered_ms, NOW);
        assert_eq!(
            registry.get_key(hex::encode(context.public_key)),
            Some(view)
        );
        let cost =
            u128::from(env::storage_usage() - before) * env::storage_byte_cost().as_yoctonear();
        assert!(cost > 0 && cost < DEPOSIT);
        assert_refund(DEPOSIT - cost);
    }

    #[test]
    fn insufficient_storage_rolls_back_absent_and_existing_records() {
        let (config, context, mut result) = sample();
        let original =
            verified_key(&config, "registry.testnet", &context, result.clone(), NOW).unwrap();
        let slot = key_slot(&context.public_key);
        let VerificationResultWithCollateralDates::Verified { report, .. } = &mut result else {
            unreachable!()
        };
        report.advisory_ids = vec!["x".repeat(2000)];
        for previous in [None, Some(borsh::to_vec(&original).unwrap())] {
            callback_env(
                PromiseResult::Successful(borsh::to_vec(&result).unwrap()),
                NOW,
            );
            match &previous {
                Some(bytes) => {
                    env::storage_write(&slot, bytes);
                }
                None => {
                    env::storage_remove(&slot);
                }
            }
            let mut candidate = context.clone();
            candidate.issued_at_ms += 1;
            let mut updated = result.clone();
            let VerificationResultWithCollateralDates::Verified { report, .. } = &mut updated
            else {
                unreachable!()
            };
            let Report::TD10(td) = &mut report.report else {
                unreachable!()
            };
            td.report_data = report_data(
                "registry.testnet",
                &candidate.public_key,
                candidate.issued_at_ms,
                &candidate.aux,
            );
            callback_env(
                PromiseResult::Successful(borsh::to_vec(&updated).unwrap()),
                NOW,
            );
            let before = env::storage_usage();
            let mut registry = AttestRegistry {
                config: config.clone(),
            };
            assert!(registry.on_quote_verified(candidate).is_none());
            assert_eq!(env::storage_read(&slot), previous);
            assert_eq!(env::storage_usage(), before);
            assert_refund(DEPOSIT);
        }
    }

    #[test]
    fn callback_failure_malformed_trailing_and_oversized_results_refund() {
        let (config, context, result) = sample();
        let mut trailing = borsh::to_vec(&result).unwrap();
        trailing.push(0);
        for result in [
            PromiseResult::Failed,
            PromiseResult::Successful(vec![]),
            PromiseResult::Successful(trailing),
            PromiseResult::Successful(vec![0; MAX_RESULT_BYTES + 1]),
        ] {
            callback_env(result, NOW);
            let mut registry = AttestRegistry {
                config: config.clone(),
            };
            assert!(registry.on_quote_verified(context.clone()).is_none());
            assert!(env::storage_read(&key_slot(&context.public_key)).is_none());
            assert_refund(DEPOSIT);
        }
    }

    #[test]
    fn compact_storage_reproduces_shared_key_view() {
        let vectors: Value =
            serde_json::from_str(include_str!("../../../fixtures/attest/vectors.json")).unwrap();
        for vector in vectors["attested_keys"].as_array().unwrap() {
            let view: AttestedKeyView = serde_json::from_value(vector["view"].clone()).unwrap();
            let key = stored_key(&view);
            let stored = borsh::to_vec(&key).unwrap();
            assert_eq!(
                borsh::from_slice::<StoredKey>(&stored)
                    .unwrap()
                    .view(&lower_hex::<32>(&view.public_key).unwrap()),
                view
            );
        }
    }
}
