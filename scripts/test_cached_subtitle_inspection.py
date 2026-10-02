"""Background subtitle inspection must never extract uncached subtitle text."""

import ast
import sqlite3
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
INSERT INTO index_task_queue VALUES(1,'subtitles','/uncached','pending',NULL);
INSERT INTO index_task_queue VALUES(2,'subtitles','/partial','pending',NULL);
INSERT INTO index_task_queue VALUES(3,'subtitles','/ready','pending',NULL);
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
assert ready == [(3,)], ready
print("PASS: cached inspection does not extract; uncached and partial items wait in queue")
