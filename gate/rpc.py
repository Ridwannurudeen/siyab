"""NEAR JSON-RPC and HTTP JSON client with record and replay fixtures.

The mode comes from GATE_FIXTURES_MODE:

    replay  (default) answer from fixtures only; a request with no fixture raises FixtureMiss,
            which the rules report as unknown, never as a pass
    record  answer from a fixture younger than 24 hours without a request; otherwise fetch,
            write the fixture and answer
    off     fetch every time and write nothing

A fixture is fixtures/rpc/<key>.json holding {"request", "response", "url", "fetched_at"}, where
the key is the sha256 hex of json.dumps({"url", "method", "params"}, sort_keys=True,
separators=(",", ":")). A POST body is never written: its params are {"body_sha256": ...}, the
sha256 of the body as sorted compact JSON. No header is written, and a URL with user info or a
query string is refused. Every request has a 30 s timeout. HTTP errors (a 429 included) raise
HttpError and are never written unless a GET caller explicitly selects a status for recording;
selected statuses are stored as markers and replay as HttpError. A truncated or broken reply raises TransportError. A body
without the fields the caller reads raises MalformedResponse. NearBlocks fetches (its free tier
allows 6 calls a minute and 333 a day) are spaced at least 10 s apart, and past 300 in one UTC
day per process they raise RateLimited without a request. Inside request_deadline (a request being
served) a NearBlocks fetch whose slot is not free yet raises RateLimited instead of sleeping.

In mode off, each fetch is first reserved against the durable provider budget (intel/budget.py:reserve)
for the provider class of its host and the caller class the entry point set with caller_class
(operator when none is set). A spent budget raises BudgetExhausted, a RateLimited, without a request.
The FastNEAR key (an Authorization header to a FastNEAR host) goes out only when reserve allows it:
when no budget database is configured, or a key cooldown runs, the request goes out without it. A 403
or 429 answering a keyed request starts that cooldown (intel/budget.py:key_failure). Replay and record
never touch the budget database.
"""

import base64
import contextvars
import hashlib
import http.client
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from gate import base58
from intel import budget

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "rpc"
MODES = ("replay", "record", "off")
KINDS = ("rpc", "http")
TIMEOUT_S = 30
MAX_AGE_S = 24 * 60 * 60
HTTP_STATUS_MARKER = "_gate_http_status"
MAINNET_RPC_URL = "https://free.rpc.fastnear.com"
TESTNET_RPC_URL = "https://test.rpc.fastnear.com"
ARCHIVAL_RPC_URL = "https://archival-rpc.mainnet.fastnear.com"
ARCHIVAL_TESTNET_RPC_URL = "https://archival-rpc.testnet.fastnear.com"
NEARBLOCKS_URL = "https://api.nearblocks.io"
NEARBLOCKS_TESTNET_URL = "https://api-testnet.nearblocks.io"
NEARBLOCKS_HOSTS = ("api.nearblocks.io", "api-testnet.nearblocks.io")
NEARBLOCKS_MIN_INTERVAL_S = 10
NEARBLOCKS_DAILY_BUDGET = 300
# The code_hash view_account reports for an account with no contract: base58 of 32 zero bytes.
EMPTY_CODE_HASH = base58.encode(bytes(32))
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_REQUEST_DEADLINE = contextvars.ContextVar("gate_rpc_request_deadline", default=None)
_URL_IN_ERROR = re.compile(r"https?://[^\s\"'<>]+")
_CALLER_CLASS = contextvars.ContextVar("gate_rpc_caller_class", default=None)
_BUDGET_REFUSALS = contextvars.ContextVar("gate_rpc_budget_refusals", default=None)
# Committed recordings reduced to the fields the code reads (gate/demo.py:4), by path under fixtures/ to the
# sha256 of their bytes. Replay alone may answer from one, and only while its bytes match.
REDUCED_RECORDINGS = json.loads((FIXTURES_DIR.parent / "reduced-recordings.json").read_text(
    encoding="utf-8"))["recordings"]
_REDUCED_FIELDS = ("block_hash", "amount", "locked", "storage_usage", "logs")
_REDUCED_SCHEMA = contextvars.ContextVar("gate_rpc_reduced_schema", default=None)
BLOCK_HASH_CACHE_SIZE = 1024
# Per process NearBlocks use: host -> {"last": seconds of the last fetch, "day": UTC date, "count"}.
_NEARBLOCKS_USE = {}


