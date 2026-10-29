use near_sdk::{near, PanicOnDefault};

#[derive(PanicOnDefault)]
#[near(contract_state)]
pub struct Gate {}

#[near]
impl Gate {
    #[init]
    pub fn new() -> Self {
        Self {}
    }

    pub fn version(&self) -> String {
        env!("CARGO_PKG_VERSION").to_owned()
    }
}
