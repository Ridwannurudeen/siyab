#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID NEAR_KEYS_DIR
if [[ -z "${REPORTER_ACCOUNT_ID+x}" ]]; then
  REPORTER_ACCOUNT_ID=$OWNER_ACCOUNT_ID
  printf 'the owner cannot moderate a report it filed\n' >&2
fi
if [[ "${#SCRIPT_ARGS[@]}" -ne 6 ]]; then
  printf 'usage: report.sh <reports_account> <subject> <kind> <outcome> <evidence_sha256> <evidence_url> [--dry-run]\n' >&2
  exit 2
fi
reports_account=${SCRIPT_ARGS[0]}
subject=${SCRIPT_ARGS[1]}
kind=${SCRIPT_ARGS[2]}
outcome=${SCRIPT_ARGS[3]}
evidence_sha256=${SCRIPT_ARGS[4]}
evidence_url=${SCRIPT_ARGS[5]}
require_account_id "$OWNER_ACCOUNT_ID" "owner account"
require_account_id "$REPORTER_ACCOUNT_ID" "reporter account"
require_account_id "$reports_account" "reports account"
require_account_id "$subject" "subject"
require_sha256 "$evidence_sha256" "evidence hash"
require_https_url "$evidence_url" "evidence URL"
if ! python3 - "$ROOT_DIR/intel/record_enums.json" "$kind" "$outcome" <<'PY'
import json
import sys
from pathlib import Path

enums = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
raise SystemExit(0 if sys.argv[2] in enums["kind"] and sys.argv[3] in enums["outcome"] else 1)
PY
then
  printf 'invalid report kind or outcome; see intel/record_enums.json\n' >&2
  exit 2
fi
ARGS=$(python3 - "$subject" "$kind" "$outcome" "$evidence_sha256" "$evidence_url" <<'PY'
import json
import sys

print(json.dumps({
    "subject": sys.argv[1],
    "kind": sys.argv[2],
    "outcome": sys.argv[3],
    "evidence_sha256": sys.argv[4],
    "evidence_url": sys.argv[5],
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
call_transaction "$reports_account" submit_report "$ARGS" "$REPORTER_ACCOUNT_ID" "$(account_key "$REPORTER_ACCOUNT_ID")" "$DEPOSIT"
summary_block "$reports_account"