@contextmanager
def request_deadline(seconds):
    """Bound every RPC and HTTP read in the current request to one monotonic deadline."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        raise ValueError("request deadline must be a positive number of seconds")
    deadline = time.monotonic() + seconds
    current = _REQUEST_DEADLINE.get()
    token = _REQUEST_DEADLINE.set(min(current, deadline) if current is not None else deadline)
    try:
        yield
    finally:
        _REQUEST_DEADLINE.reset(token)


@contextmanager
def caller_class(name):
    """Meter the fetches made in this block as caller class name (intel/budget.py:CALLER_CLASSES); yields the
    list of the BudgetExhausted refusals raised in the block."""
    if name not in budget.CALLER_CLASSES:
        raise ValueError(f"caller class must be one of {budget.CALLER_CLASSES}, not {name!r}")
    refusals = []
    tokens = (_CALLER_CLASS.set(name), _BUDGET_REFUSALS.set(refusals))
    try:
        yield refusals
    finally:
        _BUDGET_REFUSALS.reset(tokens[1])
        _CALLER_CLASS.reset(tokens[0])


class _DeadlineReader(io.RawIOBase):
    def __init__(self, connection, deadline):
        super().__init__()
        self.connection = connection
        self.stream = connection.makefile("rb", buffering=0)
        self.deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request deadline exceeded")
        self.connection.settimeout(min(TIMEOUT_S, remaining))
        return self.stream.readinto(buffer)

    def close(self):
        if not self.closed:
            self.stream.close()
        super().close()


class _DeadlineSocket:
    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline

    def makefile(self, mode, *args, **kwargs):
        if mode != "rb":
            return self.connection.makefile(mode, *args, **kwargs)
        return io.BufferedReader(_DeadlineReader(self.connection, self.deadline))

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _DeadlineHTTPResponse(http.client.HTTPResponse):
    def __init__(self, connection, *args, **kwargs):
        super().__init__(_DeadlineSocket(connection, _REQUEST_DEADLINE.get()), *args, **kwargs)


class _DeadlineHTTPConnection(http.client.HTTPConnection):
    response_class = _DeadlineHTTPResponse


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    response_class = _DeadlineHTTPResponse


class _DeadlineHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_DeadlineHTTPConnection, request)


class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_DeadlineHTTPSConnection, request, context=self._context)


def _open_with_deadline(request, timeout):
    opener = urllib.request.build_opener(_DeadlineHTTPHandler, _DeadlineHTTPSHandler)
    return opener.open(request, timeout=timeout)


class FixtureMiss(Exception):
    """Replay mode found no fixture for the request."""

    def __init__(self, key, url, method, params):
        super().__init__(f"no fixture {key}.json for {method} {url}")
        self.key = key


class HttpError(Exception):
    """The server answered with a status outside 2xx; retry_after is its Retry-After header or None."""

    def __init__(self, status, url, retry_after=None):
        super().__init__(f"HTTP {status} from {url}")
        self.status = status
        self.retry_after = retry_after


class TransportError(Exception):
    """The reply was cut off or was not valid HTTP."""

    def __init__(self, message):
        super().__init__(_URL_IN_ERROR.sub("endpoint", str(message)))


class RateLimited(Exception):
    """The NearBlocks daily budget is used up; no request was sent."""


class BudgetExhausted(RateLimited):
    """A durable provider budget refused the request; none was sent. retry_after is whole seconds until it
    reopens."""

    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


class MalformedResponse(ValueError):
    """A body that lacks the fields the caller reads."""


class RpcError(Exception):
    """The JSON-RPC response, or a call_function result, carried an error."""

    def __init__(self, error):
        super().__init__(json.dumps(error, sort_keys=True))
        self.error = error

    @property
    def cause(self):
        """The nearcore cause name, such as UNKNOWN_ACCOUNT, when the error has one."""
        if isinstance(self.error, dict) and isinstance(self.error.get("cause"), dict):
            return self.error["cause"].get("name")
        return None


def fixture_key(url, method, params):
    text = json.dumps({"url": url, "method": method, "params": params}, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def body_params(body):
    """The params a POST body is recorded and keyed as: only its sha256."""
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"body_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ValueError("a URL with user info would be written into fixtures; it is refused")
    if parts.query:
        raise ValueError("a URL with a query string would be written into fixtures; it is refused")
    return url


def _uint(value):
    """True for a non negative int that is not a bool (JSON true would otherwise pass as 1)."""
    return type(value) is int and value >= 0


def _timestamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).strftime(_TIME_FORMAT)


def _seconds(timestamp):
    return datetime.strptime(timestamp, _TIME_FORMAT).replace(tzinfo=UTC).timestamp()


def _decimal(value):
    return type(value) is str and re.fullmatch(r"[0-9]+", value) is not None


def _uint64(value):
    return _uint(value) and value < 2**64


def _balance(value):
    if not _decimal(value):
        return False
    digits = value.lstrip("0") or "0"
    return len(digits) <= 39 and int(digits) < 2**128


def _text(value):
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _int64(value):
    return type(value) is int and -2**63 <= value < 2**63


def _decimal_uint64(value):
    if not _decimal(value):
        return False
    digits = value.lstrip("0") or "0"
    return len(digits) <= 20 and int(digits) < 2**64


def _hash(value):
    if type(value) is not str or not 32 <= len(value) <= 44:
        return False
    try:
        return len(base58.decode(value)) == 32
    except ValueError:
        return False


def _public_key(value, *, handle=False):
    if type(value) is not str:
        return False
    kind, separator, encoded = value.partition(":")
    if not separator:
        kind, encoded = "ed25519", value
    sizes = {"ed25519": 32, "secp256k1": 64,
             **({"ml-dsa-65-hash": 32} if handle else {"ml-dsa-65": 1952})}
    size = sizes.get(kind)
    if size is None or not size <= len(encoded) <= size * 2:
        return False
    try:
        return len(base58.decode(encoded)) == size
    except ValueError:
        return False


def _account_id(value):
    return (type(value) is str and 2 <= len(value) <= 64
            and re.fullmatch(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", value) is not None)


def _base64(value):
    if type(value) is not str:
        return False
    try:
        return base64.b64encode(base64.b64decode(value, validate=True)).decode("ascii") == value
    except ValueError:
        return False


def _error_variant(value):
    cause = value["cause"]
    pair = (value["name"], cause["name"])
    if pair not in _ERROR_PAIRS:
        return False
    code = {("REQUEST_VALIDATION_ERROR", "METHOD_NOT_FOUND"): -32601,
            ("REQUEST_VALIDATION_ERROR", "PARSE_ERROR"): -32700}.get(pair, -32000)
    if value["code"] != code:
        return False
    fields = _ERROR_PAIRS[pair]
    info = cause.get("info", {})
    schema = {**_CONTEXT_SCHEMA, **fields}
    _validate_schema(info, schema)
    return not info.keys() - schema.keys()


def _error_data(value):
    if type(value) is dict:
        _validate_schema(value, _ERROR_DATA_SCHEMA)
        if "cause" in value or value.get("name") in ("REQUEST_VALIDATION_ERROR", "HANDLER_ERROR", "INTERNAL_ERROR"):
            _validate_schema(value, _ERROR_SCHEMA)
        for child in value.values():
            _error_data(child)
    elif type(value) is list:
        for child in value:
            _error_data(child)
    return True


def _enum(value, units, variants):
    if type(value) is str:
        return value in units
    if type(value) is not dict or len(value) != 1:
        return False
    name = next(iter(value))
    if name not in variants:
        return False
    _validate_schema(value[name], variants[name])
    return True


# Wire types pinned by contracts/{gate,registry,test-token}/Cargo.lock:
# near-primitives 0.37.4 src/views.rs (AccountView, AccessKeyPermissionView,
# AccessKeyInfoView, CallResult, BlockHeaderView); src/errors.rs (FunctionCallError).
# near-primitives-core 0.37.4 src/types.rs (u64 heights/nonces, u16 NonceIndex)
# and src/account.rs (AccountContract: local, global hash, or global account).
# near-crypto 0.37.4 src/signature.rs (PublicKey versus PublicKeyHandle);
# near-jsonrpc-primitives 0.37.4 src/errors.rs and src/types/{query,blocks}.rs
# (envelope, variant codes and query context). Method schemas follow the
# contracts/test-token/Cargo.toml near-contract-standards 5.29.1 metadata/storage
# wire types and contracts/reports/src/lib.rs SubjectSummary. The pinned
# near-sdk-core 4.2.3 src/json_types/vector.rs Base64VecU8 is a byte vector,
# including empty, rather than a fixed-size CryptoHash.
# Fields are (required, schema, optional diagnostic); lists declare items, tuples alternatives.
_HOST_ERROR_VARIANTS = {
    "GuestPanic": {"panic_msg": (True, _text)},
    "InvalidPromiseIndex": {"promise_idx": (True, _uint64)},
    "InvalidPromiseResultIndex": {"result_idx": (True, _uint64)},
    "InvalidRegisterId": {"register_id": (True, _uint64)},
    "IteratorWasInvalidated": {"iterator_index": (True, _uint64)},
    "InvalidReceiptIndex": {"receipt_index": (True, _uint64)},
    "InvalidIteratorIndex": {"iterator_index": (True, _uint64)},
    "ProhibitedInView": {"method_name": (True, _text)},
    "NumberOfLogsExceeded": {"limit": (True, _uint64)},
    **{name: {"length": (True, _uint64), "limit": (True, _uint64)} for name in (
        "KeyLengthExceeded", "ValueLengthExceeded", "TotalLogLengthExceeded", "ReturnedValueLengthExceeded")},
    "NumberPromisesExceeded": {"number_of_promises": (True, _uint64), "limit": (True, _uint64)},
    "NumberInputDataDependenciesExceeded": {"number_of_input_data_dependencies": (True, _uint64), "limit": (True, _uint64)},
    "ContractSizeExceeded": {"size": (True, _uint64), "limit": (True, _uint64)},
    "Deprecated": {"method_name": (True, _text)},
    **{name: {"msg": (True, _text)} for name in (
        "ECRecoverError", "AltBn128InvalidInput", "Ed25519VerifyInvalidInput", "P256VerifyInvalidInput")},
}
_HOST_ERROR_UNITS = (
    "BadUTF16", "BadUTF8", "GasExceeded", "GasLimitExceeded", "BalanceExceeded", "EmptyMethodName",
    "IntegerOverflow", "CannotAppendActionToJointPromise", "CannotReturnJointPromise", "MemoryAccessViolation",
    "InvalidAccountId", "InvalidMethodName", "InvalidPublicKey",
)
_PREPARE_ERROR_UNITS = (
    "Serialization", "Deserialization", "InternalMemoryDeclared", "GasInstrumentation", "StackHeightInstrumentation",
    "Instantiate", "Memory", "TooManyFunctions", "TooManyLocals", "TooManyTables", "TooManyTableElements",
    "FunctionBodyTooLarge", "InstrumentedCodeTooLarge", "TooManyBlocksPerFunction", "TooManyBlocksPerContract",
    "TooManyTypes", "TooManyParamsPerFunction", "TooManyParamsPerContract", "OperandStackTooLarge",
)
_COMPILATION_ERROR_VARIANTS = {
    "CodeDoesNotExist": {"account_id": (True, _account_id)},
    "PrepareError": lambda value: type(value) is str and value in _PREPARE_ERROR_UNITS,
    "WasmerCompileError": {"msg": (True, _text)},
}
_FUNCTION_ERROR_VARIANTS = {
    "CompilationError": lambda value: _enum(value, (), _COMPILATION_ERROR_VARIANTS),
    "LinkError": {"msg": (True, _text)},
    "MethodResolveError": lambda value: type(value) is str and value in (
        "MethodEmptyName", "MethodNotFound", "MethodInvalidSignature"),
    "WasmTrap": lambda value: type(value) is str and value in (
        "Unreachable", "IncorrectCallIndirectSignature", "MemoryOutOfBounds", "CallIndirectOOB",
        "IllegalArithmetic", "MisalignedAtomicAccess", "IndirectCallToNull", "StackOverflow", "GenericTrap"),
    "HostError": lambda value: _enum(value, _HOST_ERROR_UNITS, _HOST_ERROR_VARIANTS),
    "ExecutionError": _text,
}
_FUNCTION_ERROR_SCHEMA = lambda value: _enum(value, ("WasmUnknownError", "_EVMError"), _FUNCTION_ERROR_VARIANTS)
_CONTEXT_SCHEMA = {
    "block_height": (False, _uint64), "block_hash": (False, _hash),
    "requested_account_id": (False, _account_id), "account_id": (False, _account_id),
    "contract_account_id": (False, _account_id),
}
_ERROR_BLOCK_SCHEMA = {"block_height": (True, _uint64), "block_hash": (True, _hash)}
_ERROR_ACCOUNT_SCHEMA = {**_ERROR_BLOCK_SCHEMA, "requested_account_id": (True, _account_id)}
_GLOBAL_IDENTIFIER_SCHEMA = ({"hash": (True, _hash)}, {"account_id": (True, _account_id)})
# Pair variants and required info: near-jsonrpc-primitives 0.37.4, src/errors.rs
# (RpcErrorKind, RpcRequestValidationErrorKind), src/types/{query,blocks}.rs.
# TOO_MANY_ACCESS_KEYS is the recorded query variant in fixtures/rpc for account near.
# Historical query recordings omit the newer structured error alongside vm_error.
_ERROR_PAIRS = {
    ("REQUEST_VALIDATION_ERROR", "METHOD_NOT_FOUND"): {"method_name": (True, _text)},
    ("REQUEST_VALIDATION_ERROR", "PARSE_ERROR"): {"error_message": (True, _text)},
    ("INTERNAL_ERROR", "INTERNAL_ERROR"): {"error_message": (True, _text)},
    ("HANDLER_ERROR", "NO_SYNCED_BLOCKS"): {},
    ("HANDLER_ERROR", "NOT_SYNCED_YET"): {},
    ("HANDLER_ERROR", "UNAVAILABLE_SHARD"): {"requested_shard_id": (True, _uint64)},
    ("HANDLER_ERROR", "GARBAGE_COLLECTED_BLOCK"): _ERROR_BLOCK_SCHEMA,
    ("HANDLER_ERROR", "UNKNOWN_BLOCK"): {"block_reference": (False, (
        {"block_id": (True, (_uint64, _hash))},
        {"finality": (True, lambda value: type(value) is str and value in ("final", "near-final", "optimistic"))},
        {"sync_checkpoint": (True, lambda value: type(value) is str and value in ("genesis", "earliest_available"))},
    ))},
    ("HANDLER_ERROR", "INVALID_ACCOUNT"): _ERROR_ACCOUNT_SCHEMA,
    ("HANDLER_ERROR", "UNKNOWN_ACCOUNT"): _ERROR_ACCOUNT_SCHEMA,
    ("HANDLER_ERROR", "NO_CONTRACT_CODE"): {
        **_ERROR_BLOCK_SCHEMA, "contract_account_id": (True, _account_id)},
    ("HANDLER_ERROR", "TOO_LARGE_CONTRACT_STATE"): {
        **_ERROR_BLOCK_SCHEMA, "contract_account_id": (True, _account_id)},
    ("HANDLER_ERROR", "UNKNOWN_ACCESS_KEY"): {**_ERROR_BLOCK_SCHEMA, "public_key": (True, _public_key)},
    ("HANDLER_ERROR", "UNKNOWN_GAS_KEY"): {**_ERROR_BLOCK_SCHEMA, "public_key": (True, _public_key)},
    ("HANDLER_ERROR", "CONTRACT_EXECUTION_ERROR"): {
        **_ERROR_BLOCK_SCHEMA, "vm_error": (True, _text), "error": (False, _FUNCTION_ERROR_SCHEMA)},
    ("HANDLER_ERROR", "TOO_MANY_ACCESS_KEYS"): {**_ERROR_ACCOUNT_SCHEMA, "limit": (True, _uint64)},
    ("HANDLER_ERROR", "NO_GLOBAL_CONTRACT_CODE"): {**_ERROR_BLOCK_SCHEMA, "identifier": (True, _GLOBAL_IDENTIFIER_SCHEMA)},
}
_ERROR_INFO_SCHEMA = {
    **_CONTEXT_SCHEMA, "vm_error": (False, _text), "error": (False, _FUNCTION_ERROR_SCHEMA), "limit": (False, _uint64),
    "public_key": (False, _public_key),
    "requested_shard_id": (False, _uint64), "method_name": (False, _text), "error_message": (False, _text),
    "block_reference": _ERROR_PAIRS[("HANDLER_ERROR", "UNKNOWN_BLOCK")]["block_reference"],
    "identifier": (False, _GLOBAL_IDENTIFIER_SCHEMA),
}
_ERROR_CAUSE_SCHEMA = {"name": (True, _text), "info": (False, _ERROR_INFO_SCHEMA)}
_ERROR_DATA_SCHEMA = {
    **_ERROR_INFO_SCHEMA, "name": (False, _text), "message": (False, _text),
    "code": (False, _int64), "cause": (False, _ERROR_CAUSE_SCHEMA),
}
_ERROR_SCHEMA = {
    **_CONTEXT_SCHEMA, "name": (True, _text), "code": (True, _int64), "message": (True, _text),
    "data": (False, (_text, lambda value: type(value) is dict and _error_data(value))),
    "cause": (True, _ERROR_CAUSE_SCHEMA),
}
_ERROR_VALUE_SCHEMA = (_text, _ERROR_SCHEMA)
_ENVELOPE_SCHEMA = {
    **_CONTEXT_SCHEMA, "jsonrpc": (True, lambda value: type(value) is str and value == "2.0"),
    "id": (True, _text), "error": (False, _ERROR_SCHEMA),
    "result": (False, (dict, list, _text, int, float, bool, type(None))),
}
_ERROR_RESULT_SCHEMA = {**_CONTEXT_SCHEMA, "error": (True, _ERROR_VALUE_SCHEMA), "logs": (False, [_text])}
_HEADER_SCHEMA = {
    "height": (True, _uint64), "timestamp": (True, _uint64), "hash": (True, _hash),
    "timestamp_nanosec": (False, _decimal_uint64),
}
_FUNCTION_PERMISSION_SCHEMA = {
    "allowance": (True, (type(None), _balance)), "receiver_id": (True, _text), "method_names": (True, [_text]),
}
_PERMISSION_SCHEMA = {"FunctionCall": (True, _FUNCTION_PERMISSION_SCHEMA)}
_GAS_KEY_SCHEMA = {"balance": (True, _balance), "num_nonces": (True, lambda value: _uint(value) and value < 2**16)}
_GAS_FUNCTION_SCHEMA = {**_GAS_KEY_SCHEMA, **_FUNCTION_PERMISSION_SCHEMA}
_GAS_FULL_PERMISSION_SCHEMA = {"GasKeyFullAccess": (True, _GAS_KEY_SCHEMA)}
_GAS_FUNCTION_PERMISSION_SCHEMA = {"GasKeyFunctionCall": (True, _GAS_FUNCTION_SCHEMA)}
_ACCESS_KEY_SCHEMA = {
    "nonce": (True, _uint64, "an access key has no non negative int nonce"),
    "permission": (True, (lambda value: type(value) is str and value == "FullAccess", _PERMISSION_SCHEMA,
                          _GAS_FULL_PERMISSION_SCHEMA, _GAS_FUNCTION_PERMISSION_SCHEMA)),
}
_QUERY_CONTEXT_SCHEMA = {"block_height": (True, _uint64), "block_hash": (True, _hash)}
_RESULT_SCHEMAS = {
    "block": {"header": (True, _HEADER_SCHEMA)},
    "view_account": {
        **_QUERY_CONTEXT_SCHEMA, "amount": (True, _balance), "locked": (True, _balance),
        "storage_usage": (True, _uint64), "code_hash": (True, _hash), "storage_paid_at": (False, _uint64),
        "global_contract_hash": (False, (type(None), _hash)),
        "global_contract_account_id": (False, (type(None), _account_id)),
    },
    "view_access_key": {**_QUERY_CONTEXT_SCHEMA, **_ACCESS_KEY_SCHEMA},
    "view_access_key_list": {
        **_QUERY_CONTEXT_SCHEMA, "keys": (True, [{"public_key": (True, lambda value: _public_key(value, handle=True)), "access_key": (True, _ACCESS_KEY_SCHEMA)}]),
    },
    "view_code": {**_QUERY_CONTEXT_SCHEMA, "code_base64": (True, _base64), "hash": (True, _hash)},
    "call_function": {
        **_QUERY_CONTEXT_SCHEMA, "logs": (True, [_text]),
        "result": (True, [lambda value: type(value) is int and 0 <= value <= 255]),
    },
}
_ENCODED_ERROR_RESULT_SCHEMA = {
    **_CONTEXT_SCHEMA, "logs": (False, [_text]), "result": _RESULT_SCHEMAS["call_function"]["result"],
}
_METHOD_SCHEMAS = {
    "contract_source_metadata": {
        "version": (True, _text), "link": (True, (type(None), _text)),
        "standards": (True, [{"standard": (True, _text), "version": (True, _text)}]),
    },
    "storage_balance_of": (type(None), {"total": (True, _balance), "available": (True, _balance)}),
    "ft_metadata": {
        "name": (True, _text), "symbol": (True, _text),
        "decimals": (True, lambda value: _uint(value) and value <= 255), "spec": (False, _text),
        "icon": (False, (type(None), _text)), "reference": (False, (type(None), _text)),
        "reference_hash": (False, (type(None), _base64)),
    },
    "subject_summary": {
        "accepted": (True, _uint64), "pending": (True, _uint64), "contested": (True, _uint64),
        "latest_accepted_id": (True, (type(None), lambda value: _uint64(value) and value < 2**64 - 1)),
        "latest_deny_id": (True, (type(None), lambda value: _uint64(value) and value < 2**64 - 1)),
        "deny": (True, bool),
    },
}
_SCHEMA_CONSTRAINTS = (
    (_ERROR_SCHEMA, _error_variant),
    (_RESULT_SCHEMAS["view_account"], lambda value:
     sum(value.get(field) is not None for field in ("global_contract_hash", "global_contract_account_id")) <= 1
     and (not any(value.get(field) is not None for field in ("global_contract_hash", "global_contract_account_id"))
          or value["code_hash"] == EMPTY_CODE_HASH)),
    (_METHOD_SCHEMAS["subject_summary"], lambda value:
     value["contested"] <= value["accepted"]
     and value["accepted"] + value["pending"] < 2**64
     and (value["accepted"] != 1 or not value["deny"]
          or value["latest_accepted_id"] == value["latest_deny_id"])
     and (value["latest_accepted_id"] is not None if value["accepted"] else
          value["latest_accepted_id"] is None and not value["deny"])
     and (value["accepted"] > 0 and value["latest_deny_id"] is not None if value["deny"] else
          value["latest_deny_id"] is None)),
    (_METHOD_SCHEMAS["storage_balance_of"][1], lambda value:
     int(value["available"].lstrip("0") or "0") <= int(value["total"].lstrip("0") or "0")),
    (_METHOD_SCHEMAS["ft_metadata"], lambda value:
     (value.get("reference") is None) == (value.get("reference_hash") is None)),
    (_RESULT_SCHEMAS["view_access_key_list"], lambda value:
     len({key["public_key"] for key in value["keys"]}) == len(value["keys"])),
    (_HEADER_SCHEMA, lambda value: "timestamp_nanosec" not in value
     or value["timestamp_nanosec"] == str(value["timestamp"])),
    (_ENVELOPE_SCHEMA, lambda value: ("result" in value) != ("error" in value)),
)
_CLOSED_SCHEMAS = (_PERMISSION_SCHEMA, _FUNCTION_PERMISSION_SCHEMA, _ERROR_SCHEMA,
                   _ERROR_CAUSE_SCHEMA, _ERROR_INFO_SCHEMA, _METHOD_SCHEMAS["subject_summary"],
                   *_ERROR_PAIRS[("HANDLER_ERROR", "UNKNOWN_BLOCK")]["block_reference"][1],
                   *_GLOBAL_IDENTIFIER_SCHEMA,
                   _GAS_KEY_SCHEMA, _GAS_FUNCTION_SCHEMA, _GAS_FULL_PERMISSION_SCHEMA, _GAS_FUNCTION_PERMISSION_SCHEMA,
                   *(schema for variants in (_HOST_ERROR_VARIANTS, _COMPILATION_ERROR_VARIANTS, _FUNCTION_ERROR_VARIANTS)
                     for schema in variants.values() if isinstance(schema, dict)))


def _validate_schema(value, schema):
    if isinstance(schema, tuple):
        for alternative in schema:
            try:
                _validate_schema(value, alternative)
            except MalformedResponse:
                continue
            return
        raise MalformedResponse("the RPC field matches no declared type")
    if isinstance(schema, dict):
        if type(value) is not dict:
            raise MalformedResponse("the RPC field is not an object")
        for field, entry in schema.items():
            required, child, *diagnostic = entry
            if field not in value:
                if required and not (schema is _REDUCED_SCHEMA.get() and field in _REDUCED_FIELDS):
                    raise MalformedResponse(diagnostic[0] if diagnostic else f"the RPC object lacks {field}")
            else:
                try:
                    _validate_schema(value[field], child)
                except MalformedResponse as error:
                    if diagnostic:
                        raise MalformedResponse(diagnostic[0]) from error
                    raise
        for constrained, rule in _SCHEMA_CONSTRAINTS:
            if schema is constrained and not rule(value):
                raise MalformedResponse("the RPC object violates its schema constraints")
        if any(schema is closed for closed in _CLOSED_SCHEMAS) and value.keys() - schema.keys():
            raise MalformedResponse("the RPC object has undeclared fields")
        return
    if isinstance(schema, list):
        if type(value) is not list:
            raise MalformedResponse("the RPC field is not a list")
        for child in value:
            _validate_schema(child, schema[0])
        return
    valid = type(value) is schema if isinstance(schema, type) else schema(value)
    if not valid:
        raise MalformedResponse("the RPC field has an invalid type or value")


def _validate_tree(value, *, execution_cause=False, execution_info=False):
    if type(value) is dict:
        if "error" in value and not execution_info:
            _validate_schema(value["error"], _ERROR_VALUE_SCHEMA)
        for key, child in value.items():
            if key == "error" and execution_info:
                continue
            _validate_tree(child, execution_info=execution_cause and key == "info",
                           execution_cause=key == "cause" and value.get("name") == "HANDLER_ERROR"
                           and isinstance(child, dict) and child.get("name") == "CONTRACT_EXECUTION_ERROR")
    elif type(value) is list:
        for child in value:
            _validate_tree(child)


def _result_bytes(value):
    _validate_schema(value, _RESULT_SCHEMAS["call_function"]["result"][1])
    return bytes(value)


def _validate_result(result, method, params, reduced=False):
    if type(result) is dict and "error" in result:
        _validate_schema(result, _ERROR_RESULT_SCHEMA)
        return
    kind = ("block" if method == "block" else params.get("request_type")
            if method == "query" and isinstance(params, dict) else None)
    schema = _RESULT_SCHEMAS.get(kind)
    if schema is None:
        return
    if kind == "call_function" and type(result) is dict and "result" in result:
        try:
            value = json.loads(_result_bytes(result["result"]))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise MalformedResponse("the view method did not return JSON") from error
        _validate_tree(value)
        if type(value) is dict and "error" in value:
            _validate_schema(result, _ENCODED_ERROR_RESULT_SCHEMA)
            _validate_schema(value, _ERROR_RESULT_SCHEMA)
            return
    token = _REDUCED_SCHEMA.set(schema if reduced else None)
    try:
        _validate_schema(result, schema)
    finally:
        _REDUCED_SCHEMA.reset(token)
    if kind == "view_code":
        try:
            code = base64.b64decode(result["code_base64"], validate=True)
        except ValueError as error:
            raise MalformedResponse("the view_code result has invalid base64 code") from error
        if result["hash"] != base58.encode(hashlib.sha256(code).digest()):
            raise MalformedResponse("the view_code hash does not match its code")
    elif kind == "call_function" and params.get("method_name") in _METHOD_SCHEMAS:
        _validate_schema(value, _METHOD_SCHEMAS[params["method_name"]])


def response_gate(response, method, params, *, block_hash=None, request_id=None, reduced=False):
    """Reject mixed results and errors, and bind response context to its request. reduced marks a replayed
    REDUCED_RECORDINGS file: it may lack _REDUCED_FIELDS and may not answer a pinned request."""
    if reduced and isinstance(params, dict) and "block_id" in params:
        raise MalformedResponse("a reduced recording cannot answer a pinned request")
    try:
        envelope = ({"jsonrpc": "2.0", "id": "gate", **response}
                    if request_id is None and type(response) is dict else response)
        _validate_schema(envelope, _ENVELOPE_SCHEMA)
        if method == "query" and isinstance(params, dict) and "account_id" in params:
            _validate_schema(params, {"account_id": (True, _account_id)})
        _validate_tree(response)
        if "result" in response:
            _validate_result(response["result"], method, params, reduced)
    except RecursionError as error:
        raise MalformedResponse("the RPC body is nested too deeply to validate") from error
    if request_id is not None and (type(response["id"]) is not type(request_id) or response["id"] != request_id):
        raise MalformedResponse("the JSON-RPC envelope does not match the request")
    errors, contexts, required_contexts = [], [], []

    def collect_contexts(value, *, diagnostic=False):
        if isinstance(value, dict):
            if any(key in value for key in _CONTEXT_SCHEMA):
                contexts.append(value)
            for key, child in value.items():
                if key == "error" and "vm_error" in value:
                    continue
                if diagnostic or key in ("result", "error", "cause", "info", "data"):
                    collect_contexts(child, diagnostic=diagnostic or key == "data")
        elif diagnostic and isinstance(value, list):
            for child in value:
                collect_contexts(child, diagnostic=True)

    def inspect(value, *, decoded=False, in_error=False, execution_cause=False, execution_info=False):
        if in_error:
            if isinstance(value, dict):
                if "error" in value and not execution_info or "result" in value:
                    raise MalformedResponse("the RPC error carries a nested result or error")
                for key, child in value.items():
                    if key == "error" and execution_info:
                        continue
                    inspect(child, in_error=True, execution_info=execution_cause and key == "info",
                            execution_cause=key == "cause" and value.get("name") == "HANDLER_ERROR"
                            and isinstance(child, dict) and child.get("name") == "CONTRACT_EXECUTION_ERROR")
            elif isinstance(value, list):
                for child in value:
                    inspect(child, in_error=True)
            return False
        if isinstance(value, dict):
            data = "error" in value and "result" in value
            if "error" in value:
                errors.append(value["error"])
            if "error" in value and not decoded and value is not response:
                required_contexts.append(value)
            for key, child in value.items():
                if key == "error":
                    inspect(child, in_error=True)
                    continue
                if key in ("jsonrpc", "id", "block_height", "block_hash", "requested_account_id", "account_id", "contract_account_id"):
                    if isinstance(child, (dict, list)):
                        data = inspect(child) or data
                    continue
                if key == "result":
                    decoded_child = False
                    if isinstance(child, list) and not decoded:
                        raw = _result_bytes(child)
                        try:
                            child = json.loads(raw)
                        except (ValueError, UnicodeError):
                            pass
                        else:
                            decoded_child = True
                            if isinstance(child, dict) and "error" in child:
                                collect_contexts(child)
                    count = len(errors)
                    child_data = inspect(child, decoded=decoded_child)
                    if decoded_child and len(errors) > count:
                        required_contexts.append(value)
                    data = child_data or len(errors) == count or data
                elif key == "logs" and child == []:
                    continue
                else:
                    inspect(child)
                    data = True
            return data
        if isinstance(value, list):
            for child in value:
                inspect(child)
        return True

    try:
        collect_contexts(response)
        data = inspect(response)
    except RecursionError as error:
        raise MalformedResponse("the RPC body is nested too deeply to inspect") from error
    request = params if isinstance(params, dict) else {}
    height = request.get("block_id")
    pinned = _uint(height) or isinstance(height, str)
    expected = None
    observed = {}
    if method == "block" and "result" in response and "header" in response["result"]:
        header = response["result"]["header"]
        if (_uint(height) and header["height"] != height
                or isinstance(height, str) and header["hash"] != height):
            raise MalformedResponse("the block header does not match the pinned request")
        observed.update(block_height=header["height"], block_hash=header["hash"])
        expected = header["hash"]
    for context in contexts:
        _validate_schema(context, _CONTEXT_SCHEMA)
        for field in _CONTEXT_SCHEMA:
            if field in context:
                name = "account_id" if field in ("requested_account_id", "contract_account_id") else field
                if name in observed and context[field] != observed[name]:
                    raise MalformedResponse("the RPC response carries conflicting context")
                observed[name] = context[field]
        if (_uint(height) and "block_height" in context
                and (type(context["block_height"]) is not int or context["block_height"] != height)):
            raise MalformedResponse("RPC response context does not match the pinned request")
        account = request.get("account_id")
        for field in ("requested_account_id", "account_id", "contract_account_id"):
            if account is not None and field in context and context[field] != account:
                raise MalformedResponse("RPC response context does not match the pinned request")
        if "block_hash" in context and pinned:
            if isinstance(height, str):
                expected = height
            elif expected is None:
                try:
                    expected = block_hash(height) if block_hash is not None and method != "block" else None
                except RpcError as failure:
                    raise MalformedResponse("the pinned block could not be read") from failure
            if expected is None or context["block_hash"] != expected:
                raise MalformedResponse("RPC response context does not match the pinned block hash")
    if not errors:
        return response
    if data or len(errors) != 1:
        raise MalformedResponse("the RPC body carries both a result and an error")
    error = errors[0]
    cause = error.get("cause") if isinstance(error, dict) else None
    info = cause.get("info") if isinstance(cause, dict) else None
    if method == "query" and isinstance(cause, dict) and cause["name"] == "UNKNOWN_BLOCK":
        reference = _ERROR_PAIRS[("HANDLER_ERROR", "UNKNOWN_BLOCK")]["block_reference"][1]
        _validate_schema(info, {"block_reference": (True, reference)})
        if any(request.get(field) != value for field, value in info["block_reference"].items()):
            raise MalformedResponse("RPC error block reference does not match the request")
    if pinned and not isinstance(cause, dict):
        raise MalformedResponse("RPC error context does not match the pinned request")
    for context in required_contexts:
        if _uint(height) and (type(context.get("block_height")) is not int
                              or context["block_height"] != height):
            raise MalformedResponse("RPC error context does not match the pinned request")
        if isinstance(height, str) and context.get("block_hash") != height:
            raise MalformedResponse("RPC error context does not match the pinned block hash")
    raise RpcError(error)


def trim_block_response(response):
    """Keep only the block header fields callers read in a successful RPC response."""
    if not isinstance(response, dict) or "error" in response:
        return response
    result = response.get("result")
    if not isinstance(result, dict):
        return response
    header = result.get("header")
    try:
        _validate_schema(header, _HEADER_SCHEMA)
    except MalformedResponse:
        return response
    if not all(key in response for key in ("jsonrpc", "id")):
        return response
    return {"jsonrpc": response["jsonrpc"], "id": response["id"],
            "result": {"header": {key: header[key] for key in ("hash", "height", "timestamp")}}}


class Client:
    """One endpoint: kind "rpc" for NEAR JSON-RPC at url, kind "http" for JSON over HTTP.

    After each request, last_source is {provider, request_sha256, fetched_at} for the evidence
    document, and last_block_height is the block a JSON-RPC result was read at (or None).
    """

    def __init__(self, url, mode=None, *, kind="rpc", fixtures_dir=FIXTURES_DIR, clock=time.time,
                 sleep=time.sleep, headers=None):
        mode = mode or os.environ.get("GATE_FIXTURES_MODE") or "replay"
        if mode not in MODES:
            raise ValueError(f"GATE_FIXTURES_MODE must be one of {MODES}, not {mode!r}")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
        self.url = _check_url(url).rstrip("/")
        self.mode = mode
        self.kind = kind
        self.fixtures_dir = Path(fixtures_dir)
        self.clock = clock
        self.sleep = sleep
        self.headers = dict(headers or {})
        self.last_source = None
        self.last_block_height = None
        self.last_reduced = False
        self._block_hashes = {}

    def call(self, method, params):
        """The result of a JSON-RPC call; raises RpcError when the response carries an error and
        MalformedResponse when it carries neither an error nor a result."""
        if self.kind != "rpc":
            raise ValueError("call needs a client of kind rpc")
        self.last_block_height = None
        body = {"jsonrpc": "2.0", "id": "gate", "method": method, "params": params}
        self.last_reduced = False
        response = self._exchange(self.url, method, params, body)
        try:
            response_gate(response, method, params, block_hash=self._block_hash, request_id=body["id"],
                          reduced=self.last_reduced)
        except RpcError:
            result = response.get("result")
            height = result.get("block_height") if isinstance(result, dict) else None
            self.last_block_height = height if _uint(height) else None
            raise
        result = response.get("result")
        if result is None:
            raise MalformedResponse("the JSON-RPC body has no result")
        self.last_block_height = result.get("block_height") if isinstance(result, dict) else None
        return result

    def get(self, url, *, params=None, record_http_errors=()):
        """The JSON body of a GET; url is absolute or a path under the client's url."""
        absolute = self._absolute(url)
        request_url = absolute
        if params is not None:
            if not isinstance(params, dict):
                raise ValueError("GET params must be an object")
            request_url = f"{absolute}?{urllib.parse.urlencode(params)}"
        return self._exchange(absolute, "GET", params, None, request_url=request_url,
                              record_http_errors=record_http_errors)

    def post(self, url, body):
        """The JSON body of a POST of body; the fixture keeps only the body's sha256."""
        return self._exchange(self._absolute(url), "POST", body_params(body), body)

    def view_account(self, account_id):
        result = self.call("query", {"request_type": "view_account", "finality": "final",
                                     "account_id": account_id})
        return result

    def view_account_at(self, account_id, height):
        result = self.call("query", {"request_type": "view_account", "block_id": height, "account_id": account_id})
        return result

    def code_hash(self, account_id):
        """The account's code hash in base58; EMPTY_CODE_HASH when it has no contract."""
        return self.view_account(account_id)["code_hash"]

    def call_function(self, contract, method, args, *, block_id=None):
        """The parsed JSON a view method returns, optionally pinned to a block height."""
        encoded = json.dumps(args, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if block_id is not None and (type(block_id) is not int or block_id < 0):
            raise ValueError("block_id must be a non negative integer")
        params = {"request_type": "call_function", "account_id": contract, "method_name": method,
                  "args_base64": base64.b64encode(encoded).decode("ascii")}
        params["finality" if block_id is None else "block_id"] = "final" if block_id is None else block_id
        result = self.call("query", params)
        value = result.get("result")
        raw = _result_bytes(value)
        try:
            return json.loads(raw)
        except ValueError as error:
            raise MalformedResponse(f"the view method did not return JSON: {error}") from error

    def view_access_key_list(self, account_id):
        result = self.call("query", {"request_type": "view_access_key_list", "finality": "final",
                                     "account_id": account_id})
        return result

    def final_block(self):
        result = self.call("block", {"finality": "final"})
        return result

    def block_at(self, height):
        result = self.call("block", {"block_id": height})
        header = result["header"]
        if _uint(height) and header["height"] != height:
            raise MalformedResponse(f"block {height} came back as block {header['height']}")
        return result

    def _block_hash(self, height):
        if height in self._block_hashes:
            return self._block_hashes[height]
        source, observed, reduced = self.last_source, self.last_block_height, self.last_reduced
        try:
            value = self.block_at(height)["header"]["hash"]
        finally:
            self.last_source, self.last_block_height, self.last_reduced = source, observed, reduced
        if len(self._block_hashes) >= BLOCK_HASH_CACHE_SIZE:
            del self._block_hashes[next(iter(self._block_hashes))]
        self._block_hashes[height] = value
        return value

    def _absolute(self, url):
        if self.kind != "http":
            raise ValueError("get and post need a client of kind http")
        return _check_url(url if url.startswith(("https://", "http://")) else self.url + url)

    def _respect_nearblocks(self, host):
        """Space NearBlocks fetches 10 s apart and refuse past the daily budget."""
        now = self.clock()
        use = _NEARBLOCKS_USE.setdefault(host, {"last": None, "day": None, "count": 0})
        day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
        if use["day"] != day:
            use.update(day=day, count=0)
        if use["count"] >= NEARBLOCKS_DAILY_BUDGET:
            raise RateLimited(f"{use['count']} NearBlocks requests from this process today; the budget "
                              f"is {NEARBLOCKS_DAILY_BUDGET}")
        wait = 0 if use["last"] is None else use["last"] + NEARBLOCKS_MIN_INTERVAL_S - now
        if wait > 0:
            if _REQUEST_DEADLINE.get() is not None:
                raise RateLimited(f"the next NearBlocks request slot is {wait:.1f} s away; a request being "
                                  "served never waits for it")
            self.sleep(wait)
        use["last"] = now + max(wait, 0)
        use["count"] += 1

    def _metered(self, host):
        """The headers to send once this fetch is reserved, and whether they carry the FastNEAR key."""
        provider = budget.provider_class(host)
        keyed = (provider in budget.FASTNEAR_CLASSES
                 and any(name.lower() == "authorization" for name in self.headers))
        try:
            send_key = budget.reserve(provider, _CALLER_CLASS.get() or "operator", keyed=keyed, now=self.clock())
        except budget.Refused as refused:
            error = BudgetExhausted(str(refused), refused.retry_after)
            refusals = _BUDGET_REFUSALS.get()
            if refusals is not None:
                refusals.append(error)
            raise error from None
        if not keyed or not send_key:
            return {name: value for name, value in self.headers.items() if name.lower() != "authorization"}, False
        return self.headers, keyed

    def _fixture_bytes(self, path, data=None):
        return path.read_bytes() if data is None else data

    def _exchange(self, url, method, params, body, *, request_url=None, record_http_errors=()):
        key = fixture_key(url, method, params)
        path = self.fixtures_dir / f"{key}.json"
        host = urllib.parse.urlsplit(url).netloc
        self.last_source = {"provider": host, "request_sha256": key, "fetched_at": None}
        used_fixture = False
        if self.mode != "off" and path.exists():
            data = self._fixture_bytes(path)
            try:
                fixture = json.loads(data)
            except RecursionError as error:
                raise MalformedResponse("the body is nested too deeply to parse") from error
            if self.mode == "replay" or self.clock() - _seconds(fixture["fetched_at"]) < MAX_AGE_S:
                self.last_source["fetched_at"] = fixture["fetched_at"]
                response = fixture["response"]
                used_fixture = True
                if self.mode == "replay" and path.resolve().is_relative_to(FIXTURES_DIR.parent):
                    name = path.resolve().relative_to(FIXTURES_DIR.parent).as_posix()
                    self.last_reduced = REDUCED_RECORDINGS.get(name) == hashlib.sha256(data).hexdigest()
        if not used_fixture:
            if self.mode == "replay":
                raise FixtureMiss(key, url, method, params)
            if host in NEARBLOCKS_HOSTS:
                self._respect_nearblocks(host)
            headers, keyed = self._metered(host) if self.mode == "off" else (self.headers, False)
            if self.mode == "record" and budget.provider_class(host) not in budget.FASTNEAR_CLASSES:
                headers = {name: value for name, value in headers.items() if name.lower() != "authorization"}
            try:
                response = _fetch(request_url or url, body, headers, record_http_errors)
            except HttpError as error:
                if keyed:
                    budget.key_failure(error.status, self.clock())
                raise
            fetched_at = _timestamp(self.clock())
            self.last_source["fetched_at"] = fetched_at
            if self.mode == "record":
                recorded_response = response
                if self.kind == "rpc":
                    try:
                        response_gate(response, method, params, block_hash=self._block_hash, request_id=body["id"])
                    except (RpcError, MalformedResponse):
                        recorded_response = response
                    else:
                        if method == "block":
                            recorded_response = trim_block_response(response)
                fixture = {"request": {"method": method, "params": params}, "response": recorded_response,
                           "url": url, "fetched_at": fetched_at}
                try:
                    text = json.dumps(fixture, indent=2, ensure_ascii=False)
                except RecursionError as error:
                    raise MalformedResponse("the body is nested too deeply to record") from error
                self.fixtures_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self._fixture_bytes(path, (text + "\n").encode("utf-8")))
        if (isinstance(response, dict) and set(response) == {HTTP_STATUS_MARKER}
                and type(response[HTTP_STATUS_MARKER]) is int):
            raise HttpError(response[HTTP_STATUS_MARKER], request_url or url)
        return response


