#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 4 ]]; then
  printf 'usage: moderate.sh <reports_account> <report_id> <accept|reject> <reason_sha256> [--dry-run]\n' >&2
  exit 2
fi
reports_account=${SCRIPT_ARGS[0]}
report_id=${SCRIPT_ARGS[1]}
action=${SCRIPT_ARGS[2]}
reason_sha256=${SCRIPT_ARGS[3]}
require_account_id "$OWNER_ACCOUNT_ID" "owner account"
require_account_id "$reports_account" "reports account"
require_u64 "$report_id" "report id"
if [[ "$action" != "accept" && "$action" != "reject" ]]; then
  printf 'action must be accept or reject\n' >&2
  exit 2
fi
require_sha256 "$reason_sha256" "reason hash"
ARGS=$(python3 - "$report_id" "$action" "$reason_sha256" <<'PY'
import json
import sys

print(json.dumps({
    "id": int(sys.argv[1]),
    "accept": sys.argv[2] == "accept",
    "reason_sha256": sys.argv[3],
}, separators=(",", ":")))
PY
)
call_transaction "$reports_account" moderate "$ARGS" "$OWNER_ACCOUNT_ID" "$(account_key "$OWNER_ACCOUNT_ID")" '1 yoctoNEAR'
summary_block "$reports_account"
