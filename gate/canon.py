"""The canonical verdict message: the exact bytes the verifier signs and the Gate rebuilds.

    GATEv1|<gate>|<agent_pk hex>|<kind>|<token_id or ->|<receiver_id>|<amount>|<memo sha256 or ->
    |<issued_ms>|<expires_ms>|<verdict_id>|<evidence sha256>|<key_id>|ALLOW

UTF-8, no trailing newline. digest = sha256(message); the signature is pure Ed25519 over the
32 byte digest, not Ed25519ph. No field can hold "|": account ids, lowercase hex, decimals and
the literals exclude it, and every field is validated before it is joined.

This module also builds the signed test vectors under fixtures/canon/, which the Python tests
and the Rust contract tests both read.
"""

import hashlib
import json
import re
from pathlib import Path

from gate import base58, ed25519, evidence

PREFIX = "GATEv1"
LITERAL = "ALLOW"
KINDS = ("near", "ft")
KEY_IDS = ("cur", "prev")
FIELD_COUNT = 14
U64_MAX = 2**64 - 1
U128_MAX = 2**128 - 1

_ACCOUNT_ID = re.compile(r"(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+", re.ASCII)
_DECIMAL = re.compile(r"0|[1-9][0-9]*")
_HEX = {32: re.compile(r"[0-9a-f]{32}"), 64: re.compile(r"[0-9a-f]{64}")}
_LITERAL = re.compile(r"[A-Z]+")


class CanonError(ValueError):
    """A value that has no canonical form."""


def account_id(value, field):
    """The account id itself when it is a valid NEAR account id of 2 to 64 characters."""
    if not isinstance(value, str) or not 2 <= len(value) <= 64 or not _ACCOUNT_ID.fullmatch(value):
        raise CanonError(f"{field}: not a valid NEAR account id: {value!r}")
    return value


