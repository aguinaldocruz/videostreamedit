"""Exercise the subtitle cache contract without touching the live database."""

import ast
import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


source = (Path(__file__).resolve().parents[1] / "app/subtitle_cache.py").read_text()
tree = ast.parse(source)
names = {"TextSubtitle", "ensure_subtitle_cache_schema", "invalidate_media", "invalidate_media_many", "invalidate_and_enqueue_media_many", "enqueue_media", "enqueue_media_many", "prioritize_final_media", "record_media_failure", "retry_failed_media", "pending_revision", "next_pending_media", "complete_pending_media", "fail_pending_media", "publish_media", "publish_track", "get_valid_media", "get_valid_track", "get_valid_tracks", "publish_replacement_cache", "cache_records_present", "reconcile_source_cache", "valid_cached_keys", "manifest_complete", "cache_record_complete", "missing_cache_paths"}
functions = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
for node in functions:
    node.decorator_list = [decorator for decorator in node.decorator_list if isinstance(node, ast.ClassDef)]
scope = {"dataclass": dataclass, "hashlib": hashlib, "CACHE_FORMAT_VERSION": 1,
         "IMAGE_CACHE_FORMAT_VERSION": 1}
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
assert scope["get_valid_tracks"](path, "source-a") == [english]
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
assert scope["get_valid_tracks"](path, "source-a") == [portuguese]
publish_media(path, "source-b", keys, [english, portuguese])
assert get_valid_media(path, "source-b") == [english, portuguese]
invalidate_media(path)
assert get_valid_media(path, "source-b") is None
assert db.execute("SELECT count(*) FROM subtitle_cache_track").fetchone()[0] == 0

empty = TextSubtitle("embedded", 1, "", "subrip", "", "empty", "UTF-8")
legacy = TextSubtitle("embedded", 0, "", "subrip", portuguese.text, "ready", "Windows-1252 (inferred)")
publish_media(path, "empty-source", {empty.key, legacy.key}, [empty, legacy])
assert get_valid_media(path, "empty-source") == [legacy, empty]
assert manifest_complete(path, "empty-source", 2)
assert get_valid_track(path, "empty-source", empty.key).extraction_status == "empty"
try:
    publish_track(path, "empty-source", {empty.key}, TextSubtitle("embedded", 1, "", "subrip", ""))
except ValueError:
    pass
else:
    raise AssertionError("An unverified empty payload was accepted")
db.execute("UPDATE subtitle_cache_track SET extraction_status='ready' WHERE path=? AND type_index=1", (path,))
assert get_valid_track(path, "empty-source", empty.key) is None
assert get_valid_media(path, "empty-source") is None
invalidate_media(path)

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
CREATE TABLE media_stream_index(path TEXT, stream_type TEXT, codec TEXT);
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
INSERT INTO media_stream_index VALUES('/media/with-sub.mkv','subtitle','SubRip/SRT');
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

# A cleanup publishes changed and unchanged tracks together, retaining a
# complete, source-matching image cache without transferring/reinserting blobs.
replacement = scope['publish_replacement_cache']
images = {(2, 'pgs')}
db.execute('INSERT INTO subtitle_image_cache_media(path,source_signature,format_version,expected_tracks) VALUES(?,?,1,1)', (path, 'image-before'))
db.execute('INSERT INTO subtitle_image_cache_track(path,type_index,codec,payload,index_payload,sha256) VALUES(?,2,?,?,?,?)', (path, 'pgs', b'image', b'', hashlib.sha256(b'image').hexdigest()))
def replace_cache(sig, items, before='image-before', after='image-after'):
    return replacement(path, sig, keys, items, image_before_signature=before, image_after_signature=after, expected_images=images)
