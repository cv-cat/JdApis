# coding: utf-8
"""Passport AKS query encryption, rebuilt from the loaded ``aks.js`` and
``summer-cryptico-h5.min.js`` sources.

The passport page encrypts the *already URL-encoded* jQuery query string and
replaces it with one ``aksParamsU`` parameter.  This module mirrors the
browser format:

``base64(public-key-header || SM2(C1C3C2) || iv || SM4-CBC(query))``

The browser keeps an 8-byte random SM4 key (represented as 16 ASCII hex
characters) in localStorage, encrypted with the fixed storage key/IV.  A
small local file is used for the same persistence when running outside the
browser; it contains no JD cookie or account data.
"""

import base64
import json
import os
import secrets
from urllib.parse import quote

from gmssl import sm2, sm4


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AKS_STATE = os.path.join(_ROOT, "datas", "aks_key.json")
_STORAGE_KEY = b"6c3d6878252e641b"  # JS utf8ToHex("6c3d...")
_STORAGE_IV = bytes.fromhex("5f5e5a247f544d771255134517043757")


def _sm4_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = sm4.CryptSM4()
    cipher.set_key(key, sm4.SM4_ENCRYPT)
    return cipher.crypt_cbc(iv, data)


def _sm4_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = sm4.CryptSM4()
    cipher.set_key(key, sm4.SM4_DECRYPT)
    return cipher.crypt_cbc(iv, data)


def _random_hex_key() -> str:
    # summer-cryptico randomUnit8Array(1, 127, 8), then buffertoHex().
    return bytes(secrets.randbelow(127) + 1 for _ in range(8)).hex()


def _load_sm4_key(path: str = _AKS_STATE) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            encrypted_hex = (json.load(fh) or {}).get("aksKey", "")
        if encrypted_hex:
            plain = _sm4_decrypt(bytes.fromhex(encrypted_hex), _STORAGE_KEY,
                                 _STORAGE_IV).decode("utf-8")
            if len(plain) == 16 and all(c in "0123456789abcdef" for c in plain):
                return plain
    except (OSError, ValueError, TypeError, KeyError):
        pass

    key = _random_hex_key()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    encrypted = _sm4_encrypt(key.encode("utf-8"), _STORAGE_KEY, _STORAGE_IV)
    # Keep the file shape equivalent to localStorage's single aksKey value.
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"aksKey": encrypted.hex()}, fh, ensure_ascii=False)
    return key


def _decode_public_key(public_key_b64: str):
    raw = base64.b64decode(public_key_b64)
    if len(raw) < 65:
        raise ValueError("passport publicKey/init 返回值长度不足")
    return raw[:-65], raw[-65:]


def encrypt_query(query: str, public_key_b64: str, state_path: str = _AKS_STATE) -> str:
    """Return the browser-compatible base64 AKS payload for ``query``.

    ``query`` must be the jQuery-encoded text (for example ``t=...&h5st=...``),
    not a decoded dict.  ``public_key_b64`` is the exact ``data`` value from
    ``/publicKey/init``.
    """
    if not query:
        raise ValueError("AKS query cannot be empty")
    public_header, public_key = _decode_public_key(public_key_b64)
    # summer-cryptico strips the uncompressed-point 04 prefix before SM2.
    public_hex = public_key[1:].hex() if public_key[:1] == b"\x04" else public_key.hex()
    sm4_key = _load_sm4_key(state_path)
    iv = bytes(secrets.randbelow(127) + 1 for _ in range(16))
    # summer-cryptico's default ``doEncrypt(..., mode=1)`` emits C1C3C2
    # (the gmssl mode=1 layout), not gmssl's default C1C2C3.
    sm2_cipher = sm2.CryptSM2(private_key="", public_key=public_hex,
                               mode=1).encrypt(sm4_key.encode("utf-8"))
    sm4_cipher = _sm4_encrypt(query.encode("utf-8"), sm4_key.encode("utf-8"), iv)
    payload = public_header + sm2_cipher + iv + sm4_cipher
    return base64.b64encode(payload).decode("ascii")


def encode_component(value) -> str:
    """JavaScript ``encodeURIComponent`` equivalent used by jQuery.param."""
    return quote("" if value is None else str(value), safe="-_.!~*'()")


def encode_query_pairs(pairs) -> str:
    """Serialize ordered key/value pairs exactly as jQuery's URL query data."""
    return "&".join(f"{encode_component(k)}={encode_component(v)}"
                     for k, v in pairs)
