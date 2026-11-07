#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
export DRY_RUN
require_testnet
resolve_counterparty_account_id
require_vars OWNER_ACCOUNT_ID REPORTER_ACCOUNT_ID DISPUTER_ACCOUNT_ID REPORTS_ACCOUNT_ID GATE_ACCOUNT_ID COUNTERPARTY_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 0 ]]; then
  printf 'usage: live_beat.sh [--dry-run]\n' >&2
  exit 2
fi
require_account_id "$OWNER_ACCOUNT_ID" "owner account"
require_account_id "$REPORTER_ACCOUNT_ID" "reporter account"
require_account_id "$DISPUTER_ACCOUNT_ID" "disputer account"
require_account_id "$REPORTS_ACCOUNT_ID" "reports account"
require_account_id "$GATE_ACCOUNT_ID" "gate account"
require_account_id "$COUNTERPARTY_ACCOUNT_ID" "subject"
if [[ "$REPORTER_ACCOUNT_ID" == "$OWNER_ACCOUNT_ID" ]]; then
  printf 'REPORTER_ACCOUNT_ID must differ from OWNER_ACCOUNT_ID: the owner moderates and cannot moderate its own report\n' >&2
  exit 2
fi
if [[ "$DISPUTER_ACCOUNT_ID" == "$REPORTER_ACCOUNT_ID" || "$DISPUTER_ACCOUNT_ID" == "$OWNER_ACCOUNT_ID" ]]; then
  printf 'DISPUTER_ACCOUNT_ID must differ from REPORTER_ACCOUNT_ID and OWNER_ACCOUNT_ID\n' >&2
  exit 2
fi
SUBJECT=$COUNTERPARTY_ACCOUNT_ID
if [[ "$SUBJECT" == "$OWNER_ACCOUNT_ID" || "$SUBJECT" == "$REPORTER_ACCOUNT_ID" || "$SUBJECT" == "$DISPUTER_ACCOUNT_ID" ]]; then
  printf 'the subject COUNTERPARTY_ACCOUNT_ID must differ from the owner, the reporter and the disputer\n' >&2
  exit 2
fi
if [[ "$GATE_ACCOUNT_ID" != *.testnet ]]; then
  printf 'GATE_ACCOUNT_ID must be a .testnet account: the verdict reads select the network from it\n' >&2
  exit 2
fi

EVIDENCE_PATH=docs/demo/synthetic-report.md
COMMIT=$(git -C "$ROOT_DIR" rev-parse HEAD)
EVIDENCE_URL="https://raw.githubusercontent.com/Ridwannurudeen/shieldbot-near/$COMMIT/$EVIDENCE_PATH"
EVIDENCE_SHA256=$(python3 - "$ROOT_DIR/$EVIDENCE_PATH" <<'PY'
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)
if ! git -C "$ROOT_DIR" ls-files --error-unmatch -- "$EVIDENCE_PATH" >/dev/null 2>&1 \
  || ! git -C "$ROOT_DIR" diff --quiet HEAD -- "$EVIDENCE_PATH"; then
  if [[ "$DRY_RUN" != "1" ]]; then
    printf '%s is not committed unchanged at HEAD, so the evidence URL would not serve these bytes\n' "$EVIDENCE_PATH" >&2
    exit 2
  fi
  printf 'dry run: %s is not committed unchanged at HEAD; the real run refuses until it is\n' "$EVIDENCE_PATH" >&2
fi
MODERATE_REASON="live report beat: accepted to show the deny path on our own demo counterparty account; the evidence page says the report is synthetic"
RESOLVE_REASON="live report beat: overturned because the report is synthetic, as the first line of its evidence page says; nothing happened to anyone"
sha256_text() {
  python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest())' "$1"
}
MODERATE_REASON_SHA256=$(sha256_text "$MODERATE_REASON")
RESOLVE_REASON_SHA256=$(sha256_text "$RESOLVE_REASON")
TOKEN_ACCOUNT_ID="token.$GATE_ACCOUNT_ID"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG="$ROOT_DIR/fixtures/runs/live-beat-$STAMP.txt"
WORK_DIR=$(mktemp -d)

