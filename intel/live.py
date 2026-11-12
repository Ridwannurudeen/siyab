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
from intel import accounts, enrichment, history, identity, labels, portfolio, reports

RATE_PER_MINUTE = 60
COST_PER_MINUTE = 240
GLOBAL_PER_MINUTE = 600
RATE_WINDOW_S = 60
MAX_IN_FLIGHT = 8
MAX_CLIENT_IN_FLIGHT = 4
MAX_CLIENT_HEAVY = 2
MAX_CLIENT_ADMITTED = 12
QUEUE_WAIT_S = 5
HEAVY_SLOTS = 3
HEAVY_ROUTES = ("portfolio", "portfolio-at-block", "holdings", "graph", "backfill")
# Units a request costs against COST_PER_MINUTE, about the provider calls it can make; portfolio,
# portfolio-at-block and holdings depend on their query (LiveAPI._cost) and every other route costs 1.
ROUTE_COSTS = {"verdict": 10, "reports/recent": 21, "reports": 22, "graph": 6, "identity": 4, "intents": 4,
               "scan": 2, "backfill": 2, "profile": 1, "transactions": 1, "transfers": 1}
# The max_tokens portfolio.PortfolioData.inspect reads when the query names none.
PORTFOLIO_DEFAULT_TOKENS = 40
# Routes the process cache never holds: retained archives, the entity directory and backfill writes.
UNCACHED_ROUTES = ("snapshot", "entity-directory", "history", "backfill", "enrichment", "portfolio-history")
MAX_SUBJECT_REPORTS = 20
REPORTS_PAGE = 50
VERDICT_NEARBLOCKS_ENV = "SIYAB_VERDICT_NEARBLOCKS"
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


class _NoNearBlocks:
    """The NearBlocks client of the hosted verdict unless SIYAB_VERDICT_NEARBLOCKS=1: it sends nothing, so an
    age the archival read cannot date reads unknown."""

    def __init__(self):
        self.last_source = {"provider": "nearblocks", "request_sha256": None, "fetched_at": None}

    def get(self, url, **kwargs):
        raise rpc.RateLimited("NearBlocks is not read by the hosted verdict route")


class _Flight:
    """One upstream read that identical concurrent requests wait for."""

    def __init__(self):
        self.done = threading.Event()
        self.answer = (500, {"error": "internal_error", "message": "The live check could not be completed."}, {})