def amount_field(value):
    """The decimal of an amount in 1 to 2**128 - 1; a string must already be that decimal."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CanonError(f"amount: not an integer or a decimal string: {value!r}")
    if isinstance(value, str):
        if len(value) > len(str(U128_MAX)) or not _DECIMAL.fullmatch(value):
            raise CanonError(f"amount: not a decimal without sign, spaces or leading zeros: {value!r}")
        value = int(value)
    if not 1 <= value <= U128_MAX:
        raise CanonError(f"amount: outside 1 to 2**128 - 1: {value!r}")
    return str(value)


def memo_field(memo):
    """'-' when the memo is absent, else the sha256 hex of its UTF-8 bytes (the empty string too)."""
    if memo is None:
        return "-"
    if not isinstance(memo, str):
        raise CanonError(f"memo: not a string or null: {memo!r}")
    try:
        data = memo.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CanonError(f"memo: not encodable as UTF-8: {error}") from error
    return hashlib.sha256(data).hexdigest()


def _ms_field(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U64_MAX:
        raise CanonError(f"{field}: not an integer in 0 to 2**64 - 1: {value!r}")
    return str(value)


def _hex_field(value, length, field):
    if not isinstance(value, str) or not _HEX[length].fullmatch(value):
        raise CanonError(f"{field}: not {length} lowercase hex characters: {value!r}")
    return value


def _fields(gate, agent_pk_hex, kind, token_id, receiver_id, amount, memo, issued_ms, expires_ms,
            verdict_id, evidence_sha256, key_id):
    if kind not in KINDS:
        raise CanonError(f"kind: not near or ft: {kind!r}")
    if kind == "near":
        if token_id is not None:
            raise CanonError("token_id: must be null for kind near")
        token = "-"
    else:
        token = account_id(token_id, "token_id")
    if key_id not in KEY_IDS:
        raise CanonError(f"key_id: not cur or prev: {key_id!r}")
    issued = _ms_field(issued_ms, "issued_ms")
    expires = _ms_field(expires_ms, "expires_ms")
    if expires_ms < issued_ms:
        raise CanonError(f"expires_ms: {expires_ms} is earlier than issued_ms {issued_ms}")
    return [
        PREFIX,
        account_id(gate, "gate"),
        _hex_field(agent_pk_hex, 64, "agent_pk_hex"),
        kind,
        token,
        account_id(receiver_id, "receiver_id"),
        amount_field(amount),
        memo_field(memo),
        issued,
        expires,
        _hex_field(verdict_id, 32, "verdict_id"),
        _hex_field(evidence_sha256, 64, "evidence_sha256"),
        key_id,
    ]


def canonical_message(*, gate, agent_pk_hex, kind, token_id, receiver_id, amount, memo, issued_ms,
                      expires_ms, verdict_id, evidence_sha256, key_id):
    """The canonical message bytes for an ALLOW; the literal is always ALLOW on this path."""
    fields = _fields(gate, agent_pk_hex, kind, token_id, receiver_id, amount, memo, issued_ms,
                     expires_ms, verdict_id, evidence_sha256, key_id)
    return "|".join(fields + [LITERAL]).encode("utf-8")


def validate_transfer(*, gate, agent_pk_hex, kind, token_id, receiver_id, amount, memo):
    """Raise CanonError unless the transfer fields have a canonical form; return the amount as its
    canonical decimal. The verdict service calls this before it judges or writes anything."""
    _fields(gate, agent_pk_hex, kind, token_id, receiver_id, amount, memo, 0, 0, "0" * 32, "0" * 64, "cur")
    return amount_field(amount)


def digest(message):
    """sha256 of the message: the 32 bytes that are signed."""
    return hashlib.sha256(message).digest()


def parse_canonical(message):
    """The fields of a canonical message; raises CanonError when the bytes are not canonical.

    The memo comes back as its hash (memo_sha256), because the message holds only the hash. Any
    upper case literal is accepted and returned as written, by design, so a test vector signed
    over DENY parses too; only canonical_message is restricted to ALLOW.
    """
    if not isinstance(message, bytes):
        raise CanonError("message: not bytes")
    try:
        text = message.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CanonError(f"message: not UTF-8: {error}") from error
    parts = text.split("|")
    if len(parts) != FIELD_COUNT or parts[0] != PREFIX:
        raise CanonError(f"message: not {FIELD_COUNT} fields starting with {PREFIX}")
    (_, gate, agent_pk_hex, kind, token, receiver_id, amount, memo_sha256, issued, expires,
     verdict_id, evidence_sha256, key_id, literal) = parts
    for field, value in (("issued_ms", issued), ("expires_ms", expires)):
        if len(value) > len(str(U64_MAX)) or not _DECIMAL.fullmatch(value):
            raise CanonError(f"{field}: not a decimal without leading zeros: {value!r}")
    if memo_sha256 != "-":
        _hex_field(memo_sha256, 64, "memo_sha256")
    if not _LITERAL.fullmatch(literal):
        raise CanonError(f"literal: not upper case letters: {literal!r}")
    parsed = {
        "gate": gate,
        "agent_pk_hex": agent_pk_hex,
        "kind": kind,
        "token_id": None if token == "-" else token,
        "receiver_id": receiver_id,
        "amount": amount,
        "memo_sha256": None if memo_sha256 == "-" else memo_sha256,
        "issued_ms": int(issued),
        "expires_ms": int(expires),
        "verdict_id": verdict_id,
        "evidence_sha256": evidence_sha256,
        "key_id": key_id,
        "literal": literal,
    }
    _fields(gate, agent_pk_hex, kind, parsed["token_id"], receiver_id, amount, None,
            parsed["issued_ms"], parsed["expires_ms"], verdict_id, evidence_sha256, key_id)
    return parsed


# The signed test vectors (fixtures/canon/). Vectors named *-unit are for the contract unit tests
# (gate gate.testnet, block timestamp UNIT_BLOCK_MS); vectors named *-sandbox are for the sandbox
# (gate gate.<root>, timestamps in the year 2100 with a ttl of 300000 ms).
DEFAULT_GATE_ROOT = "test.near"
SANDBOX_ISSUED_MS = 4102444500000
SANDBOX_EXPIRES_MS = 4102444800000
UNIT_ISSUED_MS = 1700000000000
UNIT_EXPIRES_MS = 1700000300000
UNIT_BLOCK_MS = 1700000100000
VECTOR_FIELDS = ("gate_account_id", "agent_pk_hex", "kind", "token_id", "receiver_id", "amount",
                 "memo", "issued_ms", "expires_ms", "verdict_id", "evidence_sha256", "key_id")


VECTOR_REASON = "canonical message test vector: no check ran, so this document records no judgement"


def _test_verdict_id(name):
    """A fixed verdict id per vector name, so the vectors regenerate byte for byte."""
    return hashlib.sha256(b"gate test verdict " + name.encode("utf-8")).hexdigest()[:32]


def _vector_specs(root):
    agent1 = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED).hex()
    agent2 = ed25519.public_from_seed(ed25519.TEST_AGENT2_SEED).hex()
    sandbox = {
        "gate_account_id": f"gate.{root}",
        "agent_pk_hex": agent1,
        "kind": "ft",
        "token_id": f"token.gate.{root}",
        "receiver_id": f"vendor.{root}",
        "amount": "1000000",
        "memo": "invoice 1",
        "issued_ms": SANDBOX_ISSUED_MS,
        "expires_ms": SANDBOX_EXPIRES_MS,
        "key_id": "cur",
    }
    unit = dict(sandbox, gate_account_id="gate.testnet", token_id="token.gate.testnet",
                receiver_id="vendor.testnet", issued_ms=UNIT_ISSUED_MS, expires_ms=UNIT_EXPIRES_MS)
    on_sandbox = (f" Agent TEST agent 1 unless stated. Sandbox root account id {root}: UNVERIFIED"
                  " until docs/SPIKES.md S1 records the measured id; regenerate with"
                  " python -m gate canon-vectors --write fixtures/canon --gate-root <id>.")
    on_unit = f" Agent TEST agent 1 unless stated; contract block timestamp {UNIT_BLOCK_MS} ms."
    # (name, base, changes, mode, expect_contract, note); mode "flip" flips one signature bit,
    # mode "deny" signs a message whose literal is DENY.
    return [
        ("01-ft-happy-sandbox", sandbox, {}, None, "OK",
         "FT payment to the allowlisted vendor." + on_sandbox),
        ("02-near-happy-sandbox", sandbox,
         {"kind": "near", "token_id": None, "amount": "1000000000000000000000000", "memo": None},
         None, "OK", "Native NEAR payment of 1 NEAR to the allowlisted vendor, no token and no"
         " memo; timestamps as 01." + on_sandbox),
        ("03-memo-empty-sandbox", sandbox, {"memo": ""}, None, "OK",
         "As 01 with the empty memo, which is hashed like any other memo." + on_sandbox),
        ("04-ft-happy-unit", unit, {}, None, "OK",
         "FT payment to the allowlisted vendor, inside its ttl." + on_unit),
        ("05-expired-unit", unit, {"issued_ms": 1600000000000, "expires_ms": 1600000300000}, None,
         "REFUSED:expired", "As 04 with an expiry before the block timestamp." + on_unit),
        ("06-ttl-too-long-unit", unit, {"expires_ms": 1700000600000}, None,
         "REFUSED:ttl_too_long",
         "As 04 with a ttl of 600000 ms, above the default max_ttl_ms of 300000." + on_unit),
        ("07-bad-signature-unit", unit, {"verdict_id": _test_verdict_id("04-ft-happy-unit")},
         "flip", "REFUSED:bad_signature",
         "Vector 04 with bit 0 of signature byte 0 flipped; the signature does not verify."
         + on_unit),
        ("08-wrong-literal-unit", unit, {}, "deny", "REFUSED:not_allow",
         "As 04 with DENY in place of ALLOW, signed by the test only path that skips the literal"
         " check; the signature verifies over this message and the contract refuses the literal."
         + on_unit),
        ("09-prev-key-unit", unit, {"key_id": "prev"}, None, "OK",
         "As 04 with key_id prev; OK when the TEST verifier key is registered as the previous key"
         " and its deadline has not passed." + on_unit),
        ("10-wrong-contract-unit", unit, {"gate_account_id": "other.testnet"}, None,
         "REFUSED:bad_signature",
         "As 04 signed for gate other.testnet; refused as bad_signature when submitted to"
         " gate.testnet, because the contract rebuilds the message with its own account id."
         + on_unit),
        ("11-wrong-agent-unit", unit, {"agent_pk_hex": agent2}, None, "REFUSED:bad_signature",
         "As 04 signed for TEST agent 2; refused as bad_signature when submitted by TEST agent 1,"
         " because the contract rebuilds the message with the signer key." + on_unit),
        ("12-unknown-payee-sandbox", sandbox, {"receiver_id": f"stranger.{root}", "amount": "1"},
         None, "OK", "Receiver not on the allowlist, amount 1 base unit; OK on the first"
         " submission within the unknown payee cap; other fields as 01." + on_sandbox),
        ("13-unknown-payee-over-cap-sandbox", sandbox,
         {"receiver_id": f"stranger.{root}", "amount": "1000000"}, None,
         "REFUSED:unknown_payee_cap", "Receiver not on the allowlist, amount 1000000 base units,"
         " above the unknown payee cap; other fields as 01." + on_sandbox),
    ]


def _evidence_document(request, literal):
    # No check runs for a test vector, so the document lists none and says so. As in every
    # evidence document, issue, expiry and verdict id are set on ALLOW only.
    allow = literal == LITERAL
    return evidence.document(
        subject=evidence.subject(
            gate=request["gate_account_id"],
            agent_pk=request["agent_pk_hex"],
            kind=request["kind"],
            token=request["token_id"],
            receiver=request["receiver_id"],
            amount=request["amount"],
            memo=request["memo"],
        ),
        checks=[],
        labels=[],
        verdict=literal,
        reasons=[VECTOR_REASON],
        observed={"block_height": None, "timestamp_ms": None},
        issued_ms=request["issued_ms"] if allow else None,
        expires_ms=request["expires_ms"] if allow else None,
        verdict_id=request["verdict_id"] if allow else None,
        latency_ms=None,
    )


def _message(request):
    return canonical_message(
        gate=request["gate_account_id"],
        agent_pk_hex=request["agent_pk_hex"],
        kind=request["kind"],
        token_id=request["token_id"],
        receiver_id=request["receiver_id"],
        amount=request["amount"],
        memo=request["memo"],
        issued_ms=request["issued_ms"],
        expires_ms=request["expires_ms"],
        verdict_id=request["verdict_id"],
        evidence_sha256=request["evidence_sha256"],
        key_id=request["key_id"],
    )


def build_vectors(gate_root=DEFAULT_GATE_ROOT):
    """The signed test vectors as (vector dict, evidence document) pairs, in name order."""
    root = account_id(gate_root, "gate_root")
    verifier_pub = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED)
    built = []
    for name, base, changes, mode, expect_contract, note in _vector_specs(root):
        request = dict(base, verdict_id=_test_verdict_id(name))
        request.update(changes)
        literal = "DENY" if mode == "deny" else LITERAL
        document = _evidence_document(request, literal)
        request["evidence_sha256"] = evidence.evidence_hash(document)
        message = _message(request)
        if mode == "deny":
            # The test only path: swap the literal after the strict builder, then sign.
            message = message[: -len(LITERAL)] + literal.encode("utf-8")
        signature = ed25519.sign(ed25519.TEST_VERIFIER_SEED, digest(message))
        if mode == "flip":
            signature = bytes([signature[0] ^ 0x01]) + signature[1:]
        vector = {"name": name}
        vector.update((field, request[field]) for field in VECTOR_FIELDS)
        vector.update({
            "literal": literal,
            "message": message.decode("utf-8"),
            "digest_sha256": digest(message).hex(),
            "verifier_pub_hex": verifier_pub.hex(),
            "signature_hex": signature.hex(),
            "expect_valid": mode != "flip",
            "expect_contract": expect_contract,
            "note": note,
        })
        built.append((vector, document))
    return built


def write_vectors(canon_dir, gate_root=DEFAULT_GATE_ROOT):
    """Write the vectors to canon_dir, their evidence to the sibling evidence/ and the TEST
    verifier public key to the sibling keys/; returns (written paths, removed paths).

    Ed25519 signing is deterministic and every field is fixed, so the output is byte for byte
    the same on every run with the same gate root. Vector evidence documents left from an
    earlier run (another gate root) are removed; other evidence documents are left alone.
    """
    canon_dir = Path(canon_dir)
    evidence_dir = canon_dir.parent / "evidence"
    keys_dir = canon_dir.parent / "keys"
    for directory in (canon_dir, keys_dir):
        directory.mkdir(parents=True, exist_ok=True)
    written = []
    for vector, document in build_vectors(gate_root):
        path = canon_dir / f"{vector['name']}.json"
        path.write_bytes((json.dumps(vector, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        written.append(path)
        evidence_path = evidence.write(document, evidence_dir)
        if evidence_path not in written:
            written.append(evidence_path)
    removed = []
    for path in sorted(evidence_dir.glob("*.json")):
        if path in written:
            continue
        try:
            document = json.loads(path.read_bytes())
        except ValueError:
            continue
        if isinstance(document, dict) and document.get("reasons") == [VECTOR_REASON]:
            path.unlink()
            removed.append(path)
    verifier_pub = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED)
    key_path = keys_dir / "test-verifier.pub"
    key_path.write_bytes(f"{base58.encode_key(verifier_pub)}\n{verifier_pub.hex()}\n".encode("ascii"))
    written.append(key_path)
    return written, removed
