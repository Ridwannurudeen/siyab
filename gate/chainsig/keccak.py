"""Keccak-256 over bytes, for Ethereum addresses. SHA3-256 is a different hash.

Permutation, offsets and constants: https://keccak.team/keccak_specs_summary.html
This is a public-data encoder, with no signing operations.
"""

_MASK = 2**64 - 1
_RATE = 136
_ROTATION = (
    0, 1, 62, 28, 27,
    36, 44, 6, 55, 20,
    3, 10, 43, 25, 39,
    41, 45, 15, 21, 8,
    18, 2, 61, 56, 14,
)
_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)


def _rotate(value, count):
    return ((value << count) | (value >> (64 - count))) & _MASK


def keccak256(data):
    """The 32 byte Keccak-256 digest, using the original 0x01 suffix."""
    if not isinstance(data, bytes):
        raise ValueError("Keccak input must be bytes")
    padded = bytearray(data)
    padded.append(1)
    padded.extend(bytes((-len(padded)) % _RATE))
    padded[-1] |= 0x80
    state = [0] * 25
    for start in range(0, len(padded), _RATE):
        for index in range(_RATE // 8):
            offset = start + index * 8
            state[index] ^= int.from_bytes(padded[offset:offset + 8], "little")
        for constant in _ROUND_CONSTANTS:
            columns = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20]
                       for x in range(5)]
            deltas = [columns[(x - 1) % 5] ^ _rotate(columns[(x + 1) % 5], 1) for x in range(5)]
            moved = [0] * 25
            for y in range(5):
                for x in range(5):
                    index = x + 5 * y
                    moved[y + 5 * ((2 * x + 3 * y) % 5)] = _rotate(
                        state[index] ^ deltas[x], _ROTATION[index])
            for y in range(5):
                for x in range(5):
                    state[x + 5 * y] = (moved[x + 5 * y]
                                        ^ (~moved[(x + 1) % 5 + 5 * y] & moved[(x + 2) % 5 + 5 * y]))
            state[0] ^= constant
    return b"".join(lane.to_bytes(8, "little") for lane in state[:4])