def _fetch(url, body, extra_headers=None, record_http_errors=()):
    headers = {"Accept": "application/json", "User-Agent": "siyab"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    headers.update(extra_headers or {})
    request = urllib.request.Request(url, data=data,
                                     headers={name: value for name, value in headers.items()
                                              if name.lower() != "authorization"},
                                     method="GET" if data is None else "POST")
    for name, value in headers.items():
        if name.lower() == "authorization":
            request.add_unredirected_header(name, value)
    deadline = _REQUEST_DEADLINE.get()
    timeout = TIMEOUT_S
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("request deadline exceeded")
    try:
        opener = (urllib.request.urlopen(request, timeout=timeout) if deadline is None
                  else _open_with_deadline(request, timeout))
        with opener as reply:
            if deadline is None:
                return json.loads(reply.read())
            chunks = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("request deadline exceeded")
                chunk = reply.read1(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            response = json.loads(b"".join(chunks))
            if time.monotonic() >= deadline:
                raise TimeoutError("request deadline exceeded")
            return response
    except urllib.error.HTTPError as error:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("request deadline exceeded") from error
        if error.code in record_http_errors:
            return {HTTP_STATUS_MARKER: error.code}
        raise HttpError(error.code, url, error.headers.get("Retry-After")) from error
    except http.client.HTTPException as error:
        raise TransportError(type(error).__name__) from error
    except RecursionError as error:
        raise MalformedResponse("the body is nested too deeply to parse") from error
