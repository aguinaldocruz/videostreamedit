"""Schedule safety checks without starting a worker or touching production DB."""

import ast
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


tree = ast.parse((Path(__file__).resolve().parents[1] / "app/subtitle_cache_schedule.py").read_text())
names = {"_parse_clock", "_due", "_next_run", "ensure_schema", "_final_uncached_paths", "_remaining_work_count"}
nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
for node in nodes:
    node.decorator_list = []

database = sqlite3.connect(":memory:")
database.row_factory = sqlite3.Row


@contextmanager
def connect():
    with database:
        yield database


scope = {"connect": connect, "datetime": datetime, "timedelta": timedelta,
         "time": time, "remaining_work_lock": threading.Lock(), "remaining_work_snapshot": (0.0, 0),
         "FREQUENCY_DAYS": {"daily": 1, "every_other_day": 2, "weekly": 7},
         "CACHE_FORMAT_VERSION": 1}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "subtitle-cache-schedule-test", "exec"), scope)
scope["ensure_schema"]()
row = dict(database.execute("SELECT * FROM subtitle_cache_schedule WHERE id=1").fetchone())
assert row["frequency"] == "disabled" and row["run_minutes"] == 60 and row["cursor_offset"] == 0
assert not scope["_due"](row, datetime.now().astimezone())
assert scope["_next_run"](row) is None

now = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
row.update(frequency="daily", time_of_day="03:00", last_run=None)
assert scope["_due"](row, now)
row["last_run"] = now.isoformat()
assert not scope["_due"](row, now)
assert scope["_due"](row, now + timedelta(days=1))
for invalid in ("25:00", "03:60", "bad"):
    try:
        scope["_parse_clock"](invalid)
    except ValueError:
        pass
    else:
        raise AssertionError(f"Invalid schedule time accepted: {invalid}")
database.executescript("""
CREATE TABLE plex_media(path TEXT PRIMARY KEY, modified INTEGER, plex_added_at INTEGER);
CREATE TABLE subtitle_cache_pending(path TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1);
INSERT INTO plex_media VALUES('/old',50,50),('/changed',120,50),('/new',50,120);
INSERT INTO subtitle_cache_pending(path,revision) VALUES('/changed',4);
""")
assert [tuple(row) for row in database.execute("SELECT path,revision FROM subtitle_cache_pending")] == [("/changed", 4)]
database.executescript("""
ALTER TABLE plex_media ADD COLUMN kind TEXT NOT NULL DEFAULT 'movie';
ALTER TABLE plex_media ADD COLUMN library_key TEXT NOT NULL DEFAULT '';
ALTER TABLE plex_media ADD COLUMN show_title TEXT NOT NULL DEFAULT '';
CREATE TABLE media_notes(entity_type TEXT, entity_key TEXT, final_version INTEGER);
CREATE TABLE subtitle_cache_media(path TEXT, format_version INTEGER, expected_tracks INTEGER, cached_tracks INTEGER);
CREATE TABLE subtitle_cache_failure(path TEXT PRIMARY KEY);
CREATE TABLE media_stream_index_state(path TEXT PRIMARY KEY);
CREATE TABLE media_stream_index(path TEXT,stream_type TEXT);
CREATE TABLE subtitle_cache_track(path TEXT);
INSERT INTO plex_media(path,kind,library_key,show_title) VALUES
 ('/final-movie','movie','',''),('/show-episode','episode','tv','A Show'),('/not-final','movie','','');
INSERT INTO media_notes VALUES('movie','/final-movie',1),('tv','tv:A Show',1);
INSERT INTO subtitle_cache_media VALUES('/final-movie',1,1,1);
INSERT INTO subtitle_cache_track VALUES('/final-movie');
""")
assert scope["_final_uncached_paths"](database) == ["/show-episode"]
database.execute("INSERT INTO media_stream_index_state VALUES('/show-episode')")
assert scope["_final_uncached_paths"](database) == []
database.execute("INSERT INTO media_stream_index VALUES('/show-episode','subtitle')")
assert scope["_final_uncached_paths"](database) == ["/show-episode"]
assert scope["_remaining_work_count"]() == 5
database.execute("INSERT INTO subtitle_cache_failure VALUES('/show-episode')")
scope["remaining_work_snapshot"] = (0.0, 0)
assert scope["_remaining_work_count"]() == 4
print("PASS: cache schedule starts disabled, validates time, and waits for the next recurrence")
