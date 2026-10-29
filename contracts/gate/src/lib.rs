mod types;

use near_sdk::{
    env,
    near,
    store::{IterableMap, IterableSet, LookupMap},
    AccountId, CurveType, PanicOnDefault, PublicKey,
};
use types::{Agent, Pending};

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Gate {
    owner_id: AccountId,
    verifier_current: [u8; 32],
    verifier_previous: Option<([u8; 32], u64)>,
    max_ttl_ms: u64,
    paused: bool,
    agents: LookupMap<PublicKey, Agent>,
    denylist: IterableSet<AccountId>,
    used_verdicts: IterableMap<String, u64>,
    pending: LookupMap<u64, Pending>,
    next_request_id: u64,
}

#[near]
impl Gate {
    #[init]
    pub fn new(owner_id: AccountId, verifier_key: PublicKey, max_ttl_ms: u64) -> Self {
        assert_eq!(verifier_key.curve_type(), CurveType::ED25519, "REFUSED:bad_request");
        assert!(max_ttl_ms > 0, "REFUSED:bad_request");
        let mut key = [0; 32];
        key.copy_from_slice(&verifier_key.as_bytes()[1..]);
        Self {
            owner_id,
            verifier_current: key,
            verifier_previous: None,
            max_ttl_ms,
            paused: false,
            agents: LookupMap::new(b"agents".as_slice()),
            denylist: IterableSet::new(b"denylist".as_slice()),
            used_verdicts: IterableMap::new(b"used_verdicts".as_slice()),
            pending: LookupMap::new(b"pending".as_slice()),
            next_request_id: 0,
        }
    }

    pub fn version(&self) -> String {
        env!("CARGO_PKG_VERSION").to_owned()
    }
}
