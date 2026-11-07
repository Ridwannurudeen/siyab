#!/usr/bin/env bash
# Deploys the verdict registry to NEAR mainnet and initializes it with new(owner_id, recorder_id).
#
# Do not run this script until all six registry mainnet gates are met and the owner approves:
#   1. this mainnet deploy script is reviewed;
#   2. the recorder key is separate from the verifier key and off the build machine;
#   3. recorder rotation and recovery are written and rehearsed;
#   4. a correction and supersession policy is written;
#   5. records are limited to our own verdicts and primary sourced incident labels;
#   6. one outside party has run python -m gate verify on a testnet record.
# docs/REGISTRY.md (Mainnet) lists which gates are met and the file behind each.
#
# It refuses unless NETWORK=mainnet and MAINNET_DEPLOY_OK=1, the wasm sha256 equals
# res/registry.wasm.sha256 and res/BUILD-registry.md names a 40 hex source commit. The registry
# account must already exist; it refuses an account that holds code unless REDEPLOY=1, which deploys
# without an init call and requires EXPECT_VERSION and EXPECT_CODE_HASH, and EXPECT_CODE_HASH must
# equal the code hash of the wasm. DRY_RUN=1 prints every command and assertion and makes no near call.
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
require_mainnet_deploy_ok
require_vars NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 4 ]]; then
  printf 'usage: deploy_registry_mainnet.sh <registry_account> <owner_account> <recorder_account> <wasm>\n' >&2
  exit 2
fi
registry=${SCRIPT_ARGS[0]}
owner=${SCRIPT_ARGS[1]}
recorder=${SCRIPT_ARGS[2]}
wasm=${SCRIPT_ARGS[3]}
REDEPLOY=${REDEPLOY:-0}

# A named mainnet account: dot separated parts of lower case letters and digits joined by - or _,
# ending in .near, at most 64 characters. A 64 character implicit account never ends in .near.
named_account='^(([a-z0-9]+[-_])*[a-z0-9]+\.)+near$'
for account in "$registry" "$owner" "$recorder"; do
  if [[ ! "$account" =~ $named_account || "${#account}" -gt 64 ]]; then
    printf 'not a named mainnet account ending in .near: %s\n' "$account" >&2
    exit 2
  fi
done
if [[ "$recorder" == "$owner" ]]; then
  printf 'the recorder account must differ from the owner account\n' >&2
  exit 2
fi
if [[ "$REDEPLOY" == "1" ]]; then
  require_vars EXPECT_VERSION EXPECT_CODE_HASH
fi

if [[ ! -f "$wasm" ]]; then
  printf 'wasm file not found: %s\n' "$wasm" >&2
  exit 2
