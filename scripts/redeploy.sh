#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
MIGRATE=0
arguments=()
for argument in "$@"; do
  if [[ "$argument" == "--migrate" ]]; then
    MIGRATE=1
  else
    arguments+=("$argument")
  fi
done
parse_dry_run "${arguments[@]}"
require_testnet
require_vars GATE_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 2 ]]; then
  printf 'usage: redeploy.sh <gate|registry|reports> <wasm> [--migrate]\n' >&2
  exit 2
fi
contract=${SCRIPT_ARGS[0]}
wasm=${SCRIPT_ARGS[1]}
case "$wasm" in
  /*) ;;
  *) wasm="$(cd -- "$(dirname -- "$wasm")" && pwd)/$(basename -- "$wasm")" ;;
esac
cd "$ROOT_DIR"
case "$contract" in
  gate) account=$GATE_ACCOUNT_ID ;;
  registry) account="registry.$GATE_ACCOUNT_ID" ;;
  reports) account=${REPORTS_ACCOUNT_ID:-reports.$GATE_ACCOUNT_ID} ;;
  *) printf 'contract must be gate, registry or reports\n' >&2; exit 2 ;;
esac
# The reports contract has no version view, so its version can be neither read nor asserted.
if [[ "$contract" == "reports" && -n "${EXPECT_VERSION:-}" ]]; then
  printf 'EXPECT_VERSION cannot be asserted: the reports contract has no version view\n' >&2
  exit 2
fi
if [[ "$(date -u +%F)" > "2026-11-04" && "${GATE_STATE_FROZEN:-}" != "1" ]]; then
  printf 'GATE_STATE_FROZEN=1 is required after 4 Nov\n' >&2
  exit 2
fi
KEY_PATH=$(account_key "$account")
before_args=$(printf '{}')

parse_version() {
  python3 - "$1" <<'PY'
import json
import sys

raw = sys.argv[1]
mark = "Function execution return value (printed to stdout):"
footer = "Here is your console command"
if mark not in raw or footer not in raw:
    print(raw, file=sys.stderr)
    raise SystemExit(3)
try:
    value = json.loads(raw.split(mark, 1)[1].split(footer, 1)[0].strip())
except json.JSONDecodeError:
    print(raw, file=sys.stderr)
    raise SystemExit(3)
if not isinstance(value, str):
    print(raw, file=sys.stderr)
    raise SystemExit(3)
print(value)
PY
}

read_code_hash() {
  python3 - "$1" <<'PY'
import sys

from gate import rpc

print(rpc.Client(rpc.TESTNET_RPC_URL, "off").code_hash(sys.argv[1]))
PY
}

if [[ "$contract" == "reports" ]]; then
  printf 'version before: not read, the reports contract has no version view\n'
elif [[ "$DRY_RUN" == "1" ]]; then
  run near contract call-function as-read-only "$account" version json-args "$before_args" network-config "$NETWORK" now
else
  before=$(near contract call-function as-read-only "$account" version json-args "$before_args" network-config "$NETWORK" now)
  printf 'version before: %s\n' "$before"
fi
if [[ "$MIGRATE" == "1" ]]; then
  deploy_with_init "$account" "$wasm" migrate '{}' "$KEY_PATH"
else
  run near contract deploy "$account" use-file "$wasm" without-init-call network-config "$NETWORK" sign-with-access-key-file "$KEY_PATH" send
fi
if [[ "$DRY_RUN" == "1" ]]; then
  if [[ "$contract" != "reports" ]]; then
    run near contract call-function as-read-only "$account" version json-args "$before_args" network-config "$NETWORK" now
  fi
  printf 'code_hash: unavailable during dry run\n'
  printf 'wasm_sha256: '
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$wasm"; else printf 'sha256sum unavailable\n'; fi
  if python3 -m gate codehash --help >/dev/null 2>&1; then run python3 -m gate codehash "$wasm"; fi
else
  if [[ "$contract" != "reports" ]]; then
    after=$(near contract call-function as-read-only "$account" version json-args "$before_args" network-config "$NETWORK" now 2>&1)
    printf 'version after: %s\n' "$after"
  fi
  near account view-account-summary "$account" network-config "$NETWORK" now
  sha256sum "$wasm"
  if python3 -m gate codehash --help >/dev/null 2>&1; then python3 -m gate codehash "$wasm"; fi
fi
summary_block "$account"

if [[ "$DRY_RUN" == "1" ]]; then
  if [[ -n "${EXPECT_VERSION:-}" ]]; then
    printf 'would assert: version after equals %s\n' "$EXPECT_VERSION"
  fi
  if [[ -n "${EXPECT_CODE_HASH:-}" ]]; then
    printf 'would assert: code_hash of %s equals %s\n' "$account" "$EXPECT_CODE_HASH"
  fi
  exit 0
fi
if [[ -n "${EXPECT_VERSION:-}" ]]; then
  if ! version=$(parse_version "$after"); then
    printf 'version after could not be read, expected %s\n' "$EXPECT_VERSION" >&2
    exit 1
  fi
  if [[ "$version" != "$EXPECT_VERSION" ]]; then
    printf 'version after is %s, expected %s\n' "$version" "$EXPECT_VERSION" >&2
    exit 1
  fi
  printf 'asserted: version after equals %s\n' "$EXPECT_VERSION"
fi
if [[ -n "${EXPECT_CODE_HASH:-}" ]]; then
  # view_account reads final state, which can trail the deploy by a block or two.
  for attempt in 1 2 3 4 5; do
    code_hash=$(read_code_hash "$account")
    if [[ "$code_hash" == "$EXPECT_CODE_HASH" ]]; then
      break
    fi
    if [[ "$attempt" -lt 5 ]]; then
      sleep 2
    fi
  done
  if [[ "$code_hash" != "$EXPECT_CODE_HASH" ]]; then
    printf 'code_hash of %s is %s, expected %s\n' "$account" "$code_hash" "$EXPECT_CODE_HASH" >&2
    exit 1
  fi
  printf 'asserted: code_hash of %s equals %s\n' "$account" "$EXPECT_CODE_HASH"
fi
