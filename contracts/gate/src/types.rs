use near_sdk::{json_types::U128, near, AccountId, PublicKey};

#[near(serializers = [json, borsh])]
#[derive(Clone, PartialEq, Eq)]
pub enum Asset {
    Near,
    Ft(AccountId),
}

#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct AssetCap {
    pub asset: Asset,
    pub per_call: u128,
    pub window_cap: u128,
    pub window_ms: u64,
    pub window_start_ms: u64,
    pub spent: u128,
}

#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct Agent {
    pub label: String,
    pub enabled: bool,
    pub allowlist: Vec<AccountId>,
    pub caps: Vec<AssetCap>,
    pub unknown_caps: Option<Vec<AssetCap>>,
}

#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct Pending {
    pub agent_pk: PublicKey,
    pub asset: Asset,
    pub amount: u128,
}

#[near(serializers = [json, borsh])]
pub struct TransferRequest {
    pub kind: String,
    pub token_id: Option<AccountId>,
    pub receiver_id: AccountId,
    pub amount: U128,
    pub memo: Option<String>,
}

#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct Verdict {
    pub literal: String,
    pub issued_ms: u64,
    pub expires_ms: u64,
    pub verdict_id: String,
    pub evidence_hash: String,
    pub key_id: String,
    pub signature: String,
}

#[near(serializers = [json])]
pub struct AssetCapView {
    pub asset: Asset,
    pub per_call: U128,
    pub window_cap: U128,
    pub window_ms: u64,
    pub window_start_ms: u64,
    pub spent: U128,
}

#[near(serializers = [json])]
pub struct AgentView {
    pub label: String,
    pub enabled: bool,
    pub allowlist: Vec<AccountId>,
    pub caps: Vec<AssetCapView>,
    pub unknown_caps: Option<Vec<AssetCapView>>,
}

impl From<AssetCap> for AssetCapView {
    fn from(cap: AssetCap) -> Self {
        Self {
            asset: cap.asset,
            per_call: U128(cap.per_call),
            window_cap: U128(cap.window_cap),
            window_ms: cap.window_ms,
            window_start_ms: cap.window_start_ms,
            spent: U128(cap.spent),
        }
    }
}

impl From<Agent> for AgentView {
    fn from(agent: Agent) -> Self {
        Self {
            label: agent.label,
            enabled: agent.enabled,
            allowlist: agent.allowlist,
            caps: agent.caps.into_iter().map(AssetCapView::from).collect(),
            unknown_caps: agent.unknown_caps.map(|caps| caps.into_iter().map(AssetCapView::from).collect()),
        }
    }
}
