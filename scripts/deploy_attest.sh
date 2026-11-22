#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"

DRY_RUN=1
execute=0
dry_run=0
for argument in "$@"; do
  case "$argument" in
    --dry-run) dry_run=1 ;;
    --execute) execute=1 ;;
    --*) printf 'unknown option: %s\n' "$argument" >&2; exit 2 ;;
    *) SCRIPT_ARGS+=("$argument") ;;
  esac
done
require_testnet
if [[ "$execute" == "1" && "$dry_run" == "1" ]]; then
  printf 'choose either --dry-run or --execute\n' >&2
  exit 2
fi
if [[ "${#SCRIPT_ARGS[@]}" -lt 2 || "${#SCRIPT_ARGS[@]}" -gt 3 ]]; then
  printf 'usage: deploy_attest.sh [--dry-run | --execute] <registry.testnet> <public-config.json> [wasm]\n' >&2
  exit 2
fi
registry=${SCRIPT_ARGS[0]}
config=${SCRIPT_ARGS[1]}
wasm=${SCRIPT_ARGS[2]:-"$ROOT_DIR/res/attest.wasm"}
require_account_id "$registry" 'registry account id'
if [[ "$registry" != *.testnet ]]; then
  printf 'registry account must end in .testnet\n' >&2
  exit 2
fi
args=$(python3 - "$config" <<'PY'
import json
import re
import sys


def exact_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate config field")
        result[key] = value
    return result


def account(value):
    return (isinstance(value, str) and 2 <= len(value) <= 64
            and re.fullmatch(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", value) is not None)


try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        config = json.load(handle, object_pairs_hook=exact_object)
    fields = {"verifier_account_id", "signer_account_id", "ed25519_domain_id",
              "max_lease_ms", "vote_recipients"}
    if not isinstance(config, dict) or set(config) != fields:
        raise ValueError("expected exactly the five new/get_config fields")
    for field in ("verifier_account_id", "signer_account_id"):
        if not account(config[field]):
            raise ValueError(f"invalid {field}")
    for field in ("ed25519_domain_id", "max_lease_ms"):
        value = config[field]
        if type(value) is not int or not 0 <= value < 1 << 64:
            raise ValueError(f"invalid u64 {field}")
    if config["max_lease_ms"] == 0:
        raise ValueError("max_lease_ms must be nonzero")
    recipients = config["vote_recipients"]
    if (not isinstance(recipients, list) or not 1 <= len(recipients) <= 8
            or not all(account(value) for value in recipients)
            or recipients != sorted(set(recipients))):
        raise ValueError("vote_recipients must contain 1 through 8 sorted unique account ids")
except (OSError, ValueError) as error:
    raise SystemExit(f"invalid public config: {error}")
print(json.dumps(config, separators=(",", ":"), ensure_ascii=True))
PY
)
case "$wasm" in
  /*) ;;
  *) wasm="$(cd -- "$(dirname -- "$wasm")" && pwd)/$(basename -- "$wasm")" ;;
esac
if [[ "$execute" == "1" ]]; then
  DRY_RUN=0
  require_vars NEAR_KEYS_DIR
  if [[ ! -f "$wasm" ]]; then
    printf 'wasm file not found: %s\n' "$wasm" >&2
    exit 2
  fi
else
  NEAR_KEYS_DIR=${NEAR_KEYS_DIR:-'<external-key-directory>'}
fi

printf 'Confirm the configured verifier and signer exist on testnet and the domain is Ed25519 before executing.\n'
printf 'This script leaves access keys unchanged. A registry with a code replacement key is not immutable.\n'
key_path=$(account_key "$registry")
deploy_with_init "$registry" "$wasm" new "$args" "$key_path"
run near contract call-function as-read-only "$registry" get_config json-args '{}' network-config "$NETWORK" now
