"""Exercise the subtitle cache contract without touching the live database."""

import ast
import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


source = (Path(__file__).resolve().parents[1] / "app/subtitle_cache.py").read_text()
tree = ast.parse(source)
names = {"TextSubtitle", "ensure_subtitle_cache_schema", "invalidate_media", "invalidate_media_many", "invalidate_and_enqueue_media_many", "enqueue_media", "enqueue_media_many", "prioritize_final_media", "record_media_failure", "retry_failed_media", "pending_revision", "next_pending_media", "complete_pending_media", "fail_pending_media", "publish_media", "publish_track", "get_valid_media", "get_valid_track", "valid_cached_keys", "manifest_complete", "cache_record_complete", "missing_cache_paths"}
functions = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
for node in functions:
    node.decorator_list = [decorator for decorator in node.decorator_list if isinstance(node, ast.ClassDef)]
scope = {"dataclass": dataclass, "hashlib": hashlib, "CACHE_FORMAT_VERSION": 1}
exec(compile(ast.Module(body=functions, type_ignores=[]), "subtitle-cache-test", "exec"), scope)


db = sqlite3.connect(":memory:")
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys=ON")


@contextmanager
def local_connection():
    with db:
        yield db


scope["connect"] = local_connection
TextSubtitle = scope["TextSubtitle"]
ensure_subtitle_cache_schema = scope["ensure_subtitle_cache_schema"]
publish_media = scope["publish_media"]
get_valid_media = scope["get_valid_media"]
invalidate_media = scope["invalidate_media"]
publish_track = scope["publish_track"]
get_valid_track = scope["get_valid_track"]
manifest_complete = scope["manifest_complete"]
ensure_subtitle_cache_schema()

path = "/media/example.mkv"
english = TextSubtitle("embedded", 0, "", "subrip", "1\n00:00:00,000 --> 00:00:01,000\nHello\n")
portuguese = TextSubtitle("external", -1, "/media/example.pt.srt", "srt", "1\n00:00:00,000 --> 00:00:01,000\nOlá\n")
keys = {english.key, portuguese.key}

try:
    publish_media(path, "source-a", keys, [english])
except ValueError:
    pass
else:
    raise AssertionError("Partial cache was accepted")
assert get_valid_media(path, "source-a") is None
assert not manifest_complete(path, "source-a", 2)

publish_track(path, "source-a", keys, english)
assert get_valid_media(path, "source-a") is None
assert get_valid_track(path, "source-a", english.key) == english
assert get_valid_track(path, "source-b", english.key) is None
assert scope["valid_cached_keys"](path, "source-a") == {english.key}
assert scope["valid_cached_keys"](path, "source-b") == set()
publish_track(path, "source-a", keys, portuguese)
assert get_valid_media(path, "source-a") == [english, portuguese]
assert scope["valid_cached_keys"](path, "source-a") == keys
assert manifest_complete(path, "source-a", 2)

publish_media(path, "source-a", keys, [english, portuguese])
assert get_valid_media(path, "source-a") == [english, portuguese]
assert get_valid_media(path, "source-b") is None

try:
    publish_media(path, "source-b", keys, [english, english])
except ValueError:
    pass
else:
    raise AssertionError("Duplicate track was accepted")
assert get_valid_media(path, "source-a") == [english, portuguese]

db.execute("UPDATE subtitle_cache_track SET text_content='corrupt' WHERE path=? AND source='embedded'", (path,))
assert get_valid_media(path, "source-a") is None
assert scope["valid_cached_keys"](path, "source-a") == {portuguese.key}
publish_media(path, "source-b", keys, [english, portuguese])
assert get_valid_media(path, "source-b") == [english, portuguese]
invalidate_media(path)
assert get_valid_media(path, "source-b") is None
assert db.execute("SELECT count(*) FROM subtitle_cache_track").fetchone()[0] == 0

scope["enqueue_media"](path)
first_revision = scope["pending_revision"](path)
assert scope["next_pending_media"](1000) == path
assert scope["enqueue_media_many"]([path, path]) == 1
assert scope["pending_revision"](path) == first_revision + 1
scope["complete_pending_media"](path, first_revision)
assert scope["next_pending_media"](1000) == path
scope["fail_pending_media"](path, "temporary error", 1000)
assert scope["next_pending_media"](1000) is None
assert scope["next_pending_media"](1060) == path
scope["complete_pending_media"](path, first_revision + 1)
assert scope["next_pending_media"](2000) is None

