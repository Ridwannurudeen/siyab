#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID GATE_ACCOUNT_ID VENDOR_ACCOUNT_ID AGED_PAYEE_ACCOUNT_ID NEAR_KEYS_DIR VERIFIER_PUB TOKEN_SUPPLY REGISTRY_WASM TOKEN_WASM

TOKEN_SYMBOL=${TOKEN_SYMBOL:-DMT}
TOKEN_DECIMALS=${TOKEN_DECIMALS:-6}
MAX_TTL_MS=${MAX_TTL_MS:-300000}
GATE_WASM=${GATE_WASM:-res/gate.wasm}
TOKEN_ACCOUNT_ID="token.$GATE_ACCOUNT_ID"
REGISTRY_ACCOUNT_ID="registry.$GATE_ACCOUNT_ID"
RECORDER_ACCOUNT_ID="recorder.$GATE_ACCOUNT_ID"
GATE_KEY=$(account_key "$GATE_ACCOUNT_ID")
TOKEN_KEY=$(account_key "$TOKEN_ACCOUNT_ID")
REGISTRY_KEY=$(account_key "$REGISTRY_ACCOUNT_ID")

for account in "$TOKEN_ACCOUNT_ID" "$REGISTRY_ACCOUNT_ID" "$RECORDER_ACCOUNT_ID"; do
  if [[ "$DRY_RUN" == "1" ]]; then
    run near account view-account-summary "$account" network-config "$NETWORK" now
    amount=3
    [[ "$account" == "$RECORDER_ACCOUNT_ID" ]] && amount=1
    create_subaccount "$account" "$amount"
  elif ! account_exists "$account"; then
    amount=3
    [[ "$account" == "$RECORDER_ACCOUNT_ID" ]] && amount=1
    create_subaccount "$account" "$amount"
  fi
done

token_new=$(python3 - "$GATE_ACCOUNT_ID" "$TOKEN_SUPPLY" "$TOKEN_SYMBOL" "$TOKEN_DECIMALS" <<'PY'
import json
import sys
print(json.dumps({"owner_id": sys.argv[1], "total_supply": sys.argv[2], "metadata": {"spec": "ft-1.0.0", "name": "ShieldBot test token", "symbol": sys.argv[3], "decimals": int(sys.argv[4]), "icon": None, "reference": None, "reference_hash": None}}, separators=(",", ":")))
PY
)
if [[ "$DRY_RUN" == "1" ]]; then
  run near contract call-function as-read-only "$TOKEN_ACCOUNT_ID" ft_metadata json-args '{}' network-config "$NETWORK" now
  deploy_with_init "$TOKEN_ACCOUNT_ID" "$TOKEN_WASM" new "$token_new" "$TOKEN_KEY"
elif ! near contract call-function as-read-only "$TOKEN_ACCOUNT_ID" ft_metadata json-args '{}' network-config "$NETWORK" now >/dev/null 2>&1; then
  deploy_with_init "$TOKEN_ACCOUNT_ID" "$TOKEN_WASM" new "$token_new" "$TOKEN_KEY"
fi

for account in "$GATE_ACCOUNT_ID" "$VENDOR_ACCOUNT_ID" "$AGED_PAYEE_ACCOUNT_ID"; do
  call_transaction "$TOKEN_ACCOUNT_ID" storage_deposit "$(printf '{\"account_id\":%s,\"registration_only\":false}' "$(json_string "$account")")" "$GATE_ACCOUNT_ID" "$GATE_KEY" '0.00125 NEAR'
done

gate_new=$(python3 - "$OWNER_ACCOUNT_ID" "$VERIFIER_PUB" "$MAX_TTL_MS" <<'PY'
import json
import sys
print(json.dumps({"owner_id": sys.argv[1], "verifier_key": sys.argv[2], "max_ttl_ms": int(sys.argv[3])}, separators=(",", ":")))
PY
)
if [[ "$DRY_RUN" == "1" ]]; then
  run near contract call-function as-read-only "$GATE_ACCOUNT_ID" version json-args '{}' network-config "$NETWORK" now
  deploy_with_init "$GATE_ACCOUNT_ID" "$GATE_WASM" new "$gate_new" "$GATE_KEY"
elif ! contract_initialized "$GATE_ACCOUNT_ID"; then
  deploy_with_init "$GATE_ACCOUNT_ID" "$GATE_WASM" new "$gate_new" "$GATE_KEY"
fi

registry_new=$(python3 - "$OWNER_ACCOUNT_ID" "$RECORDER_ACCOUNT_ID" <<'PY'
import json
import sys
print(json.dumps({"owner_id": sys.argv[1], "recorder_id": sys.argv[2]}, separators=(",", ":")))
PY
)
if [[ "$DRY_RUN" == "1" ]]; then
  run near contract call-function as-read-only "$REGISTRY_ACCOUNT_ID" version json-args '{}' network-config "$NETWORK" now
  deploy_with_init "$REGISTRY_ACCOUNT_ID" "$REGISTRY_WASM" new "$registry_new" "$REGISTRY_KEY"
elif ! contract_initialized "$REGISTRY_ACCOUNT_ID"; then
  deploy_with_init "$REGISTRY_ACCOUNT_ID" "$REGISTRY_WASM" new "$registry_new" "$REGISTRY_KEY"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  run near account view-account-summary "$GATE_ACCOUNT_ID" network-config "$NETWORK" now
else
  # UNVERIFIED until first run: near-cli-rs view-account-summary balance output parsing.
  gate_summary=$(near account view-account-summary "$GATE_ACCOUNT_ID" network-config "$NETWORK" now)
  printf '%s\n' "$gate_summary"
  gate_balance=$(printf '%s\n' "$gate_summary" | python3 -c 'import re,sys; text=sys.stdin.read(); m=re.search(r"([0-9]+(?:[.][0-9]+)?) NEAR", text); print(m.group(1) if m else "")')
  if [[ -z "$gate_balance" ]] || ! python3 - "$gate_balance" <<'PY'
from decimal import Decimal
import sys
raise SystemExit(0 if Decimal(sys.argv[1]) >= 10 else 1)
PY
  then
    printf 'Gate account must retain at least 10 NEAR\n' >&2
    exit 1
  fi
fi
summary_block "$GATE_ACCOUNT_ID" "$TOKEN_ACCOUNT_ID" "$REGISTRY_ACCOUNT_ID" "$RECORDER_ACCOUNT_ID"
