"""Timezone selection changes wall-clock scheduling without altering UTC instants."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import system_timezone as clock


original_tz = os.environ.get("TZ")
try:
    with TemporaryDirectory() as folder:
        clock.CONFIG_PATH = Path(folder) / "system-timezone.json"
        clock.initialize()
        assert clock.selected_timezone() == "America/Sao_Paulo"
        assert datetime.now().astimezone().utcoffset().total_seconds() == -3 * 3600
        saved = clock.set_timezone(clock.TimezoneSelection(timezone="Europe/Lisbon"))
        assert saved["timezone"] == "Europe/Lisbon"
        assert json.loads(clock.CONFIG_PATH.read_text())["timezone"] == "Europe/Lisbon"
        clock.initialize()
        assert clock.selected_timezone() == "Europe/Lisbon"
        assert datetime(2026, 9, 30, 1, tzinfo=timezone.utc).astimezone().hour == 2
        try:
            clock.set_timezone(clock.TimezoneSelection(timezone="Not/A_Timezone"))
        except Exception:
            pass
        else:
            raise AssertionError("Invalid timezone was accepted")
        assert clock.selected_timezone() == "Europe/Lisbon"
finally:
    if original_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original_tz
    time.tzset()

print("PASS: persisted IANA timezone, local scheduling clock, and invalid-zone rejection")
