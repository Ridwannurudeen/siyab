"""Keyless live routes mounted by ``python -m intel serve --live``."""

import copy
import ipaddress
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from collections import OrderedDict

from gate import canon, evidence, intents, rpc, rules, scan
from intel import accounts, history, identity, labels, reports

RATE_PER_MINUTE = 60
RATE_WINDOW_S = 60
MAX_IN_FLIGHT = 8
REQUEST_TIMEOUT_S = 25
CACHE_TTL_S = 60
MAX_CACHE_ENTRIES = 128
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_CACHE_BYTES = 4 * 1024 * 1024
MAX_RATE_CLIENTS = 4096
REPORTS_ACCOUNT = "reports.kestrelridge-main.testnet"
INTENTS_ORIGIN_ASSET = "nep141:wrap.near"
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


class LiveInputError(ValueError):
    """A live route input that is not in its public contract shape."""


class LiveReadError(ValueError):
    """An unreadable response from a live read-only view."""


class LiveAPI:
    def __init__(self, store=None, *, clock=time.monotonic, wall_clock=time.time,
                 perf_clock=time.perf_counter, rate_per_minute=RATE_PER_MINUTE,
                 request_timeout_s=REQUEST_TIMEOUT_S, cache_ttl_s=CACHE_TTL_S,
                 cache_size=MAX_CACHE_ENTRIES, max_in_flight=MAX_IN_FLIGHT, history_store=None):
        if type(rate_per_minute) is not int or rate_per_minute < 1:
            raise ValueError("rate_per_minute must be a positive integer")
        if not isinstance(request_timeout_s, (int, float)) or isinstance(request_timeout_s, bool) or not (
                0 < request_timeout_s < 30):
            raise ValueError("request_timeout_s must be greater than zero and less than 30")
        if type(cache_size) is not int or cache_size < 1:
            raise ValueError("cache_size must be a positive integer")
        self.store = store or labels.load()
        self.history_store = history_store
        self.clock = clock
        self.wall_clock = wall_clock
        self.perf_clock = perf_clock
        self.rate_per_minute = rate_per_minute
        self.request_timeout_s = request_timeout_s
        self.cache_ttl_s = cache_ttl_s
        self.cache_size = cache_size
        self.max_in_flight = max_in_flight
        self._slots = threading.BoundedSemaphore(max_in_flight)
        self._lock = threading.Lock()
        self._rates = OrderedDict()
        self._cache = OrderedDict()
        self._cache_bytes = 0

    def handle(self, target, peer, headers):
        try:
            parsed = urllib.parse.urlsplit(target)
        except ValueError:
            return self._error(400, "invalid_input", "The request target is malformed.")
        if parsed.path != "/live" and not parsed.path.startswith("/live/"):
            return None
        if not self._slots.acquire(blocking=False):
            return self._error(503, "busy", "The live API is busy.")
        try:
            client = self._client_ip(peer, headers)
            retry_after = self._rate_limit(client)
            if retry_after is not None:
                return self._error(429, "rate_limited", "The client rate limit was reached.",
                                   {"Retry-After": str(retry_after)})
            key = (parsed.path, parsed.query)
            cacheable = not (parsed.path.startswith(("/live/account-data/", "/live/account-events/")) or
                             (parsed.path.startswith("/live/accounts/") and parsed.path.rsplit("/", 1)[-1] in ("history", "backfill")))
            cached = self._cache_get(key) if cacheable else None
            if cached is not None:
                return cached
            deadline = time.monotonic() + self.request_timeout_s
            try:
                with rpc.request_deadline(self.request_timeout_s):
                    snapshot = self.exact_snapshot(parsed.path, parsed.query)
                    if snapshot is None:
                        status, body = self._route(parsed.path, parsed.query)
                        response_headers = {}
                    else:
                        status, body, response_headers = snapshot
                response_size = len(body) if isinstance(body, bytes) else self._response_size(body, MAX_RESPONSE_BYTES)
                if response_size is None:
                    return self._error(503, "response_too_large", "The live response exceeds the size limit.")
                if time.monotonic() >= deadline:
                    raise TimeoutError("request deadline exceeded")
            except LiveInputError as error:
                return self._error(400, "invalid_input", str(error))
            except TimeoutError:
                return self._error(504, "timeout", "The live read exceeded its request deadline.")
            except history.HistoryCapacityError as error:
                return 503, {"error": "archive_capacity", "message": str(error),
                             "coverage": {"history_complete": False, "new_observations_paused": True}}, {"Retry-After": "60"}
            except LiveReadError:
                return self._error(503, "read_failed", "A required live view could not be read.")
            except rules.UNAVAILABLE:
                return self._error(503, "read_failed", "A required live view could not be read.")
            except Exception:
                return self._error(500, "internal_error", "The live check could not be completed.")
            answer = (status, body, response_headers)
            if status == 200 and cacheable:
                self._cache_put(key, answer, response_size)
            return answer
        finally:
            self._slots.release()

    def exact_snapshot(self, path, query):
        if not path.startswith(("/live/account-data/", "/live/account-events/")):
            return None
        match = re.fullmatch(r"/live/(account-data|account-events)/([a-f0-9]{64})\.json", path)
        if match is None or query:
            return self._error(400, "invalid_input", "A snapshot URL contains one lowercase SHA-256 identifier and no query fields.")
        if self.history_store is None:
            return self._error(503, "archive_unavailable", "The account observation archive is unavailable.")
        try:
            body = (self.history_store.get_graph_bytes(match[2]) if match[1] == "account-data"
                    else self.history_store.get_event_bytes(match[2]))
        except TimeoutError:
            raise
        except (history.HistoryInputError, history.HistoryReadError, OSError, sqlite3.Error):
            return self._error(503, "read_failed", "The stored public observation could not be read.")
        if body is None:
            return self._error(404, "not_found", "No retained public observation matches this identifier.")
        return 200, body, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "public, max-age=31536000, immutable"}

    def _route(self, path, query):
        if path.startswith("/live/accounts/"):
            return self._account_read(path, query)
        if path == "/live/health":
            self._no_query(query)
            return 200, {"ok": True, "network": "mainnet", "rate_per_minute": self.rate_per_minute}
        if path == "/live/verdict":
            return 200, self._verdict(self._query(query, {"receiver", "amount", "token"}))
        if path == "/live/intents":
            return 200, self._intents(self._query(query, {"destination_asset", "recipient", "amount"}))
        if path == "/live/reports":
            subject = self._account(self._query(query, {"subject"})["subject"], "subject")
            return 200, self._reports(subject)
        if path == "/live/reports/recent":
            self._no_query(query)
            return 200, self._recent_reports()
        if path.startswith("/live/scan/"):
            self._no_query(query)
            raw_account = path[len("/live/scan/"):]
            if not raw_account or "/" in raw_account:
                raise LiveInputError("The scan path must contain one NEAR account id.")
            account = self._account(urllib.parse.unquote(raw_account), "account")
            if account.endswith(".testnet"):
                raise LiveInputError("The live scan accepts mainnet accounts only.")
            return 200, self._scan(account)
        return self._error(404, "not_found", "No live route matches this path.")[:2]

    def _account_read(self, path, query):
        parts = path.split("/")
        if len(parts) != 5 or not parts[3]:
            raise LiveInputError("The investigation path must contain one mainnet account identifier.")
        account = self._account(urllib.parse.unquote(parts[3]), "account")
        if account == "testnet" or account.endswith(".testnet"):
            raise LiveInputError("Account investigations read mainnet accounts only.")
        kind = parts[4]
        allowed = {"profile": set(), "holdings": {"offset", "limit"},
                   "transactions": {"cursor", "limit"}, "transfers": {"cursor", "limit"}, "graph": {"depth"},
                   "identity": set(), "history": {"kind", "before", "limit"}, "backfill": {"kind", "cursor", "max_pages"}}
        if kind not in allowed:
            return self._error(404, "not_found", "No account investigation view matches this path.")[:2]
        try:
            pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
        except ValueError as error:
            raise LiveInputError("Investigation query fields are malformed.") from error
        if len({name for name, _ in pairs}) != len(pairs) or any(name not in allowed[kind] for name, _ in pairs):
            raise LiveInputError("Investigation query fields are unsupported or repeated.")
        values = dict(pairs)
        if kind in ("history", "backfill") and values.get("kind") not in ("transactions", "transfers"):
            raise LiveInputError("The archive kind must be transactions or transfers.")
        for field in ("offset", "limit", "depth", "before", "max_pages"):
            if field in values:
                upper = 2 ** 63 - 1 if field == "before" else 10000 if field == "offset" else 2 if field in ("depth", "max_pages") else 20 if kind == "holdings" else 100
                lower = 0 if field in ("offset", "before") else 1
                if not re.fullmatch(r"0|[1-9][0-9]{0,18}", values[field]) or not lower <= int(values[field]) <= upper:
                    raise LiveInputError("Investigation pagination or graph depth is outside its supported range.")
                values[field] = int(values[field])
        try:
            reader = accounts.AccountData(self.store)
            if kind in ("history", "backfill"):
                if self.history_store is None:
                    raise LiveReadError("The account observation archive is unavailable.")
                archive_kind = values.pop("kind")
                if kind == "history":
                    result = self.history_store.list_records(account, archive_kind, **values)
                else:
                    result = self.history_store.backfill_account(reader, account, archive_kind, **values)
            elif kind == "identity":
                result = identity.IdentityData(self.store).inspect(account)
            elif kind == "profile":
                result = reader.profile(account)
            elif kind == "holdings":
                result = reader.holdings(account, **values)
            elif kind == "transactions":
                result = reader.transactions(account, **values)
            elif kind == "transfers":
                result = reader.transfers(account, **values)
            else:
                result = reader.neighborhood(account, **values)
            if self.history_store is not None:
                if kind in history.KINDS:
                    self.history_store.record_page(account, kind, result)
                elif kind == "graph":
                    result = {**result, "snapshot": self.history_store.save_graph(result)}
        except TimeoutError:
            raise
        except history.HistoryCapacityError:
            raise
        except (accounts.AccountInputError, history.HistoryInputError) as error:
            raise LiveInputError(str(error)) from error
        except (accounts.AccountReadError, history.HistoryReadError, OSError, sqlite3.Error) as error:
            raise LiveReadError(str(error)) from error
        return 200, result

    def _verdict(self, query):
        receiver = self._account(query["receiver"], "receiver")
        if receiver.endswith(".testnet"):
            raise LiveInputError("The live verdict accepts mainnet receivers only.")
        amount = self._amount(query["amount"])
        token = query["token"]
        if token == "near":
            kind, token_id = "near", None
        else:
            kind, token_id = "ft", self._account(token, "token")
            if token_id.endswith(".testnet"):
                raise LiveInputError("The live verdict accepts mainnet tokens only.")
        request = {"kind": kind, "token_id": token_id, "receiver_id": receiver,
                   "amount": amount, "memo": None}
        near = rpc.Client(os.environ.get("NEAR_MAINNET_RPC_URL") or rpc.MAINNET_RPC_URL, "off")
        nearblocks = rpc.Client(os.environ.get("NEARBLOCKS_API_URL") or rpc.NEARBLOCKS_URL,
                                 "off", kind="http")
        archival = rpc.Client(os.environ.get("NEAR_ARCHIVAL_RPC_URL") or rpc.ARCHIVAL_RPC_URL, "off")
        started = self.perf_clock()
        judgement = rules.judge(request, {"allowlist": [], "caps": []}, self.store, near, nearblocks,
                                now_ms=int(self.wall_clock() * 1000), injection_scanner=None,
                                archival=archival, issuers=rules.issuers_for("mainnet"))
        latency_ms = int((self.perf_clock() - started) * 1000)
        document = evidence.document(
            subject={"network": "mainnet", "kind": kind, "token": token_id, "receiver": receiver,
                     "amount": amount, "memo_sha256": None},
            checks=judgement.checks, labels=judgement.labels, verdict=judgement.verdict,
            reasons=judgement.reasons, observed=judgement.observed, issued_ms=None, expires_ms=None,
            verdict_id=None, latency_ms=latency_ms)
        return self._verdict_shape(judgement.verdict, judgement.checks, judgement.reasons,
                                   evidence.evidence_hash(document), judgement.observed, latency_ms)

    def _scan(self, account):
        client = rpc.Client(os.environ.get("NEAR_MAINNET_RPC_URL") or rpc.MAINNET_RPC_URL, "off")
        result = scan.scan(account, client=client, store=self.store)
        findings = result["findings"]
        keys = findings["keys"]
        if keys is None:
            sentences = ["The access key list could not be read."]
            full_access = empty_methods = dead_keys = unlimited = None
            access_keys = None
        else:
            full_access = keys["full_access"]
            empty_methods = keys["empty_method_names"]
            dead_keys = keys["dead"]
            unlimited = keys["unlimited"]
            access_keys = keys["count"]
            sentences = [f"The scan read {access_keys} access key entries."]
            for values, sentence in ((full_access, "full access keys"), (empty_methods, "keys with empty method names"),
                                     (dead_keys, "keys with zero allowance"),
                                     (unlimited, "keys with no allowance limit")):
                sentences.append(f"The scan found {len(values)} {sentence}.")
        if findings["verifier"] is None:
            sentences.append("The account did not return a readable Gate key view.")
        return {"access_keys": access_keys, "full_access_keys": full_access,
                "empty_method_names": empty_methods, "dead_keys": dead_keys,
                "unlimited_allowance": unlimited, "findings": sentences,
                "read_at_block": findings["block_height"]}

    def _intents(self, query):
        destination = query["destination_asset"]
        try:
            intents.parse_destination(destination)
        except ValueError as error:
            raise LiveInputError("destination_asset is not a parsed 1Click asset id.") from error
        recipient = query["recipient"]
        if not isinstance(recipient, str) or not _EVM_ADDRESS.fullmatch(recipient):
            raise LiveInputError("recipient must be an EVM address with 40 hexadecimal characters.")
        amount = self._amount(query["amount"])
        started = self.perf_clock()
        result = intents.judge_quote(INTENTS_ORIGIN_ASSET, destination, amount, recipient,
                                     mode="off", firewall_url=os.environ.get("SHIELDBOT_API_URL"),
                                     clock=self.wall_clock, deadline=intents.live_deadline(self.wall_clock))
        latency_ms = int((self.perf_clock() - started) * 1000)
        return self._verdict_shape(result["verdict"], result["checks"], result["reasons"],
                                   result["evidence_hash"], result["document"]["observed"], latency_ms)

    def _reports(self, subject):
        client = self._reports_client()
        summary = client.call_function(REPORTS_ACCOUNT, "subject_summary", {"subject": subject})
        if not reports._readable(summary):
            raise LiveReadError("the subject summary is unreadable")
        rows, offset = [], 0
        while True:
            page = client.call_function(REPORTS_ACCOUNT, "reports_by_subject",
                                        {"subject": subject, "from": offset, "limit": 50})
            if not isinstance(page, list):
                raise LiveReadError("the subject report page is unreadable")
            for item in page:
                if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 0:
                    raise LiveReadError("the subject report index is unreadable")
                report = client.call_function(REPORTS_ACCOUNT, "get_report", {"id": item["id"]})
                rows.append(self._report(report, subject))
            if len(page) < 50:
                break
            offset += len(page)
        return {"subject_summary": summary, "reports": rows}

    def _recent_reports(self):
        client = self._reports_client()
        totals = client.call_function(REPORTS_ACCOUNT, "totals", {})
        if (not isinstance(totals, dict) or totals.keys() != {"reports", "disputes"}
                or any(type(totals.get(field)) is not int or totals[field] < 0
                       for field in ("reports", "disputes"))):
            raise LiveReadError("the report totals view is unreadable")
        start = max(0, totals["reports"] - 20)
        rows = []
        for report_id in range(totals["reports"] - 1, start - 1, -1):
            report = client.call_function(REPORTS_ACCOUNT, "get_report", {"id": report_id})
            rows.append(self._report(report))
        return {"reports": rows}

    def _reports_client(self):
        url = os.environ.get("NEAR_RPC_URL") or rpc.TESTNET_RPC_URL
        return rpc.Client(url, "off")

    @staticmethod
    def _report(report, expected_subject=None):
        required = {"id", "subject", "kind", "outcome", "state", "evidence_url", "evidence_sha256",
                    "submitter", "moderator"}
        if (not isinstance(report, dict) or not required.issubset(report)
                or type(report["id"]) is not int or report["id"] < 0
                or not all(isinstance(report[field], str) for field in
                           ("subject", "kind", "outcome", "state", "evidence_url", "evidence_sha256", "submitter"))
                or (report["moderator"] is not None and not isinstance(report["moderator"], str))
                or (expected_subject is not None and report["subject"] != expected_subject)):
            raise LiveReadError("a report view is unreadable")
        return {"id": report["id"], "subject": report["subject"], "kind": report["kind"],
                "outcome": report["outcome"], "status": report["state"],
                "evidence_url": report["evidence_url"], "evidence_sha256": report["evidence_sha256"],
                "submitter": report["submitter"], "moderator": report["moderator"]}

    @staticmethod
    def _verdict_shape(verdict, checks, reasons, evidence_hash, freshness, latency_ms):
        statuses = {check["status"] for check in checks}
        if "signal" in statuses or verdict == "DENY":
            outcome, action = "DENY", "refuse"
        elif "unknown" in statuses or verdict != "ALLOW":
            outcome, action = "ASK", "ask"
        else:
            outcome, action = "ALLOW", "sign"
        return {"outcome": outcome, "action": action, "reasons": reasons,
                "checks": [{"name": check["id"], "status": check["status"], "detail": check["detail"]}
                           for check in checks],
                "evidence_hash": evidence_hash, "freshness": dict(freshness), "verdict": None,
                "signing": "never", "latency_ms": latency_ms}

    def _rate_limit(self, client):
        now = self.clock()
        window = math.floor(now / RATE_WINDOW_S)
        with self._lock:
            start, count = self._rates.get(client, (window, 0))
            if window > start:
                start, count = window, 0
            if count >= self.rate_per_minute:
                self._rates.move_to_end(client)
                return max(1, math.ceil((start + 1) * RATE_WINDOW_S - now))
            if client not in self._rates and len(self._rates) >= MAX_RATE_CLIENTS:
                self._rates.popitem(last=False)
            self._rates[client] = (start, count + 1)
            self._rates.move_to_end(client)
        return None

    def _cache_get(self, key):
        now = self.clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is None:
                return None
            expires, answer, response_size = cached
            if expires <= now:
                del self._cache[key]
                self._cache_bytes -= response_size
                return None
            self._cache.move_to_end(key)
            return (answer[0], copy.deepcopy(answer[1]), dict(answer[2]))

    def _cache_put(self, key, answer, response_size):
        with self._lock:
            now = self.clock()
            expired = [cache_key for cache_key, (expires, _, _) in self._cache.items() if expires <= now]
            for cache_key in expired:
                _, _, size = self._cache.pop(cache_key)
                self._cache_bytes -= size
            previous = self._cache.pop(key, None)
            if previous is not None:
                self._cache_bytes -= previous[2]
            if response_size > MAX_CACHE_BYTES:
                return
            self._cache[key] = (now + self.cache_ttl_s,
                                (answer[0], copy.deepcopy(answer[1]), dict(answer[2])), response_size)
            self._cache_bytes += response_size
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size or self._cache_bytes > MAX_CACHE_BYTES:
                _, (_, _, size) = self._cache.popitem(last=False)
                self._cache_bytes -= size

    @staticmethod
    def _response_size(body, limit):
        size = 1
        for part in json.JSONEncoder(ensure_ascii=False).iterencode(body):
            if len(part) > limit - size:
                return None
            size += len(part.encode("utf-8"))
            if size > limit:
                return None
        return size

    @staticmethod
    def _client_ip(peer, headers):
        if peer != "127.0.0.1":
            return peer
        forwarded = headers.get("X-Forwarded-For")
        if not forwarded:
            return peer
        try:
            return str(ipaddress.ip_address(forwarded.split(",", 1)[0].strip()))
        except ValueError:
            return peer

    @staticmethod
    def _query(query, required):
        try:
            pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        except ValueError as error:
            raise LiveInputError("The query string is malformed.") from error
        if len(pairs) != len(required) or {key for key, _ in pairs} != required:
            names = ", ".join(sorted(required))
            raise LiveInputError(f"The query must contain exactly these fields: {names}.")
        return dict(pairs)

    @staticmethod
    def _no_query(query):
        if query:
            raise LiveInputError("This route does not take query fields.")

    @staticmethod
    def _account(value, field):
        try:
            return canon.account_id(value, field)
        except canon.CanonError as error:
            raise LiveInputError(f"{field} must be a valid NEAR account id.") from error

    @staticmethod
    def _amount(value):
        if not isinstance(value, str):
            raise LiveInputError("amount must be a decimal string.")
        try:
            return canon.amount_field(value)
        except canon.CanonError as error:
            raise LiveInputError("amount must be a decimal string between 1 and the u128 maximum.") from error

    @staticmethod
    def _error(status, code, message, headers=None):
        return status, {"error": code, "message": message}, headers or {}
