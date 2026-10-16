"""Signaling access key: a from-scratch AES-128/192/256 ECB decrypt (stdlib only) and the key derivation
the /im server expects."""
import base64
import hashlib

from . import config


def _gf_mul(a, b):
    p = 0
    for i in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = (a << 1) ^ (0x11b if a & 0x80 else 0)
    return p

def _gf_inv(x):
    if x == 0:
        return 0
    p, r, e = 1, x, 254
    while e:
        if e & 1:
            p = _gf_mul(p, r)
        e >>= 1
        r = _gf_mul(r, r)
    return p

def _rotl8(b, n):
    return ((b << n) | (b >> (8 - n))) & 0xff

# FIPS-197 5.1.1: affine transform uses bitwise rotations, not GF(2^8) multiplies
_SBOX = bytes((lambda b: b ^ _rotl8(b, 1) ^ _rotl8(b, 2) ^ _rotl8(b, 3) ^ _rotl8(b, 4) ^ 0x63)(_gf_inv(i))
              for i in range(256))
_INV_SBOX = bytes(_SBOX.index(i) for i in range(256))

def _aes_expand(key):
    nk, nr = len(key) // 4, len(key) // 4 + 6
    rcon = [1, 2, 4, 8, 16, 32, 64, 128, 27, 54]
    w = list(key)
    for i in range(nk, 4 * (nr + 1)):
        prev = w[(i - 1) * 4:(i - 1) * 4 + 4]
        cur = w[(i - nk) * 4:(i - nk) * 4 + 4]
        if i % nk == 0:
            prev = prev[1:] + prev[:1]
            prev = [_SBOX[b] for b in prev]
            prev[0] ^= rcon[i // nk - 1]
        w += [a ^ b for a, b in zip(cur, prev)]
    return [bytes(w[i * 16:(i + 1) * 16]) for i in range(len(w) // 16)]

def _inv_cipher_block(blk, rk):
    nr = len(rk) - 1
    s = list(blk)

    def add_rk(s, k):
        return [a ^ b for a, b in zip(s, k)]

    def inv_shift(s):
        out = [0] * 16
        for c in range(4):
            for rr in range(4):
                out[c * 4 + rr] = s[((c - rr) % 4) * 4 + rr]
        return out

    def inv_mix(s):
        mc = [0] * 16
        for c in range(4):
            col = s[c * 4:c * 4 + 4]
            mc[c * 4]     = _gf_mul(col[0], 14) ^ _gf_mul(col[1], 11) ^ _gf_mul(col[2], 13) ^ _gf_mul(col[3], 9)
            mc[c * 4 + 1] = _gf_mul(col[0], 9)  ^ _gf_mul(col[1], 14) ^ _gf_mul(col[2], 11) ^ _gf_mul(col[3], 13)
            mc[c * 4 + 2] = _gf_mul(col[0], 13) ^ _gf_mul(col[1], 9)  ^ _gf_mul(col[2], 14) ^ _gf_mul(col[3], 11)
            mc[c * 4 + 3] = _gf_mul(col[0], 11) ^ _gf_mul(col[1], 13) ^ _gf_mul(col[2], 9)  ^ _gf_mul(col[3], 14)
        return mc

    s = add_rk(s, rk[nr])
    for r in range(nr - 1, 0, -1):
        s = inv_shift(s)
        s = [_INV_SBOX[b] for b in s]
        s = add_rk(s, rk[r])
        s = inv_mix(s)
    s = inv_shift(s)
    s = [_INV_SBOX[b] for b in s]
    return bytes(add_rk(s, rk[0]))

def aes_ecb_decrypt(key, data):
    if len(key) not in (16, 24, 32) or len(data) % 16:
        raise ValueError("invalid AES key/data")
    rk = _aes_expand(key)
    out = [_inv_cipher_block(data[i:i + 16], rk) for i in range(0, len(data), 16)]
    dec = b"".join(out)
    pad = dec[-1]
    if 1 <= pad <= 16:
        dec = dec[:-pad]
    return dec

def derive_access_key(session_id, magic, cipher_b64):
    """Signaling access key: AES key = md5(prefix+sid).lower() hex chars selected by
    1-bits of magic's (padded) binary; AES/ECB/PKCS5 decrypt of cipher[16:];
    result = uppercase MD5 of the plaintext."""
    key_hex = hashlib.md5((config.aes_prefix() + session_id).encode("utf-8")).hexdigest().lower()
    sel = "".join(key_hex[i] for i, bit in enumerate(format(magic, "b").zfill(32)) if bit == "1")
    if len(sel) not in (16, 24, 32):
        raise ValueError("selected AES key length %d not in (16,24,32)" % len(sel))
    dec = aes_ecb_decrypt(sel.encode("utf-8"), base64.b64decode(cipher_b64)[16:])
    return hashlib.md5(dec).hexdigest().upper()
