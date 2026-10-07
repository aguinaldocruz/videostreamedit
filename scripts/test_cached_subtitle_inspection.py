"""Background subtitle inspection must never extract uncached subtitle text."""

import ast
import sqlite3
import sys
import types
from pathlib import Path


root = Path(__file__).resolve().parents[1]
tree = ast.parse((root / "app/v51.py").read_text())
nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in {
    "SubtitleCachePending", "inspect_extended",
}]
scope = {
    "Path": Path,
    "TEXT_SUBTITLE_CODECS": {"subrip"},
    "probe": lambda _: (_ for _ in ()).throw(AssertionError("Cached inspection must reuse supplied metadata")),
    "extracted_text": lambda *_: (_ for _ in ()).throw(AssertionError("No subtitle extraction is allowed")),
    "decode_external": lambda *_: (_ for _ in ()).throw(AssertionError("No sidecar read is allowed")),
    "markup_kind": lambda text: "HTML tags" if "<i>" in text else "None",
    "damage_kind": lambda _: "None",
    "external_subtitles": lambda _: [],
}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "app/v51.py", "exec"), scope)
media = Path("/not-needed-for-cached-test.mkv")
metadata = {"streams": [{"codec_type": "subtitle", "codec_name": "subrip"}]}
text = "1\n00:00:00,000 --> 00:00:01,000\n<i>Hello</i>\n"
rows = scope["inspect_extended"](media, {("embedded", 0): text}, cache_only=True, metadata=metadata)
assert len(rows) == 1 and rows[0][6] == "HTML tags"
try:
    scope["inspect_extended"](media, {}, cache_only=True, metadata=metadata)
except scope["SubtitleCachePending"]:
    pass
else:
    raise AssertionError("An uncached subtitle must be deferred")
scope["external_subtitles"] = lambda _: [{"path": "/subtitles/episode.srt", "codec": "srt"}]
external = scope["inspect_extended"](
    media, {("external", "/subtitles/episode.srt"): text}, cache_only=True, metadata={"streams": []},
)
assert len(external) == 1 and external[0][5] == "UTF-8 (cached)"
damage = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "damage_kind")
scope["re"] = __import__("re")
exec(compile(ast.Module(body=[damage], type_ignores=[]), "app/v51.py", "exec"), scope)
scope["external_subtitles"] = lambda _: []
empty_rows = scope["inspect_extended"](media, {("embedded", 0): ""}, cache_only=True, metadata=metadata)
assert "Empty subtitle track" in empty_rows[0][7]
legacy_rows = scope["inspect_extended"](
    media, {("embedded", 0): text, ("source_encoding", "embedded", 0): "Windows-1252 (inferred)"},
    cache_only=True, metadata=metadata,
)
assert "inferred" in legacy_rows[0][5] and "Non-UTF-8 source bytes" in legacy_rows[0][7]

wrapper = next(node for node in ast.parse((root / "app/v79.py").read_text()).body if
               isinstance(node, ast.FunctionDef) and node.name == "subtitle_index_with_sidecars")
calls = []
wrapper_scope = {
    "_is_final_version": lambda _: False,
    "_legacy_processors": {"subtitles": lambda item: calls.append(("inspect", item["_subtitle_text_cache"].copy()))},
    "persist_external_sidecars": lambda *_: calls.append(("sidecars", None)),
    "inspect_portuguese_language": lambda _path, _scope, cache, *, cache_only: calls.append(("language", cache.copy(), cache_only)),
}
exec(compile(ast.Module(body=[wrapper], type_ignores=[]), "app/v79.py", "exec"), wrapper_scope)
cached = {("embedded", 0): text}
wrapper_scope["subtitle_index_with_sidecars"]({"path": str(media), "_subtitle_cache_only": True,
                                                 "_subtitle_text_cache": cached})
assert calls[0] == ("inspect", cached) and calls[-1] == ("language", cached, True), calls

