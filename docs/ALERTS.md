# Alerts library

`intel.alerts` provides immutable `Alert` values, a JSON stdout sink, a signed HTTPS webhook sink, and a receiver-side signature verifier. It uses only the Python standard library and is not connected to `intel/feed.py` or `gate/agent.py`.

An alert has `source`, `severity`, `subject`, `ref`, `title`, `detail`, `evidence_sha256`, and `created_ms` fields. Severity is `info`, `warn`, or `critical`. Its dedupe key is `(source, subject, ref)`.

`WebhookSink(url, secret_path)` accepts HTTPS URLs only and builds a urllib opener with a redirect handler that raises. It sends compact UTF-8 JSON with a five second timeout and rejects payloads above 16 KiB. The `X-ShieldBot-Signature` header has the form `t=<unix seconds>,v1=<hex HMAC-SHA256>`. The HMAC covers the ASCII timestamp, a period, and the exact request body bytes. The secret file is read when `send` runs; surrounding whitespace is stripped.

`verify(signature_header, body, secret, now, window_s=300)` checks the same HMAC and rejects timestamps more than 300 seconds before or after `now` by default. Pass the body bytes and secret bytes used by the receiver.

Webhook delivery makes at most three attempts. Failed attempts wait one second and then two seconds before the next attempt. The backoff callable can be injected into `WebhookSink` for offline tests. Each attempt appends one JSON line to the delivery log. The default path is `~/.shieldbot/alert-deliveries.jsonl`; pass `log_path` to select another path. Entries record only the HTTPS origin, attempt number, timestamp, status, result, and exception class. They omit the secret, payload, and URL query string.

`send(alert, sinks)` calls each sink and returns one result per sink. A sink exception is returned as a failed result and does not escape the helper. `StdoutSink` writes one JSON alert per call.

Set `SHIELDBOT_ALERT_SECRET_FILE` to the webhook secret file, then run `python -m intel alert-test --url https://alerts.example.test/hook` to send one test alert.
