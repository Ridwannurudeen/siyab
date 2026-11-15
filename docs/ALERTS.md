# Alerts library

`intel.alerts` provides immutable `Alert` values, a JSON stdout sink, a signed HTTPS webhook sink, a Telegram broadcast sink, and a receiver-side signature verifier. The core alert and webhook code uses the Python standard library; broadcast reuses the existing delivery client. The feed sends an alert for each new measured event, and `gate pay` sends one for a DENY or a human ask on UNKNOWN.

An alert has `source`, `severity`, `subject`, `ref`, `title`, `detail`, `evidence_sha256`, and `created_ms` fields. Severity is `info`, `warn`, or `critical`. Its dedupe key is `(source, subject, ref)`. An `Alert` refuses at construction when `source`, `subject`, `ref` or `title` is empty, when `evidence_sha256` is not 64 lowercase hex characters, when `created_ms` is not a non negative integer, or when the encoded payload would exceed 16 KiB. The caller learns at the call site rather than losing an alert later.

`WebhookSink(url, secret_path)` accepts HTTPS URLs only and builds a urllib opener with a redirect handler that raises. It sends compact UTF-8 JSON with a five second timeout. The `X-ShieldBot-Signature` header has the form `t=<unix seconds>,v1=<hex HMAC-SHA256>`. The HMAC covers the ASCII timestamp, a period, and the exact request body bytes. The secret file is read when `send` runs; surrounding whitespace is stripped and a secret shorter than 32 bytes is refused, so the signature cannot be decoration.

`verify(signature_header, body, secret, now, window_s=300)` checks the same HMAC and rejects timestamps more than 300 seconds before or after `now` by default. Pass the body bytes and secret bytes used by the receiver.

Webhook delivery makes at most three attempts. A 408, a 425, a 429, a 5xx, a connection error and a timeout are retried; any other 4xx is a permanent answer and ends delivery after one attempt, as do a refused redirect and any failure that is not a transport failure, such as a refused TLS certificate or an exception raised by a misconfigured opener. Failed attempts wait one second and then two seconds before the next attempt, or honour a `Retry-After` header (seconds or an HTTP date) capped at 30 seconds. The backoff callable can be injected into `WebhookSink` for offline tests. Each attempt appends one JSON line to the delivery log. The default path is `~/.shieldbot/alert-deliveries.jsonl`; pass `log_path` to select another path. Entries record only the HTTPS origin, attempt number, timestamp, status, result, and exception class. They omit the secret, payload, and URL query string.

`send(alert, sinks)` calls each sink and returns one result per sink. A sink exception is returned as a failed result and does not escape the helper. `StdoutSink` writes one JSON alert per call.

Alerts are off by default. To enable webhook delivery, set `SHIELDBOT_ALERT_URL_FILE` to a file containing one HTTPS URL on one line, and set `SHIELDBOT_ALERT_SECRET_FILE` to the file holding the signing secret. The secret is read when an alert is sent. If the URL file is missing, unreadable, or does not contain one valid HTTPS URL, no webhook sink is built. With only `SHIELDBOT_ALERT_STDOUT=1` set, alerts go to `StdoutSink`; this is intended for local use.

Feed alerts use source `feed`, severity `warn`, the event subject and signal, the event detail, the evidence document's sha256, and the event timestamp. Replaying a block height does not create another alert, and the large transfer poll asks non-overlapping time windows. Payment alerts use source `payment`, severity `critical` for DENY and `warn` for UNKNOWN when it asks a human; each carries the verdict evidence hash. They are sent after the verdict is final. Sink failures are logged and returned by `send`; they do not change the verdict or `gate pay` exit code.

Set `SHIELDBOT_ALERT_SECRET_FILE` for the webhook secret, then run `python -m intel alert-test --url https://alerts.example.test/hook` to send one test alert.

## Telegram broadcast

The public channel is Siyab Alerts, https://t.me/siyabalerts; the operator posts incidents there with `python -m intel incident-broadcast <id>`. Measured feed signals are posted only by this repository's feed running `python -m intel feed --follow` with `SIYAB_TELEGRAM_BROADCAST_CHAT` set; the hosted mainnet feed unit runs from the separate `/opt/shieldbot-near` checkout, which releases neither move nor restore (`deploy/shieldbot-near-feed.service:10`), so this repository does not establish automatic feed posts to the channel. Broadcast is separate from owner-confirmed private workspace delivery described in `docs/WORKSPACES.md`.

`SIYAB_TELEGRAM_BROADCAST_CHAT` must be a numeric channel or supergroup id matching `-100[0-9]{6,13}`. An unset or invalid value leaves broadcast off. An `@username` is refused because it can move to another chat. Credentials use the existing external file path in `SIYAB_TELEGRAM_TOKEN_FILE`. Webhook or stdout selection remains independent of broadcast configuration.

Only alerts with source `feed` or `incident` are broadcast; payment alerts are never posted. Only live follow walks broadcast feed alerts; replay and record walks never post. The five measured feed signals can raise alerts. `full_access_key_then_outflow` remains unmeasured and raises none. Incidents are operator posted with `python -m intel incident-broadcast <id>`; ids match `[a-z0-9-]{1,64}` and select a committed record in `intel/incidents/`. An eligible run attempts one message using the record's title, date, source publishers and JSON record file bytes hash.

