"""Local AES-GCM for camera passwords. Key lives next to the DB, not in git."""

from __future__ import annotations

from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes


def load_or_create_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        key = path.read_bytes()
        if len(key) == 32:
            return key
    key = get_random_bytes(32)
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def encrypt(key: bytes, plaintext: str) -> bytes:
    nonce = get_random_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return nonce + tag + ciphertext


def decrypt(key: bytes, blob: bytes) -> str:
    if len(blob) < 28:
        return ""
    nonce, tag, ciphertext = blob[:12], blob[12:28], blob[28:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
