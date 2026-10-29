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
      if [[ "${line,,}" == *transaction* ]]; then
        # UNVERIFIED until first run: transaction hash output line format from near-cli-rs.
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
  # UNVERIFIED until first run: the legacy keychain output path and key file layout; near-cli-rs 0.30.1 help lists fund-myself and autogenerate-new-keypair.
  run near account create-account fund-myself "$account_id" "$balance NEAR" autogenerate-new-keypair save-to-legacy-keychain sign-as "$GATE_ACCOUNT_ID" network-config "$NETWORK" sign-with-access-key-file "$gate_key" send
}

deploy_with_init() {
  local account_id=$1 wasm=$2 method=$3 args=$4 key_path=$5
  run near contract deploy "$account_id" use-file "$wasm" with-init-call "$method" json-args "$args" prepaid-gas '100.0 Tgas' attached-deposit '0 NEAR' network-config "$NETWORK" sign-with-access-key-file "$key_path" send
}

call_transaction() {
  local contract=$1 method=$2 args=$3 signer=$4 key_path=$5 deposit=${6:-'0 NEAR'}
  run near contract call-function as-transaction "$contract" "$method" json-args "$args" prepaid-gas '100 Tgas' attached-deposit "$deposit" sign-as "$signer" network-config "$NETWORK" sign-with-access-key-file "$key_path" send
}
