import pytest

from _aeshelper import MAGIC, PREFIX, make_cipher
from imcam import accesskey


def test_aes_ecb_decrypt_matches_fips197_vector():
    key = bytes(range(16))
    cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    assert accesskey.aes_ecb_decrypt(key, cipher) == bytes.fromhex(
        "00112233445566778899aabbccddeeff"
    )


def test_aes_rejects_bad_key_or_data_length():
    with pytest.raises(ValueError):
        accesskey.aes_ecb_decrypt(b"short", bytes(16))
    with pytest.raises(ValueError):
        accesskey.aes_ecb_decrypt(bytes(16), bytes(15))


@pytest.mark.parametrize("plaintext", [b"secret-token", b"x" * 16, b"y" * 47])
def test_derive_access_key_round_trips_against_an_independent_aes(
    monkeypatch, plaintext
):
    monkeypatch.setenv("IMCAM_AES_PREFIX", PREFIX)
    cipher_b64, expected = make_cipher("sess-1", plaintext)
    assert accesskey.derive_access_key("sess-1", MAGIC, cipher_b64) == expected


def test_derive_access_key_rejects_a_magic_selecting_a_bad_key_length(monkeypatch):
    monkeypatch.setenv("IMCAM_AES_PREFIX", PREFIX)
    with pytest.raises(ValueError, match="not in"):
        accesskey.derive_access_key("sess-1", 0b111, "AAAA")
