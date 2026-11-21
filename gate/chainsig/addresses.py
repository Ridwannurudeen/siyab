"""Ethereum, Bitcoin and Solana address encoders for derived public keys."""

import hashlib

from gate import base58
from gate.chainsig import ed25519_bytes, secp256k1_bytes
from gate.chainsig.keccak import keccak256

_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
_BITCOIN_NETWORKS = {"mainnet": (0, 5, "bc"), "testnet": (111, 196, "tb"), "regtest": (111, 196, "bcrt")}


def ethereum_address(public_key):
    """The EIP-55 address of a NEAR secp256k1 public key."""
    address = keccak256(secp256k1_bytes(public_key)[1:])[-20:].hex()
    checksum = keccak256(address.encode("ascii")).hex()
    return "0x" + "".join(char.upper() if int(checksum[index], 16) >= 8 else char
                          for index, char in enumerate(address))


def _hash160(data):
    try:
        return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()
    except ValueError as error:
        raise ValueError("Bitcoin addresses require RIPEMD-160 from an enabled OpenSSL provider") from error


def _base58check(version, payload):
    body = bytes([version]) + payload
    checksum = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    return base58.encode(body + checksum)


def _p2wpkh(hrp, program):
    # BIP-173 witness version zero. No Bech32m or other witness version.
    data, accumulator, bits = [0], 0, 0
    for byte in program:
        accumulator = (accumulator << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((accumulator >> bits) & 31)
    if bits:
        data.append((accumulator << (5 - bits)) & 31)
    expanded = [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]
    checksum = 1
    for value in expanded + data + [0] * 6:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(_GENERATOR):
            if (top >> index) & 1:
                checksum ^= generator
    checksum ^= 1
    check_digits = [(checksum >> (5 * (5 - index))) & 31 for index in range(6)]
    return hrp + "1" + "".join(_BECH32[value] for value in data + check_digits)


def bitcoin_address(public_key, *, network, kind="p2wpkh"):
    """Encode a compressed key as P2WPKH, P2PKH or P2SH-P2WPKH.

    chainsig.js's Bitcoin adapter derives P2WPKH. The other two encodings
    match its bitcoinjs-lib dependency; they do not imply adapter support for
    preparing or signing transactions with those scripts.
    """
    if network not in _BITCOIN_NETWORKS:
        raise ValueError("Bitcoin network must be mainnet, testnet or regtest")
    if kind not in ("p2wpkh", "p2pkh", "p2sh_p2wpkh"):
        raise ValueError("Bitcoin address kind must be p2wpkh, p2pkh or p2sh_p2wpkh")
    p2pkh_version, p2sh_version, hrp = _BITCOIN_NETWORKS[network]
    program = _hash160(secp256k1_bytes(public_key, compressed=True))
    if kind == "p2wpkh":
        return _p2wpkh(hrp, program)
    if kind == "p2pkh":
        return _base58check(p2pkh_version, program)
    return _base58check(p2sh_version, _hash160(b"\x00\x14" + program))


def solana_address(public_key):
    """Base58 of the canonical 32 byte derived ed25519 key."""
    return base58.encode(ed25519_bytes(public_key))