class LiveAPI:
    def __init__(self, store=None, *, clock=time.monotonic, wall_clock=time.time,
                 perf_clock=time.perf_counter, rate_per_minute=RATE_PER_MINUTE,
                 request_timeout_s=REQUEST_TIMEOUT_S, queue_wait_s=QUEUE_WAIT_S, cache_ttl_s=CACHE_TTL_S,
                 cache_size=MAX_CACHE_ENTRIES, max_in_flight=MAX_IN_FLIGHT, history_store=None,
                 entity_directory=None, portfolio_data=None, portfolio_history=None):
        if type(rate_per_minute) is not int or rate_per_minute < 1:
            raise ValueError("rate_per_minute must be a positive integer")
        if not isinstance(request_timeout_s, (int, float)) or isinstance(request_timeout_s, bool) or not (
                0 < request_timeout_s < 30):
            raise ValueError("request_timeout_s must be greater than zero and less than 30")
        if type(cache_size) is not int or cache_size < 1:
            raise ValueError("cache_size must be a positive integer")
        self.store = store or labels.load()
        self.history_store = history_store
        self.entity_directory = entity_directory
        self.portfolio_data = portfolio_data
        self.portfolio_history = portfolio_history
        self.clock = clock
        self.wall_clock = wall_clock
        self.perf_clock = perf_clock
        self.rate_per_minute = rate_per_minute
        self.request_timeout_s = request_timeout_s
        self.queue_wait_s = queue_wait_s
        self.cache_ttl_s = cache_ttl_s
        self.cache_size = cache_size
        self.max_in_flight = max_in_flight
        self._slots = threading.BoundedSemaphore(max_in_flight)
        self._heavy_slots = threading.BoundedSemaphore(min(HEAVY_SLOTS, max_in_flight))
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._rates = OrderedDict()
        self._requests = [None, 0]
        self._in_flight = {}
        self._flights = {}
        self._cache = OrderedDict()
        self._cache_bytes = 0

    def handle(self, target, peer, headers):
        arrived = time.monotonic()
        try:
            parsed = urllib.parse.urlsplit(target)
        except ValueError:
            return self._error(400, "invalid_input", "The request target is malformed.")
        if parsed.path != "/live" and not parsed.path.startswith("/live/"):
            return None
        client = self._client_ip(peer, headers)
        route = self._route_name(parsed.path)
        key = self._cache_key(route, parsed.path, parsed.query)
        with self._lock:
            cached = self._cache.get(key) if key is not None else None
            if cached is not None and cached[0] > self.clock():
                self._cache.move_to_end(key)
                cached = cached[1]
            else:
                cached = None
            cost = 1 if cached is not None else self._cost(route, parsed.query)
        refused = self._rate_limit(client, cost)
        if refused is not None:
            status, code, message, retry_after = refused
            return self._error(status, code, message, {"Retry-After": str(retry_after)})
        if cached is not None:
            return cached[0], copy.deepcopy(cached[1]), dict(cached[2])
        heavy = route in HEAVY_ROUTES
        with self._condition:
            state = self._in_flight.setdefault(client, [0, 0, 0])
            if state[0] >= MAX_CLIENT_ADMITTED:
                return self._error(429, "too_many_in_flight", "This client has too many live requests waiting.",
                                   {"Retry-After": "1"})
            state[0] += 1
        leader, flight = False, None
        try:
            with self._condition:
                while True:
                    cached = self._cache.get(key) if key is not None else None
                    if cached is not None and cached[0] > self.clock():
                        self._cache.move_to_end(key)
                        answer = cached[1]
                        return answer[0], copy.deepcopy(answer[1]), dict(answer[2])
                    flight = self._flights.get(key) if key is not None else None
                    if flight is not None:
                        break
                    remaining = arrived + self.queue_wait_s - time.monotonic()
                    if remaining <= 0:
                        return self._error(429, "too_many_in_flight", "This client has too many live requests waiting.",
                                           {"Retry-After": "1"})
                    if state[1] < MAX_CLIENT_IN_FLIGHT and (not heavy or state[2] < MAX_CLIENT_HEAVY):
                        leader = True
                        state[1] += 1
                        if heavy:
                            state[2] += 1
                        if key is not None:
                            flight = self._flights[key] = _Flight()
                        break
                    self._condition.wait(remaining)
            if not leader:
                if not flight.done.wait(max(0, arrived + self.request_timeout_s - time.monotonic())):
                    return self._error(504, "timeout", "The live read exceeded its request deadline.")
                return flight.answer[0], copy.deepcopy(flight.answer[1]), dict(flight.answer[2])
            answer = self._read(route, parsed, key, arrived)
            if flight is not None:
                flight.answer = answer
            return answer
        finally:
            with self._condition:
                state[0] -= 1
                if leader:
                    state[1] -= 1
                    if heavy:
                        state[2] -= 1
                    if flight is not None:
                        flight.done.set()
                        del self._flights[key]
                if state == [0, 0, 0]:
                    del self._in_flight[client]
                self._condition.notify_all()

    def _read(self, route, parsed, key, arrived):
        """Answer a request that found no cached answer, holding one of the slots for the upstream read."""
        heavy = route in HEAVY_ROUTES
        if heavy and not self._heavy_slots.acquire(timeout=max(0, arrived + self.queue_wait_s - time.monotonic())):
            return self._error(503, "busy", "The live API's slots for heavy account reads are busy.",
                               {"Retry-After": "1"})
        try:
            if not self._slots.acquire(timeout=max(0, arrived + self.queue_wait_s - time.monotonic())):
                return self._error(503, "busy", "The live API is busy.", {"Retry-After": "1"})
            try:
                with rpc.caller_class("backfill" if route == "backfill" else "anonymous") as refusals:
                    answer, response_size = self._attempt(parsed, arrived)
            finally:
                self._slots.release()
        finally:
            if heavy:
                self._heavy_slots.release()
        if refusals:
            if answer[0] >= 500 and answer[0] != 504:
                retry_after = max(refusal.retry_after for refusal in refusals)
                return self._error(503, "provider_budget", "A provider budget is spent; retained data stays readable.",
                                   {"Retry-After": str(retry_after)})
            return answer
        if answer[0] == 200 and key is not None:
            settled = route == "portfolio-at-block" and self._settled_block(answer[1])
            self._cache_put(key, answer, response_size, math.inf if settled else self.cache_ttl_s)
        return answer

    def _attempt(self, parsed, arrived):
        """The answer to one routed request and its response size (None for an error answer)."""
        deadline = arrived + self.request_timeout_s
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("request deadline exceeded")
            with rpc.request_deadline(max(deadline - time.monotonic(), 0.001)):
                snapshot = self.exact_snapshot(parsed.path, parsed.query)
                if snapshot is None:
                    status, body = self._route(parsed.path, parsed.query)
                    response_headers = {}
                else:
                    status, body, response_headers = snapshot
            response_size = len(body) if isinstance(body, bytes) else self._response_size(body, MAX_RESPONSE_BYTES)
            if response_size is None:
                return self._error(503, "response_too_large", "The live response exceeds the size limit."), None
            if time.monotonic() >= deadline:
                raise TimeoutError("request deadline exceeded")
        except (LiveInputError, enrichment.EnrichmentInputError, portfolio.PortfolioInputError) as error:
            return self._error(400, "invalid_input", str(error)), None
        except TimeoutError:
            return self._error(504, "timeout", "The live read exceeded its request deadline."), None
        except history.HistoryCapacityError as error:
            return (503, {"error": "archive_capacity", "message": str(error),
                          "coverage": {"history_complete": False, "new_observations_paused": True}}, {"Retry-After": "60"}), None
        except (LiveReadError, enrichment.EnrichmentReadError, portfolio.PortfolioReadError):
            return self._error(503, "read_failed", "A required live view could not be read."), None
        except rules.UNAVAILABLE:
            return self._error(503, "read_failed", "A required live view could not be read."), None
        except Exception:
            return self._error(500, "internal_error", "The live check could not be completed."), None
        return (status, body, response_headers), response_size

    @staticmethod
    def _route_name(path):
        if path.startswith("/live/accounts/"):
            parts = path.split("/")
            return parts[4] if len(parts) == 5 else "account"
        if path.startswith("/live/scan/"):
            return "scan"
        if path.startswith(("/live/account-data/", "/live/portfolio-data/")):
            return "snapshot"
        return path[len("/live/"):]

    @staticmethod
    def _cost(route, query):
        """The units a request of route with query costs; an unreadable query costs the route's default."""
        if route in ROUTE_COSTS:
            return ROUTE_COSTS[route]
        if route not in ("portfolio", "portfolio-at-block", "holdings"):
            return 1
        try:
            values = dict(urllib.parse.parse_qsl(query, keep_blank_values=True, max_num_fields=3))
        except ValueError:
            values = {}
        if route == "portfolio-at-block":
            contracts = values.get("contracts")
            return 2 + 2 * min(len(contracts.split(",")) if contracts else 0, portfolio.MAX_HISTORICAL_TOKENS)
        # A holdings page defaults to its largest size.
        field, base, default, upper = (("max_tokens", 6, PORTFOLIO_DEFAULT_TOKENS, portfolio.MAX_TOKENS)
                                       if route == "portfolio" else
                                       ("limit", 1, accounts.MAX_HOLDINGS_PAGE, accounts.MAX_HOLDINGS_PAGE))
        value = values.get(field, "")
        return base + (min(int(value), upper) if re.fullmatch(r"[0-9]{1,4}", value) else default)

    def _cache_key(self, route, path, query):
        """The cache and single flight key from decoded, sorted, normalised values; None for a route the cache
        bypasses or a request its route refuses as invalid input."""
        if route in UNCACHED_ROUTES:
            return None
        try:
            pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        except ValueError:
            return None
        if path.startswith(("/live/accounts/", "/live/scan/")):
            parts = path.split("/")
            try:
                parts[3] = canon.account_id(urllib.parse.unquote(parts[3]), "account")
            except canon.CanonError:
                return None
            path = "/".join(parts)
        if route == "intents":
            pairs = [(name, value.lower() if name == "recipient" else value) for name, value in pairs]
        elif route == "portfolio-at-block":
            pairs = [(name, ",".join(sorted(value.split(","))) if name == "contracts" else value) for name, value in pairs]
        return path, tuple(sorted(pairs))

    @staticmethod
    def _settled_block(body):
        """A block-pinned portfolio whose every selected balance, metadata and price read succeeded: the only
        answer the cache keeps without expiry, since a block-pinned read never changes."""
        return ("archive_paused" not in body and body.get("summary", {}).get("unpriced_assets") == 0
                and all(asset.get("balance_status") == "historical" and asset.get("metadata") is not None
                        for asset in body.get("assets", ())))

    def exact_snapshot(self, path, query):
        if not path.startswith(("/live/account-data/", "/live/portfolio-data/")):
            return None
        match = re.fullmatch(r"/live/(account-data|portfolio-data)/([a-f0-9]{64})\.json", path)
        if match is None or query:
            return self._error(400, "invalid_input", "A snapshot URL contains one lowercase SHA-256 identifier and no query fields.")
        archive = self.portfolio_history if match[1] == "portfolio-data" else self.history_store
        if archive is None:
            return self._error(503, "archive_unavailable", "The account observation archive is unavailable.")
        try:
            body = archive.get_graph_bytes(match[2]) if match[1] == "account-data" else archive.get_bytes(match[2])
        except TimeoutError:
            raise
        except (history.HistoryInputError, history.HistoryReadError, portfolio.PortfolioInputError, portfolio.PortfolioReadError, OSError, sqlite3.Error):
            return self._error(503, "read_failed", "The stored public observation could not be read.")
        if body is None:
            return self._error(404, "not_found", "No retained public observation matches this identifier.")
        return 200, body, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "public, max-age=31536000, immutable"}

    def _route(self, path, query):
        if path.startswith("/live/accounts/"):
            return self._account_read(path, query)
        if path == "/live/entity-directory":
            try:
                pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
            except ValueError:
                raise LiveInputError("Directory query fields are malformed.") from None
            if len({name for name, _ in pairs}) != len(pairs) or any(name not in {"query", "category", "limit", "offset"} for name, _ in pairs):
                raise LiveInputError("Directory query fields are unsupported or repeated.")
            values = dict(pairs)
            for field, lower, upper in (("limit", 1, 100), ("offset", 0, enrichment.MAX_ENTRIES)):
                if field in values:
                    if re.fullmatch(r"0|[1-9][0-9]{0,3}", values[field]) is None or not lower <= int(values[field]) <= upper:
                        raise LiveInputError("Directory pagination is outside its supported range.")
                    values[field] = int(values[field])
            if self.entity_directory is None:
                raise LiveReadError("The sourced entity directory is unavailable.")
            return 200, self.entity_directory.search(**values)
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
                   "identity": set(), "history": {"kind", "before", "limit"}, "backfill": {"kind", "cursor", "max_pages"},
                   "enrichment": set(), "portfolio": {"max_tokens"}, "portfolio-history": {"before", "limit", "asset_id"},
                   "portfolio-at-block": {"block_height", "contracts"}}
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
        if kind == "portfolio-at-block" and "block_height" not in values:
            raise LiveInputError("A historical portfolio requires block_height.")
        for field in ("offset", "limit", "depth", "before", "max_pages", "max_tokens", "block_height"):
            if field in values:
                upper = canon.U64_MAX if field == "block_height" else 2 ** 63 - 1 if field == "before" else 10000 if field == "offset" else 2 if field in ("depth", "max_pages") else portfolio.MAX_TOKENS if field == "max_tokens" else 20 if kind == "holdings" else 100
                lower = 0 if field in ("offset", "before") else 1
                if not re.fullmatch(r"0|[1-9][0-9]{0,19}", values[field]) or not lower <= int(values[field]) <= upper:
                    raise LiveInputError("Investigation pagination or graph depth is outside its supported range.")
                values[field] = int(values[field])
        if "contracts" in values:
            contracts = values["contracts"].split(",")
            if len(contracts) > portfolio.MAX_HISTORICAL_TOKENS or len(set(contracts)) != len(contracts):
                raise LiveInputError("Historical portfolios accept at most 20 distinct token contracts.")
            for contract in contracts:
                validated = self._account(contract, "contract")
                if validated == "testnet" or validated.endswith(".testnet"):
                    raise LiveInputError("Historical portfolios read mainnet token contracts only.")
            values["contracts"] = contracts
        try:
            if kind == "enrichment":
                if self.entity_directory is None:
                    raise LiveReadError("The sourced entity directory is unavailable.")
                result = self.entity_directory.lookup(account)
            elif kind == "portfolio-history":
                if self.portfolio_history is None:
                    raise LiveReadError("The portfolio observation archive is unavailable.")
                result = self.portfolio_history.list_records(account, **values)
            elif kind in ("portfolio", "portfolio-at-block"):
                if self.portfolio_data is None:
                    raise LiveReadError("The portfolio data service is unavailable.")
                if kind == "portfolio":
                    result = self.portfolio_data.inspect(account, **values)
                else:
                    result = self.portfolio_data.historical(account, values.pop("block_height"), **values)
                if self.portfolio_history is not None:
                    try:
                        result = {**result, "snapshot": self.portfolio_history.save(result)}
                    except portfolio.PortfolioCapacityError:
                        result = {**result, "archive_paused": True}
            elif kind in ("history", "backfill"):
                if self.history_store is None:
                    raise LiveReadError("The account observation archive is unavailable.")
                archive_kind = values.pop("kind")
                if kind == "history":
                    result = self.history_store.list_records(account, archive_kind, **values)
                else:
                    result = self.history_store.backfill_account(accounts.AccountData(self.store), account, archive_kind, **values)
            elif kind == "identity":
                result = identity.IdentityData(self.store).inspect(account)
            elif kind == "profile":
                result = accounts.AccountData(self.store).profile(account)
            elif kind == "holdings":
                result = accounts.AccountData(self.store).holdings(account, **values)
            elif kind == "transactions":
                result = accounts.AccountData(self.store).transactions(account, **values)
            elif kind == "transfers":
                result = accounts.AccountData(self.store).transfers(account, **values)
            else:
                result = accounts.AccountData(self.store).neighborhood(account, **values)
            if self.entity_directory is not None:
                if kind in ("profile", "identity"):
                    result = {**result, "enrichment": self.entity_directory.lookup(account)}
                elif kind == "graph":
                    result = {**result, "nodes": [{**node, "enrichment": self.entity_directory.lookup(node["id"])} for node in result["nodes"]]}
            if self.history_store is not None:
                try:
                    if kind in history.KINDS:
                        self.history_store.record_page(account, kind, result)
                    elif kind == "graph":
                        result = {**result, "snapshot": self.history_store.save_graph(result)}
                except history.HistoryCapacityError:
                    result = {**result, "archive_paused": True}
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
        nearblocks = (rpc.Client(os.environ.get("NEARBLOCKS_API_URL") or rpc.NEARBLOCKS_URL, "off", kind="http")
                      if os.environ.get(VERDICT_NEARBLOCKS_ENV) == "1" else _NoNearBlocks())
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
        answer = self._verdict_shape(judgement.verdict, judgement.checks, judgement.reasons,
                                     evidence.evidence_hash(document), judgement.observed, latency_ms)
        answer["evidence"] = evidence.canonical_bytes(document).decode("utf-8")
        return answer

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
        recipient = recipient.lower()
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

        def page(start, limit):
            items = client.call_function(REPORTS_ACCOUNT, "reports_by_subject",
                                         {"subject": subject, "from": start, "limit": limit})
            if not isinstance(items, list) or len(items) > limit:
                raise LiveReadError("the subject report page is unreadable")
            return items

        items = page(0, REPORTS_PAGE)
        count = len(items)
        if count == REPORTS_PAGE:
            # The contract has no count view: double past the last offset known to hold a report, then halve
            # back to the first empty one, so a long index costs a few one-report pages.
            low, step = REPORTS_PAGE - 1, REPORTS_PAGE
            while low + step <= 2 ** 64 and page(low + step, 1):
                low, step = low + step, step * 2
            if low + step > 2 ** 64:
                raise LiveReadError("the subject report index is unreadable")
            high = low + step
            while high - low > 1:
                middle = (low + high) // 2
                low, high = (middle, high) if page(middle, 1) else (low, middle)
            count = high
            items = page(max(0, count - MAX_SUBJECT_REPORTS), MAX_SUBJECT_REPORTS)
        rows = []
        for item in items[-MAX_SUBJECT_REPORTS:]:
            if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 0:
                raise LiveReadError("the subject report index is unreadable")
            report = client.call_function(REPORTS_ACCOUNT, "get_report", {"id": item["id"]})
            rows.append(self._report(report, subject))
        return {"subject_summary": summary, "reports": rows, "truncated": count > MAX_SUBJECT_REPORTS}

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

    def _rate_limit(self, client, cost):
        """None when the request is admitted and counted, else (status, error, message, Retry-After seconds)."""
        now = self.clock()
        window = math.floor(now / RATE_WINDOW_S)
        retry_after = max(1, math.ceil((window + 1) * RATE_WINDOW_S - now))
        with self._lock:
            start, count, spent = self._rates.get(client, (window, 0, 0))
            if window > start:
                start, count, spent = window, 0, 0
            if count >= self.rate_per_minute or spent + cost > COST_PER_MINUTE:
                self._rates.move_to_end(client)
                limit = "rate limit" if count >= self.rate_per_minute else "cost budget"
                return 429, "rate_limited", f"The client {limit} was reached.", retry_after
            if self._requests[0] != window:
                self._requests = [window, 0]
            if self._requests[1] >= GLOBAL_PER_MINUTE:
                return 503, "busy", "The live API request ceiling was reached.", retry_after
            if client not in self._rates:
                # Entries are kept in order of use, so those from an earlier minute are at the front.
                while self._rates and next(iter(self._rates.values()))[0] < window:
                    self._rates.popitem(last=False)
                if len(self._rates) >= MAX_RATE_CLIENTS:
                    return 503, "busy", "The live API client table is full.", retry_after
            self._requests[1] += 1
            self._rates[client] = (start, count + 1, spent + cost)
            self._rates.move_to_end(client)
        return None

    def _cache_put(self, key, answer, response_size, ttl):
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
            self._cache[key] = (now + ttl,
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
        """The rate key: the rightmost X-Forwarded-For entry when the peer is the local proxy, else the peer; an
        IPv6 address is keyed by its /64."""
        candidates = [peer]
        if peer == "127.0.0.1":
            values = headers.get_all("X-Forwarded-For") if hasattr(headers, "get_all") else [headers.get("X-Forwarded-For")]
            if values and values[-1]:
                candidates.insert(0, values[-1].rsplit(",", 1)[-1].strip())
        for candidate in candidates:
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.version == 4:
                return str(address)
            if address.ipv4_mapped is not None:
                return str(address.ipv4_mapped)
            return str(ipaddress.ip_network(f"{address}/64", strict=False))
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
