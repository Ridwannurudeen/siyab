#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=${DRY_RUN:-0}
SCRIPT_ARGS=()
TRANSACTION_HASHES=()
parse_dry_run() {
  for argument in "$@"; do
    if [[ "$argument" == "--dry-run" ]]; then
      DRY_RUN=1
    else
      SCRIPT_ARGS+=("$argument")
    fi
  done
}

require_vars() {
  local name
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      printf 'missing required environment variable: %s\n' "$name" >&2
      exit 2
    fi
  done
}

require_testnet() {
  NETWORK=${NETWORK:-testnet}
  if [[ "$NETWORK" != "testnet" ]]; then
    printf 'NETWORK must be testnet\n' >&2
    exit 2
  fi
}

run() {
  printf '+ '
  printf '%q ' "$@"
  printf '\n'
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  local output status line hash shortened
  if output=$("$@" 2>&1); then
    while IFS= read -r line; do
      if [[ "${line,,}" == *"transaction id:"* ]]; then
        hash=$(printf '%s\n' "$line" | grep -oE '[1-9A-HJ-NP-Za-km-z]{43,50}' | head -n 1 || true)
        if [[ -n "$hash" ]]; then
          shortened="${hash:0:8}...${hash: -6}"
          TRANSACTION_HASHES+=("$shortened")
          line=${line//"$hash"/"$shortened"}
        fi
      fi
      printf '%s\n' "$line"
    done <<< "$output"
  else
    status=$?
    printf '%s\n' "$output" >&2
    return "$status"
  fi
}

account_key() {
  printf '%s/%s.json' "$NEAR_KEYS_DIR" "$1"
}

summary_block() {
  printf '\nSummary\n'
  printf '  network: %s\n' "$NETWORK"
  local account
  for account in "$@"; do
    printf '  account: %s\n' "$account"
    if [[ "$DRY_RUN" == "1" ]]; then
      printf '  code_hash: unavailable during dry run\n'
    else
      run near account view-account-summary "$account" network-config "$NETWORK" now
    fi
  done
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '  transaction_hashes: unavailable during dry run\n'
  elif [[ "${#TRANSACTION_HASHES[@]}" -gt 0 ]]; then
    printf '  transaction_hashes: %s\n' "${TRANSACTION_HASHES[*]}"
  else
    printf '  transaction_hashes: none returned\n'
  fi
}

json_string() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

account_exists() {
  if [[ "$DRY_RUN" == "1" ]]; then
    run near account view-account-summary "$1" network-config "$NETWORK" now
    return 1
  fi
  near account view-account-summary "$1" network-config "$NETWORK" now >/dev/null 2>&1
}

contract_initialized() {
  if [[ "$DRY_RUN" == "1" ]]; then
    run near contract call-function as-read-only "$1" version json-args '{}' network-config "$NETWORK" now
    return 1
  fi
  near contract call-function as-read-only "$1" version json-args '{}' network-config "$NETWORK" now >/dev/null 2>&1
}

create_subaccount() {
  local account_id=$1
  local balance=$2
  local gate_key
  gate_key=$(account_key "$GATE_ACCOUNT_ID")
  run near account create-account fund-myself "$account_id" "$balance NEAR" autogenerate-new-keypair save-to-legacy-keychain sign-as "$GATE_ACCOUNT_ID" network-config "$NETWORK" sign-with-access-key-file "$gate_key" send
}

install_generated_key() {
  local account_id=$1
  local key_path legacy_home legacy_path config_path
  key_path=$(account_key "$account_id")
  config_path="$HOME/.config/near-cli/config.toml"
  legacy_home=$(python3 - "$config_path" "$HOME/.near-credentials" <<'PY'
import pathlib
import sys
import tomllib

config_path = pathlib.Path(sys.argv[1])
fallback = pathlib.Path(sys.argv[2])
try:
    config = tomllib.loads(config_path.read_text())
    configured = config.get("credentials_home_dir")
except (OSError, tomllib.TOMLDecodeError):
    configured = None
if configured is not None and not isinstance(configured, str):
    raise SystemExit("credentials_home_dir must be a string")
home = pathlib.Path(configured).expanduser() if configured else fallback
if not home.is_absolute():
    home = pathlib.Path.home() / home
print(home)
PY
  ) || return 1
  legacy_path="$legacy_home/$NETWORK/$account_id.json"
  if [[ -f "$key_path" ]]; then
    if [[ -f "$legacy_path" ]]; then
      printf 'generated key exists at both %s and %s\n' "$key_path" "$legacy_path" >&2
      return 1
    fi
    chmod 600 "$key_path"
    return 0
  fi
  if [[ -f "$legacy_path" ]]; then
    mkdir -p -- "$NEAR_KEYS_DIR"
    mv -- "$legacy_path" "$key_path"
    chmod 600 "$key_path"
    return 0
  fi
  printf 'generated key missing at %s and %s\n' "$key_path" "$legacy_path" >&2
  return 1
}

deploy_with_init() {
  local account_id=$1 wasm=$2 method=$3 args=$4 key_path=$5
  run near contract deploy "$account_id" use-file "$wasm" with-init-call "$method" json-args "$args" prepaid-gas '100.0 Tgas' attached-deposit '0 NEAR' network-config "$NETWORK" sign-with-access-key-file "$key_path" send
}

call_transaction() {
  local contract=$1 method=$2 args=$3 signer=$4 key_path=$5 deposit=${6:-'0 NEAR'}
  run near contract call-function as-transaction "$contract" "$method" json-args "$args" prepaid-gas '100 Tgas' attached-deposit "$deposit" sign-as "$signer" network-config "$NETWORK" sign-with-access-key-file "$key_path" send
}
