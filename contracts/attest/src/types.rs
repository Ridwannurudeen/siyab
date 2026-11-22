use crate::measurement_id;

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MeasurementView {
    pub mrtd: String,
    pub rtmr0: String,
    pub rtmr1: String,
    pub rtmr2: String,
    pub key_provider_digest: String,
    pub compose_hash: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttestedKeyView {
    pub public_key: String,
    pub measurement_id: String,
    pub measurement: MeasurementView,
    pub ppid: String,
    pub aux: String,
    pub tcb_status: String,
    pub advisory_ids: Vec<String>,
    pub issued_at_ms: u64,
    pub registered_ms: u64,
    pub expires_ms: u64,
    pub collateral_expires_ms: u64,
}

#[derive(Debug, Clone, borsh::BorshSerialize)]
pub struct VoteIntent {
    pub registry_account_id: String,
    pub public_key: [u8; 32],
    pub measurement_id: [u8; 32],
    pub platforms: Vec<[u8; 16]>,
    pub verb: String,
    pub approval_id: String,
    pub wallet_pubkey: String,
    pub request_hash: [u8; 32],
    pub recipient: String,
    pub nonce: [u8; 32],
    pub evidence_hash: [u8; 32],
    pub expires_ms: u64,
}

#[derive(
    Debug,
    Clone,
    PartialEq,
    Eq,
    serde::Serialize,
    serde::Deserialize,
    borsh::BorshSerialize,
    borsh::BorshDeserialize,
)]
#[serde(deny_unknown_fields)]
pub struct RegistryConfig {
    pub verifier_account_id: String,
    pub signer_account_id: String,
    pub ed25519_domain_id: u64,
    pub max_lease_ms: u64,
    pub vote_recipients: Vec<String>,
}

#[derive(
    Debug,
    Clone,
    serde::Serialize,
    serde::Deserialize,
    borsh::BorshSerialize,
    borsh::BorshDeserialize,
)]
#[serde(remote = "Self", deny_unknown_fields)]
pub struct ReplayEvent {
    pub imr: u32,
    pub event_type: u32,
    pub digest: String,
    pub event: String,
    pub event_payload: String,
}

impl serde::Serialize for ReplayEvent {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        Self::serialize(self, serializer)
    }
}

impl<'de> serde::Deserialize<'de> for ReplayEvent {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct EventVisitor;

        impl<'de> serde::de::Visitor<'de> for EventVisitor {
            type Value = ReplayEvent;

            fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
                formatter.write_str("an event object")
            }

            fn visit_map<M: serde::de::MapAccess<'de>>(
                self,
                map: M,
            ) -> Result<Self::Value, M::Error> {
                ReplayEvent::deserialize(serde::de::value::MapAccessDeserializer::new(map))
            }
        }

        deserializer.deserialize_map(EventVisitor)
    }
}

#[derive(Clone, borsh::BorshSerialize, borsh::BorshDeserialize)]
pub struct RegisterContext {
    pub public_key: [u8; 32],
    pub issued_at_ms: u64,
    pub aux: [u8; 32],
    pub app_compose: String,
    pub event_log: Vec<ReplayEvent>,
    pub relayer: near_sdk::AccountId,
    pub deposit: u128,
}

#[derive(Debug, Clone, PartialEq, Eq, borsh::BorshSerialize, borsh::BorshDeserialize)]
pub(crate) struct StoredKey {
    pub mrtd: [u8; 48],
    pub rtmr0: [u8; 48],
    pub rtmr1: [u8; 48],
    pub rtmr2: [u8; 48],
    pub key_provider_digest: [u8; 48],
    pub compose_hash: [u8; 32],
    pub ppid: [u8; 16],
    pub aux: [u8; 32],
    pub advisory_ids: Vec<String>,
    pub issued_at_ms: u64,
    pub registered_ms: u64,
    pub expires_ms: u64,
    pub collateral_expires_ms: u64,
}

impl StoredKey {
    pub fn measurement_id(&self) -> [u8; 32] {
        measurement_id(
            &self.mrtd,
            &self.rtmr0,
            &self.rtmr1,
            &self.rtmr2,
            &self.key_provider_digest,
            &self.compose_hash,
        )
    }

    pub fn view(&self, public_key: &[u8; 32]) -> AttestedKeyView {
        AttestedKeyView {
            public_key: hex::encode(public_key),
            measurement_id: hex::encode(self.measurement_id()),
            measurement: MeasurementView {
                mrtd: hex::encode(self.mrtd),
                rtmr0: hex::encode(self.rtmr0),
                rtmr1: hex::encode(self.rtmr1),
                rtmr2: hex::encode(self.rtmr2),
                key_provider_digest: hex::encode(self.key_provider_digest),
                compose_hash: hex::encode(self.compose_hash),
            },
            ppid: hex::encode(self.ppid),
            aux: hex::encode(self.aux),
            tcb_status: "UpToDate".into(),
            advisory_ids: self.advisory_ids.clone(),
            issued_at_ms: self.issued_at_ms,
            registered_ms: self.registered_ms,
            expires_ms: self.expires_ms,
            collateral_expires_ms: self.collateral_expires_ms,
        }
    }
}
