mod events;

use events::emit;
use near_sdk::{
    env,
    near,
    serde_json::json,
    store::LookupMap,
    AccountId, PanicOnDefault,
};

#[near(serializers = [json, borsh])]
#[derive(Clone, Copy, PartialEq, Eq)]
#[borsh(use_discriminant = true)]
pub enum VerdictCode {
    Unknown = 0,
    Allow = 1,
    Deny = 2,
}

#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct RecordInput {
    pub subject: AccountId,
    pub verdict: VerdictCode,
    pub evidence_hash: String,
    pub observed_block: u64,
    pub verdict_id: String,
}

#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct Record {
    verdict: VerdictCode,
    evidence_hash: [u8; 32],
    observed_block: u64,
    recorded_ms: u64,
    verdict_id: String,
}

#[near(serializers = [json])]
pub struct RecordView {
    verdict: VerdictCode,
    evidence_hash: String,
    observed_block: u64,
    recorded_ms: u64,
    verdict_id: String,
}

impl From<Record> for RecordView {
    fn from(record: Record) -> Self {
        Self {
            verdict: record.verdict,
            evidence_hash: lower_hex(&record.evidence_hash),
            observed_block: record.observed_block,
            recorded_ms: record.recorded_ms,
            verdict_id: record.verdict_id,
        }
    }
}

#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct LabelRecord {
    subject: String,
    chain: String,
    label: String,
    category: String,
    provenance: String,
    assertion_hash: [u8; 32],
    recorded_ms: u64,
}

#[near(contract_state, contract_metadata())]
#[derive(PanicOnDefault)]
pub struct Registry {
    owner_id: AccountId,
    recorder_id: AccountId,
    latest: LookupMap<AccountId, Record>,
    total_records: u64,
    total_subjects: u64,
    labels: LookupMap<String, LabelRecord>,
    total_labels: u64,
}

#[near]
impl Registry {
    #[init]
    pub fn new(owner_id: AccountId, recorder_id: AccountId) -> Self {
        Self {
            owner_id,
            recorder_id,
            latest: LookupMap::new(b"r".as_slice()),
            total_records: 0,
            total_subjects: 0,
            labels: LookupMap::new(b"l".as_slice()),
            total_labels: 0,
        }
    }

    pub fn record(
        &mut self,
        subject: AccountId,
        verdict: VerdictCode,
        evidence_hash: String,
        observed_block: u64,
        verdict_id: String,
    ) {
        self.assert_recorder();
        self.record_one(RecordInput {
            subject,
            verdict,
            evidence_hash,
            observed_block,
            verdict_id,
        });
    }

    pub fn record_batch(&mut self, records: Vec<RecordInput>) {
        self.assert_recorder();
        near_sdk::require!(records.len() <= 50, "REFUSED:batch_too_large");
        for record in records {
            self.record_one(record);
        }
    }

    pub fn record_label(
        &mut self,
        subject: String,
        chain: String,
        label: String,
        category: String,
        provenance: String,
        assertion_hash: String,
    ) {
        self.assert_recorder();
        let hash = parse_hash(&assertion_hash);
        near_sdk::require!(
            !self.labels.contains_key(&assertion_hash),
            "REFUSED:label_exists"
        );
        let record = LabelRecord {
            subject: subject.clone(),
            chain: chain.clone(),
            label: label.clone(),
            category: category.clone(),
            provenance: provenance.clone(),
            assertion_hash: hash,
            recorded_ms: env::block_timestamp_ms(),
        };
        self.labels.insert(assertion_hash.clone(), record);
        self.total_labels += 1;
        emit(
            "label_recorded",
            json!({
                "subject": subject,
                "chain": chain,
                "label": label,
                "category": category,
                "provenance": provenance,
                "assertion_hash": assertion_hash
            }),
        );
    }

    #[payable]
    pub fn set_recorder(&mut self, recorder_id: AccountId) {
        near_sdk::assert_one_yocto();
        near_sdk::require!(
            env::predecessor_account_id() == self.owner_id,
            "REFUSED:not_owner"
        );
        let previous = self.recorder_id.clone();
        self.recorder_id = recorder_id.clone();
        emit(
            "recorder_updated",
            json!({"previous": previous, "current": recorder_id}),
        );
    }

    pub fn latest(&self, subject: AccountId) -> Option<RecordView> {
        self.latest.get(&subject).cloned().map(RecordView::from)
    }

    pub fn is_allowed(&self, subject: AccountId, max_age_ms: u64) -> (bool, String) {
        let Some(record) = self.latest.get(&subject) else {
            return (false, "unrecorded".to_owned());
        };
        if env::block_timestamp_ms().saturating_sub(record.recorded_ms) > max_age_ms {
            return (false, "stale".to_owned());
        }
        match record.verdict {
            VerdictCode::Deny => (false, "deny".to_owned()),
            VerdictCode::Unknown => (false, "unknown".to_owned()),
            VerdictCode::Allow => (true, "allow".to_owned()),
        }
    }

    pub fn total_records(&self) -> u64 {
        self.total_records
    }

    pub fn total_subjects(&self) -> u64 {
        self.total_subjects
    }

    pub fn total_labels(&self) -> u64 {
        self.total_labels
    }

    pub fn get_label(&self, assertion_hash: String) -> Option<LabelRecord> {
        self.labels.get(&assertion_hash).cloned()
    }

    pub fn get_recorder(&self) -> AccountId {
        self.recorder_id.clone()
    }

    pub fn version(&self) -> String {
        env!("CARGO_PKG_VERSION").to_owned()
    }
}

impl Registry {
    fn assert_recorder(&self) {
        near_sdk::require!(
            env::predecessor_account_id() == self.recorder_id,
            "REFUSED:not_recorder"
        );
    }

    fn record_one(&mut self, input: RecordInput) {
        let evidence_hash = parse_hash(&input.evidence_hash);
        if !self.latest.contains_key(&input.subject) {
            self.total_subjects += 1;
        }
        self.latest.insert(
            input.subject.clone(),
            Record {
                verdict: input.verdict,
                evidence_hash,
                observed_block: input.observed_block,
                recorded_ms: env::block_timestamp_ms(),
                verdict_id: input.verdict_id.clone(),
            },
        );
        self.total_records += 1;
        emit(
            "verdict_recorded",
            json!({
                "subject": input.subject,
                "verdict": input.verdict,
                "evidence_hash": input.evidence_hash,
                "observed_block": input.observed_block,
                "verdict_id": input.verdict_id
            }),
        );
    }
}

fn parse_hash(value: &str) -> [u8; 32] {
    near_sdk::require!(
        value.len() == 64
            && value
                .as_bytes()
                .iter()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte)),
        "REFUSED:bad_hash"
    );
    let mut hash = [0; 32];
    for (index, byte) in hash.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&value[index * 2..index * 2 + 2], 16)
            .unwrap_or_else(|_| env::panic_str("REFUSED:bad_hash"));
    }
    hash
}

fn lower_hex(bytes: &[u8; 32]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

