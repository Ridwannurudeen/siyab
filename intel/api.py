"""A read only JSON API over the committed files, on the standard library http.server.

Routes (GET only; without --live, no route opens the network):

    /health               the label store's size and sha256
    /labels/<subject>     every assertion on the subject, on any chain
    /incidents/<id>       the incident file intel/incidents/<id>.json
    /evidence/<hash>      an evidence document from fixtures/evidence/ or intel/feed/events/
    /feed                 the feed state without its fresh accounts and the last 50 events, or null
                          and an empty list, with follower: "running" for a follower's state, or
                          "stopped" with lag_blocks null once its last update (updated_at, else
                          last_time, else started_at) is more than FOLLOWER_STOPPED_AFTER_S old;
                          null for any other state; event_ids parallels events with their evidence-file
                          hashes, or null where a legacy filename is not a lowercase SHA-256 identifier
    /feed.html            with FEED_STATE_DIR naming a follower's state, the feed page built from it,
                          which gives a stopped follower's last update time and says the lag is
                          unknown; else the static file as below
    /flow/<subject>       the exit trace intel/flows/<subject>.json, else the trace file under
                          intel/flows/ whose subject it is (such as rhea-collector.json); 403 for a
                          subject that carries no incident label, 404 when no trace was written
    /cluster/<subject>    the cluster file under intel/clusters/ whose seeds hold the subject; 403
                          for a subject that carries no incident label, 404 when no cluster holds it
    /issuers              the issuer status rows and history rows of intel/issuers/, the number of
                          status reads and of the account ids they cover, and the history coverage
    /search/<term>        the entries of demo/site/search.json whose subject, name or one of whose labels
                          holds the term, ignoring case, each with the path of its page
    /accounts/<subject>   every current assertion on the subject, on any chain; 404 for a subject with none
    /entities/<id>        the registry entity <id> of intel/entities.jsonl; 404 when there is none
    /live/...              live mainnet checks and testnet reports, only with --live
    anything else         a static file under --site when one is given; a path whose last segment
                          ends in .html is never an API route, so /incidents/<id>.html is the page
                          and /incidents/<id> the JSON; a path that is neither answers 404 with the
                          site's 404.html when it has one, else with JSON

Every answer is JSON (static files aside) and carries Cache-Control: no-store, except successful
static /graph-data/<sha256>.json and /assets/<sha256>.js reads whose bytes match the filename hash;
these are public and immutable.
"""

import dataclasses
import hashlib
import io
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gate import incident
from intel import feed, issuers, labels, render, trace

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIRS = (ROOT / "fixtures" / "evidence", ROOT / "intel" / "feed" / "events")
FEED_STATE = ROOT / "intel" / "feed" / "state.json"
SEARCH_INDEX = ROOT / "demo" / "site" / "search.json"
FLOWS_DIR = ROOT / "intel" / "flows"
CLUSTERS_DIR = ROOT / "intel" / "clusters"
FEED_LIMIT = 50
WORKSPACE_BODY_TIMEOUT_S = 5
# The follower stamps updated_at at every height and at every read of the final height while it waits
# there, and, once its run has done either, at the end of every transfer poll; a run that has done
# neither, such as a restart on a refused key or one whose first block fetch fails, stamps nothing, so
# a crash loop never refreshes the stamp. With every request answered inside its rpc.TIMEOUT_S (30 s),
# a poll is one page each for 23 of the 24 incident and denylist subjects and all MAX_TRANSFER_PAGES
# (20) for the last, 43 requests FOLLOW_INTERVAL_S (0.4 s) apart, 43 x 30 + 42 x 0.4 = 1306.8 s; a
# final read is two requests since neardata redirects it, with one 429 back off (BACKOFF_S, 10 s),
# 2 x 30 + 10 + 2 x 30 = 130 s; a block fetch with one back off is 30 + 10 + 30 = 70 s. A poll that
# falls due during the final read after the 1 s wait at the final height runs before the next fetch,
# so the longest gap between two stamps of a running follower is that wait, that read, a 0.4 s pace
# and the poll: 1 + 130 + 0.4 + 1306.8 = 1438.2 s. A poll due at the wait itself gives
# 1 + 1306.8 = 1307.8 s. At that speed a poll outlasts POLL_S (600 s), so the gap after a poll's own
# stamp can hold a final read, a pace and a second poll that fell due during the first:
# 130 + 0.4 + 1306.8 = 1437.2 s. 1600 s clears 1438.2 s with room for five more subjects. Not covered:
# the polls a run makes before its first stamp, which at that speed can be two before its first fetch
# (the first stamp about 2206 s after the restart), so a restarted follower can read stopped for about
# 1000 s; a second subject paging to the cap at that speed; and an unbroken run of 429 answers, which
# Fetcher.get waits out without end.
FOLLOWER_STOPPED_AFTER_S = 1600
JSON_TYPE = "application/json; charset=utf-8"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PUBLIC_CORS_PATH = re.compile(
    r"/live/(?:verdict|intents|health|reports(?:/recent)?|entity-directory|ask"
    r"|(?:scan|account-check|token-check)/[^/]+"
    r"|accounts/[^/]+/(?:identity|history|enrichment|portfolio-history)"
    r"|(?:account-data|portfolio-data)/[0-9a-f]{64}\.json)"
)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _labels(store, subject):
    rows = [dataclasses.asdict(a) for a in store.assertions if a.subject == subject]
    return 200, {"subject": subject, "assertions": rows}


