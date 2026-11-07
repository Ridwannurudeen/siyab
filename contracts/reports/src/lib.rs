mod events;

use events::emit;
use near_sdk::{
    env, json_types::U128, near, serde::Serialize, serde_json::json, store::LookupMap, AccountId,
    PanicOnDefault,
};

pub const REPORT_DEPOSIT: u128 = 30_000_000_000_000_000_000_000;
const ACCEPTING_MODERATOR_PREFIX: &[u8] = b"ab";
const APPROVALS_REQUIRED_KEY: &[u8] = b"mt";
const REPORT_VOTES_PREFIX: &[u8] = b"mv";
const MAX_MODERATORS: usize = 7;
const MAX_PENDING_REPORTS: u64 = 5;
const MAX_EVIDENCE_URL_BYTES: usize = 512;
const MAX_PAGE_SIZE: u32 = 50;

#[near(serializers = [json, borsh])]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReportKind {
    Scam,
    Theft,
    Phishing,
    Exploit,
    KeyCompromise,
    Social,
}

impl ReportKind {
    fn is_deny(self) -> bool {
        match self {
            Self::Scam | Self::Theft | Self::Phishing | Self::Exploit | Self::KeyCompromise => true,
            Self::Social => false,
        }
    }
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReportOutcome {
    Loss,
    Attempt,
    NearMiss,
    Unknown,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReportState {
    Pending,
    Accepted,
    Rejected,
    Withdrawn,
    Contested,
    Overturned,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum VoteSide {
    Accept,
    Reject,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReportVote {
    moderator: AccountId,
    side: VoteSide,
    reason_sha256: String,
}

#[near(serializers = [borsh])]
#[derive(Clone)]
pub struct Report {
    id: u64,
    body_sha256: String,
    submitter: AccountId,
    subject: AccountId,
    kind: ReportKind,
    outcome: ReportOutcome,
    evidence_sha256: String,
    evidence_url: String,
    submitted_ms: u64,
    state: ReportState,
    moderator: Option<AccountId>,
    reason_sha256: Option<String>,
    moderated_ms: Option<u64>,
    dispute_ids: Vec<u64>,
    upheld_disputes: u8,
    accepted_prev: Option<u64>,
    accepted_next: Option<u64>,
    deny_prev: Option<u64>,
    deny_next: Option<u64>,
}

#[near(serializers = [json])]
pub struct ReportView {
    id: u64,
    body_sha256: String,
    submitter: AccountId,
    subject: AccountId,
    kind: ReportKind,
    outcome: ReportOutcome,
    evidence_sha256: String,
    evidence_url: String,
    submitted_ms: u64,
    state: ReportState,
    moderator: Option<AccountId>,
    reason_sha256: Option<String>,
    moderated_ms: Option<u64>,
    dispute_ids: Vec<u64>,
}

impl From<Report> for ReportView {
    fn from(report: Report) -> Self {
        Self {
            id: report.id,
            body_sha256: report.body_sha256,
            submitter: report.submitter,
            subject: report.subject,
            kind: report.kind,
            outcome: report.outcome,
            evidence_sha256: report.evidence_sha256,
            evidence_url: report.evidence_url,
            submitted_ms: report.submitted_ms,
            state: report.state,
            moderator: report.moderator,
            reason_sha256: report.reason_sha256,
            moderated_ms: report.moderated_ms,
            dispute_ids: report.dispute_ids,
        }
    }
}

#[near(serializers = [borsh])]
#[derive(Clone)]
pub struct Dispute {
    id: u64,
    report_id: u64,
    submitter: AccountId,
    evidence_sha256: String,
    evidence_url: String,
    submitted_ms: u64,
    open: bool,
}

#[near(serializers = [json])]
pub struct DisputeView {
    id: u64,
    report_id: u64,
    submitter: AccountId,
    evidence_sha256: String,
    evidence_url: String,
    submitted_ms: u64,
    open: bool,
}

impl From<Dispute> for DisputeView {
    fn from(dispute: Dispute) -> Self {
        Self {
            id: dispute.id,
            report_id: dispute.report_id,
            submitter: dispute.submitter,
            evidence_sha256: dispute.evidence_sha256,
            evidence_url: dispute.evidence_url,
            submitted_ms: dispute.submitted_ms,
            open: dispute.open,
        }
    }
}

#[near(serializers = [borsh])]
#[derive(Clone, Default)]
pub struct SubjectSummary {
    accepted: u64,
    pending: u64,
    contested: u64,
    latest_accepted_id: Option<u64>,
    accepted_deny: u64,
    latest_deny_id: Option<u64>,
}

#[near(serializers = [json])]
pub struct SubjectSummaryView {
    accepted: u64,
    pending: u64,
    contested: u64,
    latest_accepted_id: Option<u64>,
    latest_deny_id: Option<u64>,
    deny: bool,
}

#[near(serializers = [json, borsh])]
#[derive(Clone, Default)]
pub struct PublisherStats {
    submitted: u64,
    accepted: u64,
    rejected: u64,
    withdrawn: u64,
    overturned: u64,
}

#[near(serializers = [json])]
pub struct Totals {
    reports: u64,
    disputes: u64,
}

#[derive(Serialize)]
#[serde(crate = "near_sdk::serde")]
struct CanonicalBody<'a> {
    evidence_sha256: &'a str,
    evidence_url: &'a str,
    kind: ReportKind,
    outcome: ReportOutcome,
    subject: &'a AccountId,
    submitter: &'a AccountId,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Reports {
    owner_id: AccountId,
    moderators: Vec<AccountId>,
    state_version: u8,
    reports: LookupMap<u64, Report>,
    disputes: LookupMap<u64, Dispute>,
    subject_report_counts: LookupMap<AccountId, u64>,
    subject_report_ids: LookupMap<(AccountId, u64), u64>,
    submitter_report_counts: LookupMap<AccountId, u64>,
    submitter_report_ids: LookupMap<(AccountId, u64), u64>,
    pending_by_submitter: LookupMap<AccountId, u64>,
    subject_summaries: LookupMap<AccountId, SubjectSummary>,
    publisher_stats: LookupMap<AccountId, PublisherStats>,
    next_report_id: u64,
    next_dispute_id: u64,
}

#[near]
impl Reports {
    #[init]
    pub fn new(owner_id: AccountId, moderators: Vec<AccountId>) -> Self {
        assert_distinct_moderators(&moderators);
        Self {
            owner_id,
            moderators,
            state_version: 1,
            reports: LookupMap::new(b"r".as_slice()),
            disputes: LookupMap::new(b"d".as_slice()),
            subject_report_counts: LookupMap::new(b"sc".as_slice()),
            subject_report_ids: LookupMap::new(b"si".as_slice()),
            submitter_report_counts: LookupMap::new(b"pc".as_slice()),
            submitter_report_ids: LookupMap::new(b"pi".as_slice()),
            pending_by_submitter: LookupMap::new(b"pp".as_slice()),
            subject_summaries: LookupMap::new(b"ss".as_slice()),
            publisher_stats: LookupMap::new(b"ps".as_slice()),
            next_report_id: 0,
            next_dispute_id: 0,
        }
    }

    pub fn version(&self) -> String {
        env!("CARGO_PKG_VERSION").to_owned()
    }

    pub fn report_deposit(&self) -> U128 {
        U128(REPORT_DEPOSIT)
    }

    pub fn state_version(&self) -> u8 {
        self.state_version
    }

    pub fn approvals_required(&self) -> u32 {
        self.current_approvals_required()
    }

    #[payable]
    pub fn set_approvals_required(&mut self, required: u32) {
        self.assert_owner();
        near_sdk::assert_one_yocto();
        near_sdk::require!(
            required >= 1 && required as usize <= self.moderators.len(),
            "REFUSED:bad_threshold"
        );
        let previous = self.current_approvals_required();
        env::storage_write(APPROVALS_REQUIRED_KEY, &required.to_le_bytes());
        emit(
            "approvals_required_updated",
            json!({"previous": previous, "current": required}),
        );
    }

    #[payable]
    pub fn set_moderators(&mut self, moderators: Vec<AccountId>) {
        self.assert_owner();
        near_sdk::assert_one_yocto();
        assert_distinct_moderators(&moderators);
        near_sdk::require!(
            moderators.len() >= self.current_approvals_required() as usize,
            "REFUSED:threshold_above_moderators"
        );
        self.moderators = moderators;
        emit(
            "moderators_updated",
            json!({"moderators": self.moderators.clone()}),
        );
    }

    #[payable]
    pub fn submit_report(
        &mut self,
        subject: String,
        kind: ReportKind,
        outcome: ReportOutcome,
        evidence_sha256: String,
        evidence_url: String,
    ) -> u64 {
        self.assert_report_deposit();
        let subject = subject
            .parse::<AccountId>()
            .unwrap_or_else(|_| env::panic_str("REFUSED:bad_subject"));
        validate_evidence(&evidence_sha256, &evidence_url);

        let submitter = env::predecessor_account_id();
        let pending = self
            .pending_by_submitter
            .get(&submitter)
            .copied()
            .unwrap_or_default();
        near_sdk::require!(pending < MAX_PENDING_REPORTS, "REFUSED:pending_limit");

        let id = self.next_report_id;
        self.next_report_id = id
            .checked_add(1)
            .unwrap_or_else(|| env::panic_str("REFUSED:counter_exhausted"));
        let canonical = CanonicalBody {
            evidence_sha256: &evidence_sha256,
            evidence_url: &evidence_url,
            kind,
            outcome,
            subject: &subject,
            submitter: &submitter,
        };
        let canonical_json = near_sdk::serde_json::to_vec(&canonical).unwrap();
        let body_sha256 = lower_hex(&env::sha256(&canonical_json));
        let report = Report {
            id,
            body_sha256: body_sha256.clone(),
            submitter: submitter.clone(),
            subject: subject.clone(),
            kind,
            outcome,
            evidence_sha256,
            evidence_url,
            submitted_ms: env::block_timestamp_ms(),
            state: ReportState::Pending,
            moderator: None,
            reason_sha256: None,
            moderated_ms: None,
            dispute_ids: Vec::new(),
            upheld_disputes: 0,
            accepted_prev: None,
            accepted_next: None,
            deny_prev: None,
            deny_next: None,
        };
        self.reports.insert(id, report);

        let subject_offset = self
            .subject_report_counts
            .get(&subject)
            .copied()
            .unwrap_or_default();
        self.subject_report_ids
            .insert((subject.clone(), subject_offset), id);
        self.subject_report_counts
            .insert(subject.clone(), subject_offset + 1);

        let submitter_offset = self
            .submitter_report_counts
            .get(&submitter)
            .copied()
            .unwrap_or_default();
        self.submitter_report_ids
            .insert((submitter.clone(), submitter_offset), id);
        self.submitter_report_counts
            .insert(submitter.clone(), submitter_offset + 1);
        self.pending_by_submitter
            .insert(submitter.clone(), pending + 1);

        let mut summary = self.summary_for(&subject);
        summary.pending += 1;
        self.subject_summaries.insert(subject.clone(), summary);

        let mut stats = self.stats_for(&submitter);
        stats.submitted += 1;
        self.publisher_stats.insert(submitter, stats);

        emit(
            "report_submitted",
            json!({
                "report_id": id,
                "subject": subject,
                "state": ReportState::Pending,
                "body_sha256": body_sha256
            }),
        );
        id
    }

    #[payable]
    pub fn moderate(&mut self, id: u64, accept: bool, reason_sha256: String) {
        self.assert_moderator();
        near_sdk::assert_one_yocto();
        validate_hash(&reason_sha256);
        let moderator = env::predecessor_account_id();
        let mut report = self.report_or_refuse(id);
        near_sdk::require!(report.submitter != moderator, "REFUSED:self_moderation");
        near_sdk::require!(report.subject != moderator, "REFUSED:self_moderation");
        near_sdk::require!(report.state == ReportState::Pending, "REFUSED:not_pending");

        let mut decision_accept = accept;
        let mut decision_moderator = moderator.clone();
        let mut decision_reason_sha256 = reason_sha256.clone();
        let approvals_required = self.current_approvals_required();
        if approvals_required > 1 {
            let mut vote_map = LookupMap::<u64, Vec<ReportVote>>::new(REPORT_VOTES_PREFIX);
            let mut votes = vote_map.get(&id).cloned().unwrap_or_default();
            if let Some(index) = votes.iter().position(|vote| vote.moderator == moderator) {
                votes.remove(index);
            }
            votes.push(ReportVote {
                moderator: moderator.clone(),
                side: if accept {
                    VoteSide::Accept
                } else {
                    VoteSide::Reject
                },
                reason_sha256: reason_sha256.clone(),
            });
            vote_map.insert(id, votes.clone());
            vote_map.flush();

            let (accept_votes, reject_votes) = votes
                .iter()
                .filter(|vote| self.moderators.contains(&vote.moderator))
                .fold((0_u32, 0_u32), |(accepts, rejects), vote| match vote.side {
                    VoteSide::Accept => (accepts + 1, rejects),
                    VoteSide::Reject => (accepts, rejects + 1),
                });
            emit(
                "report_vote",
                json!({
                    "report_id": id,
                    "moderator": moderator,
                    "side": if accept { VoteSide::Accept } else { VoteSide::Reject },
                    "reason_sha256": reason_sha256,
                    "accept_votes": accept_votes,
                    "reject_votes": reject_votes,
                    "approvals_required": approvals_required
                }),
            );
            let accept_met = accept_votes >= approvals_required;
            let reject_met = reject_votes >= approvals_required;
            // Neither side at the threshold, or both (after the owner lowered it): the report stays
            // pending, so no single call settles a contested report; a moderator changing sides decides it.
            if accept_met == reject_met {
                return;
            }
            decision_accept = accept_met;
            let deciding_side = if decision_accept {
                VoteSide::Accept
            } else {
                VoteSide::Reject
            };
            let deciding_vote = votes
                .iter()
                .rev()
                .find(|vote| {
                    self.moderators.contains(&vote.moderator) && vote.side == deciding_side
                })
                .unwrap();
            decision_moderator = deciding_vote.moderator.clone();
            decision_reason_sha256 = deciding_vote.reason_sha256.clone();
        }

        let now = env::block_timestamp_ms();
        let mut summary = self.summary_for(&report.subject);
        let mut stats = self.stats_for(&report.submitter);
        let mut pending = self
            .pending_by_submitter
            .get(&report.submitter)
            .copied()
            .unwrap_or_default();
        pending -= 1;
        self.pending_by_submitter
            .insert(report.submitter.clone(), pending);

        if decision_accept {
            report.state = ReportState::Accepted;
            report.moderator = Some(decision_moderator.clone());
            LookupMap::<u64, AccountId>::new(ACCEPTING_MODERATOR_PREFIX)
                .insert(id, decision_moderator.clone());
            report.reason_sha256 = Some(decision_reason_sha256.clone());
            report.moderated_ms = Some(now);
            summary.pending -= 1;
            summary.accepted += 1;
            if report.kind.is_deny() {
                summary.accepted_deny = summary
                    .accepted_deny
                    .checked_add(1)
                    .unwrap_or_else(|| env::panic_str("REFUSED:counter_exhausted"));
            }
            stats.accepted += 1;
            self.append_accepted(&mut report, &mut summary);
            if report.kind.is_deny() {
                self.append_deny(&mut report, &mut summary);
            }
        } else {
            report.state = ReportState::Rejected;
            report.moderator = Some(decision_moderator.clone());
            report.reason_sha256 = Some(decision_reason_sha256.clone());
            report.moderated_ms = Some(now);
            summary.pending -= 1;
            stats.rejected += 1;
        }

        let subject = report.subject.clone();
        let submitter = report.submitter.clone();
        let body_sha256 = report.body_sha256.clone();
        self.reports.insert(id, report);
        self.subject_summaries.insert(subject.clone(), summary);
        self.publisher_stats.insert(submitter, stats);
        emit(
            "report_moderated",
            json!({
                "report_id": id,
                "subject": subject,
                "state": if decision_accept { ReportState::Accepted } else { ReportState::Rejected },
                "body_sha256": body_sha256,
                "moderator": decision_moderator,
                "reason_sha256": decision_reason_sha256
            }),
        );
    }

    #[payable]
    pub fn withdraw(&mut self, id: u64) {
        near_sdk::assert_one_yocto();
        let submitter = env::predecessor_account_id();
        let mut report = self.report_or_refuse(id);
        near_sdk::require!(report.submitter == submitter, "REFUSED:not_submitter");
        near_sdk::require!(report.state == ReportState::Pending, "REFUSED:not_pending");
        report.state = ReportState::Withdrawn;
        let subject = report.subject.clone();
        let body_sha256 = report.body_sha256.clone();
        self.reports.insert(id, report);

        let mut summary = self.summary_for(&subject);
        summary.pending -= 1;
        self.subject_summaries.insert(subject.clone(), summary);
        let pending = self
            .pending_by_submitter
            .get(&submitter)
            .copied()
            .unwrap_or_default();
        self.pending_by_submitter
            .insert(submitter.clone(), pending - 1);
        let mut stats = self.stats_for(&submitter);
        stats.withdrawn += 1;
        self.publisher_stats.insert(submitter, stats);

        emit(
            "report_withdrawn",
            json!({
                "report_id": id,
                "subject": subject,
                "state": ReportState::Withdrawn,
                "body_sha256": body_sha256
            }),
        );
    }

    #[payable]
    pub fn dispute(
        &mut self,
        report_id: u64,
        evidence_sha256: String,
        evidence_url: String,
    ) -> u64 {
        self.assert_report_deposit();
        validate_evidence(&evidence_sha256, &evidence_url);
        let mut report = self.report_or_refuse(report_id);
        let has_open_dispute = report
            .dispute_ids
            .last()
            .is_some_and(|id| self.disputes.get(id).is_some_and(|dispute| dispute.open));
        near_sdk::require!(!has_open_dispute, "REFUSED:dispute_open");
        near_sdk::require!(
            report.state == ReportState::Accepted,
            "REFUSED:not_accepted"
        );

        let id = self.next_dispute_id;
        self.next_dispute_id = id
            .checked_add(1)
            .unwrap_or_else(|| env::panic_str("REFUSED:counter_exhausted"));
        let submitter = env::predecessor_account_id();
        self.disputes.insert(
            id,
            Dispute {
                id,
                report_id,
                submitter,
                evidence_sha256,
                evidence_url,
                submitted_ms: env::block_timestamp_ms(),
                open: true,
            },
        );
        report.dispute_ids.push(id);
        report.state = ReportState::Contested;
        let subject = report.subject.clone();
        let report_submitter = report.submitter.clone();
        let body_sha256 = report.body_sha256.clone();
        self.reports.insert(report_id, report);

        let mut summary = self.summary_for(&subject);
        summary.contested += 1;
        self.subject_summaries.insert(subject.clone(), summary);
        let mut stats = self.stats_for(&report_submitter);
        stats.accepted -= 1;
        self.publisher_stats.insert(report_submitter, stats);

        emit(
            "report_disputed",
            json!({
                "report_id": report_id,
                "dispute_id": id,
                "subject": subject,
                "state": ReportState::Contested,
                "body_sha256": body_sha256
            }),
        );
        id
    }

    #[payable]
    pub fn resolve_dispute(&mut self, dispute_id: u64, overturn: bool, reason_sha256: String) {
        self.assert_moderator();
        near_sdk::assert_one_yocto();
        validate_hash(&reason_sha256);
        let moderator = env::predecessor_account_id();
        let mut dispute = self.dispute_or_refuse(dispute_id);
        near_sdk::require!(dispute.open, "REFUSED:dispute_closed");
        let mut report = self.report_or_refuse(dispute.report_id);
        near_sdk::require!(report.subject != moderator, "REFUSED:self_moderation");
        near_sdk::require!(
            report.state == ReportState::Contested,
            "REFUSED:not_contested"
        );
        let stored_accepting_moderator =
            LookupMap::<u64, AccountId>::new(ACCEPTING_MODERATOR_PREFIX)
                .get(&report.id)
                .cloned();
        let accepting_moderator = if report.upheld_disputes == 0 {
            stored_accepting_moderator
                .clone()
                .or(report.moderator.clone())
        } else {
            stored_accepting_moderator.clone()
        };
        near_sdk::require!(
            self.moderators.len() <= 1
                || report.upheld_disputes == 0
                || accepting_moderator.is_some(),
            "REFUSED:same_moderator"
        );
        near_sdk::require!(
            self.moderators.len() <= 1
                || (report.submitter != moderator && dispute.submitter != moderator),
            "REFUSED:self_moderation"
        );
        near_sdk::require!(
            self.moderators.len() <= 1 || accepting_moderator.as_ref() != Some(&moderator),
            "REFUSED:same_moderator"
        );
        let holds_accept_vote = self.moderators.len() > 1
            && LookupMap::<u64, Vec<ReportVote>>::new(REPORT_VOTES_PREFIX)
                .get(&report.id)
                .is_some_and(|votes| {
                    votes
                        .iter()
                        .any(|vote| vote.moderator == moderator && vote.side == VoteSide::Accept)
                });
        near_sdk::require!(!holds_accept_vote, "REFUSED:same_moderator");

        if report.upheld_disputes == 0 && stored_accepting_moderator.is_none() {
            if let Some(accepting_moderator) = accepting_moderator.as_ref() {
                LookupMap::<u64, AccountId>::new(ACCEPTING_MODERATOR_PREFIX)
                    .insert(report.id, accepting_moderator.clone());
            }
        }

        dispute.open = false;
        self.disputes.insert(dispute_id, dispute.clone());
        let mut summary = self.summary_for(&report.subject);
        let mut stats = self.stats_for(&report.submitter);
        summary.contested -= 1;
        if overturn {
            report.state = ReportState::Overturned;
            summary.accepted -= 1;
            if report.kind.is_deny() {
                summary.accepted_deny = summary
                    .accepted_deny
                    .checked_sub(1)
                    .unwrap_or_else(|| env::panic_str("REFUSED:counter_underflow"));
            }
            stats.overturned += 1;
            self.remove_accepted(&mut report, &mut summary);
            if report.kind.is_deny() {
                self.remove_deny(&mut report, &mut summary);
            }
        } else {
            report.state = ReportState::Accepted;
            report.upheld_disputes = report.upheld_disputes.saturating_add(1);
            stats.accepted += 1;
        }
        report.moderator = Some(moderator.clone());
        report.reason_sha256 = Some(reason_sha256.clone());
        report.moderated_ms = Some(env::block_timestamp_ms());
        let subject = report.subject.clone();
        let submitter = report.submitter.clone();
        let body_sha256 = report.body_sha256.clone();
        self.reports.insert(report.id, report);
        self.subject_summaries.insert(subject.clone(), summary);
        self.publisher_stats.insert(submitter, stats);

        emit(
            "dispute_resolved",
            json!({
                "report_id": dispute.report_id,
                "dispute_id": dispute_id,
                "subject": subject,
                "state": if overturn { ReportState::Overturned } else { ReportState::Accepted },
                "body_sha256": body_sha256,
                "moderator": moderator,
                "reason_sha256": reason_sha256
            }),
        );
    }

    pub fn get_report(&self, id: u64) -> Option<ReportView> {
        self.reports.get(&id).cloned().map(ReportView::from)
    }

    pub fn report_votes(&self, id: u64) -> Vec<ReportVote> {
        LookupMap::<u64, Vec<ReportVote>>::new(REPORT_VOTES_PREFIX)
            .get(&id)
            .cloned()
            .unwrap_or_default()
    }

    pub fn get_dispute(&self, id: u64) -> Option<DisputeView> {
        self.disputes.get(&id).cloned().map(DisputeView::from)
    }

    pub fn reports_by_subject(&self, subject: AccountId, from: u64, limit: u32) -> Vec<ReportView> {
        page_reports(
            &self.subject_report_ids,
            &self.subject_report_counts,
            &self.reports,
            subject,
            from,
            limit,
        )
    }

    pub fn reports_by_submitter(
        &self,
        submitter: AccountId,
        from: u64,
        limit: u32,
    ) -> Vec<ReportView> {
        page_reports(
            &self.submitter_report_ids,
            &self.submitter_report_counts,
            &self.reports,
            submitter,
            from,
            limit,
        )
    }

    pub fn subject_summary(&self, subject: AccountId) -> SubjectSummaryView {
        let summary = self.summary_for(&subject);
        SubjectSummaryView {
            accepted: summary.accepted,
            pending: summary.pending,
            contested: summary.contested,
            latest_accepted_id: summary.latest_accepted_id,
            latest_deny_id: summary.latest_deny_id,
            deny: summary.accepted_deny > 0,
        }
    }

    pub fn publisher_stats(&self, account: AccountId) -> PublisherStats {
        self.stats_for(&account)
    }

    pub fn totals(&self) -> Totals {
        Totals {
            reports: self.next_report_id,
            disputes: self.next_dispute_id,
        }
    }
}

impl Reports {
    fn current_approvals_required(&self) -> u32 {
        env::storage_read(APPROVALS_REQUIRED_KEY)
            .map(|bytes| u32::from_le_bytes(bytes.try_into().unwrap()))
            .unwrap_or(1)
    }

    fn assert_owner(&self) {
        near_sdk::require!(
            env::predecessor_account_id() == self.owner_id,
            "REFUSED:not_owner"
        );
    }

    fn assert_moderator(&self) {
        near_sdk::require!(
            self.moderators.contains(&env::predecessor_account_id()),
            "REFUSED:not_moderator"
        );
    }

    fn assert_report_deposit(&self) {
        near_sdk::require!(
            env::attached_deposit().as_yoctonear() == REPORT_DEPOSIT,
            "REFUSED:bad_deposit"
        );
    }

    fn report_or_refuse(&self, id: u64) -> Report {
        self.reports
            .get(&id)
            .cloned()
            .unwrap_or_else(|| env::panic_str("REFUSED:report_missing"))
    }

    fn dispute_or_refuse(&self, id: u64) -> Dispute {
        self.disputes
            .get(&id)
            .cloned()
            .unwrap_or_else(|| env::panic_str("REFUSED:dispute_missing"))
    }

    fn summary_for(&self, subject: &AccountId) -> SubjectSummary {
        self.subject_summaries
            .get(subject)
            .cloned()
            .unwrap_or_default()
    }

    fn stats_for(&self, submitter: &AccountId) -> PublisherStats {
        self.publisher_stats
            .get(submitter)
            .cloned()
            .unwrap_or_default()
    }

    fn append_accepted(&mut self, report: &mut Report, summary: &mut SubjectSummary) {
        report.accepted_prev = summary.latest_accepted_id;
        report.accepted_next = None;
        if let Some(previous_id) = summary.latest_accepted_id {
            let mut previous = self.report_or_refuse(previous_id);
            previous.accepted_next = Some(report.id);
            self.reports.insert(previous_id, previous);
        }
        summary.latest_accepted_id = Some(report.id);
    }

    fn append_deny(&mut self, report: &mut Report, summary: &mut SubjectSummary) {
        report.deny_prev = summary.latest_deny_id;
        report.deny_next = None;
        if let Some(previous_id) = summary.latest_deny_id {
            let mut previous = self.report_or_refuse(previous_id);
            previous.deny_next = Some(report.id);
            self.reports.insert(previous_id, previous);
        }
        summary.latest_deny_id = Some(report.id);
    }

    fn remove_accepted(&mut self, report: &mut Report, summary: &mut SubjectSummary) {
        if let Some(previous_id) = report.accepted_prev {
            let mut previous = self.report_or_refuse(previous_id);
            previous.accepted_next = report.accepted_next;
            self.reports.insert(previous_id, previous);
        }
        if let Some(next_id) = report.accepted_next {
            let mut next = self.report_or_refuse(next_id);
            next.accepted_prev = report.accepted_prev;
            self.reports.insert(next_id, next);
        } else {
            summary.latest_accepted_id = report.accepted_prev;
        }
        report.accepted_prev = None;
        report.accepted_next = None;
    }

    fn remove_deny(&mut self, report: &mut Report, summary: &mut SubjectSummary) {
        if let Some(previous_id) = report.deny_prev {
            let mut previous = self.report_or_refuse(previous_id);
            previous.deny_next = report.deny_next;
            self.reports.insert(previous_id, previous);
        }
        if let Some(next_id) = report.deny_next {
            let mut next = self.report_or_refuse(next_id);
            next.deny_prev = report.deny_prev;
            self.reports.insert(next_id, next);
        } else {
            summary.latest_deny_id = report.deny_prev;
        }
        report.deny_prev = None;
        report.deny_next = None;
    }
}

fn assert_distinct_moderators(moderators: &[AccountId]) {
    near_sdk::require!(
        moderators.len() <= MAX_MODERATORS,
        "REFUSED:too_many_moderators"
    );
    let mut seen = std::collections::BTreeSet::new();
    for moderator in moderators {
        near_sdk::require!(seen.insert(moderator), "REFUSED:duplicate_moderator");
    }
}

fn page_reports(
    ids: &LookupMap<(AccountId, u64), u64>,
    counts: &LookupMap<AccountId, u64>,
    reports: &LookupMap<u64, Report>,
    account: AccountId,
    from: u64,
    limit: u32,
) -> Vec<ReportView> {
    near_sdk::require!(limit <= MAX_PAGE_SIZE, "REFUSED:limit_too_large");
    let count = counts.get(&account).copied().unwrap_or_default();
    let end = from.saturating_add(u64::from(limit)).min(count);
    (from.min(end)..end)
        .map(|offset| {
            let id = ids
                .get(&(account.clone(), offset))
                .copied()
                .unwrap_or_else(|| env::panic_str("REFUSED:index_corrupt"));
            reports
                .get(&id)
                .cloned()
                .map(ReportView::from)
                .unwrap_or_else(|| env::panic_str("REFUSED:report_missing"))
        })
        .collect()
}

fn validate_evidence(evidence_sha256: &str, evidence_url: &str) {
    validate_hash(evidence_sha256);
    near_sdk::require!(
        evidence_url.starts_with("https://") && evidence_url.len() <= MAX_EVIDENCE_URL_BYTES,
        "REFUSED:bad_evidence_url"
    );
}

fn validate_hash(value: &str) {
    near_sdk::require!(
        value.len() == 64
            && value
                .as_bytes()
                .iter()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte)),
        "REFUSED:bad_hash"
    );
}

fn lower_hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::{
        serde_json::Value,
        test_utils::{get_logs, VMContextBuilder},
        testing_env, NearToken,
    };

