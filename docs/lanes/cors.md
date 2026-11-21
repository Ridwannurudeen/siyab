# Public read CORS

Lane CORS, 21 Nov 2026. Changes are left uncommitted on `feat/cors-live` from `0e113268c`.

## Cause

At 14:56 UTC, a public read of `https://siyab.xyz/live/account-check/aurora` with `Origin: https://dapp.example` returned 200 without `Access-Control-Allow-Origin`. An OPTIONS request with that Origin and `Access-Control-Request-Method: GET` returned 403 with nginx's HTML error body.

`intel/api.py` had no `do_OPTIONS`. Python 3.12.10's installed `BaseHTTPRequestHandler.handle_one_request` returns 501 for an unimplemented method. The live 403 therefore comes from an additional edge restriction or error mapping, not this handler's default response. The exact live nginx directive was not inspected.

## Application policy

The explicit allowlist in `intel/api.py` covers these anonymous reads:

| Path | Existing implementation |
| --- | --- |
| `/live/verdict`, `/live/account-check/<account>`, `/live/token-check/<token>` | Unsigned checks, with existing feature flags and validation |
| `/live/scan/<account>` | Access key inspection |
| `/live/intents` | Quote judgement |
| `/live/reports`, `/live/reports/recent`, `/live/health` | Public contract views and service status |
| `/live/entity-directory` | Sourced directory search |
| `/live/ask` | Anonymous verdict projection returning `sign_in_required`; no model call or answer cache |
| `/live/accounts/<account>/identity` | Contract identity inspection |
| `/live/accounts/<account>/history`, `/live/accounts/<account>/portfolio-history` | Retained public observation reads |
| `/live/accounts/<account>/enrichment` | Sourced directory lookup |
| `/live/account-data/<hash>.json`, `/live/portfolio-data/<hash>.json` | Public snapshot bytes, with one lowercase SHA-256 identifier |

These are the implemented route shapes in `LiveAPI._route`, `_account_read`, `_ask` and `exact_snapshot`. The retained getters only read stored rows. They do not authenticate a workspace session. Account identifiers and query values still pass the existing route validation.

GET and HEAD responses on these paths carry `Access-Control-Allow-Origin: *` and `Access-Control-Expose-Headers: Retry-After`, including application errors. Headers are constant even without Origin, so no `Vary: Origin` is added. `Retry-After` lets a browser read the existing rate and capacity retry interval. No response includes `Access-Control-Allow-Credentials`.

A preflight needs Origin and exactly one requested method of GET, HEAD or OPTIONS. It returns 204 with an empty body, `Access-Control-Allow-Methods: GET, HEAD, OPTIONS` and `Access-Control-Max-Age: 600`. The response has no Content-Length or Content-Type. An absent or empty `Access-Control-Request-Headers` needs no `Access-Control-Allow-Headers`. A request for only Content-Type, case insensitive and with surrounding whitespace permitted, receives `Access-Control-Allow-Headers: Content-Type`. This supports wrappers that set `Content-Type: application/json` on GET. Every other requested header, including `Content-Type, X-Other`, is refused with 403 and no CORS headers. A normal GET can use the browser's default headers or `Accept: application/json`; it needs no Content-Type, Authorization or CSRF header.

Adding `do_OPTIONS` changes OPTIONS on every application path from the former default 501 to 403, except allowed public preflights which return 204. Missing Origin, duplicate requested method headers, private paths and unknown paths return 403 without CORS headers.

OPTIONS does not execute a live read, authenticate a session, access a key, call a model or consume a GET rate unit. Existing connection limits still apply. GET and HEAD keep their existing rate, cost, queue, error, body and cache behavior. Hash-verified snapshots keep their existing immutable cache header; other application responses remain `no-store`.

All `/workspace-api` routes, every POST and every unknown route have no CORS permission. Refused preflights return 403 without CORS headers. The allowlist also excludes GET investigation routes that write observations: `profile`, `holdings`, `transactions`, `transfers`, `graph`, `backfill`, `portfolio` and `portfolio-at-block`. `/live/account-events/<id>.json` stays excluded because team inbox events are private. Static pages and the separate local Gate HTTP service are outside this policy.