Incident posts use a local SQLite ledger, `incident-broadcast.sqlite3`, under `FEED_STATE_DIR`, the existing feed state directory (default `intel/feed/`). Each successful incident id and exact record file bytes SHA-256 pair retains its latest successful send time in Unix milliseconds, numeric channel id and Telegram message id when returned. A repeat of that pair sends nothing, prints that it was already broadcast with a `--repost` hint, and exits 3, even if the configured channel has changed. `python -m intel incident-broadcast <id> --repost` explicitly permits another attempt and updates that pair's ledger entry only on success. Changed file bytes, including whitespace changes, produce a new hash eligible for one post; earlier hashes remain recorded, so reverting to an earlier sent version is refused too. Concurrent runs sharing this ledger serialize the duplicate check, send and record update.

The command exits 0 after a successful post and ledger commit, 2 when broadcast is unconfigured, and 1 for an invalid or unreadable incident, delivery failure or ledger error. Network errors, unreadable replies, cooldown and budget refusals do not mark a record as sent; a failed repost keeps the previous successful entry. An unavailable or damaged ledger refuses before posting, including with `--repost`. If Telegram confirms the post but saving the ledger fails, the command exits 1 and reports that it was sent but the ledger could not be saved. A process crash after Telegram accepts a message but before the ledger commits, or a lost success reply, can leave a post unrecorded; inspect the channel before retrying such an attempt. Deduplication covers successful sends retained in this local ledger only: it does not import earlier channel posts, and changing or deleting the ledger loses that history. Keep `FEED_STATE_DIR` on persistent operator storage. The existing broadcast interval and daily limits also apply to `--repost`.

Posts are plain text with no `parse_mode`. The head has one `pre` entity containing the feed signal and account or incident title, detail cut to 600 characters, observed UTC time for feed alerts or posting UTC time for incidents, and evidence hash. The Evidence value is the SHA-256 of the feed event JSON record file bytes or the committed incident JSON record file bytes, not the linked HTML page. The page link and this notice stay outside that entity: "Unsigned advice. A signal is an observation, not a verdict. Unknown is never a pass." Feed posts link to `https://siyab.xyz/feed.html`; incident posts link to `https://siyab.xyz/incidents/<id>.html`. Client rendering of the entity and footer is unverified here; confirm it on the channel's first post.

Broadcast is best effort: the feed walk sends synchronously, makes one attempt under the client's 15 second deadline, and has no retry or queue. A burst inside 3 seconds is dropped. A process allows at most 200 posts per UTC day; interval and daily counters are shared by its sinks and reset on process restart. A `provider_429` response moves the next permitted send by its `retry_after`. Cooldown and daily-cap refusals send nothing and return `broadcast_cooldown` or `broadcast_budget`. A reply naming a different chat fails as `response_unreadable`; missing or unreadable credentials retain the client's error code. The feed page is the record; missing broadcast posts do not establish that no event occurred.

### One-time record of the earlier Rhea post

The `rhea` incident was posted to the live channel on 14 Nov 2026 at 17:31 UTC, before the ledger existed. Record that post once before running another incident broadcast. Run the following commands as the service user, from the checkout containing the ledger code. Replace every angle-bracket placeholder first. You must export the service's actual `FEED_STATE_DIR`, an absolute directory on persistent storage outside the code tree; otherwise the default puts the ledger in `intel/feed/` in the checkout.

```sh
sudo -u shieldbot-near -H bash
cd /opt/siyab
export FEED_STATE_DIR='<SERVICE_FEED_STATE_DIR>'
python -c 'import hashlib; from pathlib import Path; print(hashlib.sha256(Path("intel/incidents/rhea.json").read_bytes()).hexdigest())'
```

First compare the printed hash with the Evidence line of the 14 Nov post. Continue only if they match; do not record the current file hash for a different version of the post. Take the numeric channel id and message id from that post's `t.me` link. For a `t.me/c/<channel-number>/<message-id>` link, the numeric channel id is `-100<channel-number>`. A public `t.me/siyabalerts/<message-id>` link supplies only the message id; use the service's numeric broadcast channel id and verify it identifies that same channel.

Still in that service-user shell, insert the row with the commands below. The `CREATE TABLE IF NOT EXISTS` statement is quoted from `intel/__main__.py`, `_incident_broadcast`; keep this copy in sync with that code. The command computes the committed JSON file bytes hash again and converts `2026-11-14T17:31Z` to Unix milliseconds. A plain `INSERT` refuses an existing row rather than replacing it.

```sh
python -c 'import hashlib, os, sqlite3; from datetime import datetime; from pathlib import Path
channel_id = int("<NUMERIC_CHANNEL_ID_FROM_POST>")
message_id = int("<MESSAGE_ID_FROM_POST_LINK>")
record_sha256 = hashlib.sha256(Path("intel/incidents/rhea.json").read_bytes()).hexdigest()
sent_ms = int(datetime.fromisoformat("2026-11-14T17:31Z").timestamp() * 1000)
state = Path(os.environ["FEED_STATE_DIR"])
state.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(state / "incident-broadcast.sqlite3", timeout=30) as connection:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("CREATE TABLE IF NOT EXISTS incident_broadcasts ("
                       "incident_id TEXT NOT NULL,record_sha256 TEXT NOT NULL,sent_ms INTEGER NOT NULL,"
                       "channel_id INTEGER NOT NULL,message_id INTEGER,PRIMARY KEY(incident_id,record_sha256))")
    connection.execute("INSERT INTO incident_broadcasts(incident_id,record_sha256,sent_ms,channel_id,message_id) VALUES(?,?,?,?,?)",
                       ("rhea", record_sha256, sent_ms, channel_id, message_id))'
```

With the service's existing broadcast configuration available in the same shell and the same `FEED_STATE_DIR` still exported, check the duplicate refusal:

```sh
python -m intel incident-broadcast rhea
test "$?" -eq 3
```

`python -m intel incident-broadcast rhea` must exit 3 and report that it was already broadcast, without posting a message. The `test` command exits 0 only for that expected status. Do not use `--repost` for this check.
