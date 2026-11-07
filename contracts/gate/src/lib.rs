mod events;
mod policy;
mod types;
mod verdict;

use events::emit;
use near_sdk::{
    env,
    json_types::U128,
    near,
    serde_json::json,
    store::{IterableMap, IterableSet, LookupMap},
    AccountId, CurveType, Gas, NearToken, PanicOnDefault, Promise, PromiseError, PublicKey,
};
use types::{Agent, Asset, Pending, TransferRequest, Verdict};
use verdict::verify;

// FT receipt burned 1719818926064 gas, twice stays below the 5 Tgas floor.
const GAS_FT_TRANSFER: Gas = Gas::from_tgas(5);
// Callback receipt burned 1630829006859 gas, twice stays below the 5 Tgas floor.
const GAS_CALLBACK: Gas = Gas::from_tgas(5);
const GAS_BASE_RESERVE: Gas = Gas::from_tgas(10);
// This queue stays outside the root state to preserve the existing Borsh layout.
const USED_VERDICT_QUEUE_CURSOR_PREFIX: &[u8] = b"uq:c";
const USED_VERDICT_QUEUE_ENTRY_PREFIX: &[u8] = b"uq:e";
const USED_VERDICT_QUEUE_HEAD: u8 = 0;
const USED_VERDICT_QUEUE_TAIL: u8 = 1;
const USED_VERDICT_PRUNE_LIMIT: u8 = 4;

fn push_used_verdict(expires_ms: u64, verdict_id: String) {
    let mut cursors = LookupMap::<u8, u64>::new(USED_VERDICT_QUEUE_CURSOR_PREFIX);
    let mut entries = LookupMap::<u64, (u64, String)>::new(USED_VERDICT_QUEUE_ENTRY_PREFIX);
    let tail = cursors
        .get(&USED_VERDICT_QUEUE_TAIL)
        .copied()
        .unwrap_or(0);
    entries.insert(tail, (expires_ms, verdict_id));
    cursors.insert(
        USED_VERDICT_QUEUE_TAIL,
        tail.checked_add(1).expect("used verdict queue exhausted"),
    );
}

