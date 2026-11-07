#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars OWNER_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 3 ]]; then
  printf 'usage: deploy_reports.sh <reports_account> <wasm> <moderator[,moderator...]>' >&2
  exit 2
fi
account=${SCRIPT_ARGS[0]}
wasm=${SCRIPT_ARGS[1]}
IFS=',' read -r -a moderators <<< "${SCRIPT_ARGS[2]}"
if [[ "${#moderators[@]}" -eq 0 ]]; then
  printf 'at least one moderator is required\n' >&2
  exit 2
fi
for moderator in "${moderators[@]}"; do
  if [[ -z "$moderator" ]]; then
    printf 'moderator account ids must not be empty\n' >&2
    exit 2
  fi
done
case "$wasm" in
  /*) ;;
  *) wasm="$(cd -- "$(dirname -- "$wasm")" && pwd)/$(basename -- "$wasm")" ;;
esac
ARGS=$(python3 - "$OWNER_ACCOUNT_ID" "${moderators[@]}" <<'PY'
import json
import sys

print(json.dumps({"owner_id": sys.argv[1], "moderators": sys.argv[2:]}, separators=(",", ":")))
PY
)
cd "$ROOT_DIR"
deploy_with_init "$account" "$wasm" new "$ARGS" "$(account_key "$account")"
summary_block "$account"
