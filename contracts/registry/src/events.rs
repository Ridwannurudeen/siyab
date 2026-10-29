use near_sdk::env;
use near_sdk::serde_json::Value;

pub const STANDARD: &str = "shieldbot-near";
pub const VERSION: &str = "1.0.0";

pub fn emit(event: &str, data: Value) {
    env::log_str(&format!(
        "EVENT_JSON:{}",
        near_sdk::serde_json::json!({
            "standard": STANDARD,
            "version": VERSION,
            "event": event,
            "data": [data]
        })
    ));
}