fi
case "$wasm" in
  /*) ;;
  *) wasm="$(cd -- "$(dirname -- "$wasm")" && pwd)/$(basename -- "$wasm")" ;;
esac
cd "$ROOT_DIR"
codehash_output=$(python3 -m gate codehash "$wasm")
wasm_sha256=$(printf '%s\n' "$codehash_output" | sed -n 's/^sha256: //p')
code_hash=$(printf '%s\n' "$codehash_output" | sed -n 's/^code_hash: //p')
if ! python3 - "$code_hash" <<'PY'
import sys

from gate import base58

try:
    decoded = base58.decode(sys.argv[1])
except ValueError:
    raise SystemExit(1)
if len(decoded) != 32:
    raise SystemExit(1)
PY
then
  printf 'invalid code_hash from python -m gate codehash: expected base58 encoding of 32 bytes\n' >&2
  exit 2
fi
recorded_sha256=$(cut -d ' ' -f 1 res/registry.wasm.sha256)
if [[ -z "$wasm_sha256" || "$wasm_sha256" != "$recorded_sha256" ]]; then
  printf 'wasm sha256 %s does not equal res/registry.wasm.sha256 %s\n' "$wasm_sha256" "$recorded_sha256" >&2
  exit 2
fi
source_commit=$(sed -n 's/^Source commit: \([0-9a-f]\{40\}\)$/\1/p' res/BUILD-registry.md)
if [[ -z "$source_commit" ]]; then
  printf 'res/BUILD-registry.md has no 40 hex Source commit line\n' >&2
  exit 2
fi
printf 'source commit: %s\n' "$source_commit"
printf 'wasm sha256: %s (equals res/registry.wasm.sha256)\n' "$wasm_sha256"
printf 'expected code_hash: %s\n' "$code_hash"
if [[ "$REDEPLOY" == "1" && "$EXPECT_CODE_HASH" != "$code_hash" ]]; then
  printf 'EXPECT_CODE_HASH %s does not equal the code_hash of %s, %s\n' "$EXPECT_CODE_HASH" "$wasm" "$code_hash" >&2
  exit 2
fi

printf '\nMAINNET registry deploy to %s\n' "$registry"
printf 'Run this only when all six registry mainnet gates in docs/REGISTRY.md are met and the owner approves.\n'
printf '  owner: %s\n  recorder: %s\n  redeploy: %s\n\n' "$owner" "$recorder" "$REDEPLOY"
if [[ "$DRY_RUN" != "1" ]]; then
  printf 'type the registry account name to continue: '
  read -r answer < /dev/tty
  if [[ "$answer" != "$registry" ]]; then
    printf 'confirmation does not match %s, nothing was sent\n' "$registry" >&2
    exit 2
  fi
fi

KEY_PATH=$(account_key "$registry")

read_code_hash() {
  python3 - "$1" <<'PY'
import sys

from gate import rpc

try:
    print(rpc.Client(rpc.MAINNET_RPC_URL, "off").code_hash(sys.argv[1]))
except rpc.RpcError as error:
    if error.cause == "UNKNOWN_ACCOUNT":
        print(f"registry account {sys.argv[1]} not found on mainnet", file=sys.stderr)
    else:
        print(f"view_account of {sys.argv[1]} failed: {error}", file=sys.stderr)
    raise SystemExit(2)
PY
}

empty_code_hash() {
  python3 -c 'from gate import rpc; print(rpc.EMPTY_CODE_HASH)'
}

parse_view() {
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

empty=$(empty_code_hash)
if [[ "$DRY_RUN" == "1" ]]; then
  printf 'would check: code_hash of %s from view_account on %s equals the empty code hash %s\n' \
    "$registry" "$(python3 -c 'from gate import rpc; print(rpc.MAINNET_RPC_URL)')" "$empty"
  if [[ "$REDEPLOY" == "1" ]]; then
    printf 'would refuse if %s holds no code, since REDEPLOY=1 deploys over existing state\n' "$registry"
  else
    printf 'would refuse if %s holds code, since REDEPLOY is not 1\n' "$registry"
  fi
else
  current_code_hash=$(read_code_hash "$registry")
  printf 'code_hash of %s before: %s\n' "$registry" "$current_code_hash"
  if [[ "$current_code_hash" != "$empty" && "$REDEPLOY" != "1" ]]; then
    printf '%s already holds code; set REDEPLOY=1 with EXPECT_VERSION and EXPECT_CODE_HASH to redeploy\n' "$registry" >&2
    exit 2
  fi
  if [[ "$current_code_hash" == "$empty" && "$REDEPLOY" == "1" ]]; then
    printf '%s holds no code, so there is nothing to redeploy; run without REDEPLOY\n' "$registry" >&2
    exit 2
  fi
fi

if [[ "$REDEPLOY" == "1" ]]; then
  run near contract deploy "$registry" use-file "$wasm" without-init-call network-config "$NETWORK" sign-with-access-key-file "$KEY_PATH" send
  expected_code_hash=$EXPECT_CODE_HASH
else
  registry_new=$(python3 - "$owner" "$recorder" <<'PY'
import json
import sys
print(json.dumps({"owner_id": sys.argv[1], "recorder_id": sys.argv[2]}, separators=(",", ":")))
PY
  )
  deploy_with_init "$registry" "$wasm" new "$registry_new" "$KEY_PATH"
  expected_code_hash=$code_hash
fi

if [[ "$DRY_RUN" == "1" ]]; then
  run near contract call-function as-read-only "$registry" get_recorder json-args '{}' network-config "$NETWORK" now
  run near contract call-function as-read-only "$registry" version json-args '{}' network-config "$NETWORK" now
  printf 'would assert: get_recorder of %s equals %s\n' "$registry" "$recorder"
  if [[ "$REDEPLOY" == "1" ]]; then
    printf 'would assert: version of %s equals %s\n' "$registry" "$EXPECT_VERSION"
  else
    printf 'would print: version of %s\n' "$registry"
  fi
  printf 'would assert: code_hash of %s equals %s\n' "$registry" "$expected_code_hash"
  printf 'owner: not read back, the registry has no owner view\n'
  summary_block "$registry"
  exit 0
fi

recorder_answer=$(near contract call-function as-read-only "$registry" get_recorder json-args '{}' network-config "$NETWORK" now 2>&1)
if ! read_recorder=$(parse_view "$recorder_answer"); then
  printf 'get_recorder could not be read, expected %s\n' "$recorder" >&2
  exit 1
fi
if [[ "$read_recorder" != "$recorder" ]]; then
  printf 'get_recorder of %s is %s, expected %s\n' "$registry" "$read_recorder" "$recorder" >&2
  exit 1
fi
printf 'asserted: get_recorder of %s equals %s\n' "$registry" "$recorder"
version_answer=$(near contract call-function as-read-only "$registry" version json-args '{}' network-config "$NETWORK" now 2>&1)
if ! version=$(parse_view "$version_answer"); then
  printf 'version could not be read\n' >&2
  exit 1
fi
if [[ "$REDEPLOY" == "1" ]]; then
  if [[ "$version" != "$EXPECT_VERSION" ]]; then
    printf 'version of %s is %s, expected %s\n' "$registry" "$version" "$EXPECT_VERSION" >&2
    exit 1
  fi
  printf 'asserted: version of %s equals %s\n' "$registry" "$EXPECT_VERSION"
else
  printf 'version of %s: %s\n' "$registry" "$version"
fi
# view_account reads final state, which can trail the deploy by a block or two.
for attempt in 1 2 3 4 5; do
  deployed_code_hash=$(read_code_hash "$registry")
  if [[ "$deployed_code_hash" == "$expected_code_hash" ]]; then
    break
  fi
  if [[ "$attempt" -lt 5 ]]; then
    sleep 2
  fi
done
if [[ "$deployed_code_hash" != "$expected_code_hash" ]]; then
  printf 'code_hash of %s is %s, expected %s\n' "$registry" "$deployed_code_hash" "$expected_code_hash" >&2
  exit 1
fi
printf 'asserted: code_hash of %s equals %s\n' "$registry" "$expected_code_hash"
printf 'owner: not read back, the registry has no owner view\n'
summary_block "$registry"