def _incident(incident_id):
    try:
        return 200, _read_json(incident.path_for(incident_id))
    except ValueError:
        return 404, {"error": f"no incident {incident_id!r}"}


def _evidence(evidence_hash):
    if not _HEX64.fullmatch(evidence_hash):
        return 404, {"error": "an evidence hash is 64 lowercase hex"}
    for directory in EVIDENCE_DIRS:
        path = directory / f"{evidence_hash}.json"
        if path.is_file():
            return 200, _read_json(path)
    state_dir = os.environ.get("FEED_STATE_DIR")
    if state_dir is not None:
        directory = (Path(state_dir) / "events").resolve()
        path = directory / f"{evidence_hash}.json"
        if path.is_file() and path.resolve().parent == directory:
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() == evidence_hash:
                return 200, json.loads(data)
    return 404, {"error": f"no evidence document {evidence_hash}"}


def _timestamp(value):
    """The POSIX time of a _TIME string, or None for anything else, a null or malformed value included."""
    try:
        return datetime.strptime(value, feed._TIME).replace(tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None


def _stopped_since(state, now):
    """The time of a follower's last update when that is more than FOLLOWER_STOPPED_AFTER_S before now, else
    None. The last update is updated_at; for a state without a readable one, last_time, the chain time of the
    last height it walked, which the transfer poll never moves (older than the walk itself while the follower
    catches up, so such a state reads stopped sooner); else started_at, when the follower first started. A
    state with none of the three readable reads stopped since an unknown time. The state file's mtime is never
    read, since the transfer poll rewrites it."""
    written = _timestamp(state.get("updated_at"))
    if written is None:
        written = _timestamp(state.get("last_time"))
    if written is None:
        written = _timestamp(state.get("started_at"))
    if written is None:
        return "an unknown time"
    if now - written <= FOLLOWER_STOPPED_AFTER_S:
        return None
    return datetime.fromtimestamp(written, UTC).strftime(feed._TIME)


def _feed():
    """The state file without its fresh accounts and the last FEED_LIMIT events ordered by observed
    block height, then hash, with follower: for a follower's state "running", or "stopped" with
    lag_blocks null once _stopped_since says so, else null; a 503 while the state file does not parse."""
    state_dir = Path(os.environ.get("FEED_STATE_DIR", FEED_STATE.parent))
    state_path, events_dir = state_dir / "state.json", state_dir / "events"
    try:
        stats = _read_json(state_path) if state_path.is_file() else None
    except ValueError:
        return 503, {"error": "feed state unreadable"}
    follower = None
    if stats is not None:
        stats.pop("fresh_accounts", None)
        if stats.get("mode") == "follow":
            follower = "running" if _stopped_since(stats, time.time()) is None else "stopped"
        if follower == "stopped":
            stats["lag_blocks"] = None
    events = render.feed_events(events_dir)[0]
    return 200, {"follower": follower, "stats": stats,
                 "event_ids": [stem if _HEX64.fullmatch(stem) else None for _, stem, _ in events[-FEED_LIMIT:]],
                 "events": [document for _, _, document in events[-FEED_LIMIT:]]}


def _live_feed_page():
    state_dir = os.environ.get("FEED_STATE_DIR")
    if state_dir is None:
        return None
    state_path = Path(state_dir) / "state.json"
    events_dir = Path(state_dir) / "events"
    try:
        state = _read_json(state_path)
        if not isinstance(state, dict) or state.get("mode") != "follow":
            return None
        stopped_since = _stopped_since(state, time.time())
        events, unreadable = render.feed_events(events_dir)
    except (OSError, UnicodeError, ValueError):
        return None
    event_rows = [(document, stem) for _, stem, document in events[-FEED_LIMIT:]]
    return render.feed_page(state=state, event_rows=event_rows, total=len(events), unreadable=unreadable,
                            stopped_since=stopped_since).encode("utf-8")


def _flow(store, subject):
    if not any(a.subject == subject and a.category == "incident" for a in store.assertions):
        return 403, {"error": "subject carries no incident label"}
    path = FLOWS_DIR / f"{subject}.json"
    if path.resolve().parent != FLOWS_DIR.resolve() or not path.is_file():
        path = trace.find(subject, FLOWS_DIR)
    if path is None:
        return 404, {"error": f"no exit trace for {subject}"}
    return 200, _read_json(path)


def _cluster(store, subject):
    if not any(a.subject == subject and a.category == "incident" for a in store.assertions):
        return 403, {"error": "subject carries no incident label"}
    for path in sorted(CLUSTERS_DIR.glob("*.json")):
        document = _read_json(path)
        if subject in document["seeds"]:
            return 200, document
    return 404, {"error": f"no cluster for {subject}"}


def _issuers():
    reads = issuers.reads()
    return 200, {"status": [dataclasses.asdict(a) for a in labels.load(issuers.STATUS_PATH).assertions],
                 "history": [dataclasses.asdict(a) for a in labels.load(issuers.HISTORY_PATH).assertions],
                 "status_reads": len(reads), "status_read_subjects": len({read["subject"] for read in reads}),
                 "history_coverage": issuers.history_coverage()}


def _search(term):
    index = _read_json(SEARCH_INDEX)
    needle = term.casefold()
    results = [entry for entry in index["accounts"] + index["entities"]
               if needle in entry.get("subject", "").casefold()
               or needle in entry.get("name", "").casefold()
               or any(needle in label.casefold() for label in entry.get("labels", []))]
    return 200, {"term": term, "results": results}


def _account(store, subject):
    rows = [dataclasses.asdict(a) for a in store.assertions
            if a.subject == subject and a.superseded_by is None]
    if not rows:
        return None
    return 200, {"subject": subject, "assertions": rows}


def _entity(entity_id):
    for line in (ROOT / "intel" / "entities.jsonl").read_text(encoding="utf-8").splitlines():
        entity = json.loads(line)
        if entity["id"] == entity_id:
            return 200, entity
    return None


def route(path, store):
    """(status, JSON body) for an API path, or None when the path is not an API route."""
    parts = [urllib.parse.unquote(part) for part in urllib.parse.urlsplit(path).path.split("/")[1:]]
    if parts[-1].endswith(".html"):
        return None
    if parts == ["health"]:
        return 200, {"status": "ok", "assertions": len(store.assertions), "label_store_sha256": store.sha256}
    if parts == ["feed"]:
        return _feed()
    if parts == ["issuers"]:
        return _issuers()
    if len(parts) == 2 and parts[0] == "search" and parts[1]:
        return _search(parts[1])
    if len(parts) == 2 and parts[1]:
        name, value = parts
        if name == "accounts":
            return _account(store, value)
        if name == "entities":
            return _entity(value)
        if name == "labels":
            return _labels(store, value)
        if name == "incidents":
            return _incident(value)
        if name == "evidence":
            return _evidence(value)
        if name == "flow":
            return _flow(store, value)
        if name == "cluster":
            return _cluster(store, value)
    return None


def _static(site, path):
    """(status, content type, bytes) for a file under site, never outside it."""
    if site is None:
        return None
    relative = urllib.parse.unquote(urllib.parse.urlsplit(path).path).lstrip("/") or "index.html"
    try:
        root = Path(site).resolve()
        target = (root / relative).resolve()
        if target.is_dir():
            target = target / "index.html"
        if root not in target.parents or not target.is_file():
            return None
    except (OSError, ValueError):
        return None
    return 200, mimetypes.guess_type(target.name)[0] or "application/octet-stream", target.read_bytes()


class _DeadlineReader(io.RawIOBase):
    def __init__(self, connection, deadline):
        super().__init__()
        self.connection = connection
        self.deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request line and headers exceeded their deadline")
        self.connection.settimeout(remaining)
        return self.connection.recv_into(buffer)


def make_server(host, port, site=None, store=None, live_routes=None, workspace_routes=None):
    """A ThreadingHTTPServer for the API; port 0 picks a free port."""
    store = store or labels.load()
    def request_timeout():
        return live_routes.request_timeout_s if live_routes is not None else 10 if workspace_routes is not None else None

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self._route_slot = None
            timeout = request_timeout()
            if timeout is not None:
                deadline = time.monotonic() + timeout
                self.rfile = io.BufferedReader(_DeadlineReader(self.connection, deadline))

        def parse_request(self):
            parsed = super().parse_request()
            timeout = request_timeout()
            if timeout is not None:
                self.connection.settimeout(timeout)
                if parsed:
                    request_path = urllib.parse.urlsplit(self.path).path
                    if workspace_routes is not None and (request_path == "/workspace-api" or request_path.startswith("/workspace-api/")):
                        if not self.server._workspace_slots.acquire(blocking=False):
                            self.send_error(503)
                            return False
                        self._route_slot = self.server._workspace_slots
                    elif request_path != "/live" and not request_path.startswith("/live/"):
                        if not self.server._read_only_slots.acquire(blocking=False):
                            self.send_error(503)
                            return False
                        self._route_slot = self.server._read_only_slots
            return parsed

        def finish(self):
            try:
                super().finish()
            finally:
                if self._route_slot is not None:
                    self._route_slot.release()
                    self._route_slot = None

        def do_GET(self):
            if self._workspace_request():
                return
            request_path = urllib.parse.urlsplit(self.path).path
            if request_path == "/live" or request_path.startswith("/live/"):
                if live_routes is None:
                    self._send(404, JSON_TYPE, b'{"error": "not_found", "message": "No live route is enabled."}\n')
                    return
                status, body, headers = live_routes.handle(self.path, self.client_address[0], self.headers)
                data = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
                observation = re.fullmatch(r"/live/(?:account-data|portfolio-data)/([0-9a-f]{64})\.json", request_path)
                immutable = status == 200 and observation and not urllib.parse.urlsplit(self.path).query and hashlib.sha256(data).hexdigest() == observation.group(1)
                response_headers = {name: value for name, value in headers.items() if name.lower() != "cache-control"}
                self._send(status, JSON_TYPE, data, response_headers,
                           cache_control="public, max-age=31536000, immutable" if immutable else "no-store")
                return
            answer = route(self.path, store)
            if answer is not None:
                status, body = answer
                self._send(status, JSON_TYPE, (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8"))
                return
            if urllib.parse.urlsplit(self.path).path == "/feed.html":
                live_page = _live_feed_page()
                if live_page is not None:
                    self._send(200, "text/html", live_page)
                    return
            static = _static(site, self.path)
            if static is None:
                missing = _static(site, "/404.html")
                if missing is None:
                    self._send(404, JSON_TYPE, b'{"error": "not found"}\n')
                else:
                    self._send(404, *missing[1:])
                return
            asset_hash = (re.fullmatch(r"/graph-data/([0-9a-f]{64})\.json", request_path)
                          or re.fullmatch(r"/assets/([0-9a-f]{64})\.js", request_path))
            cache_control = ("public, max-age=31536000, immutable"
                             if static[0] == 200 and asset_hash
                             and hashlib.sha256(static[2]).hexdigest() == asset_hash.group(1)
                             else "no-store")
            self._send(*static, cache_control=cache_control)

        def do_HEAD(self):
            self.do_GET()

        def do_OPTIONS(self):
            path = urllib.parse.urlsplit(self.path).path
            methods = self.headers.get_all("Access-Control-Request-Method", [])
            requested_headers = [value.strip().lower()
                                 for header in self.headers.get_all("Access-Control-Request-Headers", [])
                                 for value in header.split(",")]
            if (not self.headers.get("Origin") or methods not in (["GET"], ["HEAD"], ["OPTIONS"])
                    or any(value not in ("", "content-type") for value in requested_headers)
                    or _PUBLIC_CORS_PATH.fullmatch(path) is None):
                self._send(403, JSON_TYPE, b'{"error": "cors_forbidden", "message": "Cross-origin access is limited to public reads with optional Content-Type."}\n')
                return
            headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                       "Access-Control-Max-Age": "600"}
            if "content-type" in requested_headers:
                headers["Access-Control-Allow-Headers"] = "Content-Type"
            self._send(204, JSON_TYPE, b"", headers)

        def do_POST(self):
            if not self._workspace_request():
                self.send_error(501, "Unsupported method")

        def _workspace_request(self):
            path = urllib.parse.urlsplit(self.path).path
            if workspace_routes is None or not (path == "/workspace-api" or path.startswith("/workspace-api/")):
                return False
            headers = {"Vary": "Cookie", "X-Content-Type-Options": "nosniff"}
            def refuse(status, code, message):
                self.close_connection = True
                self._send(status, JSON_TYPE, json.dumps({"error": code, "message": message}).encode("utf-8"), headers)
                return True
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding", []):
                return refuse(400, "invalid_framing", "Transfer-Encoding is not supported for workspace requests.")
            body = b""
            if self.command == "POST":
                if not lengths:
                    return refuse(411, "length_required", "Content-Length is required.")
                if len(lengths) != 1 or re.fullmatch(r"0|[1-9][0-9]{0,9}", lengths[0]) is None:
                    return refuse(400, "invalid_framing", "Send one unambiguous Content-Length.")
                length = int(lengths[0])
                if length > 32768:
                    return refuse(413, "body_too_large", "Workspace request exceeds 32 KiB.")
                deadline = time.monotonic() + WORKSPACE_BODY_TIMEOUT_S
                if isinstance(self.rfile, io.BufferedReader) and isinstance(self.rfile.raw, _DeadlineReader):
                    self.rfile.raw.deadline = min(self.rfile.raw.deadline, deadline)
                chunks = []
                try:
                    while length:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("workspace body deadline exceeded")
                        self.connection.settimeout(remaining)
                        chunk = self.rfile.read1(min(length, 8192))
                        if not chunk:
                            return refuse(400, "incomplete_body", "The workspace request body is incomplete.")
                        chunks.append(chunk)
                        length -= len(chunk)
                    body = b"".join(chunks)
                except TimeoutError:
                    return refuse(408, "body_timeout", "The workspace request body exceeded its deadline.")
            elif lengths and lengths != ["0"]:
                return refuse(400, "invalid_framing", "GET and HEAD workspace requests do not take a body.")
            status, data, extra = workspace_routes.handle(self.command, self.path, self.client_address[0], self.headers, body)
            self._send(status, JSON_TYPE, data if isinstance(data, bytes) else (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"), {**headers, **extra})
            return True

        def log_request(self, code="-", size="-"):
            if hasattr(self, "path") and urllib.parse.urlsplit(self.path).path.startswith("/workspace-api"):
                self.log_message('"%s /workspace-api %s" %s %s', self.command, self.request_version, code, size)
            else:
                super().log_request(code, size)

        def _send(self, status, content_type, data, headers=None, *, cache_control="no-store"):
            extra = dict(headers or {})
            if self.command in ("GET", "HEAD") and _PUBLIC_CORS_PATH.fullmatch(urllib.parse.urlsplit(self.path).path):
                extra.update({"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "Retry-After"})
            content_type = extra.pop("Content-Type", content_type)
            cache_control = extra.pop("Cache-Control", cache_control)
            self.send_response(status)
            if status != 204:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache_control)
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

    class Server(ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            self._connection_slots = (threading.BoundedSemaphore(live_routes.max_in_flight * 4)
                                      if live_routes is not None else threading.BoundedSemaphore(8) if workspace_routes is not None else None)
            self._read_only_slots = (threading.BoundedSemaphore(live_routes.max_in_flight)
                                     if live_routes is not None else threading.BoundedSemaphore(4) if workspace_routes is not None else None)
            self._workspace_slots = threading.BoundedSemaphore(4) if workspace_routes is not None else None
            super().__init__(*args, **kwargs)

        def get_request(self):
            request, client_address = super().get_request()
            timeout = request_timeout()
            if timeout is not None:
                request.settimeout(timeout)
            return request, client_address

        def process_request(self, request, client_address):
            if self._connection_slots is not None and not self._connection_slots.acquire(blocking=False):
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                if self._connection_slots is not None:
                    self._connection_slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                if self._connection_slots is not None:
                    self._connection_slots.release()

    return Server((host, port), Handler)


def serve(host, port, site=None, live=False, state_dir=None, workspace_origin="https://siyab.xyz"):
    store = labels.load() if live else None
    live_routes = None
    workspace_routes = None
    history_store = None
    portfolio_store = None
    jobs = None
    if state_dir is not None:
        from intel import delivery, history, investigation_jobs, monitor, portfolio
        from intel.workspace import SQLiteWorkspaceStore
        from intel.workspace_api import WorkspaceAPI

        state = Path(state_dir).expanduser().resolve()
        if state == ROOT or ROOT in state.parents:
            raise ValueError("workspace state must be outside the source repository")
        history_store = history.AccountHistory(state / "history.sqlite3")
        portfolio_store = portfolio.PortfolioHistory(state / "portfolio.sqlite3")
        jobs = investigation_jobs.InvestigationJobs(state / "investigation-jobs.sqlite3", history_store)
        def monitor_status():
            return {**monitor.read_status(state / "monitor.sqlite3"), "configured": True,
                    "account_polling": history.read_status(state / "history.sqlite3"), "investigations": jobs.status()}
        workspace_store = SQLiteWorkspaceStore(state / "workspace.sqlite3")
        telegram = delivery.TelegramDelivery(workspace_store, delivery.configured_client())
        workspace_routes = WorkspaceAPI(workspace_store, origin=workspace_origin,
                                        secure_cookie=urllib.parse.urlsplit(workspace_origin).scheme == "https",
                                        monitor_status=monitor_status, investigation_jobs=jobs, delivery=telegram,
                                        history_store=history_store)
    if live:
        from intel import accounts, enrichment, portfolio
        from intel.live import LiveAPI

        live_routes = LiveAPI(store, history_store=history_store, entity_directory=enrichment.EntityDirectory(state if state_dir is not None else None),
                              portfolio_data=portfolio.PortfolioData(accounts.AccountData(store)), portfolio_history=portfolio_store)
    if state_dir is not None and live:
        from intel.ask import AskService

        ask = AskService(state, store, lambda query, peer, headers: live_routes.handle(
            "/live/verdict?" + urllib.parse.urlencode(sorted(query.items())), peer, headers))
        live_routes.ask = workspace_routes.ask = ask
    server = make_server(host, port, site, store=store, live_routes=live_routes, workspace_routes=workspace_routes)
    mode = " with live routes" if live else ""
    access = "public read-only API and authenticated workspace" if workspace_routes is not None else "read only API"
    print(f"serving the {access}{mode} on http://{server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
    return 0
