# Alerts library

`intel.alerts` provides immutable `Alert` values, a JSON stdout sink, a signed HTTPS webhook sink, and a receiver-side signature verifier. It uses only the Python standard library and is not connected to `intel/feed.py` or `gate/agent.py`.

An alert has `source`, `severity`, `subject`, `ref`, `title`, `detail`, `evidence_sha256`, and `created_ms` fields. Severity is `info`, `warn`, or `critical`. Its dedupe key is `(source, subject, ref)`. An `Alert` refuses at construction when `source`, `subject`, `ref` or `title` is empty, when `evidence_sha256` is not 64 lowercase hex characters, when `created_ms` is not a non negative integer, or when the encoded payload would exceed 16 KiB. The caller learns at the call site rather than losing an alert later.

`WebhookSink(url, secret_path)` accepts HTTPS URLs only and builds a urllib opener with a redirect handler that raises. It sends compact UTF-8 JSON with a five second timeout. The `X-ShieldBot-Signature` header has the form `t=<unix seconds>,v1=<hex HMAC-SHA256>`. The HMAC covers the ASCII timestamp, a period, and the exact request body bytes. The secret file is read when `send` runs; surrounding whitespace is stripped and a secret shorter than 32 bytes is refused, so the signature cannot be decoration.

`verify(signature_header, body, secret, now, window_s=300)` checks the same HMAC and rejects timestamps more than 300 seconds before or after `now` by default. Pass the body bytes and secret bytes used by the receiver.

Webhook delivery makes at most three attempts. A 408, a 425, a 429, a 5xx, a connection error and a timeout are retried; any other 4xx is a permanent answer and ends delivery after one attempt, as do a refused redirect and any failure that is not a transport failure, such as a refused TLS certificate or an exception raised by a misconfigured opener. Failed attempts wait one second and then two seconds before the next attempt, or honour a `Retry-After` header (seconds or an HTTP date) capped at 30 seconds. The backoff callable can be injected into `WebhookSink` for offline tests. Each attempt appends one JSON line to the delivery log. The default path is `~/.shieldbot/alert-deliveries.jsonl`; pass `log_path` to select another path. Entries record only the HTTPS origin, attempt number, timestamp, status, result, and exception class. They omit the secret, payload, and URL query string.

`send(alert, sinks)` calls each sink and returns one result per sink. A sink exception is returned as a failed result and does not escape the helper. `StdoutSink` writes one JSON alert per call.

Set `SHIELDBOT_ALERT_SECRET_FILE` to the webhook secret file, then run `python -m intel alert-test --url https://alerts.example.test/hook` to send one test alert.
