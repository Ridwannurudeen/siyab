use near_sdk::{env, json_types::Base64VecU8, near, AccountId, Gas, GasWeight, NearToken, Promise};
use sha2::{Digest, Sha384};
use tee_verifier_interface::{Report, VerificationResultWithCollateralDates};

#[derive(Clone, near_sdk::serde::Serialize, near_sdk::serde::Deserialize)]
#[serde(crate = "near_sdk::serde")]
pub struct Event {
    imr: u32,
    event_type: u32,
    digest: String,
    event: String,
    event_payload: String,
}

#[near(contract_state)]
#[derive(Default)]
pub struct ReplayProbe {}

#[near]
impl ReplayProbe {
    pub fn verify(
        &self,
        verifier_id: AccountId,
        input: Base64VecU8,
        events: Vec<Event>,
    ) -> Promise {
        Promise::new(verifier_id)
            .function_call_weight(
                "verify_quote_with_collateral_dates",
                input.0,
                NearToken::from_yoctonear(0),
                Gas::from_tgas(200),
                GasWeight(0),
            )
            .then(
                Promise::new(env::current_account_id()).function_call_weight(
                    "on_verified",
                    near_sdk::serde_json::to_vec(&near_sdk::serde_json::json!({"events": events}))
                        .unwrap(),
                    NearToken::from_yoctonear(0),
                    Gas::from_tgas(60),
                    GasWeight(0),
                ),
            )
    }

    #[private]
    pub fn on_verified(&self, events: Vec<Event>) -> bool {
        let Ok(bytes) = env::promise_result_checked(0, 16384) else {
            return false;
        };
        let Ok(VerificationResultWithCollateralDates::Verified { report, .. }) =
            borsh::from_slice(&bytes)
        else {
            return false;
        };
        let Report::TD10(td) = report.report else {
            return false;
        };
        if report.status != "UpToDate" {
            return false;
        }
        let mut rtmr3 = [0; 48];
        for event in events.iter().filter(|event| event.imr == 3) {
            if event.event_type != 134217729 {
                return false;
            }
            let Ok(payload) = hex::decode(&event.event_payload) else {
                return false;
            };
            let Ok(digest) = hex::decode(&event.digest) else {
                return false;
            };
            let mut hash = Sha384::new();
            hash.update(event.event_type.to_le_bytes());
            hash.update(b":");
            hash.update(event.event.as_bytes());
            hash.update(b":");
            hash.update(payload);
            if hash.finalize()[..] != digest {
                return false;
            }
            let mut hash = Sha384::new();
            hash.update(rtmr3);
            hash.update(digest);
            rtmr3 = hash.finalize().into();
        }
        let accepted = rtmr3 == td.rt_mr3;
        env::log_str("ATTEST_REPLAY_CALLBACK");
        accepted
    }
}
