# Account investigations and team workspaces

This document describes the implemented configuration and its limits. Service files are operational templates; their presence does not establish that a particular release is deployed or that a worker is running.

## Browser and shared work

The existing browser workspace remains usable without an account. It keeps investigations, graph view settings, notes, watchlists and optional browser notifications on that device. Local monitoring checks the current feed window while a Siyab page stays open. Display pause controls remain separate from alert monitoring.

The save dialog defaults to **This browser**. Signing in does not upload local data. Choose a team explicitly to save a shared investigation, or review and confirm an import from browser storage or a JSON file. Imports create records without replacing existing notes; identical title, notes and URL triples are skipped. Import time becomes the shared record's creation time. Original browser data and backup files remain unchanged.

Cloud backups can contain up to 500 investigations and 100 watched accounts per team. The browser-only workspace has its existing 50-investigation limit, so a larger shared backup cannot be imported there as a whole. Shared file imports are bounded to 8 MiB and sent in batches of at most 50 investigations and 24 KiB; progress reports completed batches. After an interrupted response, refresh before retrying the same backup, because a timed-out request may have committed. Exact duplicates are skipped on retry.

Shared investigations use versions. If another editor saves first, a stale update or deletion is rejected. The editor preserves the draft and offers a draft download and an explicit reload of the latest version.

## Accounts and permissions

Accounts use a 3 to 40 character lowercase username and a 12 to 128 character password. Passwords use salted scrypt hashes. Sessions expire after seven days and use HttpOnly, SameSite=Strict cookies, with Secure cookies on the production HTTPS origin. Session tokens are not returned to browser JavaScript or stored in browser storage. Authenticated changes require the configured exact Origin and a CSRF token.

Registration, password changes and recovery show a recovery code once. Keep it privately; it can replace a forgotten password. Password changes and recovery revoke other sessions and the previous recovery code. Email password reset is not configured. Losing both the password and recovery code has no self-service recovery path.

| Role | Permissions |
| --- | --- |
| Viewer | Read and export team investigations, watches, alerts and audit entries |
| Editor | Viewer permissions plus save/edit/delete investigations, manage watched accounts, import and acknowledge alerts |
| Owner | Editor permissions plus invitations, member removal and role changes |

Invitations are single-use codes bound to an existing recipient username. They expire after 24 hours and initially grant viewer or editor access. Owners can subsequently change roles; the last owner cannot be removed or demoted. An account can belong to at most 20 teams. Application audit entries record team changes but are not tamper-proof against server administrators.

## Public data and source bounds

Global search links valid mainnet account identifiers to `/investigate.html?account=<account>`, while retaining recorded account/entity matches. Testnet accounts are outside this view. A missing recorded match does not establish a favorable payment result.

Mainnet profile, holdings, transaction and transfer reads expose RPC/indexer sources and unavailable states. Holdings metadata is read from token contracts; names, symbols and decimals are contract-reported. Contract source metadata and links are also reported values, not verified ownership or reproducible source-code matches. Validator observations apply to the returned epoch. Labels retain their own provenance and uncertainty.

Live flow/relationship graphs read at most six accounts and 20 transfers per account. Edges establish observed transfer relationships within those windows, not common ownership. Public observations and immutable graph/event evidence can be retained in the account history database. Private team notes, credentials and watchlist ownership do not belong in those public artifacts.

Transaction and transfer backfills read one or two provider pages per request, up to 100 rows per page. The shared archive allows one backfill call per account per minute across transaction/transfer kinds and reserves at most 12 backfill pages per minute globally. Continuations must match the archive's committed cursor; concurrent backfills for an account are refused. Archive browsing reads already retained rows; it does not fetch all account history automatically. An exhausted provider cursor still reports `history_complete: false`. Rate limits, provider omissions, polling gaps and history preceding the available pages can remain.

History schema version 2 tracks archive budgets: 128 MiB of retained document/row bodies, 10,000 provider pages, 100,000 history rows, 2,000 graphs, 100,000 event observations and 200,000 watch-deduplication entries. Capacity is shared across accounts. When a limit is reached, new observations are refused or paused while retained evidence remains available; the archive does not silently evict evidence to make space. The byte budget measures retained bodies, not the entire SQLite file or its WAL/index overhead.

## State and services

Use one external state directory for the API and both workers. The service templates use:

| Purpose | Path |
| --- | --- |
| Checkout and generated pages | `/opt/siyab` and `/opt/siyab/demo/site` |
| Environment file | `/etc/shieldbot-near/env` |
| Shared state directory | `/var/lib/shieldbot-near/siyab` |
| Accounts, teams, cases, watches, inbox and audit | `workspace.sqlite3` in the state directory |
| Public account observations, graph/event evidence and polling progress | `history.sqlite3` in the state directory |
| Retained-feed indexing progress and retry leases | `monitor.sqlite3` in the state directory |

