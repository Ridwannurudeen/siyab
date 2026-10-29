mod events;

use events::emit;
use near_sdk::{env, near, serde_json::json, store::LookupMap, AccountId, PanicOnDefault};

const MAX_BATCH_RECORDS: usize = 29;

#[near(serializers = [json, borsh])]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
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

#[near(serializers = [json])]
pub struct LabelRecordView {
    subject: String,
    chain: String,
    label: String,
    category: String,
    provenance: String,
    assertion_hash: String,
    recorded_ms: u64,
}

impl From<LabelRecord> for LabelRecordView {
    fn from(record: LabelRecord) -> Self {
        Self {
            subject: record.subject,
            chain: record.chain,
            label: record.label,
            category: record.category,
            provenance: record.provenance,
            assertion_hash: lower_hex(&record.assertion_hash),
            recorded_ms: record.recorded_ms,
        }
    }
}

#[near(contract_state)]
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
        near_sdk::require!(
            records.len() <= MAX_BATCH_RECORDS,
            "REFUSED:batch_too_large"
        );
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
        near_sdk::require!(
            env::predecessor_account_id() == self.owner_id,
            "REFUSED:not_owner"
        );
        near_sdk::assert_one_yocto();
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

    pub fn get_label(&self, assertion_hash: String) -> Option<LabelRecordView> {
        self.labels
            .get(&assertion_hash)
            .cloned()
            .map(LabelRecordView::from)
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

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::{
        test_utils::{get_logs, VMContextBuilder},
        testing_env, NearToken,
    };

    fn hash(letter: char) -> String {
        letter.to_string().repeat(64)
    }

    fn context(predecessor: &str, deposit: NearToken, timestamp_ms: u64) {
        testing_env!(VMContextBuilder::new()
            .current_account_id("registry.testnet".parse().unwrap())
            .predecessor_account_id(predecessor.parse().unwrap())
            .attached_deposit(deposit)
            .block_timestamp(timestamp_ms * 1_000_000)
            .build());
    }

    fn registry() -> Registry {
        context("recorder.testnet", NearToken::from_yoctonear(0), 1_000);
        Registry::new(
            "owner.testnet".parse().unwrap(),
            "recorder.testnet".parse().unwrap(),
        )
    }

    fn subject(value: &str) -> AccountId {
        value.parse().unwrap()
    }

    fn record(registry: &mut Registry, subject_id: &str, verdict: VerdictCode, time: u64) {
        context("recorder.testnet", NearToken::from_yoctonear(0), time);
        registry.record(
            subject(subject_id),
            verdict,
            hash('a'),
            42,
            format!("verdict-{subject_id}"),
        );
    }

    #[test]
    fn recorder_can_record() {
        let mut registry = registry();
        record(&mut registry, "vendor.testnet", VerdictCode::Allow, 1_000);
        assert_eq!(registry.total_records(), 1);
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_recorder")]
    fn stranger_cannot_record() {
        let mut registry = registry();
        context("stranger.testnet", NearToken::from_yoctonear(0), 1_000);
        registry.record(
            subject("vendor.testnet"),
            VerdictCode::Allow,
            hash('a'),
            42,
            "verdict".to_owned(),
        );
    }

    #[test]
    fn owner_rotates_recorder_with_one_yocto() {
        let mut registry = registry();
        context("owner.testnet", NearToken::from_yoctonear(1), 1_000);
        registry.set_recorder(subject("new-recorder.testnet"));
        assert_eq!(registry.get_recorder(), subject("new-recorder.testnet"));
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_owner")]
    fn non_owner_cannot_rotate_recorder() {
        let mut registry = registry();
        context("stranger.testnet", NearToken::from_yoctonear(0), 1_000);
        registry.set_recorder(subject("new-recorder.testnet"));
    }

    #[test]
    #[should_panic(expected = "Requires attached deposit of exactly 1 yoctoNEAR")]
    fn recorder_rotation_requires_one_yocto() {
        let mut registry = registry();
        context("owner.testnet", NearToken::from_yoctonear(0), 1_000);
        registry.set_recorder(subject("new-recorder.testnet"));
    }

    #[test]
    fn record_upserts_without_inflating_subject_count() {
        let mut registry = registry();
        record(&mut registry, "vendor.testnet", VerdictCode::Allow, 1_000);
        record(&mut registry, "vendor.testnet", VerdictCode::Deny, 1_001);
        assert_eq!(registry.total_records(), 2);
        assert_eq!(registry.total_subjects(), 1);
        assert_eq!(
            registry.latest(subject("vendor.testnet")).unwrap().verdict,
            VerdictCode::Deny
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:batch_too_large")]
    fn batch_limit_is_enforced() {
        let mut registry = registry();
        context("recorder.testnet", NearToken::from_yoctonear(0), 1_000);
        registry.record_batch(
            (0..=MAX_BATCH_RECORDS)
                .map(|index| RecordInput {
                    subject: subject(&format!("subject{index}.testnet")),
                    verdict: VerdictCode::Allow,
                    evidence_hash: hash('a'),
                    observed_block: 42,
                    verdict_id: format!("verdict-{index}"),
                })
                .collect(),
        );
    }

    #[test]
    fn is_allowed_returns_each_reason() {
        let mut registry = registry();
        assert_eq!(
            registry.is_allowed(subject("missing.testnet"), 10),
            (false, "unrecorded".to_owned())
        );
        record(&mut registry, "allow.testnet", VerdictCode::Allow, 1_000);
        record(&mut registry, "deny.testnet", VerdictCode::Deny, 1_000);
        record(
            &mut registry,
            "unknown.testnet",
            VerdictCode::Unknown,
            1_000,
        );
        assert_eq!(
            registry.is_allowed(subject("allow.testnet"), 10),
            (true, "allow".to_owned())
        );
        assert_eq!(
            registry.is_allowed(subject("deny.testnet"), 10),
            (false, "deny".to_owned())
        );
        assert_eq!(
            registry.is_allowed(subject("unknown.testnet"), 10),
            (false, "unknown".to_owned())
        );
        context("recorder.testnet", NearToken::from_yoctonear(0), 1_011);
        assert_eq!(
            registry.is_allowed(subject("allow.testnet"), 10),
            (false, "stale".to_owned())
        );
    }

    #[test]
    fn latest_is_none_for_an_unrecorded_subject() {
        assert!(registry().latest(subject("missing.testnet")).is_none());
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_hash")]
    fn invalid_hash_is_refused() {
        let mut registry = registry();
        context("recorder.testnet", NearToken::from_yoctonear(0), 1_000);
        registry.record(
            subject("vendor.testnet"),
            VerdictCode::Allow,
            "ABC".to_owned(),
            42,
            "verdict".to_owned(),
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:label_exists")]
    fn record_label_is_once_per_assertion_hash() {
        let mut registry = registry();
        context("recorder.testnet", NearToken::from_yoctonear(0), 1_000);
        let args = (
            "vendor.testnet".to_owned(),
            "near".to_owned(),
            "catalog_entry".to_owned(),
            "infrastructure".to_owned(),
            "official_source".to_owned(),
            hash('a'),
        );
        registry.record_label(
            args.0.clone(),
            args.1.clone(),
            args.2.clone(),
            args.3.clone(),
            args.4.clone(),
            args.5.clone(),
        );
        assert_eq!(registry.total_labels(), 1);
        assert_eq!(
            registry.get_label(hash('a')).unwrap().assertion_hash,
            hash('a')
        );
        registry.record_label(
            args.0.clone(),
            args.1.clone(),
            args.2.clone(),
            args.3.clone(),
            args.4.clone(),
            args.5.clone(),
        );
    }

    #[test]
    fn emits_all_nep_297_events() {
        let mut registry = registry();
        record(&mut registry, "vendor.testnet", VerdictCode::Allow, 1_000);
        let verdict_logs = get_logs();
        context("recorder.testnet", NearToken::from_yoctonear(0), 1_000);
        registry.record_label(
            "vendor.testnet".to_owned(),
            "near".to_owned(),
            "catalog_entry".to_owned(),
            "infrastructure".to_owned(),
            "official_source".to_owned(),
            hash('b'),
        );
        let label_logs = get_logs();
        context("owner.testnet", NearToken::from_yoctonear(1), 1_000);
        registry.set_recorder(subject("next-recorder.testnet"));
        let recorder_logs = get_logs();
        for event in ["verdict_recorded", "label_recorded", "recorder_updated"] {
            let logs = match event {
                "verdict_recorded" => &verdict_logs,
                "label_recorded" => &label_logs,
                _ => &recorder_logs,
            };
            assert!(logs.iter().any(|line| {
                line.starts_with("EVENT_JSON:")
                    && line.contains("\"standard\":\"shieldbot-near\"")
                    && line.contains("\"version\":\"1.0.0\"")
                    && line.contains(&format!("\"event\":\"{event}\""))
            }));
        }
    }
}
