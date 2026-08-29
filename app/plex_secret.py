from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.v2 import CONFIG_DIR

KEY_PATH = CONFIG_DIR / "plex-token.key"


def cipher() -> Fernet:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        key = KEY_PATH.read_bytes()
    except FileNotFoundError:
        key = Fernet.generate_key()
        try:
            with KEY_PATH.open("xb") as file:
                file.write(key)
            os.chmod(KEY_PATH, 0o600)
        except FileExistsError:
            key = KEY_PATH.read_bytes()
    return Fernet(key)


def encrypt_token(token: str) -> str:
    return cipher().encrypt(token.encode()).decode() if token else ""


def decrypt_token(value: str) -> str:
    if not value:
        return ""
    try:
        return cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        # One-time compatibility for a token saved before encryption support.
        return value
