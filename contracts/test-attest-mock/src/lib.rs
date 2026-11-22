use near_sdk::{env, near, serde_json::Value, store::LookupMap, AccountId, PanicOnDefault};

#[near(serializers = [json, borsh])]
#[derive(Clone)]
#[serde(deny_unknown_fields)]
pub struct Config {
    verifier_account_id: AccountId,
    signer_account_id: AccountId,
    ed25519_domain_id: u64,
    max_lease_ms: u64,
    vote_recipients: Vec<AccountId>,
}

// TEST ONLY: records are supplied by the sandbox, without quote verification.
#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct MockRegistry {
    config: Config,
    keys: LookupMap<String, String>,
}

#[near]
impl MockRegistry {
    #[init]
    pub fn new(
        verifier_account_id: AccountId,
        signer_account_id: AccountId,
        ed25519_domain_id: u64,
        max_lease_ms: u64,
        vote_recipients: Vec<AccountId>,
    ) -> Self {
        near_sdk::require!(
            env::predecessor_account_id() == env::current_account_id(),
            "TEST_ONLY:self_call_required"
        );
        near_sdk::require!(max_lease_ms > 0, "TEST_ONLY:invalid_config");
        near_sdk::require!(
            !vote_recipients.is_empty()
                && vote_recipients.len() <= 8
                && vote_recipients.windows(2).all(|pair| pair[0] < pair[1]),
            "TEST_ONLY:invalid_config"
        );
        Self {
            config: Config {
                verifier_account_id,
                signer_account_id,
                ed25519_domain_id,
                max_lease_ms,
                vote_recipients,
            },
            keys: LookupMap::new(b"k".as_slice()),
        }
    }

    pub fn get_config(&self) -> Config {
        self.config.clone()
    }

    pub fn get_key(&self, public_key: String) -> Option<Value> {
        self.keys
            .get(&public_key)
            .map(|record| near_sdk::serde_json::from_str(record).unwrap())
    }

    #[private]
    pub fn set_key(&mut self, public_key: String, record: Option<Value>) {
        match record {
            Some(record) => {
                self.keys.insert(public_key, record.to_string());
            }
            None => {
                self.keys.remove(&public_key);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::{serde_json::json, test_utils::VMContextBuilder, testing_env};

    fn registry() -> MockRegistry {
        let account: AccountId = "registry.test.near".parse().unwrap();
        testing_env!(VMContextBuilder::new()
            .current_account_id(account.clone())
            .predecessor_account_id(account)
            .build());
        MockRegistry::new(
            "verifier.near".parse().unwrap(),
            "v1.signer.near".parse().unwrap(),
            7,
            86_400_000,
            vec!["outlayer.near".parse().unwrap()],
        )
    }

    #[test]
    fn config_has_the_frozen_five_fields() {
        let registry = registry();
        assert_eq!(
            near_sdk::serde_json::to_value(registry.get_config()).unwrap(),
            json!({
                "verifier_account_id": "verifier.near",
                "signer_account_id": "v1.signer.near",
                "ed25519_domain_id": 7,
                "max_lease_ms": 86_400_000,
                "vote_recipients": ["outlayer.near"]
            })
        );
    }

    #[test]
    fn sandbox_controls_preserve_records_and_absence() {
        let mut registry = registry();
        let key = "01".repeat(32);
        assert_eq!(registry.get_key(key.clone()), None);
        let record = json!({"public_key": key, "synthetic_malformed_record": true});
        registry.set_key(key.clone(), Some(record.clone()));
        assert_eq!(registry.get_key(key.clone()), Some(record));
        registry.set_key(key.clone(), None);
        assert_eq!(registry.get_key(key), None);
    }

    #[test]
    #[should_panic(expected = "TEST_ONLY:self_call_required")]
    fn initialization_requires_the_mock_account() {
        testing_env!(VMContextBuilder::new()
            .current_account_id("registry.test.near".parse().unwrap())
            .predecessor_account_id("other.test.near".parse().unwrap())
            .build());
        MockRegistry::new(
            "verifier.near".parse().unwrap(),
            "v1.signer.near".parse().unwrap(),
            7,
            86_400_000,
            vec!["outlayer.near".parse().unwrap()],
        );
    }
}
