"""HKDF-based encryption for V3 frontend shards.

V3 format (per-file envelope):
  Bytes  0..7  : "ICSv3\\x00\\x00\\x00"  (magic)
  Bytes  8..39 : SHA-256(file_id)        (32 bytes, doubles as HKDF info)
  Bytes 40..   : AES-256-CBC ciphertext  (PKCS7 padded)

Key derivation:
  1. PBKDF2-HMAC-SHA256(password, fixed_salt, 100000) -> 32-byte master_key
  2. HKDF-SHA256(master_key, info=sha256(file_id), salt=b"") -> 48 bytes
     -> first 32 = AES key, last 16 = IV

The master_key is derived once; per-file keys are instant via HKDF.
"""

from __future__ import annotations

import hashlib

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Util.Padding import pad, unpad
from cryptography.hazmat.primitives.kdf.hkdf import HKDF as _HKDF
from cryptography.hazmat.primitives import hashes

MAGIC = b"ICSv3\x00\x00\x00"
MAGIC_LEN = 8
INFO_LEN = 32  # SHA-256 digest length
HEADER_LEN = MAGIC_LEN + INFO_LEN  # 40
KEY_SIZE = 32
IV_SIZE = 16
ITERATIONS = 100_000


def derive_master_key(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    """PBKDF2 password -> 32-byte master key."""
    return PBKDF2(
        password.encode("utf-8"), salt,
        dkLen=KEY_SIZE, count=iterations, hmac_hash_module=SHA256,
    )


def _file_id_hash(file_id: str) -> bytes:
    return hashlib.sha256(file_id.encode("utf-8")).digest()


def hkdf_derive_file_key(master_key: bytes, file_id: str) -> tuple[bytes, bytes]:
    """Derive (aes_key, iv) for a specific file_id via HKDF."""
    info = _file_id_hash(file_id)
    derived = _HKDF(
        algorithm=hashes.SHA256(), length=KEY_SIZE + IV_SIZE,
        salt=b"", info=info,
    ).derive(master_key)
    return derived[:KEY_SIZE], derived[KEY_SIZE:]


def encrypt_file(plaintext: bytes, master_key: bytes, file_id: str) -> bytes:
    """Encrypt plaintext with V3 envelope."""
    key, iv = hkdf_derive_file_key(master_key, file_id)
    info = _file_id_hash(file_id)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plaintext, AES.block_size))
    return MAGIC + info + ct


def decrypt_file(blob: bytes, master_key: bytes) -> bytes:
    """Decrypt a V3 envelope."""
    if len(blob) < HEADER_LEN + AES.block_size:
        raise ValueError("blob too short")
    if blob[:MAGIC_LEN] != MAGIC:
        raise ValueError("bad magic")
    info = blob[MAGIC_LEN:HEADER_LEN]
    ct = blob[HEADER_LEN:]
    derived = _HKDF(
        algorithm=hashes.SHA256(), length=KEY_SIZE + IV_SIZE,
        salt=b"", info=info,
    ).derive(master_key)
    key, iv = derived[:KEY_SIZE], derived[KEY_SIZE:]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return unpad(cipher.decrypt(ct), AES.block_size)