Path variant tests preserve existing routing. `/live/health/` and uppercase snapshot identifiers have no CORS permission and their preflights return 403. Python normalizes `//live/health` to `/live/health`, and absolute form `http://x/live/health` selects the same public health path. Both retain public read permission. `/live/accounts/aurora%2Fprofile/history` matches a public history route shape, so its preflight succeeds, but the GET and HEAD reject the decoded account with 400. It cannot select the excluded profile route or write observations.

## Proxy and owner action

Simple cross-origin GETs work with the application change alone. The nginx OPTIONS change only matters for preflights. The example now permits GET, HEAD and OPTIONS in the `/live/` location with `limit_except GET OPTIONS { deny all; }`. Workspace locations are unchanged. The application still decides which OPTIONS requests succeed.

The example's `/live/` location sends edge 429s from its existing `limit_req` and `limit_conn` limits to `@siyab_live_limited`. That named location returns constant JSON with status 429, wildcard origin permission, `Access-Control-Expose-Headers: Retry-After` and `Retry-After: 60`. The headers use `always` because 429 is outside nginx's default add_header status list. No CORS add_header is present on the normal proxied `/live/` location: the application already sends those headers, and duplicate origin permission breaks browser CORS checks. Do not enable proxy error interception; application error responses retain their existing body and headers.

After independent review and owner approval, the operator must:

1. Inspect the effective live restriction with `nginx -T | grep -nE 'limit_except|request_method|return 403|deny'`. Find the directive that produces the current OPTIONS 403. Its exact live form has not been inspected.
2. Roll out the reviewed application. Allow OPTIONS in `/live/` only by adapting that live directive, for example to `limit_except GET OPTIONS { deny all; }`. A server level request method condition runs before location selection, so scope it accordingly. Preserve workspace protections, other write restrictions and the existing rate and connection limits.
3. Add `error_page 429 = @siyab_live_limited;` inside `/live/` and the named JSON error location from the example. Keep normal proxied responses free of CORS add_header directives. Confirm the effective configuration does not intercept upstream errors for this location.
4. Purge `/live/account-data/<hash>.json` and `/live/portfolio-data/<hash>.json` snapshot paths from any CDN at rollout. They are served with `immutable`, so previously cached responses can retain the missing CORS headers until expiry unless purged.
5. Run `nginx -t`, then `systemctl reload nginx`. Run the read-only probes below and a cross-origin browser GET. In a controlled staging or local nginx instance with the same location configuration, trigger each edge limit and confirm 429 JSON, exactly one wildcard origin header, exposed Retry-After and Retry-After 60. Do not load test production to manufacture a 429.

No deployment, nginx reload, CDN purge or remote configuration change was performed. No transaction or paid call is needed to enable this change.

The first two probes must return 204 with no Content-Type or Content-Length; the second also allows Content-Type. The GET must return its existing check response with exactly one wildcard origin header. The last three must return 403 without CORS headers.

```sh
curl -i -X OPTIONS 'https://siyab.xyz/live/account-check/aurora' -H 'Origin: https://dapp.example' -H 'Access-Control-Request-Method: GET'
curl -i -X OPTIONS 'https://siyab.xyz/live/account-check/aurora' -H 'Origin: https://dapp.example' -H 'Access-Control-Request-Method: GET' -H 'Access-Control-Request-Headers: Content-Type'
curl -i 'https://siyab.xyz/live/account-check/aurora' -H 'Origin: https://dapp.example'
curl -i -X OPTIONS 'https://siyab.xyz/workspace-api/session' -H 'Origin: https://dapp.example' -H 'Access-Control-Request-Method: GET'
curl -i -X OPTIONS 'https://siyab.xyz/live/verdict' -H 'Origin: https://dapp.example' -H 'Access-Control-Request-Method: POST'
curl -i -X OPTIONS 'https://siyab.xyz/live/health' -H 'Origin: https://dapp.example' -H 'Access-Control-Request-Method: GET' -H 'Access-Control-Request-Headers: Content-Type, X-Other'
```

Browser callers use an unsigned check and omit credentials:

```js
const response = await fetch("https://siyab.xyz/live/account-check/aurora", {
  credentials: "omit",
});
const check = await response.json();
```

An unsigned ALLOW is still advice. CORS adds no authority to pay.

## Verification

