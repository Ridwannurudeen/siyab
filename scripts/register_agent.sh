#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID GATE_ACCOUNT_ID NEAR_KEYS_DIR AGENT_PUB AGENT_LABEL CAP_PER_CALL CAP_WINDOW CAP_WINDOW_MS UNKNOWN_PER_CALL UNKNOWN_WINDOW

TOKEN_DECIMALS=${TOKEN_DECIMALS:-6}
AGENT_ALLOWANCE_NEAR=${AGENT_ALLOWANCE_NEAR:-1}
TOKEN_ACCOUNT_ID="token.$GATE_ACCOUNT_ID"
KEY_PATH=$(account_key "$OWNER_ACCOUNT_ID")
ARGS=$(python3 - "$TOKEN_ACCOUNT_ID" "$AGENT_PUB" "$AGENT_LABEL" "$CAP_PER_CALL" "$CAP_WINDOW" "$CAP_WINDOW_MS" "$UNKNOWN_PER_CALL" "$UNKNOWN_WINDOW" "$AGENT_ALLOWANCE_NEAR" "$TOKEN_DECIMALS" <<'PY'
from decimal import Decimal
import json
import sys

token, public_key, label, per_call, window_cap, window_ms, unknown_per_call, unknown_window, allowance, decimals = sys.argv[1:]
scale = 10 ** int(decimals)
def amount(value):
    number = Decimal(value) * scale
    if number < 0 or number != number.to_integral_value():
        raise SystemExit("cap amounts must be nonnegative and fit TOKEN_DECIMALS")
    return str(int(number))
def cap(per, window):
    return {"asset": {"Ft": token}, "per_call": amount(per), "window_cap": amount(window), "window_ms": int(window_ms), "window_start_ms": 0, "spent": "0"}
allowance_yocto = Decimal(allowance) * 10**24
if allowance_yocto <= 0 or allowance_yocto != allowance_yocto.to_integral_value():
    raise SystemExit("AGENT_ALLOWANCE_NEAR must be positive and fit yoctoNEAR")
print(json.dumps({"public_key": public_key, "label": label, "caps": [cap(per_call, window_cap)], "unknown_caps": [cap(unknown_per_call, unknown_window)], "allowance_yocto": str(int(allowance_yocto))}, separators=(",", ":")))
PY
)
call_transaction "$GATE_ACCOUNT_ID" register_agent "$ARGS" "$OWNER_ACCOUNT_ID" "$KEY_PATH" '1 yoctoNEAR'
summary_block "$GATE_ACCOUNT_ID"
