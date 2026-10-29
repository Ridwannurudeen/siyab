#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_testnet
require_vars GATE_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -lt 1 || "${#SCRIPT_ARGS[@]}" -gt 3 ]]; then
  printf 'usage: record_verdicts.sh <args.json> [--labels <args.json>] [--dry-run]' >&2
  exit 2
fi
records_file=${SCRIPT_ARGS[0]}
labels_file=''
if [[ "${#SCRIPT_ARGS[@]}" -eq 3 && "${SCRIPT_ARGS[1]}" == "--labels" ]]; then
  labels_file=${SCRIPT_ARGS[2]}
elif [[ "${#SCRIPT_ARGS[@]}" -ne 1 ]]; then
  printf 'usage: record_verdicts.sh <args.json> [--labels <args.json>] [--dry-run]' >&2
  exit 2
fi
REGISTRY_ACCOUNT_ID="registry.$GATE_ACCOUNT_ID"
RECORDER_ACCOUNT_ID="recorder.$GATE_ACCOUNT_ID"
ARGS=$(python3 - "$records_file" <<'PY'
import json
import pathlib
import sys
print(json.dumps(json.loads(pathlib.Path(sys.argv[1]).read_text()), separators=(",", ":")))
PY
)
call_transaction "$REGISTRY_ACCOUNT_ID" record_batch "$ARGS" "$RECORDER_ACCOUNT_ID" "$(account_key "$RECORDER_ACCOUNT_ID")"
if [[ -n "$labels_file" ]]; then
  while IFS= read -r args; do
    call_transaction "$REGISTRY_ACCOUNT_ID" record_label "$args" "$RECORDER_ACCOUNT_ID" "$(account_key "$RECORDER_ACCOUNT_ID")"
  done < <(python3 - "$labels_file" <<'PY'
import json
import pathlib
import sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
labels = value.get("labels", value)
for label in labels:
    print(json.dumps(label, separators=(",", ":")))
PY
  )
fi
summary_block "$REGISTRY_ACCOUNT_ID"
