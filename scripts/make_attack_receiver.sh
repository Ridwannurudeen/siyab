#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$ROOT_DIR/scripts/lib.sh"
parse_dry_run "$@"
resolve_counterparty_account_id
require_testnet
require_vars COUNTERPARTY_ACCOUNT_ID NEAR_KEYS_DIR
if [[ "${#SCRIPT_ARGS[@]}" -ne 1 ]]; then
  printf 'usage: make_attack_receiver.sh <name> (funded by our demo counterparty account)\n' >&2
  exit 2
fi
name=${SCRIPT_ARGS[0]}
account="$name.testnet"
counterparty_key=$(account_key "$COUNTERPARTY_ACCOUNT_ID")
new_account_key=$(account_key "$account")
if [[ "$DRY_RUN" == "1" ]] || ! account_exists "$account"; then
  # UNVERIFIED until first run: near-cli-rs legacy keychain output path and generated key JSON layout.
  run near account create-account fund-myself "$account" '0.01 NEAR' autogenerate-new-keypair save-to-legacy-keychain sign-as "$COUNTERPARTY_ACCOUNT_ID" network-config "$NETWORK" sign-with-access-key-file "$counterparty_key" send
  if [[ "$DRY_RUN" != "1" ]]; then
    install_generated_key "$account"
  fi
  printf 'generated account key path: %s\n' "$new_account_key"
fi
printf 'creation time: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
summary_block "$account"