REPORT_ID=none
DISPUTE_ID=none
SENT=no
STEP=start
MISMATCHES=0
finish() {
  local status=$?
  trap - EXIT
  rm -rf -- "$WORK_DIR"
  if [[ "$SENT" == "yes" && "$STEP" != "done" && "$status" -ne 0 ]]; then
    printf 'live beat stopped at step %s with exit %s; report id %s, dispute id %s\n' "$STEP" "$status" "$REPORT_ID" "$DISPUTE_ID" >&2
    printf '  read get_report %s first; the next step depends on its state:\n' "$REPORT_ID" >&2
    printf '  pending: accept it with scripts/moderate.sh, or reject it and stop (a rejected report has no effect)\n' >&2
    printf '  accepted: open a dispute with scripts/dispute.sh as the disputer, then resolve it with scripts/dispute.sh --resolve <dispute_id> overturn <reason_sha256>\n' >&2
    printf '  contested: resolve the open dispute %s with scripts/dispute.sh --resolve %s overturn <reason_sha256>\n' "$DISPUTE_ID" "$DISPUTE_ID" >&2
    printf '  overturned: nothing left to do\n' >&2
  fi
  exit "$status"
}
trap finish EXIT

record() {
  local text
  text=$(python3 -c '
import re, sys
text = sys.stdin.read()
text = re.sub(r"ed25519:[1-9A-HJ-NP-Za-km-z]{80,}", "ed25519:<signature removed>", text)
text = re.sub(r"[0-9a-fA-F]{128}", lambda m: m.group(0)[:8] + "..." + m.group(0)[-8:], text)
sys.stdout.write(text)
' <<< "$1")
  printf '%s\n' "$text"
  if [[ "$DRY_RUN" != "1" ]]; then
    printf '%s\n' "$text" >> "$LOG"
  fi
}

command_line() {
  printf '+ '
  printf '%q ' "$@"
}

mismatch() {
  MISMATCHES=$((MISMATCHES + 1))
  record "MISMATCH: $1"
}

VIEW_VALUE=
view() {
  local method=$1 args=$2
  local -a command=(near contract call-function as-read-only "$REPORTS_ACCOUNT_ID" "$method" json-args "$args" network-config "$NETWORK" now)
  record "$(command_line "${command[@]}")"
  VIEW_VALUE=
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  local output
  if ! output=$("${command[@]}" 2>&1); then
    printf '%s\n' "$output" >&2
    return 1
  fi
  VIEW_VALUE=$(python3 - "$output" <<'PY'
import json
import sys

raw = sys.argv[1]
mark = "Function execution return value (printed to stdout):"
footer = "Here is your console command"
if mark not in raw or footer not in raw:
    print(raw, file=sys.stderr)
    raise SystemExit(3)
text = raw.split(mark, 1)[1].split(footer, 1)[0].strip()
try:
    value = json.loads(text)
except json.JSONDecodeError:
    print(raw, file=sys.stderr)
    raise SystemExit(3)
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
PY
  )
  record "$method: $VIEW_VALUE"
}

field() {
  python3 -c 'import json,sys; value = json.loads(sys.argv[1])[sys.argv[2]]; print(json.dumps(value) if not isinstance(value, str) else value)' "$1" "$2"
}

summary_json() {
  python3 - "$@" <<'PY'
import json
import sys

accepted, pending, contested, latest_accepted, latest_deny, deny = sys.argv[1:]
nullable = lambda value: None if value == "null" else int(value)
print(json.dumps({
    "accepted": int(accepted),
    "contested": int(contested),
    "deny": deny == "true",
    "latest_accepted_id": nullable(latest_accepted),
    "latest_deny_id": nullable(latest_deny),
    "pending": int(pending),
}, sort_keys=True, separators=(",", ":")))
PY
}

read_summary() {
  local expected=$1
  view subject_summary "$(python3 -c 'import json,sys; print(json.dumps({"subject": sys.argv[1]}, separators=(",", ":")))' "$SUBJECT")"
  record "expect subject_summary: $expected"
  if [[ "$DRY_RUN" != "1" && "$VIEW_VALUE" != "$expected" ]]; then
    mismatch "subject_summary is $VIEW_VALUE, expected $expected"
  fi
}

read_report() {
  local state=$1
  view get_report "{\"id\":$REPORT_ID}"
  record "expect get_report $REPORT_ID: state $state, subject $SUBJECT, submitter $REPORTER_ACCOUNT_ID, kind theft, outcome near_miss, evidence_sha256 $EVIDENCE_SHA256"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  if [[ "$VIEW_VALUE" == "null" ]]; then
    mismatch "get_report $REPORT_ID is null"
    return 0
  fi
  local name want got
  for name in state subject submitter kind outcome evidence_sha256 evidence_url; do
    case $name in
      state) want=$state ;;
      subject) want=$SUBJECT ;;
      submitter) want=$REPORTER_ACCOUNT_ID ;;
      kind) want=theft ;;
      outcome) want=near_miss ;;
      evidence_sha256) want=$EVIDENCE_SHA256 ;;
      evidence_url) want=$EVIDENCE_URL ;;
    esac
    got=$(field "$VIEW_VALUE" "$name")
    if [[ "$got" != "$want" ]]; then
      mismatch "get_report $REPORT_ID $name is $got, expected $want"
    fi
  done
}

