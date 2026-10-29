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
CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-"$HOME/target/shieldbot-near"}
case "$CARGO_TARGET_DIR" in
  /*) ;;
  *) CARGO_TARGET_DIR="$ROOT_DIR/$CARGO_TARGET_DIR" ;;
esac
if [[ -n "${GATE_WASM:-}" ]]; then
  case "$GATE_WASM" in
    /*) ;;
    *) GATE_WASM="$ROOT_DIR/$GATE_WASM" ;;
  esac
else
  GATE_WASM="$ROOT_DIR/res/gate.wasm"
  if [[ ! -f "$GATE_WASM" ]]; then
    GATE_WASM="$CARGO_TARGET_DIR/near/gate.wasm"
  fi
fi
for wasm in "$GATE_WASM" "$REGISTRY_WASM" "$TOKEN_WASM"; do
  if [[ ! -f "$wasm" ]]; then
    printf 'wasm file not found: %s\n' "$wasm" >&2
    exit 1
  fi
done
TOKEN_ACCOUNT_ID="token.$GATE_ACCOUNT_ID"
REGISTRY_ACCOUNT_ID="registry.$GATE_ACCOUNT_ID"
RECORDER_ACCOUNT_ID="recorder.$GATE_ACCOUNT_ID"
GATE_KEY=$(account_key "$GATE_ACCOUNT_ID")
TOKEN_KEY=$(account_key "$TOKEN_ACCOUNT_ID")
REGISTRY_KEY=$(account_key "$REGISTRY_ACCOUNT_ID")

missing_accounts=()
planned_spend_micronear=3870
for account in "$TOKEN_ACCOUNT_ID" "$REGISTRY_ACCOUNT_ID" "$RECORDER_ACCOUNT_ID"; do
  if ! account_exists "$account"; then
    missing_accounts+=("$account")
    amount=3
    [[ "$account" == "$RECORDER_ACCOUNT_ID" ]] && amount=1
    planned_spend_micronear=$((planned_spend_micronear + amount * 1000000))
  fi
done
planned_spend=$(python3 - "$planned_spend_micronear" <<'PY'
from decimal import Decimal
import sys
print(Decimal(sys.argv[1]) / Decimal(1000000))
PY
)
printf 'planned setup spend: %s NEAR\n' "$planned_spend"
if [[ "$DRY_RUN" == "1" ]]; then
  run near account view-account-summary "$GATE_ACCOUNT_ID" network-config "$NETWORK" now
  printf 'preflight: Gate balance must cover %s NEAR and retain at least 10 NEAR\n' "$planned_spend"
else
  gate_summary=$(near account view-account-summary "$GATE_ACCOUNT_ID" network-config "$NETWORK" now)
  printf '%s\n' "$gate_summary"
  # UNVERIFIED until first run: near-cli-rs 0.30.1 balance text output format.
  gate_balance=$(printf '%s\n' "$gate_summary" | python3 -c 'import re,sys; text=sys.stdin.read(); m=re.search(r"([0-9]+(?:[.][0-9]+)?) NEAR", text); print(m.group(1) if m else "")')
  if [[ -z "$gate_balance" ]] || ! python3 - "$gate_balance" "$planned_spend" <<'PY'
from decimal import Decimal
import sys
raise SystemExit(0 if Decimal(sys.argv[1]) - Decimal(sys.argv[2]) >= Decimal("10") else 1)
PY
  then
    printf 'Gate account must retain at least 10 NEAR after the planned %s NEAR setup spend\n' "$planned_spend" >&2
    exit 1
  fi
fi

for account in "${missing_accounts[@]}"; do
  amount=3
  [[ "$account" == "$RECORDER_ACCOUNT_ID" ]] && amount=1
  create_subaccount "$account" "$amount"
done
if [[ "$DRY_RUN" != "1" ]]; then
  install_generated_key "$TOKEN_ACCOUNT_ID"
  install_generated_key "$REGISTRY_ACCOUNT_ID"
  install_generated_key "$RECORDER_ACCOUNT_ID"
fi

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
  call_transaction "$TOKEN_ACCOUNT_ID" storage_deposit "$(printf '{\"account_id\":%s,\"registration_only\":false}' "$(json_string "$account")")" "$GATE_ACCOUNT_ID" "$GATE_KEY" '0.00129 NEAR'
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

summary_block "$GATE_ACCOUNT_ID" "$TOKEN_ACCOUNT_ID" "$REGISTRY_ACCOUNT_ID" "$RECORDER_ACCOUNT_ID"
