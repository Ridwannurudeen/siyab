"""Pure Python Ed25519: verification everywhere, signing only with the TEST key.

Origin: the Python implementation at https://ed25519.cr.yp.to/python/ed25519.py (fetched on
29 Oct 2026, sha256 6d4e...cc90). https://ed25519.cr.yp.to/software.html says "The Ed25519
software is in the public domain", and that page links this Python file. The code between the
BEGIN and END markers is that file with these changes:
- ported to Python 3: bytes instead of str, integer division (//) instead of /, bytes indexing
  instead of ord;
- the recursive expmod is replaced by the built in three argument pow;
- decodepoint raises ValueError, and rejects a y coordinate that is not below q and an x of 0
  with the sign bit set, so only canonical encodings decode;
- checkvalid is replaced by verify below.
Added here, outside the markers: public_from_seed, sign and verify. They check the seed, key and
signature lengths, and verify rejects S not below l and returns True or False instead of raising.

The algorithm is specified by RFC 8032 (Ed25519, sections 5.1 and 7.1), which is cited as the
normative reference and not copied. tests/test_ed25519.py checks this file against lines 1 to 3
of https://ed25519.cr.yp.to/python/sign.input, which equal RFC 8032 section 7.1 TEST 1 to 3, and
against PyNaCl. This code is slow and does not resist side channel attacks, so the live verifier
key is signed with PyNaCl, and this code signs only with the TEST key derived from a published
seed.
"""

import hashlib

# ---- BEGIN based on https://ed25519.cr.yp.to/python/ed25519.py (public domain) ----

b = 256
q = 2**255 - 19
l = 2**252 + 27742317777372353535851937790883648493


def H(m):
    return hashlib.sha512(m).digest()


def inv(x):
    return pow(x, q - 2, q)


d = -121665 * inv(121666)
I = pow(2, (q - 1) // 4, q)


def xrecover(y):
    xx = (y * y - 1) * inv(d * y * y + 1)
    x = pow(xx, (q + 3) // 8, q)
    if (x * x - xx) % q != 0:
        x = (x * I) % q
    if x % 2 != 0:
        x = q - x
    return x


By = 4 * inv(5)
Bx = xrecover(By)
B = [Bx % q, By % q]


def edwards(P, Q):
    x1 = P[0]
    y1 = P[1]
    x2 = Q[0]
    y2 = Q[1]
    x3 = (x1 * y2 + x2 * y1) * inv(1 + d * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * inv(1 - d * x1 * x2 * y1 * y2)
    return [x3 % q, y3 % q]


def scalarmult(P, e):
    if e == 0:
        return [0, 1]
    Q = scalarmult(P, e // 2)
    Q = edwards(Q, Q)
    if e & 1:
        Q = edwards(Q, P)
    return Q


def encodeint(y):
    bits = [(y >> i) & 1 for i in range(b)]
    return bytes([sum([bits[i * 8 + j] << j for j in range(8)]) for i in range(b // 8)])


def encodepoint(P):
    x = P[0]
    y = P[1]
    bits = [(y >> i) & 1 for i in range(b - 1)] + [x & 1]
    return bytes([sum([bits[i * 8 + j] << j for j in range(8)]) for i in range(b // 8)])


def bit(h, i):
    return (h[i // 8] >> (i % 8)) & 1


def publickey(sk):
    h = H(sk)
    a = 2 ** (b - 2) + sum(2**i * bit(h, i) for i in range(3, b - 2))
    A = scalarmult(B, a)
    return encodepoint(A)


def Hint(m):
    h = H(m)
    return sum(2**i * bit(h, i) for i in range(2 * b))


def signature(m, sk, pk):
    h = H(sk)
    a = 2 ** (b - 2) + sum(2**i * bit(h, i) for i in range(3, b - 2))
    r = Hint(h[b // 8 : b // 4] + m)
    R = scalarmult(B, r)
    S = (r + Hint(encodepoint(R) + pk + m) * a) % l
    return encodepoint(R) + encodeint(S)


def isoncurve(P):
    x = P[0]
    y = P[1]
    return (-x * x + y * y - 1 - d * x * x * y * y) % q == 0


def decodeint(s):
    return sum(2**i * bit(s, i) for i in range(0, b))  # noqa: PIE808 (kept as in the reference)


def decodepoint(s):
    y = sum(2**i * bit(s, i) for i in range(0, b - 1))  # noqa: PIE808 (kept as in the reference)
    if y >= q:
        raise ValueError("decoding a y coordinate that is not below q")
    x = xrecover(y)
    if x == 0 and bit(s, b - 1):
        raise ValueError("decoding x = 0 with the sign bit set")
    if x & 1 != bit(s, b - 1):
        x = q - x
    P = [x, y]
    if not isoncurve(P):
        raise ValueError("decoding point that is not on curve")
    return P


# ---- END based on https://ed25519.cr.yp.to/python/ed25519.py ----


def _check_seed(seed32):
    if len(seed32) != 32:
        raise ValueError(f"an Ed25519 seed is 32 bytes, not {len(seed32)}")


def public_from_seed(seed32):
    """The 32 byte public key for a 32 byte seed."""
    _check_seed(seed32)
    return publickey(seed32)


def sign(seed32, message):
    """The 64 byte pure Ed25519 signature of message under the seed's key."""
    _check_seed(seed32)
    return signature(message, seed32, publickey(seed32))


def verify(pub32, message, sig64):
    """True when sig64 is a valid signature of message under pub32, else False.

    Wrong lengths, non canonical point encodings and S not below the group order l are False.
    """
    if len(pub32) != 32 or len(sig64) != 64:
        return False
    try:
        R = decodepoint(sig64[:32])
        A = decodepoint(pub32)
    except ValueError:
        return False
    S = decodeint(sig64[32:])
    if S >= l:
        return False
    h = Hint(encodepoint(R) + pub32 + message)
    return scalarmult(B, S) == edwards(R, scalarmult(A, h))


# The TEST seeds are sha256 of published strings, so anyone can derive the TEST keys. The TEST
# verifier key is never registered on testnet; the live verifier key is a separate PyNaCl key.
TEST_VERIFIER_SEED = hashlib.sha256(b"gate test verifier v1").digest()
TEST_AGENT_SEED = hashlib.sha256(b"gate test agent v1").digest()
TEST_AGENT2_SEED = hashlib.sha256(b"gate test agent v2").digest()
