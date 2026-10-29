"""Verdict signers. Each exposes public_key_hex, key_id and sign(digest) -> 64 byte signature.

TestSigner signs with the TEST verifier key derived from a published seed (pure Python), for
tests, the vectors and offline runs. NaClSigner signs with a live key read from a near-cli style
key file (a public_key and a secret_key, each written as ed25519:<base58>, the secret holding
the 32 byte seed followed by the 32 byte public key) through PyNaCl. PurePythonLiveSigner signs a
live key with the pure Python code and refuses to start unless GATE_ALLOW_PURE_PYTHON_SIGNER=1.
"""

import json
import os
from pathlib import Path

from gate import base58, ed25519

KEY_IDS = ("cur", "prev")


class SignerError(Exception):
    """A signer that cannot start: a missing library, a refused mode or a bad key file."""


def _key_id(key_id):
    if key_id not in KEY_IDS:
        raise SignerError(f"key_id must be cur or prev, not {key_id!r}")
    return key_id


def _read_key_file(path):
    """(seed32, public32) from a near-cli style key file; the halves must agree."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SignerError(f"{path}: cannot read a key file") from error
    try:
        secret = base58.decode_key(document["secret_key"])
        public = base58.decode_key(document["public_key"])
    except (KeyError, TypeError, ValueError) as error:
        raise SignerError(f"{path}: not a key file with ed25519 public_key and secret_key") from error
    if len(secret) != 64 or len(public) != 32 or secret[32:] != public:
        raise SignerError(f"{path}: the secret key does not hold the 32 byte seed and this public key")
    if ed25519.public_from_seed(secret[:32]) != public:
        raise SignerError(f"{path}: the public key is not the one the seed derives")
    return secret[:32], public


class TestSigner:
    """The TEST verifier key; never registered on testnet."""

    def __init__(self, key_id="cur"):
        self.key_id = _key_id(key_id)
        self.public_key_hex = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED).hex()

    def sign(self, digest):
        return ed25519.sign(ed25519.TEST_VERIFIER_SEED, digest)


class NaClSigner:
    """A live verifier key signed with PyNaCl (nacl.signing.SigningKey on the 32 byte seed)."""

    def __init__(self, path, key_id="cur"):
        try:
            import nacl.signing
        except ImportError as error:
            raise SignerError("PyNaCl is not installed; the live signer runs from the venv "
                              "~/.venvs/near (pip install pynacl)") from error
        self.key_id = _key_id(key_id)
        seed, public = _read_key_file(path)
        self._key = nacl.signing.SigningKey(seed)
        if self._key.verify_key.encode() != public:
            raise SignerError(f"{path}: PyNaCl derives another public key from this seed")
        self.public_key_hex = public.hex()

    def sign(self, digest):
        return self._key.sign(digest).signature


class PurePythonLiveSigner:
    """A live key signed with the pure Python code, only when GATE_ALLOW_PURE_PYTHON_SIGNER=1."""

    def __init__(self, path, key_id="cur"):
        if os.environ.get("GATE_ALLOW_PURE_PYTHON_SIGNER") != "1":
            raise SignerError("the pure Python signer refuses a live key unless "
                              "GATE_ALLOW_PURE_PYTHON_SIGNER=1; use NaClSigner")
        self.key_id = _key_id(key_id)
        self._seed, public = _read_key_file(path)
        self.public_key_hex = public.hex()

    def sign(self, digest):
        return ed25519.sign(self._seed, digest)
