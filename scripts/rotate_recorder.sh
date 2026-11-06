#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID GATE_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 1 ]]; then
  printf 'usage: rotate_recorder.sh <new_recorder_account> [--dry-run]\n' >&2
  exit 2
fi
require_account_id "$OWNER_ACCOUNT_ID" "owner account"
require_account_id "$GATE_ACCOUNT_ID" "Gate account"
new_recorder=${SCRIPT_ARGS[0]}
registry_account="registry.$GATE_ACCOUNT_ID"
require_account_id "$registry_account" "registry account"
require_account_id "$new_recorder" "new recorder account"
ARGS=$(python3 - "$new_recorder" <<'PY'
import json
import sys

print(json.dumps({"recorder_id": sys.argv[1]}, separators=(",", ":")))
PY
)
call_transaction "$registry_account" set_recorder "$ARGS" "$OWNER_ACCOUNT_ID" "$(account_key "$OWNER_ACCOUNT_ID")" '1 yoctoNEAR'
summary_block "$registry_account"