    const ENUMS_JSON: &str = include_str!("../../../intel/record_enums.json");
    const BODY_VECTOR_JSON: &str = include_str!("../../../fixtures/records/body-vector.json");

    fn hash(letter: char) -> String {
        letter.to_string().repeat(64)
    }

    fn account(value: &str) -> AccountId {
        value.parse().unwrap()
    }

    fn context(predecessor: &str, deposit: u128, timestamp_ms: u64) {
        testing_env!(VMContextBuilder::new()
            .current_account_id("reports.testnet".parse().unwrap())
            .predecessor_account_id(account(predecessor))
            .attached_deposit(NearToken::from_yoctonear(deposit))
            .block_timestamp(timestamp_ms * 1_000_000)
            .build());
    }

    fn context_with_storage(
        predecessor: &str,
        deposit: u128,
        timestamp_ms: u64,
        storage_usage: u64,
    ) {
        testing_env!(VMContextBuilder::new()
            .current_account_id("reports.testnet".parse().unwrap())
            .predecessor_account_id(account(predecessor))
            .attached_deposit(NearToken::from_yoctonear(deposit))
            .block_timestamp(timestamp_ms * 1_000_000)
            .storage_usage(storage_usage)
            .build());
    }

    fn reports() -> Reports {
        context("reporter.testnet", 1, 1_000);
        Reports::new(account("owner.testnet"), vec![account("moderator.testnet")])
    }

