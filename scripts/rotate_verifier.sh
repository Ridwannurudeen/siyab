#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID GATE_ACCOUNT_ID NEAR_KEYS_DIR VERIFIER_PUB
MAX_TTL_MS=${MAX_TTL_MS:-300000}
if [[ "${#SCRIPT_ARGS[@]}" -ne 1 ]]; then
  printf 'usage: rotate_verifier.sh <new_verifier_public_key> [--dry-run]\n' >&2
  exit 2
fi
new_verifier=${SCRIPT_ARGS[0]}
require_account_id "$OWNER_ACCOUNT_ID" "owner account"
require_account_id "$GATE_ACCOUNT_ID" "Gate account"
require_ed25519_public_key "$VERIFIER_PUB" "current verifier public key"
require_ed25519_public_key "$new_verifier" "new verifier public key"
if [[ "$new_verifier" == "$VERIFIER_PUB" ]]; then
  printf 'new verifier public key must differ from VERIFIER_PUB\n' >&2
  exit 2
fi
require_u64 "$MAX_TTL_MS" "MAX_TTL_MS"
if [[ "$MAX_TTL_MS" == "0" ]]; then
  printf 'MAX_TTL_MS must be greater than zero\n' >&2
  exit 2
fi
ARGS=$(python3 - "$new_verifier" "$VERIFIER_PUB" "$MAX_TTL_MS" <<'PY'
import json
import sys
import time

ttl_ms = int(sys.argv[3])
valid_until_ms = time.time_ns() // 1_000_000 + ttl_ms
if valid_until_ms > (1 << 64) - 1:
    print("MAX_TTL_MS makes previous_valid_until_ms exceed u64", file=sys.stderr)
    raise SystemExit(2)
print(json.dumps({
    "current": sys.argv[1],
    "previous": sys.argv[2],
    "previous_valid_until_ms": valid_until_ms,
}, separators=(",", ":")))
PY
)
call_transaction "$GATE_ACCOUNT_ID" set_verifier_keys "$ARGS" "$OWNER_ACCOUNT_ID" "$(account_key "$OWNER_ACCOUNT_ID")" '1 yoctoNEAR'
summary_block "$GATE_ACCOUNT_ID"
