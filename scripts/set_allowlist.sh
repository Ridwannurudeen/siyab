#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID GATE_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 2 ]]; then
  printf 'usage: set_allowlist.sh <agent_pub> <account[,account...]>\n' >&2
  exit 2
fi
AGENT_PUB=${SCRIPT_ARGS[0]}
IFS=',' read -r -a accounts <<< "${SCRIPT_ARGS[1]}"
ARGS=$(python3 - "$AGENT_PUB" "${accounts[@]}" <<'PY'
import json
import sys
print(json.dumps({"agent_pk": sys.argv[1], "accounts": sys.argv[2:]}, separators=(",", ":")))
PY
)
call_transaction "$GATE_ACCOUNT_ID" set_allowlist "$ARGS" "$OWNER_ACCOUNT_ID" "$(account_key "$OWNER_ACCOUNT_ID")" '1 yoctoNEAR'
summary_block "$GATE_ACCOUNT_ID"