read_totals() {
  view totals '{}'
  if [[ "$DRY_RUN" == "1" ]]; then
    TOTAL_REPORTS=0
    TOTAL_DISPUTES=0
  else
    TOTAL_REPORTS=$(field "$VIEW_VALUE" reports)
    TOTAL_DISPUTES=$(field "$VIEW_VALUE" disputes)
  fi
}

read_verdict() {
  local expect_line=$1 expect_outcome=${2:-}
  local -a command=(env GATE_FIXTURES_MODE=off "REPORTS_ACCOUNT_ID=$REPORTS_ACCOUNT_ID" python3 -m gate verdict --kind ft --token "$TOKEN_ACCOUNT_ID" --receiver "$SUBJECT" --amount 1000000 --gate "$GATE_ACCOUNT_ID" --evidence-dir "$WORK_DIR" --out "$WORK_DIR")
  record "$(command_line "${command[@]}")"
  record "expect: $expect_line"
  if [[ -n "$expect_outcome" ]]; then
    record "expect: verdict: $expect_outcome"
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  local output
  if ! output=$(cd -- "$ROOT_DIR" && "${command[@]}" 2>&1); then
    record "$output"
    return 1
  fi
  record "$output"
  if ! grep -qF -- "$expect_line" <<< "$output"; then
    mismatch "the verdict has no line containing: $expect_line"
  fi
  if [[ -n "$expect_outcome" ]] && ! grep -qxF -- "verdict: $expect_outcome" <<< "$output"; then
    mismatch "the verdict outcome is not $expect_outcome"
  fi
}

transaction() {
  local output
  record "$(command_line bash "$@")"
  SENT=yes
  if ! output=$(bash "$@" 2>&1); then
    record "$output"
    return 1
  fi
  record "$output"
}

community() {
  printf 'community_report: reports for %s: %s' "$SUBJECT" "$1"
}

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'dry run: nothing is sent and no run log is written; the real run appends every read to %s\n' "${LOG#"$ROOT_DIR"/}"
  printf 'dry run: the report and dispute ids are shown as 0; the real run reads them from totals\n'
else
  mkdir -p -- "$ROOT_DIR/fixtures/runs"
fi
record "# live report beat on $NETWORK, $STAMP; transaction output with ed25519 signature text removed and 128 hex strings shortened to first and last 8; every other line as captured"
record "commit: $COMMIT"
record "reports: $REPORTS_ACCOUNT_ID; owner and moderator: $OWNER_ACCOUNT_ID; reporter: $REPORTER_ACCOUNT_ID; disputer: $DISPUTER_ACCOUNT_ID; subject: $SUBJECT"
record "evidence_url: $EVIDENCE_URL"
record "evidence_sha256 (sha256 of $EVIDENCE_PATH): $EVIDENCE_SHA256"
record "moderation reason: $MODERATE_REASON"
record "moderation reason_sha256: $MODERATE_REASON_SHA256"
record "resolution reason: $RESOLVE_REASON"
record "resolution reason_sha256: $RESOLVE_REASON_SHA256"

ZERO_SUMMARY=$(summary_json 0 0 0 null null false)

STEP=a
record ""
record "## a. before the report"
record "+ fetch $EVIDENCE_URL and compare its sha256 with $EVIDENCE_SHA256"
if [[ "$DRY_RUN" != "1" ]]; then
  if ! python3 - "$EVIDENCE_URL" "$EVIDENCE_SHA256" <<'PY'
import hashlib
import sys
import urllib.request

