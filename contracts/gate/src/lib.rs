mod events;
mod policy;
mod types;
mod verdict;

use near_sdk::{
    env,
    near,
    json_types::U128,
    serde_json::json,
    store::{IterableMap, IterableSet, LookupMap},
    AccountId, CurveType, Gas, NearToken, PanicOnDefault, Promise, PromiseResult, PublicKey,
};
use events::emit;
use types::{Agent, Asset, Pending, TransferRequest, Verdict};
use verdict::verify;

const GAS_FT_TRANSFER: Gas = Gas::from_tgas(10);
const GAS_CALLBACK: Gas = Gas::from_tgas(10);
const GAS_BASE_RESERVE: Gas = Gas::from_tgas(10);

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

    pub fn request_transfer(&mut self, request: TransferRequest, verdict: Option<Verdict>) -> Promise {
        assert!(!self.paused, "REFUSED:paused");
        assert_eq!(env::predecessor_account_id(), env::current_account_id(), "REFUSED:unknown_agent");
        let agent_pk = env::signer_account_pk();
        assert_eq!(agent_pk.curve_type(), CurveType::ED25519, "REFUSED:unknown_agent");
        let mut agent = self.agents.get(&agent_pk).cloned().expect("REFUSED:unknown_agent");
        assert!(agent.enabled, "REFUSED:agent_disabled");
        assert!(!self.denylist.contains(&request.receiver_id), "REFUSED:denylisted");
        let allowlisted = agent.allowlist.contains(&request.receiver_id);
        assert!(allowlisted || agent.unknown_caps.is_some(), "REFUSED:not_allowlisted");
        assert!(env::is_valid_account_id(request.receiver_id.as_bytes()), "REFUSED:bad_request");
        assert!(request.amount.0 > 0, "REFUSED:bad_request");
        let asset = match (&request.kind[..], &request.token_id) {
            ("near", None) => Asset::Near,
            ("ft", Some(token_id)) if env::is_valid_account_id(token_id.as_bytes()) => Asset::Ft(token_id.clone()),
            _ => env::panic_str("REFUSED:bad_request"),
        };
        let verdict = verdict.expect("REFUSED:no_verdict");
        assert!(verdict.literal == "ALLOW", "REFUSED:not_allow");
        let now_ms = env::block_timestamp_ms();
        if let Err(code) = verify(
            &env::current_account_id(),
            &agent_key_bytes(&agent_pk),
            &request,
            &verdict,
            &self.verifier_current,
            self.verifier_previous.as_ref(),
            now_ms,
            self.max_ttl_ms,
        ) {
            env::panic_str(&format!("REFUSED:{code}"));
        }
        assert!(!self.used_verdicts.contains_key(&verdict.verdict_id), "REFUSED:verdict_used");
        let caps = if allowlisted {
            &mut agent.caps
        } else {
            agent.unknown_caps.as_mut().expect("unknown payee caps")
        };
        let cap = caps.iter_mut().find(|cap| cap.asset == asset).unwrap_or_else(|| {
            env::panic_str(if allowlisted { "REFUSED:per_call_cap" } else { "REFUSED:unknown_payee_cap" })
        });
        if now_ms.saturating_sub(cap.window_start_ms) >= cap.window_ms {
            cap.window_start_ms = now_ms;
            cap.spent = 0;
        }
        if allowlisted {
            assert!(request.amount.0 <= cap.per_call, "REFUSED:per_call_cap");
            assert!(cap.spent.checked_add(request.amount.0).is_some_and(|spent| spent <= cap.window_cap), "REFUSED:window_cap");
        } else {
            assert!(request.amount.0 <= cap.per_call, "REFUSED:unknown_payee_cap");
            assert!(cap.spent.checked_add(request.amount.0).is_some_and(|spent| spent <= cap.window_cap), "REFUSED:unknown_payee_cap");
        }
        let required_gas = GAS_FT_TRANSFER.as_gas() + GAS_CALLBACK.as_gas() + GAS_BASE_RESERVE.as_gas();
        assert!(env::prepaid_gas().as_gas() >= required_gas, "REFUSED:low_gas");

        cap.spent += request.amount.0;
        self.agents.insert(agent_pk.clone(), agent);
        self.used_verdicts.insert(verdict.verdict_id.clone(), verdict.expires_ms);
        let request_id = self.next_request_id;
        self.next_request_id = self.next_request_id.checked_add(1).expect("request id exhausted");
        self.pending.insert(request_id, Pending { agent_pk: agent_pk.clone(), asset: asset.clone(), amount: request.amount.0 });
        let agent_hex = lower_hex(&agent_key_bytes(&agent_pk));
        emit("transfer_scheduled", json!({
            "request_id": request_id,
            "agent_pk": agent_hex,
            "kind": request.kind,
            "token": request.token_id,
            "receiver": request.receiver_id,
            "amount": request.amount.0.to_string(),
            "verdict_id": verdict.verdict_id,
            "evidence_hash": verdict.evidence_hash
        }));
        let transfer = match asset {
            Asset::Near => Promise::new(request.receiver_id).transfer(NearToken::from_yoctonear(request.amount.0)),
            Asset::Ft(token_id) => Promise::new(token_id).function_call(
                "ft_transfer".to_owned(),
                json!({"receiver_id": request.receiver_id, "amount": U128(request.amount.0), "memo": request.memo}).to_string().into_bytes(),
                NearToken::from_yoctonear(1),
                GAS_FT_TRANSFER,
            ),
        };
        transfer.then(Self::ext(env::current_account_id()).with_static_gas(GAS_CALLBACK).on_transfer(request_id, !allowlisted))
    }

    #[private]
    pub fn on_transfer(&mut self, request_id: u64, unknown_payee: bool) {
        let pending = self.pending.remove(&request_id).expect("pending request missing");
        match env::promise_result(0) {
            PromiseResult::Successful(_) => emit("transfer_completed", json!({"request_id": request_id})),
            PromiseResult::Failed => {
                let mut agent = self.agents.get(&pending.agent_pk).cloned().expect("agent missing");
                let caps = if unknown_payee { agent.unknown_caps.as_mut().expect("unknown payee caps") } else { &mut agent.caps };
                let cap = caps.iter_mut().find(|cap| cap.asset == pending.asset).expect("debited cap missing");
                cap.spent = cap.spent.saturating_sub(pending.amount);
                self.agents.insert(pending.agent_pk, agent);
                emit("transfer_failed", json!({"request_id": request_id, "restored": true}));
            }
        }
    }
}

fn agent_key_bytes(public_key: &PublicKey) -> [u8; 32] {
    let mut bytes = [0; 32];
    bytes.copy_from_slice(&public_key.as_bytes()[1..]);
    bytes
}

fn lower_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
