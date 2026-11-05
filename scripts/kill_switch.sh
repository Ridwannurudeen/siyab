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
if [[ "$COLD_ACCOUNT_ID" == "$GATE_ACCOUNT_ID" ]]; then
  printf 'COLD_ACCOUNT_ID must differ from GATE_ACCOUNT_ID\n' >&2
  exit 2
fi

AGENT_PUB=${SCRIPT_ARGS[0]}
TOKEN_ACCOUNT_ID="token.$GATE_ACCOUNT_ID"
KEY_PATH=$(account_key "$OWNER_ACCOUNT_ID")
AGENT_ARGS=$(python3 - "$AGENT_PUB" <<'PY'
import json
import sys
print(json.dumps({"public_key": sys.argv[1]}, separators=(",", ":")))
PY
)
MANUAL_ARGS=$(python3 - "$TOKEN_ACCOUNT_ID" "$COLD_ACCOUNT_ID" <<'PY'
import json
import sys
token, receiver = sys.argv[1:]
print(json.dumps({"asset": {"Ft": token}, "receiver_id": receiver, "amount": "<balance>"}, separators=(",", ":")))
PY
)

PAUSED_STATE=no
AGENT_REVOKED_STATE='no in this run'
TOKEN_BALANCE_MOVED=no
report_failure() {
  local status=$?
  trap - EXIT
  if [[ "$PAUSED_STATE" == "yes" && "$status" -ne 0 ]]; then
    printf 'Kill switch partial state after exit %s:\n' "$status" >&2
    printf '  Gate paused: %s\n' "$PAUSED_STATE" >&2
    printf '  Agent key revoked: %s\n' "$AGENT_REVOKED_STATE" >&2
    printf '  Token balance moved: %s\n' "$TOKEN_BALANCE_MOVED" >&2
    printf '  Next step: address the reported issue, then run this script again; get_agent returns null after revocation, so revoke_agent is skipped.\n' >&2
    local -a manual_command=(near contract call-function as-transaction "$GATE_ACCOUNT_ID" withdraw json-args "$MANUAL_ARGS" prepaid-gas '100 Tgas' attached-deposit '1 yoctoNEAR' sign-as "$OWNER_ACCOUNT_ID" network-config "$NETWORK" sign-with-access-key-file "$KEY_PATH" send)
    printf '  Manual command after checking the Gate balance: ' >&2
    printf '%q ' "${manual_command[@]}" >&2
    printf '\n' >&2
  fi
  exit "$status"
}
trap report_failure EXIT

exec 3>&1
read_only() {
  local account=$1 method=$2 args=$3
  local -a command=(near contract call-function as-read-only "$account" "$method" json-args "$args" network-config "$NETWORK" now)
  {
    printf '+ '
    printf '%q ' "${command[@]}"
    printf '\n'
  } >&3
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  local output status
  if output=$("${command[@]}" 2>&1); then
    printf '%s\n' "$output"
  else
    status=$?
    printf '%s\n' "$output" >&2
    return "$status"
  fi
}

parse_read() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

kind, raw = sys.argv[1:]
mark = "Function execution return value (printed to stdout):"
footer = "Here is your console command"
if mark not in raw or footer not in raw:
    print(raw, file=sys.stderr)
    raise SystemExit(3)
text = raw.split(mark, 1)[1].split(footer, 1)[0].strip()
try:
    value = json.loads(text)
except json.JSONDecodeError:
    print(raw, file=sys.stderr)
    raise SystemExit(3)

if kind == "agent":
    if value is None:
        print("null")
    elif isinstance(value, dict):
        print("entry")
    else:
        print(raw, file=sys.stderr)
        raise SystemExit(3)
elif kind == "balance":
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        print(raw, file=sys.stderr)
        raise SystemExit(3)
    print(value)
elif kind == "storage":
    if value is None:
        print("null")
    elif isinstance(value, dict):
        print("registered")
    else:
        print(raw, file=sys.stderr)
        raise SystemExit(3)
else:
    raise SystemExit(3)
PY
}

