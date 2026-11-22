"""An ephemeral signing key generated inside the guest process."""


class EnclaveSigner:
    __slots__ = ("__key", "public_key_hex")
    key_id = "cur"

    def __init__(self):
        from nacl.signing import SigningKey
        self.__key = SigningKey.generate()
        self.public_key_hex = self.__key.verify_key.encode().hex()

    def sign(self, digest):
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise ValueError("enclave signing requires a 32 byte digest")
        return self.__key.sign(digest).signature

    def __reduce_ex__(self, protocol):
        raise TypeError("enclave signing keys cannot be serialized")
