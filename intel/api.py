"""A read only JSON API over the committed files, on the standard library http.server.

Routes (GET only; no route writes anything and no route opens the network):

    /health               the label store's size and sha256
    /labels/<subject>     every assertion on the subject, on any chain
    /incidents/<id>       the incident file intel/incidents/<id>.json
    /evidence/<hash>      an evidence document from fixtures/evidence/ or intel/feed/events/
    /feed                 the feed state without its fresh accounts and the last 50 events, or null
                          and an empty list
    /flow/<subject>       the exit trace intel/flows/<subject>.json, else the trace file under
                          intel/flows/ whose subject it is (such as rhea-collector.json); 403 for a
                          subject that carries no incident label, 404 when no trace was written
    anything else         a static file under --site when one is given, else 404

Every answer is JSON (static files aside) and carries Cache-Control: no-store.
"""

import dataclasses
import json
import mimetypes
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gate import incident
from intel import labels, trace

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIRS = (ROOT / "fixtures" / "evidence", ROOT / "intel" / "feed" / "events")
FEED_STATE = ROOT / "intel" / "feed" / "state.json"
FEED_EVENTS = ROOT / "intel" / "feed" / "events"
FLOWS_DIR = ROOT / "intel" / "flows"
FEED_LIMIT = 50
JSON_TYPE = "application/json; charset=utf-8"
_HEX64 = re.compile(r"[0-9a-f]{64}")


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
    return 404, {"error": f"no evidence document {evidence_hash}"}


def _feed():
    """The state file without its fresh accounts and the last FEED_LIMIT events ordered by observed
    block height, then hash."""
    import os

    state_dir = Path(os.environ.get("FEED_STATE_DIR", FEED_STATE.parent))
    state_path, events_dir = state_dir / "state.json", state_dir / "events"
    stats = _read_json(state_path) if state_path.is_file() else None
    if stats is not None:
        stats.pop("fresh_accounts", None)
    events = []
    if events_dir.is_dir():
        for path in events_dir.glob("*.json"):
            document = _read_json(path)
            height = (document.get("observed") or {}).get("block_height") or 0
            events.append((height, path.stem, document))
    events.sort(key=lambda item: (item[0], item[1]))
    return 200, {"stats": stats, "events": [document for _, _, document in events[-FEED_LIMIT:]]}


def _flow(store, subject):
    if not any(a.subject == subject and a.category == "incident" for a in store.assertions):
        return 403, {"error": "subject carries no incident label"}
    path = FLOWS_DIR / f"{subject}.json"
    if path.resolve().parent != FLOWS_DIR.resolve() or not path.is_file():
        path = trace.find(subject, FLOWS_DIR)
    if path is None:
        return 404, {"error": f"no exit trace for {subject}"}
    return 200, _read_json(path)


def route(path, store):
    """(status, JSON body) for an API path, or None when the path is not an API route."""
    parts = [urllib.parse.unquote(part) for part in urllib.parse.urlsplit(path).path.split("/")[1:]]
    if parts == ["health"]:
        return 200, {"status": "ok", "assertions": len(store.assertions), "label_store_sha256": store.sha256}
    if parts == ["feed"]:
        return _feed()
    if len(parts) == 2 and parts[1]:
        name, value = parts
        if name == "labels":
            return _labels(store, value)
        if name == "incidents":
            return _incident(value)
        if name == "evidence":
            return _evidence(value)
        if name == "flow":
            return _flow(store, value)
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


def make_server(host, port, site=None, store=None):
    """A ThreadingHTTPServer for the API; port 0 picks a free port."""
    store = store or labels.load()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            answer = route(self.path, store)
            if answer is not None:
                status, body = answer
                self._send(status, JSON_TYPE, (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8"))
                return
            static = _static(site, self.path)
            if static is None:
                self._send(404, JSON_TYPE, b'{"error": "not found"}\n')
                return
            self._send(*static)

        def _send(self, status, content_type, data):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer((host, port), Handler)


def serve(host, port, site=None):
    server = make_server(host, port, site)
    print(f"serving the read only API on http://{server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
    return 0