Fix round 1 first ran the two new preflight regressions against the earlier implementation. Both test methods failed, with 146 subtest failures confirming Content-Type rejection and the unwanted Content-Type on 204 answers. After the fix, all 21 tests in `tests/test_cors.py` passed with no skips. They cover GET and HEAD over every public allowlist entry, the path variants above, OPTIONS without Origin, two separate requested method headers, empty requested header values, HEAD errors, Content-Type case and whitespace handling and mixed-header refusal, alongside the original read, error, rate, cache and workspace cases.

All ten other test modules importing `intel.api` passed 225 tests with no skips. The final focused total is 246 tests, all passing. The full-tree hygiene check and `git diff --check` also passed. Commands:

```sh
python -m unittest tests.test_cors
python -m unittest tests.test_analytics_api tests.test_api tests.test_feed tests.test_feed_workspace tests.test_graph_cache tests.test_live_api tests.test_live_limits tests.test_observation_http tests.test_trace tests.test_workspace_http
python scripts/check_hygiene.py --all
git diff --check
```

The initial lane run also passed seven local cross-origin Playwright browser checks: readable GET 200, HEAD 200 and rate error 429 with Retry-After 60, and browser refusal of workspace, POST, custom-header preflight and credentialed reads. Those browser checks were not repeated in fix round 1. The temporary browser page and both local servers were closed. A focused independent review of this round found no required fixes. The full suite is left to the orchestrator as requested.

The nginx directives were checked against the official documentation below. Local `nginx -t` could not run because nginx is absent from both the Windows PATH and the default WSL distribution. The operator must validate the adapted live configuration before reload.

## Primary sources

Verified on 21 Nov 2026:

* [Fetch CORS protocol](https://fetch.spec.whatwg.org/#http-cors-protocol): Origin, preflight method and request header declarations, permission, expiry and response header exposure.
* [Fetch credentials](https://fetch.spec.whatwg.org/#cors-protocol-and-credentials): wildcard origin does not grant credentialed browser reads.
* [Fetch CORS cache behavior](https://fetch.spec.whatwg.org/#cors-protocol-and-http-caches): a constant wildcard is sent on requests with and without Origin, without adding Vary.
* [Python 3.12.10 HTTP handler source](https://github.com/python/cpython/blob/v3.12.10/Lib/http/server.py): leading double slashes normalize to one slash before dispatch, and an unimplemented method returns 501. The installed source was also inspected.
* [HTTP absolute request targets](https://www.rfc-editor.org/rfc/rfc9112.html#section-3.2.2): servers must accept absolute form request targets. The application's existing parser selects their path for routing and CORS.
* [HTTP Content-Length](https://httpwg.org/specs/rfc9110.html#field.content-length): a 204 response must not send Content-Length.
* [nginx proxy method](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_method) and [request headers](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass_request_headers): forwarding keeps the client method unless replaced and forwards request headers by default.
* [nginx method restrictions](https://nginx.org/en/docs/http/ngx_http_core_module.html#limit_except): OPTIONS can be allowed independently; allowing GET also allows HEAD.
* [nginx named error processing](https://nginx.org/en/docs/http/ngx_http_core_module.html#error_page): a location scoped error_page can dispatch errors to a named location without changing the method or URI.
* [nginx response headers](https://nginx.org/en/docs/http/ngx_http_headers_module.html#add_header): always adds headers for every status, including 429.
* [nginx upstream error interception](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_intercept_errors): interception is off by default; enabling it sends upstream errors through error_page processing.
* [nginx request rejection status](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html#limit_req_status) and [connection rejection status](https://nginx.org/en/docs/http/ngx_http_limit_conn_module.html#limit_conn_status): both support the example's configured 429 status.
* [nginx constant responses and processing order](https://nginx.org/en/docs/http/ngx_http_rewrite_module.html#return): return supports a status and response text; server rewrite directives run before location selection.
* [HTTP Retry-After](https://httpwg.org/specs/rfc9110.html#field.retry-after): a nonnegative integer specifies delay seconds.
* [HTTP immutable responses](https://httpwg.org/specs/rfc8246.html#section-2): fresh immutable responses need not be revalidated, which requires the rollout purge above to refresh cached headers.
* [Public hosted check](https://siyab.xyz/live/account-check/aurora): live GET and OPTIONS observations recorded above.
