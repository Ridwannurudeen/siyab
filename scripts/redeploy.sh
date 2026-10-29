#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
cd "$ROOT_DIR"
parse_dry_run "$@"
require_testnet
require_vars GATE_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 2 ]]; then
  printf 'usage: redeploy.sh <gate|registry> <wasm>\n' >&2
  exit 2
fi
contract=${SCRIPT_ARGS[0]}
wasm=${SCRIPT_ARGS[1]}
case "$contract" in
  gate) account=$GATE_ACCOUNT_ID ;;
  registry) account="registry.$GATE_ACCOUNT_ID" ;;
  *) printf 'contract must be gate or registry\n' >&2; exit 2 ;;
esac
if [[ "$(date -u +%F)" > "2026-11-04" && "${GATE_STATE_FROZEN:-}" != "1" ]]; then
  printf 'GATE_STATE_FROZEN=1 is required after 4 Nov\n' >&2
  exit 2
fi
KEY_PATH=$(account_key "$account")
before_args=$(printf '{}')
if [[ "$DRY_RUN" == "1" ]]; then
  run near contract call-function as-read-only "$account" version json-args "$before_args" network-config "$NETWORK" now
else
  before=$(near contract call-function as-read-only "$account" version json-args "$before_args" network-config "$NETWORK" now)
  printf 'version before: %s\n' "$before"
fi
run near contract deploy "$account" use-file "$wasm" without-init-call network-config "$NETWORK" sign-with-access-key-file "$KEY_PATH" send
if [[ "$DRY_RUN" == "1" ]]; then
  run near contract call-function as-read-only "$account" version json-args "$before_args" network-config "$NETWORK" now
  printf 'code_hash: unavailable during dry run\n'
  printf 'wasm_sha256: '
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$wasm"; else printf 'sha256sum unavailable\n'; fi
  if python3 -m gate codehash --help >/dev/null 2>&1; then run python3 -m gate codehash "$wasm"; fi
else
  after=$(near contract call-function as-read-only "$account" version json-args "$before_args" network-config "$NETWORK" now)
  printf 'version after: %s\n' "$after"
  near account view-account-summary "$account" network-config "$NETWORK" now
  sha256sum "$wasm"
  if python3 -m gate codehash --help >/dev/null 2>&1; then python3 -m gate codehash "$wasm"; fi
fi
summary_block "$account"
