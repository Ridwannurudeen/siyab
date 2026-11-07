#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -lt 1 ]]; then
  printf 'usage: dispute.sh <reports_account> <report_id> <evidence_sha256> <evidence_url> [--dry-run]\n       dispute.sh <reports_account> --resolve <dispute_id> <uphold|overturn> <reason_sha256> [--dry-run]\n' >&2
  exit 2
fi
reports_account=${SCRIPT_ARGS[0]}
require_account_id "$OWNER_ACCOUNT_ID" "owner account"
require_account_id "$reports_account" "reports account"
if [[ "${SCRIPT_ARGS[1]:-}" == "--resolve" ]]; then
  if [[ "${#SCRIPT_ARGS[@]}" -ne 5 ]]; then
    printf 'usage: dispute.sh <reports_account> --resolve <dispute_id> <uphold|overturn> <reason_sha256> [--dry-run]\n' >&2
    exit 2
  fi
  dispute_id=${SCRIPT_ARGS[2]}
  resolution=${SCRIPT_ARGS[3]}
  reason_sha256=${SCRIPT_ARGS[4]}
  require_u64 "$dispute_id" "dispute id"
  if [[ "$resolution" != "uphold" && "$resolution" != "overturn" ]]; then
    printf 'resolution must be uphold or overturn\n' >&2
    exit 2
  fi
  require_sha256 "$reason_sha256" "reason hash"
  ARGS=$(python3 - "$dispute_id" "$resolution" "$reason_sha256" <<'PY'
import json
import sys

print(json.dumps({
    "dispute_id": int(sys.argv[1]),
    "overturn": sys.argv[2] == "overturn",
    "reason_sha256": sys.argv[3],
}, separators=(",", ":")))
PY
  )
  call_transaction "$reports_account" resolve_dispute "$ARGS" "$OWNER_ACCOUNT_ID" "$(account_key "$OWNER_ACCOUNT_ID")" '1 yoctoNEAR'
else
  if [[ "${#SCRIPT_ARGS[@]}" -ne 4 ]]; then
    printf 'usage: dispute.sh <reports_account> <report_id> <evidence_sha256> <evidence_url> [--dry-run]\n' >&2
    exit 2
  fi
  report_id=${SCRIPT_ARGS[1]}
  evidence_sha256=${SCRIPT_ARGS[2]}
  evidence_url=${SCRIPT_ARGS[3]}
  if [[ -z "${DISPUTER_ACCOUNT_ID+x}" ]]; then
    DISPUTER_ACCOUNT_ID=$OWNER_ACCOUNT_ID
    printf 'the owner is the moderator; file disputes from a separate account by setting DISPUTER_ACCOUNT_ID\n' >&2
  fi
  require_account_id "$DISPUTER_ACCOUNT_ID" "disputer account"
  require_u64 "$report_id" "report id"
  require_sha256 "$evidence_sha256" "evidence hash"
  require_https_url "$evidence_url" "evidence URL"
  ARGS=$(python3 - "$report_id" "$evidence_sha256" "$evidence_url" <<'PY'
import json
import sys

print(json.dumps({
    "report_id": int(sys.argv[1]),
    "evidence_sha256": sys.argv[2],
    "evidence_url": sys.argv[3],
}, separators=(",", ":")))
PY
  )
  DEPOSIT=$(python3 - "$ROOT_DIR/contracts/reports/src/lib.rs" <<'PY'
import re
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r"pub const REPORT_DEPOSIT: u128 = ([0-9_]+);", source)
if match is None:
    raise SystemExit("REPORT_DEPOSIT was not found in contracts/reports/src/lib.rs")
print(f"{match.group(1).replace('_', '')} yoctoNEAR")
PY
  )
  call_transaction "$reports_account" dispute "$ARGS" "$DISPUTER_ACCOUNT_ID" "$(account_key "$DISPUTER_ACCOUNT_ID")" "$DEPOSIT"
fi
summary_block "$reports_account"