    fn submit(reports: &mut Reports, submitter: &str, subject: &str) -> u64 {
        context(submitter, REPORT_DEPOSIT, 1_001);
        reports.submit_report(
            subject.to_owned(),
            ReportKind::Scam,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/report".to_owned(),
        )
    }

    fn moderate(reports: &mut Reports, id: u64, accept: bool) {
        context("moderator.testnet", 1, 1_002);
        reports.moderate(id, accept, hash('b'));
    }

    fn configure_threshold(reports: &mut Reports, moderators: &[&str], required: u32) {
        context("owner.testnet", 1, 1_000);
        reports.set_moderators(
            moderators
                .iter()
                .map(|moderator| account(moderator))
                .collect(),
        );
        context("owner.testnet", 1, 1_001);
        reports.set_approvals_required(required);
    }

    fn cast_vote(
        reports: &mut Reports,
        id: u64,
        moderator: &str,
        accept: bool,
        reason: char,
        timestamp_ms: u64,
    ) {
        context(moderator, 1, timestamp_ms);
        reports.moderate(id, accept, hash(reason));
    }

    fn report_in_state(state: ReportState) -> (Reports, u64) {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        match state {
            ReportState::Pending => {}
            ReportState::Accepted => moderate(&mut reports, id, true),
            ReportState::Rejected => moderate(&mut reports, id, false),
            ReportState::Withdrawn => {
                context("reporter.testnet", 1, 1_002);
                reports.withdraw(id);
            }
            ReportState::Contested | ReportState::Overturned => {
                moderate(&mut reports, id, true);
                context("disputer.testnet", REPORT_DEPOSIT, 1_003);
                let dispute_id =
                    reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
                if state == ReportState::Overturned {
                    context("moderator.testnet", 1, 1_004);
                    reports.resolve_dispute(dispute_id, true, hash('d'));
                }
            }
        }
        (reports, id)
    }

