use crate::{
    events,
    types::{Agent, AgentView, Asset, AssetCap, AssetCapInput, PendingView},
    Gate, GateExt,
};
use near_sdk::{
    assert_one_yocto, env, json_types::U128, near, serde_json::json, AccountId, Allowance,
    CurveType, NearToken, Promise, PublicKey,
};

fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[near]
impl Gate {
    fn assert_owner(&self) {
        near_sdk::require!(
            env::predecessor_account_id() == self.owner_id,
            "REFUSED:not_owner"
        );
        assert_one_yocto();
    }

    fn key_bytes(public_key: &PublicKey) -> [u8; 32] {
        near_sdk::require!(
            public_key.curve_type() == CurveType::ED25519,
            "REFUSED:bad_request"
        );
        let mut bytes = [0; 32];
        bytes.copy_from_slice(&public_key.as_bytes()[1..]);
        bytes
    }

    #[payable]
    pub fn register_agent(
        &mut self,
        public_key: PublicKey,
        label: String,
        caps: Vec<AssetCapInput>,
        unknown_caps: Option<Vec<AssetCapInput>>,
        allowance_yocto: U128,
    ) -> Promise {
        self.assert_owner();
        near_sdk::require!(
            !self.agents.contains_key(&public_key),
            "REFUSED:agent_exists"
        );
        near_sdk::require!(allowance_yocto.0 > 0, "REFUSED:zero_allowance");
        self.agents.insert(
            public_key.clone(),
            Agent {
                label,
                enabled: true,
                allowlist: Vec::new(),
                caps: caps.into_iter().map(AssetCap::from).collect(),
                unknown_caps: unknown_caps
                    .map(|caps| caps.into_iter().map(AssetCap::from).collect()),
            },
        );
        events::emit(
            "agent_registered",
            json!({"agent_pk": hex_encode(&Self::key_bytes(&public_key))}),
        );
        Promise::new(env::current_account_id()).add_access_key_allowance(
            public_key,
            Allowance::limited(NearToken::from_yoctonear(allowance_yocto.0))
                .unwrap_or_else(|| env::panic_str("REFUSED:zero_allowance")),
            env::current_account_id(),
            "request_transfer",
        )
    }

    #[payable]
    pub fn revoke_agent(&mut self, public_key: PublicKey) -> Promise {
        self.assert_owner();
        self.agents.remove(&public_key);
        events::emit(
            "agent_revoked",
            json!({"agent_pk": hex_encode(&Self::key_bytes(&public_key))}),
        );
        Promise::new(env::current_account_id()).delete_key(public_key)
    }

    #[payable]
    pub fn set_allowlist(&mut self, agent_pk: PublicKey, accounts: Vec<AccountId>) {
        self.assert_owner();
        let mut agent = self
            .agents
            .get(&agent_pk)
            .cloned()
            .unwrap_or_else(|| env::panic_str("REFUSED:unknown_agent"));
        agent.allowlist = accounts;
        self.agents.insert(agent_pk, agent);
    }

    #[payable]
    pub fn set_denylist(&mut self, accounts: Vec<AccountId>) {
        self.assert_owner();
        for account in self.denylist.iter().cloned().collect::<Vec<_>>() {
            self.denylist.remove(&account);
        }
        for account in accounts {
            self.denylist.insert(account);
        }
    }

    #[payable]
    pub fn set_agent_enabled(&mut self, agent_pk: PublicKey, enabled: bool) {
        self.assert_owner();
        let mut agent = self
            .agents
            .get(&agent_pk)
            .cloned()
            .unwrap_or_else(|| env::panic_str("REFUSED:unknown_agent"));
        agent.enabled = enabled;
        self.agents.insert(agent_pk, agent);
    }

    #[payable]
    pub fn set_caps(
        &mut self,
        agent_pk: PublicKey,
        caps: Vec<AssetCapInput>,
        unknown_caps: Option<Vec<AssetCapInput>>,
    ) {
        self.assert_owner();
        let mut agent = self
            .agents
            .get(&agent_pk)
            .cloned()
            .unwrap_or_else(|| env::panic_str("REFUSED:unknown_agent"));
        agent.caps = caps.into_iter().map(AssetCap::from).collect();
        agent.unknown_caps =
            unknown_caps.map(|caps| caps.into_iter().map(AssetCap::from).collect());
        self.agents.insert(agent_pk, agent);
    }

    #[payable]
    pub fn set_verifier_keys(
        &mut self,
        current: PublicKey,
        previous: Option<PublicKey>,
        previous_valid_until_ms: u64,
    ) {
        self.assert_owner();
        self.verifier_current = Self::key_bytes(&current);
        self.verifier_previous =
            previous.map(|key| (Self::key_bytes(&key), previous_valid_until_ms));
        events::emit(
            "verifier_keys_updated",
            json!({
                "current": hex_encode(&self.verifier_current),
                "previous_valid_until_ms": previous_valid_until_ms
            }),
        );
    }

    #[payable]
    pub fn set_max_ttl(&mut self, max_ttl_ms: u64) {
        self.assert_owner();
        near_sdk::require!(max_ttl_ms > 0, "REFUSED:bad_request");
        self.max_ttl_ms = max_ttl_ms;
    }

    #[payable]
    pub fn set_paused(&mut self, paused: bool) {
        self.assert_owner();
        self.paused = paused;
    }

    #[payable]
    pub fn withdraw(&mut self, asset: Asset, receiver_id: AccountId, amount: U128) -> Promise {
        self.assert_owner();
        match asset {
            Asset::Near => Promise::new(receiver_id).transfer(NearToken::from_yoctonear(amount.0)),
            Asset::Ft(token_id) => Promise::new(token_id).function_call(
                "ft_transfer".to_owned(),
                json!({"receiver_id": receiver_id, "amount": amount})
                    .to_string()
                    .into_bytes(),
                NearToken::from_yoctonear(1),
                near_sdk::Gas::from_tgas(10),
            ),
        }
    }

    pub fn get_agent(&self, public_key: PublicKey) -> Option<AgentView> {
        self.agents.get(&public_key).cloned().map(AgentView::from)
    }

    pub fn get_policy(&self) -> near_sdk::serde_json::Value {
        json!({"denylist": self.denylist.iter().cloned().collect::<Vec<_>>(), "max_ttl_ms": self.max_ttl_ms, "paused": self.paused})
    }

    pub fn get_verifier_keys(&self) -> near_sdk::serde_json::Value {
        json!({
            "current": hex_encode(&self.verifier_current),
            "previous": self.verifier_previous.as_ref().map(|(key, until)| json!({"key": hex_encode(key), "valid_until_ms": until}))
        })
    }

    pub fn is_verdict_used(&self, verdict_id: String) -> bool {
        self.used_verdicts.contains_key(&verdict_id)
    }

    pub fn get_pending(&self, request_id: u64) -> Option<PendingView> {
        self.pending
            .get(&request_id)
            .cloned()
            .map(PendingView::from)
    }
}
