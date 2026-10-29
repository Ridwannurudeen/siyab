"""The wasm hash and export list, offline and standard library only.

codehash gives the sha256 of a wasm file in hex and the base58 of the same 32 byte digest, which is
the code_hash form view_account reports. exports parses the binary module format (the magic, the
version 1, then sections of an id byte and a LEB128 size) and returns the names of the exported
functions from the export section, id 7: each entry is a name, a kind byte (0 is a function) and an
index.
"""

import hashlib

from gate import base58

MAGIC = b"\0asm"
VERSION = b"\x01\0\0\0"
EXPORT_SECTION = 7
FUNCTION_KIND = 0


def codehash(data):
    """(sha256 hex, base58 of the digest) of the wasm bytes."""
    digest = hashlib.sha256(data).digest()
    return digest.hex(), base58.encode(digest)


def _uleb(data, offset):
    """The unsigned LEB128 number at offset and the offset after it."""
    value, shift = 0, 0
    while True:
        if offset >= len(data):
            raise ValueError("the module ends inside a LEB128 number")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 35:
            raise ValueError("a LEB128 number is longer than 5 bytes")


def exports(data):
    """The exported function names, sorted; raises ValueError on a module that does not parse."""
    if data[:4] != MAGIC or data[4:8] != VERSION:
        raise ValueError("not a version 1 wasm module: the magic or the version does not match")
    names, offset = [], 8
    while offset < len(data):
        section, offset = data[offset], offset + 1
        size, offset = _uleb(data, offset)
        end = offset + size
        if end > len(data):
            raise ValueError(f"section {section} runs past the end of the module")
        if section == EXPORT_SECTION:
            count, cursor = _uleb(data, offset)
            for _ in range(count):
                length, cursor = _uleb(data, cursor)
                name = data[cursor:cursor + length].decode("utf-8")
                cursor += length
                if cursor >= end:
                    raise ValueError("an export entry runs past its section")
                kind = data[cursor]
                _, cursor = _uleb(data, cursor + 1)
                if kind == FUNCTION_KIND:
                    names.append(name)
        offset = end
    return sorted(names)