    fn enum_values(name: &str) -> Vec<String> {
        near_sdk::serde_json::from_str::<Value>(ENUMS_JSON).unwrap()[name]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_str().unwrap().to_owned())
            .collect()
    }

    fn flush_storage(reports: &mut Reports) {
        reports.reports.flush();
        reports.disputes.flush();
        reports.subject_report_counts.flush();
        reports.subject_report_ids.flush();
        reports.submitter_report_counts.flush();
        reports.submitter_report_ids.flush();
        reports.pending_by_submitter.flush();
        reports.subject_summaries.flush();
        reports.publisher_stats.flush();
        LookupMap::<u64, Vec<ReportVote>>::new(REPORT_VOTES_PREFIX).flush();
    }

    #[test]
    fn initializes_empty_counters_and_views() {
        let reports = reports();
        let summary = reports.subject_summary(account("vendor.testnet"));
        let stats = reports.publisher_stats(account("reporter.testnet"));
        assert_eq!(reports.totals().reports, 0);
        assert_eq!(reports.totals().disputes, 0);
        assert_eq!(summary.accepted, 0);
        assert_eq!(summary.pending, 0);
        assert_eq!(summary.contested, 0);
        assert_eq!(summary.latest_accepted_id, None);
        assert_eq!(summary.latest_deny_id, None);
        assert!(!summary.deny);
        assert_eq!(stats.submitted, 0);
        assert!(reports.get_report(0).is_none());
        assert!(reports.get_dispute(0).is_none());
    }

    #[test]
    fn version_views_return_the_crate_and_state_versions() {
        let reports = reports();
        assert_eq!(reports.version(), env!("CARGO_PKG_VERSION"));
        assert_eq!(reports.state_version(), 1);
    }

    #[test]
    fn report_deposit_view_returns_fixed_amount() {
        let reports = reports();
        assert_eq!(
            reports.report_deposit(),
            U128(30_000_000_000_000_000_000_000)
        );
    }

    #[test]
    fn rust_enum_values_match_the_canonical_json() {
        assert_eq!(
            [
                ReportKind::Scam,
                ReportKind::Theft,
                ReportKind::Phishing,
                ReportKind::Exploit,
                ReportKind::KeyCompromise,
                ReportKind::Social
            ]
            .iter()
            .map(|value| near_sdk::serde_json::to_value(value)
                .unwrap()
                .as_str()
                .unwrap()
                .to_owned())
            .collect::<Vec<_>>(),
            enum_values("kind")
        );
        assert_eq!(
            [
                ReportOutcome::Loss,
                ReportOutcome::Attempt,
                ReportOutcome::NearMiss,
                ReportOutcome::Unknown
            ]
            .iter()
            .map(|value| near_sdk::serde_json::to_value(value)
                .unwrap()
                .as_str()
                .unwrap()
                .to_owned())
            .collect::<Vec<_>>(),
            enum_values("outcome")
        );
        assert_eq!(
            [
                ReportState::Pending,
                ReportState::Accepted,
                ReportState::Rejected,
                ReportState::Withdrawn,
                ReportState::Contested,
                ReportState::Overturned
            ]
            .iter()
            .map(|value| near_sdk::serde_json::to_value(value)
                .unwrap()
                .as_str()
                .unwrap()
                .to_owned())
            .collect::<Vec<_>>(),
            enum_values("state")
        );
    }

    #[test]
    fn submission_hash_is_sha256_of_sorted_compact_json() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        let report = reports.get_report(id).unwrap();
        let expected_body = near_sdk::serde_json::json!({
            "evidence_sha256": hash('a'),
            "evidence_url": "https://evidence.example/report",
            "kind": "scam",
            "outcome": "loss",
            "subject": "vendor.testnet",
            "submitter": "reporter.testnet"
        });
        let expected_json = near_sdk::serde_json::to_vec(&expected_body).unwrap();
        assert_eq!(report.body_sha256, lower_hex(&env::sha256(&expected_json)));
        assert_eq!(report.state, ReportState::Pending);
        assert!(report.moderator.is_none());
    }

    #[test]
    fn canonical_body_hash_matches_shared_vector() {
        let vector: Value = near_sdk::serde_json::from_str(BODY_VECTOR_JSON).unwrap();
        let input = &vector["input"];
        let kind = match input["kind"].as_str().unwrap() {
            "scam" => ReportKind::Scam,
            "theft" => ReportKind::Theft,
            "phishing" => ReportKind::Phishing,
            "exploit" => ReportKind::Exploit,
            "key_compromise" => ReportKind::KeyCompromise,
            "social" => ReportKind::Social,
            other => panic!("unexpected report kind in vector: {other}"),
        };
        let outcome = match input["outcome"].as_str().unwrap() {
            "loss" => ReportOutcome::Loss,
            "attempt" => ReportOutcome::Attempt,
            "near_miss" => ReportOutcome::NearMiss,
            "unknown" => ReportOutcome::Unknown,
            other => panic!("unexpected report outcome in vector: {other}"),
        };
        let subject = account(input["subject"].as_str().unwrap());
        let submitter = account(input["submitter"].as_str().unwrap());
        let canonical_json = near_sdk::serde_json::to_vec(&CanonicalBody {
            evidence_sha256: input["evidence_sha256"].as_str().unwrap(),
            evidence_url: input["evidence_url"].as_str().unwrap(),
            kind,
            outcome,
            subject: &subject,
            submitter: &submitter,
        })
        .unwrap();
        let expected_json = near_sdk::serde_json::to_vec(&near_sdk::serde_json::json!({
            "evidence_sha256": input["evidence_sha256"],
            "evidence_url": input["evidence_url"],
            "kind": input["kind"],
            "outcome": input["outcome"],
            "subject": input["subject"],
            "submitter": input["submitter"]
        }))
        .unwrap();
        assert_eq!(canonical_json, expected_json);
        assert_eq!(
            lower_hex(&env::sha256(&canonical_json)),
            vector["expected_sha256"].as_str().unwrap()
        );
    }

    #[test]
    fn owner_can_set_moderators() {
        let mut reports = reports();
        context("owner.testnet", 1, 1_000);
        reports.set_moderators(vec![account("next.testnet")]);
        let update = get_logs();
        let event_line = update
            .iter()
            .find(|line| line.contains("\"event\":\"moderators_updated\""))
            .unwrap();
        let event: Value =
            near_sdk::serde_json::from_str(event_line.strip_prefix("EVENT_JSON:").unwrap())
                .unwrap();
        assert_eq!(event["standard"], "shieldbot-near");
        assert_eq!(event["version"], "1.1.0");
        assert_eq!(event["data"][0]["moderators"][0], "next.testnet");
        context("reporter.testnet", REPORT_DEPOSIT, 1_001);
        let id = reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Scam,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/report".to_owned(),
        );
        context("next.testnet", 1, 1_002);
        reports.moderate(id, false, hash('b'));
        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Rejected);
    }

    #[test]
    fn approvals_required_defaults_to_one_and_emits_an_update() {
        let mut reports = reports();
        assert_eq!(reports.approvals_required(), 1);
        assert_eq!(reports.state_version(), 1);
        context("owner.testnet", 1, 1_000);
        reports.set_approvals_required(1);
        let event_line = get_logs()
            .into_iter()
            .find(|line| line.contains("\"event\":\"approvals_required_updated\""))
            .unwrap();
        let event: Value =
            near_sdk::serde_json::from_str(event_line.strip_prefix("EVENT_JSON:").unwrap())
                .unwrap();
        assert_eq!(event["version"], "1.1.0");
        assert_eq!(event["data"][0]["previous"], 1);
        assert_eq!(event["data"][0]["current"], 1);
        assert_eq!(
            event["data"][0]
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            vec!["previous", "current"]
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_owner")]
    fn non_owner_cannot_set_approvals_required() {
        let mut reports = reports();
        context("stranger.testnet", 1, 1_000);
        reports.set_approvals_required(1);
    }

    #[test]
    fn setting_approvals_required_needs_exactly_one_yocto() {
        let mut reports = reports();
        for deposit in [0, 2] {
            context("owner.testnet", deposit, 1_000);
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                reports.set_approvals_required(1);
            }))
            .is_err());
        }
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_threshold")]
    fn zero_approvals_required_is_refused() {
        let mut reports = reports();
        context("owner.testnet", 1, 1_000);
        reports.set_approvals_required(0);
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_threshold")]
    fn approvals_required_above_moderator_count_is_refused() {
        let mut reports = reports();
        context("owner.testnet", 1, 1_000);
        reports.set_approvals_required(2);
    }

    #[test]
    #[should_panic(expected = "REFUSED:threshold_above_moderators")]
    fn moderator_list_cannot_be_shorter_than_approvals_required() {
        let mut reports = reports();
        configure_threshold(
            &mut reports,
            &[
                "moderator-a.testnet",
                "moderator-b.testnet",
                "moderator-c.testnet",
            ],
            2,
        );
        context("owner.testnet", 1, 1_002);
        reports.set_moderators(vec![account("moderator-a.testnet")]);
    }

    #[test]
    #[should_panic(expected = "REFUSED:too_many_moderators")]
    fn new_refuses_more_than_seven_moderators() {
        let moderators = (0..8)
            .map(|index| account(&format!("moderator-{index}.testnet")))
            .collect();
        context("owner.testnet", 0, 1_000);
        Reports::new(account("owner.testnet"), moderators);
    }

    #[test]
    #[should_panic(expected = "REFUSED:too_many_moderators")]
    fn set_moderators_refuses_more_than_seven_moderators() {
        let mut reports = reports();
        let moderators = (0..8)
            .map(|index| account(&format!("moderator-{index}.testnet")))
            .collect();
        context("owner.testnet", 1, 1_000);
        reports.set_moderators(moderators);
    }

    #[test]
    fn two_of_three_accepting_moderators_accept_a_report() {
        let mut reports = reports();
        configure_threshold(
            &mut reports,
            &[
                "moderator-a.testnet",
                "moderator-b.testnet",
                "moderator-c.testnet",
            ],
            2,
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");

        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'b', 1_002);
        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Pending);
        cast_vote(&mut reports, id, "moderator-b.testnet", true, 'c', 1_003);

        let report = reports.get_report(id).unwrap();
        assert_eq!(report.state, ReportState::Accepted);
        assert_eq!(report.moderator, Some(account("moderator-b.testnet")));
        assert_eq!(report.reason_sha256, Some(hash('c')));
        assert_eq!(
            LookupMap::<u64, AccountId>::new(ACCEPTING_MODERATOR_PREFIX)
                .get(&id)
                .cloned(),
            Some(account("moderator-b.testnet"))
        );
        assert_eq!(reports.report_votes(id).len(), 2);
    }

    #[test]
    fn split_threshold_votes_leave_the_report_pending() {
        let mut reports = reports();
        configure_threshold(
            &mut reports,
            &[
                "moderator-a.testnet",
                "moderator-b.testnet",
                "moderator-c.testnet",
            ],
            2,
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'b', 1_002);
        cast_vote(&mut reports, id, "moderator-b.testnet", false, 'c', 1_003);

        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Pending);
        assert_eq!(reports.report_votes(id).len(), 2);
        assert_eq!(reports.report_votes(id)[0].side, VoteSide::Accept);
        assert_eq!(reports.report_votes(id)[1].side, VoteSide::Reject);
    }

    #[test]
    fn repeat_vote_replaces_the_old_vote_and_re_tallies() {
        let mut reports = reports();
        configure_threshold(
            &mut reports,
            &[
                "moderator-a.testnet",
                "moderator-b.testnet",
                "moderator-c.testnet",
            ],
            2,
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'b', 1_002);
        cast_vote(&mut reports, id, "moderator-b.testnet", false, 'c', 1_003);
        cast_vote(&mut reports, id, "moderator-a.testnet", false, 'd', 1_004);

        let report = reports.get_report(id).unwrap();
        assert_eq!(report.state, ReportState::Rejected);
        assert_eq!(report.moderator, Some(account("moderator-a.testnet")));
        assert_eq!(report.reason_sha256, Some(hash('d')));
        let votes = reports.report_votes(id);
        assert_eq!(votes.len(), 2);
        assert_eq!(votes[0].moderator, account("moderator-b.testnet"));
        assert_eq!(votes[0].side, VoteSide::Reject);
        assert_eq!(votes[1].moderator, account("moderator-a.testnet"));
        assert_eq!(votes[1].side, VoteSide::Reject);
        assert_eq!(votes[1].reason_sha256, hash('d'));
    }

    #[test]
    fn lowering_threshold_then_repeating_a_vote_finishes_the_report() {
        let mut reports = reports();
        configure_threshold(
            &mut reports,
            &[
                "moderator-a.testnet",
                "moderator-b.testnet",
                "moderator-c.testnet",
            ],
            3,
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'b', 1_002);
        cast_vote(&mut reports, id, "moderator-b.testnet", false, 'c', 1_003);
        cast_vote(&mut reports, id, "moderator-c.testnet", true, 'd', 1_004);
        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Pending);

        context("owner.testnet", 1, 1_005);
        reports.set_approvals_required(2);
        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'e', 1_006);

        let report = reports.get_report(id).unwrap();
        assert_eq!(report.state, ReportState::Accepted);
        assert_eq!(report.moderator, Some(account("moderator-a.testnet")));
        assert_eq!(report.reason_sha256, Some(hash('e')));
    }

    #[test]
    fn both_sides_at_a_lowered_threshold_stay_pending_until_a_moderator_changes_side() {
        let mut reports = reports();
        configure_threshold(
            &mut reports,
            &[
                "moderator-a.testnet",
                "moderator-b.testnet",
                "moderator-c.testnet",
                "moderator-d.testnet",
            ],
            3,
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'b', 1_002);
        cast_vote(&mut reports, id, "moderator-b.testnet", true, 'c', 1_003);
        cast_vote(&mut reports, id, "moderator-c.testnet", false, 'd', 1_004);
        cast_vote(&mut reports, id, "moderator-d.testnet", false, 'e', 1_005);

        context("owner.testnet", 1, 1_006);
        reports.set_approvals_required(2);
        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'f', 1_007);
        cast_vote(&mut reports, id, "moderator-c.testnet", false, 'a', 1_008);
        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Pending);

        cast_vote(&mut reports, id, "moderator-d.testnet", true, 'b', 1_009);
        let report = reports.get_report(id).unwrap();
        assert_eq!(report.state, ReportState::Accepted);
        assert_eq!(report.moderator, Some(account("moderator-d.testnet")));
        assert_eq!(report.reason_sha256, Some(hash('b')));
    }

    #[test]
    fn a_removed_moderators_vote_stops_counting_at_threshold_two() {
        let mut reports = reports();
        configure_threshold(
            &mut reports,
            &[
                "moderator-a.testnet",
                "moderator-b.testnet",
                "moderator-c.testnet",
            ],
            3,
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'b', 1_002);
        cast_vote(&mut reports, id, "moderator-b.testnet", true, 'c', 1_003);

        context("owner.testnet", 1, 1_004);
        reports.set_approvals_required(2);
        context("owner.testnet", 1, 1_005);
        reports.set_moderators(vec![
            account("moderator-b.testnet"),
            account("moderator-c.testnet"),
        ]);

        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Pending);
        assert_eq!(reports.report_votes(id).len(), 2);
    }

    #[test]
    fn a_readded_moderators_vote_counts_on_the_next_tally() {
        let mut reports = reports();
        configure_threshold(
            &mut reports,
            &[
                "moderator-a.testnet",
                "moderator-b.testnet",
                "moderator-c.testnet",
            ],
            3,
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'b', 1_002);
        cast_vote(&mut reports, id, "moderator-b.testnet", true, 'c', 1_003);

        context("owner.testnet", 1, 1_004);
        reports.set_approvals_required(2);
        context("owner.testnet", 1, 1_005);
        reports.set_moderators(vec![
            account("moderator-b.testnet"),
            account("moderator-c.testnet"),
        ]);
        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Pending);
        context("owner.testnet", 1, 1_006);
        reports.set_moderators(vec![
            account("moderator-b.testnet"),
            account("moderator-c.testnet"),
            account("moderator-a.testnet"),
        ]);
        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Pending);

        cast_vote(&mut reports, id, "moderator-c.testnet", false, 'd', 1_007);

        let report = reports.get_report(id).unwrap();
        assert_eq!(report.state, ReportState::Accepted);
        assert_eq!(report.moderator, Some(account("moderator-b.testnet")));
        assert_eq!(report.reason_sha256, Some(hash('c')));
    }

    #[test]
    fn threshold_one_moderation_keeps_legacy_events_and_stores_no_vote() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        cast_vote(&mut reports, id, "moderator.testnet", true, 'b', 1_002);

        let logs = get_logs();
        assert_eq!(logs.len(), 1);
        assert!(logs[0].contains("\"event\":\"report_moderated\""));
        assert!(!logs[0].contains("report_vote"));
        assert!(reports.report_votes(id).is_empty());
        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Accepted);
    }

    #[test]
    fn threshold_vote_and_moderation_events_have_the_required_key_order() {
        let mut reports = reports();
        configure_threshold(
            &mut reports,
            &["moderator-a.testnet", "moderator-b.testnet"],
            2,
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'b', 1_002);
        cast_vote(&mut reports, id, "moderator-b.testnet", true, 'c', 1_003);
        let logs = get_logs();
        let parse_event = |name: &str| {
            let line = logs
                .iter()
                .find(|line| line.contains(&format!("\"event\":\"{name}\"")))
                .unwrap();
            near_sdk::serde_json::from_str::<Value>(line.strip_prefix("EVENT_JSON:").unwrap())
                .unwrap()
        };
        let vote = parse_event("report_vote");
        assert_eq!(vote["version"], "1.1.0");
        assert_eq!(vote["data"][0]["report_id"], id);
        assert_eq!(vote["data"][0]["moderator"], "moderator-b.testnet");
        assert_eq!(vote["data"][0]["side"], "accept");
        assert_eq!(vote["data"][0]["reason_sha256"], hash('c'));
        assert_eq!(vote["data"][0]["accept_votes"], 2);
        assert_eq!(vote["data"][0]["reject_votes"], 0);
        assert_eq!(vote["data"][0]["approvals_required"], 2);
        assert_eq!(
            vote["data"][0]
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            vec![
                "report_id",
                "moderator",
                "side",
                "reason_sha256",
                "accept_votes",
                "reject_votes",
                "approvals_required"
            ]
        );

        let moderated = parse_event("report_moderated");
        assert_eq!(moderated["version"], "1.1.0");
        assert_eq!(moderated["data"][0]["state"], "accepted");
        assert_eq!(moderated["data"][0]["moderator"], "moderator-b.testnet");
        assert_eq!(moderated["data"][0]["reason_sha256"], hash('c'));
        assert_eq!(
            moderated["data"][0]
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<Vec<_>>(),
            vec![
                "report_id",
                "subject",
                "state",
                "body_sha256",
                "moderator",
                "reason_sha256"
            ]
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:duplicate_moderator")]
    fn duplicate_moderators_are_refused_by_set_moderators() {
        let mut reports = reports();
        context("owner.testnet", 1, 1_000);
        reports.set_moderators(vec![account("a.testnet"), account("a.testnet")]);
    }

    #[test]
    #[should_panic(expected = "REFUSED:duplicate_moderator")]
    fn duplicate_moderators_are_refused_by_new() {
        context("owner.testnet", 0, 1_000);
        Reports::new(account("owner.testnet"), vec![account("a.testnet"), account("a.testnet")]);
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_owner")]
    fn non_owner_cannot_set_moderators() {
        let mut reports = reports();
        context("stranger.testnet", 1, 1_000);
        reports.set_moderators(Vec::new());
    }

    #[test]
    fn owner_moderator_and_withdraw_calls_require_exactly_one_yocto() {
        let mut reports = reports();
        for deposit in [0, 2] {
            context("owner.testnet", deposit, 1_000);
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                reports.set_moderators(vec![account("moderator.testnet")]);
            }))
            .is_err());
        }

        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        for deposit in [0, 2] {
            context("moderator.testnet", deposit, 1_002);
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                reports.moderate(id, true, hash('b'));
            }))
            .is_err());
        }
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        for deposit in [0, 2] {
            context("moderator.testnet", deposit, 1_004);
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                reports.resolve_dispute(dispute_id, false, hash('d'));
            }))
            .is_err());
        }

        let withdraw_id = submit(&mut reports, "reporter.testnet", "withdraw.testnet");
        for deposit in [0, 2] {
            context("reporter.testnet", deposit, 1_005);
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                reports.withdraw(withdraw_id);
            }))
            .is_err());
        }
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_deposit")]
    fn report_requires_the_pinned_deposit() {
        let mut reports = reports();
        context("reporter.testnet", REPORT_DEPOSIT - 1, 1_000);
        reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Scam,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/report".to_owned(),
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_deposit")]
    fn report_rejects_a_deposit_above_the_exact_amount() {
        let mut reports = reports();
        context("reporter.testnet", REPORT_DEPOSIT + 1, 1_000);
        reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Scam,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/report".to_owned(),
        );
    }

    #[test]
    fn pending_summary_counts_each_new_submission() {
        let mut reports = reports();
        submit(&mut reports, "reporter.testnet", "vendor.testnet");
        submit(&mut reports, "reporter.testnet", "vendor.testnet");
        let summary = reports.subject_summary(account("vendor.testnet"));
        assert_eq!(summary.pending, 2);
        assert_eq!(summary.accepted, 0);
        assert_eq!(summary.contested, 0);
        assert_eq!(summary.latest_accepted_id, None);
        assert!(!summary.deny);
        assert_eq!(
            reports
                .publisher_stats(account("reporter.testnet"))
                .submitted,
            2
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_subject")]
    fn invalid_subject_is_refused() {
        let mut reports = reports();
        context("reporter.testnet", REPORT_DEPOSIT, 1_000);
        reports.submit_report(
            "not an account".to_owned(),
            ReportKind::Scam,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/report".to_owned(),
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_evidence_url")]
    fn non_https_evidence_url_is_refused() {
        let mut reports = reports();
        context("reporter.testnet", REPORT_DEPOSIT, 1_000);
        reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Scam,
            ReportOutcome::Loss,
            hash('a'),
            "http://evidence.example/report".to_owned(),
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_evidence_url")]
    fn evidence_url_over_512_bytes_is_refused() {
        let mut reports = reports();
        context("reporter.testnet", REPORT_DEPOSIT, 1_000);
        reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Scam,
            ReportOutcome::Loss,
            hash('a'),
            format!("https://{}", "x".repeat(506)),
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_hash")]
    fn invalid_evidence_hash_is_refused() {
        let mut reports = reports();
        context("reporter.testnet", REPORT_DEPOSIT, 1_000);
        reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Scam,
            ReportOutcome::Loss,
            "A".repeat(64),
            "https://evidence.example/report".to_owned(),
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:pending_limit")]
    fn submitter_is_limited_to_five_pending_reports() {
        let mut reports = reports();
        for _ in 0..MAX_PENDING_REPORTS {
            submit(&mut reports, "reporter.testnet", "vendor.testnet");
        }
        submit(&mut reports, "reporter.testnet", "vendor.testnet");
    }

    #[test]
    fn accepted_report_updates_summary_and_publisher_stats() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        let summary = reports.subject_summary(account("vendor.testnet"));
        assert_eq!(summary.pending, 0);
        assert_eq!(summary.accepted, 1);
        assert_eq!(summary.contested, 0);
        assert_eq!(summary.latest_accepted_id, Some(id));
        assert!(summary.deny);
        assert_eq!(
            reports
                .summary_for(&account("vendor.testnet"))
                .accepted_deny,
            1
        );
        let stats = reports.publisher_stats(account("reporter.testnet"));
        assert_eq!(stats.submitted, 1);
        assert_eq!(stats.accepted, 1);
    }

    #[test]
    fn accepted_social_report_does_not_set_deny_flag() {
        let mut reports = reports();
        context("reporter.testnet", REPORT_DEPOSIT, 1_001);
        let id = reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Social,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/report".to_owned(),
        );
        moderate(&mut reports, id, true);
        let summary = reports.subject_summary(account("vendor.testnet"));
        assert_eq!(summary.accepted, 1);
        assert_eq!(summary.latest_deny_id, None);
        assert!(!summary.deny);
        assert_eq!(
            reports
                .summary_for(&account("vendor.testnet"))
                .accepted_deny,
            0
        );
    }

    #[test]
    fn deny_follows_kind_alone_for_every_outcome_when_accepted_and_contested() {
        let kinds = [
            ReportKind::Scam,
            ReportKind::Theft,
            ReportKind::Phishing,
            ReportKind::Exploit,
            ReportKind::KeyCompromise,
            ReportKind::Social,
        ];
        let outcomes = [
            ReportOutcome::Loss,
            ReportOutcome::Attempt,
            ReportOutcome::NearMiss,
            ReportOutcome::Unknown,
        ];

        let mut pair = 0u32;
        for kind in kinds {
            for outcome in outcomes {
                pair += 1;
                let subject = format!("vendor-{pair}.testnet");
                let mut reports = reports();
                context("reporter.testnet", REPORT_DEPOSIT, 1_001);
                let id = reports.submit_report(
                    subject.clone(),
                    kind,
                    outcome,
                    hash('a'),
                    "https://evidence.example/report".to_owned(),
                );
                moderate(&mut reports, id, true);
                assert_eq!(
                    reports.subject_summary(account(&subject)).deny,
                    kind.is_deny()
                );

                context("disputer.testnet", REPORT_DEPOSIT, 1_003);
                reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
                assert_eq!(
                    reports.subject_summary(account(&subject)).deny,
                    kind.is_deny()
                );
            }
        }
    }

    #[test]
    fn newer_social_report_does_not_clear_deny_flag() {
        let mut reports = reports();
        context("reporter.testnet", REPORT_DEPOSIT, 1_001);
        let theft = reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Theft,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/theft".to_owned(),
        );
        moderate(&mut reports, theft, true);
        context("reporter.testnet", REPORT_DEPOSIT, 1_003);
        let social = reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Social,
            ReportOutcome::Loss,
            hash('b'),
            "https://evidence.example/social".to_owned(),
        );
        context("moderator.testnet", 1, 1_004);
        reports.moderate(social, true, hash('b'));
        let summary = reports.subject_summary(account("vendor.testnet"));
        assert!(summary.deny);
        assert_eq!(summary.latest_accepted_id, Some(social));
        assert_eq!(summary.latest_deny_id, Some(theft));
        assert_eq!(
            reports
                .summary_for(&account("vendor.testnet"))
                .accepted_deny,
            1
        );
    }

    #[test]
    fn rejected_report_updates_summary_and_publisher_stats() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, false);
        let summary = reports.subject_summary(account("vendor.testnet"));
        assert_eq!(summary.pending, 0);
        assert_eq!(summary.accepted, 0);
        assert_eq!(
            reports
                .publisher_stats(account("reporter.testnet"))
                .rejected,
            1
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_moderator")]
    fn non_moderator_cannot_moderate() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("stranger.testnet", 1, 1_002);
        reports.moderate(id, true, hash('b'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:self_moderation")]
    fn report_submitter_cannot_moderate_own_report() {
        context("reporter.testnet", 1, 1_000);
        let mut reports = Reports::new(
            account("owner.testnet"),
            vec![account("reporter.testnet")],
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("reporter.testnet", 1, 1_002);
        reports.moderate(id, true, hash('b'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:self_moderation")]
    fn report_subject_cannot_moderate_own_report() {
        context("owner.testnet", 0, 1_000);
        let mut reports = Reports::new(account("owner.testnet"), vec![account("vendor.testnet")]);
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("vendor.testnet", 1, 1_002);
        reports.moderate(id, true, hash('b'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:report_missing")]
    fn moderator_cannot_moderate_missing_report() {
        let mut reports = reports();
        context("moderator.testnet", 1, 1_002);
        reports.moderate(42, true, hash('b'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_pending")]
    fn moderator_cannot_moderate_a_second_time() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        moderate(&mut reports, id, false);
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_hash")]
    fn moderation_reason_hash_is_validated() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("moderator.testnet", 1, 1_002);
        reports.moderate(id, true, "ABC".to_owned());
    }

    #[test]
    fn submitter_can_withdraw_pending_report() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("reporter.testnet", 1, 1_002);
        reports.withdraw(id);
        assert_eq!(
            reports.get_report(id).unwrap().state,
            ReportState::Withdrawn
        );
        assert_eq!(
            reports.subject_summary(account("vendor.testnet")).pending,
            0
        );
        assert_eq!(
            reports
                .publisher_stats(account("reporter.testnet"))
                .withdrawn,
            1
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_submitter")]
    fn another_account_cannot_withdraw_report() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("stranger.testnet", 1, 1_002);
        reports.withdraw(id);
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_pending")]
    fn submitter_cannot_withdraw_accepted_report() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("reporter.testnet", 1, 1_003);
        reports.withdraw(id);
    }

    #[test]
    #[should_panic(expected = "REFUSED:report_missing")]
    fn submitter_cannot_withdraw_missing_report() {
        let mut reports = reports();
        context("reporter.testnet", 1, 1_002);
        reports.withdraw(42);
    }

    #[test]
    fn dispute_contests_accepted_report_and_updates_summary() {
        let mut reports = reports();
        context("reporter.testnet", REPORT_DEPOSIT, 1_001);
        let id = reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Theft,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/report".to_owned(),
        );
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        let summary = reports.subject_summary(account("vendor.testnet"));
        assert_eq!(summary.accepted, 1);
        assert_eq!(summary.contested, 1);
        assert_eq!(summary.latest_accepted_id, Some(id));
        assert_eq!(summary.latest_deny_id, Some(id));
        assert!(summary.deny);
        assert_eq!(
            reports
                .summary_for(&account("vendor.testnet"))
                .accepted_deny,
            1
        );
        assert_eq!(reports.get_dispute(dispute_id).unwrap().report_id, id);
        assert!(reports.get_dispute(dispute_id).unwrap().open);
        assert_eq!(reports.report_or_refuse(id).upheld_disputes, 0);
        assert_eq!(reports.totals().disputes, 1);
    }

    #[test]
    #[should_panic(expected = "REFUSED:dispute_open")]
    fn second_open_dispute_is_refused() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("another.testnet", REPORT_DEPOSIT, 1_004);
        reports.dispute(id, hash('d'), "https://evidence.example/another".to_owned());
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_deposit")]
    fn dispute_requires_the_pinned_deposit() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT - 1, 1_003);
        reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_deposit")]
    fn dispute_rejects_a_deposit_above_the_exact_amount() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT + 1, 1_003);
        reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_accepted")]
    fn pending_report_cannot_be_disputed() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
    }

    #[test]
    fn all_disallowed_report_state_transitions_are_refused() {
        for state in [
            ReportState::Accepted,
            ReportState::Rejected,
            ReportState::Withdrawn,
            ReportState::Contested,
            ReportState::Overturned,
        ] {
            let (mut reports, id) = report_in_state(state);
            context("moderator.testnet", 1, 1_010);
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                reports.moderate(id, true, hash('e'));
            }))
            .is_err());

            let (mut reports, id) = report_in_state(state);
            context("reporter.testnet", 1, 1_010);
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                reports.withdraw(id);
            }))
            .is_err());

            if state != ReportState::Accepted {
                let (mut reports, id) = report_in_state(state);
                context("disputer.testnet", REPORT_DEPOSIT, 1_010);
                assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    reports.dispute(id, hash('f'), "https://evidence.example/dispute".to_owned());
                }))
                .is_err());
            }
        }
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_hash")]
    fn dispute_evidence_hash_is_validated() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        reports.dispute(
            id,
            "z".repeat(64),
            "https://evidence.example/dispute".to_owned(),
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_evidence_url")]
    fn dispute_evidence_url_is_validated() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        reports.dispute(id, hash('c'), "http://evidence.example/dispute".to_owned());
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_evidence_url")]
    fn dispute_evidence_url_over_512_bytes_is_refused() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        reports.dispute(
            id,
            hash('c'),
            format!("https://{}", "x".repeat(MAX_EVIDENCE_URL_BYTES - 7)),
        );
    }

    #[test]
    fn upheld_disputes_never_cap_further_disputes() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        for index in 0..7 {
            context(
                "disputer.testnet",
                REPORT_DEPOSIT,
                1_003 + index as u64 * 2,
            );
            let dispute_id =
                reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
            context("moderator.testnet", 1, 1_004 + index as u64 * 2);
            reports.resolve_dispute(dispute_id, false, hash('b'));
        }
        assert_eq!(reports.get_report(id).unwrap().dispute_ids.len(), 7);
        assert_eq!(reports.report_or_refuse(id).upheld_disputes, 7);

        context("another.testnet", REPORT_DEPOSIT, 1_017);
        let eighth_dispute =
            reports.dispute(id, hash('d'), "https://evidence.example/eighth".to_owned());
        assert_eq!(eighth_dispute, 7);
    }

    #[test]
    fn upheld_counter_saturates_instead_of_panicking() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        let mut report = reports.report_or_refuse(id);
        report.upheld_disputes = u8::MAX;
        reports.reports.insert(id, report);

        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("moderator.testnet", 1, 1_004);
        reports.resolve_dispute(dispute_id, false, hash('d'));

        let report = reports.report_or_refuse(id);
        assert_eq!(report.state, ReportState::Accepted);
        assert_eq!(report.upheld_disputes, u8::MAX);
    }

    #[test]
    #[should_panic(expected = "REFUSED:dispute_open")]
    fn open_dispute_check_reads_the_last_dispute() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);

        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let first_dispute =
            reports.dispute(id, hash('c'), "https://evidence.example/first".to_owned());
        context("moderator.testnet", 1, 1_004);
        reports.resolve_dispute(first_dispute, false, hash('d'));

        context("another.testnet", REPORT_DEPOSIT, 1_005);
        reports.dispute(id, hash('e'), "https://evidence.example/second".to_owned());
        context("third.testnet", REPORT_DEPOSIT, 1_006);
        reports.dispute(id, hash('f'), "https://evidence.example/third".to_owned());
    }

    #[test]
    fn moderator_can_overturn_dispute() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("moderator.testnet", 1, 1_004);
        reports.resolve_dispute(dispute_id, true, hash('d'));
        let report = reports.get_report(id).unwrap();
        assert_eq!(report.state, ReportState::Overturned);
        assert_eq!(reports.report_or_refuse(id).upheld_disputes, 0);
        assert_eq!(report.moderator, Some(account("moderator.testnet")));
        assert_eq!(
            reports.subject_summary(account("vendor.testnet")).contested,
            0
        );
        assert_eq!(
            reports.subject_summary(account("vendor.testnet")).accepted,
            0
        );
        assert_eq!(
            reports
                .subject_summary(account("vendor.testnet"))
                .latest_accepted_id,
            None
        );
        assert_eq!(
            reports
                .subject_summary(account("vendor.testnet"))
                .latest_deny_id,
            None
        );
        assert!(!reports.subject_summary(account("vendor.testnet")).deny);
        assert_eq!(
            reports
                .summary_for(&account("vendor.testnet"))
                .accepted_deny,
            0
        );
        assert_eq!(
            reports
                .publisher_stats(account("reporter.testnet"))
                .overturned,
            1
        );
        assert!(!reports.get_dispute(dispute_id).unwrap().open);
    }

    #[test]
    fn same_moderator_can_resolve_when_the_moderator_list_has_one_member() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("moderator.testnet", 1, 1_004);
        reports.resolve_dispute(dispute_id, false, hash('d'));
        let summary = reports.subject_summary(account("vendor.testnet"));
        assert_eq!(summary.contested, 0);
        assert_eq!(summary.accepted, 1);
        assert_eq!(summary.latest_accepted_id, Some(id));
        assert!(summary.deny);
        assert_eq!(
            reports
                .summary_for(&account("vendor.testnet"))
                .accepted_deny,
            1
        );
        assert_eq!(
            reports
                .publisher_stats(account("reporter.testnet"))
                .accepted,
            1
        );
    }

    #[test]
    #[should_panic(expected = "REFUSED:same_moderator")]
    fn accept_voter_cannot_resolve_when_not_the_deciding_voter() {
        let mut reports = reports();
        configure_threshold(
            &mut reports,
            &[
                "moderator-a.testnet",
                "moderator-b.testnet",
                "resolver.testnet",
            ],
            2,
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        cast_vote(&mut reports, id, "moderator-a.testnet", true, 'b', 1_002);
        cast_vote(&mut reports, id, "moderator-b.testnet", true, 'c', 1_003);
        context("disputer.testnet", REPORT_DEPOSIT, 1_004);
        let dispute_id =
            reports.dispute(id, hash('d'), "https://evidence.example/dispute".to_owned());
        context("moderator-a.testnet", 1, 1_005);
        reports.resolve_dispute(dispute_id, true, hash('e'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:same_moderator")]
    fn accepting_moderator_cannot_resolve_with_multiple_moderators() {
        context("reporter.testnet", 1, 1_000);
        let mut reports = Reports::new(
            account("owner.testnet"),
            vec![account("moderator.testnet"), account("second.testnet")],
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("moderator.testnet", 1, 1_004);
        reports.resolve_dispute(dispute_id, false, hash('d'));
    }

    #[test]
    fn missing_accepting_moderator_uses_report_moderator_fallback_without_upheld_disputes() {
        context("owner.testnet", 0, 1_000);
        let mut reports = Reports::new(
            account("owner.testnet"),
            vec![
                account("moderator-a.testnet"),
                account("moderator-b.testnet"),
            ],
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("moderator-a.testnet", 1, 1_002);
        reports.moderate(id, true, hash('b'));
        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Accepted);
        assert_eq!(reports.report_or_refuse(id).upheld_disputes, 0);
        assert_eq!(
            reports.report_or_refuse(id).moderator,
            Some(account("moderator-a.testnet"))
        );
        assert_eq!(
            LookupMap::<u64, AccountId>::new(ACCEPTING_MODERATOR_PREFIX).remove(&id),
            Some(account("moderator-a.testnet"))
        );

        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("moderator-a.testnet", 1, 1_004);
        let refusal = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            reports.resolve_dispute(dispute_id, false, hash('d'));
        }))
        .unwrap_err();
        let reason = refusal
            .downcast_ref::<String>()
            .map(String::as_str)
            .or_else(|| refusal.downcast_ref::<&str>().copied());
        assert!(reason.is_some_and(|reason| reason.contains("REFUSED:same_moderator")));

        context("moderator-b.testnet", 1, 1_005);
        reports.resolve_dispute(dispute_id, false, hash('d'));
        assert_eq!(reports.get_report(id).unwrap().state, ReportState::Accepted);
        assert!(!reports.get_dispute(dispute_id).unwrap().open);
    }

    #[test]
    fn legacy_report_fallback_is_migrated_before_moderator_changes() {
        context("owner.testnet", 0, 1_000);
        let mut reports = Reports::new(
            account("owner.testnet"),
            vec![account("moderator-a.testnet"), account("moderator-b.testnet")],
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("moderator-a.testnet", 1, 1_002);
        reports.moderate(id, true, hash('b'));
        assert_eq!(
            LookupMap::<u64, AccountId>::new(ACCEPTING_MODERATOR_PREFIX).remove(&id),
            Some(account("moderator-a.testnet"))
        );
        assert_eq!(reports.report_or_refuse(id).upheld_disputes, 0);

        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let first_dispute =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("moderator-b.testnet", 1, 1_004);
        reports.resolve_dispute(first_dispute, false, hash('d'));

        assert_eq!(
            LookupMap::<u64, AccountId>::new(ACCEPTING_MODERATOR_PREFIX)
                .get(&id)
                .cloned(),
            Some(account("moderator-a.testnet"))
        );
        assert_eq!(reports.report_or_refuse(id).upheld_disputes, 1);
        assert_eq!(
            reports.get_report(id).unwrap().moderator,
            Some(account("moderator-b.testnet"))
        );

        context("disputer.testnet", REPORT_DEPOSIT, 1_005);
        let second_dispute = reports.dispute(
            id,
            hash('e'),
            "https://evidence.example/dispute-2".to_owned(),
        );
        context("moderator-a.testnet", 1, 1_006);
        let refusal = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            reports.resolve_dispute(second_dispute, false, hash('f'));
        }))
        .unwrap_err();
        let reason = refusal
            .downcast_ref::<String>()
            .map(String::as_str)
            .or_else(|| refusal.downcast_ref::<&str>().copied());
        assert!(reason.is_some_and(|reason| reason.contains("REFUSED:same_moderator")));
    }

    #[test]
    fn accepting_moderator_cannot_resolve_after_an_upheld_dispute() {
        context("owner.testnet", 0, 1_000);
        let mut reports = Reports::new(
            account("owner.testnet"),
            vec![account("moderator-a.testnet"), account("moderator-b.testnet")],
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("moderator-a.testnet", 1, 1_002);
        reports.moderate(id, true, hash('b'));
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let first_dispute =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("moderator-b.testnet", 1, 1_004);
        reports.resolve_dispute(first_dispute, false, hash('d'));
        context("disputer.testnet", REPORT_DEPOSIT, 1_005);
        let second_dispute =
            reports.dispute(id, hash('e'), "https://evidence.example/dispute-2".to_owned());
        context("moderator-a.testnet", 1, 1_006);
        let refusal = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            reports.resolve_dispute(second_dispute, false, hash('f'));
        }))
        .unwrap_err();
        let reason = refusal
            .downcast_ref::<String>()
            .map(String::as_str)
            .or_else(|| refusal.downcast_ref::<&str>().copied());
        assert!(reason.is_some_and(|reason| reason.contains("REFUSED:same_moderator")));
        context("moderator-b.testnet", 1, 1_007);
        reports.resolve_dispute(second_dispute, false, hash('f'));
        assert!(!reports.get_dispute(second_dispute).unwrap().open);
    }

    #[test]
    fn legacy_upheld_report_without_accepting_moderator_refuses_every_resolver() {
        context("owner.testnet", 0, 1_000);
        let mut reports = Reports::new(
            account("owner.testnet"),
            vec![
                account("moderator-a.testnet"),
                account("moderator-b.testnet"),
                account("reporter.testnet"),
                account("disputer.testnet"),
            ],
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        context("moderator-a.testnet", 1, 1_002);
        reports.moderate(id, true, hash('b'));
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let first_dispute =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("moderator-b.testnet", 1, 1_004);
        reports.resolve_dispute(first_dispute, false, hash('d'));
        assert_eq!(reports.report_or_refuse(id).upheld_disputes, 1);
        assert_eq!(
            LookupMap::<u64, AccountId>::new(ACCEPTING_MODERATOR_PREFIX).remove(&id),
            Some(account("moderator-a.testnet"))
        );

        context("disputer.testnet", REPORT_DEPOSIT, 1_005);
        let second_dispute =
            reports.dispute(id, hash('e'), "https://evidence.example/dispute-2".to_owned());
        for resolver in [
            "moderator-a.testnet",
            "moderator-b.testnet",
            "reporter.testnet",
            "disputer.testnet",
        ] {
            context(resolver, 1, 1_006);
            let refusal = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                reports.resolve_dispute(second_dispute, false, hash('f'));
            }))
            .unwrap_err();
            let reason = refusal
                .downcast_ref::<String>()
                .map(String::as_str)
                .or_else(|| refusal.downcast_ref::<&str>().copied());
            assert!(reason.is_some_and(|reason| reason.contains("REFUSED:same_moderator")));
        }

        context("owner.testnet", 1, 1_007);
        reports.set_moderators(vec![account("moderator-b.testnet")]);
        context("moderator-b.testnet", 1, 1_008);
        reports.resolve_dispute(second_dispute, false, hash('f'));
        assert!(!reports.get_dispute(second_dispute).unwrap().open);
    }

    #[test]
    #[should_panic(expected = "REFUSED:self_moderation")]
    fn report_subject_cannot_resolve_own_dispute_with_one_moderator() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("owner.testnet", 1, 1_003);
        reports.set_moderators(vec![account("vendor.testnet")]);
        context("disputer.testnet", REPORT_DEPOSIT, 1_004);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("vendor.testnet", 1, 1_005);
        reports.resolve_dispute(dispute_id, false, hash('d'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:self_moderation")]
    fn report_subject_cannot_resolve_own_dispute_with_multiple_moderators() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("owner.testnet", 1, 1_003);
        reports.set_moderators(vec![account("vendor.testnet"), account("reviewer.testnet")]);
        context("disputer.testnet", REPORT_DEPOSIT, 1_004);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("vendor.testnet", 1, 1_005);
        reports.resolve_dispute(dispute_id, false, hash('d'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:self_moderation")]
    fn report_submitter_cannot_resolve_own_dispute() {
        context("owner.testnet", 0, 1_000);
        let mut reports = Reports::new(
            account("owner.testnet"),
            vec![account("moderator.testnet"), account("reporter.testnet")],
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("reporter.testnet", 1, 1_004);
        reports.resolve_dispute(dispute_id, false, hash('d'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:self_moderation")]
    fn dispute_submitter_cannot_resolve_own_dispute() {
        context("owner.testnet", 0, 1_000);
        let mut reports = Reports::new(
            account("owner.testnet"),
            vec![account("moderator.testnet"), account("disputer.testnet")],
        );
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("disputer.testnet", 1, 1_004);
        reports.resolve_dispute(dispute_id, false, hash('d'));
    }

    #[test]
    fn report_submitter_can_resolve_when_the_moderator_list_has_one_member() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("owner.testnet", 1, 1_003);
        reports.set_moderators(vec![account("reporter.testnet")]);
        context("disputer.testnet", REPORT_DEPOSIT, 1_004);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("reporter.testnet", 1, 1_005);
        reports.resolve_dispute(dispute_id, false, hash('d'));
        assert!(!reports.get_dispute(dispute_id).unwrap().open);
    }

    #[test]
    fn dispute_submitter_can_resolve_when_the_moderator_list_has_one_member() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("owner.testnet", 1, 1_004);
        reports.set_moderators(vec![account("disputer.testnet")]);
        context("disputer.testnet", 1, 1_005);
        reports.resolve_dispute(dispute_id, false, hash('d'));
        assert!(!reports.get_dispute(dispute_id).unwrap().open);
    }

    #[test]
    #[should_panic(expected = "REFUSED:not_moderator")]
    fn non_moderator_cannot_resolve_dispute() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("stranger.testnet", 1, 1_004);
        reports.resolve_dispute(dispute_id, true, hash('d'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:dispute_missing")]
    fn moderator_cannot_resolve_missing_dispute() {
        let mut reports = reports();
        context("moderator.testnet", 1, 1_004);
        reports.resolve_dispute(42, true, hash('d'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:dispute_closed")]
    fn closed_dispute_cannot_be_resolved_again() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("moderator.testnet", 1, 1_004);
        reports.resolve_dispute(dispute_id, false, hash('d'));
        reports.resolve_dispute(dispute_id, true, hash('d'));
    }

    #[test]
    #[should_panic(expected = "REFUSED:bad_hash")]
    fn resolution_reason_hash_is_validated() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            reports.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        context("moderator.testnet", 1, 1_004);
        reports.resolve_dispute(dispute_id, true, "not a hash".to_owned());
    }

    #[test]
    fn summary_deny_stays_true_when_one_of_two_deny_reports_is_overturned() {
        let mut reports = reports();
        let first = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, first, true);
        context("reporter.testnet", REPORT_DEPOSIT, 1_003);
        let second = reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Theft,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/report".to_owned(),
        );
        context("moderator.testnet", 1, 1_004);
        reports.moderate(second, true, hash('b'));
        assert_eq!(
            reports
                .subject_summary(account("vendor.testnet"))
                .latest_accepted_id,
            Some(second)
        );
        assert_eq!(
            reports
                .subject_summary(account("vendor.testnet"))
                .latest_deny_id,
            Some(second)
        );
        assert!(reports.subject_summary(account("vendor.testnet")).deny);
        assert_eq!(
            reports
                .summary_for(&account("vendor.testnet"))
                .accepted_deny,
            2
        );
        context("disputer.testnet", REPORT_DEPOSIT, 1_005);
        let dispute_id = reports.dispute(
            second,
            hash('c'),
            "https://evidence.example/dispute".to_owned(),
        );
        assert_eq!(
            reports
                .subject_summary(account("vendor.testnet"))
                .latest_accepted_id,
            Some(second)
        );
        assert_eq!(
            reports
                .subject_summary(account("vendor.testnet"))
                .latest_deny_id,
            Some(second)
        );
        assert!(reports.subject_summary(account("vendor.testnet")).deny);
        context("moderator.testnet", 1, 1_006);
        reports.resolve_dispute(dispute_id, true, hash('d'));
        assert_eq!(
            reports
                .subject_summary(account("vendor.testnet"))
                .latest_accepted_id,
            Some(first)
        );
        assert_eq!(
            reports
                .subject_summary(account("vendor.testnet"))
                .latest_deny_id,
            Some(first)
        );
        assert!(reports.subject_summary(account("vendor.testnet")).deny);
        assert_eq!(
            reports
                .summary_for(&account("vendor.testnet"))
                .accepted_deny,
            1
        );
    }

    #[test]
    fn overturning_middle_deny_report_relinks_deny_chain_without_changing_latest() {
        let mut reports = reports();
        let first = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, first, true);
        context("reporter.testnet", REPORT_DEPOSIT, 1_003);
        let middle = reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Theft,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/middle".to_owned(),
        );
        context("moderator.testnet", 1, 1_004);
        reports.moderate(middle, true, hash('b'));
        context("reporter.testnet", REPORT_DEPOSIT, 1_005);
        let newest = reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Phishing,
            ReportOutcome::Loss,
            hash('c'),
            "https://evidence.example/newest".to_owned(),
        );
        context("moderator.testnet", 1, 1_006);
        reports.moderate(newest, true, hash('d'));
        context("disputer.testnet", REPORT_DEPOSIT, 1_007);
        let dispute_id = reports.dispute(
            middle,
            hash('e'),
            "https://evidence.example/dispute".to_owned(),
        );
        context("moderator.testnet", 1, 1_008);
        reports.resolve_dispute(dispute_id, true, hash('f'));

        assert_eq!(
            reports
                .subject_summary(account("vendor.testnet"))
                .latest_deny_id,
            Some(newest)
        );
        let first_report = reports.report_or_refuse(first);
        let middle_report = reports.report_or_refuse(middle);
        let newest_report = reports.report_or_refuse(newest);
        assert_eq!(first_report.deny_next, Some(newest));
        assert_eq!(middle_report.deny_prev, None);
        assert_eq!(middle_report.deny_next, None);
        assert_eq!(newest_report.deny_prev, Some(first));
    }

    #[test]
    fn overturning_a_social_report_leaves_the_deny_chain_intact() {
        let mut reports = reports();
        context("reporter.testnet", REPORT_DEPOSIT, 1_001);
        let theft = reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Theft,
            ReportOutcome::Loss,
            hash('a'),
            "https://evidence.example/theft".to_owned(),
        );
        context("moderator.testnet", 1, 1_002);
        reports.moderate(theft, true, hash('b'));
        context("reporter.testnet", REPORT_DEPOSIT, 1_003);
        let social = reports.submit_report(
            "vendor.testnet".to_owned(),
            ReportKind::Social,
            ReportOutcome::Loss,
            hash('c'),
            "https://evidence.example/social".to_owned(),
        );
        context("moderator.testnet", 1, 1_004);
        reports.moderate(social, true, hash('d'));
        context("disputer.testnet", REPORT_DEPOSIT, 1_005);
        let dispute_id = reports.dispute(
            social,
            hash('e'),
            "https://evidence.example/dispute".to_owned(),
        );
        context("moderator.testnet", 1, 1_006);
        reports.resolve_dispute(dispute_id, true, hash('f'));

        let summary = reports.subject_summary(account("vendor.testnet"));
        assert_eq!(summary.accepted, 1);
        assert_eq!(summary.latest_accepted_id, Some(theft));
        assert_eq!(summary.latest_deny_id, Some(theft));
        assert!(summary.deny);
        let theft_report = reports.report_or_refuse(theft);
        assert_eq!(theft_report.deny_prev, None);
        assert_eq!(theft_report.deny_next, None);
        let social_report = reports.report_or_refuse(social);
        assert_eq!(social_report.deny_prev, None);
        assert_eq!(social_report.deny_next, None);
    }

    #[test]
    fn views_page_by_subject_and_submitter() {
        let mut reports = reports();
        let first = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        submit(&mut reports, "reporter.testnet", "other.testnet");
        let third = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        let page = reports.reports_by_subject(account("vendor.testnet"), 1, 1);
        assert_eq!(page.len(), 1);
        assert_eq!(page[0].id, third);
        let page = reports.reports_by_submitter(account("reporter.testnet"), 1, 1);
        assert_eq!(page.len(), 1);
        assert_eq!(page[0].id, 1);
        assert_eq!(reports.get_report(first).unwrap().id, first);
        assert_eq!(reports.totals().reports, 3);
        assert_eq!(reports.totals().disputes, 0);
    }

    #[test]
    fn record_views_return_none_when_ids_are_missing() {
        let reports = reports();
        assert!(reports.get_report(42).is_none());
        assert!(reports.get_dispute(42).is_none());
    }

    #[test]
    fn accepting_moderator_uses_registered_ab_storage_key() {
        let mut reports = reports();
        let id = submit(&mut reports, "reporter.testnet", "vendor.testnet");
        moderate(&mut reports, id, true);

        let mut expected_key = b"ab".to_vec();
        expected_key.extend(id.to_le_bytes());
        assert!(env::storage_has_key(&expected_key));
    }

    #[test]
    #[should_panic(expected = "REFUSED:limit_too_large")]
    fn page_limit_is_capped_at_fifty() {
        reports().reports_by_subject(account("vendor.testnet"), 0, 51);
    }

    #[test]
    fn emits_all_five_nep_297_events_with_report_identity() {
        context("owner.testnet", 0, 1_000);
        let mut contract = Reports::new(
            account("owner.testnet"),
            vec![account("moderator.testnet"), account("resolver.testnet")],
        );
        let id = submit(&mut contract, "reporter.testnet", "vendor.testnet");
        let submitted = get_logs();
        let moderation_reason_sha256 = hash('b');
        context("moderator.testnet", 1, 1_002);
        contract.moderate(id, true, moderation_reason_sha256.clone());
        let moderated = get_logs();
        context("disputer.testnet", REPORT_DEPOSIT, 1_003);
        let dispute_id =
            contract.dispute(id, hash('c'), "https://evidence.example/dispute".to_owned());
        let disputed = get_logs();
        let body_sha256 = contract.get_report(id).unwrap().body_sha256;
        let resolution_reason_sha256 = hash('d');
        context("resolver.testnet", 1, 1_004);
        contract.resolve_dispute(dispute_id, true, resolution_reason_sha256.clone());
        let resolved = get_logs();
        let mut reports = reports();
        let withdraw_id = submit(&mut reports, "reporter.testnet", "withdraw.testnet");
        let withdrawn_body_sha256 = reports.get_report(withdraw_id).unwrap().body_sha256;
        context("reporter.testnet", 1, 1_002);
        reports.withdraw(withdraw_id);
        let withdrawn = get_logs();
        for (logs, event, state, subject, expected_body) in [
            (
                &submitted,
                "report_submitted",
                "pending",
                "vendor.testnet",
                &body_sha256,
            ),
            (
                &moderated,
                "report_moderated",
                "accepted",
                "vendor.testnet",
                &body_sha256,
            ),
            (
                &withdrawn,
                "report_withdrawn",
                "withdrawn",
                "withdraw.testnet",
                &withdrawn_body_sha256,
            ),
            (
                &disputed,
                "report_disputed",
                "contested",
                "vendor.testnet",
                &body_sha256,
            ),
            (
                &resolved,
                "dispute_resolved",
                "overturned",
                "vendor.testnet",
                &body_sha256,
            ),
        ] {
            let line = logs
                .iter()
                .find(|line| line.contains(&format!("\"event\":\"{event}\"")))
                .unwrap();
            let value: Value =
                near_sdk::serde_json::from_str(line.strip_prefix("EVENT_JSON:").unwrap()).unwrap();
            let data = &value["data"][0];
            assert_eq!(value["standard"], "shieldbot-near");
            assert_eq!(value["version"], "1.1.0");
            assert_eq!(data["report_id"], id);
            assert_eq!(data["subject"], subject);
            assert_eq!(data["state"], state);
            assert_eq!(data["body_sha256"].as_str(), Some(expected_body.as_str()));
            if event == "report_disputed" || event == "dispute_resolved" {
                assert_eq!(data["dispute_id"], dispute_id);
            }
            if event == "report_moderated" {
                assert_eq!(data["moderator"], "moderator.testnet");
                assert_eq!(
                    data["reason_sha256"].as_str(),
                    Some(moderation_reason_sha256.as_str())
                );
            }
            if event == "dispute_resolved" {
                assert_eq!(data["moderator"], "resolver.testnet");
                assert_eq!(
                    data["reason_sha256"].as_str(),
                    Some(resolution_reason_sha256.as_str())
                );
            }
        }
    }

    #[test]
    fn report_and_dispute_storage_measurement_is_pinned() {
        let mut reports = reports();
        let subject = "a".repeat(64);
        let submitter = "b".repeat(64);
        let moderator = "m".repeat(64);
        context("owner.testnet", 1, 999);
        reports.set_moderators(vec![account(&moderator)]);
        context(&submitter, REPORT_DEPOSIT, 1_000);
        let before_report = env::storage_usage();
        let report_id = reports.submit_report(
            subject.clone(),
            ReportKind::Scam,
            ReportOutcome::Loss,
            hash('a'),
            format!("https://{}", "x".repeat(MAX_EVIDENCE_URL_BYTES - 8)),
        );
        flush_storage(&mut reports);
        let submitted_usage = env::storage_usage();
        context_with_storage(&moderator, 1, 1_001, submitted_usage);
        reports.moderate(report_id, true, hash('c'));
        flush_storage(&mut reports);
        let report_bytes = env::storage_usage() - before_report;
        assert!(reports.report_votes(report_id).is_empty());

        let disputer = "c".repeat(64);
        let before_dispute = env::storage_usage();
        context_with_storage(&disputer, REPORT_DEPOSIT, 1_002, before_dispute);
        let dispute_id = reports.dispute(
            report_id,
            hash('d'),
            format!("https://{}", "y".repeat(MAX_EVIDENCE_URL_BYTES - 8)),
        );
        flush_storage(&mut reports);
        let dispute_bytes = env::storage_usage() - before_dispute;
        assert_eq!(dispute_id, 0);
        let measured_bytes = report_bytes.max(dispute_bytes);
        let byte_cost = env::storage_byte_cost().as_yoctonear();
        let measured_storage_cost = u128::from(measured_bytes) * byte_cost;
        assert_eq!((report_bytes, dispute_bytes), (2_046, 734));
        assert!(measured_storage_cost * 120 / 100 <= REPORT_DEPOSIT);
        assert!(REPORT_DEPOSIT <= measured_storage_cost * 2);
        assert_eq!(REPORT_DEPOSIT, 30_000_000_000_000_000_000_000);
    }

    #[test]
    fn one_threshold_vote_storage_measurement_is_pinned() {
        let moderator = "m".repeat(64);
        let second_moderator = "n".repeat(64);
        context("owner.testnet", 0, 999);
        let mut reports = Reports::new(
            account("owner.testnet"),
            vec![account(&moderator), account(&second_moderator)],
        );
        context("owner.testnet", 1, 1_000);
        reports.set_approvals_required(2);

        let subject = "a".repeat(64);
        let submitter = "b".repeat(64);
        context(&submitter, REPORT_DEPOSIT, 1_001);
        let report_id = reports.submit_report(
            subject,
            ReportKind::Scam,
            ReportOutcome::Loss,
            hash('a'),
            format!("https://{}", "x".repeat(MAX_EVIDENCE_URL_BYTES - 8)),
        );
        flush_storage(&mut reports);
        let before_vote = env::storage_usage();
        context_with_storage(&moderator, 1, 1_002, before_vote);
        reports.moderate(report_id, true, hash('c'));
        flush_storage(&mut reports);

        assert_eq!(reports.report_votes(report_id).len(), 1);
        assert_eq!(env::storage_usage() - before_vote, 151);
    }
}