call_transaction "$GATE_ACCOUNT_ID" set_paused '{"paused":true}' "$OWNER_ACCOUNT_ID" "$KEY_PATH" '1 yoctoNEAR'
if [[ "$DRY_RUN" != "1" ]]; then
  PAUSED_STATE=yes
fi

GATE_ARGS=$(printf '{"account_id":%s}' "$(json_string "$GATE_ACCOUNT_ID")")
COLD_ARGS=$(printf '{"account_id":%s}' "$(json_string "$COLD_ACCOUNT_ID")")
if [[ "$DRY_RUN" == "1" ]]; then
  read_only "$GATE_ACCOUNT_ID" get_agent "$AGENT_ARGS"
  call_transaction "$GATE_ACCOUNT_ID" revoke_agent "$AGENT_ARGS" "$OWNER_ACCOUNT_ID" "$KEY_PATH" '1 yoctoNEAR'
  read_only "$TOKEN_ACCOUNT_ID" ft_balance_of "$GATE_ARGS"
  read_only "$TOKEN_ACCOUNT_ID" storage_balance_of "$COLD_ARGS"
  BALANCE='<ft_balance_of of the Gate>'
else
  AGENT_OUTPUT=$(read_only "$GATE_ACCOUNT_ID" get_agent "$AGENT_ARGS")
  AGENT_RESULT=$(parse_read agent "$AGENT_OUTPUT")
  AGENT_RESULT=${AGENT_RESULT//$'\r'/}
  if [[ "$AGENT_RESULT" == "entry" ]]; then
    call_transaction "$GATE_ACCOUNT_ID" revoke_agent "$AGENT_ARGS" "$OWNER_ACCOUNT_ID" "$KEY_PATH" '1 yoctoNEAR'
    AGENT_REVOKED_STATE=yes
  else
    AGENT_REVOKED_STATE='no in this run (get_agent returned null; revoke_agent was skipped)'
    printf 'no agent entry for %s on %s: revoke_agent already ran or the key is not an agent key; not sent\n' "$AGENT_PUB" "$GATE_ACCOUNT_ID"
  fi

  BALANCE_OUTPUT=$(read_only "$TOKEN_ACCOUNT_ID" ft_balance_of "$GATE_ARGS")
  BALANCE=$(parse_read balance "$BALANCE_OUTPUT")
  BALANCE=${BALANCE//$'\r'/}
  if [[ "$BALANCE" == "0" ]]; then
    printf 'the Gate holds no %s; nothing to withdraw\n' "$TOKEN_ACCOUNT_ID"
  else
    STORAGE_OUTPUT=$(read_only "$TOKEN_ACCOUNT_ID" storage_balance_of "$COLD_ARGS")
    COLD_REGISTRATION=$(parse_read storage "$STORAGE_OUTPUT")
    COLD_REGISTRATION=${COLD_REGISTRATION//$'\r'/}
    if [[ "$COLD_REGISTRATION" == "null" ]]; then
      printf 'the cold account %s is not registered on %s; register it first with storage_deposit, then run this script again; set_paused and revoke_agent have already run\n' "$COLD_ACCOUNT_ID" "$TOKEN_ACCOUNT_ID" >&2
      exit 3
    fi
  fi
fi

if [[ "$BALANCE" != "0" ]]; then
  ARGS=$(python3 - "$TOKEN_ACCOUNT_ID" "$COLD_ACCOUNT_ID" "$BALANCE" <<'PY'
import json
import sys
token, receiver, amount = sys.argv[1:]
print(json.dumps({"asset": {"Ft": token}, "receiver_id": receiver, "amount": amount}, separators=(",", ":")))
PY
)
  call_transaction "$GATE_ACCOUNT_ID" withdraw "$ARGS" "$OWNER_ACCOUNT_ID" "$KEY_PATH" '1 yoctoNEAR'
  if [[ "$DRY_RUN" != "1" ]]; then
    TOKEN_BALANCE_MOVED=yes
  fi
fi
printf 'The Gate NEAR balance stays in place to stake its own state; the owner can withdraw NEAR by hand with withdraw and asset "Near" if needed.\n'
summary_block "$GATE_ACCOUNT_ID"
