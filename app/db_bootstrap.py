"""First-run PostgreSQL bootstrap configuration.

The application can start without a DATABASE_URL so a new installation can
use the web setup wizard.  Once configured, only the application connection
URL is encrypted on disk; administrator credentials are deliberately never
stored.
"""
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
CONNECTION_PATH = CONFIG_DIR / "database.connection.enc"
KEY_PATH = CONFIG_DIR / "plex-token.key"


def _cipher() -> Fernet:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        key = KEY_PATH.read_bytes()
    except FileNotFoundError:
        key = Fernet.generate_key()
        try:
            KEY_PATH.write_bytes(key)
            os.chmod(KEY_PATH, 0o600)
        except OSError:
            pass
    return Fernet(key)


def load_saved_url() -> str:
    try:
        encrypted = CONNECTION_PATH.read_text(encoding="utf-8").strip()
        if not encrypted:
            return ""
        return _cipher().decrypt(encrypted.encode()).decode()
    except (FileNotFoundError, InvalidToken, OSError, ValueError):
        return ""


def save_url(url: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    encrypted = _cipher().encrypt(url.encode()).decode()
    temporary = CONNECTION_PATH.with_suffix(".enc.partial")
    temporary.write_text(encrypted, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, CONNECTION_PATH)


def effective_url() -> str:
    saved = load_saved_url()
    # A completed wizard/migration is authoritative. An environment URL can
    # still be forced for emergency/admin overrides with DATABASE_URL_OVERRIDE=1.
    if saved and os.getenv("DATABASE_URL_OVERRIDE", "0") != "1":
        return saved
    return os.getenv("DATABASE_URL", "").strip() or saved


def target_url(host_url: str, username: str, password: str, database: str) -> str:
    raw = str(host_url or "").strip()
    # Accept the forms users commonly paste into the wizard while normalizing
    # them to a real PostgreSQL URI. A scheme is still required for ambiguous
    # values such as a bare hostname containing a colon.
    if "://" not in raw:
        raw = "postgresql://" + raw.rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("Enter a PostgreSQL server as postgresql://host[:port]/database or host[:port]")
    query = parsed.query
    auth = urllib.parse.quote(username, safe="")
    if password:
        auth += ":" + urllib.parse.quote(password, safe="")
    netloc = auth + "@" + parsed.hostname
    if parsed.port:
        netloc += f":{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, "/" + database, query, ""))


def maintenance_url(host_url: str, username: str, password: str, database: str = "postgres") -> str:
    return target_url(host_url, username, password, database)


def configured() -> bool:
    return bool(os.getenv("DATABASE_URL", "").strip() or CONNECTION_PATH.is_file())


# Load saved configuration before app modules construct their database adapter.
_saved = effective_url()
if _saved:
    os.environ["DATABASE_URL"] = _saved
if os.getenv("DATABASE_URL", "").strip():
    # A saved URL becomes authoritative after restart.
    os.environ["DATABASE_BACKEND"] = "postgres"
else:
    # First access serves only the wizard: no temporary operational database.
    os.environ["DATABASE_BACKEND"] = "postgres"
