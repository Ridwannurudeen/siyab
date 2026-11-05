#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID GATE_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 1 ]]; then
  printf 'usage: set_paused.sh <true|false> [--dry-run]\n' >&2
  exit 2
fi
if [[ "${SCRIPT_ARGS[0]}" != "true" && "${SCRIPT_ARGS[0]}" != "false" ]]; then
  printf 'usage: set_paused.sh <true|false> [--dry-run]\n' >&2
  exit 2
fi
ARGS=$(printf '{"paused":%s}' "${SCRIPT_ARGS[0]}")
call_transaction "$GATE_ACCOUNT_ID" set_paused "$ARGS" "$OWNER_ACCOUNT_ID" "$(account_key "$OWNER_ACCOUNT_ID")" '1 yoctoNEAR'
summary_block "$GATE_ACCOUNT_ID"
