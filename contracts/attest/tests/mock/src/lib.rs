//! TEST ONLY. Echoes caller chosen Borsh results and performs no verification.
use near_sdk::{env, Gas, GasWeight, NearToken, Promise};
use tee_verifier_interface::{Collateral, QuoteBytes};

#[no_mangle]
pub extern "C" fn verify_quote_with_collateral_dates() {
    let (quote, collateral): (QuoteBytes, Collateral) =
        borsh::from_slice(&env::input().unwrap()).unwrap();
    let (mode, delay, output): (u8, u8, Vec<u8>) = borsh::from_slice(&quote.0).unwrap();
    env::log_str("ATTEST_TEST_ONLY_VERIFIER");
    if mode == 1 {
        env::panic_str("TEST ONLY verifier failure");
    }
    if delay == 0 {
        env::value_return(&output);
    } else {
        let args = borsh::to_vec(&(
            QuoteBytes(borsh::to_vec(&(mode, delay - 1, output)).unwrap()),
            collateral,
        ))
        .unwrap();
        Promise::new(env::current_account_id())
            .function_call_weight(
                "verify_quote_with_collateral_dates",
                args,
                NearToken::from_yoctonear(0),
                Gas::from_tgas(150 - u64::from(16 - delay.min(16)) * 5),
                GasWeight(0),
            )
            .as_return()
            .detach();
    }
}