fn prune_expired_used_verdicts(gate: &mut Gate, now_ms: u64) -> u8 {
    let mut cursors = LookupMap::<u8, u64>::new(USED_VERDICT_QUEUE_CURSOR_PREFIX);
    let mut entries = LookupMap::<u64, (u64, String)>::new(USED_VERDICT_QUEUE_ENTRY_PREFIX);
    let mut head = cursors
        .get(&USED_VERDICT_QUEUE_HEAD)
        .copied()
        .unwrap_or(0);
    let tail = cursors
        .get(&USED_VERDICT_QUEUE_TAIL)
        .copied()
        .unwrap_or(0);
    let mut removed = 0;

    while head < tail && removed < USED_VERDICT_PRUNE_LIMIT {
        let Some((expires_ms, _)) = entries.get(&head) else {
            break;
        };
        if *expires_ms > now_ms {
            break;
        }
        let (queued_expires_ms, verdict_id) =
            entries.remove(&head).expect("used verdict queue entry");
        // Replay protection holds only because verify refuses expires_ms <= now_ms (verdict.rs:66)
        // before request_transfer checks used IDs. Keep this predicate on expiry, never age.
        if gate.used_verdicts.get(&verdict_id).copied() == Some(queued_expires_ms) {
            gate.used_verdicts.remove(&verdict_id);
        }
        head = head.checked_add(1).expect("used verdict queue exhausted");
        removed += 1;
    }

    if removed > 0 {
        cursors.insert(USED_VERDICT_QUEUE_HEAD, head);
    }
    removed
}

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
        near_sdk::require!(
            verifier_key.curve_type() == CurveType::ED25519,
            "REFUSED:bad_request"
        );
        near_sdk::require!(max_ttl_ms > 0, "REFUSED:bad_request");
        let mut key = [0; 32];
        key.copy_from_slice(&verifier_key.as_bytes()[1..]);
        Self {
            owner_id,
            verifier_current: key,
            verifier_previous: None,
            max_ttl_ms,
            paused: false,
            agents: LookupMap::new(b"a".as_slice()),
            denylist: IterableSet::new(b"d".as_slice()),
            used_verdicts: IterableMap::new(b"v".as_slice()),
            pending: LookupMap::new(b"p".as_slice()),
            next_request_id: 0,
        }
    }

    pub fn version(&self) -> String {
        env!("CARGO_PKG_VERSION").to_owned()
    }

    pub fn request_transfer(
        &mut self,
        request: TransferRequest,
        verdict: Option<Verdict>,
    ) -> Promise {
        let now_ms = env::block_timestamp_ms();
        prune_expired_used_verdicts(self, now_ms);
        near_sdk::require!(!self.paused, "REFUSED:paused");
        near_sdk::require!(
            env::predecessor_account_id() == env::current_account_id(),
            "REFUSED:unknown_agent"
        );
        let agent_pk = env::signer_account_pk();
        near_sdk::require!(
            agent_pk.curve_type() == CurveType::ED25519,
            "REFUSED:unknown_agent"
        );
        let mut agent = self
            .agents
            .get(&agent_pk)
            .cloned()
            .unwrap_or_else(|| env::panic_str("REFUSED:unknown_agent"));
        near_sdk::require!(agent.enabled, "REFUSED:agent_disabled");
        near_sdk::require!(
            !self.denylist.contains(&request.receiver_id),
            "REFUSED:denylisted"
        );
        let allowlisted = agent.allowlist.contains(&request.receiver_id);
        near_sdk::require!(
            allowlisted || agent.unknown_caps.is_some(),
            "REFUSED:not_allowlisted"
        );
        near_sdk::require!(
            env::is_valid_account_id(request.receiver_id.as_bytes()),
            "REFUSED:bad_request"
        );
        near_sdk::require!(request.amount.0 > 0, "REFUSED:bad_request");
        let asset = match (&request.kind[..], &request.token_id) {
            ("near", None) => Asset::Near,
            ("ft", Some(token_id)) if env::is_valid_account_id(token_id.as_bytes()) => {
                Asset::Ft(token_id.clone())
            }
            _ => env::panic_str("REFUSED:bad_request"),
        };
        let verdict = verdict.unwrap_or_else(|| env::panic_str("REFUSED:no_verdict"));
        near_sdk::require!(verdict.literal == "ALLOW", "REFUSED:not_allow");
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
        near_sdk::require!(
            !self.used_verdicts.contains_key(&verdict.verdict_id),
            "REFUSED:verdict_used"
        );
        let caps = if allowlisted {
            &mut agent.caps
        } else {
            agent.unknown_caps.as_mut().expect("unknown payee caps")
        };
        let cap = caps
            .iter_mut()
            .find(|cap| cap.asset == asset)
            .unwrap_or_else(|| {
                env::panic_str(if allowlisted {
                    "REFUSED:per_call_cap"
                } else {
                    "REFUSED:unknown_payee_cap"
                })
            });
        if now_ms.saturating_sub(cap.window_start_ms) >= cap.window_ms {
            cap.window_start_ms = now_ms;
            cap.spent = 0;
        }
        if allowlisted {
            near_sdk::require!(request.amount.0 <= cap.per_call, "REFUSED:per_call_cap");
            near_sdk::require!(
                cap.spent
                    .checked_add(request.amount.0)
                    .is_some_and(|spent| spent <= cap.window_cap),
                "REFUSED:window_cap"
            );
        } else {
            near_sdk::require!(
                request.amount.0 <= cap.per_call,
                "REFUSED:unknown_payee_cap"
            );
            near_sdk::require!(
                cap.spent
                    .checked_add(request.amount.0)
                    .is_some_and(|spent| spent <= cap.window_cap),
                "REFUSED:unknown_payee_cap"
            );
        }
        let required_gas =
            GAS_FT_TRANSFER.as_gas() + GAS_CALLBACK.as_gas() + GAS_BASE_RESERVE.as_gas();
        near_sdk::require!(
            env::prepaid_gas().as_gas() >= required_gas,
            "REFUSED:low_gas"
        );

        cap.spent += request.amount.0;
        let window_start_ms = cap.window_start_ms;
        self.agents.insert(agent_pk.clone(), agent);
        self.used_verdicts
            .insert(verdict.verdict_id.clone(), verdict.expires_ms);
        push_used_verdict(verdict.expires_ms, verdict.verdict_id.clone());
        let request_id = self.next_request_id;
        self.next_request_id = self
            .next_request_id
            .checked_add(1)
            .expect("request id exhausted");
        self.pending.insert(
            request_id,
            Pending {
                agent_pk: agent_pk.clone(),
                asset: asset.clone(),
                amount: request.amount.0,
                unknown_payee: !allowlisted,
                window_start_ms,
            },
        );
        let agent_hex = lower_hex(&agent_key_bytes(&agent_pk));
        emit(
            "transfer_scheduled",
            json!({
                "request_id": request_id,
                "agent_pk": agent_hex,
                "kind": request.kind,
                "token": request.token_id,
                "receiver": request.receiver_id,
                "amount": request.amount.0.to_string(),
                "verdict_id": verdict.verdict_id,
                "evidence_hash": verdict.evidence_hash
            }),
        );
        let transfer = match asset {
            Asset::Near => Promise::new(request.receiver_id).transfer(NearToken::from_yoctonear(request.amount.0)),
            Asset::Ft(token_id) => Promise::new(token_id).function_call(
                "ft_transfer".to_owned(),
                json!({"receiver_id": request.receiver_id, "amount": U128(request.amount.0), "memo": request.memo}).to_string().into_bytes(),
                NearToken::from_yoctonear(1),
                GAS_FT_TRANSFER,
            ),
        };
        transfer.then(
            Self::ext(env::current_account_id())
                .with_static_gas(GAS_CALLBACK)
                .on_transfer(request_id),
        )
    }

    #[private]
    pub fn on_transfer(&mut self, request_id: u64) {
        let pending = self
            .pending
            .remove(&request_id)
            .expect("pending request missing");
        if matches!(env::promise_result_checked(0, 0), Err(PromiseError::Failed)) {
            let mut restored = false;
            if let Some(mut agent) = self.agents.get(&pending.agent_pk).cloned() {
                let caps = if pending.unknown_payee {
                    agent.unknown_caps.as_mut()
                } else {
                    Some(&mut agent.caps)
                };
                if let Some(cap) = caps.and_then(|caps| {
                    caps.iter_mut().find(|cap| {
                        cap.asset == pending.asset && cap.window_start_ms == pending.window_start_ms
                    })
                }) {
                    cap.spent = cap.spent.saturating_sub(pending.amount);
                    self.agents.insert(pending.agent_pk, agent);
                    restored = true;
                }
            }
            emit(
                "transfer_failed",
                json!({"request_id": request_id, "restored": restored}),
            );
        } else {
            emit("transfer_completed", json!({"request_id": request_id}));
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

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};
    use near_sdk::{
        json_types::U128,
        test_utils::{testing_env_with_promise_results, VMContextBuilder},
        testing_env, PromiseResult,
    };
    use serde_json::Value;
    use std::{fs, path::PathBuf, str::FromStr};
    use types::{Agent, AssetCap};

    fn read_vector(name: &str) -> Value {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../fixtures/canon")
            .join(format!("{name}.json"));
        serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
    }

    fn decode_hex(value: &str) -> Vec<u8> {
        (0..value.len())
            .step_by(2)
            .map(|index| u8::from_str_radix(&value[index..index + 2], 16).unwrap())
            .collect()
    }

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

    fn public_key(bytes: &[u8]) -> PublicKey {
        PublicKey::from_str(&format!("ed25519:{}", base58_encode(bytes))).unwrap()
    }

    fn prepare(
        vector_name: &str,
        allowlisted: bool,
    ) -> (Gate, TransferRequest, Verdict, PublicKey) {
        prepare_with_result(
            vector_name,
            allowlisted,
            Gas::from_tgas(300),
            PromiseResult::Successful(Vec::new()),
        )
    }

    fn prepare_with_gas(
        vector_name: &str,
        allowlisted: bool,
        prepaid_gas: Gas,
    ) -> (Gate, TransferRequest, Verdict, PublicKey) {
        prepare_with_result(
            vector_name,
            allowlisted,
            prepaid_gas,
            PromiseResult::Successful(Vec::new()),
        )
    }

    fn prepare_with_result(
        vector_name: &str,
        allowlisted: bool,
        prepaid_gas: Gas,
        promise_result: PromiseResult,
    ) -> (Gate, TransferRequest, Verdict, PublicKey) {
        let vector = read_vector(vector_name);
        let gate_id: AccountId = if vector_name == "02-near-happy-sandbox" {
            vector["gate_account_id"].as_str().unwrap().parse().unwrap()
        } else {
            "gate.testnet".parse().unwrap()
        };
        let owner_id: AccountId = "owner.testnet".parse().unwrap();
        let agent_seed: [u8; 32] = env::sha256(b"gate test agent v1").try_into().unwrap();
        let agent_bytes = SigningKey::from_bytes(&agent_seed)
            .verifying_key()
            .to_bytes();
        let agent_pk = public_key(&agent_bytes);
        let verifier_bytes: [u8; 32] = decode_hex(vector["verifier_pub_hex"].as_str().unwrap())
            .try_into()
            .unwrap();
        let verifier_key = public_key(&verifier_bytes);
        let now_ms = if vector_name == "02-near-happy-sandbox" {
            vector["issued_ms"].as_u64().unwrap()
        } else {
            1_700_000_100_000u64
        };
        let context = VMContextBuilder::new()
            .current_account_id(gate_id.clone())
            .predecessor_account_id(gate_id.clone())
            .signer_account_pk(agent_pk.clone())
            .block_timestamp(now_ms * 1_000_000)
            .prepaid_gas(prepaid_gas)
            .build();
        testing_env_with_promise_results(context, promise_result);
        let mut gate = Gate::new(owner_id, verifier_key, 300_000);
        let token_id = vector["token_id"]
            .as_str()
            .map(|value| value.parse().unwrap());
        let asset = if vector["kind"] == "near" {
            Asset::Near
        } else {
            Asset::Ft(token_id.clone().unwrap())
        };
        let (per_call, window_cap) = if asset == Asset::Near {
            (
                2_000_000_000_000_000_000_000_000,
                5_000_000_000_000_000_000_000_000,
            )
        } else {
            (10_000_000, 50_000_000)
        };
        let cap = AssetCap {
            asset,
            per_call,
            window_cap,
            window_ms: 86_400_000,
            window_start_ms: now_ms,
            spent: 0,
        };
        let receiver: AccountId = vector["receiver_id"].as_str().unwrap().parse().unwrap();
        let unknown_caps = if allowlisted {
            None
        } else {
            Some(vec![cap.clone()])
        };
        gate.agents.insert(
            agent_pk.clone(),
            Agent {
                label: "unit agent".to_owned(),
                enabled: true,
                allowlist: if allowlisted {
                    vec![receiver]
                } else {
                    Vec::new()
                },
                caps: vec![cap],
                unknown_caps,
            },
        );
        let request = TransferRequest {
            kind: vector["kind"].as_str().unwrap().to_owned(),
            token_id,
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
        (gate, request, verdict, agent_pk)
    }

    fn submit(
        gate: &mut Gate,
        request: TransferRequest,
        verdict: Option<Verdict>,
        agent_pk: PublicKey,
    ) {
        let _ = agent_pk;
        let _ = gate.request_transfer(request, verdict);
    }

    fn seed_used_verdict(gate: &mut Gate, verdict_id: &str, expires_ms: u64) {
        gate.used_verdicts
            .insert(verdict_id.to_owned(), expires_ms);
        push_used_verdict(expires_ms, verdict_id.to_owned());
    }

    #[test]
    fn happy_path_debits_cap_and_emits_events() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        submit(&mut gate, request, Some(verdict.clone()), agent_pk.clone());
        let agent = gate.agents.get(&agent_pk).unwrap();
        assert_eq!(agent.caps[0].spent, 1_000_000);
        assert!(gate.used_verdicts.contains_key(&verdict.verdict_id));
        assert!(near_sdk::test_utils::get_logs()
            .iter()
            .any(|line| line.starts_with("EVENT_JSON:") && line.contains("transfer_scheduled")));
        let pending = gate.get_pending(0).unwrap();
        assert_eq!(serde_json::to_value(pending).unwrap()["amount"], "1000000");
    }

    #[test]
    fn expired_used_verdict_is_pruned_on_next_transfer() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        let now_ms = env::block_timestamp_ms();
        let expired_id = "expired-before-upgrade".to_owned();
        seed_used_verdict(&mut gate, &expired_id, now_ms - 1);

        submit(&mut gate, request, Some(verdict.clone()), agent_pk);

        assert!(!gate.used_verdicts.contains_key(&expired_id));
        assert!(gate.used_verdicts.contains_key(&verdict.verdict_id));
    }

    #[test]
    fn live_used_verdict_is_not_pruned() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        let now_ms = env::block_timestamp_ms();
        let live_id = "still-live".to_owned();
        seed_used_verdict(&mut gate, &live_id, now_ms + 1);

        submit(&mut gate, request, Some(verdict), agent_pk);

        assert!(gate.used_verdicts.contains_key(&live_id));
    }

    #[test]
    fn pruning_stops_at_four_entries_per_call() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        let now_ms = env::block_timestamp_ms();
        for index in 0..5 {
            seed_used_verdict(&mut gate, &format!("expired-{index}"), now_ms - 1);
        }

        assert_eq!(prune_expired_used_verdicts(&mut gate, now_ms), 4);
        for index in 0..4 {
            assert!(!gate
                .used_verdicts
                .contains_key(&format!("expired-{index}")));
        }
        assert!(gate.used_verdicts.contains_key(&"expired-4".to_owned()));
    }

    #[test]
    fn non_monotonic_expiries_wait_for_the_live_queue_head() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        seed_used_verdict(&mut gate, "live-head", 100);
        seed_used_verdict(&mut gate, "expired-next", 50);

        assert_eq!(prune_expired_used_verdicts(&mut gate, 60), 0);
        assert!(gate.used_verdicts.contains_key(&"live-head".to_owned()));
        assert!(gate
            .used_verdicts
            .contains_key(&"expired-next".to_owned()));

        assert_eq!(prune_expired_used_verdicts(&mut gate, 101), 2);
        assert!(!gate.used_verdicts.contains_key(&"live-head".to_owned()));
        assert!(!gate
            .used_verdicts
            .contains_key(&"expired-next".to_owned()));
    }

    #[test]
    #[should_panic(expected = "REFUSED:verdict_used")]
    fn stale_queue_entry_does_not_remove_reused_verdict_id() {
        let (mut gate, request, mut verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        let now_ms = env::block_timestamp_ms();
        let verdict_id = verdict.verdict_id.clone();
        seed_used_verdict(&mut gate, "live-head", now_ms + 1);
        seed_used_verdict(&mut gate, &verdict_id, now_ms - 1);

        let owner_context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("owner.testnet".parse().unwrap())
            .attached_deposit(NearToken::from_yoctonear(1))
            .block_timestamp(now_ms * 1_000_000)
            .build();
        testing_env!(owner_context);
        assert_eq!(gate.prune_used(vec![verdict_id.clone()]), 1);

        let agent_context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("gate.testnet".parse().unwrap())
            .signer_account_pk(agent_pk.clone())
            .block_timestamp(now_ms * 1_000_000)
            .prepaid_gas(Gas::from_tgas(300))
            .build();
        testing_env!(agent_context);
        verdict.issued_ms = now_ms;
        verdict.expires_ms = now_ms + 300_000;
        let message = verdict::canonical_message(
            &env::current_account_id(),
            &agent_key_bytes(&agent_pk),
            &request,
            &verdict,
        );
        let verifier_seed: [u8; 32] = env::sha256(b"gate test verifier v1").try_into().unwrap();
        let verifier = SigningKey::from_bytes(&verifier_seed);
        verdict.signature = lower_hex(&verifier.sign(&env::sha256(&message)).to_bytes());
        submit(
            &mut gate,
            request.clone(),
            Some(verdict.clone()),
            agent_pk.clone(),
        );
        assert_eq!(
            gate.used_verdicts.get(&verdict_id).copied(),
            Some(verdict.expires_ms)
        );

        let replay_ms = now_ms + 2;
        assert_eq!(prune_expired_used_verdicts(&mut gate, replay_ms), 2);
        assert_eq!(
            gate.used_verdicts.get(&verdict_id).copied(),
            Some(verdict.expires_ms)
        );

        let replay_context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("gate.testnet".parse().unwrap())
            .signer_account_pk(agent_pk.clone())
            .block_timestamp(replay_ms * 1_000_000)
            .prepaid_gas(Gas::from_tgas(300))
            .build();
        testing_env!(replay_context);
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:expired")]
    fn replay_of_pruned_verdict_is_still_refused_as_expired() {
        let (mut gate, request, verdict, agent_pk) = prepare("05-expired-unit", true);
        let now_ms = env::block_timestamp_ms();
        seed_used_verdict(&mut gate, &verdict.verdict_id, verdict.expires_ms);

        assert_eq!(prune_expired_used_verdicts(&mut gate, now_ms), 1);
        assert!(!gate.used_verdicts.contains_key(&verdict.verdict_id));
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    fn native_near_happy_path_debits_cap() {
        let (mut gate, request, verdict, agent_pk) = prepare("02-near-happy-sandbox", true);
        submit(&mut gate, request, Some(verdict), agent_pk.clone());
        assert_eq!(
            gate.agents.get(&agent_pk).unwrap().caps[0].spent,
            1_000_000_000_000_000_000_000_000
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:no_verdict")]
    fn missing_verdict_is_refused() {
        let (mut gate, request, _, agent_pk) = prepare("04-ft-happy-unit", true);
        submit(&mut gate, request, None, agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:expired")]
    fn expired_vector_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("05-expired-unit", true);
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:ttl_too_long")]
    fn excessive_ttl_vector_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("06-ttl-too-long-unit", true);
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_signature")]
    fn flipped_signature_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("07-bad-signature-unit", true);
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_allow")]
    fn non_allow_literal_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("08-wrong-literal-unit", true);
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_signature")]
    fn verdict_for_another_contract_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("10-wrong-contract-unit", true);
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_signature")]
    fn verdict_for_another_agent_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("11-wrong-agent-unit", true);
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    fn previous_key_is_accepted_before_deadline() {
        let (mut gate, request, verdict, agent_pk) = prepare("09-prev-key-unit", true);
        gate.verifier_previous = Some((gate.verifier_current, verdict.expires_ms));
        submit(&mut gate, request, Some(verdict), agent_pk);
        assert!(near_sdk::test_utils::get_logs()
            .iter()
            .any(|line| line.contains("transfer_scheduled")));
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_key_id")]
    fn previous_key_is_refused_after_deadline() {
        let (mut gate, request, verdict, agent_pk) = prepare("09-prev-key-unit", true);
        gate.verifier_previous = Some((gate.verifier_current, 1_700_000_099_999));
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    fn derives_agent_public_key_from_test_seed() {
        let seed = near_sdk::env::sha256(b"gate test agent v1");
        let seed: [u8; 32] = seed.try_into().unwrap();
        let signing_key = SigningKey::from_bytes(&seed);
        let vector = read_vector("04-ft-happy-unit");
        assert_eq!(
            signing_key.verifying_key().to_bytes().to_vec(),
            decode_hex(vector["agent_pk_hex"].as_str().unwrap())
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:paused")]
    fn paused_gate_refuses_first() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        gate.paused = true;
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:unknown_agent")]
    fn unregistered_signer_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        gate.agents.remove(&agent_pk);
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:agent_disabled")]
    fn disabled_agent_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        gate.agents.get_mut(&agent_pk).unwrap().enabled = false;
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:denylisted")]
    fn denylisted_receiver_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        gate.denylist.insert(request.receiver_id.clone());
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_allowlisted")]
    fn unlisted_receiver_without_unknown_caps_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        let agent = gate.agents.get_mut(&agent_pk).unwrap();
        agent.allowlist.clear();
        agent.unknown_caps = None;
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:verdict_used")]
    fn repeated_verdict_id_is_refused() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        gate.used_verdicts
            .insert(verdict.verdict_id.clone(), verdict.expires_ms);
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:per_call_cap")]
    fn per_call_cap_is_enforced() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        gate.agents.get_mut(&agent_pk).unwrap().caps[0].per_call = 1;
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:window_cap")]
    fn window_cap_is_enforced() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        gate.agents.get_mut(&agent_pk).unwrap().caps[0].window_cap = 1;
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    fn elapsed_window_resets_spent_before_debit() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        let cap = &mut gate.agents.get_mut(&agent_pk).unwrap().caps[0];
        cap.window_start_ms = 1_700_000_100_000 - cap.window_ms;
        cap.spent = cap.window_cap;
        submit(&mut gate, request, Some(verdict), agent_pk.clone());
        let cap = &gate.agents.get(&agent_pk).unwrap().caps[0];
        assert_eq!(cap.spent, 1_000_000);
        assert_eq!(cap.window_start_ms, 1_700_000_100_000);
    }

    #[test]
    #[should_panic(expected = "REFUSED:unknown_payee_cap")]
    fn unknown_payee_cap_is_enforced() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", false);
        gate.agents
            .get_mut(&agent_pk)
            .unwrap()
            .unknown_caps
            .as_mut()
            .unwrap()[0]
            .per_call = 1;
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:unknown_payee_cap")]
    fn unknown_payee_window_cap_is_enforced() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", false);
        gate.agents
            .get_mut(&agent_pk)
            .unwrap()
            .unknown_caps
            .as_mut()
            .unwrap()[0]
            .window_cap = 1;
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "REFUSED:low_gas")]
    fn insufficient_gas_is_refused() {
        let (mut gate, request, verdict, agent_pk) =
            prepare_with_gas("04-ft-happy-unit", true, Gas::from_tgas(19));
        submit(&mut gate, request, Some(verdict), agent_pk);
    }

    #[test]
    #[should_panic(expected = "Requires attached deposit of exactly 1 yoctoNEAR")]
    fn owner_method_requires_one_yocto() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        let context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("owner.testnet".parse().unwrap())
            .build();
        testing_env!(context);
        gate.set_paused(true);
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_owner")]
    fn owner_method_refuses_non_owner() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        let context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("stranger.testnet".parse().unwrap())
            .attached_deposit(NearToken::from_yoctonear(1))
            .build();
        testing_env!(context);
        gate.set_paused(true);
    }

    #[test]
    #[should_panic(expected = "Requires attached deposit of exactly 1 yoctoNEAR")]
    fn prune_used_requires_one_yocto() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        let context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("owner.testnet".parse().unwrap())
            .build();
        testing_env!(context);
        gate.prune_used(Vec::new());
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_owner")]
    fn prune_used_refuses_non_owner() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        let context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("stranger.testnet".parse().unwrap())
            .attached_deposit(NearToken::from_yoctonear(1))
            .build();
        testing_env!(context);
        gate.prune_used(Vec::new());
    }

    #[test]
    fn prune_used_removes_expired_and_skips_live_ids() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        let now_ms = 100;
        let context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("owner.testnet".parse().unwrap())
            .attached_deposit(NearToken::from_yoctonear(1))
            .block_timestamp(now_ms * 1_000_000)
            .build();
        testing_env!(context);
        gate.used_verdicts.insert("expired-legacy".to_owned(), 99);
        gate.used_verdicts.insert("live-legacy".to_owned(), 101);

        let removed = gate.prune_used(vec![
            "expired-legacy".to_owned(),
            "live-legacy".to_owned(),
            "missing-legacy".to_owned(),
        ]);

        assert_eq!(removed, 1);
        assert!(!gate
            .used_verdicts
            .contains_key(&"expired-legacy".to_owned()));
        assert!(gate
            .used_verdicts
            .contains_key(&"live-legacy".to_owned()));
    }

    #[test]
    fn prune_used_accepts_fifty_expired_ids() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        let now_ms = 100;
        let context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("owner.testnet".parse().unwrap())
            .attached_deposit(NearToken::from_yoctonear(1))
            .block_timestamp(now_ms * 1_000_000)
            .build();
        testing_env!(context);
        let ids: Vec<_> = (0..50).map(|index| format!("legacy-{index}")).collect();
        for id in &ids {
            gate.used_verdicts.insert(id.clone(), now_ms - 1);
        }

        assert_eq!(gate.prune_used(ids), 50);
    }

    #[test]
    fn used_verdicts_view_pages_in_map_order() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        seed_used_verdict(&mut gate, "first", 101);
        seed_used_verdict(&mut gate, "second", 102);
        seed_used_verdict(&mut gate, "third", 103);

        assert_eq!(
            gate.used_verdicts(1, 2),
            vec![("second".to_owned(), 102), ("third".to_owned(), 103)]
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:limit_too_large")]
    fn used_verdicts_view_rejects_limits_over_fifty() {
        let (gate, _, _, _) = prepare("04-ft-happy-unit", true);

        gate.used_verdicts(0, 51);
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_request")]
    fn prune_used_rejects_more_than_fifty_ids() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        let context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("owner.testnet".parse().unwrap())
            .attached_deposit(NearToken::from_yoctonear(1))
            .build();
        testing_env!(context);
        gate.prune_used((0..51).map(|index| format!("legacy-{index}")).collect());
    }

    #[test]
    #[should_panic(expected = "REFUSED:agent_exists")]
    fn duplicate_agent_registration_is_refused() {
        let (mut gate, _, _, agent_pk) = prepare("04-ft-happy-unit", true);
        let context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("owner.testnet".parse().unwrap())
            .attached_deposit(NearToken::from_yoctonear(1))
            .build();
        testing_env!(context);
        let _ = gate.register_agent(agent_pk, "duplicate".to_owned(), Vec::new(), None, U128(1));
    }

    #[test]
    #[should_panic(expected = "REFUSED:zero_allowance")]
    fn zero_agent_allowance_is_refused() {
        let (mut gate, _, _, _) = prepare("04-ft-happy-unit", true);
        let second_seed: [u8; 32] = env::sha256(b"gate test agent v2").try_into().unwrap();
        let second_public = SigningKey::from_bytes(&second_seed)
            .verifying_key()
            .to_bytes();
        let agent_pk = public_key(&second_public);
        let context = VMContextBuilder::new()
            .current_account_id("gate.testnet".parse().unwrap())
            .predecessor_account_id("owner.testnet".parse().unwrap())
            .attached_deposit(NearToken::from_yoctonear(1))
            .build();
        testing_env!(context);
        let _ = gate.register_agent(agent_pk, "zero".to_owned(), Vec::new(), None, U128(0));
    }

    #[test]
    fn failed_transfer_restores_the_debited_cap() {
        let (mut gate, request, verdict, agent_pk) = prepare_with_result(
            "04-ft-happy-unit",
            true,
            Gas::from_tgas(300),
            PromiseResult::Failed,
        );
        submit(&mut gate, request, Some(verdict), agent_pk.clone());
        gate.on_transfer(0);
        assert_eq!(gate.agents.get(&agent_pk).unwrap().caps[0].spent, 0);
        assert!(gate.pending.get(&0).is_none());
        assert!(near_sdk::test_utils::get_logs()
            .iter()
            .any(|line| line.contains("transfer_failed") && line.contains("true")));
    }

    fn assert_failed_unrestored(gate: &Gate) {
        assert!(gate.pending.get(&0).is_none());
        assert!(near_sdk::test_utils::get_logs()
            .iter()
            .any(|line| line.contains("\"event\":\"transfer_failed\"")
                && line.contains("\"restored\":false")));
    }

    #[test]
    fn failed_transfer_after_agent_revoked_is_not_restored() {
        let (mut gate, request, verdict, agent_pk) = prepare_with_result(
            "04-ft-happy-unit",
            true,
            Gas::from_tgas(300),
            PromiseResult::Failed,
        );
        submit(&mut gate, request, Some(verdict), agent_pk.clone());
        gate.agents.remove(&agent_pk);
        gate.on_transfer(0);
        assert!(gate.agents.get(&agent_pk).is_none());
        assert_failed_unrestored(&gate);
    }

    #[test]
    fn failed_transfer_after_asset_cap_removed_is_not_restored() {
        let (mut gate, request, verdict, agent_pk) = prepare_with_result(
            "04-ft-happy-unit",
            true,
            Gas::from_tgas(300),
            PromiseResult::Failed,
        );
        submit(&mut gate, request, Some(verdict), agent_pk.clone());
        gate.agents.get_mut(&agent_pk).unwrap().caps.clear();
        gate.on_transfer(0);
        assert!(gate.agents.get(&agent_pk).unwrap().caps.is_empty());
        assert_failed_unrestored(&gate);
    }

    #[test]
    fn failed_transfer_after_unknown_caps_removed_is_not_restored() {
        let (mut gate, request, verdict, agent_pk) = prepare_with_result(
            "04-ft-happy-unit",
            false,
            Gas::from_tgas(300),
            PromiseResult::Failed,
        );
        submit(&mut gate, request, Some(verdict), agent_pk.clone());
        gate.agents.get_mut(&agent_pk).unwrap().unknown_caps = None;
        gate.on_transfer(0);
        assert!(gate.agents.get(&agent_pk).unwrap().unknown_caps.is_none());
        assert_failed_unrestored(&gate);
    }

    #[test]
    fn failed_transfer_after_window_roll_is_not_restored() {
        let (mut gate, request, verdict, agent_pk) = prepare_with_result(
            "04-ft-happy-unit",
            true,
            Gas::from_tgas(300),
            PromiseResult::Failed,
        );
        submit(&mut gate, request, Some(verdict), agent_pk.clone());
        let cap = &mut gate.agents.get_mut(&agent_pk).unwrap().caps[0];
        cap.window_start_ms += cap.window_ms;
        cap.spent = 3_000_000;
        gate.on_transfer(0);
        assert_eq!(gate.agents.get(&agent_pk).unwrap().caps[0].spent, 3_000_000);
        assert_failed_unrestored(&gate);
    }

    #[test]
    fn successful_transfer_keeps_the_debit() {
        let (mut gate, request, verdict, agent_pk) = prepare("04-ft-happy-unit", true);
        submit(&mut gate, request, Some(verdict), agent_pk.clone());
        gate.on_transfer(0);
        assert_eq!(gate.agents.get(&agent_pk).unwrap().caps[0].spent, 1_000_000);
        assert!(gate.pending.get(&0).is_none());
    }
}
