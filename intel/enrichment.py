"""Curated primary-source account associations, separate from verdict labels and ownership."""

import copy
import hashlib
import http.client
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from intel.accounts import AccountInputError, _account

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = Path(__file__).resolve().parent / "entity_catalog.json"
MAX_SOURCE_BYTES = 256 * 1024
MAX_SOURCES = 8
MAX_ENTRIES = 256
STALE_AFTER_S = 30 * 86400
REFRESH_DEADLINE_S = 20
_HOSTS = {"raw.githubusercontent.com", "docs.metapool.app", "docs.templarfi.org", "docs.pyth.network"}
_HEX = re.compile(r"[0-9a-f]{64}")


class EnrichmentInputError(ValueError):
    """The entity directory request is outside its supported bounded scope."""


class EnrichmentReadError(ValueError):
    """A source snapshot or its maintenance state cannot be validated."""


def _stamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(value):
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise EnrichmentReadError("The source retrieval timestamp is invalid.") from None
    return parsed.timestamp()


def _text(value, maximum):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise EnrichmentReadError("The entity catalog contains invalid text.")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def _fetch(url, deadline):
    opener = urllib.request.build_opener(_NoRedirect)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Entity source refresh reached its deadline.")
    request = urllib.request.Request(url, headers={"User-Agent": "Siyab-Source-Directory/1.0", "Accept-Encoding": "identity"})
    with opener.open(request, timeout=min(5, remaining)) as reply:
        if reply.geturl() != url:
            raise EnrichmentReadError("The entity source redirected.")
        length = reply.headers.get("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_SOURCE_BYTES):
            raise EnrichmentReadError("The entity source exceeds its read bound.")
        chunks, size = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Entity source refresh reached its deadline.")
            chunk = reply.read(min(16384, MAX_SOURCE_BYTES + 1 - size))
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise EnrichmentReadError("The entity source exceeds its read bound.")
            chunks.append(chunk)


def _body(source, body):
    if not isinstance(body, bytes) or not 1 <= len(body) <= MAX_SOURCE_BYTES:
        raise EnrichmentReadError("The entity source body is empty or exceeds its read bound.")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise EnrichmentReadError("The entity source is not UTF-8.") from None
    if not all(marker in text for marker in source["required_markers"]):
        raise EnrichmentReadError("The entity source no longer has its expected document structure.")
    return text


class EntityDirectory:
    def __init__(self, state_dir=None, *, clock=time.time):
        self.clock = clock
        raw = CATALOG_PATH.read_bytes()
        if len(raw) > 256 * 1024:
            raise EnrichmentReadError("The entity catalog exceeds its read bound.")
        try:
            catalog = json.loads(raw)
        except (ValueError, RecursionError):
            raise EnrichmentReadError("The entity catalog is invalid JSON.") from None
        if (not isinstance(catalog, dict) or catalog.get("version") != 1 or catalog.get("network") != "mainnet"
                or not isinstance(catalog.get("sources"), list) or not 1 <= len(catalog["sources"]) <= MAX_SOURCES
                or not isinstance(catalog.get("entries"), list) or not 1 <= len(catalog["entries"]) <= MAX_ENTRIES):
            raise EnrichmentReadError("The entity catalog schema is unsupported.")
        self.sources, texts = {}, {}
        for source in catalog["sources"]:
            if not isinstance(source, dict):
                raise EnrichmentReadError("An entity source is invalid.")
            sid = _text(source.get("id"), 64)
            if sid in self.sources:
                raise EnrichmentReadError("The entity catalog repeats a source.")
            url = urllib.parse.urlsplit(_text(source.get("url"), 2048))
            if (url.scheme != "https" or url.hostname not in _HOSTS or url.netloc != url.hostname
                    or url.query or url.fragment or "\\" in url.path):
                raise EnrichmentReadError("An entity source URL is outside the fixed publisher allowlist.")
            _text(source.get("publisher"), 128)
            if not _HEX.fullmatch(str(source.get("content_sha256"))):
                raise EnrichmentReadError("An entity source digest is invalid.")
            _seconds(source.get("retrieved_at"))
            markers = source.get("required_markers")
            if not isinstance(markers, list) or not 1 <= len(markers) <= 8:
                raise EnrichmentReadError("An entity source has no bounded document markers.")
            for marker in markers:
                _text(marker, 256)
            fixture = (ROOT / _text(source.get("fixture"), 256)).resolve()
            if not fixture.is_relative_to((ROOT / "fixtures" / "enrichment").resolve()):
                raise EnrichmentReadError("An entity source fixture is outside the evidence directory.")
            body = fixture.read_bytes()
            if hashlib.sha256(body).hexdigest() != source["content_sha256"]:
                raise EnrichmentReadError("An entity source fixture does not match its digest.")
            texts[sid] = _body(source, body)
            self.sources[sid] = source
        self.entries, seen = [], set()
        for entry in catalog["entries"]:
            if not isinstance(entry, dict) or entry.get("source_id") not in self.sources:
                raise EnrichmentReadError("An entity entry has no registered source.")
            for field, maximum in (("entity_id", 64), ("name", 128), ("category", 64), ("role", 256), ("anchor", 2048)):
                _text(entry.get(field), maximum)
            try:
                account = _account(entry.get("account_id"))
            except AccountInputError:
                raise EnrichmentReadError("An entity entry is not a valid mainnet account.") from None
            key = (entry["entity_id"], account)
            if key in seen or entry.get("lifecycle") not in ("documented", "deprecated"):
                raise EnrichmentReadError("An entity entry is repeated or has an invalid lifecycle.")
            if account not in entry["anchor"] or entry["anchor"] not in texts[entry["source_id"]]:
                raise EnrichmentReadError("An entity entry has no exact supporting source excerpt.")
            if not isinstance(entry.get("aliases"), list) or len(entry["aliases"]) > 8:
                raise EnrichmentReadError("An entity entry has invalid aliases.")
            for alias in entry["aliases"]:
                _text(alias, 128)
            seen.add(key)
            self.entries.append(entry)
        self.path = None
        if state_dir is not None:
            directory = Path(state_dir).resolve()
            if directory.is_relative_to(ROOT.resolve()):
                raise EnrichmentInputError("Entity maintenance state must stay outside the repository.")
            directory.mkdir(parents=True, exist_ok=True)
            self.path = directory / "entity-sources.sqlite3"
            if self.path.resolve().is_relative_to(ROOT.resolve()):
                raise EnrichmentInputError("Entity maintenance database must stay outside the repository.")
            with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
                connection.execute("CREATE TABLE IF NOT EXISTS source_checks (source_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, "
                                   "confirmed_at TEXT, status TEXT NOT NULL, content_sha256 TEXT, missing_accounts TEXT NOT NULL, "
                                   "assessment TEXT NOT NULL, last_document_sha256 TEXT, catalog_sha256 TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS source_documents (source_id TEXT PRIMARY KEY, "
                                   "content_sha256 TEXT NOT NULL, body BLOB NOT NULL)")

    def _checks(self):
        if self.path is None:
            return {}
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            return {row["source_id"]: dict(row) for row in connection.execute("SELECT * FROM source_checks")
                    if row["source_id"] in self.sources
                    and row["catalog_sha256"] == self.sources[row["source_id"]]["content_sha256"]}

    def _entry(self, entry, checks):
        source = self.sources[entry["source_id"]]
        check = checks.get(entry["source_id"])
        retrieved = check["confirmed_at"] if check and check["confirmed_at"] else source["retrieved_at"]
        status, check_status = "documented", "bundled_snapshot"
        if self.clock() - _seconds(retrieved) > STALE_AFTER_S:
            status = "stale"
        if check:
            check_status = check["status"]
            if check_status == "unavailable":
                status = "stale"
            if check["assessment"] == "changed":
                status = "source_withdrawn" if entry["account_id"] in json.loads(check["missing_accounts"]) else "source_changed"
        return {key: copy.deepcopy(entry[key]) for key in ("entity_id", "name", "aliases", "category", "account_id", "role", "lifecycle")} | {
            "network": "mainnet", "provenance": "official_source_documented", "status": status,
            "check_status": check_status,
            "last_checked_at": check["checked_at"] if check else None,
            "last_document_sha256": check["last_document_sha256"] if check else source["content_sha256"],
            "latest_check": {"content_sha256": check["content_sha256"], "status": check_status} if check else None,
            "evidence": {"url": source["url"], "publisher": source["publisher"], "retrieved_at": retrieved,
                         "first_retrieved_at": source["retrieved_at"], "content_sha256": source["content_sha256"],
                         "excerpt": entry["anchor"]}}

    def lookup(self, account):
        try:
            account = _account(account)
        except AccountInputError as error:
            raise EnrichmentInputError(str(error)) from None
        checks = self._checks()
        return {"account_id": account, "network": "mainnet",
                "entities": [self._entry(entry, checks) for entry in self.entries if entry["account_id"] == account],
                "coverage": self._coverage()}

    def _coverage(self):
        return {"scope": "Curated exact-address associations documented by official publishers; not proof of current ownership, code integrity or a payment verdict.",
                "sources": len(self.sources), "accounts": len({entry["account_id"] for entry in self.entries}),
                "entities": len({entry["entity_id"] for entry in self.entries}), "stale_after_days": STALE_AFTER_S // 86400,
                "exchange_ownership": "Unknown unless an official source explicitly documents the exact account; none included.",
                "maintenance": "Changed documents require catalog review; missing accounts retain historical evidence with source_withdrawn status."}

    def search(self, query="", *, category=None, limit=25, offset=0):
        if (not isinstance(query, str) or len(query) > 128 or any(ord(char) < 32 for char in query)
                or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= MAX_ENTRIES
                or category is not None and category not in {entry["category"] for entry in self.entries}):
            raise EnrichmentInputError("Use a bounded search query, known category and page size from 1 to 100.")
        query, checks = query.casefold().strip(), self._checks()
        rows = [entry for entry in self.entries if (category is None or entry["category"] == category)
                and (not query or query in " ".join([entry["name"], entry["account_id"], entry["role"], *entry["aliases"]]).casefold())]
        rows.sort(key=lambda entry: (entry["name"].casefold(), entry["account_id"]))
        return {"network": "mainnet", "items": [self._entry(entry, checks) for entry in rows[offset:offset + limit]],
                "pagination": {"offset": offset, "limit": limit, "total": len(rows),
                               "next_offset": offset + limit if offset + limit < len(rows) else None},
                "categories": sorted({entry["category"] for entry in self.entries}), "coverage": self._coverage()}

    def refresh(self, *, limit=4, fetcher=None):
        if self.path is None:
            raise EnrichmentInputError("Configure an external state directory before refreshing entity sources.")
        if type(limit) is not int or not 1 <= limit <= MAX_SOURCES:
            raise EnrichmentInputError("Refresh between 1 and 8 curated sources per pass.")
        checks, now = self._checks(), _stamp(self.clock())
        selected = sorted(self.sources.values(), key=lambda source: (checks.get(source["id"], {}).get("checked_at", ""), source["id"]))[:limit]
        deadline = time.monotonic() + REFRESH_DEADLINE_S
        result = {"checked": 0, "confirmed": 0, "changed": 0, "unavailable": 0, "checked_at": now, "sources": []}
        for source in selected:
            if time.monotonic() >= deadline:
                break
            sid, digest, missing, status = source["id"], None, [], "unavailable"
            previous = checks.get(sid)
            confirmed_at = previous["confirmed_at"] if previous else None
            assessment = previous["assessment"] if previous else "bundled_snapshot"
            last_document = previous["last_document_sha256"] if previous else source["content_sha256"]
            missing = json.loads(previous["missing_accounts"]) if previous else []
            try:
                body = fetcher(source["url"]) if fetcher is not None else _fetch(source["url"], deadline)
                text = _body(source, body)
                digest = hashlib.sha256(body).hexdigest()
                if digest == source["content_sha256"]:
                    status, confirmed_at = "confirmed", now
                    missing = []
                else:
                    status = "changed"
                    missing = [entry["account_id"] for entry in self.entries if entry["source_id"] == sid
                               and entry["anchor"] not in text]
                assessment, last_document = status, digest
            except (OSError, ValueError, http.client.HTTPException):
                status = "unavailable"
            with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
                connection.execute("INSERT INTO source_checks VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                                   "checked_at=excluded.checked_at, confirmed_at=excluded.confirmed_at, status=excluded.status, "
                                   "content_sha256=excluded.content_sha256, missing_accounts=excluded.missing_accounts, "
                                   "assessment=excluded.assessment, last_document_sha256=excluded.last_document_sha256, catalog_sha256=excluded.catalog_sha256",
                                   (sid, now, confirmed_at, status, digest, json.dumps(missing), assessment, last_document, source["content_sha256"]))
                if status != "unavailable":
                    connection.execute("INSERT INTO source_documents VALUES(?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                                       "content_sha256=excluded.content_sha256, body=excluded.body", (sid, digest, body))
            result["checked"] += 1
            result[status] += 1
            result["sources"].append({"source_id": sid, "status": status, "content_sha256": digest})
        return result