url, expected = sys.argv[1:]
with urllib.request.urlopen(url, timeout=30) as response:
    body = response.read()
actual = hashlib.sha256(body).hexdigest()
if actual != expected:
    print(f"the evidence URL serves sha256 {actual}, expected {expected}", file=sys.stderr)
    raise SystemExit(1)
PY
  then
    printf 'the evidence URL does not serve the committed page; push the commit first\n' >&2
    exit 2
  fi
  record "evidence page matches"
fi
read_summary "$ZERO_SUMMARY"
if [[ "$MISMATCHES" -ne 0 ]]; then
  printf 'the subject already has reports; nothing was sent\n' >&2
  exit 2
fi
read_verdict "$(community 'no community report; pending 0')"
if [[ "$MISMATCHES" -ne 0 ]]; then
  printf 'the verdict does not read the reports contract on this commit; nothing was sent\n' >&2
  exit 2
fi
read_totals
REPORT_ID=$TOTAL_REPORTS
REPORTS_BEFORE=$TOTAL_REPORTS
DISPUTES_BEFORE=$TOTAL_DISPUTES

STEP=b
record ""
record "## b. file the report"
transaction "$ROOT_DIR/scripts/report.sh" "$REPORTS_ACCOUNT_ID" "$SUBJECT" theft near_miss "$EVIDENCE_SHA256" "$EVIDENCE_URL"
read_totals
if [[ "$DRY_RUN" != "1" && "$TOTAL_REPORTS" -ne $((REPORTS_BEFORE + 1)) ]]; then
  printf 'totals went from %s to %s reports, not one more; the report id is not known\n' "$REPORTS_BEFORE" "$TOTAL_REPORTS" >&2
  exit 1
fi
record "report id: $REPORT_ID (totals.reports before the report)"
read_report pending

STEP=c
record ""
record "## c. pending"
read_summary "$(summary_json 0 1 0 null null false)"
read_verdict "$(community '1 pending report(s) await moderation; no effect')"

STEP=d
record ""
record "## d. accept"
transaction "$ROOT_DIR/scripts/moderate.sh" "$REPORTS_ACCOUNT_ID" "$REPORT_ID" accept "$MODERATE_REASON_SHA256"
read_report accepted

STEP=e
record ""
record "## e. accepted"
read_summary "$(summary_json 1 0 0 "$REPORT_ID" "$REPORT_ID" true)"
read_verdict "$(community "deny report $REPORT_ID is accepted; 0 under dispute; pending 0")" DENY

STEP=f
record ""
record "## f. dispute"
DISPUTE_ID=$DISPUTES_BEFORE
transaction "$ROOT_DIR/scripts/dispute.sh" "$REPORTS_ACCOUNT_ID" "$REPORT_ID" "$EVIDENCE_SHA256" "$EVIDENCE_URL"
read_totals
if [[ "$DRY_RUN" != "1" && "$TOTAL_DISPUTES" -ne $((DISPUTES_BEFORE + 1)) ]]; then
  printf 'totals went from %s to %s disputes, not one more; the dispute id is not known\n' "$DISPUTES_BEFORE" "$TOTAL_DISPUTES" >&2
  exit 1
fi
record "dispute id: $DISPUTE_ID (totals.disputes before the dispute)"
read_report contested
read_summary "$(summary_json 1 0 1 "$REPORT_ID" "$REPORT_ID" true)"
read_verdict "$(community "deny report $REPORT_ID is accepted; 1 under dispute; pending 0")" DENY

STEP=g
record ""
record "## g. overturn"
transaction "$ROOT_DIR/scripts/dispute.sh" "$REPORTS_ACCOUNT_ID" --resolve "$DISPUTE_ID" overturn "$RESOLVE_REASON_SHA256"
read_report overturned
read_summary "$ZERO_SUMMARY"
read_verdict "$(community 'no community report; pending 0')"

STEP=done
record ""
if [[ "$MISMATCHES" -ne 0 ]]; then
  record "result: $MISMATCHES read back(s) did not match"
  printf 'run log: %s\n' "${LOG#"$ROOT_DIR"/}"
  exit 1
fi
if [[ "$DRY_RUN" == "1" ]]; then
  printf 'dry run complete: nothing was sent\n'
else
  record "result: every read back matched"
  printf 'run log: %s\n' "${LOG#"$ROOT_DIR"/}"
fi