ordinary = "/media/ordinary.mkv"
final = "/media/final.mkv"
db.executescript("""
CREATE TABLE media_stream_index_state(path TEXT PRIMARY KEY);
CREATE TABLE media_stream_index(path TEXT, stream_type TEXT);
""")
scope["enqueue_media"](ordinary)
assert scope["prioritize_final_media"]([final, final]) == 1
assert scope["next_pending_media"](2000) == final
revision = scope["pending_revision"](final)
assert scope["prioritize_final_media"]([final]) == 1
assert scope["record_media_failure"](final, "unreadable subtitle", scope["pending_revision"](final))
assert scope["pending_revision"](final) is None
assert scope["prioritize_final_media"]([final]) == 0
assert scope["retry_failed_media"]() == 1
assert scope["pending_revision"](final) == 1
assert db.execute("SELECT 1 FROM subtitle_cache_failure WHERE path=?", (final,)).fetchone() is None
assert scope["record_media_failure"](final, "still unreadable", 1)
scope["enqueue_media"](final)
assert db.execute("SELECT 1 FROM subtitle_cache_failure WHERE path=?", (final,)).fetchone() is None
assert scope["pending_revision"](final) == 1
scope["complete_pending_media"](final, 1)
assert scope["next_pending_media"](2000) == ordinary

publish_media(final, "source-c", {english.key}, [english])
assert scope["cache_record_complete"](final)
assert scope["prioritize_final_media"]([final]) == 0
assert scope["pending_revision"](final) is None
invalidate_media(final)
assert scope["prioritize_final_media"]([final]) == 1

db.executescript("""
CREATE TABLE plex_media(path TEXT PRIMARY KEY);
INSERT INTO plex_media VALUES('/media/no-sub.mkv'),('/media/with-sub.mkv'),('/media/not-indexed.mkv');
INSERT INTO media_stream_index_state VALUES('/media/no-sub.mkv'),('/media/with-sub.mkv');
INSERT INTO media_stream_index VALUES('/media/with-sub.mkv','subtitle');
""")
assert scope["prioritize_final_media"](['/media/no-sub.mkv']) == 0
assert scope["missing_cache_paths"](['/media/no-sub.mkv', '/media/with-sub.mkv', '/media/not-indexed.mkv']) == {
    '/media/with-sub.mkv', '/media/not-indexed.mkv'
}
publish_media('/media/with-sub.mkv', 'signature', {english.key}, [english])
assert scope["missing_cache_paths"](['/media/with-sub.mkv']) == set()
assert scope["invalidate_and_enqueue_media_many"](['/media/with-sub.mkv']) == 1
assert not scope["cache_record_complete"]('/media/with-sub.mkv')
assert scope["missing_cache_paths"](['/media/with-sub.mkv']) == {'/media/with-sub.mkv'}
assert scope["pending_revision"]('/media/with-sub.mkv') == 1
publish_media('/media/with-sub.mkv', 'signature', {english.key}, [english])
revision = scope["pending_revision"]('/media/with-sub.mkv')
assert scope["invalidate_media_many"](['/media/with-sub.mkv', '/media/with-sub.mkv']) == 1
assert not scope["cache_record_complete"]('/media/with-sub.mkv')
assert scope["pending_revision"]('/media/with-sub.mkv') == revision  # Invalidation alone does not enqueue.

scope["enqueue_media"]('/media/failure-one.mkv')
assert scope["record_media_failure"]('/media/failure-one.mkv', 'bad subtitle', 1)
scope["enqueue_media"]('/media/failure-two.mkv')
assert scope["record_media_failure"]('/media/failure-two.mkv', 'bad subtitle', 1)
assert scope["retry_failed_media"]('/media/failure-one.mkv') == 1
assert scope["pending_revision"]('/media/failure-one.mkv') == 1
assert db.execute("SELECT 1 FROM subtitle_cache_failure WHERE path='/media/failure-two.mkv'").fetchone()

print("PASS: complete cache publication, validity, invalidation, and generation-safe priority recache")
