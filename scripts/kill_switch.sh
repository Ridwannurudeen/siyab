#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID GATE_ACCOUNT_ID NEAR_KEYS_DIR COLD_ACCOUNT_ID
if [[ "${#SCRIPT_ARGS[@]}" -ne 1 ]]; then
  printf 'usage: kill_switch.sh <agent_pub> [--dry-run]\n' >&2
  exit 2
fi

AGENT_PUB=${SCRIPT_ARGS[0]}
TOKEN_ACCOUNT_ID="token.$GATE_ACCOUNT_ID"
KEY_PATH=$(account_key "$OWNER_ACCOUNT_ID")
ARGS=$(python3 - "$AGENT_PUB" <<'PY'
import json
import sys
print(json.dumps({"public_key": sys.argv[1]}, separators=(",", ":")))
PY
)
call_transaction "$GATE_ACCOUNT_ID" set_paused '{"paused":true}' "$OWNER_ACCOUNT_ID" "$KEY_PATH" '1 yoctoNEAR'
call_transaction "$GATE_ACCOUNT_ID" revoke_agent "$ARGS" "$OWNER_ACCOUNT_ID" "$KEY_PATH" '1 yoctoNEAR'

GATE_ARGS=$(printf '{"account_id":%s}' "$(json_string "$GATE_ACCOUNT_ID")")
COLD_ARGS=$(printf '{"account_id":%s}' "$(json_string "$COLD_ACCOUNT_ID")")
if [[ "$DRY_RUN" == "1" ]]; then
  run near contract call-function as-read-only "$TOKEN_ACCOUNT_ID" ft_balance_of json-args "$GATE_ARGS" network-config "$NETWORK" now
  run near contract call-function as-read-only "$TOKEN_ACCOUNT_ID" storage_balance_of json-args "$COLD_ARGS" network-config "$NETWORK" now
  BALANCE='<ft_balance_of of the Gate>'
else
  BALANCE_OUTPUT=$(run near contract call-function as-read-only "$TOKEN_ACCOUNT_ID" ft_balance_of json-args "$GATE_ARGS" network-config "$NETWORK" now)
  STORAGE_OUTPUT=$(run near contract call-function as-read-only "$TOKEN_ACCOUNT_ID" storage_balance_of json-args "$COLD_ARGS" network-config "$NETWORK" now)
  READ_RESULTS=$(python3 - "$BALANCE_OUTPUT" "$STORAGE_OUTPUT" <<'PY'
import json
import sys

# near-cli-rs 0.30.1 prints the return value, one line or pretty printed over several, between this
# line and the block that repeats the console command.
MARK = "Function execution return value (printed to stdout):"
FOOTER = "Here is your console command"


def answer(raw):
    if MARK not in raw:
        print(raw, file=sys.stderr)
        raise SystemExit(3)
    text = raw.split(MARK, 1)[1].split(FOOTER, 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(raw, file=sys.stderr)
        raise SystemExit(3)

balance_raw, storage_raw = sys.argv[1:]
balance = answer(balance_raw)
storage = answer(storage_raw)
if not isinstance(balance, str) or not balance.isascii() or not balance.isdigit() or not isinstance(storage, (dict, type(None))):
    print(balance_raw, file=sys.stderr)
    print(storage_raw, file=sys.stderr)
    raise SystemExit(3)
print(balance)
print("null" if storage is None else "registered")
PY
)
  READ_RESULTS=${READ_RESULTS//$'\r'/}
  BALANCE=${READ_RESULTS%%$'\n'*}
  COLD_REGISTRATION=${READ_RESULTS##*$'\n'}
  if [[ "$COLD_REGISTRATION" == "null" ]]; then
    printf 'the cold account %s is not registered on %s; register it first with storage_deposit, then run this script again; set_paused and revoke_agent have already run\n' "$COLD_ACCOUNT_ID" "$TOKEN_ACCOUNT_ID" >&2
    exit 3
  fi
fi

ARGS=$(python3 - "$TOKEN_ACCOUNT_ID" "$COLD_ACCOUNT_ID" "$BALANCE" <<'PY'
import json
import sys
token, receiver, amount = sys.argv[1:]
print(json.dumps({"asset": {"Ft": token}, "receiver_id": receiver, "amount": amount}, separators=(",", ":")))
PY
)
call_transaction "$GATE_ACCOUNT_ID" withdraw "$ARGS" "$OWNER_ACCOUNT_ID" "$KEY_PATH" '1 yoctoNEAR'
printf 'The Gate NEAR balance stays in place to stake its own state; the owner can withdraw NEAR by hand with withdraw and asset "Near" if needed.\n'
summary_block "$GATE_ACCOUNT_ID"
