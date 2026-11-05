#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID GATE_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 1 ]]; then
  printf 'usage: set_denylist.sh <account[,account...]> (replaces the entire denylist)\n' >&2
  exit 2
fi
IFS=',' read -r -a accounts <<< "${SCRIPT_ARGS[0]}"
ARGS=$(python3 - "${accounts[@]}" <<'PY'
import json
import sys
print(json.dumps({"accounts": sys.argv[1:]}, separators=(",", ":")))
PY
)
call_transaction "$GATE_ACCOUNT_ID" set_denylist "$ARGS" "$OWNER_ACCOUNT_ID" "$(account_key "$OWNER_ACCOUNT_ID")" '1 yoctoNEAR'
summary_block "$GATE_ACCOUNT_ID"
