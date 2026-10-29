import hashlib
import unittest
from pathlib import Path

from gate import base58, ed25519

try:
    import nacl.signing
except ImportError:
    nacl = None

KEY_FILE = Path(__file__).resolve().parent.parent / "fixtures" / "keys" / "test-verifier.pub"

# RFC 8032 section 7.1, TEST 1 to TEST 3: (secret key, public key, message, signature).
RFC8032_VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        (
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
        ),
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        (
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
        ),
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        (
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
            "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"
        ),
    ),
]


def flip(data, index, bit=0):
    return data[:index] + bytes([data[index] ^ (1 << bit)]) + data[index + 1:]


class Rfc8032Test(unittest.TestCase):
    def test_section_7_1_vectors(self):
        for number, (secret, public, message, signature) in enumerate(RFC8032_VECTORS, start=1):
            with self.subTest(test=number):
                secret, public = bytes.fromhex(secret), bytes.fromhex(public)
                message, signature = bytes.fromhex(message), bytes.fromhex(signature)
                self.assertEqual(ed25519.public_from_seed(secret), public)
                self.assertEqual(ed25519.sign(secret, message), signature)
                self.assertTrue(ed25519.verify(public, message, signature))

    def test_flipped_bits_fail(self):
        _, public, message, signature = (bytes.fromhex(v) for v in RFC8032_VECTORS[2])
        for index in (0, 31, 32, 63):
            with self.subTest(signature_byte=index):
                self.assertFalse(ed25519.verify(public, message, flip(signature, index)))
        self.assertFalse(ed25519.verify(public, flip(message, 0), signature))
        self.assertFalse(ed25519.verify(flip(public, 0), message, signature))

    def test_wrong_lengths_return_false(self):
        _, public, message, signature = (bytes.fromhex(v) for v in RFC8032_VECTORS[0])
        self.assertFalse(ed25519.verify(public[:31], message, signature))
        self.assertFalse(ed25519.verify(public, message, signature[:63]))
        self.assertFalse(ed25519.verify(public, message, signature + b"\0"))

    def test_non_canonical_s_fails(self):
        _, public, message, signature = (bytes.fromhex(v) for v in RFC8032_VECTORS[0])
        s = int.from_bytes(signature[32:], "little") + ed25519.q
        self.assertFalse(ed25519.verify(public, message, signature[:32] + s.to_bytes(32, "little")))

    def test_seed_must_be_32_bytes(self):
        with self.assertRaises(ValueError):
            ed25519.sign(b"\0" * 31, b"message")


class TestKeyTest(unittest.TestCase):
    def test_seeds_come_from_the_published_strings(self):
        self.assertEqual(ed25519.TEST_VERIFIER_SEED, hashlib.sha256(b"gate test verifier v1").digest())
        self.assertEqual(ed25519.TEST_AGENT_SEED, hashlib.sha256(b"gate test agent v1").digest())
        self.assertEqual(ed25519.TEST_AGENT2_SEED, hashlib.sha256(b"gate test agent v2").digest())

    def test_round_trip_with_the_test_key(self):
        public = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED)
        message = hashlib.sha256(b"GATEv1|example").digest()
        signature = ed25519.sign(ed25519.TEST_VERIFIER_SEED, message)
        self.assertTrue(ed25519.verify(public, message, signature))
        self.assertFalse(ed25519.verify(public, flip(message, 5), signature))
        self.assertFalse(ed25519.verify(public, message, flip(signature, 40, 3)))
        agent = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED)
        self.assertFalse(ed25519.verify(agent, message, signature))

    def test_key_file_equals_the_derived_key(self):
        public = ed25519.public_from_seed(ed25519.TEST_VERIFIER_SEED)
        lines = KEY_FILE.read_text(encoding="ascii").split("\n")
        self.assertEqual(lines, [base58.encode_key(public), public.hex(), ""])
        self.assertEqual(base58.decode_key(lines[0]), public)


class Base58Test(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(base58.encode(b"hello world"), "StV1DL6CwTryKyV")
        self.assertEqual(base58.encode(b""), "")
        self.assertEqual(base58.encode(b"\0\0\x01"), "112")
        self.assertEqual(base58.encode(bytes(32)), "1" * 32)

    def test_round_trip(self):
        for data in (b"", b"\0", b"\0\0\xff", bytes(range(32)), hashlib.sha256(b"x").digest()):
            with self.subTest(data=data):
                self.assertEqual(base58.decode(base58.encode(data)), data)

    def test_rejects_characters_outside_the_alphabet(self):
        for text in ("0", "O", "I", "l", "abc+"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                base58.decode(text)

    def test_key_form(self):
        public = ed25519.public_from_seed(ed25519.TEST_AGENT_SEED)
        self.assertTrue(base58.encode_key(public).startswith("ed25519:"))
        self.assertEqual(base58.decode_key(base58.encode_key(public)), public)
        with self.assertRaises(ValueError):
            base58.decode_key(base58.encode(public))


@unittest.skipIf(nacl is None, "PyNaCl is not installed; run under ~/.venvs/near/bin/python")
class PyNaClCrossCheckTest(unittest.TestCase):
    def test_rfc_vectors_and_test_seeds_agree(self):
        seeds = [bytes.fromhex(v[0]) for v in RFC8032_VECTORS]
        seeds += [ed25519.TEST_VERIFIER_SEED, ed25519.TEST_AGENT_SEED, ed25519.TEST_AGENT2_SEED]
        message = hashlib.sha256(b"cross check").digest()
        for seed in seeds:
            with self.subTest(seed=seed.hex()):
                key = nacl.signing.SigningKey(seed)
                self.assertEqual(key.verify_key.encode(), ed25519.public_from_seed(seed))
                self.assertEqual(key.sign(message).signature, ed25519.sign(seed, message))


if __name__ == "__main__":
    unittest.main()
