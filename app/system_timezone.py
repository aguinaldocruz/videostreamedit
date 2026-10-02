"""One persisted IANA timezone for application schedules and presentation."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


DEFAULT_ZONE = "America/Sao_Paulo"
CONFIG_PATH = Path(os.getenv("CONFIG_DIR", "/config")) / "system-timezone.json"
router = APIRouter()
_lock = threading.RLock()
_selected = DEFAULT_ZONE


def _validate(zone: str) -> str:
    zone = str(zone or "").strip()
    if zone not in available_timezones():
        raise ValueError("Choose a valid Linux/IANA timezone from the list")
    ZoneInfo(zone)
    return zone


def _apply(zone: str) -> None:
    global _selected
    os.environ["TZ"] = zone
    time.tzset()
    _selected = zone


def initialize() -> None:
    with _lock:
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            zone = _validate(saved.get("timezone"))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            if CONFIG_PATH.exists():
                logging.getLogger("uvicorn.error").warning("timezone event=invalid_saved_config error=%s", str(exc)[:200])
            zone = DEFAULT_ZONE
        _apply(zone)


def selected_timezone() -> str:
    with _lock:
        return _selected


class TimezoneSelection(BaseModel):
    timezone: str


@router.get("/api/system/timezone")
def timezone_status() -> dict:
    with _lock:
        return {"timezone": _selected, "now": datetime.now().astimezone().isoformat(timespec="seconds")}


@router.get("/api/system/timezone/zones")
def timezone_zones() -> dict:
    return {"zones": sorted(available_timezones())}


@router.put("/api/system/timezone")
def set_timezone(request: TimezoneSelection) -> dict:
    try:
        zone = _validate(request.timezone)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with _lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump({"timezone": zone}, output)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, CONFIG_PATH)
        finally:
            temporary.unlink(missing_ok=True)
        _apply(zone)
    logging.getLogger("uvicorn.error").info("timezone event=changed zone=%s", zone)
    return timezone_status()
