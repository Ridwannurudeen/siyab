"""Pure Python Ed25519 (RFC 8032): verification everywhere, signing only with the TEST key.

Origin: RFC 8032, "Edwards-Curve Digital Signature Algorithm (EdDSA)", S. Josefsson and
I. Liusvaara, January 2017, Appendix A, "Ed25519/Ed448 Python Library" (the Ed25519 part,
Python 3.2 or later). The code between the BEGIN and END markers follows that appendix. It
was written out for this file, not fetched; tests/test_ed25519.py checks it against the
RFC 8032 section 7.1 test vectors 1 to 3 and against PyNaCl. Changes from the appendix:
`verify` returns False for a public key or signature of the wrong length instead of raising,
and the size errors raise ValueError. The appendix says the library is not intended for
production use, which is why the live verifier key is signed with PyNaCl (gate/signer.py) and
this code signs only with the TEST key derived from a published seed.

Licence: RFC 8032 is subject to BCP 78 and the IETF Trust's Legal Provisions Relating to IETF
Documents (https://trustee.ietf.org/license-info) in effect on the date of its publication;
Code Components extracted from it must include the Simplified BSD License text as described in
Section 4.e of the Trust Legal Provisions and are provided without warranty as described in
the Simplified BSD License. The page https://trustee.ietf.org/license-info was fetched on
29 Oct 2026: it redirects to https://trustee.ietf.org/documents/trust-legal-provisions/, which
lists the Trust Legal Provisions 5.0 (published and effective 25 Mar 2015, still current, so in
effect in January 2017) and links the document, but does not hold the clause text itself.
UNVERIFIED: the clause wording above and the licence text below are the RFC boilerplate and
TLP section 4.e as known, not read from the TLP 5.0 document on that fetch.

Copyright (c) 2017 IETF Trust and the persons identified as the document authors.
All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are
permitted provided that the following conditions are met:

- Redistributions of source code must retain the above copyright notice, this list of
  conditions and the following disclaimer.
- Redistributions in binary form must reproduce the above copyright notice, this list of
  conditions and the following disclaimer in the documentation and/or other materials
  provided with the distribution.
- Neither the name of Internet Society, IETF or IETF Trust, nor the names of specific
  contributors, may be used to endorse or promote products derived from this software
  without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS
OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE
GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
OF THE POSSIBILITY OF SUCH DAMAGE.
"""

import hashlib

# ---- BEGIN RFC 8032 Appendix A (Ed25519 part) ----


def sha512(s):
    return hashlib.sha512(s).digest()


# Base field Z_p
p = 2**255 - 19


def modp_inv(x):
    return pow(x, p - 2, p)


# Curve constant
d = -121665 * modp_inv(121666) % p

# Group order
q = 2**252 + 27742317777372353535851937790883648493


def sha512_modq(s):
    return int.from_bytes(sha512(s), "little") % q


# Points are represented as tuples (X, Y, Z, T) of extended
# coordinates, with x = X/Z, y = Y/Z, x*y = T/Z


def point_add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % p, (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C, D = 2 * P[3] * Q[3] * d % p, 2 * P[2] * Q[2] % p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F, G * H, F * G, E * H)


# Computes Q = s * Q
def point_mul(s, P):
    Q = (0, 1, 1, 0)  # Neutral element
    while s > 0:
        if s & 1:
            Q = point_add(Q, P)
        P = point_add(P, P)
        s >>= 1
    return Q


def point_equal(P, Q):
    # x1 / z1 == x2 / z2  <==>  x1 * z2 == x2 * z1
    if (P[0] * Q[2] - Q[0] * P[2]) % p != 0:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % p != 0:  # noqa: SIM103 (kept as in the appendix)
        return False
    return True


# Square root of -1
modp_sqrt_m1 = pow(2, (p - 1) // 4, p)


# Compute corresponding x-coordinate, with low bit corresponding to
# sign, or return None on failure
def recover_x(y, sign):
    if y >= p:
        return None
    x2 = (y * y - 1) * modp_inv(d * y * y + 1)
    if x2 == 0:
        if sign:
            return None
        else:
            return 0

    # Compute square root of x2
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p != 0:
        x = x * modp_sqrt_m1 % p
    if (x * x - x2) % p != 0:
        return None

    if (x & 1) != sign:
        x = p - x
    return x


# Base point
g_y = 4 * modp_inv(5) % p
g_x = recover_x(g_y, 0)
G = (g_x, g_y, 1, g_x * g_y % p)


def point_compress(P):
    zinv = modp_inv(P[2])
    x = P[0] * zinv % p
    y = P[1] * zinv % p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def point_decompress(s):
    if len(s) != 32:
        raise ValueError("Invalid input length for decompression")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1

    x = recover_x(y, sign)
    if x is None:
        return None
    else:
        return (x, y, 1, x * y % p)


def secret_expand(secret):
    if len(secret) != 32:
        raise ValueError("Bad size of private key")
    h = sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return (a, h[32:])


def secret_to_public(secret):
    (a, dummy) = secret_expand(secret)  # noqa: RUF059 (kept as in the appendix)
    return point_compress(point_mul(a, G))


def sign(secret, msg):
    a, prefix = secret_expand(secret)
    A = point_compress(point_mul(a, G))
    r = sha512_modq(prefix + msg)
    R = point_mul(r, G)
    Rs = point_compress(R)
    h = sha512_modq(Rs + A + msg)
    s = (r + h * a) % q
    return Rs + int.to_bytes(s, 32, "little")


def verify(public, msg, signature):
    if len(public) != 32:
        return False
    if len(signature) != 64:
        return False
    A = point_decompress(public)
    if not A:
        return False
    Rs = signature[:32]
    R = point_decompress(Rs)
    if not R:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= q:
        return False
    h = sha512_modq(Rs + public + msg)
    sB = point_mul(s, G)
    hA = point_mul(h, A)
    return point_equal(sB, point_add(R, hA))


# ---- END RFC 8032 Appendix A ----


def public_from_seed(seed32):
    """The 32 byte public key for a 32 byte seed (RFC 8032 section 5.1.5)."""
    return secret_to_public(seed32)


# The TEST seeds are sha256 of published strings, so anyone can derive the TEST keys. The TEST
# verifier key is never registered on testnet; the live verifier key is a separate PyNaCl key.
TEST_VERIFIER_SEED = hashlib.sha256(b"gate test verifier v1").digest()
TEST_AGENT_SEED = hashlib.sha256(b"gate test agent v1").digest()
TEST_AGENT2_SEED = hashlib.sha256(b"gate test agent v2").digest()