The state directory must be outside the source checkout and generated site. The service user is `shieldbot-near`; give that user ownership of the directory, mode 0700, and keep database/backup files private. SQLite databases use WAL, so sidecar files may exist. Do not publish the state directory as a static asset or copy it into a release archive.

`FASTNEAR_API_KEY_FILE` names a provider credential file outside the repository. Its contents must be readable by the service user without being placed in command arguments, browser code or logs. `FEED_STATE_DIR` names the existing external feed state directory; the retained-feed monitor reads its `events` subdirectory. The account reader also uses the configured mainnet RPC endpoint where applicable. Keep existing environment names and deployed contract identifiers unchanged.

The three commands are:

```sh
python -m intel serve --host 127.0.0.1 --port 8792 --site demo/site --live --state-dir /var/lib/shieldbot-near/siyab --workspace-origin https://siyab.xyz
python -m intel monitor --state-dir /var/lib/shieldbot-near/siyab
python -m intel history-follow --state-dir /var/lib/shieldbot-near/siyab
```

Use `--once` on either worker for a bounded pass. That flag does not wait for all watched accounts to be polled. The API requires `--state-dir` for shared workspaces and durable account history; `--live` enables live account reads. An explicit localhost HTTP workspace origin is supported for local development; production uses the exact HTTPS origin.

After release review, install the matching `deploy/siyab-api.service`, `deploy/siyab-monitor.service` and `deploy/siyab-history.service` templates in systemd. They use the external environment file and shared state directory, restart on failure, run without root, and restrict filesystem writes to that directory. Create the state directory before starting them. The monitor reads local feed evidence; the API and history worker need outbound provider network access.

The reverse proxy must send `/workspace-api/` and `/live/` to the loopback API while preserving their paths. Do not cache workspace responses. Overwrite `X-Forwarded-For` with the actual client address rather than passing client-supplied values through, because rate limits use that address behind a loopback proxy. Keep the public site origin aligned with `--workspace-origin`.

## Worker coverage and limits

The workers serve different sources; one healthy heartbeat does not establish that the other worker is healthy.

- The retained-feed monitor indexes existing evidence files in bounded batches of 200. Initial retained files are backfill candidates; they do not represent newly observed external deliveries. Missing or unreadable evidence remains visible in retry/coverage status.
- Ordinary-transfer polling handles at most one watched account per five-second global tick. A successfully polled account is not due again for at least 60 seconds. Each account poll reads at most two pages of 100 transfers within a 20-second provider deadline. Larger watchlists, provider latency and retries increase revisit time.
- Initial ordinary-transfer observations seed archive rows and deduplication state without enqueueing historical inbox events. Later unseen rows can enter matching team inboxes. If the previous anchor is not found within the bounded pages, the account records a coverage gap. A running process does not clear that gap or establish complete event recovery.
- Monitor status exposes retained-feed health separately from `account_polling`, including heartbeat, last successful poll, initialized accounts, retries, gaps, backfills and pending event delivery. An empty inbox does not establish absence of activity or risk.

Delivery is persistent storage in the team's inbox, which can be read after reopening the browser. Email, chat, webhook and closed-browser desktop notification delivery are not configured by these workers. The separate alert library/test command does not imply that team inbox events have an external delivery integration.

The workspace API allows 120 requests per client per minute and accepts JSON mutations up to 32 KiB. Authentication has tighter limits: 10 requests per minute per client, with registration and recovery each capped at five per hour per client. The live API's default is 60 requests per client per minute with at most eight in flight. Provider plans impose their own additional limits. Requests can return unavailable, busy or rate-limited responses; these must remain visible.

## Backup and recovery operations

For a transactionally consistent workspace-only backup, choose a new external destination:

```sh
python -m intel workspace-backup --state-dir /var/lib/shieldbot-near/siyab --out /var/backups/siyab/workspace-YYYYMMDD-HHMMSS.sqlite3
```

Replace the timestamp before running. The destination must be outside the repository and must not already exist. The command reserves the path exclusively and uses SQLite's backup API. It includes private account/team state, sessions, recovery hashes and notes; restrict access accordingly. It does not include `history.sqlite3`, `monitor.sqlite3`, external feed evidence or browser-only data.

For a full state snapshot, stop the API and both workers, then copy the entire external state directory, including any WAL/SHM sidecars, before restarting. Retain the external feed evidence separately if replay of retained-feed files is required. Keep release code and state backups distinct.

To restore, stop all three services and restore into a separate external directory. Check each restored SQLite database with `PRAGMA integrity_check`, restore the service user's ownership and private permissions, and point all three service commands to the same restored directory before restarting. Retain the previous state directory for rollback. Restoring workspace state also restores session and recovery hashes from that snapshot; do not assume a code rollback revokes sessions.

Team JSON exports are portable case/watch backups, not full account, inbox or audit backups. Browser-only JSON exports remain separate and are the user's responsibility. Neither the shared platform nor these operational commands change financial signing, Gate policy, testnet contract configuration or mainnet write authorization.