database = sqlite3.connect(":memory:")
database.executescript("""
CREATE TABLE index_task_queue(id INTEGER PRIMARY KEY,job TEXT,path TEXT,status TEXT,expedite_until TEXT);
CREATE TABLE subtitle_cache_media(path TEXT PRIMARY KEY,format_version INTEGER,expected_tracks INTEGER,cached_tracks INTEGER);
CREATE TABLE media_stream_index_state(path TEXT PRIMARY KEY);
CREATE TABLE media_stream_index(path TEXT,stream_type TEXT);
INSERT INTO index_task_queue VALUES(1,'subtitles','/uncached','pending',NULL);
INSERT INTO index_task_queue VALUES(2,'subtitles','/partial','pending',NULL);
INSERT INTO index_task_queue VALUES(3,'subtitles','/ready','pending',NULL);
INSERT INTO index_task_queue VALUES(4,'subtitles','/no-subtitles','pending',NULL);
INSERT INTO media_stream_index_state VALUES('/no-subtitles');
INSERT INTO media_stream_index_state VALUES('/uncached');
INSERT INTO media_stream_index VALUES('/uncached','subtitle');
INSERT INTO subtitle_cache_media VALUES('/partial',1,2,1);
INSERT INTO subtitle_cache_media VALUES('/ready',1,2,2);
""")
v80 = ast.parse((root / "app/v80.py").read_text())
assignment = next(node for node in v80.body if isinstance(node, ast.Assign) and any(
    isinstance(target, ast.Name) and target.id == "SUBTITLE_CACHE_READY_SQL" for target in node.targets))
ready_sql = ast.literal_eval(assignment.value)
ready = database.execute(
    "SELECT q.id FROM index_task_queue q WHERE q.job=? AND q.status='pending' AND " + ready_sql + " ORDER BY q.id",
    ("subtitles", 1),
).fetchall()
assert ready == [(3,), (4,)], ready

# A manifest with no text tracks can complete without a cache. A newly
# arrived sidecar, however, must still wait for a complete verified cache.
def stub_module(name, **values):
    module = types.ModuleType(name)
    module.__dict__.update(values)
    sys.modules[name] = module

manifest = []
stub_module('app.v2', probe=lambda _: {'streams': []})
stub_module('app.subtitle_cache_worker', text_track_manifest=lambda *_: ('fixture-source', manifest))
stub_module('app.subtitle_cache', get_valid_media=lambda *_: None)
verified = next(node for node in v80.body if isinstance(node, ast.FunctionDef)
                and node.name == '_verified_subtitle_inspection_cache')
verified_scope = {'Path': Path}
exec(compile(ast.Module(body=[verified], type_ignores=[]), 'cached-inspection-test', 'exec'), verified_scope)
assert verified_scope['_verified_subtitle_inspection_cache'](media) == ('fixture-source', {'streams': []}, {})
manifest.append(dict(source='external', type_index=-1, external_path='/new-sidecar.srt'))
assert verified_scope['_verified_subtitle_inspection_cache'](media) is None

cached_track = types.SimpleNamespace(text=text, extraction_revision=0)
sys.modules['app.subtitle_cache'].get_valid_track = lambda *_: cached_track
helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'cached_subtitle_text')
helper_scope = {'Path': Path}
exec(compile(ast.Module(body=[helper], type_ignores=[]), 'cached-input-test', 'exec'), helper_scope)
snapshot = ('fixture-source', {('embedded', 0, '')})
assert helper_scope['cached_subtitle_text'](media, 'embedded', 0, snapshot=snapshot) == text
assert helper_scope['cached_subtitle_text'](media, 'embedded', 0, snapshot=snapshot, mutation_input=True) is None
cached_track.extraction_revision = 2
assert helper_scope['cached_subtitle_text'](media, 'embedded', 0, snapshot=snapshot, mutation_input=True) == text
print("PASS: cached inspection never extracts; uncached/partial tracks wait; no-subtitle media completes without cache; new sidecars still require cache")
