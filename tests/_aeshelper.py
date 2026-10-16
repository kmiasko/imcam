"""Builds the encrypted access-key blob the server sends, using the `cryptography` package (an aiortc dependency)
as an independent AES implementation."""

import base64
import hashlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PREFIX = "pre-"
MAGIC = 0x0000FFFF  # selects hex chars 16..31 of the md5 -> a 16-byte AES key


def make_cipher(session_id, plaintext, magic=MAGIC, prefix=PREFIX):
    """(cipher_b64, expected access key) for the given session id and plaintext."""
    key_hex = hashlib.md5((prefix + session_id).encode()).hexdigest()
    key = "".join(
        key_hex[i] for i, bit in enumerate(format(magic, "b").zfill(32)) if bit == "1"
    ).encode()
    padder = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ct = enc.update(padder.update(plaintext) + padder.finalize()) + enc.finalize()
    return base64.b64encode(bytes(16) + ct).decode(), hashlib.md5(
        plaintext
    ).hexdigest().upper()
