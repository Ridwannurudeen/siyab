"""Base58 with the Bitcoin alphabet, for the NEAR `ed25519:<base58>` key form."""

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: value for value, char in enumerate(ALPHABET)}
KEY_PREFIX = "ed25519:"


def encode(data):
    """Base58 text of the bytes; each leading zero byte becomes a leading '1'."""
    number = int.from_bytes(data, "big")
    digits = []
    while number:
        number, rest = divmod(number, 58)
        digits.append(ALPHABET[rest])
    zeros = len(data) - len(data.lstrip(b"\0"))
    return "1" * zeros + "".join(reversed(digits))


def decode(text):
    """Bytes of the base58 text; raises ValueError on a character outside the alphabet."""
    number = 0
    for char in text:
        if char not in _INDEX:
            raise ValueError(f"not a base58 character: {char!r}")
        number = number * 58 + _INDEX[char]
    zeros = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\0" * zeros + body


def encode_key(raw):
    """The NEAR key form `ed25519:<base58>` of raw key bytes."""
    return KEY_PREFIX + encode(raw)


def decode_key(text):
    """Raw bytes of a NEAR `ed25519:<base58>` key string."""
    if not text.startswith(KEY_PREFIX):
        raise ValueError("key string does not start with ed25519:")
    return decode(text[len(KEY_PREFIX):])