assert replace_cache('changed-source', [english])
assert not manifest_complete(path, 'changed-source', 2)
assert scope['get_valid_tracks'](path, 'changed-source') == [english]
assert db.execute('SELECT source_signature FROM subtitle_image_cache_media WHERE path=?', (path,)).fetchone()[0] == 'image-after'
assert db.execute('SELECT payload FROM subtitle_image_cache_track WHERE path=?', (path,)).fetchone()[0] == b'image'
assert replace_cache('all-updated', [english, portuguese], before='image-after', after='image-after-two')
assert get_valid_media(path, 'all-updated') == [english, portuguese]
# SQL failure rolls back the entire publication, including image rebinding.
db.execute("CREATE TRIGGER fail_publication BEFORE INSERT ON subtitle_cache_track WHEN NEW.codec='injected-failure' BEGIN SELECT RAISE(ABORT,'test publication failure'); END")
try:
    replace_cache('failed-publish', [TextSubtitle('embedded', 0, '', 'injected-failure', english.text)], before='image-after-two', after='wrong')
except sqlite3.IntegrityError:
    pass
else:
    raise AssertionError('Injected publication failure was ignored')
assert get_valid_media(path, 'all-updated') == [english, portuguese]
assert db.execute('SELECT source_signature FROM subtitle_image_cache_media WHERE path=?', (path,)).fetchone()[0] == 'image-after-two'
db.execute('DROP TRIGGER fail_publication')
assert not replace_cache('image-invalidated', [english, portuguese], before='stale-image-signature')
assert db.execute('SELECT 1 FROM subtitle_image_cache_media WHERE path=?', (path,)).fetchone() is None
assert get_valid_media(path, 'image-invalidated') == [english, portuguese]

# Core indexing after cleanup cannot discard the cache just published by that
# writer. Partial valid data is retained too; only missing work stays pending.
reconcile = scope['reconcile_source_cache']
text_manifest = {item.key: item.codec for item in (english, portuguese)}
scope['enqueue_media'](path)
revision = scope['pending_revision'](path)
assert scope['cache_records_present'](path)
assert reconcile(path, 'image-invalidated', text_manifest, '', set(), revision)
assert get_valid_media(path, 'image-invalidated') == [english, portuguese]
assert scope['pending_revision'](path) is None
replacement(path, 'partial', keys, [english], image_before_signature='', image_after_signature='', expected_images=set())
assert not reconcile(path, 'partial', text_manifest, '', set(), None)
assert scope['get_valid_tracks'](path, 'partial') == [english]
revision = scope['pending_revision'](path)
assert not reconcile(path, 'partial', text_manifest, '', set(), revision)
assert scope['pending_revision'](path) == revision
scope['fail_pending_media'](path, 'old source failure', 1000, revision)
assert not reconcile(path, 'external-replacement', text_manifest, '', set(), revision)
assert not scope['cache_records_present'](path)
assert scope['pending_revision'](path) == revision + 1
assert db.execute('SELECT retry_after FROM subtitle_cache_pending WHERE path=?', (path,)).fetchone()[0] == 0
# A request arriving after identity capture must survive reconciliation.
publish_media(path, 'fresh', keys, [english, portuguese])
revision = scope['pending_revision'](path)
scope['enqueue_media'](path)
assert reconcile(path, 'fresh', text_manifest, '', set(), revision)
assert scope['pending_revision'](path) == revision + 1
# Image blobs are read neither for source-matching reconciliation nor rebinding.
db.execute('INSERT INTO subtitle_image_cache_media(path,source_signature,format_version,expected_tracks) VALUES(?,?,1,1)', (path, 'images-current'))
db.execute('INSERT INTO subtitle_image_cache_track(path,type_index,codec,payload,index_payload,sha256) VALUES(?,2,?,?,?,?)', (path, 'pgs', b'image', b'', hashlib.sha256(b'image').hexdigest()))
assert reconcile(path, 'fresh', text_manifest, 'images-current', images, scope['pending_revision'](path))
assert db.execute('SELECT payload FROM subtitle_image_cache_track WHERE path=?', (path,)).fetchone()[0] == b'image'
assert not reconcile(path, 'fresh', text_manifest, 'stale-image', images, None)
assert get_valid_media(path, 'fresh') == [english, portuguese]
assert db.execute('SELECT 1 FROM subtitle_image_cache_media WHERE path=?', (path,)).fetchone() is None
assert reconcile(path, 'image-only', {}, 'stale-image', set(), scope['pending_revision'](path))
assert get_valid_media(path, 'image-only') == []

print("PASS: complete/partial atomic cache publication, image retention, current-cache index reconciliation, corruption rejection, invalidation, and generation-safe priority recache")
